from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
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

logger = logging.getLogger("openbrain.ideas_sheet_sync")

IDEAS_SHEET_NAME = "Ideas"
IDEAS_HEADERS = [
    "id", "title", "content", "status", "priority",
    "topic", "who", "new_task", "tasks", "parent_entry_id", "created_at", "Problem",
]
END_COL_LETTER = chr(ord("A") + len(IDEAS_HEADERS) - 1)  # "L"
IDEAS_SNAPSHOT_FIELDS = ["status", "priority", "title", "content", "topic", "who"]
IDEAS_TAB_NAME = "ideas"

VALID_STATUSES = {"open", "done", "archived"}
VALID_PRIORITIES = {1, 2, 3}

# 1-indexed column positions
COL = {h: i + 1 for i, h in enumerate(IDEAS_HEADERS)}


@dataclass
class IdeaSyncResult:
    updated: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    pushed: int = 0
    conflicts: list[int] = field(default_factory=list)

    def as_message(self) -> str:
        msg = f"Ideas synced: {self.updated} updated, {self.skipped} skipped"
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


def _open_ideas_worksheet(spreadsheet: gspread.Spreadsheet) -> gspread.Worksheet:
    try:
        return spreadsheet.worksheet(IDEAS_SHEET_NAME)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=IDEAS_SHEET_NAME, rows=500, cols=len(IDEAS_HEADERS))
        logger.info("Created new worksheet: %s", IDEAS_SHEET_NAME)
        return ws


def _apply_sheet_formatting(spreadsheet: gspread.Spreadsheet, worksheet: gspread.Worksheet) -> None:
    """Freeze header, dropdowns, content wrap/width, grey created_at, auto-filter."""
    sid = worksheet.id
    content_col_idx = COL["content"] - 1  # 0-indexed

    red = {"red": 0.96, "green": 0.72, "blue": 0.72}
    requests = [
        # Problem rows: sheet/DB conflict visible across the full row
        {
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [{
                        "sheetId": sid, "startRowIndex": 1, "endRowIndex": 500,
                        "startColumnIndex": 0, "endColumnIndex": len(IDEAS_HEADERS),
                    }],
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
        },
        # Expand grid to accommodate all columns BEFORE any repeatCell/validation requests
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sid,
                    "gridProperties": {"columnCount": len(IDEAS_HEADERS)},
                },
                "fields": "gridProperties.columnCount",
            }
        },
        # Clear ALL existing data validations before re-applying correct ones
        {
            "setDataValidation": {
                "range": {
                    "sheetId": sid, "startRowIndex": 0, "endRowIndex": 500,
                    "startColumnIndex": 0, "endColumnIndex": len(IDEAS_HEADERS),
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
        # Status dropdown: open / done / archived
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
        # new_task column: light yellow background (writable input cue)
        {
            "repeatCell": {
                "range": {
                    "sheetId": sid, "startRowIndex": 1, "endRowIndex": 500,
                    "startColumnIndex": COL["new_task"] - 1, "endColumnIndex": COL["new_task"],
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": {"red": 1.0, "green": 0.97, "blue": 0.80},
                    }
                },
                "fields": "userEnteredFormat.backgroundColor",
            }
        },
        # tasks column: grey background (read-only visual cue)
        {
            "repeatCell": {
                "range": {
                    "sheetId": sid, "startRowIndex": 1, "endRowIndex": 500,
                    "startColumnIndex": COL["tasks"] - 1, "endColumnIndex": COL["tasks"],
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
                        "endColumnIndex": len(IDEAS_HEADERS),
                    }
                }
            }
        },
    ]
    spreadsheet.batch_update({"requests": requests})
    logger.info("Applied formatting to Ideas tab")


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _fetch_ideas_from_db() -> list[dict[str, Any]]:
    with engine.begin() as conn:
        rows = conn.execute(
            text("""
                SELECT i.id, i.title, i.content, i.status, i.priority, i.topic, i.who,
                       i.parent_entry_id, i.created_at, i.sync_error,
                       COALESCE(t.task_count, 0) AS task_count,
                       t.latest_task_id,
                       t.latest_task_title
                FROM entries i
                LEFT JOIN LATERAL (
                    SELECT
                        COUNT(*) FILTER (WHERE status IS DISTINCT FROM 'archived') AS task_count,
                        (SELECT id FROM entries
                         WHERE type = 'task'
                           AND spawned_from_idea_id = i.id
                           AND status IS DISTINCT FROM 'archived'
                         ORDER BY created_at DESC
                         LIMIT 1) AS latest_task_id,
                        (SELECT title FROM entries
                         WHERE type = 'task'
                           AND spawned_from_idea_id = i.id
                           AND status IS DISTINCT FROM 'archived'
                         ORDER BY created_at DESC
                         LIMIT 1) AS latest_task_title
                    FROM entries
                    WHERE type = 'task' AND spawned_from_idea_id = i.id
                ) t ON true
                WHERE i.type = 'idea'
                ORDER BY
                    CASE i.status
                        WHEN 'open'     THEN 0
                        WHEN 'done'     THEN 1
                        WHEN 'archived' THEN 2
                        ELSE 3
                    END ASC,
                    i.priority ASC NULLS LAST,
                    i.created_at DESC
            """)
        ).mappings().all()
    return [dict(r) for r in rows]


