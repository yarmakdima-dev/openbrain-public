from __future__ import annotations

import logging
import os
from pathlib import Path
from datetime import date, datetime, time, timedelta
from typing import Any, Optional

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
import uvicorn
from sqlalchemy import create_engine, text

from app.config import get_config
from app.main import (
    classify_tags,
    detect_language_bucket,
    embedding_to_vector_literal,
    ensure_entries_table,
    extract_entry_metadata,
    get_openai_embedding,
    hybrid_search_entries,
)

load_dotenv()
cfg = get_config()
logger = logging.getLogger("openbrain.mcp")
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
)

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
MCP_HOST = os.getenv("OPENBRAIN_MCP_HOST", "127.0.0.1").strip() or "127.0.0.1"
MCP_PORT = int(os.getenv("OPENBRAIN_MCP_PORT", "8765"))
APP_TZ = cfg.app.timezone
BASE_DIR = Path(__file__).resolve().parent.parent
MCP_CERT_FILE = os.getenv("OPENBRAIN_MCP_CERT_FILE", str(BASE_DIR / "certs" / "openbrain-mcp.crt")).strip()
MCP_KEY_FILE = os.getenv("OPENBRAIN_MCP_KEY_FILE", str(BASE_DIR / "certs" / "openbrain-mcp.key")).strip()

if not DATABASE_URL:
    raise RuntimeError("Missing DATABASE_URL")

engine = create_engine(DATABASE_URL, future=True, pool_pre_ping=True)

mcp = FastMCP(
    name="Open Brain MCP",
    instructions="Semantic memory access for the Open Brain journal database.",
    host=MCP_HOST,
    port=MCP_PORT,
    streamable_http_path="/mcp",
    stateless_http=True,
    json_response=True,
    log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
)


VALID_TYPES = {"highlight", "book", "person", "idea", "task", "review"}
TOPIC_VOCABULARY = [topic.strip() for topic in cfg.topics.vocabulary if str(topic).strip()]
VALID_TOPICS = set(TOPIC_VOCABULARY)


def _serialize_entry(row: dict[str, Any]) -> dict[str, Any]:
    created_at = row.get("created_at")
    updated_at = row.get("updated_at")
    return {
        "id": int(row["id"]),
        "date": created_at.isoformat() if created_at else None,
        "updated_at": updated_at.isoformat() if updated_at else None,
        "type": row.get("type"),
        "who": row.get("who"),
        "title": row.get("title"),
        "content": row.get("content"),
        "language": row.get("language"),
        "status": row.get("status"),
        "source": row.get("source"),
        "tags": row.get("tags") or [],
    }


