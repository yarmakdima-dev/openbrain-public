# OpenBrain

A self-hosted personal knowledge system. Capture thoughts via Telegram in multiple languages, store them with semantic search, and query them from any AI tool via MCP.

## What it does

- **Capture**: Telegram bot accepts text and voice notes in 4 languages (EN, RU, PL, DE). Voice is transcribed via Whisper.
- **Store**: PostgreSQL with pgvector for semantic search. Entries get auto-tagged (who, topic, type, language) via an LLM metadata pass.
- **Retrieve**: Hybrid search (vector + keyword). MCP server exposes the memory as tools to external AI clients like Claude Desktop.
- **Proactive**: Daily briefing at configurable time picks open tasks by urgency. Weekly review summarizes patterns and evolution.
- **Visual layer**: Bidirectional Google Sheets sync for manual review and editing.

## Architecture

- **Language**: Python 3.11+
- **Database**: PostgreSQL 16 with pgvector extension (runs in Docker)
- **LLMs**: OpenAI (EN/PL), Anthropic (RU), Gemini (DE) — language-routed. Small model for tagging (gpt-4o-mini), larger for weekly review.
- **Embeddings**: OpenAI `text-embedding-3-small`
- **Capture**: Telegram bot via `python-telegram-bot`
- **Transcription**: OpenAI Whisper API
- **Scheduler**: systemd timers + APScheduler
- **MCP**: Local MCP server exposing 6 tools (search, count, get by ID, recent by topic, recent by date, open tasks)

## Design decisions

- **Whisper API, not local**: target VPS is small (1 core / 1.9GB RAM). Local Whisper was not feasible.
- **Capture is silent by default**: most messages are notes, not commands. The bot acknowledges briefly and stores.
- **Two-pass task detection**: conservative extraction. False positives are worse than false negatives for a trust-based system.
- **Structured output for briefings**: the LLM picks entry IDs only; displayed text is templated from the DB. Eliminates cross-section ID misbinding.
- **Google Sheets as the visual layer**: no custom web UI. Users who already live in spreadsheets get a familiar surface.
- **Build the capture, rent the intelligence**: query and reasoning happen via MCP-connected Claude, not a custom chat UI inside the bot.

## Setup

This is a working system, not a polished template. Setup requires:

1. A Linux host with Docker and Python 3.11+.
2. API keys: OpenAI, Anthropic, Google (Gemini + Sheets), Telegram bot token.
3. A Telegram bot created via BotFather.
4. PostgreSQL with pgvector (provided via `docker-compose.yml`).
5. Google Cloud service account for Sheets access.
6. A `.env` file with all secrets (derive from `app/config.py` and `CONFIG.yaml`).
7. Run SQL migrations in `sql/` in order.
8. Systemd units for the bot and MCP server (not included — host-specific).

This repo is shared as a reference implementation, not a one-click installer. If you want to run it, expect to read the code.

## Repository layout

- `app/` — bot, scheduler, LLM routing, MCP server
- `scripts/` — operational scripts (sync to Sheets, config check)
- `sql/` — schema migrations
- `CONFIG.yaml` — non-secret configuration (models, schedules, topic vocabulary)
- `docker-compose.yml` — PostgreSQL + pgvector
- `requirements.txt` — Python dependencies

## Status

Active personal project. Public for transparency and reference. Issues and discussion welcome; expect asynchronous responses.

## License

MIT. See [LICENSE](LICENSE).
