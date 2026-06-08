from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI
from sqlalchemy import text
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app.entity_resolver import (
    append_entry_to_resolution_task,
    backfill_who_ids_for_entries,
    find_open_resolution_task,
    find_skip_task,
    fuzzy_match_entities,
    get_resolution_task_entry_ids,
)
from app.inline_keyboards import register_callback_handler
from app.sheets_sync import engine

NP_PREFIX = "np"
NPP_PREFIX = "npp"
FUZZY_THRESHOLD = 0.3
MAX_FUZZY_BUTTONS = 5
PROFILE_MODEL = "gpt-4o-mini"
PROFILE_SYSTEM_PROMPT = """Extract a short structured profile from a free-form description of a person.
Return strict JSON with keys: relationship, role, note.
- relationship: one of [wife, husband, brother, sister, son, daughter, niece, nephew, father, mother, godfather, godmother, friend, colleague, boss, client, pet] or null. null if not clearly stated.
- role: short free-text job/title/role, or null.
- note: any other important context in one short sentence, or null.
Do not invent. Return null for any field the text does not support."""
PROFILE_KEYS = ("relationship", "role", "note")

logger = logging.getLogger(__name__)

load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None


def build_new_person_keyboard(entry_id: int, fuzzy_matches: list[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for match in fuzzy_matches[:MAX_FUZZY_BUTTONS]:
        rows.append([
            InlineKeyboardButton(
                f"👤 {match['canonical_name']}",
                callback_data=f"{NP_PREFIX}:link:{int(match['id'])}:{int(entry_id)}",
            )
        ])
    rows.append([InlineKeyboardButton("➕ Create new", callback_data=f"{NP_PREFIX}:create:{int(entry_id)}")])
    rows.append([
        InlineKeyboardButton("⏰ Later", callback_data=f"{NP_PREFIX}:later:{int(entry_id)}"),
        InlineKeyboardButton("⏭ Skip", callback_data=f"{NP_PREFIX}:skip:{int(entry_id)}"),
    ])
    return InlineKeyboardMarkup(rows)


def build_profile_prompt_keyboard(entity_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✍️ Yes", callback_data=f"{NPP_PREFIX}:fill:{int(entity_id)}")],
        [InlineKeyboardButton("⏰ Later", callback_data=f"{NPP_PREFIX}:skip:{int(entity_id)}")],
    ])


def build_skip_profile_keyboard(entity_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏭ Skip profile", callback_data=f"{NPP_PREFIX}:cancel:{int(entity_id)}")]
    ])


