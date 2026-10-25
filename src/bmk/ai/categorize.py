"""Pass 2: the decision model assigns each bookmark one of the fixed categories (PLAN03 §11.2).

The question is a choice over the categories plus 'other'; the answer is a calibrated probability per
category, so the confidence is meaningful and the review can approve above a threshold. Bookmarks whose
category you set are never proposed; 'other' leaves a bookmark uncategorised.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass

from .. import changesets
from ..service import Library

PROMPT_VERSION = "categorize-v1"
OTHER = "other"
INSTRUCTIONS = "Which category best fits this bookmarked web page?"


def state_text(row) -> str:
    parts = [f"Title: {row['title'] or '(none)'}", f"Site: {row['domain']}", f"URL: {row['url'][:200]}"]
    if row["description"]:
        parts.append(f"Summary: {row['description'][:500]}")
    if row["content_text"]:
        parts.append(f"Page text (start): {row['content_text'][:1200]}")
    if row["remarks"]:
        parts.append(f"My notes: {row['remarks'][:300]}")
    return "\n".join(parts)


def candidates(lib: Library, recheck_ai: bool = False, ids: list[int] | None = None, limit: int | None = None,
               exclude_changeset: int | None = None) -> list:  # fmt: skip
    """Live bookmarks without a category (and, with recheck_ai, those the AI categorised), skipping ones the
    user categorised and ones already waiting in a pending proposal."""
    who = "(b.category_id IS NULL OR b.category_assigned_by = 'ai')" if recheck_ai else "b.category_id IS NULL"
    sql = (
        "SELECT b.*, COALESCE(b.url_resolved, b.url_original) AS url FROM bookmarks b"
        f" WHERE b.deleted_at IS NULL AND b.merged_into_id IS NULL AND {who} AND COALESCE(b.category_assigned_by, '') != 'user'"
        " AND NOT EXISTS (SELECT 1 FROM changeset_items i JOIN changesets c ON c.id = i.changeset_id"
        "   WHERE i.bookmark_id = b.id AND i.op = 'set_category' AND c.status IN ('running', 'pending', 'partial') AND i.decision IN ('pending', 'approved'))"
    )
    args: list = []
    if exclude_changeset is not None:  # resuming: also skip what this run already looked at
        sql += " AND b.id NOT IN (SELECT value FROM json_each((SELECT COALESCE(json_extract(params_json, '$.seen'), '[]') FROM changesets WHERE id = ?)))"
        args.append(exclude_changeset)
    if ids:
        sql += f" AND b.id IN ({','.join('?' * len(ids))})"
        args += ids
    sql += " ORDER BY b.saved_at IS NULL, b.saved_at DESC, b.id DESC"
    if limit:
        sql += " LIMIT ?"
        args.append(limit)
    return lib.conn.execute(sql, args).fetchall()


MAX_ERRORS_IN_A_ROW = 5  # then the model server is probably down: stop instead of failing every bookmark


@dataclass
class Result:
    changeset_id: int | None
    processed: int = 0
    total: int = 0
    proposed: int = 0
    no_fit: int = 0
    unchanged: int = 0
    errors: int = 0
    stopped: bool = False  # asked to pause or cancel; the changeset is left 'running'
    last_error: str | None = None
    tokens_in: int = 0  # model input tokens, when the decider reports them
    model_seconds: float = 0.0


def run(lib: Library, config, choose: Callable[[str, str, dict[str, str]], dict[str, float]] | None = None, *,
        recheck_ai: bool = False, ids: list[int] | None = None, limit: int | None = None, author: str = "ai",
        changeset_id: int | None = None, on_progress: Callable[[Result], None] | None = None,
        should_stop: Callable[[], bool] | None = None) -> Result:  # fmt: skip
    """Classify into a 'categorize' changeset (a new one, or `changeset_id` to continue a paused run).

    Stops between bookmarks when `should_stop()` is true, leaving the changeset 'running'; otherwise
    marks it pending (or rejected when nothing was proposed). A bookmark whose classification fails is
    counted in `errors` and skipped; MAX_ERRORS_IN_A_ROW failures in a row raise."""
    cats = lib.categories()
    if not cats:
        raise ValueError("there are no categories yet; propose them first (bmk ai taxonomy) and approve them")
    rows = candidates(lib, recheck_ai, ids, limit, exclude_changeset=changeset_id)
    if not rows and changeset_id is None:
        return Result(None)
    options = {c["name"]: c["description"] or c["name"] for c in cats}
    options[OTHER] = "None of the categories above fits this page"
    by_name = {c["name"]: c["id"] for c in cats}
    if changeset_id is None:
        changeset_id = changesets.create(lib, "categorize", author=author, provider="llama.cpp", model=config.decider_file,
                                         prompt_version=PROMPT_VERSION, status="running",
                                         summary=f"Classifying {len(rows)} bookmarks into {len(cats)} categories",
                                         params={"bookmarks": len(rows), "categories": len(cats), "recheck_ai": recheck_ai})  # fmt: skip
    result = Result(changeset_id, total=len(rows))
    seen: list[int] = []
    decider = None
    in_a_row = 0
    try:
        for row in rows:
            if should_stop and should_stop():
                result.stopped = True
                break
            if choose is None:
                from .decider import Decider

                decider = Decider(config).__enter__()
                choose = decider.choose
            try:
                probs = choose(state_text(row), INSTRUCTIONS, options)
                in_a_row = 0
                owner = getattr(choose, "__self__", None)  # Decider reports what each call cost
                result.tokens_in += getattr(owner, "last_tokens", 0)
                result.model_seconds += getattr(owner, "last_seconds", 0.0)
            except Exception as exc:  # noqa: BLE001 - one bookmark's failure shouldn't end the run
                result.errors += 1
                result.last_error = f"bookmark {row['id']}: {exc}"
                result.processed += 1
                seen.append(row["id"])
                if on_progress:
                    on_progress(result)
                in_a_row += 1
                if in_a_row >= MAX_ERRORS_IN_A_ROW:
                    raise RuntimeError(f"{in_a_row} bookmarks failed in a row; last: {result.last_error}") from exc
                continue
            ranked = sorted(probs.items(), key=lambda kv: -kv[1])
            best, p = ranked[0]
            why = ", ".join(f"{k} {v:.0%}" for k, v in ranked[:3])
            if best == OTHER:
                result.no_fit += 1
            elif by_name[best] == row["category_id"]:
                result.unchanged += 1
            else:
                changesets.add_item(lib, changeset_id, "set_category", bookmark_id=row["id"], old_value=row["category_id"],
                                    new_value=by_name[best], confidence=round(p, 4), rationale=why)  # fmt: skip
                result.proposed += 1
            result.processed += 1
            seen.append(row["id"])
            if on_progress:
                on_progress(result)
    finally:
        if decider is not None:
            decider.close()
    if result.stopped:
        _remember_seen(lib, changeset_id, seen)
    else:
        finish(lib, result)
    return result


def _remember_seen(lib: Library, changeset_id: int, seen: list[int]) -> None:
    """Paused: record which bookmarks were looked at, so resuming skips them (including 'other' ones)."""
    row = lib.conn.execute("SELECT params_json FROM changesets WHERE id=?", (changeset_id,)).fetchone()
    params = json.loads(row["params_json"] or "{}")
    params["seen"] = sorted(set(params.get("seen", [])) | set(seen))
    lib.conn.execute("UPDATE changesets SET params_json=? WHERE id=?", (json.dumps(params), changeset_id))


def finish(lib: Library, result: Result) -> None:
    """Hand the changeset over for review: pending if it has proposals, else rejected."""
    if result.changeset_id is None:
        return
    items = lib.conn.execute("SELECT COUNT(*) FROM changeset_items WHERE changeset_id=?", (result.changeset_id,)).fetchone()[0]
    summary = f"{items} categories proposed"
    if result.no_fit:
        summary += f"; {result.no_fit} fit no category"
    if result.unchanged:
        summary += f"; {result.unchanged} already right"
    if result.errors:
        summary += f"; {result.errors} could not be classified"
    changesets.set_status(lib, result.changeset_id, "pending" if items else "rejected", summary=summary)
