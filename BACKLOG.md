# OpenBrain Roadmap

OpenBrain is a self-hosted personal knowledge system: a Telegram bot captures text and voice in multiple languages, a PostgreSQL database with pgvector stores entries with semantic search, and an MCP server exposes the memory to external AI tools. A daily briefing and weekly review run on schedule.

## Shipped

- Multilingual capture (4 languages) via Telegram bot
- PostgreSQL + pgvector storage with hybrid search (vector + keyword)
- Language-aware LLM routing for tagging and summarization
- Daily briefing with structured task selection
- Weekly review
- Google Sheets bidirectional sync as a visual layer
- MCP server exposing 6 tools (search, count, get by ID, recent by topic, recent by date, open tasks)
- Job observability: `job_runs` table + `/jobs` command for visibility into scheduled runs
- Two-pass task auto-detection
- Topic clustering v1 with closed vocabulary

## In Progress

- Remote MCP access with proper TLS
- MCP tool coverage audit

## Queued

- Tasks view in Google Sheets
- Apple Reminders sync
- Layered retrieval context (L0/L1/L2 tiers)
- Temporal fact supersession

## Principles

- Build the capture; rent the intelligence. Don't rebuild what external reasoning layers (Claude via MCP) already do well.
- Observability before complexity.
- Git before features.
- Ship lean, staged foundations before layering.
