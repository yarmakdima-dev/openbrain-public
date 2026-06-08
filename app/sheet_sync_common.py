"""Shared helpers for sheet-to-DB sync paths.

A3 architecture (commit bb7ef20):
- sheet_snapshots table stores last-pushed value per (tab_name, row_id).
- 3-way merge on pull: compare snapshot vs sheet vs DB per field.
  - sheet == snapshot AND db == snapshot   -> no-op
  - sheet != snapshot AND db == snapshot   -> sheet edit, sheet wins, update DB
  - sheet == snapshot AND db != snapshot   -> DB edit, DB wins, no DB update (next push will fix sheet)
  - sheet != snapshot AND db != snapshot, same value -> consensus, update DB if needed, clear conflict
  - sheet != snapshot AND db != snapshot, different value -> CONFLICT, skip row, set sync_error
- NULL snapshot (row never pushed yet) -> DB wins, defer to next push.

Snapshot values are normalized Python values (post-parse), serialized as JSONB.
Comparison happens on parsed values, not on sheet strings.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterable

from sqlalchemy import text

logger = logging.getLogger("openbrain")


def _normalize(value: Any) -> Any:
    """Normalize a Python value for JSONB storage and comparison.

    - None stays None.
    - Lists are converted via list() so tuples / PG arrays compare equal.
    - Dates are ISO strings (they survive JSONB roundtrip as strings; parse helpers
      already produce date objects, so the pull-side comparator must format DB dates
      the same way before comparing).
    """
    if value is None:
        return None
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def write_snapshots(conn, tab_name: str, target_table: str, rows: Iterable[dict[str, Any]], fields: list[str]) -> int:
    """Upsert snapshots for the given rows.

    rows: iterable of dicts containing at minimum id and the listed fields.
    fields: which keys from each row to capture in the snapshot.
    Returns count of snapshots written.
    """
    payload = []
    for row in rows:
        row_id = row.get("id")
        if row_id is None:
            continue
        snapshot = {f: _normalize(row.get(f)) for f in fields}
        payload.append({
            "tab_name": tab_name,
            "target_table": target_table,
            "row_id": int(row_id),
            "snapshot": json.dumps(snapshot),
        })
    if not payload:
        return 0
    conn.execute(
        text("""
            INSERT INTO sheet_snapshots (tab_name, target_table, row_id, snapshot, updated_at)
            VALUES (:tab_name, :target_table, :row_id, CAST(:snapshot AS jsonb), NOW())
            ON CONFLICT (tab_name, row_id) DO UPDATE
            SET snapshot = EXCLUDED.snapshot,
                target_table = EXCLUDED.target_table,
                updated_at = NOW()
        """),
        payload,
    )
    return len(payload)


def fetch_snapshots(conn, tab_name: str, row_ids: list[int]) -> dict[int, dict[str, Any]]:
    """Return {row_id: snapshot_dict} for given rows. Missing rows omitted (callers handle NULL -> DB wins)."""
    if not row_ids:
        return {}
    result = conn.execute(
        text("SELECT row_id, snapshot FROM sheet_snapshots WHERE tab_name = :tab AND row_id = ANY(:ids)"),
        {"tab": tab_name, "ids": row_ids},
    ).mappings().all()
    return {r["row_id"]: r["snapshot"] for r in result}


def three_way_resolve(field: str, snapshot_value: Any, sheet_value: Any, db_value: Any) -> tuple[str, Any]:
    """Resolve a single field via 3-way merge.

    Returns (decision, value_to_apply) where decision is one of:
      noop      -- no change anywhere
      take_sheet -- sheet edit, apply sheet_value to DB
      take_db    -- DB edit, leave DB as-is (next push refreshes sheet)
      consensus  -- both sides made same change, apply value (idempotent)
      conflict   -- both sides changed to different values, skip row
    """
    s = _normalize(snapshot_value)
    sh = _normalize(sheet_value)
    db = _normalize(db_value)

    sheet_changed = sh != s
    db_changed = db != s

    if not sheet_changed and not db_changed:
        return ("noop", db)
    if sheet_changed and not db_changed:
        return ("take_sheet", sh)
    if not sheet_changed and db_changed:
        return ("take_db", db)
    if sh == db:
        return ("consensus", sh)
    return ("conflict", None)


def clear_sync_error(conn, target_table: str, row_id: int) -> None:
    """Clear sync_error for a row (called after successful conflict-free pull)."""
    if target_table == "entries":
        conn.execute(text("UPDATE entries SET sync_error = NULL WHERE id = :id AND sync_error IS NOT NULL"), {"id": row_id})
    elif target_table == "entities":
        conn.execute(text("UPDATE entities SET sync_error = NULL WHERE id = :id AND sync_error IS NOT NULL"), {"id": row_id})


def set_sync_error(conn, target_table: str, row_id: int, message: str) -> None:
    """Set sync_error for a row (called on conflict)."""
    if target_table == "entries":
        conn.execute(text("UPDATE entries SET sync_error = :msg WHERE id = :id"), {"id": row_id, "msg": message})
    elif target_table == "entities":
        conn.execute(text("UPDATE entities SET sync_error = :msg WHERE id = :id"), {"id": row_id, "msg": message})
