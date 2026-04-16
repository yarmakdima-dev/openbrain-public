import asyncio
from html import escape
import json
from datetime import date, datetime, timedelta, timezone
import logging
import os
import re
import tempfile
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from openai import OpenAI
import anthropic
from google import genai
from sqlalchemy import create_engine, text
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.config import get_config
from app.sheets_sync import (
    GOOGLE_SHEET_URL,
    pull_sheet_updates_to_database,
    sync_entries_to_google_sheet,
    sync_google_sheet_bidirectional,
)
from app.weekly_review import format_open_tasks_message, fetch_open_tasks, generate_and_store_weekly_review, get_existing_review_for_window, mark_task_done, remember_chat_id, review_matches_current_format, split_review_sections
from app.daily_briefing import generate_and_store_daily_briefing
from app.job_runs import ensure_job_runs_table, get_recent_job_runs, mark_running_job_runs_failed_on_startup
from app.llm_clients import answer_from_context, classify_intent

load_dotenv()
cfg = get_config()
APP_TZ = ZoneInfo(cfg.app.timezone)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
)
logger = logging.getLogger("openbrain")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


# =========================
# Environment / Config
# =========================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

MAX_PROVIDER_OUTPUT_CHARS = int(os.getenv("MAX_PROVIDER_OUTPUT_CHARS", "3500"))
OPENAI_TRANSCRIPTION_MODEL = os.getenv("OPENAI_TRANSCRIPTION_MODEL", "whisper-1").strip()

OPENAI_EMBEDDING_MODEL = cfg.memory.embedding_model
ASK_TOP_K = cfg.commands.ask.top_k
ASK_MIN_RELEVANCE = cfg.commands.ask.threshold
ASK_FAIL_MESSAGE = cfg.commands.ask.fail_message
SAVE_CONFIRMATION_TEXT = cfg.telegram.save_confirmation_text
TAGGING_MODEL = "gpt-4o-mini"
TYPE_CHOICES = ("highlight", "book", "person", "idea", "task")
TOPIC_CHOICES = tuple(cfg.topics.vocabulary)
TOPIC_CHOICES_TEXT = ", ".join(TOPIC_CHOICES)
WHO_CHOICES = (
    "Я", "Юля", "Даша", "Кирилл", "Варя", "Брат", "Мы", "Дети", "Друзья",
    "Работа", "Книга", "Природа", "Здоровье",
)
TAGGING_SYSTEM_PROMPT = f"""Extract metadata for one journal entry. Return JSON only:
{{"who":"...","title":"...","language":"...","type":"...","topic":"..."}}

Rules:
- who must be one of: Я, Юля, Даша, Кирилл, Варя, Брат, Мы, Дети, Друзья, Работа, Книга, Природа, Здоровье.
- If unclear, use Я.
- who must always be in Russian.
- title must be 2-4 words in the same language as the entry.
- language must be one of: ru, en, pl, de.
- type must be one of: highlight, book, person, idea, task.

Topic rules — assign exactly one topic. The topic must come from this exact list: {TOPIC_CHOICES_TEXT}. Do not invent topics.

Topic definitions:
- openbrain: the user's personal knowledge/productivity system project. Mentions of OpenBrain, topic field, briefing, weekly review, Telegram bot, MCP, VPS, PostgreSQL, embeddings in the context of this project, Codex, Claude Code, prompts for builders.
- career: work, job search, consulting, AI implementation work, LeverX, EPAM, clients, portfolio, professional positioning, meetings, presentations for work.
- family: wife Юля, daughter Даша, daughter Варя, son Кирилл, brother Андрей, sister Оля, neece Аня, parents Саша и Люда, household, relationships, kids' activities, family logistics.
- health: sleep, exercise, sports (including МТБ, downhill, running), doctors, nutrition, mental state, wellbeing, injuries, risks to body.
- learning: books, reading, courses, languages (Polish, German), skills, Audible, podcasts, articles for learning.
- finance: money, salary, investments, spending, taxes, bills, purchases of significant items.
- travel: trips, flights, hotels, places to visit, logistics for going somewhere.
- ideas: open-ended thoughts, speculations, possibilities not tied to any of the above categories. Use this ONLY when the entry is a genuine abstract thought that doesn't fit a concrete domain. Do not use for project ideas (those go to openbrain or career).
- admin: bureaucracy, documents, visas, insurance, appointments, equipment/tools setup (monitor, laptop, home office), cemetery/grave maintenance, household repairs.
- other: use ONLY when no category above fits. Prefer a specific category over 'other' whenever any category plausibly fits.

Classification examples:
- "OpenBrain: проверить topic field" -> openbrain
- "обсудить с Кириллом риски даунхила" -> health (personal risk/sport, Кирилл is family context but the entry is about sport risk)
- "выбрать книгу в Audible" -> learning
- "книга Thinking in Systems — дочитать" -> learning
- "подобрать монитор для домашнего стола" -> admin (equipment setup)
- "подумать над portfolio для AI консалтинга" -> career
- "напомнить про благоустройство могил" -> admin
- "записаться к стоматологу" -> health
- "что если запустить курс по AI" -> ideas

Bias: when an entry plausibly fits a specific topic, choose the specific topic. Only fall back to 'other' when truly nothing fits.

Type rules:
- task = a concrete future action the user is committed to doing.
- idea = something the user is considering, imagining, or exploring, without commitment.
- highlight = something that happened, a reflection, or a general observation.
- book = primarily about a book or reading.
- person = primarily about a person.

Be conservative:
- false task positives are worse than false negatives.
- if unsure between highlight and task, use highlight.
- reflections like "I think I should change my approach" are highlight, not task.
- completed actions are highlight, not task.

Examples:
- "Need to call Максим this week" -> task
- "Надо не забыть купить гирю" -> task
- "I should prepare for the LeverX meeting" -> task
- "Записаться к стоматологу" -> task
- "Had a great day, walked in the forest" -> highlight
- "Interesting idea about building a course" -> idea
- "I think I should change my approach" -> highlight
- "I called Максим today" -> highlight

Use highlight when unclear."""

TASK_EXTRACTION_SYSTEM_PROMPT = """You are checking whether a journal entry contains a real embedded follow-up item.

Return JSON only:
{"task_decision":"none|explicit_task|embedded_task|embedded_idea","task_text":"...","due_date":"YYYY-MM-DD or null","reason":"..."}

Definitions:
- explicit_task = the whole entry is mainly a task.
- embedded_task = the entry is mainly a highlight, but it contains one clear action item that should become a separate task.
- embedded_idea = the entry is mainly a highlight, but it contains one clear idea that should become a separate idea entry.
- none = no committed future action and no standalone idea worth extracting.

Rules:
- Be conservative. False positives are worse than false negatives.
- Do not create a task or idea from a vague reflection, mood, or general thought.
- Do not create a task from something already completed.
- A task should be a concrete future action.
- An idea should be a clear standalone concept, improvement, or possible direction.
- If the entry is mostly a journal highlight but includes one clear obligation or next action, use embedded_task.
- If the entry is mostly a journal highlight but includes one clear reusable idea, use embedded_idea.
- The extracted text MUST be in the same language as the original entry.
- The extracted text MUST include enough context to be understood on its own, without reading the original entry.
- BAD: "Добавить визуалы" (wrong language, no context)
- GOOD: "Add visuals to LinkedIn posts" (same language, clear context)
- Keep the extracted text short, clear, and standalone.
- If the due date is relative, resolve it from the reference date provided below.
- If no reliable due date is stated, return null.
- Use due_date only for tasks, not ideas.

Examples:
- "Need to call Максим this week" -> explicit_task, "Need to call Максим this week"
- "Надо записаться к стоматологу" -> explicit_task, "Записаться к стоматологу"
- "Had a great day, walked in the forest" -> none
- "Met with LeverX. They want a proposal by Friday. Feeling good." -> embedded_task, "Prepare proposal for LeverX by Friday"
- "Reflection: yesterday's LinkedIn post went well. Today I realized team photos would make it better." -> embedded_idea, "Add team photos to LinkedIn posts"
- "Interesting idea about building a course" -> none
- "I think I should change my approach" -> none"""

DATE_EXTRACTION_SYSTEM_PROMPT_TEMPLATE = """You are a date extraction assistant. If the user message contains a reminder or follow-up intention with a time reference, return valid JSON only in exactly one of these formats: {{"has_date": true, "due_date": "YYYY-MM-DD", "clean_content": "message with time reference removed"}} or {{"has_date": false}}. Today's date is {today}. Keep clean_content in the same language as the user message. Be conservative — only extract dates when the intention is explicit."""

TAGS = [
    "Finance",
    "Health",
    "Work",
    "Family",
    "Ideas",
    "Learning",
    "Travel",
    "Projects",
    "Personal",
    "General",
]

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")
if not DATABASE_URL:
    raise RuntimeError("Missing DATABASE_URL")
if not OPENAI_EMBEDDING_MODEL:
    raise RuntimeError("Missing embedding model in CONFIG.yaml memory.embedding_model")

engine = create_engine(DATABASE_URL, future=True, pool_pre_ping=True)

openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None


# =========================
# Provider Registry
# =========================

@dataclass
class ProviderState:
    name: str
    configured: bool
    model: str
    fallback_providers: list[str]
    available: bool = False
    active_model: Optional[str] = None
    error: Optional[str] = None


PROVIDERS: dict[str, ProviderState] = {
    "openai": ProviderState(
        name="openai",
        configured=bool(OPENAI_API_KEY and cfg.providers["openai"].enabled and cfg.providers["openai"].model),
        model=cfg.providers["openai"].model,
        fallback_providers=cfg.providers["openai"].fallback_order,
    ),
    "anthropic": ProviderState(
        name="anthropic",
        configured=bool(ANTHROPIC_API_KEY and cfg.providers["anthropic"].enabled and cfg.providers["anthropic"].model),
        model=cfg.providers["anthropic"].model,
        fallback_providers=cfg.providers["anthropic"].fallback_order,
    ),
    "google": ProviderState(
        name="google",
        configured=bool(GEMINI_API_KEY and cfg.providers["google"].enabled and cfg.providers["google"].model),
        model=cfg.providers["google"].model,
        fallback_providers=cfg.providers["google"].fallback_order,
    ),
}


def truncate(text_value: str, limit: int = MAX_PROVIDER_OUTPUT_CHARS) -> str:
    if len(text_value) <= limit:
        return text_value
    return text_value[: limit - 3] + "..."


def normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def parse_json_object_from_text(text_value: str) -> dict[str, Any]:
    raw = (text_value or '').strip()
    if raw.startswith('```'):
        raw = re.sub(r'^```(?:json)?\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw)
    try:
        return json.loads(raw)
    except Exception:
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def detect_language_bucket(user_text: str) -> str:
    if re.search(r"[а-яА-ЯёЁ]", user_text):
        return "ru"
    if re.search(r"[ąćęłńóśźżĄĆĘŁŃÓŚŹŻ]", user_text):
        return "pl"
    if re.search(r"[äöüßÄÖÜ]", user_text):
        return "de"
    return "en"


def choose_provider_for_general_query(user_text: str, language_override: Optional[str] = None) -> str:
    lang = language_override or detect_language_bucket(user_text)
    preferred_provider = cfg.get_language_provider(lang)

    if preferred_provider:
        state = PROVIDERS.get(preferred_provider)
        if state and state.available and state.active_model:
            return preferred_provider

        for fallback_provider in cfg.providers[preferred_provider].fallback_order:
            fallback_state = PROVIDERS.get(fallback_provider)
            if fallback_state and fallback_state.available and fallback_state.active_model:
                return fallback_provider

    general_order = cfg.routing.task_type_provider_preferences.get(
        "general",
        ["openai", "anthropic", "google"],
    )
    for provider_name in general_order:
        state = PROVIDERS.get(provider_name)
        if state and state.available and state.active_model:
            return provider_name

    for provider_name, state in PROVIDERS.items():
        if state.available and state.active_model:
            return provider_name

    return "none"


QUESTION_PREFIXES = (
    "what ", "what are ", "what is ", "why ", "how ", "when ", "where ", "who ", "which ",
    "is ", "are ", "do ", "does ", "did ", "can ", "could ",
    "would ", "should ", "will ", "tell me ", "give me ", "show me ",
    "list ", "find ", "summarize ", "get me ", "pull up ", "search ",
    "remind me ", "explain ", "compare ", "help me ",
    "czy ", "jak ", "co ", "kiedy ", "gdzie ",
    "dlaczego ", "ktory ", "który ", "mozesz ", "możesz ",
    "powiedz ", "wyjasnij ", "wyjaśnij ", "porownaj ", "porównaj ",
    "pokaż ", "daj mi ", "znajdź ", "wymień ", "przypomnij ", "wypisz ",
    "какой ", "какая ", "какие ", "как ", "когда ", "где ",
    "почему ", "что ", "кто ", "сколько ", "можешь ", "помоги ",
    "объясни ", "покажи ", "дай мне ", "найди ", "перечисли ",
    "напомни ", "расскажи ", "выведи ", "какие ",
    "wer ", "was ", "wie ", "wann ", "wo ", "warum ",
    "welche ", "welcher ", "kannst ", "erklär ", "erklar ", "vergleich ",
    "zeig mir ", "gib mir ", "finde ", "liste ", "such ",
)

DATABASE_QUERY_PREFIXES = (
    "give me ", "show me ", "show ", "list ", "find ", "summarize ", "tell me ", "get me ",
    "pull up ", "search ", "remind me ", "what are ", "what is ",
    "покажи ", "дай мне ", "найди ", "перечисли ", "напомни ", "расскажи ", "выведи ", "какие ",
    "pokaż ", "daj mi ", "znajdź ", "wymień ", "przypomnij ", "wypisz ",
    "zeig mir ", "gib mir ", "finde ", "liste ", "such ",
)

REMINDER_INTENT_PREFIXES = (
    "remind me ", "follow up ", "напомни ", "напомнить ", "przypomnij ", "przypomnieć ", "erinnere mich ",
)
REMINDER_TIME_KEYWORDS = (
    "tomorrow", "today", "next ", "week", "month", "year", "day", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "завтра", "сегодня", "на следующ", "недел", "месяц", "год", "день", "понедельник", "вторник", "сред", "четверг", "пятниц", "суббот", "воскресень",
    "jutro", "dzisiaj", "następn", "tydzień", "miesiąc", "rok", "dzień", "poniedział", "wtorek", "środ", "czwart", "piątek", "sobot", "niedziel",
    "morgen", "heute", "nächst", "woche", "monat", "jahr", "tag", "montag", "dienstag", "mittwoch", "donnerstag", "freitag", "samstag", "sonntag",
)
TASK_QUERY_KEYWORDS = ("task", "tasks", "todo", "to-do", "unresolved", "open task", "open tasks", "задач", "задачи", "задачу", "taski", "zadania", "zadanie", "aufgabe", "aufgaben")
IDEA_QUERY_KEYWORDS = ("idea", "ideas", "иде", "идея", "идеи", "pomys", "idee")
ENTRY_QUERY_KEYWORDS = ("entry", "entries", "note", "notes", "journal", "memory", "record", "records", "запис", "дневник", "entrys")
TIME_FILTERS = {
    "last week": 7,
    "past week": 7,
    "from last week": 7,
    "за прошлую неделю": 7,
    "на прошлой неделе": 7,
    "за последнюю неделю": 7,
    "ostatni tydzień": 7,
    "w zeszłym tygodniu": 7,
    "letzte woche": 7,
}
SEARCH_STRIP_PHRASES = (
    "my entries", "entries", "entry", "my notes", "notes", "my journal", "journal", "my memory", "memory",
    "записи", "запись", "мой дневник", "дневник", "moje wpisy", "wpisy", "notatki",
)
SEARCH_STOPWORDS = {
    "all", "my", "me", "the", "a", "an", "about", "for", "on", "from", "last", "week", "past",
    "entries", "entry", "notes", "note", "journal", "memory", "show", "find", "search", "list", "summarize",
    "open", "tasks", "task", "ideas", "idea", "все", "мне", "мои", "мой", "записи", "запись", "дневник",
}

def extract_time_filter_days(user_text: str) -> Optional[int]:
    normalized = normalize_whitespace(user_text).lower()
    for phrase, days in TIME_FILTERS.items():
        if phrase in normalized:
            return days
    return None


def extract_database_search_topic(user_text: str) -> str:
    normalized = normalize_whitespace(user_text)
    lowered = normalized.lower()
    for prefix in DATABASE_QUERY_PREFIXES:
        if lowered.startswith(prefix):
            normalized = normalized[len(prefix):].strip()
            lowered = normalized.lower()
            break

    for phrase in SEARCH_STRIP_PHRASES:
        normalized = re.sub(rf"\b{re.escape(phrase)}\b", " ", normalized, flags=re.IGNORECASE)

    normalized = re.sub(r"\b(about|for|on|from last week|last week|past week|за прошлую неделю|на прошлой неделе)\b", " ", normalized, flags=re.IGNORECASE)
    normalized = normalize_whitespace(normalized)
    return normalized or user_text.strip()


def extract_search_type_priority(user_text: str) -> Optional[str]:
    normalized = normalize_whitespace(user_text).lower()
    if "start with ideas" in normalized or "ideas first" in normalized or "сначала идеи" in normalized:
        return "idea"
    if "start with tasks" in normalized or "tasks first" in normalized or "сначала задачи" in normalized:
        return "task"
    return None


def extract_scoped_search_topic(user_text: str) -> str:
    topic = extract_database_search_topic(user_text)
    topic = re.sub(r"\b(start with|starting with|begin with|ideas first|tasks first)\b.*$", " ", topic, flags=re.IGNORECASE)
    topic = re.sub(r"\b(сначала|начиная с)\b.*$", " ", topic, flags=re.IGNORECASE)
    topic = re.sub(r"\b(all|entries|entry|notes|note|with|about|containing|focused on)\b", " ", topic, flags=re.IGNORECASE)
    topic = re.sub(r"\b(все|записи|запись|заметки|заметка|про|о|об|с)\b", " ", topic, flags=re.IGNORECASE)
    topic = re.sub(r"^[^\wА-Яа-яЁё]+|[^\wА-Яа-яЁё]+$", " ", topic)
    return normalize_whitespace(topic) or extract_database_search_topic(user_text)


def match_who_in_text(user_text: str) -> Optional[str]:
    normalized = normalize_whitespace(user_text).lower()
    for who in sorted(WHO_CHOICES, key=len, reverse=True):
        pattern = rf"(?<!\w){re.escape(who.lower())}(?!\w)"
        if re.search(pattern, normalized, flags=re.IGNORECASE):
            return who
    return None


def classify_database_query(user_text: str) -> Optional[dict[str, Any]]:
    normalized = normalize_whitespace(user_text).lower()
    if not normalized:
        return None

    starts_with_query_prefix = any(normalized.startswith(prefix) for prefix in DATABASE_QUERY_PREFIXES)
    has_question_mark = '?' in normalized
    word_count = len(normalized.split())

    # Reflective notes often contain a trailing question but should still be saved as notes.
    # Only treat a bare question mark as a DB query signal when the message is short and query-like.
    starts_like_query = starts_with_query_prefix or (has_question_mark and word_count <= 12)
    if not starts_like_query:
        return None

    days = extract_time_filter_days(normalized)

    mentions_entries = any(keyword in normalized for keyword in ENTRY_QUERY_KEYWORDS)
    who_match = match_who_in_text(user_text)
    if mentions_entries or who_match:
        return {
            "kind": "search",
            "days": days,
            "who": who_match,
            "query": extract_scoped_search_topic(user_text),
            "type_priority": extract_search_type_priority(user_text),
        }

    if any(keyword in normalized for keyword in TASK_QUERY_KEYWORDS):
        return {"kind": "tasks", "days": days, "status": "open"}

    if any(keyword in normalized for keyword in IDEA_QUERY_KEYWORDS):
        return {"kind": "ideas", "days": days}

    return None


def filter_entries_by_days(entries: list[dict[str, Any]], days: Optional[int]) -> list[dict[str, Any]]:
    if not days:
        return entries
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return [entry for entry in entries if entry.get("created_at") and entry["created_at"] >= cutoff]


def fetch_entries_by_who(who_value: str, days: Optional[int] = None) -> list[dict[str, Any]]:
    sql = """
        SELECT id, created_at, type, who, title, content, status
        FROM entries
        WHERE who = :who
        ORDER BY created_at ASC, id ASC
    """
    with engine.begin() as conn:
        rows = conn.execute(text(sql), {"who": who_value}).mappings().all()
    entries = [dict(r) for r in rows]
    return filter_entries_by_days(entries, days)


def build_search_results_message(results: list[dict[str, Any]], query: str) -> str:
    if not results:
        return f"No matching entries found for: {query}"

    lines = [f"🔎 Matching entries ({len(results)}):", ""]
    for idx, row in enumerate(results, start=1):
        title = (row.get("title") or row.get("content") or "").strip()
        if len(title) > 70:
            title = title[:67] + "..."
        date_text = row["created_at"].astimezone(timezone.utc).strftime("%Y-%m-%d") if row.get("created_at") else "unknown date"
        lines.append(f"{idx}. {title} — {date_text} (#{row['id']})")
    return "\n".join(lines)


def looks_like_reminder_request(user_text: str) -> bool:
    normalized = normalize_whitespace(user_text).lower()
    if not normalized:
        return False
    if not any(normalized.startswith(prefix) for prefix in REMINDER_INTENT_PREFIXES):
        return False
    return any(keyword in normalized for keyword in REMINDER_TIME_KEYWORDS)


def classify_message_intent(user_text: str) -> str:
    normalized = normalize_whitespace(user_text).lower()
    if not normalized:
        return "note"

    if "?" in normalized or "¿" in normalized:
        return "question"

    if normalized.startswith(QUESTION_PREFIXES):
        return "question"

    return "note"


def build_saved_exchange_content(question: str, answer: str, provider_name: str, model_name: str) -> str:
    return (
        f"Question:\n{question}\n\n"
        f"Answer [{provider_name}:{model_name}]:\n{answer}"
    )


def ensure_entries_table() -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE EXTENSION IF NOT EXISTS vector;

                CREATE TABLE IF NOT EXISTS entries (
                    id BIGSERIAL PRIMARY KEY,
                    content TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'telegram',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    embedding vector(1536),
                    tags TEXT[] NOT NULL DEFAULT ARRAY['General'::text],
                    who TEXT,
                    title TEXT,
                    language TEXT,
                    type TEXT,
                    topic TEXT,
                    status TEXT,
                    parent_entry_id BIGINT,
                    due_date DATE,
                    reminded_at TIMESTAMPTZ,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );

                ALTER TABLE entries ADD COLUMN IF NOT EXISTS who TEXT;
                ALTER TABLE entries ADD COLUMN IF NOT EXISTS title TEXT;
                ALTER TABLE entries ADD COLUMN IF NOT EXISTS language TEXT;
                ALTER TABLE entries ADD COLUMN IF NOT EXISTS type TEXT;
                ALTER TABLE entries ADD COLUMN IF NOT EXISTS topic TEXT;
                ALTER TABLE entries ADD COLUMN IF NOT EXISTS status TEXT;
                ALTER TABLE entries ADD COLUMN IF NOT EXISTS parent_entry_id BIGINT;
                ALTER TABLE entries ADD COLUMN IF NOT EXISTS due_date DATE;
                ALTER TABLE entries ADD COLUMN IF NOT EXISTS reminded_at TIMESTAMPTZ;
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


def embedding_to_vector_literal(embedding: list[float]) -> str:
    return "[" + ",".join(f"{x:.8f}" for x in embedding) + "]"


def get_openai_embedding(input_text: str) -> list[float]:
    if not openai_client:
        raise RuntimeError("OpenAI embedding client is not configured")
    response = openai_client.embeddings.create(
        model=OPENAI_EMBEDDING_MODEL,
        input=input_text,
    )
    return response.data[0].embedding


def infer_type_from_text(entry_text: str) -> str:
    normalized = normalize_whitespace(entry_text).lower()
    if any(keyword in normalized for keyword in ["interesting idea", "what if", "идея", "idea", "concept", "portfolio", "портфолио"]):
        return "idea"
    if any(keyword in normalized for keyword in ["i think i should", "думаю, что нужно", "думаю надо", "myślę, że powinienem"]):
        return "highlight"
    if any(keyword in normalized for keyword in [
        "task:", "need to ", "have to ", "must ", "plan to ", "want to do ",
        "надо", "нужно", "должен", "планирую", "хочу сделать", "не забыть", "записаться",
        "muszę", "trzeba", "powinienem", "planuję", "chcę zrobić", "nie zapomnieć",
        "muss ", "sollte ", "plane ", "nicht vergessen",
        "call ", "check ", "prepare ", "buy ", "research "
    ]):
        return "task"
    if any(keyword in normalized for keyword in ["книг", "book", "read", "reading", "author", "chapter", "слушать книгу", "читать"]):
        return "book"
    if any(keyword in normalized for keyword in ["познаком", "met ", "person", "сосед", "новый человек"]):
        return "person"
    return "highlight"


def infer_who_from_text(entry_text: str) -> Optional[str]:
    normalized = normalize_whitespace(entry_text).lower()
    who_patterns = [
        ("Юля", ["юля", "юле", "юли", "юлей", "julia"]),
        ("Даша", ["даша", "dasha"]),
        ("Кирилл", ["кирил", "kirill"]),
        ("Варя", ["варя", "varia", "varya"]),
        ("Брат", ["брат", "brother"]),
        ("Дети", ["дети", "kids", "children"]),
        ("Друзья", ["друз", "friends"]),
        ("Мы", ["мы ", "we "] ),
        ("Работа", ["leverx", "meeting", "client", "office", "работ", "praca", "arbeit"]),
        ("Книга", ["книг", "book", "read", "reading", "lesen", "czyta"]),
        ("Природа", ["лес", "forest", "nature", "природ", "walk", "spacer", "wald"]),
        ("Здоровье", ["здоров", "doctor", "therapy", "health", "боле", "krank"]),
    ]
    for who_name, keywords in who_patterns:
        if any(keyword in normalized for keyword in keywords):
            return who_name
    return None


def extract_entry_metadata(
    entry_text: str,
    forced_type: Optional[str] = None,
    detected_language_override: Optional[str] = None,
    who_override: Optional[str] = None,
) -> dict[str, Optional[str]]:
    detected_language = detected_language_override or detect_language_bucket(entry_text)
    inferred_who = who_override or infer_who_from_text(entry_text)
    inferred_type = forced_type or infer_type_from_text(entry_text)
    fallback_who = inferred_who or "Я"
    if not openai_client:
        return {"who": fallback_who, "title": None, "language": detected_language, "type": inferred_type, "topic": None}

    try:
        response = openai_client.responses.create(
            model=TAGGING_MODEL,
            input=[
                {"role": "system", "content": TAGGING_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"Detected language: {detected_language}\nEntry:\n{entry_text}",
                },
            ],
            max_output_tokens=140,
        )
        payload = parse_json_object_from_text(response.output_text or "{}")
        who = normalize_whitespace(str(payload.get("who") or "")) or None
        title = normalize_whitespace(str(payload.get("title") or ""))
        language = normalize_whitespace(str(payload.get("language") or detected_language)).lower()
        entry_type = normalize_whitespace(str(payload.get("type") or inferred_type)).lower()
        topic = normalize_whitespace(str(payload.get("topic") or "")).lower() or None

        if who_override:
            who = who_override
        elif who not in WHO_CHOICES:
            who = fallback_who
        elif inferred_who and who == "Я":
            who = inferred_who

        if title:
            title = " ".join(title.split()[:4])
        else:
            title = None

        if language not in {"ru", "en", "pl", "de"}:
            language = detected_language
        else:
            language = detected_language

        if forced_type in TYPE_CHOICES:
            entry_type = forced_type
        elif entry_type not in TYPE_CHOICES:
            entry_type = inferred_type

        if topic not in TOPIC_CHOICES:
            topic = None

        return {"who": who or fallback_who, "title": title, "language": language, "type": entry_type, "topic": topic}
    except Exception as exc:
        logger.warning("Metadata tagging failed; saving without metadata: %s", exc)
        return {"who": fallback_who, "title": None, "language": detected_language, "type": inferred_type, "topic": None}


def normalize_due_date(raw_value: Any) -> Optional[date]:
    raw = normalize_whitespace(str(raw_value or ""))
    if not raw or raw.lower() == "null":
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def format_readable_date(target_date: date) -> str:
    return f"{target_date.day} {target_date.strftime('%B %Y')}"


def extract_reminder_signal(
    entry_text: str,
    reference_now: Optional[datetime] = None,
) -> dict[str, Any]:
    current = (reference_now or datetime.now(tz=APP_TZ)).astimezone(APP_TZ).date()
    if not openai_client:
        return {"has_date": False, "due_date": None, "clean_content": entry_text}

    try:
        system_prompt = DATE_EXTRACTION_SYSTEM_PROMPT_TEMPLATE.format(today=current.isoformat())
        response = openai_client.responses.create(
            model=TAGGING_MODEL,
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": entry_text},
            ],
            max_output_tokens=120,
        )
        payload = parse_json_object_from_text(response.output_text or "{}")
        has_date = bool(payload.get("has_date"))
        due_date = normalize_due_date(payload.get("due_date"))
        clean_content = normalize_whitespace(str(payload.get("clean_content") or "")) or entry_text
        if not has_date or due_date is None or due_date < current:
            return {"has_date": False, "due_date": None, "clean_content": entry_text}
        return {"has_date": True, "due_date": due_date, "clean_content": clean_content}
    except Exception as exc:
        logger.warning("Reminder date extraction failed; continuing without reminder: %s", exc)
        return {"has_date": False, "due_date": None, "clean_content": entry_text}


