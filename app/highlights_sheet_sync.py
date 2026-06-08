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

logger = logging.getLogger("openbrain.highlights_sheet_sync")

HIGHLIGHTS_SHEET_NAME = "Highlights"
HIGHLIGHTS_HEADERS = ["id", "title", "content", "topic", "who", "created_at", "Problem"]
END_COL_LETTER = chr(ord("A") + len(HIGHLIGHTS_HEADERS) - 1)  # "G"
HIGHLIGHTS_SNAPSHOT_FIELDS = ["title", "content", "topic", "who"]
HIGHLIGHTS_TAB_NAME = "highlights"

# 1-indexed column positions
COL = {h: i + 1 for i, h in enumerate(HIGHLIGHTS_HEADERS)}


@dataclass
class HighlightSyncResult:
    updated: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    pushed: int = 0
    conflicts: list[int] = field(default_factory=list)

    def as_message(self) -> str:
        msg = f"Highlights synced: {self.updated} updated, {self.skipped} skipped"
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


def _open_highlights_worksheet(spreadsheet: gspread.Spreadsheet) -> gspread.Worksheet:
    try:
        return spreadsheet.worksheet(HIGHLIGHTS_SHEET_NAME)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=HIGHLIGHTS_SHEET_NAME, rows=500, cols=len(HIGHLIGHTS_HEADERS))
        logger.info("Created new worksheet: %s", HIGHLIGHTS_SHEET_NAME)
        return ws


def _apply_sheet_formatting(spreadsheet: gspread.Spreadsheet, worksheet: gspread.Worksheet) -> None:
    """Freeze header, widths, read-only cues, and filters."""
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
                        "startColumnIndex": 0, "endColumnIndex": len(HIGHLIGHTS_HEADERS),
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
                    "gridProperties": {"columnCount": len(HIGHLIGHTS_HEADERS)},
                },
                "fields": "gridProperties.columnCount",
            }
        },
        # Clear ALL existing data validations before re-applying correct ones
        {
            "setDataValidation": {
                "range": {
                    "sheetId": sid, "startRowIndex": 0, "endRowIndex": 500,
                    "startColumnIndex": 0, "endColumnIndex": len(HIGHLIGHTS_HEADERS),
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
        # Header style
        {
            "repeatCell": {
                "range": {
                    "sheetId": sid, "startRowIndex": 0, "endRowIndex": 1,
                    "startColumnIndex": 0, "endColumnIndex": len(HIGHLIGHTS_HEADERS),
                },
                "cell": {
                    "userEnteredFormat": {
                        "textFormat": {"bold": True},
                        "backgroundColor": {"red": 0.92, "green": 0.92, "blue": 0.92},
                    }
                },
                "fields": "userEnteredFormat(textFormat,backgroundColor)",
            }
        },
        # Wrap content
        {
            "repeatCell": {
                "range": {
                    "sheetId": sid, "startRowIndex": 1, "endRowIndex": 500,
                    "startColumnIndex": content_col_idx, "endColumnIndex": content_col_idx + 1,
                },
                "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP"}},
                "fields": "userEnteredFormat.wrapStrategy",
            }
        },
        # created_at / Problem: grey background as read-only visual cue
        {
            "repeatCell": {
                "range": {
                    "sheetId": sid, "startRowIndex": 1, "endRowIndex": 500,
                    "startColumnIndex": COL["created_at"] - 1, "endColumnIndex": COL["Problem"],
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": {"red": 0.94, "green": 0.94, "blue": 0.94},
                    }
                },
                "fields": "userEnteredFormat.backgroundColor",
            }
        },
    ]

    widths = {
        "id": 70,
        "title": 180,
        "content": 420,
        "topic": 140,
        "who": 160,
        "created_at": 150,
        "Problem": 260,
    }
    for col_name, px in widths.items():
        col_idx = COL[col_name] - 1
        requests.append({
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sid,
                    "dimension": "COLUMNS",
                    "startIndex": col_idx,
                    "endIndex": col_idx + 1,
                },
                "properties": {"pixelSize": px},
                "fields": "pixelSize",
            }
        })

    requests.append({
        "setBasicFilter": {
            "filter": {
                "range": {
                    "sheetId": sid,
                    "startRowIndex": 0,
                    "endRowIndex": 500,
                    "startColumnIndex": 0,
                    "endColumnIndex": len(HIGHLIGHTS_HEADERS),
                }
            }
        }
    })

    spreadsheet.batch_update({"requests": requests})


# ---------------------------------------------------------------------------
# Fetch / format
# ---------------------------------------------------------------------------

def _fetch_highlights_from_db() -> list[dict[str, Any]]:
    with engine.begin() as conn:
        rows = conn.execute(
            text("""
                SELECT id, title, content, topic, who, created_at, sync_error
                FROM entries
                WHERE type = 'highlight'
                ORDER BY
                    created_at DESC NULLS LAST,
                    id ASC
            """)
        ).mappings().all()
    return [dict(r) for r in rows]


