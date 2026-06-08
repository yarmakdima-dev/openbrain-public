from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import gspread
from google.oauth2.service_account import Credentials
from sqlalchemy import text

from app.entity_resolver import resolve_who_to_ids_safe
from app.sheet_sync_common import (
    clear_sync_error,
    fetch_snapshots,
    set_sync_error,
    three_way_resolve,
    write_snapshots,
)
from app.sheets_sync import (
    APP_TZ,
    GOOGLE_SHEETS_CREDENTIALS_PATH,
    GOOGLE_SHEET_URL,
    SCOPES,
    _require_sync_config,
    engine,
)

logger = logging.getLogger("openbrain.tasks_sheet_sync")

TASKS_SHEET_NAME = "Tasks"
TASKS_FIELDS = [
    "id", "title", "content", "status", "priority",
    "topic", "who", "parent_entry_id", "parent_idea", "due_date", "created_at", "sync_error",
]
TASKS_HEADERS = [
    "id", "title", "content", "status", "priority",
    "topic", "who", "parent_entry_id", "parent_idea", "Due", "created_at", "Problem",
]
END_COL_LETTER = chr(ord("A") + len(TASKS_HEADERS) - 1)  # "L"
TASKS_SNAPSHOT_FIELDS = ["status", "priority", "due_date", "title", "content", "topic", "who"]
TASKS_TAB_NAME = "tasks"

VALID_STATUSES = {"open", "done", "archived"}
VALID_PRIORITIES = {1, 2, 3}

# 1-indexed column positions keyed by DB/internal field name.
COL = {field: i + 1 for i, field in enumerate(TASKS_FIELDS)}


@dataclass
class TaskSyncResult:
    updated: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    pushed: int = 0
    conflicts: list[int] = field(default_factory=list)

    def as_message(self) -> str:
        msg = f"Tasks synced: {self.updated} updated, {self.skipped} skipped"
        if self.errors:
            msg += f"\nWarnings: {'; '.join(self.errors[:3])}"
        return msg


# ---------------------------------------------------------------------------
# Sheet connection helpers
# ---------------------------------------------------------------------------

def _open_spreadsheet() -> gspread.Spreadsheet:
    _require_sync_config()
    creds = Credentials.from_service_account_file(GOOGLE_SHEETS_CREDENTIALS_PATH, scopes=SCOPES)
    client = gspread.authorize(creds)
    return client.open_by_url(GOOGLE_SHEET_URL)


def _open_tasks_worksheet(spreadsheet: gspread.Spreadsheet) -> gspread.Worksheet:
    try:
        return spreadsheet.worksheet(TASKS_SHEET_NAME)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=TASKS_SHEET_NAME, rows=500, cols=len(TASKS_HEADERS))
        logger.info("Created new worksheet: %s", TASKS_SHEET_NAME)
        return ws


def _conditional_format_delete_requests(
    spreadsheet: gspread.Spreadsheet,
    sheet_id: int,
) -> list[dict[str, Any]]:
    metadata = spreadsheet.fetch_sheet_metadata(
        params={"fields": "sheets(properties(sheetId),conditionalFormats)"}
    )
    for sheet in metadata.get("sheets", []):
        if sheet.get("properties", {}).get("sheetId") != sheet_id:
            continue
        rule_count = len(sheet.get("conditionalFormats", []))
        return [
            {
                "deleteConditionalFormatRule": {
                    "sheetId": sheet_id,
                    "index": idx,
                }
            }
            for idx in reversed(range(rule_count))
        ]
    return []


