# OpenBrain Roadmap

OpenBrain is a self-hosted personal knowledge system: a Telegram bot captures text and voice in multiple languages, a PostgreSQL database with pgvector stores entries with semantic search, and an MCP server exposes the memory to external AI tools. A daily briefing and weekly review run on schedule.

## Shipped

- Multilingual capture (4 languages) via Telegram bot
- PostgreSQL + pgvector storage with hybrid search (vector + keyword)
- Language-aware LLM routing for tagging and summarization
- Daily briefing with structured task selection
- Weekly review
- Google Sheets bidirectional sync as a visual layer
- MCP server exposing 11 tools for semantic search, hybrid search, filtered retrieval, counts, topic summaries, and add-entry writes
- Job observability: `job_runs` table + `/jobs` command for visibility into scheduled runs
- Two-pass task auto-detection
- Topic clustering v1 with closed vocabulary
- Public MCP access with token-based protection
- Modern Ubuntu deployment with persistent swap
- Capture trust fix for explicit labels like `Idea:` / `Task:`
- Metadata quality fix for `who` and entry-language titles

## In Progress

- Nothing actively deploying right now.

## Queued

- Topic backfill for historical entries
- Tasks view in Google Sheets
- Transcript compaction for weekly review
- End-to-end parent-link verification
- Weekly review scheduling improvement for DST safety
- OS upgrade follow-up when the normal upgrader offers it
- Public repo sync workflow
- Apple Reminders sync
- Layered retrieval context (L0/L1/L2 tiers)
- Temporal fact supersession

## Principles

- Build the capture; rent the intelligence. Don't rebuild what external reasoning layers (Claude via MCP) already do well.
- Observability before complexity.
- Git before features.
- Ship lean, staged foundations before layering.