def _format_dt(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.astimezone(APP_TZ).strftime("%Y-%m-%d %H:%M:%S")
    return str(v)


def _row_to_sheet_values(highlight: dict[str, Any]) -> list[str]:
    return [
        str(highlight["id"]),
        highlight.get("title") or "",
        highlight.get("content") or "",
        highlight.get("topic") or "",
        highlight.get("who") or "",
        _format_dt(highlight.get("created_at")),
        highlight.get("sync_error") or "",
    ]


# ---------------------------------------------------------------------------
# Push: DB -> Sheet
# ---------------------------------------------------------------------------

def _push_highlights_to_sheet(worksheet: gspread.Worksheet, highlights: list[dict[str, Any]]) -> int:
    worksheet.clear()
    rows = [HIGHLIGHTS_HEADERS] + [_row_to_sheet_values(b) for b in highlights]
    worksheet.update(f"A1:{END_COL_LETTER}{len(rows)}", rows, value_input_option="RAW")
    with engine.begin() as conn:
        write_snapshots(conn, HIGHLIGHTS_TAB_NAME, "entries", highlights, HIGHLIGHTS_SNAPSHOT_FIELDS)
    logger.info("Pushed %d highlight rows to Highlights tab", len(highlights))
    return len(highlights)


# ---------------------------------------------------------------------------
# Pull: Sheet -> DB
# ---------------------------------------------------------------------------

def _pull_highlights_from_sheet(worksheet: gspread.Worksheet) -> HighlightSyncResult:
    """
    Read Highlights tab and write back user edits to the DB via 3-way merge.

    Editable fields: title, content, topic, who.
    Read-only fields: id, created_at, sync_error.
    All writes are in a single transaction.
    """
    result = HighlightSyncResult()
    values = worksheet.get_all_values()
    if not values or len(values) < 2:
        return result

    header = values[0]
    try:
        idx = {h: header.index(h) for h in HIGHLIGHTS_HEADERS}
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
                SELECT id, title, content, topic, who, created_at, sync_error
                FROM entries WHERE type = 'highlight'
            """)
        ).mappings().all()
        snapshots = fetch_snapshots(conn, HIGHLIGHTS_TAB_NAME, row_ids)
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
            logger.warning("Highlights tab row %d: id %d not in DB, skipping", row_num, entry_id)
            result.skipped += 1
            continue

        read_only_expectations = {
            "created_at": _format_dt(db.get("created_at")),
            "Problem": db.get("sync_error") or "",
        }
        for col, expected in read_only_expectations.items():
            raw_value = get(col)
            if raw_value != expected:
                logger.warning(
                    "Row %d id=%d: %s differs (sheet=%r db=%r) -- ignored",
                    row_num, entry_id, col, raw_value, expected,
                )

        sheet_values = {
            "title": get("title") or None,
            "content": get("content") or None,
            "topic": get("topic") or None,
            "who": get("who") or None,
        }
        db_values = {field: db.get(field) for field in HIGHLIGHTS_SNAPSHOT_FIELDS}

        snapshot = snapshots.get(entry_id)
        if snapshot is None:
            continue

        decisions: dict[str, tuple[str, Any]] = {}
        conflict_fields: list[str] = []
        for field in HIGHLIGHTS_SNAPSHOT_FIELDS:
            decision, value = three_way_resolve(field, snapshot.get(field), sheet_values[field], db_values[field])
            decisions[field] = (decision, value)
            if decision == "conflict":
                conflict_fields.append(field)

        if conflict_fields:
            msg = "; ".join(
                f"{field}: sheet={sheet_values[field]!r}, db={db_values[field]!r}"
                for field in conflict_fields
            )
            conflicts.append((entry_id, f"Highlights tab conflict -- {msg}"))
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
                for field in HIGHLIGHTS_SNAPSHOT_FIELDS:
                    if field not in update_values:
                        continue
                    params[field] = update_values[field]
                    set_clauses.append(f"{field} = :{field}")
                if "who" in update_values:
                    params["who_ids"] = resolve_who_to_ids_safe(update_values.get("who"), conn)
                    set_clauses.append("who_ids = :who_ids")
                if set_clauses:
                    conn.execute(
                        text(f"UPDATE entries SET {', '.join(set_clauses)} WHERE id = :id AND type = 'highlight'"),
                        params,
                    )
            for entry_id in clear_ids:
                if any(conflict_id == entry_id for conflict_id, _ in conflicts):
                    continue
                clear_sync_error(conn, "entries", entry_id)

    result.updated = len(pending_updates)
    if pending_updates:
        logger.info("Reverse synced %d highlight rows from sheet via 3-way merge", len(pending_updates))
    if conflicts:
        logger.warning("Skipped %d highlight rows due to sheet/DB conflicts", len(conflicts))
    return result


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def sync_highlights_tab() -> HighlightSyncResult:
    """
    Full Highlights tab sync: pull -> push.

    Pull-first design: user edits in the sheet are captured into the DB
    before we overwrite the sheet with fresh DB state. Push-first would
    wipe sheet edits before the pull step could read them.

    First-run path (wrong/missing headers): format + push only (nothing to pull).
    Normal path: pull (sheet -> DB) -> push (DB -> sheet).
    """
    _require_sync_config()
    spreadsheet = _open_spreadsheet()
    worksheet = _open_highlights_worksheet(spreadsheet)

    try:
        existing_header = worksheet.row_values(1)
    except Exception:
        existing_header = []
    needs_init = (existing_header[: len(HIGHLIGHTS_HEADERS)] != HIGHLIGHTS_HEADERS
                  or len(existing_header) != len(HIGHLIGHTS_HEADERS))

    if needs_init:
        _apply_sheet_formatting(spreadsheet, worksheet)

    result = HighlightSyncResult()
    if not needs_init:
        result = _pull_highlights_from_sheet(worksheet)

    highlights = _fetch_highlights_from_db()
    result.pushed = _push_highlights_to_sheet(worksheet, highlights)

    return result
