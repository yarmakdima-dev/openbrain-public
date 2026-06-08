from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import gspread
from google.oauth2.service_account import Credentials
from sqlalchemy import text

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

logger = logging.getLogger("openbrain.people_sheet_sync")

PEOPLE_SHEET_NAME = "People"
MENTIONS_THRESHOLD = 3
ALLOWED_TYPES = {"self", "person", "group", "category", "system"}
PEOPLE_HEADERS = [
    "id", "canonical_name", "type", "aliases", "relationship", "role",
    "note", "extra", "mentions", "Problem", "created_at", "updated_at",
]
END_COL_LETTER = chr(ord("A") + len(PEOPLE_HEADERS) - 1)  # "L"
PEOPLE_SNAPSHOT_FIELDS = ["canonical_name", "type", "aliases", "profile"]
PEOPLE_TAB_NAME = "people"

# 1-indexed column positions.
COL = {h: i + 1 for i, h in enumerate(PEOPLE_HEADERS)}


@dataclass
class PeopleSyncResult:
    updated: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    pushed: int = 0
    conflicts: list[int] = field(default_factory=list)
    rejected_rows: list[list[str]] = field(default_factory=list)

    def as_message(self) -> str:
        msg = f"People synced: {self.updated} updated, {self.skipped} skipped"
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


def _open_people_worksheet(spreadsheet: gspread.Spreadsheet) -> gspread.Worksheet:
    try:
        return spreadsheet.worksheet(PEOPLE_SHEET_NAME)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=PEOPLE_SHEET_NAME, rows=500, cols=len(PEOPLE_HEADERS))
        logger.info("Created new worksheet: %s", PEOPLE_SHEET_NAME)
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


def _has_conditional_formats(spreadsheet: gspread.Spreadsheet, sheet_id: int) -> bool:
    metadata = spreadsheet.fetch_sheet_metadata(
        params={"fields": "sheets(properties(sheetId),conditionalFormats)"}
    )
    for sheet in metadata.get("sheets", []):
        if sheet.get("properties", {}).get("sheetId") == sheet_id:
            return bool(sheet.get("conditionalFormats"))
    return False


