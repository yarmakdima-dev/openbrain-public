# OpenBrain

A self-hosted personal knowledge system. Capture thoughts via Telegram in multiple languages, store them with semantic search, and query them from any AI tool via MCP.

## What it does

- **Capture**: Telegram bot accepts text and voice notes in 4 languages (EN, RU, PL, DE). Voice is transcribed via Whisper. Capture is silent by default — the bot acknowledges briefly and stores.
- **Store**: PostgreSQL with pgvector for semantic search. Entries get auto-tagged on capture (who, topic, type, language) via an LLM metadata pass. A typed-edge table (`entry_relations`) tracks how entries connect — `spawned_from`, `continues`, `contradicts` — so the system holds a graph, not just a log.
- **Retrieve**: Hybrid search (vector + keyword). MCP server exposes the memory as tools to external AI clients (Claude Desktop, ChatGPT, any MCP-speaking tool) — both read tools and write tools (`add_entry`, `update_entry`, `set_status`), with parent-entry linkage so an external assistant can attach notes or maintenance edits to an existing thread of thought.
- **Proactive**: Daily briefing at a configurable time picks open tasks by urgency. Weekly review summarizes patterns. A Monday triage step surfaces drift across the backlog.
- **Visual layer**: Google Sheets sync. The Log and General tabs are push-only — read in Sheets, edit in DB. Six per-type tabs (Projects, Books, Highlights, Tasks, Ideas, People) sync bidirectionally with a 3-way merge between DB state, last-known snapshot, and current Sheet state — conflicts are surfaced per entry, not silently overwritten.

## Architecture

- **Language**: Python 3.11+
- **Database**: PostgreSQL 16 with pgvector extension (runs in Docker)
- **LLMs**: OpenAI (EN/PL), Anthropic (RU), Gemini (DE) — language-routed. Small model for tagging (`gpt-4o-mini`), larger for weekly review.
- **Embeddings**: OpenAI `text-embedding-3-small`
- **Capture**: Telegram bot via `python-telegram-bot`
- **Transcription**: OpenAI Whisper API
- **Scheduler**: systemd timers + APScheduler
- **MCP**: HTTPS endpoint behind nginx with URL-path token auth. Exposes read tools (search, count, get-by-id, recent-by-topic, recent-by-date, hybrid search, summaries) and write tools (add, update, set status) with `parent_entry_id` linkage for hierarchical capture.
- **Sync**: 3-way merge between DB ↔ Sheets per editable tab. Conflicts written to a `sync_error` column and surfaced in the Sheet.

## Design decisions

- **Whisper API, not local**: target VPS is small (1 core / 1.9GB RAM). Local Whisper was not feasible.
- **Capture surface stays narrow**: Telegram is capture-only. Reasoning, search, and maintenance happen via Claude / ChatGPT through MCP. An earlier intent-layer design that put query and command inside Telegram was built, then abandoned — Claude is a better query interface than anything built in Telegram, and MCP exposes the DB natively.
- **Two-pass task detection**: conservative extraction. False positives are worse than false negatives for a trust-based system.
- **Structured output for briefings**: the LLM picks entry IDs only; displayed text is templated from the DB. Eliminates cross-section ID misbinding by construction, not by prompt discipline.
- **Google Sheets as the visual layer, not a custom web UI**: users who already live in spreadsheets get a familiar surface. Editable tabs use a 3-way merge rather than last-write-wins — a Sheet edit and a DB update during the same sync window surface as a conflict instead of silently dropping one.
- **Build the capture, rent the intelligence**: build only what off-the-shelf AI can't do — capture surfaces, proactive triggers, persistent multilingual storage. Reasoning and query are rented from external LLMs via MCP.
- **One canonical store, multiple LLMs**: structured so the same knowledge base serves Claude, ChatGPT, and any future MCP-speaking tool as a shared source of truth. Avoids each assistant keeping its own divergent partial view.

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

- `app/` — bot, scheduler, LLM routing, MCP server, sync logic
- `scripts/` — operational scripts (sync to Sheets, config checks)
- `sql/` — schema migrations
- `CONFIG.yaml` — non-secret configuration (models, schedules, topic vocabulary)
- `docker-compose.yml` — PostgreSQL + pgvector
- `requirements.txt` — Python dependencies
- `BACKLOG.md` — shipped and in-progress work

## Status

Active personal project, public for transparency and reference. This repo is a sanitized snapshot of an actively-developed private repo; updates land in batches, not continuously.

Current direction: structuring the knowledge base so multiple LLMs (Claude, ChatGPT) share it as canonical state — each LLM keeps a small "hot cache" of native memory; OpenBrain holds the durable source of truth across all of them.

## License

MIT. See [LICENSE](LICENSE).