def _due_date_conditional_format_requests(sheet_id: int) -> list[dict[str, Any]]:
    due_range = {
        "sheetId": sheet_id,
        "startRowIndex": 1,
        "endRowIndex": 500,
        "startColumnIndex": COL["due_date"] - 1,
        "endColumnIndex": COL["due_date"],
    }

    def rule(formula: str, color: dict[str, float], index: int) -> dict[str, Any]:
        return {
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [due_range],
                    "booleanRule": {
                        "condition": {
                            "type": "CUSTOM_FORMULA",
                            "values": [{"userEnteredValue": formula}],
                        },
                        "format": {"backgroundColor": color},
                    },
                },
                "index": index,
            }
        }

    white = {"red": 1.0, "green": 1.0, "blue": 1.0}
    green = {"red": 0.82, "green": 0.94, "blue": 0.82}
    yellow = {"red": 1.0, "green": 0.94, "blue": 0.65}
    red = {"red": 0.96, "green": 0.72, "blue": 0.72}

    problem_range = {
        "sheetId": sheet_id,
        "startRowIndex": 1,
        "endRowIndex": 500,
        "startColumnIndex": 0,
        "endColumnIndex": len(TASKS_HEADERS),
    }
    problem_rule = {
        "addConditionalFormatRule": {
            "rule": {
                "ranges": [problem_range],
                "booleanRule": {
                    "condition": {
                        "type": "CUSTOM_FORMULA",
                        "values": [{"userEnteredValue": f'=${END_COL_LETTER}2<>""'}],
                    },
                    "format": {"backgroundColor": red},
                },
            },
            "index": 0,
        }
    }

    return [
        problem_rule,
        rule('=$D2="done"', white, 1),
        rule('=AND($D2<>"done",$J2<>"",$J2>=TODAY(),$J2<=TODAY()+7)', green, 2),
        rule('=AND($D2<>"done",$J2<>"",$J2>=TODAY()-7,$J2<TODAY())', yellow, 3),
        rule('=AND($D2<>"done",$J2<>"",$J2<TODAY()-7)', red, 4),
        rule('=AND($D2<>"done",$J2="")', red, 5),
    ]


