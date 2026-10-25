-- 005: files already imported, by content hash, so a folder re-scan (or the same file under another name)
-- is skipped instead of read again. `--force` re-reads a file.
CREATE TABLE ingested_files (
  id            INTEGER PRIMARY KEY,
  source_type   TEXT NOT NULL,
  sha256        TEXT NOT NULL,
  path          TEXT NOT NULL,
  size          INTEGER NOT NULL,
  import_run_id INTEGER REFERENCES import_runs(id),
  ingested_at   TEXT NOT NULL,
  UNIQUE (source_type, sha256)
);