def extract_task_signal(
    entry_text: str,
    metadata_type: Optional[str] = None,
    forced_type: Optional[str] = None,
    detected_language_override: Optional[str] = None,
    reference_now: Optional[datetime] = None,
) -> dict[str, Any]:
    default_decision = "explicit_task" if forced_type == "task" or metadata_type == "task" else "none"
    if not openai_client:
        return {"task_decision": default_decision, "task_text": None, "due_date": None, "reason": None}

    current = (reference_now or datetime.now(tz=APP_TZ)).astimezone(APP_TZ)
    detected_language = detected_language_override or detect_language_bucket(entry_text)
    try:
        response = openai_client.responses.create(
            model=TAGGING_MODEL,
            input=[
                {"role": "system", "content": TASK_EXTRACTION_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"Reference date: {current.strftime('%Y-%m-%d')}\n"
                        f"Detected language: {detected_language}\n"
                        f"First-pass type: {metadata_type or 'unknown'}\n"
                        f"Entry:\n{entry_text}"
                    ),
                },
            ],
            max_output_tokens=180,
        )
        payload = parse_json_object_from_text(response.output_text or "{}")
        decision = normalize_whitespace(str(payload.get("task_decision") or default_decision)).lower()
        if decision not in {"none", "explicit_task", "embedded_task", "embedded_idea"}:
            decision = default_decision
        task_text = normalize_whitespace(str(payload.get("task_text") or "")) or None
        due_date = normalize_due_date(payload.get("due_date"))
        reason = normalize_whitespace(str(payload.get("reason") or "")) or None
        if forced_type == "task" and decision == "none":
            decision = "explicit_task"
        if decision == "none":
            task_text = None
            due_date = None
        if decision in {"embedded_task", "embedded_idea"} and not task_text:
            decision = "none"
            due_date = None
        if decision == "embedded_idea":
            due_date = None
        return {"task_decision": decision, "task_text": task_text, "due_date": due_date, "reason": reason}
    except Exception as exc:
        logger.warning("Task extraction check failed; skipping task upgrade: %s", exc)
        return {"task_decision": default_decision, "task_text": None, "due_date": None, "reason": None}


def prepare_entry_metadata(
    entry_text: str,
    forced_type: Optional[str] = None,
    detected_language_override: Optional[str] = None,
    allow_embedded_task: bool = True,
    who_override: Optional[str] = None,
) -> tuple[dict[str, Any], Optional[dict[str, Any]]]:
    metadata = extract_entry_metadata(entry_text, forced_type, detected_language_override, who_override)
    metadata = dict(metadata)
    metadata["due_date"] = None
    extra_item = None

    should_check_tasks = forced_type == "task" or metadata.get("type") != "idea"
    if not should_check_tasks:
        return metadata, None

    task_signal = extract_task_signal(
        entry_text,
        metadata_type=metadata.get("type"),
        forced_type=forced_type,
        detected_language_override=detected_language_override or metadata.get("language"),
    )
    due_date = task_signal.get("due_date")
    extracted_text = task_signal.get("task_text")
    extracted_language = metadata.get("language") or detected_language_override

    if forced_type == "task":
        metadata["type"] = "task"
        metadata["due_date"] = due_date
        return metadata, None

    if metadata.get("type") == "task":
        metadata["due_date"] = due_date
        return metadata, None

    if task_signal.get("task_decision") == "explicit_task":
        metadata["type"] = "task"
        metadata["due_date"] = due_date
    elif allow_embedded_task and task_signal.get("task_decision") == "embedded_task" and extracted_text:
        metadata["type"] = "highlight"
        extra_item = {
            "kind": "task",
            "content": extracted_text,
            "due_date": due_date,
            "language": extracted_language,
            "who": who_override,
        }
    elif allow_embedded_task and task_signal.get("task_decision") == "embedded_idea" and extracted_text:
        metadata["type"] = metadata.get("type") or "highlight"
        extra_item = {
            "kind": "idea",
            "content": extracted_text,
            "due_date": None,
            "language": extracted_language,
            "who": who_override,
        }
    return metadata, extra_item


def create_extracted_task_entry(
    task_text: str,
    due_date: Optional[date] = None,
    detected_language_override: Optional[str] = None,
    source: str = "task_extraction",
    who_override: Optional[str] = None,
    parent_entry_id: Optional[int] = None,
    topic_override: Optional[str] = None,
) -> dict[str, Any]:
    metadata, _ = prepare_entry_metadata(
        task_text,
        forced_type="task",
        detected_language_override=detected_language_override,
        allow_embedded_task=False,
        who_override=who_override,
    )
    if due_date is not None:
        metadata["due_date"] = due_date
    if topic_override in TOPIC_CHOICES:
        metadata["topic"] = topic_override

    embedding: Optional[list[float]] = None
    if openai_client:
        try:
            embedding = get_openai_embedding(task_text)
        except Exception as exc:
            logger.warning("Embedding failed during extracted task save; saving without embedding: %s", exc)

    tags = classify_tags(task_text)
    entry_id = save_entry(
        task_text,
        embedding,
        tags,
        metadata.get("who"),
        metadata.get("title"),
        metadata.get("language"),
        "task",
        "open",
        source,
        metadata.get("due_date"),
        parent_entry_id,
        metadata.get("topic"),
    )
    metadata["id"] = entry_id
    metadata["parent_entry_id"] = parent_entry_id
    return metadata


def create_extracted_idea_entry(
    idea_text: str,
    detected_language_override: Optional[str] = None,
    source: str = "idea_extraction",
    who_override: Optional[str] = None,
    parent_entry_id: Optional[int] = None,
    topic_override: Optional[str] = None,
) -> dict[str, Any]:
    metadata, _ = prepare_entry_metadata(
        idea_text,
        forced_type="idea",
        detected_language_override=detected_language_override,
        allow_embedded_task=False,
        who_override=who_override,
    )
    if topic_override in TOPIC_CHOICES:
        metadata["topic"] = topic_override

    embedding: Optional[list[float]] = None
    if openai_client:
        try:
            embedding = get_openai_embedding(idea_text)
        except Exception as exc:
            logger.warning("Embedding failed during extracted idea save; saving without embedding: %s", exc)

    tags = classify_tags(idea_text)
    entry_id = save_entry(
        idea_text,
        embedding,
        tags,
        metadata.get("who"),
        metadata.get("title"),
        metadata.get("language"),
        "idea",
        None,
        source,
        None,
        parent_entry_id,
        metadata.get("topic"),
    )
    metadata["id"] = entry_id
    metadata["parent_entry_id"] = parent_entry_id
    return metadata


