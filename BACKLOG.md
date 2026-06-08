# Changelog and Roadmap

This is a curated public changelog for the sanitized OpenBrain snapshot. It summarizes shipped architecture and product work without exposing private infrastructure, secrets, operational incidents, or personal data.

## Shipped

### April 2026 - Public baseline

- Initial sanitized public snapshot.
- Telegram capture, PostgreSQL/pgvector storage, semantic search, and basic MCP read access.
- MIT license and public README.

### May 2026 - Entry model and retrieval

- Expanded entry model with typed entries such as projects, tasks, ideas, highlights, books, people, reviews, briefings, memory notes, and instructions.
- Added parent-child linkage between entries.
- Added English titles/summaries and improved multilingual capture/search behavior.
- Improved topic clustering and retrieval quality.

### May 2026 - Google Sheets editing layer

- Reworked Google Sheets sync into a visual editing layer.
- Added editable per-type tabs: Projects, Books, Highlights, Tasks, Ideas, People.
- Kept Log/General views as push-only indexes.
- Added conflict-aware sync support and sync-error visibility.

### June 2026 - Graph layer and MCP write tools

- Added typed entry relations for graph-like links between entries.
- Added MCP write tools for controlled LLM-mediated entry creation, updates, and status changes.
- Added task spawning from ideas and relation-aware workflows.
- Added public-safe schema migrations and updated app modules.

### June 2026 - Public repo hardening

- Synced the public repo to current sanitized dev state.
- Added CI security workflow covering secret scanning, static analysis, dependency audit, and lint checks.
- Verified public snapshot is free of secrets and private infrastructure details.

## Roadmap

### Near term

- Automated public-repo sync workflow with explicit sanitization gates.
- Better retrieval tiers for recent, semantic, and project-linked context.
- Temporal supersession: tracking when newer entries replace or refine older ones.
- Apple Reminders or task-system integration.
- More robust public documentation and examples.

### Later

- Richer graph exploration over entry relations.
- Safer autonomous proposal flows, still gated by Dima confirmation.
- More connectors while keeping OpenBrain as the canonical memory store.

## Not included in the public changelog

This public changelog intentionally excludes:

- Private infrastructure details.
- Secrets, auth paths, hostnames, IPs, accounts, credentials.
- Operational incidents.
- Personal/entity cleanup notes.
- Internal entry IDs when they refer to private data.