def _apply_sheet_formatting(spreadsheet: gspread.Spreadsheet, worksheet: gspread.Worksheet) -> None:
    """Freeze header, validations, widths, date formats, filters, and due-date color rules."""
    sid = worksheet.id
    content_col_idx = COL["content"] - 1  # 0-indexed

    requests = _conditional_format_delete_requests(spreadsheet, sid) + [
        # Expand grid to accommodate all columns BEFORE any repeatCell/validation requests
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sid,
                    "gridProperties": {"columnCount": len(TASKS_HEADERS)},
                },
                "fields": "gridProperties.columnCount",
            }
        },
        # Clear ALL existing data validations before re-applying correct ones
        {
            "setDataValidation": {
                "range": {
                    "sheetId": sid, "startRowIndex": 1, "endRowIndex": 500,
                    "startColumnIndex": 0, "endColumnIndex": len(TASKS_HEADERS),
                },
                "rule": None,
            }
        },
        # Freeze header row
        {
            "updateSheetProperties": {
                "properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 1}},
                "fields": "gridProperties.frozenRowCount",
            }
        },
        # Status dropdown: open / done
        {
            "setDataValidation": {
                "range": {
                    "sheetId": sid, "startRowIndex": 1, "endRowIndex": 500,
                    "startColumnIndex": COL["status"] - 1, "endColumnIndex": COL["status"],
                },
                "rule": {
                    "condition": {
                        "type": "ONE_OF_LIST",
                        "values": [{"userEnteredValue": v} for v in ["open", "done", "archived"]],
                    },
                    "showCustomUi": True, "strict": True,
                },
            }
        },
        # Priority dropdown: 1 / 2 / 3 (blank allowed)
        {
            "setDataValidation": {
                "range": {
                    "sheetId": sid, "startRowIndex": 1, "endRowIndex": 500,
                    "startColumnIndex": COL["priority"] - 1, "endColumnIndex": COL["priority"],
                },
                "rule": {
                    "condition": {
                        "type": "ONE_OF_LIST",
                        "values": [{"userEnteredValue": v} for v in ["1", "2", "3"]],
                    },
                    "showCustomUi": True, "strict": False,
                },
            }
        },
        # content column: wrap text
        {
            "repeatCell": {
                "range": {
                    "sheetId": sid, "startRowIndex": 1, "endRowIndex": 500,
                    "startColumnIndex": content_col_idx, "endColumnIndex": content_col_idx + 1,
                },
                "cell": {
                    "userEnteredFormat": {
                        "wrapStrategy": "WRAP",
                    }
                },
                "fields": "userEnteredFormat.wrapStrategy",
            }
        },
        # content column: wider (400px)
        {
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sid,
                    "dimension": "COLUMNS",
                    "startIndex": content_col_idx,
                    "endIndex": content_col_idx + 1,
                },
                "properties": {"pixelSize": 400},
                "fields": "pixelSize",
            }
        },
        # Due column: DD-MM-YY date format
        {
            "repeatCell": {
                "range": {
                    "sheetId": sid, "startRowIndex": 1, "endRowIndex": 500,
                    "startColumnIndex": COL["due_date"] - 1, "endColumnIndex": COL["due_date"],
                },
                "cell": {"userEnteredFormat": {"numberFormat": {"type": "DATE", "pattern": "dd-mm-yy"}}},
                "fields": "userEnteredFormat.numberFormat",
            }
        },
        # created_at: grey background + datetime format (read-only visual cue)
        {
            "repeatCell": {
                "range": {
                    "sheetId": sid, "startRowIndex": 1, "endRowIndex": 500,
                    "startColumnIndex": COL["created_at"] - 1, "endColumnIndex": COL["created_at"],
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": {"red": 0.88, "green": 0.88, "blue": 0.88},
                        "numberFormat": {"type": "DATE_TIME", "pattern": "yyyy-mm-dd hh:mm"},
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,numberFormat)",
            }
        },
        # parent_idea: grey background (read-only visual cue)
        {
            "repeatCell": {
                "range": {
                    "sheetId": sid, "startRowIndex": 1, "endRowIndex": 500,
                    "startColumnIndex": COL["parent_idea"] - 1, "endColumnIndex": COL["parent_idea"],
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": {"red": 0.88, "green": 0.88, "blue": 0.88},
                    }
                },
                "fields": "userEnteredFormat.backgroundColor",
            }
        },
        # Auto-filter on all columns
        {
            "setBasicFilter": {
                "filter": {
                    "range": {
                        "sheetId": sid,
                        "startRowIndex": 0, "startColumnIndex": 0,
                        "endColumnIndex": len(TASKS_HEADERS),
                    }
                }
            }
        },
    ] + _due_date_conditional_format_requests(sid)
    spreadsheet.batch_update({"requests": requests})
    logger.info("Applied formatting to Tasks tab")


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _fetch_tasks_from_db() -> list[dict[str, Any]]:
    with engine.begin() as conn:
        rows = conn.execute(
            text("""
                SELECT t.id, t.title, t.content, t.status, t.priority, t.topic, t.who,
                       t.parent_entry_id, t.spawned_from_idea_id,
                       i.title AS parent_idea_title,
                       t.due_date, t.created_at, t.sync_error
                FROM entries t
                LEFT JOIN entries i ON i.id = t.spawned_from_idea_id AND i.type = 'idea'
                WHERE t.type = 'task'
                ORDER BY
                    CASE t.status WHEN 'open' THEN 0 WHEN 'done' THEN 1 ELSE 2 END ASC,
                    t.priority ASC NULLS LAST,
                    t.due_date ASC NULLS LAST
            """)
        ).mappings().all()
    return [dict(r) for r in rows]


