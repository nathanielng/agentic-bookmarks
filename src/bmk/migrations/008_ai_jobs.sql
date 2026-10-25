-- 008: AI jobs (PLAN05 §11, CG6). Each AI run is a job in its own process, separate from the web server, so
-- it survives closing the UI. One job runs at a time; the rest wait as 'queued'. The runner updates
-- done/total and heartbeat_at as it goes; `control` is how the UI or CLI asks it to pause or cancel.
-- Its proposals go into changeset_id, which stays 'running' until the job ends.
CREATE TABLE ai_jobs (
  id            INTEGER PRIMARY KEY,
  kind          TEXT NOT NULL,          -- 'taxonomy' | 'categorize'
  state         TEXT NOT NULL DEFAULT 'queued'
                CHECK (state IN ('queued', 'running', 'paused', 'succeeded', 'succeeded_with_errors', 'failed', 'cancelled')),
  params_json   TEXT,
  author        TEXT NOT NULL,
  changeset_id  INTEGER REFERENCES changesets(id),
  pid           INTEGER,
  control       TEXT CHECK (control IN ('pause', 'cancel')),
  done          INTEGER NOT NULL DEFAULT 0,
  total         INTEGER,
  errors        INTEGER NOT NULL DEFAULT 0,
  -- For estimating how long a run takes (hardware-dependent): model input tokens, seconds inside the model,
  -- and wall-clock seconds while running (pauses excluded), plus the machine it ran on.
  tokens_in     INTEGER NOT NULL DEFAULT 0,
  model_seconds REAL NOT NULL DEFAULT 0,
  run_seconds   REAL NOT NULL DEFAULT 0,
  hardware      TEXT,
  message       TEXT,                   -- what it is doing now, or the outcome
  error         TEXT,                   -- why it failed (or the last per-item error)
  created_at    TEXT NOT NULL,
  started_at    TEXT,
  heartbeat_at  TEXT,
  finished_at   TEXT
);
CREATE INDEX idx_ai_jobs_state ON ai_jobs(state);
