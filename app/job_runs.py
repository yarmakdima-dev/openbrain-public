from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
SERVICE_START = datetime.now(timezone.utc)
engine = create_engine(DATABASE_URL, future=True, pool_pre_ping=True)


def ensure_job_runs_table() -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS job_runs (
                    id SERIAL PRIMARY KEY,
                    job_name TEXT NOT NULL,
                    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    finished_at TIMESTAMPTZ,
                    status TEXT NOT NULL CHECK (status IN ('running','done','failed')),
                    error_message TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS idx_job_runs_job_name_started
                    ON job_runs (job_name, started_at DESC)
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS idx_job_runs_status
                    ON job_runs (status)
                """
            )
        )


def mark_running_job_runs_failed_on_startup() -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE job_runs
                SET status = 'failed',
                    finished_at = NOW(),
                    error_message = COALESCE(error_message, '') || ' [marked failed on startup - likely crash]'
                WHERE status = 'running' AND started_at < :service_start
                """
            ),
            {"service_start": SERVICE_START},
        )


@contextmanager
def track_job(job_name: str) -> Iterator[None]:
    ensure_job_runs_table()
    with engine.begin() as conn:
        run_id = conn.execute(
            text(
                """
                INSERT INTO job_runs (job_name, status)
                VALUES (:job_name, 'running')
                RETURNING id
                """
            ),
            {"job_name": job_name},
        ).scalar_one()

    try:
        yield
    except Exception as exc:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE job_runs
                    SET status = 'failed',
                        finished_at = NOW(),
                        error_message = :error_message
                    WHERE id = :run_id
                    """
                ),
                {"run_id": int(run_id), "error_message": str(exc)},
            )
        raise

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE job_runs
                SET status = 'done',
                    finished_at = NOW(),
                    error_message = NULL
                WHERE id = :run_id
                """
            ),
            {"run_id": int(run_id)},
        )


def get_recent_job_runs(limit: int = 10) -> list[dict[str, object]]:
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                """
                SELECT id, job_name, status, started_at, finished_at, error_message
                FROM job_runs
                ORDER BY started_at DESC
                LIMIT :limit
                """
            ),
            {"limit": limit},
        ).mappings().all()
    return [dict(row) for row in rows]
