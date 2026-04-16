from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from openai import OpenAI
from sqlalchemy import create_engine, text

from app.config import get_config

load_dotenv()
cfg = get_config()
logger = logging.getLogger("openbrain.sheets_sync")

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
GOOGLE_SHEETS_CREDENTIALS_PATH = os.getenv("GOOGLE_SHEETS_CREDENTIALS_PATH", "").strip()
GOOGLE_SHEET_URL = os.getenv("GOOGLE_SHEET_URL", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_EMBEDDING_MODEL = cfg.memory.embedding_model
APP_TZ = ZoneInfo(cfg.app.timezone)
VALID_TYPES = {"highlight", "book", "person", "idea", "task", "review", "briefing"}

SHEET_HEADERS = ["Date", "Type", "Who", "Title", "Full Entry", "DB_ID", "Synced"]
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


@dataclass
class SyncResult:
    synced_count: int
    updated_count: int
    pulled_count: int
    sheet_url: str


engine = create_engine(DATABASE_URL, future=True, pool_pre_ping=True)
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None


def _require_sync_config() -> None:
    missing = []
    if not DATABASE_URL:
        missing.append("DATABASE_URL")
    if not GOOGLE_SHEETS_CREDENTIALS_PATH:
        missing.append("GOOGLE_SHEETS_CREDENTIALS_PATH")
    if not GOOGLE_SHEET_URL:
        missing.append("GOOGLE_SHEET_URL")
    if missing:
        raise RuntimeError(f"Missing sync configuration: {', '.join(missing)}")


def ensure_sync_schema() -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                ALTER TABLE entries ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
                UPDATE entries SET updated_at = created_at WHERE updated_at IS NULL;

                CREATE OR REPLACE FUNCTION set_entries_updated_at()
                RETURNS TRIGGER AS $$
                BEGIN
                    NEW.updated_at = NOW();
                    RETURN NEW;
                END;
                $$ LANGUAGE plpgsql;

                DROP TRIGGER IF EXISTS entries_set_updated_at ON entries;
                CREATE TRIGGER entries_set_updated_at
                BEFORE UPDATE ON entries
                FOR EACH ROW
                EXECUTE FUNCTION set_entries_updated_at();
                """
            )
        )


def _open_worksheet() -> gspread.Worksheet:
    _require_sync_config()
    try:
        creds = Credentials.from_service_account_file(
            GOOGLE_SHEETS_CREDENTIALS_PATH,
            scopes=SCOPES,
        )
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_url(GOOGLE_SHEET_URL)
        return spreadsheet.sheet1
    except Exception as exc:
        logger.warning("Google Sheets connection failed: %s", exc)
        raise RuntimeError(
            "Google Sheets is not reachable yet. Check that the Sheets API is enabled in the same Google Cloud project as the service account, and that the sheet is shared with that service account."
        ) from exc


def _ensure_header_row(worksheet: gspread.Worksheet) -> None:
    first_row = worksheet.row_values(1)
    if first_row[: len(SHEET_HEADERS)] != SHEET_HEADERS:
        worksheet.update("A1:G1", [SHEET_HEADERS])


def _get_existing_sheet_rows(worksheet: gspread.Worksheet) -> dict[int, tuple[int, list[str]]]:
    values = worksheet.get_all_values()
    existing: dict[int, tuple[int, list[str]]] = {}
    for row_index, row in enumerate(values[1:], start=2):
        padded = row + [""] * max(0, len(SHEET_HEADERS) - len(row))
        raw = (padded[5] or "").strip()
        if raw.isdigit():
            existing[int(raw)] = (row_index, padded[: len(SHEET_HEADERS)])
    return existing


def _fetch_entries_for_sync() -> list[dict[str, Any]]:
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                """
                SELECT id, created_at, updated_at, type, who, title, content, language, status, tags, topic
                FROM entries
                WHERE COALESCE(type, '') NOT IN ('review', 'briefing')
                ORDER BY created_at ASC, id ASC
                """
            )
        ).mappings().all()
    return [dict(row) for row in rows]


def _fetch_entry_map() -> dict[int, dict[str, Any]]:
    return {int(row["id"]): row for row in _fetch_entries_for_sync()}


def _format_date_value(created_at: datetime) -> str:
    local_dt = created_at.astimezone(APP_TZ)
    return f"{local_dt.day}.{local_dt.month}.{str(local_dt.year)[2:]}"


def _format_synced_timestamp(moment: datetime) -> str:
    return moment.astimezone(APP_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _parse_synced_timestamp(raw: str) -> datetime | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=APP_TZ)
    except ValueError:
        return None


def _build_sheet_row(entry: dict[str, Any], synced_at: datetime) -> list[str]:
    return [
        _format_date_value(entry["created_at"]),
        entry.get("type") or "highlight",
        entry.get("who") or "",
        entry.get("title") or "",
        entry.get("content") or "",
        str(entry["id"]),
        _format_synced_timestamp(synced_at),
    ]


def _normalize_type(value: str, fallback: str) -> str:
    normalized = (value or "").strip().lower()
    if normalized in VALID_TYPES:
        return normalized
    return fallback


def _get_openai_embedding(input_text: str) -> list[float] | None:
    if not openai_client:
        return None
    response = openai_client.embeddings.create(
        model=OPENAI_EMBEDDING_MODEL,
        input=input_text,
    )
    return response.data[0].embedding


def _embedding_to_vector_literal(embedding: list[float]) -> str:
    return "[" + ",".join(f"{x:.8f}" for x in embedding) + "]"


def _update_database_from_sheet(entry_id: int, entry: dict[str, Any], sheet_row: list[str]) -> list[str]:
    sheet_type = _normalize_type(sheet_row[1], entry.get("type") or "highlight")
    sheet_who = (sheet_row[2] or "").strip() or (entry.get("who") or "")
    sheet_title = (sheet_row[3] or "").strip() or (entry.get("title") or "")
    sheet_content = (sheet_row[4] or "").strip() or (entry.get("content") or "")

    changed_fields: list[str] = []
    if sheet_content != (entry.get("content") or ""):
        changed_fields.append("Full Entry")
    if sheet_who != (entry.get("who") or ""):
        changed_fields.append("Who")
    if sheet_title != (entry.get("title") or ""):
        changed_fields.append("Title")
    if sheet_type != (entry.get("type") or "highlight"):
        changed_fields.append("Type")

    if not changed_fields:
        return []

    # The Synced column records the last sync moment, not the user's sheet edit time.
    # If fields differ, trust the sheet values on reverse sync instead of treating Synced
    # as a conflict timestamp. Otherwise real sheet edits can be skipped incorrectly.

    try:
        embedding = _get_openai_embedding(sheet_content)
    except Exception as exc:
        logger.warning(
            "Embedding regeneration failed during reverse sync for entry #%s: %s",
            entry_id,
            exc,
        )
        embedding = None

    status = entry.get("status")
    if sheet_type == "task" and not status:
        status = "open"
    elif sheet_type != "task":
        status = None

    params = {
        "entry_id": entry_id,
        "content": sheet_content,
        "who": sheet_who or None,
        "title": sheet_title or None,
        "entry_type": sheet_type,
        "status": status,
        "tags": entry.get("tags") or ["General"],
        "topic": entry.get("topic"),
        "embedding": _embedding_to_vector_literal(embedding) if embedding else None,
    }

    with engine.begin() as conn:
        if embedding is not None:
            conn.execute(
                text(
                    """
                    UPDATE entries
                    SET content = :content,
                        who = :who,
                        title = :title,
                        type = :entry_type,
                        status = :status,
                        tags = :tags,
                        topic = :topic,
                        embedding = CAST(:embedding AS vector)
                    WHERE id = :entry_id
                    """
                ),
                params,
            )
        else:
            conn.execute(
                text(
                    """
                    UPDATE entries
                    SET content = :content,
                        who = :who,
                        title = :title,
                        type = :entry_type,
                        status = :status,
                        tags = :tags,
                        topic = :topic,
                        embedding = NULL
                    WHERE id = :entry_id
                    """
                ),
                params,
            )

    logger.info("Reverse synced entry #%s: updated %s", entry_id, ", ".join(changed_fields))
    return changed_fields


def pull_sheet_updates_to_database() -> int:
    ensure_sync_schema()
    worksheet = _open_worksheet()
    _ensure_header_row(worksheet)
    existing_rows = _get_existing_sheet_rows(worksheet)
    entry_map = _fetch_entry_map()
    synced_now = datetime.now(tz=APP_TZ)
    sync_updates: list[dict[str, Any]] = []
    pulled_count = 0

    for entry_id, (row_index, row_values) in existing_rows.items():
        entry = entry_map.get(entry_id)
        if not entry:
            continue
        changed_fields = _update_database_from_sheet(entry_id, entry, row_values)
        if changed_fields:
            pulled_count += 1
            sync_updates.append({
                "range": f"G{row_index}:G{row_index}",
                "values": [[_format_synced_timestamp(synced_now)]],
            })

    if sync_updates:
        worksheet.batch_update(sync_updates, value_input_option="RAW")

    logger.info("Google Sheets reverse sync finished: %s updated database rows", pulled_count)
    return pulled_count


def reset_google_sheet_from_database() -> SyncResult:
    ensure_sync_schema()
    worksheet = _open_worksheet()
    worksheet.clear()
    _ensure_header_row(worksheet)
    entries = _fetch_entries_for_sync()
    synced_at = datetime.now(tz=APP_TZ)
    rows_to_append = [_build_sheet_row(entry, synced_at) for entry in entries]
    if rows_to_append:
        worksheet.append_rows(rows_to_append, value_input_option="RAW")
    logger.info("Google Sheets reset sync finished: %s rows", len(rows_to_append))
    return SyncResult(synced_count=len(rows_to_append), updated_count=0, pulled_count=0, sheet_url=GOOGLE_SHEET_URL)


def sync_entries_to_google_sheet() -> SyncResult:
    ensure_sync_schema()
    worksheet = _open_worksheet()
    _ensure_header_row(worksheet)
    existing_rows = _get_existing_sheet_rows(worksheet)
    entries = _fetch_entries_for_sync()
    synced_at = datetime.now(tz=APP_TZ)

    rows_to_append: list[list[str]] = []
    rows_to_update: list[dict[str, Any]] = []

    for entry in entries:
        row_values = _build_sheet_row(entry, synced_at)
        existing = existing_rows.get(int(entry["id"]))
        if not existing:
            rows_to_append.append(row_values)
            continue
        row_index, current_values = existing
        if current_values[:5] != row_values[:5]:
            rows_to_update.append({"range": f"A{row_index}:G{row_index}", "values": [row_values]})

    if rows_to_update:
        worksheet.batch_update(rows_to_update, value_input_option="RAW")
    if rows_to_append:
        worksheet.append_rows(rows_to_append, value_input_option="RAW")

    logger.info(
        "Google Sheets forward sync finished: %s new rows, %s updated rows",
        len(rows_to_append),
        len(rows_to_update),
    )
    return SyncResult(
        synced_count=len(rows_to_append),
        updated_count=len(rows_to_update),
        pulled_count=0,
        sheet_url=GOOGLE_SHEET_URL,
    )


def sync_google_sheet_bidirectional() -> SyncResult:
    pulled_count = 0
    try:
        pulled_count = pull_sheet_updates_to_database()
    except Exception:
        logger.exception("Reverse sync failed; continuing with forward sync")

    forward_result = sync_entries_to_google_sheet()
    return SyncResult(
        synced_count=forward_result.synced_count,
        updated_count=forward_result.updated_count,
        pulled_count=pulled_count,
        sheet_url=forward_result.sheet_url,
    )
