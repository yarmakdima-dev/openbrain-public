CREATE TABLE IF NOT EXISTS entry_relations (
    id BIGSERIAL PRIMARY KEY,
    from_entry_id BIGINT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    to_entry_id BIGINT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    relation_type TEXT NOT NULL CHECK (relation_type IN ('spawned_from', 'continues', 'contradicts')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT entry_relations_no_self CHECK (from_entry_id <> to_entry_id),
    CONSTRAINT entry_relations_unique_triple UNIQUE (from_entry_id, to_entry_id, relation_type)
);

CREATE INDEX IF NOT EXISTS idx_entry_relations_from
    ON entry_relations (from_entry_id);

CREATE INDEX IF NOT EXISTS idx_entry_relations_to
    ON entry_relations (to_entry_id);