def _parse_json_object_from_text(text_value: str) -> dict[str, Any]:
    cleaned = (text_value or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            raise
        payload = json.loads(match.group(0))
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object")
    return payload


def _fetch_entry_who(entry_id: int, conn) -> str | None:
    return conn.execute(text("SELECT who FROM entries WHERE id = :id"), {"id": entry_id}).scalar_one_or_none()


def _fetch_entity_name(entity_id: int, conn) -> str | None:
    return conn.execute(text("SELECT canonical_name FROM entities WHERE id = :id"), {"id": entity_id}).scalar_one_or_none()


def _complete_resolution_task(raw_who: str, entity_id: int, conn) -> None:
    task_id = find_open_resolution_task(raw_who, conn)
    if not task_id:
        return
    entry_ids = get_resolution_task_entry_ids(task_id, conn)
    backfill_who_ids_for_entries(entry_ids, entity_id, conn)
    conn.execute(text("UPDATE entries SET status = 'done', updated_at = now() WHERE id = :id"), {"id": task_id})


async def handle_unresolved_who(entry_id: int, raw_who: str, chat_id: int, context) -> None:
    try:
        with engine.begin() as conn:
            if find_skip_task(raw_who, conn) is not None:
                logger.info("skip-task exists for raw_who=%r; silent", raw_who)
                return
            open_task_id = find_open_resolution_task(raw_who, conn)
            if open_task_id is not None:
                append_entry_to_resolution_task(open_task_id, entry_id, conn)
                logger.info("appended entry #%s to open resolution task #%s", entry_id, open_task_id)
                return
            fuzzy = fuzzy_match_entities(raw_who, conn, FUZZY_THRESHOLD, MAX_FUZZY_BUTTONS)

        reply_markup = build_new_person_keyboard(entry_id, fuzzy)
        if fuzzy:
            text_value = f'New person: "{raw_who}" (from entry #{entry_id})\n\nDid you mean one of these?'
        else:
            text_value = f'New person: "{raw_who}" (from entry #{entry_id})'
        await context.bot.send_message(chat_id, text_value, reply_markup=reply_markup)
    except Exception:
        logger.exception("new-person flow failed for entry #%s raw_who=%r", entry_id, raw_who)


async def handle_new_person_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    await query.answer()
    parts = query.data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    try:
        edit_text = "Done."
        edit_markup = None
        followup: tuple[str, InlineKeyboardMarkup] | None = None

        with engine.begin() as conn:
            if action == "link":
                entity_id = int(parts[2])
                entry_id = int(parts[3])
                raw_who = _fetch_entry_who(entry_id, conn)
                if raw_who is None:
                    await query.edit_message_text("Entry not found.")
                    return
                conn.execute(
                    text(
                        """
                        UPDATE entities
                        SET aliases = array_append(aliases, :raw_who), updated_at = now()
                        WHERE id = :entity_id
                          AND NOT (:raw_who = ANY(aliases))
                        """
                    ),
                    {"entity_id": entity_id, "raw_who": raw_who},
                )
                backfill_who_ids_for_entries([entry_id], entity_id, conn)
                _complete_resolution_task(raw_who, entity_id, conn)
                canonical = _fetch_entity_name(entity_id, conn) or f"entity #{entity_id}"
                edit_text = f"Linked to {canonical} ✓"

            elif action == "create":
                entry_id = int(parts[2])
                raw_who = _fetch_entry_who(entry_id, conn)
                if raw_who is None:
                    await query.edit_message_text("Entry not found.")
                    return
                new_id = conn.execute(
                    text(
                        """
                        INSERT INTO entities (canonical_name, aliases, type, profile)
                        VALUES (:raw_who, ARRAY[:raw_who], 'person', '{}'::jsonb)
                        RETURNING id
                        """
                    ),
                    {"raw_who": raw_who},
                ).scalar_one()
                new_id = int(new_id)
                backfill_who_ids_for_entries([entry_id], new_id, conn)
                _complete_resolution_task(raw_who, new_id, conn)
                edit_text = f'Created "{raw_who}" ✓\n\nAdd profile now?'
                edit_markup = build_profile_prompt_keyboard(new_id)

            elif action == "later":
                entry_id = int(parts[2])
                raw_who = _fetch_entry_who(entry_id, conn)
                if raw_who is None:
                    await query.edit_message_text("Entry not found.")
                    return
                content = f'Resolve unknown person "{raw_who}" from entry #{entry_id}'
                task_id = conn.execute(
                    text(
                        """
                        INSERT INTO entries (content, type, status, due_date, priority, who, metadata, source, language)
                        VALUES (:content, 'task', 'open', (CURRENT_DATE + INTERVAL '1 day')::date, 2, NULL,
                                jsonb_build_object('raw_who', :raw_who,
                                                   'entry_ids', jsonb_build_array(:entry_id),
                                                   'action', 'resolve'),
                                'system', 'en')
                        RETURNING id
                        """
                    ),
                    {"content": content, "raw_who": raw_who, "entry_id": entry_id},
                ).scalar_one()
                fuzzy = fuzzy_match_entities(raw_who, conn, FUZZY_THRESHOLD, MAX_FUZZY_BUTTONS)
                followup = (
                    f'Reminder: resolve "{raw_who}" (task #{int(task_id)}, entry #{entry_id})',
                    build_new_person_keyboard(entry_id, fuzzy),
                )
                edit_text = f"Later — resolution queued ✓\nTask #{int(task_id)} due tomorrow"

            elif action == "skip":
                entry_id = int(parts[2])
                raw_who = _fetch_entry_who(entry_id, conn)
                if raw_who is None:
                    await query.edit_message_text("Entry not found.")
                    return
                content = f'Skipped unknown person "{raw_who}"'
                conn.execute(
                    text(
                        """
                        INSERT INTO entries (content, type, status, due_date, priority, who, metadata, source, language)
                        VALUES (:content, 'task', 'archived', NULL, NULL, NULL,
                                jsonb_build_object('raw_who', :raw_who,
                                                   'entry_ids', jsonb_build_array(:entry_id),
                                                   'action', 'skip'),
                                'system', 'en')
                        """
                    ),
                    {"content": content, "raw_who": raw_who, "entry_id": entry_id},
                )
                edit_text = f'Skipped "{raw_who}" ✓'

            else:
                await query.edit_message_text("Unknown action.")
                return

        await query.edit_message_text(edit_text, reply_markup=edit_markup)
        if followup and update.effective_chat:
            await context.bot.send_message(update.effective_chat.id, followup[0], reply_markup=followup[1])
    except Exception:
        logger.exception("new-person callback failed: %r", query.data)
        try:
            await query.edit_message_text("New-person action failed. Check logs.")
        except Exception:
            pass


async def handle_profile_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    await query.answer()
    parts = query.data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    entity_id = int(parts[2])

    try:
        if action == "fill":
            with engine.begin() as conn:
                canonical = _fetch_entity_name(entity_id, conn)
            if canonical is None:
                await query.edit_message_text("That person no longer exists.")
                return
            context.user_data["awaiting_profile_for"] = entity_id
            await query.edit_message_text(
                f"Tell me about {canonical} — relationship, role, anything important.\n"
                f"(Free-form, I'll structure it. Or tap below to skip.)",
                reply_markup=build_skip_profile_keyboard(entity_id),
            )
        elif action == "skip":
            await query.edit_message_text("Profile can be filled later via the People tab.")
        elif action == "cancel":
            context.user_data.pop("awaiting_profile_for", None)
            await query.edit_message_text("Profile skipped. You can fill it later via the People tab.")
        else:
            await query.edit_message_text("Unknown profile action.")
    except Exception:
        logger.exception("profile callback failed: %r", query.data)
        try:
            await query.edit_message_text("Profile action failed. Check logs.")
        except Exception:
            pass


def extract_profile_from_text(text_input: str) -> dict:
    empty = {"relationship": None, "role": None, "note": None}
    if not openai_client:
        return empty

    try:
        response = openai_client.responses.create(
            model=PROFILE_MODEL,
            input=[
                {"role": "system", "content": PROFILE_SYSTEM_PROMPT},
                {"role": "user", "content": text_input},
            ],
            max_output_tokens=160,
        )
        payload = _parse_json_object_from_text(response.output_text or "{}")
        return {key: payload.get(key) for key in PROFILE_KEYS}
    except Exception:
        logger.exception("profile extraction failed")
        return empty


async def handle_profile_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Return True when a message was consumed as profile-fill text."""
    entity_id = context.user_data.get("awaiting_profile_for")
    if entity_id is None:
        return False
    context.user_data.pop("awaiting_profile_for", None)

    raw_text = update.effective_message.text or ""
    profile = extract_profile_from_text(raw_text)
    new_profile = {key: value for key, value in profile.items() if value is not None}

    with engine.begin() as conn:
        if new_profile:
            row = conn.execute(
                text(
                    """
                    UPDATE entities
                    SET profile = profile || CAST(:profile AS jsonb), updated_at = now()
                    WHERE id = :id
                    RETURNING canonical_name, profile
                    """
                ),
                {"profile": json.dumps(new_profile), "id": entity_id},
            ).mappings().first()
        else:
            row = conn.execute(
                text("SELECT canonical_name, profile FROM entities WHERE id = :id"),
                {"id": entity_id},
            ).mappings().first()

    if row is None:
        await update.effective_message.reply_text("That person no longer exists.")
        return True

    saved_profile = row["profile"] or {}
    lines = ["Saved."]
    for key in PROFILE_KEYS:
        value = saved_profile.get(key)
        if value:
            lines.append(f"  {key}: {value}")
    if len(lines) == 1:
        lines.append("  (nothing structured extracted — you can edit via the People tab)")
    await update.effective_message.reply_text("\n".join(lines))
    return True


register_callback_handler(NP_PREFIX, handle_new_person_callback)
register_callback_handler(NPP_PREFIX, handle_profile_callback)
