-- 009: review log (PLAN05 §11, CG6). Every decision on an AI proposal, by a person or by the auto-approve
-- rule, so the classifier can be improved from it: calibrate the auto-approve threshold, find categories
-- whose descriptions confuse the model, and (with enough labels) fine-tune. Names are stored as text because
-- categories can be renamed or deleted. Personal data: lives only in the library database.
CREATE TABLE review_log (
  id             INTEGER PRIMARY KEY,
  at             TEXT NOT NULL,
  changeset_id   INTEGER NOT NULL,
  item_id        INTEGER,
  kind           TEXT NOT NULL,          -- changeset kind: 'taxonomy' | 'categorize' | 'agent'
  op             TEXT NOT NULL,
  bookmark_id    INTEGER,
  proposed       TEXT,                   -- what the AI proposed (category name)
  final          TEXT,                   -- what stands now (differs when a person corrected it)
  confidence     REAL,
  rationale      TEXT,                   -- the model's top choices with probabilities
  outcome        TEXT NOT NULL CHECK (outcome IN ('auto_approved', 'approved', 'corrected', 'rejected', 'corrected_after_apply')),
  decided_by     TEXT NOT NULL CHECK (decided_by IN ('auto', 'human')),
  model          TEXT,
  prompt_version TEXT
);
CREATE INDEX idx_review_log_item ON review_log(item_id);
CREATE INDEX idx_review_log_bookmark ON review_log(bookmark_id);