def _people_conditional_format_requests(sheet_id: int) -> list[dict[str, Any]]:
    row_range = {
        "sheetId": sheet_id,
        "startRowIndex": 1,
        "endRowIndex": 1000,
        "startColumnIndex": 0,
        "endColumnIndex": len(PEOPLE_HEADERS),
    }

    def rule(formula: str, color: dict[str, float], index: int) -> dict[str, Any]:
        return {
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [row_range],
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

    red = {"red": 0.96, "green": 0.72, "blue": 0.72}
    yellow = {"red": 1.0, "green": 0.94, "blue": 0.65}
    return [
        rule('=$J2<>""', red, 0),
        rule('=AND($J2="",$E2="",$F2="",$G2="",$H2="",$I2>=' + str(MENTIONS_THRESHOLD) + ')', yellow, 1),
    ]


def _apply_sheet_formatting(spreadsheet: gspread.Spreadsheet, worksheet: gspread.Worksheet) -> None:
    """Freeze header, type dropdown, widths, read-only cues, filters, and row color rules."""
    sid = worksheet.id
    requests = _conditional_format_delete_requests(spreadsheet, sid) + [
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sid,
                    "gridProperties": {"columnCount": len(PEOPLE_HEADERS)},
                },
                "fields": "gridProperties.columnCount",
            }
        },
        {
            "setDataValidation": {
                "range": {
                    "sheetId": sid, "startRowIndex": 1, "endRowIndex": 500,
                    "startColumnIndex": 0, "endColumnIndex": len(PEOPLE_HEADERS),
                },
                "rule": None,
            }
        },
        {
            "updateSheetProperties": {
                "properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 1}},
                "fields": "gridProperties.frozenRowCount",
            }
        },
        {
            "setDataValidation": {
                "range": {
                    "sheetId": sid, "startRowIndex": 1, "endRowIndex": 500,
                    "startColumnIndex": COL["type"] - 1, "endColumnIndex": COL["type"],
                },
                "rule": {
                    "condition": {
                        "type": "ONE_OF_LIST",
                        "values": [{"userEnteredValue": v} for v in ["self", "person", "group", "category", "system"]],
                    },
                    "showCustomUi": True, "strict": True,
                },
            }
        },
        {
            "repeatCell": {
                "range": {
                    "sheetId": sid, "startRowIndex": 1, "endRowIndex": 500,
                    "startColumnIndex": COL["aliases"] - 1, "endColumnIndex": COL["extra"],
                },
                "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP"}},
                "fields": "userEnteredFormat.wrapStrategy",
            }
        },
        {
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sid,
                    "dimension": "COLUMNS",
                    "startIndex": COL["canonical_name"] - 1,
                    "endIndex": COL["extra"],
                },
                "properties": {"pixelSize": 180},
                "fields": "pixelSize",
            }
        },
        {
            "repeatCell": {
                "range": {
                    "sheetId": sid, "startRowIndex": 1, "endRowIndex": 500,
                    "startColumnIndex": COL["mentions"] - 1, "endColumnIndex": COL["updated_at"],
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": {"red": 0.88, "green": 0.88, "blue": 0.88},
                    }
                },
                "fields": "userEnteredFormat.backgroundColor",
            }
        },
        {
            "repeatCell": {
                "range": {
                    "sheetId": sid, "startRowIndex": 1, "endRowIndex": 500,
                    "startColumnIndex": COL["created_at"] - 1, "endColumnIndex": COL["updated_at"],
                },
                "cell": {
                    "userEnteredFormat": {
                        "numberFormat": {"type": "DATE_TIME", "pattern": "yyyy-mm-dd hh:mm"},
                    }
                },
                "fields": "userEnteredFormat.numberFormat",
            }
        },
        {
            "setBasicFilter": {
                "filter": {
                    "range": {
                        "sheetId": sid,
                        "startRowIndex": 0, "startColumnIndex": 0,
                        "endColumnIndex": len(PEOPLE_HEADERS),
                    }
                }
            }
        },
    ] + _people_conditional_format_requests(sid)
    spreadsheet.batch_update({"requests": requests})
    logger.info("Applied formatting to People tab")


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _fetch_entities_from_db() -> list[dict[str, Any]]:
    with engine.begin() as conn:
        rows = conn.execute(
            text("""
                SELECT id, canonical_name, type, aliases, profile, sync_error, created_at, updated_at
                FROM entities
                ORDER BY
                    CASE type
                        WHEN 'self' THEN 0
                        WHEN 'person' THEN 1
                        WHEN 'group' THEN 2
                        WHEN 'category' THEN 3
                        WHEN 'system' THEN 4
                        ELSE 5
                    END ASC,
                    lower(canonical_name) ASC
            """)
        ).mappings().all()
    return [dict(r) for r in rows]


def _fetch_mentions_by_entity_id() -> dict[int, int]:
    with engine.begin() as conn:
        rows = conn.execute(
            text("""
                SELECT entity_id, COUNT(*) AS mentions_count
                FROM entries
                CROSS JOIN LATERAL unnest(who_ids) AS entity_id
                GROUP BY entity_id
            """)
        ).mappings().all()
    return {int(r["entity_id"]): int(r["mentions_count"]) for r in rows}


def _parse_aliases(raw: str) -> list[str]:
    return [part.strip() for part in (raw or "").split(",") if part.strip()]


def _format_aliases(aliases: Any) -> str:
    if not aliases:
        return ""
    return ", ".join(str(alias).strip() for alias in aliases if str(alias).strip())


def _profile_from_row(relationship: str, role: str, note: str, extra: str) -> dict[str, Any]:
    return _merge_profile_from_row({}, relationship, role, note, extra)


def _merge_profile_from_row(
    existing_profile: Any,
    relationship: str,
    role: str,
    note: str,
    extra: str,
) -> dict[str, Any]:
    profile = dict(existing_profile) if isinstance(existing_profile, dict) else {}
    for key, value in {
        "relationship": relationship,
        "role": role,
        "note": note,
        "extra": extra,
    }.items():
        if value:
            profile[key] = value
        else:
            profile.pop(key, None)
    return profile


def _profile_value(profile: Any, key: str) -> str:
    if not isinstance(profile, dict):
        return ""
    value = profile.get(key)
    return "" if value is None else str(value)


def _visible_profile(profile: Any) -> dict[str, str]:
    if not isinstance(profile, dict):
        return {}
    return {
        key: str(value)
        for key in ("relationship", "role", "note", "extra")
        if (value := profile.get(key))
    }


