# OpenBrain

OpenBrain is a self-hosted personal knowledge system. It captures thoughts from lightweight surfaces, stores them in PostgreSQL with semantic search, and exposes the memory to external AI tools through the Model Context Protocol (MCP).

The project is intentionally small in scope: OpenBrain stores durable memory and operational context; LLMs provide reasoning through tools.

## What It Does

- **Capture:** Telegram text and voice messages become entries. Voice notes are transcribed with Whisper, and capture remains available even if downstream AI services are unavailable.
- **Recognize:** Entries are typed, summarized in English, tagged, embedded for vector search, and assigned metadata such as language, topic, title, status, and people references.
- **Connect:** `parent_entry_id` models hierarchy, while `entry_relations` stores typed directional edges such as `spawned_from`, `continues`, and `contradicts`.
- **Retrieve:** MCP tools provide semantic search, hybrid search, filtered retrieval, topic summaries, counts, and entry lookup by ID.
- **Write:** MCP write tools can add entries, update entry content, and set entry status. These write paths are designed for user-confirmed assistant workflows.
- **Sync:** Google Sheets acts as a visual editing layer. Log and General tabs are push-only DB indexes; Projects, Books, Highlights, Tasks, Ideas, and People are editable tabs with 3-way merge conflict handling.

## Architecture

- **Database:** PostgreSQL with pgvector.
- **Bot:** Telegram capture service for text and voice.
- **MCP server:** FastMCP service exposing read and write tools to MCP-speaking clients.
- **Sheets sync:** per-tab sync modules with snapshots and `sync_error` conflict reporting.
- **LLM providers:** OpenAI, Anthropic, and Google clients are wired through environment variables.

The core design principle is one canonical store. Telegram, Sheets, scripts, and MCP all converge on the database rather than maintaining separate memories.

## Entry Model

Entries can represent highlights, books, people, ideas, tasks, projects, reviews, briefings, memory notes, or instructions. The schema is deliberately flexible: entry type is data, not a hard database enum.

Relationship modeling uses two layers:

- `parent_entry_id` for hierarchical capture, such as notes or tasks attached to a project.
- `entry_relations` for typed graph edges between entries.

This lets OpenBrain behave as a graph of memory rather than only a chronological log.

## Google Sheets Sync

The sync layer uses a snapshot-based 3-way merge:

- Sheet unchanged, DB unchanged: no-op.
- Sheet changed, DB unchanged: sheet edit wins.
- Sheet unchanged, DB changed: DB edit wins and the next push updates the sheet.
- Both changed to the same value: accept consensus.
- Both changed differently: skip the row and write a conflict message to `sync_error`.

Conflict messages are surfaced in the Sheet as a visible problem column.

## MCP Tools

The MCP server exposes read tools for search and retrieval, plus write tools for controlled maintenance:

- `add_entry`
- `update_entry`
- `set_status`
- `search_memory`
- `hybrid_search`
- `get_entry_by_id`
- `search_by_type`
- `search_by_who`
- `recent_entries`
- `recent_by_topic`
- `get_summary`
- `list_topics_with_counts`
- `count_entries`

Public deployments should put the MCP service behind HTTPS and authentication. This repository keeps deployment-specific hostnames, tokens, credentials, and runtime paths out of source control.

## Repository Layout

- `app/` — bot, MCP server, sync modules, LLM clients, entity resolution, and relation helpers.
- `sql/` — base schema.
- `migrations/` and `db/migrations/` — incremental schema changes.
- `scripts/` — maintenance and backfill scripts.
- `CONFIG.yaml` — non-secret runtime configuration.
- `docker-compose.yml` — local database service definition.

## Running Locally

This repository is a sanitized public snapshot. To run it, provide your own environment variables and service-account credentials.

Typical requirements:

1. PostgreSQL with pgvector.
2. Python dependencies from `requirements.txt`.
3. API keys for whichever LLM providers you enable.
4. Telegram bot credentials if using Telegram capture.
5. Google Sheets credentials if using sheet sync.
6. TLS/authentication configuration if exposing MCP outside localhost.

Do not commit `.env` files, service-account JSON, private keys, database dumps, logs, or host-specific operational files.

