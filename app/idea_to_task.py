from __future__ import annotations

import json
import logging
import os
import re

from sqlalchemy import text
from dotenv import load_dotenv
from openai import OpenAI
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app.inline_keyboards import register_callback_handler
from app.sheets_sync import engine
from app.entity_resolver import resolve_who_to_ids_safe

logger = logging.getLogger("openbrain.idea_to_task")

CALLBACK_PREFIX = "i2t"
TITLE_PREVIEW_MAX_LEN = 200
DERIVED_TITLE_MAX_LEN = 200
TASK_TITLE_DRAFT_MODEL = "gpt-4o-mini"
TASK_TITLE_DRAFT_SYSTEM_PROMPT = """You convert a personal note ('idea') into a short, actionable task title.
Rules:
- Output a single concise task title, imperative mood where natural.
- Maximum ~80 characters. Strip filler, keep the core action.
- Preserve the original language of the idea (Russian, English, Polish, or German).
- Do NOT add due dates, priorities, or commentary.
Return strict JSON: {"title": "<the task title>"}"""

load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None


class IdeaNotFound(Exception):
    pass


class WrongEntryType(Exception):
    pass


def create_task_from_idea(idea_id: int, title: str, source: str = "telegram") -> dict:
    """
    Create a task entry with spawned_from_idea_id set to idea_id.
    Inherits topic/who from the idea. Auto-flips idea status NULL -> open.
    Raises IdeaNotFound if the idea doesn't exist.
    Raises WrongEntryType if the target id exists but isn't an idea.
    Returns dict with new_task_id and inherited fields.
    """
    title = title.strip()
    if not title:
        raise ValueError("Task title cannot be empty")
    if len(title) > 200:
        title = title[:200]

    with engine.begin() as conn:
        idea = conn.execute(
            text("SELECT id, type, topic, who, status FROM entries WHERE id = :id"),
            {"id": idea_id},
        ).mappings().first()

        if idea is None:
            raise IdeaNotFound(f"No entry with id {idea_id}")
        if idea["type"] != "idea":
            raise WrongEntryType(f"Entry {idea_id} is type {idea['type']!r}, not 'idea'")

        who_ids = resolve_who_to_ids_safe(idea["who"], conn)
        new_task_id = conn.execute(
            text("""
                INSERT INTO entries (
                    type, status, title, content, topic, who, who_ids,
                    spawned_from_idea_id, source, created_at, updated_at
                )
                VALUES (
                    'task', 'open', :title, :title, :topic, :who, :who_ids,
                    :idea_id, :source, NOW(), NOW()
                )
                RETURNING id
            """),
            {
                "title": title,
                "topic": idea["topic"],
                "who": idea["who"],
                "who_ids": who_ids,
                "idea_id": idea_id,
                "source": source,
            },
        ).scalar()

        from app.entry_relations import insert_spawn_relation
        insert_spawn_relation(conn, new_task_id, idea_id, origin=f"idea_to_task:{source}")

        conn.execute(
            text("""
                UPDATE entries SET status = 'open', updated_at = NOW()
                WHERE id = :idea_id AND type = 'idea' AND status IS NULL
            """),
            {"idea_id": idea_id},
        )

    logger.info(
        "Created task id=%d from idea id=%d via %s: %r",
        new_task_id, idea_id, source, title,
    )
    return {
        "task_id": new_task_id,
        "idea_id": idea_id,
        "topic": idea["topic"],
        "who": idea["who"],
    }


def derive_task_title_from_idea(idea_content: str) -> str:
    """Used when /task <idea_id> is called without explicit title text."""
    title = (idea_content or "").strip().splitlines()[0] if idea_content else ""
    if len(title) > DERIVED_TITLE_MAX_LEN:
        title = title[:DERIVED_TITLE_MAX_LEN].rstrip() + "..."
    return title or "(untitled)"


