"""Changesets (PLAN05 §11): every AI or agent proposal is a set of items that change nothing until approved.

Ops:
- `add_category`: new_value = {"name", "description"} (a proposed category list is one changeset of these).
- `set_category`: bookmark_id, old_value / new_value = category ids (or null).
- `merge`: bookmark_id merged into target_id (proposed through MCP).

Applying runs the approved items in one transaction. An item whose bookmark changed since the proposal
(its category is no longer the old value, it was assigned by the user, it was deleted or merged) becomes a
`conflict` instead. Applied changesets can be reverted: items still in their applied state go back.
"""

import json
from typing import Any

from . import db as dbmod
from .service import Conflict, Library, NotFound
from .timeutil import now_utc

OPS = ("add_category", "set_category", "merge")


def _dumps(value: Any) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False)


def _loads(value: str | None) -> Any:
    return None if value is None else json.loads(value)


def create(lib: Library, kind: str, *, author: str, provider: str | None = None, model: str | None = None,
           prompt_version: str | None = None, summary: str | None = None, params: dict | None = None,
           status: str = "pending") -> int:  # fmt: skip
    cur = lib.conn.execute(
        "INSERT INTO changesets (kind, author, provider, model, prompt_version, summary, params_json, status, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (kind, author, provider, model, prompt_version, summary, _dumps(params), status, now_utc()),
    )
    return cur.lastrowid


def add_item(lib: Library, changeset_id: int, op: str, *, bookmark_id: int | None = None, target_id: int | None = None,
             old_value: Any = None, new_value: Any = None, confidence: float | None = None, rationale: str | None = None) -> int:  # fmt: skip
    if op not in OPS:
        raise ValueError(f"op must be one of {', '.join(OPS)}")
    base = None
    if bookmark_id is not None:
        row = lib.conn.execute("SELECT updated_at FROM bookmarks WHERE id=?", (bookmark_id,)).fetchone()
        if row is None:
            raise NotFound(f"bookmark {bookmark_id} not found")
        base = row["updated_at"]
    cur = lib.conn.execute(
        "INSERT INTO changeset_items (changeset_id, op, bookmark_id, target_id, old_value, new_value, confidence, rationale, base_updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (changeset_id, op, bookmark_id, target_id, _dumps(old_value), _dumps(new_value), confidence, rationale, base),
    )
    return cur.lastrowid


def set_status(lib: Library, changeset_id: int, status: str, error: str | None = None, summary: str | None = None) -> None:
    lib.conn.execute(
        "UPDATE changesets SET status=?, error=COALESCE(?, error), summary=COALESCE(?, summary) WHERE id=?",
        (status, error, summary, changeset_id),
    )


def _counts(lib: Library, changeset_id: int) -> dict[str, int]:
    return dict(lib.conn.execute("SELECT decision, COUNT(*) FROM changeset_items WHERE changeset_id=? GROUP BY 1", (changeset_id,)).fetchall())