def classify_tags(note_text: str) -> list[str]:
    normalized = normalize_whitespace(note_text).lower()
    found: list[str] = []
    keyword_groups = [
        ("Health", ["health", "doctor", "therapy", "ill", "sick", "здоров", "врач", "боле", "chor", "zdrow", "arzt", "krank"]),
        ("Work", ["work", "meeting", "client", "office", "leverx", "job", "работ", "встреч", "praca", "firma", "arbeit", "kunde"]),
        ("Learning", ["book", "read", "reading", "study", "learn", "книга", "читать", "лем", "ksi", "czyta", "buch", "lesen", "lernen"]),
        ("Travel", ["travel", "trip", "forest", "walk", "nature", "лес", "природ", "гуля", "прогул", "podro", "spacer", "wald", "natur"]),
        ("Family", ["юля", "даша", "кирил", "варя", "брат", "дети", "друз", "family", "kids", "children", "rodzin", "freund"]),
        ("Projects", ["project", "build", "launch", "bot", "product", "startup", "проект", "бот", "projekt"]),
        ("Ideas", ["idea", "thought", "think", "hypothesis", "идея", "мысл", "pomys", "gedank"]),
    ]

    for tag, keywords in keyword_groups:
        if any(keyword in normalized for keyword in keywords) and tag not in found:
            found.append(tag)

    if not found:
        return ["General"]
    return found[:3]


def save_entry(
    content: str,
    embedding: Optional[list[float]],
    tags: list[str],
    who: Optional[str] = None,
    title: Optional[str] = None,
    language: Optional[str] = None,
    entry_type: Optional[str] = None,
    status: Optional[str] = None,
    source: str = "telegram",
    due_date: Optional[date] = None,
    parent_entry_id: Optional[int] = None,
    topic: Optional[str] = None,
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
        "status": status,
        "source": source,
        "due_date": due_date,
        "parent_entry_id": parent_entry_id,
        "topic": topic if topic in TOPIC_CHOICES else None,
    }
    with engine.begin() as conn:
        if vector_literal:
            row = conn.execute(
                text(
                    """
                    INSERT INTO entries (content, embedding, tags, who, title, language, type, topic, status, source, due_date, parent_entry_id)
                    VALUES (:content, CAST(:embedding AS vector), :tags, :who, :title, :language, :entry_type, :topic, :status, :source, :due_date, :parent_entry_id)
                    RETURNING id
                    """
                ),
                params,
            ).scalar_one()
        else:
            row = conn.execute(
                text(
                    """
                    INSERT INTO entries (content, tags, who, title, language, type, topic, status, source, due_date, parent_entry_id)
                    VALUES (:content, :tags, :who, :title, :language, :entry_type, :topic, :status, :source, :due_date, :parent_entry_id)
                    RETURNING id
                    """
                ),
                params,
            ).scalar_one()
    entry_id = int(row)
    if params["topic"]:
        logger.info("Entry #%s topic assigned: %s", entry_id, params["topic"])
    else:
        logger.info("Entry #%s topic invalid, set NULL", entry_id)
    return entry_id


def update_entry(
    entry_id: int,
    content: str,
    embedding: Optional[list[float]],
    tags: list[str],
    who: Optional[str] = None,
    title: Optional[str] = None,
    language: Optional[str] = None,
    entry_type: Optional[str] = None,
    status: Optional[str] = None,
    due_date: Optional[date] = None,
    topic: Optional[str] = None,
) -> bool:
    vector_literal = embedding_to_vector_literal(embedding) if embedding else None
    params = {
        "entry_id": entry_id,
        "content": content,
        "embedding": vector_literal,
        "tags": tags,
        "who": who,
        "title": title,
        "language": language,
        "entry_type": entry_type,
        "status": status,
        "due_date": due_date,
        "topic": topic if topic in TOPIC_CHOICES else None,
    }
    with engine.begin() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM entries WHERE id = :entry_id"),
            {"entry_id": entry_id},
        ).scalar()
        if not exists:
            return False
        if vector_literal:
            conn.execute(
                text(
                    """
                    UPDATE entries
                    SET content = :content,
                        embedding = CAST(:embedding AS vector),
                        tags = :tags,
                        who = :who,
                        title = :title,
                        language = :language,
                        type = :entry_type,
                        topic = :topic,
                        status = :status,
                        due_date = :due_date
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
                        embedding = NULL,
                        tags = :tags,
                        who = :who,
                        title = :title,
                        language = :language,
                        type = :entry_type,
                        topic = :topic,
                        status = :status,
                        due_date = :due_date
                    WHERE id = :entry_id
                    """
                ),
                params,
            )
    if params["topic"]:
        logger.info("Entry #%s topic assigned: %s", entry_id, params["topic"])
    else:
        logger.info("Entry #%s topic invalid, set NULL", entry_id)
    return True


def search_entries(query_embedding: list[float], top_k: int, tag: Optional[str] = None) -> list[dict[str, Any]]:
    vector_literal = embedding_to_vector_literal(query_embedding)
    sql = """
        SELECT
            id,
            content,
            tags,
            created_at,
            1 - (embedding <=> CAST(:embedding AS vector)) AS relevance
        FROM entries
        WHERE embedding IS NOT NULL
    """
    params: dict[str, Any] = {"embedding": vector_literal, "top_k": top_k}

    if tag:
        sql += " AND :tag = ANY(tags)"
        params["tag"] = tag

    sql += """
        ORDER BY embedding <=> CAST(:embedding AS vector)
        LIMIT :top_k
    """

    with engine.begin() as conn:
        rows = conn.execute(text(sql), params).mappings().all()
        return [dict(r) for r in rows]


def fetch_entries_by_type(entry_type: str, status: Optional[str] = None) -> list[dict[str, Any]]:
    sql = """
        SELECT id, created_at, type, who, title, content, status
        FROM entries
        WHERE type = :entry_type
    """
    params: dict[str, Any] = {"entry_type": entry_type}
    if status is not None:
        sql += " AND status = :status"
        params["status"] = status
    sql += " ORDER BY created_at ASC, id ASC"

    with engine.begin() as conn:
        rows = conn.execute(text(sql), params).mappings().all()
        return [dict(r) for r in rows]


def fetch_entry_by_id(entry_id: int) -> Optional[dict[str, Any]]:
    sql = """
        SELECT id, created_at, updated_at, type, who, title, content, language, status, source, due_date, parent_entry_id
        FROM entries
        WHERE id = :entry_id
    """
    with engine.begin() as conn:
        row = conn.execute(text(sql), {"entry_id": entry_id}).mappings().first()
    return dict(row) if row else None


def parse_entry_id_list(text_value: str, limit: int = 10) -> list[int]:
    ids: list[int] = []
    for raw_id in re.findall(r"#?(\d+)", text_value or ""):
        entry_id = int(raw_id)
        if entry_id not in ids:
            ids.append(entry_id)
        if len(ids) >= limit:
            break
    return ids


def build_entry_detail_message(entry: dict[str, Any]) -> str:
    created_at = entry.get("created_at")
    date_text = created_at.astimezone(timezone.utc).strftime("%Y-%m-%d") if created_at else "unknown"
    status_text = entry.get("status") or "-"
    title_text = entry.get("title") or "(no title)"
    parent_text = f"\nContext: entry #{entry['parent_entry_id']}" if entry.get("parent_entry_id") else ""
    return (
        f"📄 Entry #{entry['id']}\n"
        f"Date: {date_text}\n"
        f"Type: {entry.get('type') or '-'} | Status: {status_text}\n"
        f"Who: {entry.get('who') or '-'} | Language: {entry.get('language') or '-'}\n"
        f"Title: {title_text}{parent_text}\n\n"
        f"{entry.get('content') or ''}"
    )


def update_entry_status(entry_id: int, status: str) -> bool:
    if status not in {"open", "done"}:
        return False
    with engine.begin() as conn:
        row = conn.execute(
            text(
                """
                UPDATE entries
                SET status = :status
                WHERE id = :entry_id
                  AND COALESCE(type, '') IN ('task', 'idea')
                RETURNING id
                """
            ),
            {"entry_id": entry_id, "status": status},
        ).mappings().first()
    return bool(row)


def infer_status_from_action_intent(intent_payload: dict[str, Any], message_text: str) -> str:
    parameters = intent_payload.get("parameters") if isinstance(intent_payload.get("parameters"), dict) else {}
    raw_status = normalize_whitespace(str(parameters.get("status") or "")).lower()
    normalized = normalize_whitespace(message_text).lower()
    if raw_status in {"open", "reopen", "active"}:
        return "open"
    if raw_status in {"done", "closed", "complete", "completed"}:
        return "done"
    if any(token in normalized for token in ["open", "reopen", "верни", "открой", "снова", "актив"]):
        return "open"
    return "done"


def find_action_target_entry(target: str, message_text: str) -> Optional[dict[str, Any]]:
    entry_ids = parse_entry_id_list(target) or parse_entry_id_list(message_text)
    for entry_id in entry_ids:
        entry = fetch_entry_by_id(entry_id)
        if entry:
            return entry

    query = normalize_whitespace(target)
    if not query:
        query = normalize_whitespace(message_text)
    if not query:
        return None

    rows = text_search_entries(query, 8, None)
    if not rows:
        rows = hybrid_search_entries(query, 8, None)
    if not rows:
        rows = fetch_recent_entries(5)
    return rows[0] if rows else None


def localized_no_query_results(language: str) -> str:
    if language == "ru":
        return "Ничего не нашёл по этому запросу."
    if language == "pl":
        return "Nic nie znalazłem dla tego zapytania."
    if language == "de":
        return "Ich habe dazu nichts gefunden."
    return "I couldn't find anything for that query."


def localized_save_context_prompt(language: str) -> str:
    if language == "ru":
        return "Сохранить этот контекст как заметку?"
    if language == "pl":
        return "Zapisać ten kontekst jako notatkę?"
    if language == "de":
        return "Diesen Kontext als Notiz speichern?"
    return "Save this context as a note?"


def is_affirmative_reply(text_value: str) -> bool:
    normalized = normalize_whitespace(text_value).lower()
    return normalized in {"да", "yes", "ja", "y", "ага", "ок", "ok"}


def extract_standalone_entry_id(query: str) -> Optional[int]:
    for match in re.finditer(r"\b\d+\b", query):
        before = query[: match.start()].rstrip()
        after = query[match.end() :].lstrip()
        previous_word_match = re.search(r"([A-Za-zА-Яа-яЁё]+)$", before)
        next_word_match = re.match(r"([A-Za-zА-Яа-яЁё]+)", after)
        previous_word = previous_word_match.group(1).lower() if previous_word_match else ""
        next_word = next_word_match.group(1).lower() if next_word_match else ""
        count_words = {
            "top",
            "топ",
            "первые",
            "первых",
            "последние",
            "последних",
            "last",
            "latest",
            "recent",
        }
        counted_things = {
            "idea",
            "ideas",
            "идей",
            "идеи",
            "задач",
            "задачи",
            "tasks",
            "task",
            "entries",
            "entry",
            "записей",
            "записи",
        }
        if previous_word in count_words or next_word in counted_things:
            continue
        return int(match.group(0))
    return None


def fetch_recent_entries(limit: int = 5) -> list[dict[str, Any]]:
    sql = """
        SELECT id, created_at, type, who, title, content, status
        FROM entries
        ORDER BY created_at DESC, id DESC
        LIMIT :limit
    """
    with engine.begin() as conn:
        rows = conn.execute(text(sql), {"limit": limit}).mappings().all()
    return [dict(r) for r in rows]


