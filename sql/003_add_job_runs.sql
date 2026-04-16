CREATE TABLE IF NOT EXISTS job_runs (
    id SERIAL PRIMARY KEY,
    job_name TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL CHECK (status IN ('running','done','failed')),
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_job_runs_job_name_started
    ON job_runs (job_name, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_job_runs_status
    ON job_runs (status);
