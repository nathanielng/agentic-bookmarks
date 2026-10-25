-- 003 (CG3): enrichment job queue (PLAN05 §6).
-- One 'enrich' job per bookmark: pending → leased → done, or retry_wait (backoff) → … → dead.
-- bookmarks.fetch_status mirrors the outcome for the UI:
--   pending | fetched | partial | skipped | needs_browser | failed
-- A lease that expires (worker crashed) makes the job available again.

CREATE TABLE processing_jobs (
  id           INTEGER PRIMARY KEY,
  bookmark_id  INTEGER NOT NULL REFERENCES bookmarks(id),
  kind         TEXT NOT NULL DEFAULT 'enrich',
  state        TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'leased', 'retry_wait', 'done', 'dead')),
  attempts     INTEGER NOT NULL DEFAULT 0,
  next_run_at  TEXT NOT NULL,
  lease_until  TEXT,
  last_error   TEXT,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL,
  UNIQUE (bookmark_id, kind)
);
CREATE INDEX idx_processing_jobs_due ON processing_jobs(state, next_run_at);

-- Whether the site allows being shown in a frame (X-Frame-Options / CSP frame-ancestors):
-- NULL = unknown (not fetched), 0 = refuses, 1 = allows. Drives the UI's live preview.
ALTER TABLE bookmarks ADD COLUMN frame_ok INTEGER;
CREATE INDEX idx_bookmarks_fetch_status ON bookmarks(fetch_status);