def parse_json_object_from_text(text_value: str) -> dict:
    raw = (text_value or "").strip()
    try:
        raw = re.sub(r'^```(?:json)?\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw)
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def draft_task_title_from_idea(idea_content: str) -> str:
    if not openai_client:
        return derive_task_title_from_idea(idea_content)

    try:
        response = openai_client.responses.create(
            model=TASK_TITLE_DRAFT_MODEL,
            input=[
                {"role": "system", "content": TASK_TITLE_DRAFT_SYSTEM_PROMPT},
                {"role": "user", "content": idea_content or ""},
            ],
            max_output_tokens=120,
        )
        payload = parse_json_object_from_text(response.output_text or "{}")
        title = str(payload.get("title") or "").strip()
        if not title:
            raise ValueError("empty drafted title")
        if len(title) > DERIVED_TITLE_MAX_LEN:
            title = title[:DERIVED_TITLE_MAX_LEN].rstrip()
        return title
    except Exception:
        logger.exception("Task title drafting failed; falling back to first-line title")
        return derive_task_title_from_idea(idea_content)


def build_idea_to_task_draft_keyboard(idea_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Create", callback_data=f"{CALLBACK_PREFIX}:c:{idea_id}"),
            InlineKeyboardButton("✏️ Edit", callback_data=f"{CALLBACK_PREFIX}:e:{idea_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"{CALLBACK_PREFIX}:n:{idea_id}"),
        ]
    ])


async def send_idea_to_task_draft(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    idea_id: int,
) -> None:
    """Called from /task and reply-to-idea paths when no explicit title is given."""
    message = update.effective_message
    if message is None:
        return

    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT id, content, type FROM entries WHERE id = :id"),
            {"id": idea_id},
        ).first()

    if row is None:
        await message.reply_text(f"Idea #{idea_id} not found.")
        return
    if row.type != "idea":
        await message.reply_text(f"Entry #{idea_id} is not an idea (type={row.type}).")
        return

    content = row.content or ""
    drafted_title = draft_task_title_from_idea(content)
    preview = content[:TITLE_PREVIEW_MAX_LEN] + ("…" if len(content) > TITLE_PREVIEW_MAX_LEN else "")
    message_text = (
        f"Draft task from idea #{idea_id}:\n\n"
        f"Title: {drafted_title}\n\n"
        f"(from idea: \"{preview}\")"
    )
    kb = build_idea_to_task_draft_keyboard(idea_id)
    await message.reply_text(message_text, reply_markup=kb)


async def handle_idea_to_task_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    await query.answer()
    try:
        _, action, idea_id_str = query.data.split(":")
        idea_id = int(idea_id_str)
    except Exception:
        await query.edit_message_text("Invalid action.")
        return

    if action == "n":
        await query.edit_message_text("Cancelled.")
        return

    if action == "c":
        try:
            with engine.begin() as conn:
                row = conn.execute(
                    text("SELECT id, content, type FROM entries WHERE id = :id"),
                    {"id": idea_id},
                ).first()
            if row is None:
                await query.edit_message_text(f"Idea #{idea_id} no longer exists.")
                return
            if row.type != "idea":
                await query.edit_message_text(f"Entry #{idea_id} is no longer an idea.")
                return

            drafted_title = draft_task_title_from_idea(row.content or "")
            result = create_task_from_idea(idea_id, drafted_title)
            task_id = result["task_id"]
        except Exception as e:
            logger.exception("create_task_from_idea failed for idea_id=%s", idea_id)
            await query.edit_message_text(f"Failed to create task: {e}")
            return

        await query.edit_message_text(
            f"Task #{task_id} created from idea #{idea_id} ✓\nTitle: {drafted_title}"
        )
        return

    if action == "e":
        context.user_data["i2t_pending_edit_idea_id"] = idea_id
        await query.edit_message_text(
            f"Editing task from idea #{idea_id}.\n"
            f"Reply to this chat with the task title you want."
        )
        return

    await query.edit_message_text("Unknown action.")


async def handle_idea_to_task_edit_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    """Consume the next user message as the edited task title for an i2t draft."""
    pending_idea_id = context.user_data.get("i2t_pending_edit_idea_id")
    if pending_idea_id is None:
        return False

    context.user_data.pop("i2t_pending_edit_idea_id", None)

    title = (update.effective_message.text or "").strip()
    if not title:
        await update.effective_message.reply_text(
            "Empty title — cancelled. Run /task again if you want to retry."
        )
        return True

    try:
        result = create_task_from_idea(pending_idea_id, title)
        task_id = result["task_id"]
    except Exception as e:
        logger.exception("create_task_from_idea (edited) failed for idea_id=%s", pending_idea_id)
        await update.effective_message.reply_text(f"Failed to create task: {e}")
        return True

    await update.effective_message.reply_text(
        f"Task #{task_id} created from idea #{pending_idea_id} ✓\nTitle: {title}"
    )
    return True


register_callback_handler(CALLBACK_PREFIX, handle_idea_to_task_callback)