def list_(lib: Library, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    sql, args = "SELECT * FROM changesets", []
    if status:
        sql += " WHERE status=?"
        args.append(status)
    out = []
    for r in lib.conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall():
        item = dict(r)
        item["params"] = _loads(item.pop("params_json"))
        item["counts"] = _counts(lib, r["id"])
        out.append(item)
    return out


def get(lib: Library, changeset_id: int) -> dict[str, Any]:
    row = lib.conn.execute("SELECT * FROM changesets WHERE id=?", (changeset_id,)).fetchone()
    if row is None:
        raise NotFound(f"changeset {changeset_id} not found")
    cs = dict(row)
    cs["params"] = _loads(cs.pop("params_json"))
    cs["counts"] = _counts(lib, changeset_id)
    names = {r[0]: r[1] for r in lib.conn.execute("SELECT id, name FROM categories")}
    items = []
    for r in lib.conn.execute(
        "SELECT i.*, COALESCE(b.url_resolved, b.url_original) AS url, b.title, b.domain, b.category_id AS current_category"
        " FROM changeset_items i LEFT JOIN bookmarks b ON b.id = i.bookmark_id WHERE i.changeset_id=?"
        " ORDER BY i.confidence IS NULL, i.confidence, i.id",
        (changeset_id,),
    ):
        item = dict(r)
        item["old_value"], item["new_value"] = _loads(item["old_value"]), _loads(item["new_value"])
        if item["op"] == "set_category":
            item["old_name"] = names.get(item["old_value"])
            item["new_name"] = names.get(item["new_value"])
        items.append(item)
    cs["items"] = items
    return cs


def _name(lib: Library, op: str, value: Any) -> str | None:
    """A category's name for the log: `value` is a category id (set_category) or {name, description} (add_category)."""
    if op == "add_category":
        return (value or {}).get("name")
    if op == "merge":
        return None
    row = lib.conn.execute("SELECT name FROM categories WHERE id=?", (value,)).fetchone() if value is not None else None
    return row[0] if row else None


def _log(lib: Library, cs: dict[str, Any], item: dict[str, Any], outcome: str, by: str, final: Any = None, now: str | None = None) -> None:
    """Record one decision. `item` is the row as proposed; `final` is the value that stands (default: as proposed)."""
    value = item["new_value"] if final is None else final
    lib.conn.execute(
        "INSERT INTO review_log (at, changeset_id, item_id, kind, op, bookmark_id, proposed, final, confidence, rationale, outcome,"
        " decided_by, model, prompt_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (now or now_utc(), item["changeset_id"], item["id"], cs["kind"], item["op"], item["bookmark_id"], _name(lib, item["op"], item["new_value"]),
         _name(lib, item["op"], value), item["confidence"], item["rationale"], outcome, by, cs["model"], cs["prompt_version"]),
    )  # fmt: skip


def decide(lib: Library, changeset_id: int, decision: str, item_ids: list[int] | None = None,
           min_confidence: float | None = None, new_values: dict[int, Any] | None = None, by: str = "human") -> int:  # fmt: skip
    """Approve or reject pending items: the listed ones, or every one at or above `min_confidence`, or all.
    `new_values` overrides an item's proposed value before approving (e.g. a different category).
    Each decision is written to the review log (`by` is 'human', or 'auto' for the auto-approve rule)."""
    if decision not in ("approved", "rejected", "pending"):
        raise ValueError("decision must be approved, rejected or pending")
    cs = lib.conn.execute("SELECT status, kind, model, prompt_version FROM changesets WHERE id=?", (changeset_id,)).fetchone()
    if cs is None:
        raise NotFound(f"changeset {changeset_id} not found")
    if cs["status"] not in ("pending", "partial"):
        raise Conflict(f"changeset {changeset_id} is {cs['status']}")
    where, args = ["changeset_id=?", "decision IN ('pending', 'approved', 'rejected', 'conflict')"], [changeset_id]
    if item_ids is not None:
        where.append(f"id IN ({','.join('?' * len(item_ids))})" if item_ids else "0")
        args += item_ids
    if min_confidence is not None:
        where.append("confidence >= ?")
        args.append(min_confidence)
    if by == "auto":  # the rule never overrides a person's decision
        where.append("decision = 'pending'")
    now = now_utc()
    with dbmod.transaction(lib.conn):
        touched = []
        if decision != "pending":  # the rows as proposed, before any override
            for r in lib.conn.execute(f"SELECT * FROM changeset_items WHERE {' AND '.join(where)}", args).fetchall():
                item = dict(r)
                item["new_value"] = _loads(item["new_value"])
                touched.append(item)
        for item_id, value in (new_values or {}).items():
            lib.conn.execute("UPDATE changeset_items SET new_value=? WHERE id=? AND changeset_id=?", (_dumps(value), item_id, changeset_id))
        cur = lib.conn.execute(f"UPDATE changeset_items SET decision=?, decided_at=? WHERE {' AND '.join(where)}", (decision, now, *args))
        for item in touched:
            final = (new_values or {}).get(item["id"])
            changed = final is not None and final != item["new_value"]
            outcome = "rejected" if decision == "rejected" else "corrected" if changed else "auto_approved" if by == "auto" else "approved"
            _log(lib, cs, item, outcome, by, final, now)
    return cur.rowcount


