-- 001: core library, provenance, import audit, taxonomy, events, FTS.
-- Later tables (processing_jobs, merges, changesets) arrive in their own migrations (PLAN05 §3).

CREATE TABLE categories (
  id          INTEGER PRIMARY KEY,
  name        TEXT NOT NULL UNIQUE,
  parent_id   INTEGER REFERENCES categories(id),
  description TEXT,
  created_by  TEXT                                   -- 'user' | 'ai'
);

CREATE TABLE bookmarks (
  id                   INTEGER PRIMARY KEY,
  url_original         TEXT NOT NULL,
  url_resolved         TEXT,
  url_canonical        TEXT NOT NULL UNIQUE,
  url_loose            TEXT,
  canonical_hint       TEXT,
  title                TEXT,
  title_source         TEXT,                         -- 'source' | 'html' | 'oembed' | 'browser' | 'user'
  description          TEXT,
  site_name            TEXT,
  domain               TEXT,
  image_url            TEXT,
  author               TEXT,
  published_at         TEXT,
  content_text         TEXT,
  content_simhash      TEXT,
  saved_at             TEXT,
  saved_at_source      TEXT NOT NULL DEFAULT 'unknown',
  imported_at          TEXT NOT NULL,
  updated_at           TEXT NOT NULL,
  remarks              TEXT,
  category_id          INTEGER REFERENCES categories(id),
  category_assigned_by TEXT,
  status               TEXT NOT NULL DEFAULT 'inbox' CHECK (status IN ('inbox', 'read', 'archived')),
  locked_fields        TEXT NOT NULL DEFAULT '[]',
  fetch_status         TEXT NOT NULL DEFAULT 'pending',
  fetch_error          TEXT,
  http_status          INTEGER,
  last_fetched_at      TEXT,
  metadata_stale       INTEGER NOT NULL DEFAULT 0,
  deleted_at           TEXT,
  merged_into_id       INTEGER REFERENCES bookmarks(id)
);
CREATE INDEX idx_bookmarks_url_loose ON bookmarks(url_loose);
CREATE INDEX idx_bookmarks_domain ON bookmarks(domain);
CREATE INDEX idx_bookmarks_saved_at ON bookmarks(saved_at);
CREATE INDEX idx_bookmarks_deleted_at ON bookmarks(deleted_at);

CREATE TABLE url_aliases (
  url_canonical TEXT PRIMARY KEY,
  bookmark_id   INTEGER NOT NULL REFERENCES bookmarks(id),
  kind          TEXT NOT NULL                       -- 'canonical' | 'original' | 'short' | 'redirect' | 'variant' | 'merged' | 'canonical_hint'
);
CREATE INDEX idx_url_aliases_bookmark ON url_aliases(bookmark_id);

CREATE TABLE import_runs (
  id                 INTEGER PRIMARY KEY,
  source_type        TEXT NOT NULL,
  params             TEXT,
  started_at         TEXT NOT NULL,
  finished_at        TEXT,
  items_seen         INTEGER NOT NULL DEFAULT 0,
  items_new          INTEGER NOT NULL DEFAULT 0,
  items_matched      INTEGER NOT NULL DEFAULT 0,
  items_unchanged    INTEGER NOT NULL DEFAULT 0,
  items_skipped      INTEGER NOT NULL DEFAULT 0,
  items_invalid      INTEGER NOT NULL DEFAULT 0,
  items_suppressed   INTEGER NOT NULL DEFAULT 0,
  error              TEXT
);

CREATE TABLE import_items (
  id            INTEGER PRIMARY KEY,
  import_run_id INTEGER NOT NULL REFERENCES import_runs(id),
  locator       TEXT,
  raw_url       TEXT,
  raw_title     TEXT,
  raw_timestamp TEXT,
  outcome       TEXT NOT NULL,   -- 'new' | 'matched_exact' | 'matched_alias' | 'unchanged' | 'invalid' | 'skipped' | 'suppressed_deleted' | 'suppressed_resaved'
  bookmark_id   INTEGER REFERENCES bookmarks(id),
  error         TEXT
);
CREATE INDEX idx_import_items_run ON import_items(import_run_id);

CREATE TABLE sources (
  id            INTEGER PRIMARY KEY,
  bookmark_id   INTEGER NOT NULL REFERENCES bookmarks(id),
  source_type   TEXT NOT NULL,
  source_ref    TEXT NOT NULL DEFAULT '',
  locator       TEXT NOT NULL DEFAULT '',
  raw_url       TEXT NOT NULL,
  raw_title     TEXT,
  event_at      TEXT,
  first_seen_at TEXT NOT NULL,
  last_seen_at  TEXT NOT NULL,
  import_run_id INTEGER REFERENCES import_runs(id),
  UNIQUE (source_type, source_ref, locator, raw_url)
);
CREATE INDEX idx_sources_bookmark ON sources(bookmark_id);

CREATE TABLE tags (
  id   INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE
);

CREATE TABLE bookmark_tags (
  bookmark_id INTEGER NOT NULL REFERENCES bookmarks(id),
  tag_id      INTEGER NOT NULL REFERENCES tags(id),
  assigned_by TEXT NOT NULL DEFAULT 'user',          -- 'user' | 'ai' | 'rule'
  PRIMARY KEY (bookmark_id, tag_id)
);

CREATE TABLE bookmark_events (
  id          INTEGER PRIMARY KEY,
  bookmark_id INTEGER NOT NULL REFERENCES bookmarks(id),
  at          TEXT NOT NULL,
  actor       TEXT NOT NULL,                         -- 'user' | 'cli' | 'api' | 'mcp' | 'system'
  kind        TEXT NOT NULL,                         -- 'edit' | 'delete' | 'restore' | 'merge' | ...
  data_json   TEXT
);
CREATE INDEX idx_bookmark_events_bookmark ON bookmark_events(bookmark_id);

CREATE VIRTUAL TABLE bookmarks_fts USING fts5(
  title, description, content_text, remarks, url_canonical,
  content='bookmarks', content_rowid='id'
);

CREATE TRIGGER bookmarks_fts_ai AFTER INSERT ON bookmarks BEGIN
  INSERT INTO bookmarks_fts(rowid, title, description, content_text, remarks, url_canonical)
  VALUES (new.id, new.title, new.description, new.content_text, new.remarks, new.url_canonical);
END;

CREATE TRIGGER bookmarks_fts_ad AFTER DELETE ON bookmarks BEGIN
  INSERT INTO bookmarks_fts(bookmarks_fts, rowid, title, description, content_text, remarks, url_canonical)
  VALUES ('delete', old.id, old.title, old.description, old.content_text, old.remarks, old.url_canonical);
END;

CREATE TRIGGER bookmarks_fts_au AFTER UPDATE OF title, description, content_text, remarks, url_canonical ON bookmarks BEGIN
  INSERT INTO bookmarks_fts(bookmarks_fts, rowid, title, description, content_text, remarks, url_canonical)
  VALUES ('delete', old.id, old.title, old.description, old.content_text, old.remarks, old.url_canonical);
  INSERT INTO bookmarks_fts(rowid, title, description, content_text, remarks, url_canonical)
  VALUES (new.id, new.title, new.description, new.content_text, new.remarks, new.url_canonical);
END;
