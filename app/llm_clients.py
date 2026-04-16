import json
import os
import re
from typing import Any

from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
INTENT_MODEL = "gpt-4o-mini"

openai_async_client = AsyncOpenAI(api_key=OPENAI_API_KEY, timeout=8.0) if OPENAI_API_KEY else None

INTENT_SYSTEM_PROMPT = """You are an intent classifier for a personal knowledge system.
Classify the user message into exactly one intent.

Intents:
- capture: user is recording something new (default)
- query: user is asking about existing content in their journal
- action: user wants to change something (mark done, edit, show entry, list tasks)
- conversation: user is thinking out loud, no clear artifact to store

Return JSON only, no markdown, no explanation:
{"intent": "...", "subtype": "...", "target": "...", "parameters": {}, "confidence": 0.0, "fallback": "capture"}

subtype examples:
- action/status_update, action/show_entry, action/list
- query/search, query/recent
- conversation/reflection

If confidence < 0.7, set intent to fallback value.
Default intent is always "capture"."""


def _format_entries_for_context(entries: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for entry in entries:
        created_at = entry.get("created_at")
        if hasattr(created_at, "strftime"):
            date_text = created_at.strftime("%Y-%m-%d")
        else:
            date_text = str(created_at or "unknown date")
        entry_type = entry.get("type") or "-"
        content = " ".join(str(entry.get("content") or "").split())
        lines.append(f"#{entry.get('id')} | {date_text} | {entry_type} | {content}")
    return "\n".join(lines)


def _fallback_intent() -> dict[str, Any]:
    return {"intent": "capture", "confidence": 0.0}


def _parse_json_object(text_value: str) -> dict[str, Any]:
    raw = (text_value or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except Exception:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


async def classify_intent(user_message: str, detected_language: str | None = None) -> dict[str, Any]:
    if not openai_async_client:
        return _fallback_intent()

    try:
        response = await openai_async_client.responses.create(
            model=INTENT_MODEL,
            input=[
                {"role": "system", "content": INTENT_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"Detected language: {detected_language or 'unknown'}\n"
                        f"Message:\n{user_message}"
                    ),
                },
            ],
            max_output_tokens=160,
        )
        payload = _parse_json_object(response.output_text or "{}")
        confidence = float(payload.get("confidence") or 0.0)
        intent = str(payload.get("intent") or "capture").strip().lower()
        fallback = str(payload.get("fallback") or "capture").strip().lower() or "capture"

        if confidence < 0.7:
            intent = fallback
        if intent not in {"capture", "query", "action", "conversation"}:
            intent = "capture"

        parameters = payload.get("parameters")
        if not isinstance(parameters, dict):
            parameters = {}

        return {
            "intent": intent,
            "subtype": str(payload.get("subtype") or "").strip(),
            "target": str(payload.get("target") or "").strip(),
            "parameters": parameters,
            "confidence": confidence,
            "fallback": fallback,
        }
    except Exception:
        return _fallback_intent()


async def answer_from_context(query: str, entries: list[dict[str, Any]], language: str) -> str | None:
    if not openai_async_client:
        return None

    try:
        system_prompt = f"""You are a personal memory assistant. The user is asking about their own journal.
Answer conversationally based only on the provided entries.
Be specific — mention dates, names, entry content where relevant.
If nothing useful is in the entries, say so honestly.
Reply in this language: {language}"""
        user_message = f"""Question: {query}

Journal entries:
{_format_entries_for_context(entries)}"""
        response = await openai_async_client.responses.create(
            model=INTENT_MODEL,
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            max_output_tokens=500,
        )
        answer = (response.output_text or "").strip()
        return answer or None
    except Exception:
        return None
