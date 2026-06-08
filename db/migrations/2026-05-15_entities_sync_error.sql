ALTER TABLE entities ADD COLUMN sync_error TEXT;
COMMENT ON COLUMN entities.sync_error IS 'Last sync push error for this row; cleared on next successful push. Surfaced in People sheet error column.';
