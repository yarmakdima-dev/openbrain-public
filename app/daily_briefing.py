from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from openai import OpenAI
from sqlalchemy import create_engine, text
from telegram import Bot

from app.config import get_config
from app.job_runs import track_job

load_dotenv()
cfg = get_config()
logger = logging.getLogger("openbrain.daily_briefing")
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
)

APP_TZ = ZoneInfo(cfg.app.timezone)
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID_FILE = Path('/home/ubuntu/openbrain_starter/logs/last_chat_id.txt')
BRIEFING_MODEL = "gpt-4o-mini"
BRIEFING_TITLE_TEMPLATE = "Morning Briefing {date}"

BRIEFING_SYSTEM_PROMPT = """You create a short daily morning briefing from a personal journal and task list.

Return JSON only with this exact schema:
{
  "selected_tasks": [
    {"id": 123, "urgency": "high", "reason": "One short sentence"}
  ],
  "opening_nudge": "One short sentence"
}

Rules:
- You may ONLY select IDs that appear in the OPEN TASKS block.
- Never reference any item from YESTERDAY'S CONTEXT or FRESH IDEAS as a task. Those sections are background context for tone only.
- Select at most 5 tasks.
- Prioritize by due date, age, and importance.
- "reason" must explain why today. It must NOT restate the task title.
- Keep each reason short, maximum 12 words.
- Keep opening_nudge short, maximum 15 words.
- Return valid JSON only. No prose, no markdown, no commentary outside JSON."""

engine = create_engine(DATABASE_URL, future=True, pool_pre_ping=True)
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None


@dataclass
class BriefingResult:
    date_label: str
    briefing_text: str
    entry_id: Optional[int] = None
    used_llm: bool = False


def _normalize_whitespace(value: str) -> str:
    return " ".join((value or "").split())


def load_last_chat_id() -> Optional[int]:
    if not CHAT_ID_FILE.exists():
        return None
    raw = CHAT_ID_FILE.read_text().strip()
    return int(raw) if raw.isdigit() else None


def _current_time(now: Optional[datetime] = None) -> datetime:
    return now.astimezone(APP_TZ) if now else datetime.now(tz=APP_TZ)


