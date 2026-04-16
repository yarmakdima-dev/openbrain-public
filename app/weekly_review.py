from __future__ import annotations

import asyncio
import ast
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import anthropic
import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from openai import OpenAI
from sqlalchemy import create_engine, text
from telegram import Bot

from app.config import get_config

load_dotenv()
cfg = get_config()
APP_TZ = ZoneInfo(cfg.app.timezone)
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
GOOGLE_SHEETS_CREDENTIALS_PATH = os.getenv("GOOGLE_SHEETS_CREDENTIALS_PATH", "").strip()
GOOGLE_SHEET_URL = os.getenv("GOOGLE_SHEET_URL", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
OPENAI_EMBEDDING_MODEL = cfg.memory.embedding_model
CHAT_ID_FILE = Path('/home/ubuntu/openbrain_starter/logs/last_chat_id.txt')
REVIEW_TITLE_TEMPLATE = 'Weekly Review {start} - {end}'
REVIEW_STYLE_REFERENCE = """📋 March 24-31

You had one foot in endings and the other in momentum. The workshop handover finished, and it hurt more than you expected because it confirmed that what you built mattered. At the same time, LeverX shifted from possibility to something more concrete, and OpenBrain stopped being just an idea and started behaving like a real asset.

🔄 Evolution

Two weeks ago you were noting friction and uncertainty around AI work; now that has turned into specific ideas, client-facing language, and actual tasks. "Build portfolio from Open Brain" moved from idea to action, which matters more than the still-stuck "Personal website" thread. You also closed a couple of small tasks this week, which is a quiet sign that things are moving, not just accumulating.

🔗 Past echoes

March 5 you wrote that you wanted to work seriously in AI implementation. A few weeks later, the LeverX thread is making that feel less hypothetical. The anxiety around impostor syndrome this week sounds a lot like the fear you described on March 19, but this time you had more evidence to stand on.

💡 Ideas

OpenBrain is still the live one. It keeps absorbing other threads: portfolio, AI positioning, even the way you think about your work. "Personal website" still exists, but right now it looks more like a side branch than the trunk."""
REVIEW_SYSTEM_PROMPT = """You are writing a weekly review for one person based on their private journal.

Write in English.
Maximum 200 words total.
Use exactly four sections, marked only with these emoji lines:
📋 [date range]
🔄 Evolution
🔗 Past echoes
💡 Ideas

Formatting is critical:
- Put each emoji heading on its own line.
- Leave one blank line after each heading.
- Leave one blank line between sections.
- Each section must be 3-4 sentences maximum.
- Do not include open tasks inside the main review. Open tasks will be sent separately.

Do not use bullet points.
Do not use extra headers.
Do not sound bureaucratic, therapeutic, or generic.
Write like a thoughtful friend who has been reading the journal closely and notices patterns the writer may miss.
Keep the tone warm, direct, and much more concise than a typical journal summary.

Section rules:

1. 📋 [date range]
Write one short narrative paragraph about the week:
- what happened
- what shifted
- what mattered emotionally or practically
Then include one non-obvious insight:
- a pattern
- contradiction
- recurring theme
- tension
Do not mention open tasks here.

2. 🔄 Evolution
Track the observation -> idea -> task loop across time.
Look for:
- older highlights that seem to have turned into ideas this week
- ideas that now have matching tasks
- tasks completed this week
- ideas that are stuck
- open tasks older than 7 days
Pick only the 2-3 most interesting progressions.
Keep this section short: 3-4 sentences max.
No lists. No bullets.
Natural prose only.
Be concrete about dates and movement over time.

3. 🔗 Past echoes
Write 2-3 concise connections between this week and older entries.
Use the older semantically similar entries provided below.
Focus on:
- progress on long-running themes
- recurring emotional patterns
- promises made to self
- things that evolved over time
Reference specific dates and specific content.

4. 💡 Ideas
Focus only on ideas that appear active or relevant.
At most 2-3 ideas.
Say which ideas are gaining energy, which seem dormant, and give one concrete suggestion for the most active idea.
Keep this section short.

Important:
- Stay grounded in the supplied entries only.
- Do not invent facts.
- Prefer specific observations over abstract advice.
- Keep it tight, vivid, and useful."""

engine = create_engine(DATABASE_URL, future=True, pool_pre_ping=True)
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None


@dataclass
class ReviewResult:
    start_date: datetime
    end_date: datetime
    date_range_label: str
    title: str
    review_text: str
    entry_id: Optional[int] = None


def _normalize_whitespace(value: str) -> str:
    return " ".join((value or "").split())


def _vector_literal(embedding: list[float]) -> str:
    return "[" + ",".join(f"{x:.8f}" for x in embedding) + "]"


def _coerce_embedding(raw_embedding: Any) -> Optional[list[float]]:
    if not raw_embedding:
        return None
    if isinstance(raw_embedding, str):
        try:
            parsed = ast.literal_eval(raw_embedding)
        except (ValueError, SyntaxError):
            return None
        if isinstance(parsed, (list, tuple)):
            try:
                return [float(x) for x in parsed]
            except (TypeError, ValueError):
                return None
        return None
    if isinstance(raw_embedding, (list, tuple)):
        try:
            return [float(x) for x in raw_embedding]
        except (TypeError, ValueError):
            return None
    return None


def remember_chat_id(chat_id: int) -> None:
    CHAT_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
    CHAT_ID_FILE.write_text(str(chat_id).strip())


def load_last_chat_id() -> Optional[int]:
    if not CHAT_ID_FILE.exists():
        return None
    raw = CHAT_ID_FILE.read_text().strip()
    return int(raw) if raw.isdigit() else None


def _open_spreadsheet():
    creds = Credentials.from_service_account_file(
        GOOGLE_SHEETS_CREDENTIALS_PATH,
        scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"],
    )
    client = gspread.authorize(creds)
    return client.open_by_url(GOOGLE_SHEET_URL)


def _ensure_reviews_worksheet():
    spreadsheet = _open_spreadsheet()
    try:
        worksheet = spreadsheet.worksheet("Reviews")
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title="Reviews", rows=1000, cols=2)
    if worksheet.row_values(1)[:2] != ["Date", "Review text"]:
        worksheet.update("A1:B1", [["Date", "Review text"]])
    return worksheet


