-- 007: changesets (PLAN05 §11, CG6). Every AI or agent proposal is a changeset of items; nothing changes
-- until items are approved and the changeset is applied, in one transaction. Applied changesets can be
-- reverted. Categories gain ordering and timestamps for the taxonomy editor.
CREATE TABLE changesets (
  id             INTEGER PRIMARY KEY,
  kind           TEXT NOT NULL,          -- 'taxonomy' | 'categorize' | 'agent'
  author         TEXT NOT NULL,          -- 'ai' | 'mcp' | 'cli' | 'web'
  provider       TEXT,                   -- e.g. 'mlx', 'llama.cpp'
  model          TEXT,
  prompt_version TEXT,
  summary        TEXT,
  params_json    TEXT,
  status         TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('running', 'pending', 'applied', 'partial', 'rejected', 'reverted', 'failed')),
  created_at     TEXT NOT NULL,
  applied_at     TEXT,
  reverted_at    TEXT,
  error          TEXT
);

CREATE TABLE changeset_items (
  id              INTEGER PRIMARY KEY,
  changeset_id    INTEGER NOT NULL REFERENCES changesets(id),
  op              TEXT NOT NULL,         -- 'add_category' | 'set_category' | 'merge'
  bookmark_id     INTEGER REFERENCES bookmarks(id),
  target_id       INTEGER,               -- merge: the bookmark to keep
  old_value       TEXT,
  new_value       TEXT,
  confidence      REAL,
  rationale       TEXT,
  base_updated_at TEXT,
  decision        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (decision IN ('pending', 'approved', 'rejected', 'conflict', 'applied', 'reverted')),
  decided_at      TEXT,
  result          TEXT
);
CREATE INDEX idx_changeset_items_cs ON changeset_items(changeset_id);
CREATE INDEX idx_changeset_items_bookmark ON changeset_items(bookmark_id);

ALTER TABLE categories ADD COLUMN position INTEGER NOT NULL DEFAULT 0;
ALTER TABLE categories ADD COLUMN created_at TEXT;
CREATE INDEX idx_bookmarks_category ON bookmarks(category_id);