def trash_item(lib: Library, changeset_id: int, item_id: int) -> int:
    """The person doesn't want this bookmark at all: move it to Trash (restorable) and drop its proposal. Not logged
    as a review decision, since it says nothing about the classifier."""
    item = lib.conn.execute("SELECT bookmark_id, decision FROM changeset_items WHERE id=? AND changeset_id=?", (item_id, changeset_id)).fetchone()
    if item is None or item["bookmark_id"] is None:
        raise NotFound(f"item {item_id} not found in changeset {changeset_id}")
    if item["decision"] in ("applied", "reverted"):
        raise Conflict("that proposal was already applied; move the bookmark to Trash from the list instead")
    lib.delete(item["bookmark_id"])
    lib.conn.execute("UPDATE changeset_items SET decision='rejected', decided_at=?, result='moved to Trash' WHERE id=?", (now_utc(), item_id))
    return item["bookmark_id"]


def auto_apply(lib: Library, changeset_id: int, min_confidence: float) -> dict[str, int]:
    """Approve every pending item at or above `min_confidence` (logged as auto) and apply them; the rest
    stay pending for a person. Returns {approved, applied, conflicts, left}."""
    approved = decide(lib, changeset_id, "approved", min_confidence=min_confidence, by="auto") if min_confidence is not None else 0
    result = apply(lib, changeset_id) if approved else {"applied": 0, "conflicts": 0, "left": 0}
    return {"approved": approved, **result}


def log_correction(lib: Library, bookmark_id: int, old_category: int | None, new_category: int | None) -> None:
    """A person changed a category the AI had applied: the strongest sign the proposal was wrong."""
    row = lib.conn.execute(
        "SELECT i.*, c.kind, c.model, c.prompt_version FROM changeset_items i JOIN changesets c ON c.id = i.changeset_id"
        " WHERE i.bookmark_id=? AND i.op='set_category' AND i.decision='applied' AND i.new_value=? ORDER BY i.id DESC LIMIT 1",
        (bookmark_id, _dumps(old_category)),
    ).fetchone()
    if row is None:
        return
    item = dict(row)
    item["new_value"] = _loads(item["new_value"])
    _log(lib, item, item, "corrected_after_apply", "human", new_category)


def review_stats(lib: Library, kind: str = "categorize") -> dict[str, Any]:
    """How the AI did, from the latest decision per item and corrections after applying: accuracy by confidence
    band, which categories get corrected into which, and whether the auto-approve threshold holds."""
    rows = [dict(r) for r in lib.conn.execute(
        "SELECT * FROM review_log WHERE kind=? AND id IN (SELECT MAX(id) FROM review_log WHERE kind=? AND item_id IS NOT NULL"
        " AND outcome != 'corrected_after_apply' GROUP BY item_id) ORDER BY id", (kind, kind)).fetchall()]  # fmt: skip
    fixed = {r["item_id"]: r for r in lib.conn.execute("SELECT * FROM review_log WHERE outcome='corrected_after_apply'")}
    bands = [(0.9, 1.01, "90-100%"), (0.8, 0.9, "80-90%"), (0.6, 0.8, "60-80%"), (0.0, 0.6, "under 60%")]
    out_bands = []
    for lo, hi, label in bands:
        group = [r for r in rows if r["confidence"] is not None and lo <= r["confidence"] < hi]
        wrong = [r for r in group if r["outcome"] in ("corrected", "rejected") or r["item_id"] in fixed]
        out_bands.append({"band": label, "decided": len(group), "auto": sum(r["decided_by"] == "auto" for r in group),
                          "wrong": len(wrong), "accuracy": round(1 - len(wrong) / len(group), 3) if group else None})  # fmt: skip
    confusions: dict[tuple[str, str], int] = {}
    for r in rows:
        final = fixed[r["item_id"]]["final"] if r["item_id"] in fixed else r["final"]
        if r["outcome"] == "corrected" or r["item_id"] in fixed:
            confusions[(r["proposed"], final)] = confusions.get((r["proposed"], final), 0) + 1
        elif r["outcome"] == "rejected":
            confusions[(r["proposed"], "(none)")] = confusions.get((r["proposed"], "(none)"), 0) + 1
    return {"decisions": len(rows), "bands": out_bands,
            "confusions": [{"proposed": a, "final": b, "count": n} for (a, b), n in sorted(confusions.items(), key=lambda kv: -kv[1])]}  # fmt: skip


