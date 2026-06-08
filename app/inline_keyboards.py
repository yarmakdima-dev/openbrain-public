"""Inline keyboard infrastructure for Open Brain.

Conventions:
  - callback_data uses ':' as separator
  - First field is always a prefix that identifies the flow (e.g. 'i2t' for idea->task)
  - Total callback_data length must stay under 64 bytes (Telegram limit)
  - Each flow module registers its handler via register_callback_handler(prefix, async_handler)
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

_HANDLERS: dict[str, Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]] = {}


def register_callback_handler(prefix: str, handler: Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]) -> None:
    if prefix in _HANDLERS:
        raise ValueError(f"Callback prefix '{prefix}' already registered")
    _HANDLERS[prefix] = handler
    logger.info("Registered callback handler for prefix '%s'", prefix)


async def dispatch_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    try:
        prefix = query.data.split(":", 1)[0]
    except Exception:
        logger.warning("Malformed callback_data: %r", query.data)
        await query.answer("Invalid action.")
        return
    handler = _HANDLERS.get(prefix)
    if handler is None:
        logger.warning("No handler for callback prefix '%s'", prefix)
        await query.answer("This action is no longer available.")
        return
    try:
        await handler(update, context)
    except Exception:
        logger.exception("Callback handler '%s' failed", prefix)
        try:
            await query.answer("Something went wrong. Check logs.")
        except Exception:
            pass


def build_confirm_keyboard(
    yes_callback: str,
    cancel_callback: str,
    yes_label: str = "✅ Yes",
    cancel_label: str = "❌ Cancel",
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(yes_label, callback_data=yes_callback),
            InlineKeyboardButton(cancel_label, callback_data=cancel_callback),
        ]
    ])