def extract_entry_id_from_text(text_value: str) -> Optional[int]:
    match = re.search(r"#(\d+)", text_value or "")
    return int(match.group(1)) if match else None


def build_last_entries_message(entries: list[dict[str, Any]], limit: int) -> str:
    if not entries:
        return "📋 Last entries: none yet."
    lines = [f"📋 Last {limit} entries:", ""]
    for idx, entry in enumerate(entries, start=1):
        preview = normalize_whitespace(entry.get("content") or "")
        if len(preview) > 80:
            preview = preview[:77] + "..."
        lines.append(f"{idx}. (#{entry['id']}) {entry.get('type') or '-'} — {preview}")
    return "\n".join(lines)


def apply_correction_instruction(original_text: str, correction_text: str) -> str:
    if not openai_client:
        raise RuntimeError("OpenAI client not configured")
    response = openai_client.responses.create(
        model=TAGGING_MODEL,
        input=[
            {
                "role": "system",
                "content": (
                    "You correct a journal entry using a short correction note. "
                    "Return only the corrected full entry text in the same language as the original. "
                    "Keep the original meaning, apply the correction, and do not add commentary."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Original entry:\n{original_text}\n\n"
                    f"Correction note:\n{correction_text}"
                ),
            },
        ],
        max_output_tokens=220,
    )
    corrected = normalize_whitespace(response.output_text or "")
    if not corrected:
        raise RuntimeError("Empty correction result")
    return corrected


def extract_search_terms(query: str) -> list[str]:
    terms = re.findall(r"[\wА-Яа-яЁёĄąĆćĘęŁłŃńÓóŚśŹźŻżÄäÖöÜüß+-]+", normalize_whitespace(query).lower())
    cleaned: list[str] = []
    for term in terms:
        if term in SEARCH_STOPWORDS:
            continue
        if len(term) < 3 and term.lower() not in {"ai"}:
            continue
        if term not in cleaned:
            cleaned.append(term)
    return cleaned[:6]


def text_search_entries(query: str, limit: int = 10, tag: Optional[str] = None) -> list[dict[str, Any]]:
    terms = extract_search_terms(query)
    if not terms:
        normalized = normalize_whitespace(query)
        terms = [normalized] if normalized else []
    if not terms:
        return []

    clauses = []
    params: dict[str, Any] = {"limit": limit}
    for idx, term in enumerate(terms):
        key = f"term_{idx}"
        params[key] = f"%{term}%"
        clauses.append(f"(content ILIKE :{key} OR COALESCE(title, '') ILIKE :{key} OR COALESCE(who, '') ILIKE :{key})")

    sql = """
        SELECT id, created_at, type, who, title, content, status
        FROM entries
        WHERE (
    """ + " OR ".join(clauses) + "\n        )"
    if tag:
        sql += " AND :tag = ANY(tags)"
        params["tag"] = tag
    sql += " ORDER BY created_at DESC, id DESC LIMIT :limit"

    with engine.begin() as conn:
        rows = conn.execute(text(sql), params).mappings().all()
    return [dict(r) for r in rows]


def hybrid_search_entries(query: str, limit: int = 10, tag: Optional[str] = None) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    seen: set[int] = set()

    if openai_client:
        try:
            query_embedding = get_openai_embedding(query)
            for row in search_entries(query_embedding, limit, tag):
                entry_id = int(row["id"])
                if entry_id not in seen:
                    seen.add(entry_id)
                    results.append(dict(row))
        except Exception as exc:
            logger.warning("Vector search failed during hybrid search: %s", exc)

    for row in text_search_entries(query, limit, tag):
        entry_id = int(row["id"])
        if entry_id not in seen:
            seen.add(entry_id)
            results.append(dict(row))

    return results[:limit]


def prioritize_entries_by_type(entries: list[dict[str, Any]], preferred_type: Optional[str]) -> list[dict[str, Any]]:
    if not preferred_type:
        return entries
    return sorted(entries, key=lambda entry: 0 if (entry.get("type") or "") == preferred_type else 1)


def humanize_days_ago(created_at: datetime) -> str:
    now_utc = datetime.now(timezone.utc)
    age_days = max(0, (now_utc.date() - created_at.astimezone(timezone.utc).date()).days)
    if age_days == 0:
        return "today"
    if age_days == 1:
        return "1 day ago"
    return f"{age_days} days ago"


def build_entry_list_message(entries: list[dict[str, Any]], header: str, show_age: bool) -> str:
    if not entries:
        return ""

    lines = [f"{header} ({len(entries)}):", ""]
    for idx, entry in enumerate(entries, start=1):
        label = (entry.get("title") or entry.get("content") or "").strip()
        if len(label) > 90:
            label = label[:87] + "..."
        if show_age:
            lines.append(f"{idx}. {label} — {humanize_days_ago(entry['created_at'])} (#{entry['id']})")
        else:
            lines.append(f"{idx}. {label} (#{entry['id']})")
    return "\n".join(lines)


def get_tag_counts() -> list[tuple[str, int]]:
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                """
                SELECT tag, COUNT(*) AS cnt
                FROM entries, UNNEST(tags) AS tag
                GROUP BY tag
                ORDER BY cnt DESC, tag ASC
                """
            )
        ).all()
        return [(row[0], row[1]) for row in rows]


def format_search_results(results: list[dict[str, Any]]) -> str:
    if not results:
        return "No matches found."

    lines = []
    for i, row in enumerate(results, start=1):
        preview = row["content"].strip()
        if len(preview) > 220:
            preview = preview[:217] + "..."
        tags_text = ", ".join(row.get("tags") or [])
        relevance = float(row.get("relevance") or 0.0)
        lines.append(f"{i}. [{relevance:.3f}] {preview}\n   tags: {tags_text}")
    return "\n\n".join(lines)


def build_memory_prompt(question: str, memories: list[dict[str, Any]]) -> list[dict[str, str]]:
    memory_blocks = []
    for i, row in enumerate(memories, start=1):
        memory_blocks.append(
            f"Memory {i} (relevance {float(row['relevance']):.3f}, tags: {', '.join(row.get('tags') or [])}):\n"
            f"{row['content']}"
        )

    system = (
        "Answer the user's question using only the stored memories provided. "
        "If the memories are insufficient, say so clearly and do not guess. "
        "Always reply in the same language the user wrote their message in. Never switch to a different language."
    )
    user = "Stored memories:\n\n" + "\n\n".join(memory_blocks) + f"\n\nQuestion:\n{question}"

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def extract_tag_filter(command_text: str) -> tuple[str, Optional[str]]:
    match = re.search(r"tag:([A-Za-z]+)", command_text)
    if not match:
        return command_text.strip(), None
    tag = match.group(1).strip()
    cleaned = re.sub(r"tag:[A-Za-z]+", "", command_text).strip()
    return cleaned, tag


def extract_forwarded_sender_name(message: Any) -> Optional[str]:
    if not message:
        return None
    sender_name = getattr(message, "forward_sender_name", None)
    if sender_name:
        return normalize_whitespace(str(sender_name)) or None
    forward_from = getattr(message, "forward_from", None)
    if forward_from:
        full_name = " ".join(part for part in [getattr(forward_from, "first_name", ""), getattr(forward_from, "last_name", "")] if part).strip()
        fallback = full_name or getattr(forward_from, "username", "")
        return normalize_whitespace(fallback) or None
    forward_origin = getattr(message, "forward_origin", None)
    if forward_origin:
        sender_user = getattr(forward_origin, "sender_user", None)
        if sender_user:
            full_name = " ".join(part for part in [getattr(sender_user, "first_name", ""), getattr(sender_user, "last_name", "")] if part).strip()
            fallback = full_name or getattr(sender_user, "username", "")
            return normalize_whitespace(fallback) or None
        origin_name = getattr(forward_origin, "sender_name", None)
        if origin_name:
            return normalize_whitespace(str(origin_name)) or None
    return None


# =========================
# Provider Calls
# =========================

def openai_generate(model: str, prompt: str, system_prompt: Optional[str] = None) -> str:
    if not openai_client:
        raise RuntimeError("OpenAI client not configured")

    input_payload: list[dict[str, str]] = []
    if system_prompt:
        input_payload.append({"role": "system", "content": system_prompt})
    input_payload.append({"role": "user", "content": prompt})

    response = openai_client.responses.create(
        model=model,
        input=input_payload,
    )
    return normalize_whitespace(response.output_text or "")


def anthropic_generate(model: str, prompt: str, system_prompt: Optional[str] = None) -> str:
    if not anthropic_client:
        raise RuntimeError("Anthropic client not configured")

    response = anthropic_client.messages.create(
        model=model,
        max_tokens=800,
        system=system_prompt or "",
        messages=[{"role": "user", "content": prompt}],
    )
    parts = []
    for block in response.content:
        if getattr(block, "type", "") == "text":
            parts.append(block.text)
    return normalize_whitespace(" ".join(parts))


def google_generate(model: str, prompt: str, system_prompt: Optional[str] = None) -> str:
    if not gemini_client:
        raise RuntimeError("Google client not configured")

    full_prompt = prompt if not system_prompt else f"{system_prompt}\n\n{prompt}"
    response = gemini_client.models.generate_content(
        model=model,
        contents=full_prompt,
    )
    return normalize_whitespace(response.text or "")


def generate_with_provider(provider_name: str, prompt: str, system_prompt: Optional[str] = None) -> str:
    state = PROVIDERS[provider_name]
    if not state.available or not state.active_model:
        raise RuntimeError(f"{provider_name} provider is not available")

    if provider_name == "openai":
        return openai_generate(state.active_model, prompt, system_prompt)
    if provider_name == "anthropic":
        return anthropic_generate(state.active_model, prompt, system_prompt)
    if provider_name == "google":
        return google_generate(state.active_model, prompt, system_prompt)

    raise RuntimeError(f"Unknown provider: {provider_name}")


def validate_openai_model(model: str) -> None:
    _ = openai_generate(model, "Reply with OK only.")


def validate_anthropic_model(model: str) -> None:
    _ = anthropic_generate(model, "Reply with OK only.")


def validate_google_model(model: str) -> None:
    _ = google_generate(model, "Reply with OK only.")


def _activate_model(
    state: ProviderState,
    validator: Callable[[str], None],
) -> None:
    state.available = False
    state.active_model = None
    state.error = None

    if not state.configured:
        state.error = "missing API key or provider disabled or model missing"
        return

    try:
        validator(state.model)
        state.available = True
        state.active_model = state.model
        state.error = None
        logger.info("Provider %s available with model %s", state.name, state.model)
    except Exception as exc:
        state.error = str(exc)
        logger.warning("Provider %s model %s failed validation: %s", state.name, state.model, exc)


def validate_all_providers() -> None:
    _activate_model(PROVIDERS["openai"], validate_openai_model)
    _activate_model(PROVIDERS["anthropic"], validate_anthropic_model)
    _activate_model(PROVIDERS["google"], validate_google_model)


# =========================
# Telegram Helpers
# =========================

async def send_typing(update: Update) -> None:
    if update.effective_chat:
        remember_chat_id(update.effective_chat.id)
        await update.effective_chat.send_action(action=ChatAction.TYPING)


async def reply_text(update: Update, text_value: str) -> None:
    if update.effective_chat:
        remember_chat_id(update.effective_chat.id)
    await update.message.reply_text(truncate(text_value, 3900))


def get_command_arg(update: Update) -> str:
    message_text = (update.message.text or "").strip()
    parts = message_text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


async def run_blocking(func: Callable, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)


def contains_cyrillic(text_value: str) -> bool:
    return bool(re.search(r"[а-яА-ЯёЁ]", text_value or ""))


def contains_latin(text_value: str) -> bool:
    return bool(re.search(r"[A-Za-ząćęłńóśźżĄĆĘŁŃÓŚŹŻäöüßÄÖÜ]", text_value or ""))