def export_labels(lib: Library, kind: str = "categorize") -> list[dict[str, Any]]:
    """Labelled examples for tuning: the bookmark's text, what the AI proposed and what a person settled on.
    Contains your bookmarks' content: write it only where you intend to (never into the repository)."""
    out = []
    for r in lib.conn.execute(
        "SELECT l.*, b.title, b.domain, b.description, COALESCE(b.url_resolved, b.url_original) AS url,"
        " (SELECT final FROM review_log f WHERE f.item_id = l.item_id AND f.outcome = 'corrected_after_apply' ORDER BY f.id DESC LIMIT 1) AS later"
        " FROM review_log l JOIN bookmarks b ON b.id = l.bookmark_id WHERE l.kind=? AND l.decided_by='human' OR (l.kind=? AND l.outcome='corrected_after_apply')"
        " ORDER BY l.id", (kind, kind)).fetchall():  # fmt: skip
        label = r["later"] or r["final"]
        out.append({"title": r["title"], "domain": r["domain"], "url": r["url"], "summary": r["description"], "proposed": r["proposed"],
                    "confidence": r["confidence"], "label": None if r["outcome"] == "rejected" else label, "outcome": r["outcome"]})  # fmt: skip
    return out


def _conflict(lib: Library, item: dict[str, Any]) -> str | None:
    if item["op"] == "add_category":
        name = (item["new_value"] or {}).get("name", "")
        return f"category '{name}' already exists" if lib.category_by_name(name) else None
    b = lib.conn.execute("SELECT * FROM bookmarks WHERE id=?", (item["bookmark_id"],)).fetchone()
    if b is None or b["deleted_at"] or b["merged_into_id"]:
        return "the bookmark was deleted or merged"
    if item["op"] == "set_category":
        if b["category_assigned_by"] == "user":
            return "you set this bookmark's category yourself"
        if b["category_id"] != item["old_value"]:
            return "the category changed since this was proposed"
        if item["new_value"] is not None and lib.conn.execute("SELECT 1 FROM categories WHERE id=?", (item["new_value"],)).fetchone() is None:
            return "the category no longer exists"
    if item["op"] == "merge":
        t = lib.conn.execute("SELECT deleted_at, merged_into_id FROM bookmarks WHERE id=?", (item["target_id"],)).fetchone()
        if t is None or t["merged_into_id"]:
            return "the bookmark to keep was merged"
    return None


