-- 002: triage statuses. A bookmark is either in the Inbox (not decided yet) or Kept; deleting
-- moves it to Trash (deleted_at). 'read' and 'archived' both become 'kept'.
-- SQLite cannot alter a CHECK constraint, so the table is rebuilt (sqlite.org/lang_altertable.html §7).
-- Row ids are unchanged, so foreign keys and the external-content FTS index stay valid; the runner
-- checks every foreign key before COMMIT.
-- bmk: foreign_keys=off

CREATE TABLE bookmarks_new (
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
  status               TEXT NOT NULL DEFAULT 'inbox' CHECK (status IN ('inbox', 'kept')),
  locked_fields        TEXT NOT NULL DEFAULT '[]',
  fetch_status         TEXT NOT NULL DEFAULT 'pending',
  fetch_error          TEXT,
  http_status          INTEGER,
  last_fetched_at      TEXT,
  metadata_stale       INTEGER NOT NULL DEFAULT 0,
  deleted_at           TEXT,
  merged_into_id       INTEGER REFERENCES bookmarks(id)
);

INSERT INTO bookmarks_new (id, url_original, url_resolved, url_canonical, url_loose, canonical_hint, title, title_source, description, site_name, domain, image_url, author, published_at, content_text, content_simhash, saved_at, saved_at_source, imported_at, updated_at, remarks, category_id, category_assigned_by, status, locked_fields, fetch_status, fetch_error, http_status, last_fetched_at, metadata_stale, deleted_at, merged_into_id)
SELECT id, url_original, url_resolved, url_canonical, url_loose, canonical_hint, title, title_source, description, site_name, domain, image_url, author, published_at, content_text, content_simhash, saved_at, saved_at_source, imported_at, updated_at, remarks, category_id, category_assigned_by, CASE status WHEN 'inbox' THEN 'inbox' ELSE 'kept' END, locked_fields, fetch_status, fetch_error, http_status, last_fetched_at, metadata_stale, deleted_at, merged_into_id
FROM bookmarks;

DROP TABLE bookmarks;
ALTER TABLE bookmarks_new RENAME TO bookmarks;

CREATE INDEX idx_bookmarks_url_loose ON bookmarks(url_loose);
CREATE INDEX idx_bookmarks_domain ON bookmarks(domain);
CREATE INDEX idx_bookmarks_saved_at ON bookmarks(saved_at);
CREATE INDEX idx_bookmarks_deleted_at ON bookmarks(deleted_at);

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

INSERT INTO bookmarks_fts(bookmarks_fts) VALUES ('rebuild');
