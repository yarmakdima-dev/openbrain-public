ALTER TABLE entries ADD COLUMN IF NOT EXISTS topic TEXT;
CREATE INDEX IF NOT EXISTS entries_topic_idx ON entries(topic);
