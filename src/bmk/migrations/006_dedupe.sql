-- 006: dedupe review (PLAN05 §7, CG5).
-- merges: every merge with a snapshot of both rows and their relations, so it can be undone.
-- not_duplicates: pairs the user said are different (never proposed again; also written by un-merge).
-- duplicate_candidates: tier 3–4 proposals from the last scan (derived; rebuilt by every scan).
CREATE TABLE merges (
  id            INTEGER PRIMARY KEY,
  survivor_id   INTEGER NOT NULL REFERENCES bookmarks(id),
  loser_id      INTEGER NOT NULL REFERENCES bookmarks(id),
  tier          INTEGER,
  reason        TEXT NOT NULL,
  snapshot_json TEXT NOT NULL,
  merged_at     TEXT NOT NULL,
  actor         TEXT,
  reverted_at   TEXT
);
CREATE INDEX idx_merges_survivor ON merges(survivor_id);
CREATE INDEX idx_merges_loser ON merges(loser_id);

CREATE TABLE not_duplicates (
  a_id       INTEGER NOT NULL REFERENCES bookmarks(id),
  b_id       INTEGER NOT NULL REFERENCES bookmarks(id),
  decided_at TEXT NOT NULL,
  reason     TEXT,
  PRIMARY KEY (a_id, b_id),
  CHECK (a_id < b_id)
);

CREATE TABLE duplicate_candidates (
  a_id       INTEGER NOT NULL REFERENCES bookmarks(id),
  b_id       INTEGER NOT NULL REFERENCES bookmarks(id),
  tier       INTEGER NOT NULL,
  confidence REAL NOT NULL,
  rationale  TEXT NOT NULL,
  found_at   TEXT NOT NULL,
  PRIMARY KEY (a_id, b_id),
  CHECK (a_id < b_id)
);

-- CG3's automatic (tier 2) merges kept their snapshot in a 'merged_into' event; list them as merges too.
-- Their snapshot has only the merged-away row, so undoing one restores that row and leaves the survivor as is.
INSERT INTO merges (survivor_id, loser_id, tier, reason, snapshot_json, merged_at, actor)
SELECT json_extract(data_json, '$.survivor'), bookmark_id, 2, COALESCE(json_extract(data_json, '$.reason'), 'merged'),
       json_object('legacy', 1, 'loser', json(json_extract(data_json, '$.snapshot'))), at, actor
FROM bookmark_events WHERE kind = 'merged_into' ORDER BY id;
