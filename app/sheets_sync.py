from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from openai import OpenAI
from sqlalchemy import create_engine, text

from app.config import get_config
from app.entity_resolver import resolve_who_to_ids_safe
from app.sheet_sync_common import (
    clear_sync_error,
    fetch_snapshots,
    set_sync_error,
    three_way_resolve,
    write_snapshots,
)

load_dotenv()
cfg = get_config()
logger = logging.getLogger("openbrain.sheets_sync")

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
GOOGLE_SHEETS_CREDENTIALS_PATH = os.getenv("GOOGLE_SHEETS_CREDENTIALS_PATH", "").strip()
GOOGLE_SHEET_URL = os.getenv("GOOGLE_SHEET_URL", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_EMBEDDING_MODEL = cfg.memory.embedding_model
APP_TZ = ZoneInfo(cfg.app.timezone)
VALID_TYPES = {"highlight", "book", "person", "idea", "task", "review", "briefing", "memory_note", "instructions"}

SHEET_HEADERS = ["Date", "Type", "Who", "Title", "Full Entry", "DB_ID", "Synced"]
GENERAL_SNAPSHOT_FIELDS = ["type", "who", "title", "content"]
GENERAL_TAB_NAME = "general"
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


@dataclass
class GeneralSyncResult:
    pulled_updated: int = 0
    pulled_skipped: int = 0
    conflicts: list[int] = field(default_factory=list)
    pushed: int = 0
    error: Optional[str] = None

    @property
    def pulled_count(self) -> int:
        return self.pulled_updated

    @property
    def synced_count(self) -> int:
        return self.pushed

    @property
    def updated_count(self) -> int:
        return 0

    @property
    def sheet_url(self) -> str:
        return GOOGLE_SHEET_URL

    def __str__(self) -> str:
        return str(self.pulled_updated)


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


def _parse_general_sheet_values(entry: dict[str, Any], sheet_row: list[str]) -> dict[str, Any]:
    return {
        "type": _normalize_type(sheet_row[1], entry.get("type") or "highlight"),
        "who": (sheet_row[2] or "").strip() or None,
        "title": (sheet_row[3] or "").strip() or None,
        "content": (sheet_row[4] or "").strip(),
    }


def _general_db_values(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": entry.get("type") or "highlight",
        "who": entry.get("who"),
        "title": entry.get("title"),
        "content": entry.get("content") or "",
    }


def pull_sheet_updates_to_database() -> GeneralSyncResult:
    # Currently unused: Log/General is read-only under Option 2; both former callers (orchestrator + /pull) removed.
    ensure_sync_schema()
    worksheet = _open_worksheet()
    _ensure_header_row(worksheet)
    existing_rows = _get_existing_sheet_rows(worksheet)
    entry_map = _fetch_entry_map()
    row_ids = [entry_id for entry_id in existing_rows if entry_id in entry_map]
    with engine.begin() as conn:
        snapshots = fetch_snapshots(conn, GENERAL_TAB_NAME, row_ids)
    synced_now = datetime.now(tz=APP_TZ)
    sync_updates: list[dict[str, Any]] = []
    result = GeneralSyncResult()

    for entry_id, (row_index, row_values) in existing_rows.items():
        try:
            entry = entry_map.get(entry_id)
            if not entry:
                result.pulled_skipped += 1
                continue

            snapshot = snapshots.get(entry_id)
            if snapshot is None:
                result.pulled_skipped += 1
                continue

            sheet_values = _parse_general_sheet_values(entry, row_values)
            db_values = _general_db_values(entry)
            decisions: dict[str, tuple[str, Any]] = {}
            conflict_fields: list[str] = []
            for field in GENERAL_SNAPSHOT_FIELDS:
                decision, value = three_way_resolve(field, snapshot.get(field), sheet_values[field], db_values[field])
                decisions[field] = (decision, value)
                if decision == "conflict":
                    conflict_fields.append(field)

            with engine.begin() as conn:
                if conflict_fields:
                    message = "General tab conflict — " + "; ".join(
                        f"{field}: sheet={sheet_values[field]!r}, db={db_values[field]!r}"
                        for field in conflict_fields
                    )
                    set_sync_error(conn, "entries", entry_id, message)
                    result.conflicts.append(entry_id)
                    result.pulled_skipped += 1
                    continue

                sheet_wins = {
                    field: value
                    for field, (decision, value) in decisions.items()
                    if decision == "take_sheet"
                }
                if not sheet_wins:
                    clear_sync_error(conn, "entries", entry_id)
                    continue

                set_clauses: list[str] = []
                params: dict[str, Any] = {"entry_id": entry_id}
                for field in GENERAL_SNAPSHOT_FIELDS:
                    if field not in sheet_wins:
                        continue
                    params[field] = sheet_wins[field]
                    set_clauses.append(f"{field} = :{field}")

                if "who" in sheet_wins:
                    params["who_ids"] = resolve_who_to_ids_safe(sheet_wins.get("who"), conn)
                    set_clauses.append("who_ids = :who_ids")

                if "content" in sheet_wins:
                    try:
                        embedding = _get_openai_embedding(sheet_wins["content"])
                    except Exception as exc:
                        logger.warning(
                            "Embedding regeneration failed during reverse sync for entry #%s: %s",
                            entry_id,
                            exc,
                        )
                        embedding = None
                    if embedding is not None:
                        params["embedding"] = _embedding_to_vector_literal(embedding)
                        set_clauses.append("embedding = CAST(:embedding AS vector)")
                    else:
                        set_clauses.append("embedding = NULL")

                set_clauses.append("updated_at = NOW()")
                conn.execute(
                    text(f"UPDATE entries SET {', '.join(set_clauses)} WHERE id = :entry_id"),
                    params,
                )
                clear_sync_error(conn, "entries", entry_id)

            result.pulled_updated += 1
            sync_updates.append({
                "range": f"G{row_index}:G{row_index}",
                "values": [[_format_synced_timestamp(synced_now)]],
            })
        except Exception:
            logger.exception("General reverse sync failed for entry #%s; continuing", entry_id)
            result.pulled_skipped += 1

    if sync_updates:
        worksheet.batch_update(sync_updates, value_input_option="RAW")

    logger.info("Google Sheets reverse sync finished: %s updated database rows", result.pulled_updated)
    return result


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

    with engine.begin() as conn:
        snapshot_rows = [
            {
                "id": entry["id"],
                "type": entry.get("type"),
                "who": entry.get("who"),
                "title": entry.get("title"),
                "content": entry.get("content"),
            }
            for entry in entries
        ]
        snapshots_written = write_snapshots(
            conn,
            GENERAL_TAB_NAME,
            "entries",
            snapshot_rows,
            GENERAL_SNAPSHOT_FIELDS,
        )

    logger.info(
        "Google Sheets forward sync finished: %s new rows, %s updated rows, %s snapshots",
        len(rows_to_append),
        len(rows_to_update),
        snapshots_written,
    )
    return SyncResult(
        synced_count=snapshots_written,
        updated_count=len(rows_to_update),
        pulled_count=0,
        sheet_url=GOOGLE_SHEET_URL,
    )


def sync_google_sheet_bidirectional() -> GeneralSyncResult:
    result = GeneralSyncResult()
    # Log/General tab is read-only (Option 2): reverse-sync disabled, push-only DB->sheet.
    logger.info("Log/General reverse-sync disabled (read-only, Option 2); skipping sheet->DB write-back")

    forward_result = sync_entries_to_google_sheet()
    result.pushed = forward_result.synced_count
    return result