def transcribe_voice_file(audio_path: str, preferred_language: Optional[str] = None) -> tuple[str, Optional[str]]:
    if not openai_client:
        raise RuntimeError("OpenAI transcription client is not configured")

    transcription_prompt = (
        "Transcribe exactly in the original spoken language and original writing system. "
        "Use Latin script for Polish, English, and German. "
        "Use Cyrillic only for Russian. Do not transliterate Polish into Cyrillic."
    )

    def _run_transcription(language_hint: Optional[str] = None):
        with open(audio_path, "rb") as audio_file:
            kwargs = {
                "model": OPENAI_TRANSCRIPTION_MODEL,
                "file": audio_file,
                "response_format": "verbose_json",
                "prompt": transcription_prompt,
            }
            if language_hint:
                kwargs["language"] = language_hint
            return openai_client.audio.transcriptions.create(**kwargs)

    response = _run_transcription()
    transcript_text = normalize_whitespace(getattr(response, "text", "") or "")
    transcript_language = normalize_whitespace(getattr(response, "language", "") or "").lower() or None

    preferred = (preferred_language or "").lower()
    latin_languages = {"pl", "en", "de"}
    should_retry = False
    retry_language = None

    if transcript_text and contains_cyrillic(transcript_text):
        if transcript_language in latin_languages:
            should_retry = True
            retry_language = transcript_language
        elif preferred in latin_languages:
            should_retry = True
            retry_language = preferred

    if should_retry and retry_language:
        retry_response = _run_transcription(retry_language)
        retry_text = normalize_whitespace(getattr(retry_response, "text", "") or "")
        retry_language_detected = normalize_whitespace(getattr(retry_response, "language", "") or "").lower() or retry_language
        if retry_text:
            transcript_text = retry_text
            transcript_language = retry_language_detected

    return transcript_text, transcript_language


async def process_captured_content(
    update: Update,
    content: str,
    source: str = "telegram",
    transcript_text: Optional[str] = None,
    detected_language: Optional[str] = None,
    who_override: Optional[str] = None,
) -> None:
    original_content = content
    reminder_signal = await run_blocking(extract_reminder_signal, original_content)
    reminder_due_date = reminder_signal.get("due_date") if reminder_signal.get("has_date") else None
    if reminder_signal.get("has_date") and reminder_signal.get("clean_content"):
        content = reminder_signal["clean_content"]

    reminder_like = looks_like_reminder_request(original_content)
    intent = "note" if reminder_due_date or reminder_like else classify_message_intent(content)
    transcript_line = f'🎤 "{transcript_text or original_content}"' if transcript_text else None

    if intent == "question":
        database_query = classify_database_query(content)
        if database_query:
            try:
                if database_query["kind"] == "tasks":
                    entries = await run_blocking(fetch_entries_by_type, "task", database_query.get("status"))
                    entries = filter_entries_by_days(entries, database_query.get("days"))
                    if not entries:
                        await reply_text(update, "✅ No open tasks!")
                    else:
                        await reply_text(update, build_entry_list_message(entries, "📋 Open tasks", True))
                    return

                if database_query["kind"] == "ideas":
                    entries = await run_blocking(fetch_entries_by_type, "idea")
                    entries = [entry for entry in entries if entry.get("status") != "done"]
                    entries = filter_entries_by_days(entries, database_query.get("days"))
                    if not entries:
                        await reply_text(update, "💡 Ideas (0):")
                    else:
                        await reply_text(update, build_entry_list_message(entries, "💡 Ideas", False))
                    return

                if database_query["kind"] == "search":
                    if database_query.get("who"):
                        results = await run_blocking(fetch_entries_by_who, database_query["who"], database_query.get("days"))
                    else:
                        results = await run_blocking(hybrid_search_entries, database_query["query"], 10, None)
                        results = filter_entries_by_days(results, database_query.get("days"))
                    results = prioritize_entries_by_type(results, database_query.get("type_priority"))
                    await reply_text(update, build_search_results_message(results[:10], database_query["query"]))
                    return
            except Exception as exc:
                logger.exception("Database query handling failed")
                await reply_text(update, f"Database query failed: {exc}")
                return

        provider_name = choose_provider_for_general_query(content, detected_language)
        if provider_name == "none":
            await reply_text(update, "No LLM providers are currently available.")
            return

        system_prompt = (
            "You are Open Brain, a self-hosted second brain assistant. "
            "Always reply in the same language the user wrote their message in. Never switch to a different language."
        )

        try:
            metadata, _ = await run_blocking(prepare_entry_metadata, content, None, detected_language, False, who_override)
            answer = await run_blocking(generate_with_provider, provider_name, content, system_prompt)
            answer_to_send = answer or "(empty response)"

            embedding: Optional[list[float]] = None
            if openai_client:
                try:
                    embedding = await run_blocking(get_openai_embedding, content)
                except Exception as exc:
                    logger.warning("Embedding failed during question capture; saving without embedding: %s", exc)

            tags = await run_blocking(classify_tags, content)
            status = "open" if metadata.get("type") == "task" else None
            final_due_date = reminder_due_date or metadata.get("due_date")
            entry_id = await run_blocking(
                save_entry,
                content,
                embedding,
                tags,
                metadata.get("who"),
                metadata.get("title"),
                metadata.get("language"),
                metadata.get("type"),
                status,
                source,
                final_due_date,
            )

            suffix = f"✓ Saved (#{entry_id})"
            if final_due_date:
                suffix += f" | Reminder set for {format_readable_date(final_due_date)}."
            if transcript_line:
                await reply_text(update, transcript_line + "\n\n" + answer_to_send + "\n\n" + suffix)
            else:
                await reply_text(update, answer_to_send + "\n\n" + suffix)
            return
        except Exception as exc:
            logger.exception("Auto-routed question failed")
            await reply_text(update, f"Question routing failed: {exc}")
            return

    try:
        metadata, extra_item = await run_blocking(prepare_entry_metadata, content, None, detected_language, True, who_override)
        if extra_item and extra_item.get("kind") == "task" and reminder_due_date and not extra_item.get("due_date"):
            extra_item["due_date"] = reminder_due_date
        if reminder_due_date:
            metadata["due_date"] = reminder_due_date

        embedding: Optional[list[float]] = None

        if openai_client:
            try:
                embedding = await run_blocking(get_openai_embedding, content)
            except Exception as exc:
                logger.warning("Embedding failed during capture; saving without embedding: %s", exc)

        tags = await run_blocking(classify_tags, content)
        status = "open" if metadata.get("type") == "task" else None
        final_due_date = reminder_due_date or metadata.get("due_date")
        entry_id = await run_blocking(
            save_entry,
            content,
            embedding,
            tags,
            metadata.get("who"),
            metadata.get("title"),
            metadata.get("language"),
            metadata.get("type"),
            status,
            source,
            final_due_date,
            None,
            metadata.get("topic"),
        )

        extra_message = None
        if extra_item and extra_item.get("content"):
            if extra_item.get("kind") == "task":
                extracted = await run_blocking(
                    create_extracted_task_entry,
                    extra_item["content"],
                    extra_item.get("due_date"),
                    extra_item.get("language"),
                    f"{source}_task_extraction",
                    extra_item.get("who"),
                    entry_id,
                    metadata.get("topic"),
                )
                extra_message = f"Also detected a task: '{extra_item['content']}' (#{extracted['id']}, linked to #{entry_id})"
            elif extra_item.get("kind") == "idea":
                extracted = await run_blocking(
                    create_extracted_idea_entry,
                    extra_item["content"],
                    extra_item.get("language"),
                    f"{source}_idea_extraction",
                    extra_item.get("who"),
                    entry_id,
                    metadata.get("topic"),
                )
                extra_message = f"Also detected an idea: '{extra_item['content']}' (#{extracted['id']}, linked to #{entry_id})"

        reminder_message = f"Reminder set for {format_readable_date(final_due_date)}." if final_due_date else None
        if transcript_line:
            message = transcript_line + "\n" + f"{SAVE_CONFIRMATION_TEXT} (#{entry_id}) | Who: {metadata.get('who') or 'Я'} | Type: {metadata.get('type') or 'highlight'}"
            if extra_message:
                message += f" | {extra_message}"
            if reminder_message:
                message += f" | {reminder_message}"
            await reply_text(update, message)
        else:
            message = f"{SAVE_CONFIRMATION_TEXT} (#{entry_id})"
            if metadata.get("who") or metadata.get("type"):
                message += f" | Who: {metadata.get('who') or 'Я'} | Type: {metadata.get('type') or 'highlight'}"
            if extra_message:
                message += f" | {extra_message}"
            if reminder_message:
                message += f" | {reminder_message}"
            await reply_text(update, message)
    except Exception as exc:
        logger.exception("Silent capture failed")
        await reply_text(update, f"Save failed: {exc}")


async def voice_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.voice:
        return

    if not openai_client:
        await reply_text(update, "Voice transcription requires OpenAI to be configured.")
        return

    await send_typing(update)
    temp_path: Optional[str] = None

    try:
        telegram_file = await context.bot.get_file(update.message.voice.file_id)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as temp_file:
            temp_path = temp_file.name
        await telegram_file.download_to_drive(custom_path=temp_path)

        preferred_language = ((update.effective_user.language_code or "") if update.effective_user else "").split("-", 1)[0].lower() or None
        transcript_text, transcript_language = await run_blocking(transcribe_voice_file, temp_path, preferred_language)
        if not transcript_text:
            await reply_text(update, "I could not transcribe that voice note.")
            return

        if transcript_language and transcript_language not in {"ru", "en", "pl", "de"}:
            transcript_language = None

        await process_captured_content(
            update,
            transcript_text,
            source="voice",
            transcript_text=transcript_text,
            detected_language=transcript_language,
        )
    except Exception as exc:
        logger.exception("Voice capture failed")
        await reply_text(update, f"Voice processing failed: {exc}")
    finally:
        if temp_path and Path(temp_path).exists():
            try:
                Path(temp_path).unlink()
            except OSError as exc:
                logger.warning("Could not delete temporary voice file %s: %s", temp_path, exc)


# =========================
# Command Handlers
# =========================

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    commands_text = " ".join(cfg.telegram.allowed_manual_commands)
    await reply_text(
        update,
        "Open Brain is running.\n"
        f"Commands: /health {commands_text}\n"
        "Any normal text message is silently captured and saved.",
    )


async def health_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lines = ["Provider health:"]
    for provider_name in ["openai", "anthropic", "google"]:
        state = PROVIDERS[provider_name]
        status = "available" if state.available else "unavailable"
        active = state.active_model or "-"
        fallbacks = ", ".join(state.fallback_providers) if state.fallback_providers else "-"
        lines.append(
            f"- {provider_name}: {status}\n"
            f"  active_model: {active}\n"
            f"  configured_model: {state.model or '-'}\n"
            f"  fallback_providers: {fallbacks}\n"
            f"  error: {state.error or '-'}"
        )
    await reply_text(update, "\n".join(lines))


async def provider_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, provider_name: str, usage_name: str) -> None:
    prompt = get_command_arg(update)
    if not prompt:
        await reply_text(update, f"Usage: /{usage_name} <prompt>")
        return

    state = PROVIDERS[provider_name]
    if not state.available:
        await reply_text(update, f"{provider_name.capitalize()} provider is not configured or not available.\nReason: {state.error or 'unknown'}")
        return

    await send_typing(update)
    try:
        answer = await run_blocking(generate_with_provider, provider_name, prompt, None)
        await reply_text(update, answer or "(empty response)")
    except Exception as exc:
        logger.exception("%s command failed", provider_name)
        await reply_text(update, f"{provider_name.capitalize()} failed: {exc}")


async def gpt_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await provider_command_handler(update, context, "openai", "gpt")


async def claude_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await provider_command_handler(update, context, "anthropic", "claude")


async def gemini_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await provider_command_handler(update, context, "google", "gemini")


