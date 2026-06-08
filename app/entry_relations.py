"""Helpers for writing to the entry_relations edge table.

Stage 1 of #273: dual-write spawned_from edges alongside the legacy
entries.spawned_from_idea_id column. Edge writes are best-effort —
they must never break the primary entry insertion path.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.sheets_sync import engine

logger = logging.getLogger(__name__)

VALID_RELATION_TYPES = frozenset({"spawned_from", "continues", "contradicts"})


class InvalidRelationType(Exception):
    """Raised when relation_type is not in the closed vocab."""


class EntryNotFound(Exception):
    """Raised when one or both entries referenced don't exist."""
    def __init__(self, missing_id: int):
        self.missing_id = missing_id
        super().__init__(f"Entry {missing_id} not found")


class RelationExists(Exception):
    """Raised when the (from, to, type) triple already exists."""



def insert_spawn_relation(
   conn: Connection,
   from_entry_id: int,
   to_idea_id: int,
   origin: str,
) -> None:
   """Insert a spawned_from edge. Logs and swallows errors.

   Must be called inside an existing transaction. The caller's
   transaction is preserved on failure (we use a savepoint).
   """
   if from_entry_id is None or to_idea_id is None:
       logger.warning("insert_spawn_relation: null id (from=%s, to=%s)", from_entry_id, to_idea_id)
       return
   if from_entry_id == to_idea_id:
       logger.warning("insert_spawn_relation: self-link rejected (id=%s)", from_entry_id)
       return
   try:
       with conn.begin_nested():
           conn.execute(
               text("""
                   INSERT INTO entry_relations
                       (from_entry_id, to_entry_id, relation_type, metadata)
                   VALUES
                       (:from_id, :to_id, 'spawned_from', CAST(:metadata AS JSONB))
                   ON CONFLICT (from_entry_id, to_entry_id, relation_type) DO NOTHING
               """),
               {
                   "from_id": from_entry_id,
                   "to_id": to_idea_id,
                   "metadata": json.dumps({"source": origin}),
               },
           )
   except Exception:
       logger.exception(
           "insert_spawn_relation failed (from=%s, to=%s, origin=%s)",
           from_entry_id, to_idea_id, origin,
       )

def create_relation(
    from_entry_id: int,
    to_entry_id: int,
    relation_type: str,
) -> int:
    """Create a manual relation edge via /link. Returns new edge id.

    Validates closed vocab, self-link, entry existence, and duplicate triples.
    Raises InvalidRelationType, ValueError, EntryNotFound, or RelationExists.
    """
    if relation_type not in VALID_RELATION_TYPES:
        raise InvalidRelationType(relation_type)
    if from_entry_id == to_entry_id:
        raise ValueError("Self-link not allowed")

    with engine.begin() as conn:
        rows = conn.execute(
            text("SELECT id FROM entries WHERE id IN (:a, :b)"),
            {"a": from_entry_id, "b": to_entry_id},
        ).scalars().all()
        found = set(rows)
        if from_entry_id not in found:
            raise EntryNotFound(from_entry_id)
        if to_entry_id not in found:
            raise EntryNotFound(to_entry_id)

        existing = conn.execute(
            text("""
                SELECT id FROM entry_relations
                WHERE from_entry_id = :f AND to_entry_id = :t AND relation_type = :r
            """),
            {"f": from_entry_id, "t": to_entry_id, "r": relation_type},
        ).scalar()
        if existing is not None:
            raise RelationExists()

        new_id = conn.execute(
            text("""
                INSERT INTO entry_relations
                    (from_entry_id, to_entry_id, relation_type, metadata)
                VALUES
                    (:f, :t, :r, CAST(:m AS JSONB))
                RETURNING id
            """),
            {
                "f": from_entry_id,
                "t": to_entry_id,
                "r": relation_type,
                "m": json.dumps({"source": "manual_link"}),
            },
        ).scalar()
        return int(new_id)