def _fetch_entries(sql: str, params: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
    with engine.begin() as conn:
        rows = conn.execute(text(sql), params or {}).mappings().all()
    return [dict(row) for row in rows]


def _parse_iso_date(raw_value: Optional[str], field_name: str) -> tuple[Optional[date], Optional[dict[str, Any]]]:
    if raw_value is None:
        return None, None
    normalized = str(raw_value).strip()
    if not normalized:
        return None, None
    try:
        return datetime.strptime(normalized, "%Y-%m-%d").date(), None
    except ValueError:
        return None, {"error": f"Invalid {field_name}: {raw_value}. Expected YYYY-MM-DD."}


def _date_bounds(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    days: Optional[int] = None,
) -> tuple[Optional[datetime], Optional[datetime], Optional[dict[str, Any]]]:
    parsed_from, error = _parse_iso_date(date_from, "date_from")
    if error:
        return None, None, error
    parsed_to, error = _parse_iso_date(date_to, "date_to")
    if error:
        return None, None, error

    if parsed_from and parsed_to and parsed_from > parsed_to:
        return None, None, {"error": "date_from must be on or before date_to."}

    if parsed_from or parsed_to:
        since = datetime.combine(parsed_from, time.min) if parsed_from else None
        until = datetime.combine(parsed_to + timedelta(days=1), time.min) if parsed_to else None
        return since, until, None

    if days is None:
        return None, None, None

    bounded_days = max(1, min(int(days), 3650))
    since = datetime.utcnow() - timedelta(days=bounded_days)
    return since, None, None


def _fetch_entries_by_ids(entry_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not entry_ids:
        return {}
    params = {f"id_{idx}": entry_id for idx, entry_id in enumerate(entry_ids)}
    placeholders = ", ".join(f":id_{idx}" for idx in range(len(entry_ids)))
    rows = _fetch_entries(
        f"""
        SELECT id, created_at, updated_at, type, who, title, content, language, status, source, tags
        FROM entries
        WHERE id IN ({placeholders})
        """,
        params,
    )
    return {int(row["id"]): row for row in rows}


def _insert_entry_returning_id(
    content: str,
    embedding: Optional[list[float]],
    tags: list[str],
    who: Optional[str],
    title: Optional[str],
    language: Optional[str],
    entry_type: Optional[str],
    topic: Optional[str],
    status: Optional[str],
    source: str,
) -> int:
    vector_literal = embedding_to_vector_literal(embedding) if embedding else None
    params = {
        "content": content,
        "embedding": vector_literal,
        "tags": tags,
        "who": who,
        "title": title,
        "language": language,
        "entry_type": entry_type,
        "topic": topic,
        "status": status,
        "source": source,
    }
    with engine.begin() as conn:
        if vector_literal:
            row_id = conn.execute(
                text(
                    """
                    INSERT INTO entries (content, embedding, tags, who, title, language, type, topic, status, source)
                    VALUES (:content, CAST(:embedding AS vector), :tags, :who, :title, :language, :entry_type, :topic, :status, :source)
                    RETURNING id
                    """
                ),
                params,
            ).scalar_one()
        else:
            row_id = conn.execute(
                text(
                    """
                    INSERT INTO entries (content, tags, who, title, language, type, topic, status, source)
                    VALUES (:content, :tags, :who, :title, :language, :entry_type, :topic, :status, :source)
                    RETURNING id
                    """
                ),
                params,
            ).scalar_one()
    entry_id = int(row_id)
    if params["topic"]:
        logger.info("Entry #%s topic assigned: %s", entry_id, params["topic"])
    else:
        logger.info("Entry #%s topic invalid, set NULL", entry_id)
    return entry_id


@mcp.tool(description="Semantic vector search across all entries using query text. Returns top 5 by relevance. [read-only]")
def search_memory(query: str) -> dict[str, Any]:
    """Run semantic search across the Open Brain memory database."""
    if not query or not query.strip():
        return {"query": query, "results": [], "error": "Query is required."}

    query_embedding = get_openai_embedding(query.strip())
    vector_literal = embedding_to_vector_literal(query_embedding)
    rows = _fetch_entries(
        """
        SELECT id, created_at, updated_at, type, who, title, content, language, status, source, tags,
               1 - (embedding <=> CAST(:embedding AS vector)) AS relevance
        FROM entries
        WHERE embedding IS NOT NULL
        ORDER BY embedding <=> CAST(:embedding AS vector)
        LIMIT 5
        """,
        {"embedding": vector_literal},
    )
    results = []
    for row in rows:
        item = _serialize_entry(row)
        item["relevance"] = float(row.get("relevance") or 0.0)
        results.append(item)
    return {"query": query, "count": len(results), "results": results}


@mcp.tool(description="Return recent entries by relative days or by ISO date_from/date_to range. If a date range is provided it overrides days. [read-only]")
def recent_entries(days: int = 7, date_from: Optional[str] = None, date_to: Optional[str] = None) -> dict[str, Any]:
    """Return recent entries from the last N days in chronological order."""
    since, until, error = _date_bounds(date_from=date_from, date_to=date_to, days=days)
    if error:
        return error

    sql = """
        SELECT id, created_at, updated_at, type, who, title, content, language, status, source, tags
        FROM entries
        WHERE 1=1
    """
    params: dict[str, Any] = {}
    if since is not None:
        sql += " AND created_at >= :since"
        params["since"] = since
    if until is not None:
        sql += " AND created_at < :until"
        params["until"] = until
    sql += " ORDER BY created_at ASC, id ASC"
    rows = _fetch_entries(sql, params)
    return {
        "days": None if (date_from or date_to) else max(1, min(int(days), 3650)),
        "date_from": date_from,
        "date_to": date_to,
        "count": len(rows),
        "entries": [_serialize_entry(row) for row in rows],
    }


@mcp.tool(description="Filter entries by type and optional status. Returns matching rows in chronological order. [read-only]")
def search_by_type(entry_type: str, status: Optional[str] = None) -> dict[str, Any]:
    """Return entries by type, optionally filtered by status."""
    normalized_type = (entry_type or "").strip().lower()
    if normalized_type not in VALID_TYPES:
        return {"error": f"Invalid type: {entry_type}", "valid_types": sorted(VALID_TYPES)}

    sql = """
        SELECT id, created_at, updated_at, type, who, title, content, language, status, source, tags
        FROM entries
        WHERE type = :entry_type
    """
    params: dict[str, Any] = {"entry_type": normalized_type}
    if status:
        sql += " AND status = :status"
        params["status"] = status.strip().lower()
    sql += " ORDER BY created_at ASC, id ASC"
    rows = _fetch_entries(sql, params)
    return {"type": normalized_type, "status": status, "count": len(rows), "entries": [_serialize_entry(row) for row in rows]}


@mcp.tool(description="Filter entries by exact who value. Returns matching rows in chronological order. [read-only]")
def search_by_who(who: str) -> dict[str, Any]:
    """Return entries for a specific person or topic label."""
    who_value = (who or "").strip()
    if not who_value:
        return {"error": "who is required."}
    rows = _fetch_entries(
        """
        SELECT id, created_at, updated_at, type, who, title, content, language, status, source, tags
        FROM entries
        WHERE who = :who
        ORDER BY created_at ASC, id ASC
        """,
        {"who": who_value},
    )
    return {"who": who_value, "count": len(rows), "entries": [_serialize_entry(row) for row in rows]}


@mcp.tool(description="Add a journal entry with auto-metadata, tags, topic, and embedding. Writes a new row to the database. [writes to DB]")
def add_entry(content: str, entry_type: Optional[str] = None, who: Optional[str] = None) -> dict[str, Any]:
    """Add a new journal entry with auto-tagging and embeddings."""
    content = (content or "").strip()
    if not content:
        return {"error": "content is required."}

    forced_type = (entry_type or "").strip().lower() or None
    if forced_type and forced_type not in VALID_TYPES - {"review"}:
        return {"error": f"Invalid type: {entry_type}", "valid_types": sorted(VALID_TYPES - {'review'})}

    metadata = extract_entry_metadata(content, forced_type)
    if who and who.strip():
        metadata["who"] = who.strip()
    if not metadata.get("language"):
        metadata["language"] = detect_language_bucket(content)
    tags = classify_tags(content)

    embedding = None
    try:
        embedding = get_openai_embedding(content)
    except Exception as exc:
        logger.warning("Embedding failed in MCP add_entry; saving without embedding: %s", exc)

    saved_type = metadata.get("type") or forced_type or "highlight"
    status = "open" if saved_type == "task" else None
    entry_id = _insert_entry_returning_id(
        content=content,
        embedding=embedding,
        tags=tags,
        who=metadata.get("who"),
        title=metadata.get("title"),
        language=metadata.get("language"),
        entry_type=saved_type,
        topic=metadata.get("topic"),
        status=status,
        source="mcp",
    )
    return {
        "ok": True,
        "entry_id": entry_id,
        "who": metadata.get("who"),
        "title": metadata.get("title"),
        "language": metadata.get("language"),
        "type": saved_type,
        "topic": metadata.get("topic"),
        "status": status,
        "tags": tags,
    }


@mcp.tool(description="Summarize recent entries over the last N days with type, who, and tag aggregates. [read-only]")
def get_summary(days: int = 7) -> dict[str, Any]:
    """Return a compact summary of entries in the last N days."""
    days = max(1, min(int(days), 3650))
    since = datetime.utcnow() - timedelta(days=days)
    entries = _fetch_entries(
        """
        SELECT id, created_at, updated_at, type, who, title, content, language, status, source, tags
        FROM entries
        WHERE created_at >= :since
        ORDER BY created_at ASC, id ASC
        """,
        {"since": since},
    )
    type_rows = _fetch_entries(
        """
        SELECT type, COUNT(*) AS count
        FROM entries
        WHERE created_at >= :since
        GROUP BY type
        ORDER BY count DESC, type ASC
        """,
        {"since": since},
    )
    who_rows = _fetch_entries(
        """
        SELECT who, COUNT(*) AS count
        FROM entries
        WHERE created_at >= :since AND who IS NOT NULL AND who <> ''
        GROUP BY who
        ORDER BY count DESC, who ASC
        LIMIT 5
        """,
        {"since": since},
    )
    tag_rows = _fetch_entries(
        """
        SELECT tag, COUNT(*) AS count
        FROM entries, UNNEST(tags) AS tag
        WHERE created_at >= :since
        GROUP BY tag
        ORDER BY count DESC, tag ASC
        LIMIT 5
        """,
        {"since": since},
    )
    return {
        "days": days,
        "entry_count": len(entries),
        "type_breakdown": {row["type"] or "unknown": int(row["count"]) for row in type_rows},
        "top_who": [{"who": row["who"], "count": int(row["count"])} for row in who_rows],
        "key_themes": [{"tag": row["tag"], "count": int(row["count"])} for row in tag_rows],
    }


@mcp.tool(description="Fetch a single entry by its numeric id. [read-only]")
def get_entry_by_id(entry_id: int) -> dict[str, Any]:
    rows = _fetch_entries(
        """
        SELECT id, created_at, updated_at, type, who, title, content, language, status, source, tags
        FROM entries
        WHERE id = :entry_id
        """,
        {"entry_id": int(entry_id)},
    )
    if not rows:
        return {"found": False}
    return {"found": True, "entry": _serialize_entry(rows[0])}


@mcp.tool(description="Hybrid search combining vector similarity and keyword match. Better than pure semantic for proper nouns, IDs, and exact phrases. [read-only]")
def hybrid_search(query: str, limit: int = 10) -> dict[str, Any]:
    if not query or not query.strip():
        return {"query": query, "results": [], "error": "Query is required."}

    bounded_limit = max(1, min(int(limit), 50))
    raw_results = hybrid_search_entries(query.strip(), bounded_limit, None)
    relevance_map: dict[int, float] = {}
    ordered_ids: list[int] = []
    for row in raw_results:
        entry_id = int(row["id"])
        if entry_id not in relevance_map:
            ordered_ids.append(entry_id)
        relevance_map[entry_id] = float(row.get("relevance") or 0.0)

    hydrated = _fetch_entries_by_ids(ordered_ids)
    results: list[dict[str, Any]] = []
    for entry_id in ordered_ids:
        row = hydrated.get(entry_id)
        if not row:
            continue
        item = _serialize_entry(row)
        item["relevance"] = relevance_map.get(entry_id, 0.0)
        results.append(item)
    return {"query": query, "count": len(results), "results": results}


@mcp.tool(description="Return recent entries for a specific topic from the CONFIG vocabulary. [read-only]")
def recent_by_topic(topic: str, days: int = 30, limit: int = 50) -> dict[str, Any]:
    normalized_topic = (topic or "").strip().lower()
    if normalized_topic not in VALID_TOPICS:
        return {"error": f"Invalid topic: {topic}", "valid_topics": TOPIC_VOCABULARY}

    bounded_days = max(1, min(int(days), 3650))
    bounded_limit = max(1, min(int(limit), 200))
    since = datetime.utcnow() - timedelta(days=bounded_days)
    rows = _fetch_entries(
        """
        SELECT id, created_at, updated_at, type, who, title, content, language, status, source, tags
        FROM entries
        WHERE topic = :topic
          AND created_at >= :since
        ORDER BY created_at DESC, id DESC
        LIMIT :limit
        """,
        {"topic": normalized_topic, "since": since, "limit": bounded_limit},
    )
    return {
        "topic": normalized_topic,
        "days": bounded_days,
        "count": len(rows),
        "entries": [_serialize_entry(row) for row in rows],
    }


@mcp.tool(description="List all topics in use with entry counts, plus the count of untagged entries. [read-only]")
def list_topics_with_counts() -> dict[str, Any]:
    topic_rows = _fetch_entries(
        """
        SELECT topic, COUNT(*) AS count
        FROM entries
        WHERE topic IS NOT NULL
        GROUP BY topic
        ORDER BY count DESC, topic ASC
        """
    )
    untagged_rows = _fetch_entries(
        """
        SELECT COUNT(*) AS count
        FROM entries
        WHERE topic IS NULL
        """
    )
    return {
        "topics": [{"topic": row["topic"], "count": int(row["count"])} for row in topic_rows],
        "untagged": int(untagged_rows[0]["count"]) if untagged_rows else 0,
    }


@mcp.tool(description="Count entries matching any combination of topic, type, language, status, date_from, and date_to. All parameters optional. [read-only]")
def count_entries(
    topic: Optional[str] = None,
    entry_type: Optional[str] = None,
    language: Optional[str] = None,
    status: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> dict[str, Any]:
    normalized_topic = (topic or "").strip().lower() or None
    if normalized_topic and normalized_topic not in VALID_TOPICS:
        return {"error": f"Invalid topic: {topic}", "valid_topics": TOPIC_VOCABULARY}

    normalized_type = (entry_type or "").strip().lower() or None
    if normalized_type and normalized_type not in VALID_TYPES:
        return {"error": f"Invalid type: {entry_type}", "valid_types": sorted(VALID_TYPES)}

    since, until, error = _date_bounds(date_from=date_from, date_to=date_to)
    if error:
        return error

    sql = "SELECT COUNT(*) AS count FROM entries WHERE 1=1"
    params: dict[str, Any] = {}
    filters_applied: dict[str, Any] = {}

    if normalized_topic:
        sql += " AND topic = :topic"
        params["topic"] = normalized_topic
        filters_applied["topic"] = normalized_topic
    if normalized_type:
        sql += " AND type = :entry_type"
        params["entry_type"] = normalized_type
        filters_applied["entry_type"] = normalized_type
    if language:
        normalized_language = language.strip().lower()
        sql += " AND language = :language"
        params["language"] = normalized_language
        filters_applied["language"] = normalized_language
    if status:
        normalized_status = status.strip().lower()
        sql += " AND status = :status"
        params["status"] = normalized_status
        filters_applied["status"] = normalized_status
    if since is not None:
        sql += " AND created_at >= :since"
        params["since"] = since
        filters_applied["date_from"] = date_from
    if until is not None:
        sql += " AND created_at < :until"
        params["until"] = until
        filters_applied["date_to"] = date_to

    rows = _fetch_entries(sql, params)
    count_value = int(rows[0]["count"]) if rows else 0
    return {"count": count_value, "filters_applied": filters_applied}


def main() -> None:
    ensure_entries_table()
    if not Path(MCP_CERT_FILE).exists():
        raise RuntimeError(f"Missing MCP certificate file: {MCP_CERT_FILE}")
    if not Path(MCP_KEY_FILE).exists():
        raise RuntimeError(f"Missing MCP key file: {MCP_KEY_FILE}")

    logger.info("Starting Open Brain MCP server on https://%s:%s/mcp", MCP_HOST, MCP_PORT)
    app = mcp.streamable_http_app()
    uvicorn.run(
        app,
        host=MCP_HOST,
        port=MCP_PORT,
        ssl_certfile=MCP_CERT_FILE,
        ssl_keyfile=MCP_KEY_FILE,
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
    )


if __name__ == "__main__":
    main()
