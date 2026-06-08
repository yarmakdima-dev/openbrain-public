from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text

logger = logging.getLogger("openbrain.entity_resolver")


def resolve_who_to_ids(who: str | None, conn: Any) -> list[int]:
    """P3a - shadow-write helper. entries.who remains source of truth; this populates entries.who_ids[] in parallel. Single-name only for now; multi-name handled in P3b."""
    normalized = (who or "").strip()
    if not normalized:
        return []

    rows = conn.execute(
        text(
            """
            SELECT id FROM entities
            WHERE lower(canonical_name) = lower(:w)
               OR EXISTS (SELECT 1 FROM unnest(aliases) a WHERE lower(a) = lower(:w))
            ORDER BY id
            LIMIT 2
            """
        ),
        {"w": normalized},
    ).scalars().all()

    if not rows:
        return []
    if len(rows) > 1:
        logger.warning("Multiple entity matches for who=%r; using entity_id=%s", normalized, rows[0])
    return [int(rows[0])]


def resolve_who_to_ids_safe(who: str | None, conn: Any) -> list[int]:
    try:
        return resolve_who_to_ids(who, conn)
    except Exception:
        logger.exception("Failed to resolve who=%r to entity ids", who)
        return []


def fuzzy_match_entities(raw_who: str, conn: Any, threshold: float = 0.3, limit: int = 5) -> list[dict]:
    rows = conn.execute(
        text(
            """
            SELECT id, canonical_name, type,
                   GREATEST(
                     similarity(canonical_name, :raw_who),
                     COALESCE((SELECT MAX(similarity(a, :raw_who)) FROM unnest(aliases) a), 0)
                   ) AS sim
            FROM entities
            WHERE type = 'person'
              AND GREATEST(
                    similarity(canonical_name, :raw_who),
                    COALESCE((SELECT MAX(similarity(a, :raw_who)) FROM unnest(aliases) a), 0)
                  ) >= :threshold
            ORDER BY sim DESC
            LIMIT :limit
            """
        ),
        {"raw_who": (raw_who or "").strip(), "threshold": threshold, "limit": limit},
    ).mappings().all()
    return [
        {"id": int(row["id"]), "canonical_name": row["canonical_name"], "sim": float(row["sim"] or 0)}
        for row in rows
    ]


def find_open_resolution_task(raw_who: str, conn: Any) -> int | None:
    task_id = conn.execute(
        text(
            """
            SELECT id FROM entries
            WHERE type = 'task' AND status = 'open'
              AND metadata->>'raw_who' = :raw_who
              AND COALESCE(metadata->>'action', 'resolve') = 'resolve'
            ORDER BY id DESC
            LIMIT 1
            """
        ),
        {"raw_who": raw_who},
    ).scalar_one_or_none()
    return int(task_id) if task_id is not None else None


def find_skip_task(raw_who: str, conn: Any) -> int | None:
    task_id = conn.execute(
        text(
            """
            SELECT id FROM entries
            WHERE type = 'task'
              AND metadata->>'raw_who' = :raw_who
              AND metadata->>'action' = 'skip'
            ORDER BY id DESC
            LIMIT 1
            """
        ),
        {"raw_who": raw_who},
    ).scalar_one_or_none()
    return int(task_id) if task_id is not None else None


def append_entry_to_resolution_task(task_id: int, entry_id: int, conn: Any) -> None:
    conn.execute(
        text(
            """
            UPDATE entries
            SET metadata = jsonb_set(
                  metadata,
                  '{entry_ids}',
                  COALESCE(metadata->'entry_ids', '[]'::jsonb) || to_jsonb(CAST(:entry_id AS bigint))
                ),
                updated_at = now()
            WHERE id = :task_id
              AND NOT (COALESCE(metadata->'entry_ids', '[]'::jsonb) @> to_jsonb(CAST(:entry_id AS bigint)))
            """
        ),
        {"task_id": task_id, "entry_id": entry_id},
    )


def backfill_who_ids_for_entries(entry_ids: list[int], entity_id: int, conn: Any) -> int:
    if not entry_ids:
        return 0
    result = conn.execute(
        text(
            """
            UPDATE entries
            SET who_ids = ARRAY[:entity_id], updated_at = now()
            WHERE id = ANY(CAST(:entry_ids AS bigint[]))
              AND who_ids = ARRAY[]::bigint[]
            """
        ),
        {"entry_ids": [int(entry_id) for entry_id in entry_ids], "entity_id": int(entity_id)},
    )
    return int(result.rowcount or 0)


def get_resolution_task_entry_ids(task_id: int, conn: Any) -> list[int]:
    raw_ids = conn.execute(
        text("SELECT metadata->'entry_ids' FROM entries WHERE id = :task_id"),
        {"task_id": task_id},
    ).scalar_one_or_none()
    if not raw_ids:
        return []
    return [int(entry_id) for entry_id in raw_ids]