def _current_time() -> datetime:
    return datetime.now(tz=APP_TZ)


def _review_window(now: Optional[datetime] = None) -> tuple[datetime, datetime]:
    end_dt = now or _current_time()
    start_dt = end_dt - timedelta(days=7)
    return start_dt, end_dt


def _format_date_only(value: datetime) -> str:
    return value.astimezone(APP_TZ).strftime("%Y-%m-%d")


def _review_label(start_dt: datetime, end_dt: datetime) -> str:
    return f"{_format_date_only(start_dt)} - {_format_date_only(end_dt)}"


def _fetch_rows(sql: str, params: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
    with engine.begin() as conn:
        rows = conn.execute(text(sql), params or {}).mappings().all()
    return [dict(r) for r in rows]


def fetch_weekly_entries(now: Optional[datetime] = None) -> tuple[datetime, datetime, list[dict[str, Any]]]:
    start_dt, end_dt = _review_window(now)
    entries = _fetch_rows(
        """
        SELECT id, created_at, updated_at, type, who, title, content, language, source, embedding
        FROM entries
        WHERE created_at >= :start_dt
          AND created_at < :end_dt
          AND COALESCE(type, '') NOT IN ('review', 'briefing')
        ORDER BY created_at ASC, id ASC
        """,
        {"start_dt": start_dt, "end_dt": end_dt},
    )
    return start_dt, end_dt, entries


def fetch_open_tasks(now: Optional[datetime] = None) -> list[dict[str, Any]]:
    current = now or _current_time()
    rows = _fetch_rows(
        """
        SELECT id, created_at, content, due_date
        FROM entries
        WHERE type = 'task' AND status = 'open'
          AND (due_date IS NULL OR due_date <= :today)
        ORDER BY created_at ASC, id ASC
        """,
        {"today": current.date()},
    )
    for row in rows:
        row["days_open"] = max(0, (current.date() - row["created_at"].astimezone(APP_TZ).date()).days)
    return rows


def fetch_idea_entries() -> list[dict[str, Any]]:
    return _fetch_rows(
        """
        SELECT id, created_at, updated_at, content, title, embedding
        FROM entries
        WHERE type = 'idea'
        ORDER BY created_at DESC, id DESC
        """
    )


def _similar_entries(embedding: list[float], sql: str, params: dict[str, Any], limit: int = 5) -> list[dict[str, Any]]:
    vector_literal = _vector_literal(embedding)
    merged = dict(params)
    merged['embedding'] = vector_literal
    merged['limit'] = limit
    return _fetch_rows(sql, merged)


def fetch_past_echo_candidates(weekly_entries: list[dict[str, Any]], start_dt: datetime) -> list[dict[str, Any]]:
    scored: dict[int, dict[str, Any]] = {}
    for entry in weekly_entries:
        embedding = _coerce_embedding(entry.get("embedding"))
        if not embedding:
            continue
        vector_literal = _vector_literal(embedding)
        rows = _fetch_rows(
            """
            SELECT id, created_at, type, who, content,
                   1 - (embedding <=> CAST(:embedding AS vector)) AS relevance
            FROM entries
            WHERE created_at < :start_dt
              AND embedding IS NOT NULL
              AND COALESCE(type, '') NOT IN ('review', 'briefing')
            ORDER BY embedding <=> CAST(:embedding AS vector)
            LIMIT 10
            """,
            {"embedding": vector_literal, "start_dt": start_dt},
        )
        for row in rows:
            existing = scored.get(int(row["id"]))
            score = float(row.get("relevance") or 0.0)
            if not existing or score > existing["relevance"]:
                row["relevance"] = score
                scored[int(row["id"])] = row
    return sorted(scored.values(), key=lambda r: float(r.get("relevance") or 0.0), reverse=True)[:10]


def fetch_highlight_to_idea_links(weekly_entries: list[dict[str, Any]], start_dt: datetime) -> list[dict[str, Any]]:
    links: list[dict[str, Any]] = []
    seen_highlights: set[int] = set()
    for idea in [entry for entry in weekly_entries if (entry.get('type') or '') == 'idea']:
        embedding = _coerce_embedding(idea.get('embedding'))
        if not embedding:
            continue
        rows = _similar_entries(
            embedding,
            """
            SELECT id, created_at, type, who, content,
                   1 - (embedding <=> CAST(:embedding AS vector)) AS relevance
            FROM entries
            WHERE created_at < :start_dt
              AND embedding IS NOT NULL
              AND type = 'highlight'
            ORDER BY embedding <=> CAST(:embedding AS vector)
            LIMIT :limit
            """,
            {'start_dt': start_dt},
            3,
        )
        if not rows:
            continue
        top = rows[0]
        if int(top['id']) in seen_highlights or float(top.get('relevance') or 0.0) < 0.78:
            continue
        seen_highlights.add(int(top['id']))
        links.append({'highlight': top, 'idea': idea, 'relevance': float(top.get('relevance') or 0.0)})
    return links[:5]


def fetch_idea_to_task_links(idea_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    links: list[dict[str, Any]] = []
    seen_tasks: set[int] = set()
    for idea in idea_entries:
        embedding = _coerce_embedding(idea.get('embedding'))
        if not embedding:
            continue
        rows = _similar_entries(
            embedding,
            """
            SELECT id, created_at, updated_at, type, status, content,
                   1 - (embedding <=> CAST(:embedding AS vector)) AS relevance
            FROM entries
            WHERE embedding IS NOT NULL
              AND type = 'task'
            ORDER BY embedding <=> CAST(:embedding AS vector)
            LIMIT :limit
            """,
            {},
            3,
        )
        if not rows:
            continue
        top = rows[0]
        if int(top['id']) in seen_tasks or float(top.get('relevance') or 0.0) < 0.80:
            continue
        seen_tasks.add(int(top['id']))
        links.append({'idea': idea, 'task': top, 'relevance': float(top.get('relevance') or 0.0)})
    return sorted(links, key=lambda item: item['relevance'], reverse=True)[:5]


def fetch_completed_tasks_this_week(start_dt: datetime, end_dt: datetime) -> list[dict[str, Any]]:
    return _fetch_rows(
        """
        SELECT id, created_at, updated_at, content
        FROM entries
        WHERE type = 'task'
          AND status = 'done'
          AND updated_at >= :start_dt
          AND updated_at < :end_dt
        ORDER BY updated_at DESC, id DESC
        """,
        {'start_dt': start_dt, 'end_dt': end_dt},
    )


def fetch_aging_open_tasks(now: datetime) -> list[dict[str, Any]]:
    rows = _fetch_rows(
        """
        SELECT id, created_at, content, due_date
        FROM entries
        WHERE type = 'task' AND status = 'open'
          AND (due_date IS NULL OR due_date <= :today)
        ORDER BY created_at ASC, id ASC
        """,
        {"today": now.date()},
    )
    aging: list[dict[str, Any]] = []
    for row in rows:
        days_open = max(0, (now.date() - row['created_at'].astimezone(APP_TZ).date()).days)
        if days_open > 7:
            row['days_open'] = days_open
            aging.append(row)
    return aging[:8]


def fetch_stuck_ideas(start_dt: datetime, end_dt: datetime, idea_to_task_links: list[dict[str, Any]]) -> list[dict[str, Any]]:
    linked_idea_ids = {int(link['idea']['id']) for link in idea_to_task_links}
    candidate_ideas = _fetch_rows(
        """
        SELECT id, created_at, updated_at, content, embedding
        FROM entries
        WHERE type = 'idea'
          AND created_at < :older_than
        ORDER BY created_at ASC, id ASC
        """,
        {'older_than': end_dt - timedelta(days=14)},
    )
    stuck: list[dict[str, Any]] = []
    recent_since = end_dt - timedelta(days=14)
    for idea in candidate_ideas:
        if int(idea['id']) in linked_idea_ids:
            continue
        embedding = _coerce_embedding(idea.get('embedding'))
        if not embedding:
            continue
        recent_related = _similar_entries(
            embedding,
            """
            SELECT id, created_at, type, content,
                   1 - (embedding <=> CAST(:embedding AS vector)) AS relevance
            FROM entries
            WHERE created_at >= :recent_since
              AND created_at < :end_dt
              AND embedding IS NOT NULL
              AND id != :idea_id
              AND COALESCE(type, '') NOT IN ('review', 'briefing')
            ORDER BY embedding <=> CAST(:embedding AS vector)
            LIMIT :limit
            """,
            {'recent_since': recent_since, 'end_dt': end_dt, 'idea_id': int(idea['id'])},
            3,
        )
        top_relevance = float(recent_related[0].get('relevance') or 0.0) if recent_related else 0.0
        if top_relevance < 0.78:
            stuck.append(idea)
    return stuck[:6]


def _format_entry_line(entry: dict[str, Any], include_type: bool = True, truncate_to: Optional[int] = None) -> str:
    date_value = entry["created_at"].astimezone(APP_TZ).strftime("%Y-%m-%d")
    content = _normalize_whitespace(entry.get("content") or "")
    if truncate_to and len(content) > truncate_to:
        content = content[:truncate_to].rstrip() + "..."
    if include_type:
        return f"[{date_value}] [{entry.get('type') or '-'}] [{entry.get('who') or '-'}] {content}"
    return f"[{date_value}] {content}"


def build_weekly_review_prompt(
    start_dt: datetime,
    end_dt: datetime,
    weekly_entries: list[dict[str, Any]],
    open_tasks: list[dict[str, Any]],
    idea_entries: list[dict[str, Any]],
    past_echoes: list[dict[str, Any]],
    highlight_to_idea_links: list[dict[str, Any]],
    idea_to_task_links: list[dict[str, Any]],
    completed_tasks: list[dict[str, Any]],
    stuck_ideas: list[dict[str, Any]],
    aging_tasks: list[dict[str, Any]],
) -> str:
    date_range = _review_label(start_dt, end_dt)
    truncate_weekly = len(weekly_entries) > 30
    weekly_entries_block = "\n".join(
        _format_entry_line(entry, include_type=True, truncate_to=200 if truncate_weekly else None)
        for entry in weekly_entries
    ) or "(none)"
    open_tasks_block = "\n".join(
        f"[#{task['id']}] [{task['created_at'].astimezone(APP_TZ).strftime('%Y-%m-%d')}] ({task['days_open']} days open) {_normalize_whitespace(task['content'])}"
        for task in open_tasks
    ) or "(none)"
    idea_entries_block = "\n".join(
        f"[{entry['created_at'].astimezone(APP_TZ).strftime('%Y-%m-%d')}] {_normalize_whitespace(entry['content'])}"
        for entry in idea_entries
    ) or "(none)"
    past_echoes_block = "\n".join(
        _format_entry_line(entry, include_type=True)
        for entry in past_echoes
    ) or "(none)"
    highlight_to_idea_block = "\n".join(
        f"[old highlight: {link['highlight']['created_at'].astimezone(APP_TZ).strftime('%Y-%m-%d')}] {_normalize_whitespace(link['highlight'].get('content') or '')} -> [idea this week: {link['idea']['created_at'].astimezone(APP_TZ).strftime('%Y-%m-%d')}] {_normalize_whitespace(link['idea'].get('content') or '')}"
        for link in highlight_to_idea_links
    ) or "(none)"
    idea_to_task_block = "\n".join(
        f"[idea: {link['idea']['created_at'].astimezone(APP_TZ).strftime('%Y-%m-%d')}] {_normalize_whitespace(link['idea'].get('content') or '')} -> [task: {link['task']['created_at'].astimezone(APP_TZ).strftime('%Y-%m-%d')}] {_normalize_whitespace(link['task'].get('content') or '')}"
        for link in idea_to_task_links
    ) or "(none)"
    completed_tasks_block = "\n".join(
        f"[completed: {entry['updated_at'].astimezone(APP_TZ).strftime('%Y-%m-%d')}] {_normalize_whitespace(entry.get('content') or '')}"
        for entry in completed_tasks
    ) or "(none)"
    stuck_ideas_block = "\n".join(
        f"[idea: {entry['created_at'].astimezone(APP_TZ).strftime('%Y-%m-%d')}] {_normalize_whitespace(entry.get('content') or '')}"
        for entry in stuck_ideas
    ) or "(none)"
    aging_tasks_block = "\n".join(
        f"[#{task['id']}] [task: {task['created_at'].astimezone(APP_TZ).strftime('%Y-%m-%d')}] ({task['days_open']} days open) {_normalize_whitespace(task.get('content') or '')}"
        for task in aging_tasks
    ) or "(none)"

    return f"""DATE RANGE:
{date_range}

STYLE REFERENCE:
{REVIEW_STYLE_REFERENCE}

THIS WEEK'S ENTRIES:
{weekly_entries_block}

OPEN TASKS:
{open_tasks_block}

IDEA ENTRIES:
{idea_entries_block}

PAST ECHO CANDIDATES:
{past_echoes_block}

EVOLUTION INPUTS

OLDER HIGHLIGHTS SIMILAR TO THIS WEEK'S IDEAS:
{highlight_to_idea_block}

IDEAS WITH MATCHING TASKS:
{idea_to_task_block}

TASKS COMPLETED THIS WEEK:
{completed_tasks_block}

STUCK IDEAS:
{stuck_ideas_block}

AGING OPEN TASKS:
{aging_tasks_block}

Now write the weekly review."""


def _format_review_text(review_text: str) -> str:
    text_value = (review_text or '').strip()
    if not text_value:
        return ''

    text_value = text_value.replace('📋', '\n📋').replace('🔄', '\n🔄').replace('🔗', '\n🔗').replace('💡', '\n💡')
    lines = [line.strip() for line in text_value.splitlines() if line.strip()]

    sections: list[tuple[str, list[str]]] = []
    current_heading = ''
    current_lines: list[str] = []
    for line in lines:
        if line.startswith('📋') or line.startswith('🔄') or line.startswith('🔗') or line.startswith('💡'):
            if current_heading:
                sections.append((current_heading, current_lines))
            current_heading = line
            current_lines = []
        else:
            current_lines.append(line)
    if current_heading:
        sections.append((current_heading, current_lines))

    if not sections:
        return _normalize_whitespace(text_value)

    formatted_sections = []
    for heading, content_lines in sections:
        paragraph = _normalize_whitespace(' '.join(content_lines))
        formatted_sections.append(f"{heading}\n\n{paragraph}".strip())
    return '\n\n'.join(formatted_sections)


def format_open_tasks_message(open_tasks: list[dict[str, Any]]) -> str:
    if not open_tasks:
        return "⏳\n\nNo open tasks."
    lines = ["⏳", ""]
    for task in open_tasks:
        date_text = task['created_at'].astimezone(APP_TZ).strftime('%Y-%m-%d')
        content = _normalize_whitespace(task['content'])
        lines.append(f"#{task['id']} ({task['days_open']}d, {date_text}) {content}")
    return "\n".join(lines)


def split_review_sections(review_text: str) -> list[str]:
    formatted = _format_review_text(review_text)
    if not formatted:
        return []
    parts: list[str] = []
    current: list[str] = []
    for block in formatted.split('\n\n'):
        if block.startswith('📋') or block.startswith('🔄') or block.startswith('🔗') or block.startswith('💡'):
            if current:
                parts.append('\n\n'.join(current).strip())
            current = [block]
        else:
            current.append(block)
    if current:
        parts.append('\n\n'.join(current).strip())
    return [part for part in parts if part]


def _generate_review_text(prompt: str) -> str:
    if not anthropic_client:
        raise RuntimeError("Anthropic client is not configured")
    response = anthropic_client.messages.create(
        model=cfg.providers["anthropic"].model,
        max_tokens=700,
        system=REVIEW_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    parts = []
    for block in response.content:
        if getattr(block, "type", "") == "text":
            parts.append(block.text)
    return _format_review_text("\n".join(parts))


def _get_openai_embedding(input_text: str) -> Optional[list[float]]:
    if not openai_client:
        return None
    response = openai_client.embeddings.create(model=OPENAI_EMBEDDING_MODEL, input=input_text)
    return response.data[0].embedding


def review_matches_current_format(review_text: str) -> bool:
    formatted = _format_review_text(review_text)
    if not formatted:
        return False
    if '⏳' in formatted:
        return False
    if len(formatted.split()) > 220:
        return False
    date_pos = formatted.find('📋')
    evolution_pos = formatted.find('🔄')
    echoes_pos = formatted.find('🔗')
    ideas_pos = formatted.find('💡')
    if min(date_pos, evolution_pos, echoes_pos, ideas_pos) < 0:
        return False
    return date_pos < evolution_pos < echoes_pos < ideas_pos


def get_existing_review_for_window(now: Optional[datetime] = None) -> Optional[ReviewResult]:
    start_dt, end_dt = _review_window(now)
    title = REVIEW_TITLE_TEMPLATE.format(start=_format_date_only(start_dt), end=_format_date_only(end_dt))
    rows = _fetch_rows(
        """
        SELECT id, created_at, title, content
        FROM entries
        WHERE source = 'review' AND title = :title
        ORDER BY id DESC
        LIMIT 1
        """,
        {"title": title},
    )
    if not rows:
        return None
    row = rows[0]
    return ReviewResult(
        start_date=start_dt,
        end_date=end_dt,
        date_range_label=_review_label(start_dt, end_dt),
        title=row.get('title') or title,
        review_text=_format_review_text(row.get('content') or ''),
        entry_id=int(row['id']),
    )


def save_review_entry(review_text: str, start_dt: datetime, end_dt: datetime) -> int:
    title = REVIEW_TITLE_TEMPLATE.format(start=_format_date_only(start_dt), end=_format_date_only(end_dt))
    embedding = _get_openai_embedding(review_text)
    params = {
        "title": title,
        "content": review_text,
        "embedding": _vector_literal(embedding) if embedding else None,
        "tags": ["Review"],
        "who": "System",
        "language": "en",
        "entry_type": "review",
        "status": None,
        "source": "review",
    }
    with engine.begin() as conn:
        existing = conn.execute(
            text("SELECT id FROM entries WHERE source = 'review' AND title = :title ORDER BY id DESC LIMIT 1"),
            {"title": title},
        ).scalar()
        if existing:
            if embedding is not None:
                conn.execute(
                    text(
                        """
                        UPDATE entries
                        SET content = :content,
                            embedding = CAST(:embedding AS vector),
                            tags = :tags,
                            who = :who,
                            language = :language,
                            type = :entry_type,
                            status = :status,
                            source = :source
                        WHERE id = :entry_id
                        """
                    ),
                    {**params, "entry_id": existing},
                )
            else:
                conn.execute(
                    text(
                        """
                        UPDATE entries
                        SET content = :content,
                            embedding = NULL,
                            tags = :tags,
                            who = :who,
                            language = :language,
                            type = :entry_type,
                            status = :status,
                            source = :source
                        WHERE id = :entry_id
                        """
                    ),
                    {**params, "entry_id": existing},
                )
            return int(existing)
        if embedding is not None:
            result = conn.execute(
                text(
                    """
                    INSERT INTO entries (title, content, embedding, tags, who, language, type, status, source)
                    VALUES (:title, :content, CAST(:embedding AS vector), :tags, :who, :language, :entry_type, :status, :source)
                    RETURNING id
                    """
                ),
                params,
            )
        else:
            result = conn.execute(
                text(
                    """
                    INSERT INTO entries (title, content, tags, who, language, type, status, source)
                    VALUES (:title, :content, :tags, :who, :language, :entry_type, :status, :source)
                    RETURNING id
                    """
                ),
                params,
            )
        return int(result.scalar_one())


def write_review_to_sheet(date_range_label: str, review_text: str) -> None:
    worksheet = _ensure_reviews_worksheet()
    values = worksheet.get_all_values()
    for row_index, row in enumerate(values[1:], start=2):
        first_col = (row[0] if row else "").strip()
        if first_col == date_range_label:
            worksheet.update(f"A{row_index}:B{row_index}", [[date_range_label, review_text]])
            return
    worksheet.append_rows([[date_range_label, review_text]], value_input_option="RAW")


def generate_and_store_weekly_review(now: Optional[datetime] = None) -> Optional[ReviewResult]:
    start_dt, end_dt, weekly_entries = fetch_weekly_entries(now)
    if not weekly_entries:
        return None
    open_tasks = fetch_open_tasks(end_dt)
    idea_entries = fetch_idea_entries()
    past_echoes = fetch_past_echo_candidates(weekly_entries, start_dt)
    highlight_to_idea_links = fetch_highlight_to_idea_links(weekly_entries, start_dt)
    idea_to_task_links = fetch_idea_to_task_links(idea_entries)
    completed_tasks = fetch_completed_tasks_this_week(start_dt, end_dt)
    stuck_ideas = fetch_stuck_ideas(start_dt, end_dt, idea_to_task_links)
    aging_tasks = fetch_aging_open_tasks(end_dt)
    prompt = build_weekly_review_prompt(
        start_dt, end_dt, weekly_entries, open_tasks, idea_entries, past_echoes,
        highlight_to_idea_links, idea_to_task_links, completed_tasks, stuck_ideas, aging_tasks
    )
    review_text = _generate_review_text(prompt)
    entry_id = save_review_entry(review_text, start_dt, end_dt)
    date_range_label = _review_label(start_dt, end_dt)
    write_review_to_sheet(date_range_label, review_text)
    return ReviewResult(
        start_date=start_dt,
        end_date=end_dt,
        date_range_label=date_range_label,
        title=REVIEW_TITLE_TEMPLATE.format(start=_format_date_only(start_dt), end=_format_date_only(end_dt)),
        review_text=review_text,
        entry_id=entry_id,
    )


def mark_task_done(entry_id: int) -> tuple[bool, Optional[str]]:
    with engine.begin() as conn:
        row = conn.execute(
            text("UPDATE entries SET status = 'done' WHERE id = :entry_id AND type IN ('task', 'idea') RETURNING type"),
            {"entry_id": entry_id},
        ).mappings().first()
    if not row:
        return False, None
    return True, row.get("type")


def send_review_notification() -> None:
    chat_id = load_last_chat_id()
    if not chat_id or not TELEGRAM_BOT_TOKEN:
        return
    message = "📋 Your weekly review is ready. Send /review to read it here, or check the Reviews tab in your sheet."

    async def _send() -> None:
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        await bot.send_message(chat_id=chat_id, text=message)
        await bot.close()

    asyncio.run(_send())


def run_weekly_review_with_retry() -> Optional[ReviewResult]:
    try:
        result = generate_and_store_weekly_review()
        if result:
            send_review_notification()
        return result
    except Exception:
        time.sleep(1800)
        try:
            result = generate_and_store_weekly_review()
            if result:
                send_review_notification()
            return result
        except Exception:
            return None