def _row_to_sheet_values(idea: dict[str, Any]) -> list[str]:
    def fmt_dt(v: Any) -> str:
        if v is None:
            return ""
        if isinstance(v, datetime):
            return v.astimezone(APP_TZ).strftime("%Y-%m-%d %H:%M:%S")
        return str(v)

    count = idea.get("task_count") or 0
    latest_id = idea.get("latest_task_id")
    latest_title = idea.get("latest_task_title")
    if count == 0:
        tasks_cell = ""
    else:
        prefix = "1" if count == 1 else str(count)
        if latest_id and latest_title:
            tasks_cell = f"{prefix} · #{latest_id} {latest_title[:50]}"
        elif latest_id:
            tasks_cell = f"{prefix} · #{latest_id}"
        else:
            tasks_cell = prefix

    return [
        str(idea["id"]),
        idea.get("title") or "",
        idea.get("content") or "",
        idea.get("status") or "",
        str(idea["priority"]) if idea.get("priority") is not None else "",
        idea.get("topic") or "",
        idea.get("who") or "",
        "",
        tasks_cell,
        str(idea["parent_entry_id"]) if idea.get("parent_entry_id") is not None else "",
        fmt_dt(idea.get("created_at")),
        idea.get("sync_error") or "",
    ]


# ---------------------------------------------------------------------------
# Push: DB → Sheet
# ---------------------------------------------------------------------------

def _push_ideas_to_sheet(worksheet: gspread.Worksheet, ideas: list[dict[str, Any]]) -> int:
    worksheet.clear()
    rows = [IDEAS_HEADERS] + [_row_to_sheet_values(i) for i in ideas]
    worksheet.update(f"A1:{END_COL_LETTER}{len(rows)}", rows, value_input_option="RAW")
    with engine.begin() as conn:
        write_snapshots(conn, IDEAS_TAB_NAME, "entries", ideas, IDEAS_SNAPSHOT_FIELDS)
    logger.info("Pushed %d idea rows to Ideas tab", len(ideas))
    return len(ideas)


# ---------------------------------------------------------------------------
# Pull: Sheet → DB
# ---------------------------------------------------------------------------