def apply(lib: Library, changeset_id: int) -> dict[str, int]:
    """Apply every approved item in one transaction; items that no longer fit become conflicts."""
    cs = get(lib, changeset_id)
    if cs["status"] not in ("pending", "partial"):
        raise Conflict(f"changeset {changeset_id} is {cs['status']}")
    now = now_utc()
    applied = conflicts = 0
    with dbmod.transaction(lib.conn):
        for item in cs["items"]:
            if item["decision"] != "approved":
                continue
            if reason := _conflict(lib, item):
                lib.conn.execute("UPDATE changeset_items SET decision='conflict', result=? WHERE id=?", (reason, item["id"]))
                conflicts += 1
                continue
            result = None
            if item["op"] == "add_category":
                value = item["new_value"]
                position = lib.conn.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM categories").fetchone()[0]
                cur = lib.conn.execute(
                    "INSERT INTO categories (name, description, created_by, position, created_at) VALUES (?, ?, 'ai', ?, ?)",
                    (value["name"], value.get("description"), position, now),
                )
                result = json.dumps({"category_id": cur.lastrowid})
            elif item["op"] == "set_category":
                lib.conn.execute(
                    "UPDATE bookmarks SET category_id=?, category_assigned_by=? WHERE id=?",
                    (item["new_value"], "ai" if item["new_value"] is not None else None, item["bookmark_id"]),
                )
                lib._event(item["bookmark_id"], "category", {"from": item["old_value"], "to": item["new_value"], "changeset": changeset_id}, now)
            elif item["op"] == "merge":
                survivor = lib.merge(item["target_id"], item["bookmark_id"], reason=item["rationale"] or "proposed merge", now=now, tier=None, keep=item["target_id"])
                merge_id = lib.conn.execute("SELECT max(id) FROM merges WHERE loser_id=?", (item["bookmark_id"],)).fetchone()[0]
                result = json.dumps({"survivor": survivor, "merge_id": merge_id})
            lib.conn.execute("UPDATE changeset_items SET decision='applied', result=? WHERE id=?", (result, item["id"]))
            applied += 1
        left = lib.conn.execute(
            "SELECT COUNT(*) FROM changeset_items WHERE changeset_id=? AND decision IN ('pending', 'approved', 'conflict')", (changeset_id,)
        ).fetchone()[0]
        status = "partial" if left else ("applied" if applied or cs["counts"].get("applied") else "rejected")
        lib.conn.execute("UPDATE changesets SET status=?, applied_at=? WHERE id=?", (status, now, changeset_id))
    if any(i["op"] == "add_category" for i in cs["items"]):
        lib.save_categories_file()
    return {"applied": applied, "conflicts": conflicts, "left": left}


def revert(lib: Library, changeset_id: int) -> dict[str, int]:
    """Undo an applied changeset: categories go back where unchanged since, added categories are removed
    if nothing uses them, merges are undone. Items changed since are left alone and counted as skipped."""
    cs = get(lib, changeset_id)
    if cs["status"] not in ("applied", "partial"):
        raise Conflict(f"changeset {changeset_id} is {cs['status']}; only applied changesets can be reverted")
    now = now_utc()
    reverted = skipped = 0
    merges = []
    with dbmod.transaction(lib.conn):
        for item in reversed(cs["items"]):
            if item["decision"] != "applied":
                continue
            result = _loads(item["result"]) or {}
            ok = True
            if item["op"] == "set_category":
                b = lib.conn.execute("SELECT category_id, category_assigned_by FROM bookmarks WHERE id=?", (item["bookmark_id"],)).fetchone()
                ok = b is not None and b["category_id"] == item["new_value"] and b["category_assigned_by"] != "user"
                if ok:
                    lib.conn.execute(
                        "UPDATE bookmarks SET category_id=?, category_assigned_by=? WHERE id=?",
                        (item["old_value"], "ai" if item["old_value"] is not None else None, item["bookmark_id"]),
                    )
                    lib._event(item["bookmark_id"], "category", {"from": item["new_value"], "to": item["old_value"], "reverted": changeset_id}, now)
            elif item["op"] == "add_category":
                cid = result.get("category_id")
                ok = cid is not None and not lib.conn.execute("SELECT 1 FROM bookmarks WHERE category_id=?", (cid,)).fetchone()
                if ok:
                    lib.conn.execute("DELETE FROM categories WHERE id=?", (cid,))
            elif item["op"] == "merge":
                merges.append((item["id"], result.get("merge_id")))
                continue
            if ok:
                lib.conn.execute("UPDATE changeset_items SET decision='reverted' WHERE id=?", (item["id"],))
                reverted += 1
            else:
                skipped += 1
        lib.conn.execute("UPDATE changesets SET status='reverted', reverted_at=? WHERE id=?", (now, changeset_id))
    if any(i["op"] == "add_category" for i in cs["items"]):
        lib.save_categories_file()
    for item_id, merge_id in merges:  # unmerge has its own transaction
        try:
            lib.unmerge(merge_id)
            lib.conn.execute("UPDATE changeset_items SET decision='reverted' WHERE id=?", (item_id,))
            reverted += 1
        except (Conflict, NotFound):
            skipped += 1
    return {"reverted": reverted, "skipped": skipped}
