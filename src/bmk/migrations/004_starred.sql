-- 004: starred bookmarks. A star marks a kept bookmark as a favourite (starring keeps it; moving it back
-- to the Inbox clears the star), giving a short prioritised list.
ALTER TABLE bookmarks ADD COLUMN starred INTEGER NOT NULL DEFAULT 0;
CREATE INDEX idx_bookmarks_starred ON bookmarks(starred) WHERE starred = 1;
