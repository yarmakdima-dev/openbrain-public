ALTER TABLE entries
ADD COLUMN IF NOT EXISTS embedding vector(1536);