async def all_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    prompt = get_command_arg(update)
    if not prompt:
        await reply_text(update, "Usage: /all <prompt>")
        return

    await send_typing(update)
    blocks = []

    for provider_name, label in [
        ("openai", "GPT"),
        ("anthropic", "Claude"),
        ("google", "Gemini"),
    ]:
        state = PROVIDERS[provider_name]
        if not state.available:
            blocks.append(f"{label}: unavailable ({state.error or 'not configured'})")
            continue
        try:
            answer = await run_blocking(generate_with_provider, provider_name, prompt, None)
            blocks.append(f"{label} [{state.active_model}]:\n{answer or '(empty response)'}")
        except Exception as exc:
            logger.exception("/all failed for %s", provider_name)
            blocks.append(f"{label}: failed ({exc})")

    await reply_text(update, "\n\n".join(blocks))


async def askg_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    prompt = get_command_arg(update)
    if not prompt:
        await reply_text(update, "Usage: /askg <question>")
        return

    provider_name = choose_provider_for_general_query(prompt)
    if provider_name == "none":
        await reply_text(update, "No LLM providers are currently available.")
        return

    state = PROVIDERS[provider_name]
    system_prompt = (
        "You are Open Brain, a self-hosted second brain assistant. "
        "Always reply in the same language the user wrote their message in. Never switch to a different language."
    )

    await send_typing(update)
    try:
        answer = await run_blocking(generate_with_provider, provider_name, prompt, system_prompt)
        await reply_text(update, f"[{provider_name}:{state.active_model}]\n{answer}")
    except Exception as exc:
        logger.exception("/askg failed")
        await reply_text(update, f"/askg failed via {provider_name}: {exc}")


async def ask_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    question = get_command_arg(update)
    if not question:
        await reply_text(update, "Usage: /ask <question>")
        return

    if not PROVIDERS["openai"].available:
        await reply_text(update, "Memory-grounded /ask requires OpenAI to be available for embeddings and answer generation.")
        return

    await send_typing(update)
    try:
        query_embedding = await run_blocking(get_openai_embedding, question)
        matches = await run_blocking(search_entries, query_embedding, ASK_TOP_K, None)

        if not matches or float(matches[0]["relevance"]) < ASK_MIN_RELEVANCE:
            await reply_text(update, ASK_FAIL_MESSAGE)
            return

        messages = build_memory_prompt(question, matches)
        answer = await run_blocking(
            openai_client.responses.create,
            model=PROVIDERS["openai"].active_model,
            input=messages,
        )
        await reply_text(update, answer.output_text or "(empty response)")
    except Exception as exc:
        logger.exception("/ask failed")
        await reply_text(update, f"/ask failed: {exc}")


async def search_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    raw_query = get_command_arg(update)
    if not raw_query:
        await reply_text(update, "Usage: /search <query> [tag:TagName]")
        return

    query, tag = extract_tag_filter(raw_query)
    if not query:
        await reply_text(update, "Usage: /search <query> [tag:TagName]")
        return

    await send_typing(update)
    try:
        results = await run_blocking(hybrid_search_entries, query, 10, tag)
        await reply_text(update, build_search_results_message(results[:10], query))
    except Exception as exc:
        logger.exception("/search failed")
        await reply_text(update, f"/search failed: {exc}")


async def tags_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        counts = await run_blocking(get_tag_counts)
        if not counts:
            await reply_text(update, "No tags yet.")
            return
        text_value = "Tag counts:\n" + "\n".join(f"- {tag}: {count}" for tag, count in counts)
        await reply_text(update, text_value)
    except Exception as exc:
        logger.exception("/tags failed")
        await reply_text(update, f"/tags failed: {exc}")


async def forced_type_save_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, forced_type: str) -> None:
    content = get_command_arg(update)
    if not content:
        await reply_text(update, f"Usage: /{forced_type} <text>")
        return

    await send_typing(update)
    try:
        metadata, _ = await run_blocking(prepare_entry_metadata, content, forced_type, None, False, None)
        embedding: Optional[list[float]] = None

        if openai_client:
            try:
                embedding = await run_blocking(get_openai_embedding, content)
            except Exception as exc:
                logger.warning("Embedding failed during forced-type capture; saving without embedding: %s", exc)

        tags = await run_blocking(classify_tags, content)
        status = "open" if forced_type == "task" else None
        entry_id = await run_blocking(
            save_entry,
            content,
            embedding,
            tags,
            metadata.get("who"),
            metadata.get("title"),
            metadata.get("language"),
            forced_type,
            status,
            "telegram",
            metadata.get("due_date"),
        )
        await reply_text(update, f"✓ Saved (#{entry_id}) | Who: {metadata.get('who') or 'Я'} | Type: {forced_type}")
    except Exception as exc:
        logger.exception("Forced type save failed")
        await reply_text(update, f"Save failed: {exc}")


async def book_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await forced_type_save_handler(update, context, "book")


async def task_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await forced_type_save_handler(update, context, "task")


async def idea_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await forced_type_save_handler(update, context, "idea")


async def person_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await forced_type_save_handler(update, context, "person")


async def edit_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message_text = (update.message.text or "").strip()
    parts = message_text.split(maxsplit=2)
    if len(parts) < 3 or not parts[2].strip() or not parts[1].isdigit():
        await reply_text(update, "Usage: /edit [id] [new text]")
        return

    entry_id = int(parts[1])
    new_content = parts[2].strip()
    await send_typing(update)
    try:
        metadata, _ = await run_blocking(prepare_entry_metadata, new_content, None, None, False, None)
        embedding: Optional[list[float]] = None
        if openai_client:
            try:
                embedding = await run_blocking(get_openai_embedding, new_content)
            except Exception as exc:
                logger.warning("Embedding failed during edit; saving without embedding: %s", exc)
        tags = await run_blocking(classify_tags, new_content)
        status = "open" if metadata.get("type") == "task" else None
        updated = await run_blocking(
            update_entry,
            entry_id,
            new_content,
            embedding,
            tags,
            metadata.get("who"),
            metadata.get("title"),
            metadata.get("language"),
            metadata.get("type"),
            status,
            metadata.get("due_date"),
            metadata.get("topic"),
        )
        if not updated:
            await reply_text(update, "Entry not found")
            return
        title_text = metadata.get("title") or "(no title)"
        await reply_text(update, f"✏️ Entry #{entry_id} updated. Type: {metadata.get('type') or 'highlight'} | Who: {metadata.get('who') or 'Я'} | Title: {title_text}")
    except Exception as exc:
        logger.exception("/edit failed")
        await reply_text(update, f"/edit failed: {exc}")


async def pull_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_typing(update)
    try:
        pulled_count = await run_blocking(pull_sheet_updates_to_database)
        await reply_text(update, f"Pulled {pulled_count} updated entries from Google Sheets.")
    except Exception as exc:
        logger.exception("/pull failed")
        await reply_text(update, f"Pull failed: {exc}")


async def sync_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_typing(update)
    try:
        result = await run_blocking(sync_google_sheet_bidirectional)
        parts = []
        if result.pulled_count:
            parts.append(f"Pulled {result.pulled_count} updated entries from Google Sheets.")
        if result.updated_count:
            parts.append(f"Synced {result.synced_count} new entries to Google Sheets and updated {result.updated_count} existing rows.")
        else:
            parts.append(f"Synced {result.synced_count} new entries to Google Sheets.")
        await reply_text(update, " ".join(parts))
    except Exception as exc:
        logger.exception("/sync failed")
        await reply_text(update, f"Sync failed: {exc}")


async def sheet_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not GOOGLE_SHEET_URL:
        await reply_text(update, "Google Sheet is not configured yet.")
        return
    await reply_text(update, GOOGLE_SHEET_URL)


async def reply_review_sections(update: Update, review_text: str) -> None:
    sections = await run_blocking(split_review_sections, review_text)
    if not sections:
        await reply_text(update, review_text)
        return
    for section in sections:
        await reply_text(update, section)


async def last_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    arg = get_command_arg(update)
    limit = 5
    if arg:
        if not arg.isdigit():
            await reply_text(update, "Usage: /last [N]")
            return
        limit = max(1, min(20, int(arg)))
    await send_typing(update)
    try:
        entries = await run_blocking(fetch_recent_entries, limit)
        await reply_text(update, build_last_entries_message(entries, limit))
    except Exception as exc:
        logger.exception("/last failed")
        await reply_text(update, f"/last failed: {exc}")


async def tasks_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_typing(update)
    try:
        entries = await run_blocking(fetch_entries_by_type, "task", "open")
        if not entries:
            await reply_text(update, "✅ No open tasks!")
            return
        await reply_text(update, build_entry_list_message(entries, "📋 Open tasks", True))
    except Exception as exc:
        logger.exception("/tasks failed")
        await reply_text(update, f"/tasks failed: {exc}")