def _snapshot_rows(entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for entity in entities:
        rows.append({
            "id": entity.get("id"),
            "canonical_name": entity.get("canonical_name"),
            "type": entity.get("type"),
            "aliases": entity.get("aliases") or [],
            "profile": _visible_profile(entity.get("profile")),
        })
    return rows


def _format_dt(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.astimezone(APP_TZ).strftime("%Y-%m-%d %H:%M:%S")
    return str(v)


def _row_to_sheet_values(entity: dict[str, Any], mentions: int) -> list[str]:
    profile = entity.get("profile") or {}
    return [
        str(entity["id"]),
        entity.get("canonical_name") or "",
        entity.get("type") or "",
        _format_aliases(entity.get("aliases")),
        _profile_value(profile, "relationship"),
        _profile_value(profile, "role"),
        _profile_value(profile, "note"),
        _profile_value(profile, "extra"),
        str(mentions) if mentions else "",
        entity.get("sync_error") or "",
        _format_dt(entity.get("created_at")),
        _format_dt(entity.get("updated_at")),
    ]


def _set_sync_error(entity_id: int, message: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE entities SET sync_error = :sync_error WHERE id = :id"),
            {"id": entity_id, "sync_error": message},
        )


# ---------------------------------------------------------------------------
# Push: DB -> Sheet
# ---------------------------------------------------------------------------

def _push_people_to_sheet(
    worksheet: gspread.Worksheet,
    entities: list[dict[str, Any]],
    rejected_rows: list[list[str]] | None = None,
) -> int:
    mentions_by_id = _fetch_mentions_by_entity_id()
    worksheet.clear()
    rejected_rows = rejected_rows or []
    rows = [PEOPLE_HEADERS] + rejected_rows + [
        _row_to_sheet_values(e, mentions_by_id.get(int(e["id"]), 0))
        for e in entities
    ]
    worksheet.update(f"A1:{END_COL_LETTER}{len(rows)}", rows, value_input_option="RAW")
    with engine.begin() as conn:
        write_snapshots(conn, PEOPLE_TAB_NAME, "entities", _snapshot_rows(entities), PEOPLE_SNAPSHOT_FIELDS)
    logger.info(
        "Pushed %d entity rows + %d rejected rows to People tab",
        len(entities),
        len(rejected_rows),
    )
    return len(entities)


# ---------------------------------------------------------------------------
# Pull: Sheet -> DB
# ---------------------------------------------------------------------------

def _pull_people_from_sheet(worksheet: gspread.Worksheet) -> PeopleSyncResult:
    """
    Read People tab and write back user edits to entities via 3-way merge.

    Editable fields: canonical_name, type, aliases, relationship, role, note, extra.
    Read-only fields: id, mentions, sync_error, created_at, updated_at; edits are logged and ignored.
    """
    result = PeopleSyncResult()
    values = worksheet.get_all_values()
    if not values or len(values) < 2:
        return result

    header = values[0]
    if header[: len(PEOPLE_HEADERS)] != PEOPLE_HEADERS or len(header) != len(PEOPLE_HEADERS):
        raise RuntimeError(f"Header mismatch: expected {PEOPLE_HEADERS!r}, got {header!r}")
    idx = {h: header.index(h) for h in PEOPLE_HEADERS}
    idx["sync_error"] = idx["Problem"]

    db_rows = _fetch_entities_from_db()
    db_map: dict[int, dict[str, Any]] = {int(r["id"]): r for r in db_rows}
    name_to_id: dict[str, int] = {str(r["canonical_name"]).casefold(): int(r["id"]) for r in db_rows}
    mentions_by_id = _fetch_mentions_by_entity_id()

    sheet_rows = values[1:]
    row_ids: list[int] = []
    for row in sheet_rows:
        raw_id = row[idx["id"]].strip() if idx["id"] < len(row) else ""
        if raw_id.isdigit():
            row_ids.append(int(raw_id))

    with engine.begin() as conn:
        snapshots = fetch_snapshots(conn, PEOPLE_TAB_NAME, row_ids)

    pending_updates: list[dict[str, Any]] = []
    conflicts: list[tuple[int, str]] = []
    clear_ids: set[int] = set()

    for row_num, row in enumerate(sheet_rows, start=2):
        def get(col: str) -> str:
            i = idx[col]
            return row[i].strip() if i < len(row) else ""

        raw_id = get("id")
        canonical_name = get("canonical_name")
        entity_type = get("type")
        aliases = _parse_aliases(get("aliases"))
        visible_profile = _profile_from_row(get("relationship"), get("role"), get("note"), get("extra"))

        if not raw_id:
            if not canonical_name or not entity_type:
                msg = f"Row {row_num}: new entity requires canonical_name and type"
                logger.warning(msg)
                result.errors.append(msg)
                result.skipped += 1
                result.rejected_rows.append([
                    "", canonical_name, entity_type, get("aliases"),
                    get("relationship"), get("role"), get("note"), get("extra"),
                    "", msg, "", "",
                ])
                continue
            if entity_type not in ALLOWED_TYPES:
                msg = f"Row {row_num}: invalid type {entity_type!r}"
                logger.warning(msg)
                result.errors.append(msg)
                result.skipped += 1
                result.rejected_rows.append([
                    "", canonical_name, entity_type, get("aliases"),
                    get("relationship"), get("role"), get("note"), get("extra"),
                    "", msg, "", "",
                ])
                continue
            folded = canonical_name.casefold()
            if folded in name_to_id:
                msg = f"Row {row_num}: canonical_name {canonical_name!r} already exists"
                logger.warning(msg)
                result.errors.append(msg)
                result.skipped += 1
                result.rejected_rows.append([
                    "", canonical_name, entity_type, get("aliases"),
                    get("relationship"), get("role"), get("note"), get("extra"),
                    "", msg, "", "",
                ])
                continue

            with engine.begin() as conn:
                new_id = conn.execute(
                    text("""
                        INSERT INTO entities (canonical_name, type, aliases, profile, sync_error)
                        VALUES (:canonical_name, :type, :aliases, CAST(:profile AS jsonb), NULL)
                        RETURNING id
                    """),
                    {
                        "canonical_name": canonical_name,
                        "type": entity_type,
                        "aliases": aliases,
                        "profile": json.dumps(visible_profile),
                    },
                ).scalar_one()
            name_to_id[folded] = int(new_id)
            result.updated += 1
            logger.info("Created entity id=%d from People tab row %d", int(new_id), row_num)
            continue

        if not raw_id.isdigit():
            msg = f"Row {row_num}: invalid id {raw_id!r}"
            logger.warning(msg)
            result.errors.append(msg)
            result.skipped += 1
            continue

        entity_id = int(raw_id)
        db = db_map.get(entity_id)
        if not db:
            msg = f"Row {row_num}: id {entity_id} not in DB"
            logger.warning(msg)
            result.errors.append(msg)
            result.skipped += 1
            continue

        expected_mentions = str(mentions_by_id.get(entity_id, 0)) if mentions_by_id.get(entity_id, 0) else ""
        read_only_expectations = {
            "mentions": expected_mentions,
            "sync_error": db.get("sync_error") or "",
            "created_at": _format_dt(db.get("created_at")),
            "updated_at": _format_dt(db.get("updated_at")),
        }
        for col, expected in read_only_expectations.items():
            raw_value = get(col)
            if raw_value != expected:
                logger.warning(
                    "Row %d id=%d: %s differs (sheet=%r db=%r) -- ignored",
                    row_num, entity_id, col, raw_value, expected,
                )

        if not canonical_name:
            msg = f"Row {row_num} id={entity_id}: canonical_name is required"
            logger.warning(msg)
            _set_sync_error(entity_id, msg)
            result.conflicts.append(entity_id)
            result.errors.append(msg)
            result.skipped += 1
            continue

        if entity_type not in ALLOWED_TYPES:
            msg = f"Row {row_num} id={entity_id}: invalid type {entity_type!r}"
            logger.warning(msg)
            _set_sync_error(entity_id, msg)
            result.conflicts.append(entity_id)
            result.errors.append(msg)
            result.skipped += 1
            continue

        folded = canonical_name.casefold()
        snapshot = snapshots.get(entity_id)
        if snapshot is None:
            continue

        db_profile = db.get("profile") or {}
        sheet_values = {
            "canonical_name": canonical_name,
            "type": entity_type,
            "aliases": aliases,
            "profile": visible_profile,
        }
        db_values = {
            "canonical_name": db.get("canonical_name"),
            "type": db.get("type"),
            "aliases": db.get("aliases") or [],
            "profile": _visible_profile(db_profile),
        }

        decisions: dict[str, tuple[str, Any]] = {}
        conflict_fields: list[str] = []
        for field in PEOPLE_SNAPSHOT_FIELDS:
            decision, value = three_way_resolve(field, snapshot.get(field), sheet_values[field], db_values[field])
            decisions[field] = (decision, value)
            if decision == "conflict":
                conflict_fields.append(field)

        if conflict_fields:
            msg = "; ".join(
                f"{field}: sheet={sheet_values[field]!r}, db={db_values[field]!r}"
                for field in conflict_fields
            )
            conflicts.append((entity_id, f"People tab conflict -- {msg}"))
            result.conflicts.append(entity_id)
            result.skipped += 1
            continue

        clear_ids.add(entity_id)
        sheet_wins = {field: value for field, (decision, value) in decisions.items() if decision == "take_sheet"}
        if "canonical_name" in sheet_wins:
            collision_id = name_to_id.get(folded)
            if collision_id is not None and collision_id != entity_id:
                msg = f"Row {row_num} id={entity_id}: canonical_name {canonical_name!r} collides with entity id={collision_id}"
                logger.warning(msg)
                conflicts.append((entity_id, msg))
                result.conflicts.append(entity_id)
                result.errors.append(msg)
                result.skipped += 1
                continue
        if sheet_wins:
            pending_updates.append({
                "id": entity_id,
                "old_folded": str(db.get("canonical_name") or "").casefold(),
                "new_folded": folded,
                "db_profile": db_profile,
                "visible_profile": visible_profile,
                **sheet_wins,
            })

    if pending_updates or conflicts or clear_ids:
        with engine.begin() as conn:
            for entity_id, message in conflicts:
                set_sync_error(conn, "entities", entity_id, message)
            for update_values in pending_updates:
                set_clauses: list[str] = []
                params = {"id": update_values["id"]}
                for field in PEOPLE_SNAPSHOT_FIELDS:
                    if field not in update_values:
                        continue
                    if field == "profile":
                        params["profile"] = json.dumps(_merge_profile_from_row(
                            update_values["db_profile"],
                            update_values["visible_profile"].get("relationship", ""),
                            update_values["visible_profile"].get("role", ""),
                            update_values["visible_profile"].get("note", ""),
                            update_values["visible_profile"].get("extra", ""),
                        ))
                        set_clauses.append("profile = CAST(:profile AS jsonb)")
                    else:
                        params[field] = update_values[field]
                        set_clauses.append(f"{field} = :{field}")
                if set_clauses:
                    set_clauses.append("sync_error = NULL")
                    conn.execute(
                        text(f"UPDATE entities SET {', '.join(set_clauses)} WHERE id = :id"),
                        params,
                    )
                if update_values["old_folded"] != update_values["new_folded"]:
                    name_to_id.pop(update_values["old_folded"], None)
                    name_to_id[update_values["new_folded"]] = int(update_values["id"])
            for entity_id in clear_ids:
                if any(conflict_id == entity_id for conflict_id, _ in conflicts):
                    continue
                clear_sync_error(conn, "entities", entity_id)

    result.updated += len(pending_updates)
    if pending_updates:
        logger.info("Reverse synced %d People rows from sheet via 3-way merge", len(pending_updates))
    if conflicts:
        logger.warning("Skipped %d People rows due to sheet/DB conflicts", len(conflicts))

    return result

# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def sync_people_tab() -> PeopleSyncResult:
    """
    Full People tab sync: pull -> push.

    Pull-first design: user edits in the sheet are captured into the DB before
    we overwrite the sheet with fresh DB state.
    """
    _require_sync_config()
    spreadsheet = _open_spreadsheet()
    worksheet = _open_people_worksheet(spreadsheet)

    try:
        existing_header = worksheet.row_values(1)
    except Exception:
        existing_header = []
    needs_init = (existing_header[: len(PEOPLE_HEADERS)] != PEOPLE_HEADERS
                  or len(existing_header) != len(PEOPLE_HEADERS))

    if needs_init or not _has_conditional_formats(spreadsheet, worksheet.id):
        _apply_sheet_formatting(spreadsheet, worksheet)

    result = PeopleSyncResult()
    if not needs_init:
        result = _pull_people_from_sheet(worksheet)

    entities = _fetch_entities_from_db()
    result.pushed = _push_people_to_sheet(worksheet, entities, result.rejected_rows)

    return result
