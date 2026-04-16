from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from telegram import Bot

from app.config import get_config

load_dotenv('/home/ubuntu/openbrain_starter/.env')
cfg = get_config()
APP_TZ = ZoneInfo(cfg.app.timezone)
DATABASE_URL = os.getenv('DATABASE_URL', '').strip()
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
CHAT_ID_FILE = Path('/home/ubuntu/openbrain_starter/logs/last_chat_id.txt')

logging.basicConfig(
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    level=os.getenv('LOG_LEVEL', 'INFO').upper(),
)
logger = logging.getLogger('openbrain.reminder_check')
engine = create_engine(DATABASE_URL, future=True, pool_pre_ping=True)


def load_last_chat_id() -> Optional[int]:
    if not CHAT_ID_FILE.exists():
        return None
    raw = CHAT_ID_FILE.read_text().strip()
    return int(raw) if raw.isdigit() else None


def fetch_due_entries(today_date):
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                """
                SELECT id, content, created_at, due_date
                FROM entries
                WHERE due_date = :today_date
                  AND reminded_at IS NULL
                ORDER BY created_at ASC, id ASC
                """
            ),
            {'today_date': today_date},
        ).mappings().all()
    return [dict(r) for r in rows]


def mark_reminded(entry_id: int) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE entries
                SET reminded_at = NOW()
                WHERE id = :entry_id AND reminded_at IS NULL
                """
            ),
            {'entry_id': entry_id},
        )


async def send_due_reminders() -> int:
    logger.info('Standalone reminder notifications are disabled; due tasks are surfaced in the daily briefing only')
    return 0


def main() -> None:
    try:
        sent_count = asyncio.run(send_due_reminders())
        logger.info('Reminder check finished; sent=%s', sent_count)
    except Exception:
        logger.exception('Reminder check crashed unexpectedly')


if __name__ == '__main__':
    main()