def _fetch_rows(sql: str, params: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
    with engine.begin() as conn:
        rows = conn.execute(text(sql), params or {}).mappings().all()
    return [dict(r) for r in rows]


def fetch_open_tasks() -> list[dict[str, Any]]:
    current = _current_time().date()
    return _fetch_rows(
        """
        SELECT id, created_at, updated_at, content, title, type, status, due_date
        FROM entries
        WHERE type = 'task' AND status = 'open'
          AND (due_date IS NULL OR due_date <= :today)
        ORDER BY CASE WHEN due_date IS NULL THEN 1 ELSE 0 END, due_date ASC, created_at ASC, id ASC
        """,
        {"today": current},
    )


def fetch_yesterday_entries(now: Optional[datetime] = None) -> list[dict[str, Any]]:
    current = _current_time(now)
    since = current.astimezone(ZoneInfo('UTC')) - timedelta(days=1)
    return _fetch_rows(
        """
        SELECT id, created_at, updated_at, type, who, title, content
        FROM entries
        WHERE created_at >= :since
          AND COALESCE(type, '') NOT IN ('review', 'briefing')
        ORDER BY created_at ASC, id ASC
        """,
        {"since": since},
    )


def fetch_recent_ideas(now: Optional[datetime] = None) -> list[dict[str, Any]]:
    current = _current_time(now)
    since = current.astimezone(ZoneInfo('UTC')) - timedelta(days=3)
    return _fetch_rows(
        """
        SELECT id, created_at, updated_at, title, content, status
        FROM entries
        WHERE type = 'idea'
          AND GREATEST(created_at, updated_at) >= :since
          AND COALESCE(status, '') != 'done'
        ORDER BY GREATEST(created_at, updated_at) DESC, id DESC
        """,
        {"since": since},
    )


def _days_open(created_at: datetime, now: Optional[datetime] = None) -> int:
    current = _current_time(now)
    return max(0, (current.date() - created_at.astimezone(APP_TZ).date()).days)


def _date_label(now: Optional[datetime] = None) -> str:
    return _current_time(now).strftime("%B %-d, %A")


def _task_label(task: dict[str, Any]) -> str:
    return _normalize_whitespace((task.get('title') or task.get('content') or '').strip())


def _due_label(task: dict[str, Any], now: Optional[datetime] = None) -> str:
    due_date = task.get('due_date')
    if not due_date:
        return 'no due date'
    current = _current_time(now).date()
    if due_date < current:
        return f'overdue since {due_date.isoformat()}'
    if due_date == current:
        return 'due today'
    if due_date == current + timedelta(days=1):
        return 'due tomorrow'
    return f'due {due_date.isoformat()}'


def build_briefing_prompt(open_tasks: list[dict[str, Any]], yesterday_entries: list[dict[str, Any]], fresh_ideas: list[dict[str, Any]], now: Optional[datetime] = None) -> str:
    today_label = _date_label(now)
    open_tasks_block = "\n".join(
        f"[#{task['id']}] [{task['created_at'].astimezone(APP_TZ).strftime('%Y-%m-%d')}] ({_days_open(task['created_at'], now)} days open, {_due_label(task, now)}) {_task_label(task)}"
        for task in open_tasks
    ) or "(none)"
    yesterday_block = "\n".join(
        f"[{entry['created_at'].astimezone(APP_TZ).strftime('%Y-%m-%d %H:%M')}] [{entry.get('type')}] [{entry.get('who') or '-'}] {_normalize_whitespace(entry.get('content') or '')[:220]}"
        for entry in yesterday_entries
    ) or "(none)"
    ideas_block = "\n".join(
        f"[{idea['created_at'].astimezone(APP_TZ).strftime('%Y-%m-%d')}] {_normalize_whitespace(idea.get('title') or idea.get('content') or '')[:180]}"
        for idea in fresh_ideas
    ) or "(none)"
    return f"""TODAY:
{today_label}

OPEN TASKS:
{open_tasks_block}

YESTERDAY'S CONTEXT (background only — DO NOT reference these as tasks):
{yesterday_block}

FRESH IDEAS (background only — DO NOT reference by ID):
{ideas_block}

Now write the morning briefing."""


def _generate_briefing_payload(prompt: str) -> dict[str, Any]:
    if not openai_client:
        raise RuntimeError("OpenAI client is not configured")
    response = openai_client.chat.completions.create(
        model=BRIEFING_MODEL,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": BRIEFING_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        max_tokens=220,
    )
    content = (response.choices[0].message.content or "").strip()
    if not content:
        raise RuntimeError("Empty briefing response")
    payload = json.loads(content)
    if not isinstance(payload, dict):
        raise RuntimeError("Briefing response is not a JSON object")
    return payload


def _briefing_task_lines_have_ids(briefing_text: str) -> bool:
    task_lines = []
    for line in (briefing_text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith(("🔴", "🟡", "⚪")):
            task_lines.append(stripped)
    if not task_lines:
        return False
    return all(re.search(r"#\d+\b", line) for line in task_lines)


def _validate_briefing_ids(briefing_text: str, allowed_ids: set[int]) -> str:
    extracted_ids = re.findall(r"#(\d+)", briefing_text or "")
    invented = [raw_id for raw_id in extracted_ids if int(raw_id) not in allowed_ids]
    if not invented:
        return briefing_text
    logger.warning("Briefing hallucinated IDs: %s. Allowed: %s", invented, sorted(allowed_ids))
    return briefing_text.rstrip() + "\n\n⚠️ Note: some IDs above may be incorrect — verify with /show before acting."


def _urgency_emoji(urgency: str) -> str:
    normalized = _normalize_whitespace(urgency).lower()
    return {"high": "🔴", "medium": "🟡", "low": "⚪"}.get(normalized, "⚪")


def _render_structured_briefing(
    payload: dict[str, Any],
    open_tasks: list[dict[str, Any]],
    now: Optional[datetime] = None,
) -> str:
    current = _current_time(now)
    lines = [f"🌅 {current.strftime('%B %-d, %A')}", ""]
    if current.weekday() >= 5:
        lines.append("It's the weekend — only tackle these if you feel like it.")
        lines.append("")

    task_lookup = {int(task["id"]): task for task in open_tasks}
    selected_tasks = payload.get("selected_tasks")
    if not isinstance(selected_tasks, list):
        raise RuntimeError("selected_tasks must be a list")

    rendered_count = 0
    seen_ids: set[int] = set()
    for item in selected_tasks[:5]:
        if not isinstance(item, dict):
            continue
        task_id_raw = item.get("id")
        try:
            task_id = int(task_id_raw)
        except (TypeError, ValueError):
            logger.warning("Briefing returned invalid task id payload: %r", task_id_raw)
            continue
        if task_id in seen_ids:
            continue
        task = task_lookup.get(task_id)
        if not task:
            logger.warning("Briefing selected task id not in open_tasks: %s", task_id)
            continue
        seen_ids.add(task_id)
        reason = _normalize_whitespace(str(item.get("reason") or ""))
        if not reason:
            raise RuntimeError(f"Briefing reason missing for task #{task_id}")
        lines.append(f"{_urgency_emoji(str(item.get('urgency') or 'low'))} {_task_label(task)} #{task_id} — {reason}")
        rendered_count += 1

    if rendered_count == 0:
        raise RuntimeError("Briefing selected no valid open tasks")

    opening_nudge = _normalize_whitespace(str(payload.get("opening_nudge") or ""))
    if not opening_nudge:
        raise RuntimeError("Briefing opening_nudge is missing")
    lines.append("")
    lines.append(f"👉 {opening_nudge}")
    return "\n".join(lines)


def _simple_fallback_briefing(open_tasks: list[dict[str, Any]], now: Optional[datetime] = None) -> str:
    current = _current_time(now)
    date_line = f"🌅 {current.strftime('%B %-d, %A')}"
    lines = [date_line, ""]
    if current.weekday() >= 5:
        lines.append("It's the weekend — only tackle these if you feel like it.")
        lines.append("")
    if not open_tasks:
        lines[-1:] = [] if lines[-1:] == [""] else lines[-1:]
        return f"🌅 {current.strftime('%B %-d, %A')} — No open tasks. Enjoy your morning."

    top_tasks = open_tasks[:3]
    for task in top_tasks:
        lines.append(f"• {_task_label(task)} #{task['id']}")
    lines.append("")
    lines.append("⚠️ Briefing generation failed, showing fallback view.")
    return "\n".join(lines)


def save_briefing_entry(briefing_text: str, now: Optional[datetime] = None) -> int:
    current = _current_time(now)
    title = BRIEFING_TITLE_TEMPLATE.format(date=current.strftime('%Y-%m-%d'))
    with engine.begin() as conn:
        row = conn.execute(
            text(
                """
                INSERT INTO entries (content, tags, who, title, language, type, status, source)
                VALUES (:content, :tags, :who, :title, :language, :entry_type, :status, :source)
                RETURNING id
                """
            ),
            {
                "content": briefing_text,
                "tags": ["Briefing"],
                "who": "System",
                "title": title,
                "language": "en",
                "entry_type": "briefing",
                "status": None,
                "source": "briefing",
            },
        ).scalar_one()
    return int(row)


def generate_and_store_daily_briefing(now: Optional[datetime] = None) -> BriefingResult:
    with track_job("briefing"):
        current = _current_time(now)
        open_tasks = fetch_open_tasks()
        allowed_ids = {int(task["id"]) for task in open_tasks}
        if not open_tasks:
            text_value = f"🌅 {current.strftime('%B %-d, %A')} — No open tasks. Enjoy your morning."
            entry_id = save_briefing_entry(text_value, current)
            return BriefingResult(date_label=_date_label(current), briefing_text=text_value, entry_id=entry_id, used_llm=False)

        yesterday_entries = fetch_yesterday_entries(current)
        fresh_ideas = fetch_recent_ideas(current)
        prompt = build_briefing_prompt(open_tasks, yesterday_entries, fresh_ideas, current)

        used_llm = False
        try:
            payload = _generate_briefing_payload(prompt)
            text_value = _render_structured_briefing(payload, open_tasks, current)
            text_value = _validate_briefing_ids(text_value, allowed_ids)
            if not _briefing_task_lines_have_ids(text_value):
                raise RuntimeError("Briefing task lines are missing task IDs")
            used_llm = True
        except Exception as exc:
            logger.warning("Daily briefing LLM failed, using fallback: %s", exc)
            text_value = _simple_fallback_briefing(open_tasks, current)

        entry_id = save_briefing_entry(text_value, current)
        return BriefingResult(date_label=_date_label(current), briefing_text=text_value, entry_id=entry_id, used_llm=used_llm)


def send_briefing_message(briefing_text: str) -> bool:
    chat_id = load_last_chat_id()
    if not chat_id or not TELEGRAM_BOT_TOKEN:
        return False

    async def _send() -> None:
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        await bot.send_message(chat_id=chat_id, text=briefing_text)
        await bot.close()

    asyncio.run(_send())
    return True


def run_daily_briefing() -> Optional[BriefingResult]:
    result = generate_and_store_daily_briefing()
    send_briefing_message(result.briefing_text)
    return result