def format_job_runs_lines(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "No job runs recorded yet."

    lines: list[str] = []
    for row in rows:
        status = normalize_whitespace(str(row.get("status") or "")).lower()
        icon = "✅" if status == "done" else "❌" if status == "failed" else "⏳"
        job_name = normalize_whitespace(str(row.get("job_name") or "-"))[:12]
        started_at = row.get("started_at")
        finished_at = row.get("finished_at")
        error_message = normalize_whitespace(str(row.get("error_message") or ""))

        if hasattr(started_at, "astimezone"):
            time_text = started_at.astimezone(APP_TZ).strftime("%H:%M")
        else:
            time_text = "--:--"

        if status == "running" or not hasattr(started_at, "astimezone") or not hasattr(finished_at, "astimezone"):
            suffix = "(running)"
        else:
            duration_seconds = max(0, int((finished_at - started_at).total_seconds()))
            suffix = f"({duration_seconds}s)"

        line = f"{icon} {job_name:<12} {time_text}  {suffix}"
        if status == "failed" and error_message:
            line += f" — {error_message[:80]}"
        lines.append(line)
    return "\n".join(lines)


async def jobs_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_typing(update)
    try:
        rows = await run_blocking(get_recent_job_runs, 10)
        if not rows:
            await reply_text(update, "No job runs recorded yet.")
            return
        body = format_job_runs_lines(rows)
        if update.effective_chat:
            remember_chat_id(update.effective_chat.id)
        await update.message.reply_text(f"<pre>{escape(body)}</pre>", parse_mode="HTML")
    except Exception as exc:
        logger.exception("/jobs failed")
        await reply_text(update, f"/jobs failed: {exc}")


async def handle_action_intent(update: Update, intent_payload: dict[str, Any], message_text: str) -> bool:
    subtype = normalize_whitespace(str(intent_payload.get("subtype") or "")).lower()
    target = normalize_whitespace(str(intent_payload.get("target") or ""))
    parameters = intent_payload.get("parameters") if isinstance(intent_payload.get("parameters"), dict) else {}
    normalized_text = normalize_whitespace(message_text).lower()

    if subtype.endswith("list") or subtype == "list":
        requested_type = normalize_whitespace(str(parameters.get("type") or target or "")).lower()
        if "task" in requested_type or "задач" in requested_type or "tasks" in normalized_text or "задач" in normalized_text:
            entries = await run_blocking(fetch_entries_by_type, "task", "open")
            if not entries:
                await reply_text(update, "✅ No open tasks!")
            else:
                await reply_text(update, build_entry_list_message(entries, "📋 Open tasks", True))
            return True
        if "idea" in requested_type or "иде" in requested_type or "ideas" in normalized_text or "идеи" in normalized_text:
            entries = await run_blocking(fetch_entries_by_type, "idea")
            entries = [entry for entry in entries if entry.get("status") != "done"]
            if not entries:
                await reply_text(update, "💡 Ideas (0):")
            else:
                await reply_text(update, build_entry_list_message(entries, "💡 Ideas", False))
            return True
        await reply_text(update, "Не понял действие — уточни или используй команду.")
        return True

    if subtype.endswith("show_entry") or subtype == "show_entry":
        entry = await run_blocking(find_action_target_entry, target, message_text)
        if not entry:
            await reply_text(update, "Entry not found.")
            return True
        await reply_text(update, build_entry_detail_message(entry))
        return True

    if subtype.endswith("status_update") or subtype == "status_update":
        entry = await run_blocking(find_action_target_entry, target, message_text)
        if not entry:
            await reply_text(update, "Entry not found.")
            return True
        new_status = infer_status_from_action_intent(intent_payload, message_text)
        updated = await run_blocking(update_entry_status, int(entry["id"]), new_status)
        if not updated:
            await reply_text(update, "Entry not found")
            return True
        label = "done" if new_status == "done" else "open"
        await reply_text(update, f"✅ Entry #{entry['id']} marked as {label}.")
        return True

    await reply_text(update, "Не понял действие — уточни или используй команду.")
    return True


async def handle_query_intent(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str, language: str) -> bool:
    entry_id = extract_standalone_entry_id(query)
    if entry_id is not None:
        entry = await run_blocking(fetch_entry_by_id, entry_id)
        if not entry:
            await reply_text(update, f"Записи #{entry_id} не существует.")
            return True
        await reply_text(update, build_entry_detail_message(entry))
        return True

    results = await run_blocking(hybrid_search_entries, query, 5, None)
    logger.info("[QUERY] search returned %d results for: %s", len(results), query)
    if not results:
        await reply_text(update, localized_no_query_results(language))
        return True

    answer = await answer_from_context(query, results, language)
    if answer is None:
        logger.info("[QUERY] answer failed, falling through")
        return False

    await reply_text(update, answer)
    context.user_data["pending_query_result"] = {
        "answer": answer,
        "language": language,
    }
    await reply_text(update, localized_save_context_prompt(language))
    return True


async def show_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    arg = get_command_arg(update)
    entry_ids = parse_entry_id_list(arg)
    if not entry_ids:
        await reply_text(update, "Usage: /show [id] or /show [id1, id2, id3]")
        return

    await send_typing(update)
    try:
        missing_ids: list[int] = []
        for entry_id in entry_ids:
            entry = await run_blocking(fetch_entry_by_id, entry_id)
            if not entry:
                missing_ids.append(entry_id)
                continue
            await reply_text(update, build_entry_detail_message(entry))
        if missing_ids:
            await reply_text(update, "Entry not found: " + ", ".join(f"#{entry_id}" for entry_id in missing_ids))
    except Exception as exc:
        logger.exception("/show failed")
        await reply_text(update, f"/show failed: {exc}")


async def ideas_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_typing(update)
    try:
        entries = await run_blocking(fetch_entries_by_type, "idea")
        entries = [entry for entry in entries if entry.get("status") != "done"]
        if not entries:
            await reply_text(update, "💡 Ideas (0):")
            return
        await reply_text(update, build_entry_list_message(entries, "💡 Ideas", False))
    except Exception as exc:
        logger.exception("/ideas failed")
        await reply_text(update, f"/ideas failed: {exc}")


async def reply_open_tasks(update: Update) -> None:
    open_tasks = await run_blocking(fetch_open_tasks)
    message = await run_blocking(format_open_tasks_message, open_tasks)
    await reply_text(update, message)


async def review_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_typing(update)
    try:
        cached_review = await run_blocking(get_existing_review_for_window)
        if cached_review and cached_review.review_text:
            is_current = await run_blocking(review_matches_current_format, cached_review.review_text)
            if is_current:
                await reply_review_sections(update, cached_review.review_text)
                await reply_open_tasks(update)
                return

        result = await run_blocking(generate_and_store_weekly_review)
        if not result:
            await reply_text(update, "Nothing new this week.")
            return
        await reply_review_sections(update, result.review_text)
        await reply_open_tasks(update)
    except Exception as exc:
        logger.exception("/review failed")
        cached_review = await run_blocking(get_existing_review_for_window)
        if cached_review and cached_review.review_text:
            await reply_review_sections(update, cached_review.review_text)
            await reply_open_tasks(update)
            return
        await reply_text(update, f"Review failed: {exc}")


async def briefing_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_typing(update)
    try:
        result = await run_blocking(generate_and_store_daily_briefing)
        await reply_text(update, result.briefing_text)
    except Exception as exc:
        logger.exception("/briefing failed")
        await reply_text(update, f"/briefing failed: {exc}")


async def done_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    arg = get_command_arg(update)
    if not arg or not arg.isdigit():
        await reply_text(update, "Usage: /done [id]")
        return

    entry_id = int(arg)
    await send_typing(update)
    try:
        updated, entry_type = await run_blocking(mark_task_done, entry_id)
        if not updated:
            await reply_text(update, "Entry not found")
            return
        label = "Task" if entry_type == "task" else "Idea"
        await reply_text(update, f"✅ {label} #{entry_id} marked as done.")
    except Exception as exc:
        logger.exception("/done failed")
        await reply_text(update, f"/done failed: {exc}")


async def handle_reply_edit(update: Update, reply_text_value: str, entry_id: int) -> bool:
    original_entry = await run_blocking(fetch_entry_by_id, entry_id)
    if not original_entry:
        await reply_text(update, "Entry not found")
        return True

    candidate_text = reply_text_value.strip()
    if not candidate_text:
        return True

    try:
        if len(candidate_text) > 20:
            new_content = candidate_text
            metadata, _ = await run_blocking(prepare_entry_metadata, new_content, None, None, False, None)
            embedding: Optional[list[float]] = None
            if openai_client:
                try:
                    embedding = await run_blocking(get_openai_embedding, new_content)
                except Exception as exc:
                    logger.warning("Embedding failed during reply edit; saving without embedding: %s", exc)
            tags = await run_blocking(classify_tags, new_content)
            status = "open" if metadata.get("type") == "task" else None
            updated = await run_blocking(
                update_entry,
                entry_id,
                new_content,
                embedding,
                tags,
                metadata.get("who"),
                metadata.get("title"),
                metadata.get("language"),
                metadata.get("type"),
                status,
                metadata.get("due_date"),
                metadata.get("topic"),
            )
            if not updated:
                await reply_text(update, "Entry not found")
                return True
            await reply_text(update, f"✏️ Entry #{entry_id} updated | Who: {metadata.get('who') or 'Я'} | Type: {metadata.get('type') or 'highlight'}")
            return True

        corrected_content = await run_blocking(apply_correction_instruction, original_entry.get("content") or "", candidate_text)
        metadata, _ = await run_blocking(prepare_entry_metadata, corrected_content, None, None, False, None)
        embedding: Optional[list[float]] = None
        if openai_client:
            try:
                embedding = await run_blocking(get_openai_embedding, corrected_content)
            except Exception as exc:
                logger.warning("Embedding failed during reply correction; saving without embedding: %s", exc)
        tags = await run_blocking(classify_tags, corrected_content)
        status = "open" if metadata.get("type") == "task" else None
        updated = await run_blocking(
            update_entry,
            entry_id,
            corrected_content,
            embedding,
            tags,
            metadata.get("who"),
            metadata.get("title"),
            metadata.get("language"),
            metadata.get("type"),
            status,
            metadata.get("due_date"),
            metadata.get("topic"),
        )
        if not updated:
            await reply_text(update, "Entry not found")
            return True
        await reply_text(update, f'✏️ Entry #{entry_id} corrected | "{truncate(corrected_content, 120)}"')
        return True
    except Exception as exc:
        logger.exception("Reply-to-fix failed")
        await reply_text(update, f"Reply edit failed: {exc}")
        return True


async def capture_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    if not cfg.telegram.silent_capture_default:
        return

    content = update.message.text.strip()
    if not content or content.startswith("/"):
        return

    if "pending_query_result" in context.user_data:
        pending_query_result = context.user_data["pending_query_result"]
        del context.user_data["pending_query_result"]
        if pending_query_result and is_affirmative_reply(content):
            answer = normalize_whitespace(str(pending_query_result.get("answer") or ""))
            language = normalize_whitespace(str(pending_query_result.get("language") or detect_language_bucket(answer))).lower()
            embedding: Optional[list[float]] = None
            if openai_client:
                try:
                    embedding = await run_blocking(get_openai_embedding, answer)
                except Exception as exc:
                    logger.warning("Embedding failed during query result save; saving without embedding: %s", exc)
            entry_id = await run_blocking(
                save_entry,
                answer,
                embedding,
                ["query_result"],
                "System",
                "Query result",
                language,
                "note",
                None,
                "query_result",
                None,
                None,
                None,
            )
            await reply_text(update, f"{SAVE_CONFIRMATION_TEXT} (#{entry_id}) | Type: note | Tag: query_result")
        return

    detected_language = detect_language_bucket(content)
    intent_payload: Optional[dict[str, Any]] = None
    if cfg.intent_layer_enabled:
        try:
            intent_payload = await asyncio.wait_for(classify_intent(content, detected_language), timeout=9)
        except Exception as exc:
            logger.warning("[INTENT] classification failed: %s", exc)
            intent_payload = None

        if intent_payload:
            intent_name = normalize_whitespace(str(intent_payload.get("intent") or "capture")).lower()
            subtype = normalize_whitespace(str(intent_payload.get("subtype") or ""))
            confidence = float(intent_payload.get("confidence") or 0.0)
            logger.info("[INTENT] %s/%s confidence=%.2f", intent_name, subtype or "-", confidence)
            if intent_name == "action" and confidence >= 0.7:
                await send_typing(update)
                handled = await handle_action_intent(update, intent_payload, content)
                if handled:
                    return
            if intent_name == "query" and confidence >= 0.7:
                await send_typing(update)
                handled = await handle_query_intent(update, context, content, detected_language)
                if handled:
                    return

    reply_message = getattr(update.message, "reply_to_message", None)
    if reply_message and getattr(reply_message, "text", None):
        replied_id = extract_entry_id_from_text(reply_message.text or "")
        if replied_id:
            await send_typing(update)
            handled = await handle_reply_edit(update, content, replied_id)
            if handled:
                return

    forwarded_sender = extract_forwarded_sender_name(update.message)
    if forwarded_sender:
        content = f"📨 Forwarded from {forwarded_sender}:\n{content}"

    await send_typing(update)
    await process_captured_content(update, content, source="telegram", who_override=forwarded_sender)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled telegram error", exc_info=context.error)


# =========================
# Main
# =========================

def build_application() -> Application:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("health", health_handler))
    app.add_handler(CommandHandler("gpt", gpt_handler))
    app.add_handler(CommandHandler("claude", claude_handler))
    app.add_handler(CommandHandler("gemini", gemini_handler))
    app.add_handler(CommandHandler("all", all_handler))
    app.add_handler(CommandHandler("askg", askg_handler))
    app.add_handler(CommandHandler("ask", ask_handler))
    app.add_handler(CommandHandler("search", search_handler))
    app.add_handler(CommandHandler("show", show_handler))
    app.add_handler(CommandHandler("last", last_handler))
    app.add_handler(CommandHandler("tags", tags_handler))
    app.add_handler(CommandHandler("tasks", tasks_handler))
    app.add_handler(CommandHandler("jobs", jobs_handler))
    app.add_handler(CommandHandler("ideas", ideas_handler))
    app.add_handler(CommandHandler("book", book_handler))
    app.add_handler(CommandHandler("task", task_handler))
    app.add_handler(CommandHandler("idea", idea_handler))
    app.add_handler(CommandHandler("person", person_handler))
    app.add_handler(CommandHandler("edit", edit_handler))
    app.add_handler(CommandHandler("pull", pull_handler))
    app.add_handler(CommandHandler("sync", sync_handler))
    app.add_handler(CommandHandler("sheet", sheet_handler))
    app.add_handler(CommandHandler("review", review_handler))
    app.add_handler(CommandHandler("briefing", briefing_handler))
    app.add_handler(CommandHandler("done", done_handler))
    app.add_handler(MessageHandler(filters.VOICE, voice_message_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, capture_message_handler))

    app.add_error_handler(error_handler)
    return app


def main() -> None:
    ensure_entries_table()
    ensure_job_runs_table()
    mark_running_job_runs_failed_on_startup()

    if cfg.health.startup_provider_validation:
        validate_all_providers()

    logger.info(
        "Startup provider status | openai=%s:%s | anthropic=%s:%s | google=%s:%s",
        PROVIDERS["openai"].available,
        PROVIDERS["openai"].active_model,
        PROVIDERS["anthropic"].available,
        PROVIDERS["anthropic"].active_model,
        PROVIDERS["google"].available,
        PROVIDERS["google"].active_model,
    )

    app = build_application()
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