def _pull_ideas_from_sheet(worksheet: gspread.Worksheet) -> IdeaSyncResult:
    """
    Read Ideas tab and write back user edits to the DB via 3-way merge.

    Editable fields: status, priority, title, content, topic, who.
    Read-only fields: id, parent_entry_id, created_at -- warn if changed, do not write back.
    All writes are in a single transaction.
    """
    result = IdeaSyncResult()
    values = worksheet.get_all_values()
    if not values or len(values) < 2:
        return result

    header = values[0]
    try:
        idx = {h: header.index(h) for h in IDEAS_HEADERS}
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
                SELECT i.id, i.title, i.content, i.status, i.priority, i.topic, i.who,
                       i.parent_entry_id, i.created_at, i.sync_error,
                       COALESCE(t.task_count, 0) AS task_count,
                       t.latest_task_id,
                       t.latest_task_title
                FROM entries i
                LEFT JOIN LATERAL (
                    SELECT
                        COUNT(*) FILTER (WHERE status IS DISTINCT FROM 'archived') AS task_count,
                        (SELECT id FROM entries
                         WHERE type = 'task'
                           AND spawned_from_idea_id = i.id
                           AND status IS DISTINCT FROM 'archived'
                         ORDER BY created_at DESC
                         LIMIT 1) AS latest_task_id,
                        (SELECT title FROM entries
                         WHERE type = 'task'
                           AND spawned_from_idea_id = i.id
                           AND status IS DISTINCT FROM 'archived'
                         ORDER BY created_at DESC
                         LIMIT 1) AS latest_task_title
                    FROM entries
                    WHERE type = 'task' AND spawned_from_idea_id = i.id
                ) t ON true
                WHERE i.type = 'idea'
            """)
        ).mappings().all()
        snapshots = fetch_snapshots(conn, IDEAS_TAB_NAME, row_ids)
    db_map: dict[int, dict[str, Any]] = {int(r["id"]): dict(r) for r in db_rows}

    pending_updates: list[dict[str, Any]] = []
    pending_task_creations: list[dict[str, Any]] = []
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
            logger.warning("Ideas tab row %d: id %d not in DB, skipping", row_num, entry_id)
            result.skipped += 1
            continue

        raw_parent = get("parent_entry_id")
        db_parent = str(db["parent_entry_id"]) if db["parent_entry_id"] is not None else ""
        if raw_parent != db_parent:
            logger.warning(
                "Row %d id=%d: parent_entry_id differs (sheet=%r db=%r) -- ignored",
                row_num, entry_id, raw_parent, db_parent,
            )

        raw_tasks = get("tasks")
        count = db.get("task_count") or 0
        latest_id = db.get("latest_task_id")
        latest_title = db.get("latest_task_title")
        if count == 0:
            expected_tasks = ""
        else:
            prefix = "1" if count == 1 else str(count)
            if latest_id and latest_title:
                expected_tasks = f"{prefix} · #{latest_id} {latest_title[:50]}"
            elif latest_id:
                expected_tasks = f"{prefix} · #{latest_id}"
            else:
                expected_tasks = prefix
        if raw_tasks != expected_tasks:
            logger.warning(
                "Row %d id=%d: tasks differs (sheet=%r db=%r) -- ignored",
                row_num, entry_id, raw_tasks, expected_tasks,
            )

        raw_new_task = get("new_task")
        if raw_new_task:
            pending_task_creations.append({
                "idea_id": entry_id,
                "title": raw_new_task[:200],
                "topic": db.get("topic"),
                "who": db.get("who"),
            })

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

        sheet_values = {
            "status": raw_status,
            "priority": parsed_priority,
            "title": get("title") or None,
            "content": get("content") or None,
            "topic": get("topic") or None,
            "who": get("who") or None,
        }
        db_values = {field: db.get(field) for field in IDEAS_SNAPSHOT_FIELDS}

        snapshot = snapshots.get(entry_id)
        if snapshot is None:
            continue

        decisions: dict[str, tuple[str, Any]] = {}
        conflict_fields: list[str] = []
        for field in IDEAS_SNAPSHOT_FIELDS:
            decision, value = three_way_resolve(field, snapshot.get(field), sheet_values[field], db_values[field])
            decisions[field] = (decision, value)
            if decision == "conflict":
                conflict_fields.append(field)

        if conflict_fields:
            msg = "; ".join(
                f"{field}: sheet={sheet_values[field]!r}, db={db_values[field]!r}"
                for field in conflict_fields
            )
            conflicts.append((entry_id, f"Ideas tab conflict -- {msg}"))
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
                for field in IDEAS_SNAPSHOT_FIELDS:
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
                        text(f"UPDATE entries SET {', '.join(set_clauses)} WHERE id = :id AND type = 'idea'"),
                        params,
                    )
            for entry_id in clear_ids:
                if any(conflict_id == entry_id for conflict_id, _ in conflicts):
                    continue
                clear_sync_error(conn, "entries", entry_id)

    result.updated = len(pending_updates)
    if pending_updates:
        logger.info("Reverse synced %d idea rows from sheet via 3-way merge", len(pending_updates))
    if conflicts:
        logger.warning("Skipped %d idea rows due to sheet/DB conflicts", len(conflicts))

    if pending_task_creations:
        with engine.begin() as conn:
            for nt in pending_task_creations:
                nt["who_ids"] = resolve_who_to_ids_safe(nt.get("who"), conn)
                new_task_id = conn.execute(
                    text("""
                        INSERT INTO entries (
                            type, status, title, content, topic, who, who_ids,
                            spawned_from_idea_id, source, created_at, updated_at
                        )
                        VALUES (
                            'task', 'open', :title, :title, :topic, :who, :who_ids,
                            :idea_id, 'sheet', NOW(), NOW()
                        )
                        RETURNING id
                    """),
                    nt,
                ).scalar()
                from app.entry_relations import insert_spawn_relation
                insert_spawn_relation(conn, new_task_id, nt["idea_id"], origin="ideas_sheet_sync")
                logger.info(
                    "Created task id=%d from idea id=%d via sheet new_task: %r",
                    new_task_id, nt["idea_id"], nt["title"],
                )
                conn.execute(
                    text("""
                        UPDATE entries SET status = 'open', updated_at = NOW()
                        WHERE id = :idea_id AND type = 'idea' AND status IS NULL
                    """),
                    {"idea_id": nt["idea_id"]},
                )
        logger.info("Created %d tasks from sheet new_task entries", len(pending_task_creations))
        result.updated += len(pending_task_creations)

    return result

# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def sync_ideas_tab() -> IdeaSyncResult:
    """
    Full Ideas tab sync: pull → push.

    Pull-first design: user edits in the sheet are captured into the DB
    before we overwrite the sheet with fresh DB state.  Push-first would
    wipe sheet edits before the pull step could read them.

    First-run path (wrong/missing headers): format + push only (nothing to pull).
    Normal path: pull (sheet → DB) → push (DB → sheet).

    Sync order: pull → push.
    Pull reads the sheet and writes diffs to DB; push then rewrites the sheet from fresh DB state.

    Known race condition (acceptable for single-user use):
    If the same field is edited via both Telegram and the sheet within the same poll window
    (default 10 min), the sheet edit wins and the Telegram edit is silently overwritten on
    the next pull. This is because pull compares sheet to live DB, not to the last-pushed
    snapshot. Mitigation deferred — see BACKLOG.md "Snapshot-based sync comparison."
    """
    _require_sync_config()
    spreadsheet = _open_spreadsheet()
    worksheet = _open_ideas_worksheet(spreadsheet)

    # Check if sheet needs initialisation
    try:
        existing_header = worksheet.row_values(1)
    except Exception:
        existing_header = []
    needs_init = (existing_header[: len(IDEAS_HEADERS)] != IDEAS_HEADERS
                   or len(existing_header) != len(IDEAS_HEADERS))

    if needs_init:
        _apply_sheet_formatting(spreadsheet, worksheet)

    # Step 1: pull user edits (skip on first run — sheet is empty/invalid)
    result = IdeaSyncResult()
    if not needs_init:
        result = _pull_ideas_from_sheet(worksheet)

    # Step 2: push fresh DB state (always)
    ideas = _fetch_ideas_from_db()
    result.pushed = _push_ideas_to_sheet(worksheet, ideas)

    return result