def _row_to_sheet_values(task: dict[str, Any]) -> list[str]:
    def fmt_date(v: Any) -> str:
        if v is None:
            return ""
        if isinstance(v, datetime):
            return v.date().strftime("%d-%m-%y")
        if isinstance(v, date):
            return v.strftime("%d-%m-%y")
        return str(v)

    def fmt_dt(v: Any) -> str:
        if v is None:
            return ""
        if isinstance(v, datetime):
            return v.astimezone(APP_TZ).strftime("%Y-%m-%d %H:%M:%S")
        return str(v)

    if task.get("spawned_from_idea_id") and task.get("parent_idea_title"):
        parent_idea_cell = f"{task['spawned_from_idea_id']} · {task['parent_idea_title'][:60]}"
    else:
        parent_idea_cell = ""

    return [
        str(task["id"]),
        task.get("title") or "",
        task.get("content") or "",
        task.get("status") or "",
        str(task["priority"]) if task.get("priority") is not None else "",
        task.get("topic") or "",
        task.get("who") or "",
        str(task["parent_entry_id"]) if task.get("parent_entry_id") is not None else "",
        parent_idea_cell,
        fmt_date(task.get("due_date")),
        fmt_dt(task.get("created_at")),
        task.get("sync_error") or "",
    ]


# ---------------------------------------------------------------------------
# Push: DB → Sheet
# ---------------------------------------------------------------------------

def _push_tasks_to_sheet(worksheet: gspread.Worksheet, tasks: list[dict[str, Any]]) -> int:
    worksheet.clear()
    rows = [TASKS_HEADERS] + [_row_to_sheet_values(t) for t in tasks]
    due_col_idx = COL["due_date"] - 1
    created_at_col = chr(ord("A") + COL["created_at"] - 1)
    raw_left_rows = [row[:due_col_idx] for row in rows]
    due_rows = [[row[due_col_idx]] for row in rows]
    raw_right_rows = [row[COL["created_at"] - 1:] for row in rows]

    worksheet.update(f"A1:I{len(rows)}", raw_left_rows, value_input_option="RAW")
    worksheet.update(f"J1:J{len(rows)}", due_rows, value_input_option="USER_ENTERED")
    worksheet.update(f"{created_at_col}1:{END_COL_LETTER}{len(rows)}", raw_right_rows, value_input_option="RAW")
    with engine.begin() as conn:
        write_snapshots(conn, TASKS_TAB_NAME, "entries", tasks, TASKS_SNAPSHOT_FIELDS)
    logger.info("Pushed %d task rows to Tasks tab", len(tasks))
    return len(tasks)


# ---------------------------------------------------------------------------
# Pull: Sheet → DB
# ---------------------------------------------------------------------------

def _parse_date(raw: str) -> date | None:
    try:
        parsed = datetime.strptime(raw, "%d-%m-%y")
    except ValueError:
        return None
    if parsed.strftime("%d-%m-%y") != raw:
        return None
    return parsed.date()


