BEGIN;

-- A3 architecture: one snapshot table for all tab/row pairs.
-- tab_name disambiguates which sheet pushed the row (tasks, ideas, people, future tabs).
-- target_table indicates which table row_id refers to (entries or entities).
CREATE TABLE IF NOT EXISTS sheet_snapshots (
    tab_name      TEXT        NOT NULL,
    target_table  TEXT        NOT NULL,
    row_id        BIGINT      NOT NULL,
    snapshot      JSONB       NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tab_name, row_id)
);

CREATE INDEX IF NOT EXISTS idx_sheet_snapshots_lookup
    ON sheet_snapshots (tab_name, target_table, row_id);

-- Mirror entities.sync_error for entries-backed tabs.
ALTER TABLE entries
    ADD COLUMN IF NOT EXISTS sync_error TEXT;

COMMIT;