def _pull_tasks_from_sheet(worksheet: gspread.Worksheet) -> TaskSyncResult:
    """
    Read Tasks tab and write back user edits to the DB via 3-way merge.

    Editable fields: status, due_date, priority, title, content, topic, who.
    Read-only fields: id, parent_entry_id, created_at -- warn if changed, do not write back.
    All writes are in a single transaction.
    """
    result = TaskSyncResult()
    values = worksheet.get_all_values()
    if not values or len(values) < 2:
        return result

    header = values[0]
    try:
        idx = {field: header.index(label) for field, label in zip(TASKS_FIELDS, TASKS_HEADERS)}
    except ValueError as e:
        result.errors.append(f"Header mismatch: {e}")
        return result

    sheet_rows = values[1:]
    row_ids: list[int] = []
    for row in sheet_rows:
        raw_id = row[idx["id"]].strip() if idx["id"] < len(row) else ""
        if raw_id.isdigit():
            row_ids.append(int(raw_id))

    with engine.begin() as conn:
        db_rows = conn.execute(
            text("""
                SELECT id, title, content, status, due_date, priority, topic, who,
                       parent_entry_id, spawned_from_idea_id, created_at, sync_error
                FROM entries WHERE type = 'task'
            """)
        ).mappings().all()
        snapshots = fetch_snapshots(conn, TASKS_TAB_NAME, row_ids)
    db_map: dict[int, dict[str, Any]] = {int(r["id"]): dict(r) for r in db_rows}

    pending_updates: list[dict[str, Any]] = []
    conflicts: list[tuple[int, str]] = []
    clear_ids: set[int] = set()

    for row_num, row in enumerate(sheet_rows, start=2):
        def get(col: str) -> str:
            i = idx[col]
            return row[i].strip() if i < len(row) else ""

        raw_id = get("id")
        if not raw_id.isdigit():
            continue
        entry_id = int(raw_id)
        db = db_map.get(entry_id)
        if not db:
            logger.warning("Tasks tab row %d: id %d not in DB, skipping", row_num, entry_id)
            result.skipped += 1
            continue

        raw_parent = get("parent_entry_id")
        db_parent = str(db["parent_entry_id"]) if db["parent_entry_id"] is not None else ""
        if raw_parent != db_parent:
            logger.warning("Row %d id=%d: parent_entry_id differs (sheet=%r db=%r) -- ignored", row_num, entry_id, raw_parent, db_parent)

        raw_parent_idea = get("parent_idea")
        db_spawned = db.get("spawned_from_idea_id")
        if db_spawned is not None:
            expected_id_str = str(db_spawned)
            sheet_id_str = raw_parent_idea.split(" · ")[0].strip() if raw_parent_idea else ""
            if sheet_id_str != expected_id_str:
                logger.warning(
                    "Row %d id=%d: parent_idea differs (sheet=%r db_spawned=%r) -- ignored",
                    row_num, entry_id, raw_parent_idea, db_spawned,
                )
        elif raw_parent_idea:
            logger.warning(
                "Row %d id=%d: parent_idea set in sheet but not in DB (sheet=%r) -- ignored",
                row_num, entry_id, raw_parent_idea,
            )

        raw_status = get("status") or None
        if raw_status is not None and raw_status not in VALID_STATUSES:
            msg = f"Row {row_num} id={entry_id}: invalid status {raw_status!r}"
            logger.warning(msg)
            result.errors.append(msg)
            result.skipped += 1
            continue

        raw_priority_str = get("priority")
        parsed_priority: int | None = None
        if raw_priority_str:
            try:
                parsed_priority = int(raw_priority_str)
                if parsed_priority not in VALID_PRIORITIES:
                    raise ValueError("out of range")
            except ValueError:
                msg = f"Row {row_num} id={entry_id}: invalid priority {raw_priority_str!r}"
                logger.warning(msg)
                result.errors.append(msg)
                result.skipped += 1
                continue

        raw_due = get("due_date")
        parsed_due: date | None = None
        if raw_due:
            parsed_due = _parse_date(raw_due)
            if parsed_due is None:
                msg = f"Row {row_num} id={entry_id}: unparseable due_date {raw_due!r}"
                logger.warning(msg)
                result.errors.append(msg)
                result.skipped += 1
                continue

        sheet_values = {
            "status": raw_status,
            "priority": parsed_priority,
            "due_date": parsed_due,
            "title": get("title") or None,
            "content": get("content") or None,
            "topic": get("topic") or None,
            "who": get("who") or None,
        }
        db_values = {field: db.get(field) for field in TASKS_SNAPSHOT_FIELDS}
        if isinstance(db_values["due_date"], datetime):
            db_values["due_date"] = db_values["due_date"].date()

        snapshot = snapshots.get(entry_id)
        if snapshot is None:
            continue

        decisions: dict[str, tuple[str, Any]] = {}
        conflict_fields: list[str] = []
        for field in TASKS_SNAPSHOT_FIELDS:
            decision, value = three_way_resolve(field, snapshot.get(field), sheet_values[field], db_values[field])
            decisions[field] = (decision, value)
            if decision == "conflict":
                conflict_fields.append(field)

        if conflict_fields:
            msg = "; ".join(
                f"{field}: sheet={sheet_values[field]!r}, db={db_values[field]!r}"
                for field in conflict_fields
            )
            conflicts.append((entry_id, f"Tasks tab conflict -- {msg}"))
            result.conflicts.append(entry_id)
            result.skipped += 1
            continue

        clear_ids.add(entry_id)
        sheet_wins = {field: value for field, (decision, value) in decisions.items() if decision == "take_sheet"}
        if sheet_wins:
            pending_updates.append({"id": entry_id, **sheet_wins})

    if pending_updates or conflicts or clear_ids:
        with engine.begin() as conn:
            for entry_id, message in conflicts:
                set_sync_error(conn, "entries", entry_id, message)
            for update_values in pending_updates:
                set_clauses: list[str] = []
                params = {"id": update_values["id"]}
                for field in TASKS_SNAPSHOT_FIELDS:
                    if field not in update_values:
                        continue
                    params[field] = update_values[field]
                    set_clauses.append(f"{field} = :{field}")
                if "who" in update_values:
                    params["who_ids"] = resolve_who_to_ids_safe(update_values.get("who"), conn)
                    set_clauses.append("who_ids = :who_ids")
                if set_clauses:
                    set_clauses.append("updated_at = NOW()")
                    conn.execute(
                        text(f"UPDATE entries SET {', '.join(set_clauses)} WHERE id = :id AND type = 'task'"),
                        params,
                    )
            for entry_id in clear_ids:
                if any(conflict_id == entry_id for conflict_id, _ in conflicts):
                    continue
                clear_sync_error(conn, "entries", entry_id)

    result.updated = len(pending_updates)
    if pending_updates:
        logger.info("Reverse synced %d task rows from sheet via 3-way merge", len(pending_updates))
    if conflicts:
        logger.warning("Skipped %d task rows due to sheet/DB conflicts", len(conflicts))
    return result

# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def sync_tasks_tab() -> TaskSyncResult:
    """
    Full Tasks tab sync: pull -> push.

    Pull-first design: user edits in the sheet are captured into the DB
    before we overwrite the sheet with fresh DB state.  Push-first would
    wipe sheet edits before the pull step could read them.

    First-run path (wrong/missing headers): format + push only (nothing to pull).
    Normal path: pull (sheet -> DB) -> push (DB -> sheet).

    Sync order: pull -> push.
    Pull reads the sheet and writes diffs to DB; push then rewrites the sheet from fresh DB state.

    Known race condition (acceptable for single-user use):
    If the same field is edited via both Telegram and the sheet within the same poll window
    (default 10 min), the sheet edit wins and the Telegram edit is silently overwritten on
    the next pull. This is because pull compares sheet to live DB, not to the last-pushed
    snapshot. Mitigation deferred -- see BACKLOG.md "Snapshot-based sync comparison."
    """
    _require_sync_config()
    spreadsheet = _open_spreadsheet()
    worksheet = _open_tasks_worksheet(spreadsheet)

    # Check if sheet needs initialisation
    try:
        existing_header = worksheet.row_values(1)
    except Exception:
        existing_header = []
    needs_init = (existing_header[: len(TASKS_HEADERS)] != TASKS_HEADERS
                   or len(existing_header) != len(TASKS_HEADERS))

    _apply_sheet_formatting(spreadsheet, worksheet)

    # Step 1: pull user edits (skip on first run -- sheet is empty/invalid)
    result = TaskSyncResult()
    if not needs_init:
        result = _pull_tasks_from_sheet(worksheet)

    # Step 2: push fresh DB state (always)
    tasks = _fetch_tasks_from_db()
    result.pushed = _push_tasks_to_sheet(worksheet, tasks)

    return result
