"""The service layer: the only code that reads or writes the library database.

CLI, HTTP API and MCP adapters call these methods; none of them touch SQL directly (PLAN05 §2).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterable
from typing import Any

from . import db as dbmod
from . import urls
from .config import Config, get_config
from .models import EDITABLE_FIELDS, STATUSES, ImportSummary, RawItem
from .timeutil import better_saved_at, now_utc, parse_iso, to_iso

log = logging.getLogger(__name__)

BACKUP_FORMAT = "bmk-backup"
# Dependency order for backup/restore.
BACKUP_TABLES = (
    "categories",
    "bookmarks",
    "processing_jobs",
    "url_aliases",
    "import_runs",
    "import_items",
    "sources",
    "tags",
    "bookmark_tags",
    "bookmark_events",
)

_SELECT_BOOKMARK = "SELECT b.*, COALESCE(b.url_resolved, b.url_original) AS url FROM bookmarks b"

# Smart lists available so far; Duplicates and Categories arrive with CG5.
VIEWS = ("inbox", "kept", "active", "unprocessed", "failed", "date_unknown", "resaved", "trash")
# Enrichment outcomes (bookmarks.fetch_status) that put a bookmark in the "failed" list.
FETCH_FAILED = ("failed", "needs_browser")
# Merge survivor: the more advanced triage status wins.
_STATUS_RANK = {"inbox": 0, "kept": 1}
SORTS = {"saved_at": "b.saved_at", "title": "lower(b.title)", "domain": "b.domain", "status": "b.status", "id": "b.id"}


class NotFound(LookupError):
    pass


class Conflict(ValueError):
    pass


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 query: every term quoted, prefix-matched, AND-ed."""
    terms = [t.replace('"', '""') for t in text.split() if t.strip()]
    return " ".join(f'"{t}"*' for t in terms)


class Library:
    def __init__(self, conn: sqlite3.Connection, config: Config | None = None, actor: str = "cli"):
        self.conn = conn
        self.config = config or get_config()
        self.actor = actor

    @classmethod
    def open(cls, config: Config | None = None, actor: str = "cli") -> "Library":
        config = config or get_config()
        return cls(dbmod.connect(config.db_path), config, actor)

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ ingest

    def ingest(self, source_type: str, items: Iterable[RawItem], params: dict | None = None) -> ImportSummary:
        """Validate, canonicalise, dedupe (tier 1) and upsert each item. One bad item never fails the run."""
        started = now_utc()
        cur = self.conn.execute(
            "INSERT INTO import_runs (source_type, params, started_at) VALUES (?, ?, ?)",
            (source_type, json.dumps(params or {}), started),
        )
        summary = ImportSummary(run_id=cur.lastrowid, source_type=source_type)
        known_refs = {
            r[0] for r in self.conn.execute("SELECT DISTINCT source_ref FROM sources WHERE source_type = ?", (source_type,))
        }
        try:
            with dbmod.transaction(self.conn):
                for item in items:
                    outcome, bookmark_id, error = self._ingest_item(summary.run_id, source_type, item, known_refs, started)
                    summary.counts[outcome] += 1
                    self.conn.execute(
                        "INSERT INTO import_items (import_run_id, locator, raw_url, raw_title, raw_timestamp, outcome, bookmark_id, error)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            summary.run_id,
                            item.locator,
                            item.url,
                            item.title,
                            to_iso(ts) if (ts := item.event_at or item.observed_by) else None,
                            outcome,
                            bookmark_id,
                            error,
                        ),
                    )
        except Exception as exc:  # reading the source failed mid-way; the item transaction is rolled back
            log.exception("Import run %s failed", summary.run_id)
            summary.error = str(exc)
            summary.counts = {k: 0 for k in summary.counts}
        c = summary.counts
        self.conn.execute(
            "UPDATE import_runs SET finished_at=?, items_seen=?, items_new=?, items_matched=?, items_unchanged=?,"
            " items_skipped=?, items_invalid=?, items_suppressed=?, error=? WHERE id=?",
            (
                now_utc(),
                summary.seen,
                c["new"],
                c["matched_exact"] + c["matched_alias"],
                c["unchanged"],
                c["skipped"],
                c["invalid"],
                c["suppressed_deleted"] + c["suppressed_resaved"],
                summary.error,
                summary.run_id,
            ),
        )
        if summary.error:
            raise RuntimeError(f"import run {summary.run_id} failed: {summary.error}")
        return summary

    def record_failed_run(self, source_type: str, error: str, params: dict | None = None) -> int:
        """Record a run that could not start (e.g. permission denied) so it shows in the import history."""
        now = now_utc()
        cur = self.conn.execute(
            "INSERT INTO import_runs (source_type, params, started_at, finished_at, error) VALUES (?, ?, ?, ?, ?)",
            (source_type, json.dumps(params or {}), now, now, error),
        )
        return cur.lastrowid

    def _ingest_item(
        self, run_id: int, source_type: str, item: RawItem, known_refs: set[str], now: str
    ) -> tuple[str, int | None, str | None]:
        try:
            canonical = urls.canonicalize(item.url)
        except urls.UnsupportedScheme as exc:
            return "skipped", None, str(exc)
        except urls.InvalidURL as exc:
            return "invalid", None, str(exc)

        self.conn.execute("SAVEPOINT item")
        try:
            result = self._upsert(run_id, source_type, item, canonical, known_refs, now)
        except Exception as exc:
            self.conn.execute("ROLLBACK TO item")
            self.conn.execute("RELEASE item")
            log.exception("Failed to ingest %s", item.url)
            return "invalid", None, f"internal error: {exc}"
        self.conn.execute("RELEASE item")
        return result

    def _item_saved_at(self, item: RawItem, source_type: str, known_refs: set[str], now: str) -> tuple[str | None, str]:
        if item.event_at is not None:
            return to_iso(item.event_at), item.event_at_kind or "exact"
        if item.observed_kind:
            # An observation is an upper bound on the save time; the source's own "existed by" time,
            # if earlier, is a tighter one. Future times are ignored.
            at = now
            if item.observed_by is not None and to_iso(item.observed_by) < now:
                at = to_iso(item.observed_by)
            # The first time a source_ref (e.g. a device) is ever observed, everything on it was already open.
            if item.observed_kind == "first_seen" and (item.source_ref or "") not in known_refs:
                return at, "first_seen_initial"
            return at, item.observed_kind
        return None, "unknown"

    def _find_bookmark(self, canonical: str) -> tuple[sqlite3.Row | None, str]:
        row = self.conn.execute("SELECT * FROM bookmarks WHERE url_canonical = ?", (canonical,)).fetchone()
        if row is not None:
            return self._follow_merges(row), "matched_exact"
        alias = self.conn.execute("SELECT bookmark_id FROM url_aliases WHERE url_canonical = ?", (canonical,)).fetchone()
        if alias is not None:
            row = self.conn.execute("SELECT * FROM bookmarks WHERE id = ?", (alias[0],)).fetchone()
            return self._follow_merges(row), "matched_alias"
        return None, "new"

    def _follow_merges(self, row: sqlite3.Row) -> sqlite3.Row:
        seen = set()
        while row["merged_into_id"] is not None and row["id"] not in seen:
            seen.add(row["id"])
            row = self.conn.execute("SELECT * FROM bookmarks WHERE id = ?", (row["merged_into_id"],)).fetchone()
        return row

    def _upsert(
        self, run_id: int, source_type: str, item: RawItem, canonical: str, known_refs: set[str], now: str
    ) -> tuple[str, int | None, str | None]:
        saved_at, saved_src = self._item_saved_at(item, source_type, known_refs, now)
        bookmark, match = self._find_bookmark(canonical)
        source_ref, locator, raw_url = item.source_ref or "", item.locator or "", item.url.strip()
        event_at = to_iso(item.event_at) if item.event_at else None
        # A title that is just the URL is no title: leave it empty so enrichment (CG3) can fill it.
        # The raw value is still kept on the source row.
        title = None if urls.title_is_url(item.title, raw_url) else (item.title or None)

        if bookmark is None:
            remarks = "\n".join(f"{k}: {v}" for k, v in item.extra.items() if v) or None
            cur = self.conn.execute(
                "INSERT INTO bookmarks (url_original, url_canonical, url_loose, title, title_source, domain,"
                " saved_at, saved_at_source, imported_at, updated_at, remarks)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    raw_url,
                    canonical,
                    urls.loose_key(canonical),
                    title,
                    "source" if title else None,
                    urls.domain_of(canonical),
                    saved_at,
                    saved_src,
                    now,
                    now,
                    remarks,
                ),
            )
            bookmark_id = cur.lastrowid
            self.conn.execute(
                "INSERT INTO url_aliases (url_canonical, bookmark_id, kind) VALUES (?, ?, 'canonical')", (canonical, bookmark_id)
            )
            self._insert_source(bookmark_id, source_type, source_ref, locator, raw_url, item.title, event_at, now, run_id)
            return "new", bookmark_id, None

        bookmark_id = bookmark["id"]
        existing = self.conn.execute(
            "SELECT id FROM sources WHERE source_type=? AND source_ref=? AND locator=? AND raw_url=?",
            (source_type, source_ref, locator, raw_url),
        ).fetchone()
        if existing is not None:
            self.conn.execute("UPDATE sources SET last_seen_at=? WHERE id=?", (now, existing["id"]))
        else:
            self._insert_source(bookmark_id, source_type, source_ref, locator, raw_url, item.title, event_at, now, run_id)

        if bookmark["deleted_at"]:
            resaved = existing is None and event_at is not None and parse_iso(event_at) > parse_iso(bookmark["deleted_at"])
            if resaved and self.config.resurrect_on_new_save:
                self._set_deleted(bookmark_id, None, "restore", {"reason": "saved again"})
            else:
                return ("suppressed_resaved" if resaved else "suppressed_deleted"), bookmark_id, None

        if existing is not None:
            return "unchanged", bookmark_id, None

        self._merge_item_fields(bookmark, title, saved_at, saved_src, now)
        return match, bookmark_id, None

    def _insert_source(self, bookmark_id, source_type, source_ref, locator, raw_url, raw_title, event_at, now, run_id) -> None:
        self.conn.execute(
            "INSERT INTO sources (bookmark_id, source_type, source_ref, locator, raw_url, raw_title, event_at,"
            " first_seen_at, last_seen_at, import_run_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (bookmark_id, source_type, source_ref, locator, raw_url, raw_title, event_at, now, now, run_id),
        )

    def _merge_item_fields(self, bookmark: sqlite3.Row, title: str | None, saved_at: str | None, saved_src: str, now: str) -> None:
        """A new source for an existing bookmark: fill gaps, move saved_at earlier. Never touch locked fields."""
        locked = set(json.loads(bookmark["locked_fields"]))
        updates: dict[str, Any] = {}
        if "saved_at" not in locked and better_saved_at(bookmark["saved_at"], bookmark["saved_at_source"], saved_at, saved_src):
            updates.update(saved_at=saved_at, saved_at_source=saved_src)
        if "title" not in locked and not bookmark["title"] and title:
            updates.update(title=title, title_source="source")
        if updates:
            updates["updated_at"] = now
            self._update_columns(bookmark["id"], updates)

    def _update_columns(self, bookmark_id: int, updates: dict[str, Any]) -> None:
        cols = ", ".join(f"{k}=?" for k in updates)
        self.conn.execute(f"UPDATE bookmarks SET {cols} WHERE id=?", (*updates.values(), bookmark_id))

    # ------------------------------------------------------------------ manual add

    def add(self, url: str, title: str | None = None, remarks: str | None = None) -> tuple[str, int | None]:
        """Add one URL by hand (CLI/API/MCP). Returns (outcome, bookmark_id)."""
        now = now_utc()
        item = RawItem(url=url, title=title, source_ref=self.actor, locator=now, event_at=parse_iso(now), event_at_kind="exact")
        summary = self.ingest("manual", [item], {"actor": self.actor})
        row = self.conn.execute(
            "SELECT outcome, bookmark_id, error FROM import_items WHERE import_run_id=?", (summary.run_id,)
        ).fetchone()
        if row["outcome"] in ("invalid", "skipped"):
            raise ValueError(row["error"])
        if remarks and row["bookmark_id"]:
            self.update(row["bookmark_id"], remarks=remarks)
        return row["outcome"], row["bookmark_id"]

    # ------------------------------------------------------------------ read

    def get(self, bookmark_id: int) -> dict[str, Any]:
        row = _row(self.conn.execute(f"{_SELECT_BOOKMARK} WHERE b.id=?", (bookmark_id,)).fetchone())
        if row is None:
            raise NotFound(f"bookmark {bookmark_id} not found")
        row["locked_fields"] = json.loads(row["locked_fields"])
        row["sources"] = [dict(r) for r in self.conn.execute("SELECT * FROM sources WHERE bookmark_id=? ORDER BY id", (bookmark_id,))]
        row["aliases"] = [dict(r) for r in self.conn.execute("SELECT * FROM url_aliases WHERE bookmark_id=? ORDER BY kind", (bookmark_id,))]
        row["tags"] = [
            r[0]
            for r in self.conn.execute(
                "SELECT t.name FROM tags t JOIN bookmark_tags bt ON bt.tag_id=t.id WHERE bt.bookmark_id=? ORDER BY t.name",
                (bookmark_id,),
            )
        ]
        row["events"] = [dict(r) for r in self.conn.execute("SELECT * FROM bookmark_events WHERE bookmark_id=? ORDER BY id", (bookmark_id,))]
        row["job"] = _row(
            self.conn.execute(
                "SELECT state, attempts, next_run_at, last_error, updated_at FROM processing_jobs WHERE bookmark_id=? AND kind='enrich'",
                (bookmark_id,),
            ).fetchone()
        )
        return row

    def list(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        view: str = "active",
        source_type: str | None = None,
        source_ref: str | None = None,
    ) -> list[dict[str, Any]]:
        """view: 'active' (default), 'trash', 'all', 'date_unknown'."""
        where, args = self._view_filter(view)
        if source_type or source_ref:
            sub, sub_args = [], []
            if source_type:
                sub.append("s.source_type=?")
                sub_args.append(source_type)
            if source_ref:
                sub.append("s.source_ref=?")
                sub_args.append(source_ref)
            where.append(f"EXISTS (SELECT 1 FROM sources s WHERE s.bookmark_id=b.id AND {' AND '.join(sub)})")
            args += sub_args
        sql = (
            f"{_SELECT_BOOKMARK} WHERE {' AND '.join(where) or '1'}"
            " ORDER BY b.saved_at IS NULL, b.saved_at DESC, b.id DESC LIMIT ? OFFSET ?"
        )
        return [dict(r) for r in self.conn.execute(sql, (*args, limit, offset))]

    def count(self, view: str = "active", source_type: str | None = None, source_ref: str | None = None) -> int:
        where, args = self._view_filter(view)
        src_where, src_args = self._source_filter(source_type, source_ref)
        where, args = where + src_where, args + src_args
        return self.conn.execute(f"SELECT COUNT(*) FROM bookmarks b WHERE {' AND '.join(where) or '1'}", args).fetchone()[0]

    def view_counts(self, source_type: str | None = None, source_ref: str | None = None) -> dict[str, int]:
        """Bookmarks per list, within one source (e.g. a device) if given, so the sidebar counts stack."""
        return {view: self.count(view, source_type, source_ref) for view in VIEWS}

    @staticmethod
    def _source_filter(source_type: str | None, source_ref: str | None) -> tuple[list[str], list[Any]]:
        if not (source_type or source_ref):
            return [], []
        sub, args = ["s.bookmark_id = b.id"], []
        if source_type:
            sub.append("s.source_type = ?")
            args.append(source_type)
        if source_ref:
            sub.append("s.source_ref = ?")
            args.append(source_ref)
        return [f"EXISTS (SELECT 1 FROM sources s WHERE {' AND '.join(sub)})"], args

    def browse(
        self,
        *,
        view: str = "active",
        q: str | None = None,
        status: str | None = None,
        source_type: str | None = None,
        source_ref: str | None = None,
        sort: str = "saved_at",
        desc: bool = True,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """One page for the viewer: filters combine (view AND search AND status AND source), plus the total."""
        where, args = self._view_filter(view)
        join = ""
        if q and (fts := _fts_query(q)):
            join = " JOIN bookmarks_fts f ON f.rowid = b.id"
            where.append("bookmarks_fts MATCH ?")
            args.append(fts)
        if status:
            where.append("b.status = ?")
            args.append(status)
        src_where, src_args = self._source_filter(source_type, source_ref)
        where += src_where
        args += src_args
        if sort not in SORTS:
            raise ValueError(f"sort must be one of {', '.join(SORTS)}")
        direction = "DESC" if desc else "ASC"
        order = f"{SORTS[sort]} IS NULL, {SORTS[sort]} {direction}, b.id {direction}"
        cond = " AND ".join(where) or "1"
        total = self.conn.execute(f"SELECT COUNT(*) FROM bookmarks b{join} WHERE {cond}", args).fetchone()[0]
        sql = (
            "SELECT b.*, COALESCE(b.url_resolved, b.url_original) AS url,"
            " (SELECT json_group_array(DISTINCT CASE WHEN s.source_ref = '' THEN s.source_type ELSE s.source_ref END)"
            "    FROM sources s WHERE s.bookmark_id = b.id) AS source_labels"
            f" FROM bookmarks b{join} WHERE {cond} ORDER BY {order} LIMIT ? OFFSET ?"
        )
        items = []
        for r in self.conn.execute(sql, (*args, limit, offset)):
            item = dict(r)
            item.pop("content_text", None)  # up to 20k chars each (CG3); the detail endpoint has it
            item["locked_fields"] = json.loads(item["locked_fields"])
            item["source_labels"] = json.loads(item["source_labels"] or "[]")
            items.append(item)
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    def source_refs(self, view: str = "active") -> list[dict[str, Any]]:
        """Distinct (source_type, source_ref) pairs, e.g. one per device, with their bookmark count in `view`.

        Every source with live bookmarks is listed, with 0 when none of them are in the view.
        """
        where, args = self._view_filter(view)
        counts = {
            (r[0], r[1]): r[2]
            for r in self.conn.execute(
                "SELECT s.source_type, s.source_ref, COUNT(DISTINCT s.bookmark_id) FROM sources s"
                f" JOIN bookmarks b ON b.id = s.bookmark_id WHERE {' AND '.join(where) or '1'} GROUP BY 1, 2",
                args,
            )
        }
        sql = (
            "SELECT DISTINCT s.source_type, s.source_ref FROM sources s JOIN bookmarks b ON b.id = s.bookmark_id"
            " WHERE b.merged_into_id IS NULL ORDER BY s.source_type, s.source_ref"
        )
        return [
            {"source_type": t, "source_ref": r, "bookmarks": counts.get((t, r), 0)} for t, r in self.conn.execute(sql)
        ]

    def last_run(self, source_type: str) -> dict[str, Any] | None:
        return _row(
            self.conn.execute("SELECT * FROM import_runs WHERE source_type=? ORDER BY id DESC LIMIT 1", (source_type,)).fetchone()
        )

    @staticmethod
    def _view_filter(view: str) -> tuple[list[str], list[Any]]:
        live = ["b.merged_into_id IS NULL"]
        if view == "active":
            return [*live, "b.deleted_at IS NULL"], []
        if view == "inbox":
            return [*live, "b.deleted_at IS NULL", "b.status = 'inbox'"], []
        if view == "kept":
            return [*live, "b.deleted_at IS NULL", "b.status = 'kept'"], []
        if view == "trash":
            return [*live, "b.deleted_at IS NOT NULL"], []
        if view == "unprocessed":
            return [*live, "b.deleted_at IS NULL", "b.fetch_status = 'pending'"], []
        if view == "failed":
            return [*live, "b.deleted_at IS NULL", f"b.fetch_status IN ({', '.join('?' for _ in FETCH_FAILED)})"], list(FETCH_FAILED)
        if view == "date_unknown":
            return [*live, "b.deleted_at IS NULL", "b.saved_at IS NULL"], []
        if view == "resaved":
            # Deleted, but a source has since reported a genuinely new save (§7.7).
            return [
                *live,
                "b.deleted_at IS NOT NULL",
                "EXISTS (SELECT 1 FROM import_items i WHERE i.bookmark_id = b.id AND i.outcome = 'suppressed_resaved')",
            ], []
        if view == "all":
            return [], []
        raise ValueError(f"unknown view '{view}'")

    def search(self, text: str, *, limit: int = 50, include_deleted: bool = False) -> list[dict[str, Any]]:
        query = _fts_query(text)
        if not query:
            return []
        where = "" if include_deleted else " AND b.deleted_at IS NULL AND b.merged_into_id IS NULL"
        sql = (
            "SELECT b.*, COALESCE(b.url_resolved, b.url_original) AS url FROM bookmarks_fts f"
            f" JOIN bookmarks b ON b.id = f.rowid WHERE bookmarks_fts MATCH ?{where} ORDER BY rank LIMIT ?"
        )
        return [dict(r) for r in self.conn.execute(sql, (query, limit))]

    def import_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM import_runs ORDER BY id DESC LIMIT ?", (limit,))]

    def import_items(self, run_id: int) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM import_items WHERE import_run_id=? ORDER BY id", (run_id,))]

    # ------------------------------------------------------------------ write

    def update(self, bookmark_id: int, **fields: Any) -> dict[str, Any]:
        """User edit. Each edited field is locked so machine processes never overwrite it."""
        unknown = set(fields) - set(EDITABLE_FIELDS)
        if unknown:
            raise ValueError(f"not editable: {', '.join(sorted(unknown))}")
        fields = {k: v for k, v in fields.items() if v is not None}
        if not fields:
            return self.get(bookmark_id)
        current = self.get(bookmark_id)
        now = now_utc()
        updates: dict[str, Any] = {}
        changes: dict[str, list[Any]] = {}
        locked = set(current["locked_fields"])

        with dbmod.transaction(self.conn):
            if "title" in fields:
                updates.update(title=fields["title"] or None, title_source="user")
            if "remarks" in fields:
                updates["remarks"] = fields["remarks"] or None
            if "status" in fields:
                if fields["status"] not in STATUSES:
                    raise ValueError(f"status must be one of {', '.join(STATUSES)}")
                updates["status"] = fields["status"]
            if "saved_at" in fields:
                updates.update(saved_at=to_iso(parse_iso(fields["saved_at"])), saved_at_source="user")
            if "url" in fields:
                updates.update(self._url_change(current, fields["url"]))
            for key in fields:
                column = "url_canonical" if key == "url" else key
                old = current["url"] if key == "url" else current.get(column)
                new = fields[key] if key == "url" else updates.get(column)
                if old != new:
                    changes[key] = [old, new]
                    if key != "status":
                        locked.add(key)
            if not changes:
                return current
            updates["locked_fields"] = json.dumps(sorted(locked))
            updates["updated_at"] = now
            self._update_columns(bookmark_id, updates)
            self._event(bookmark_id, "edit", changes, now)
        return self.get(bookmark_id)

    def _url_change(self, current: dict[str, Any], new_url: str) -> dict[str, Any]:
        canonical = urls.canonicalize(new_url)
        if canonical == current["url_canonical"]:
            return {"url_resolved": new_url.strip()}
        owner, _ = self._find_bookmark(canonical)
        if owner is not None and owner["id"] != current["id"]:
            raise Conflict(f"URL already belongs to bookmark {owner['id']}")
        # Keep the old canonical as an alias so re-ingesting the old URL still maps here.
        self.conn.execute("UPDATE url_aliases SET kind='original' WHERE url_canonical=?", (current["url_canonical"],))
        self.conn.execute(
            "INSERT OR REPLACE INTO url_aliases (url_canonical, bookmark_id, kind) VALUES (?, ?, 'canonical')",
            (canonical, current["id"]),
        )
        return {
            "url_resolved": new_url.strip(),  # the user's URL; enrichment must not replace it while 'url' is locked
            "url_canonical": canonical,
            "url_loose": urls.loose_key(canonical),
            "domain": urls.domain_of(canonical),
            "metadata_stale": 1,
        }

    def delete(self, bookmark_id: int) -> None:
        """Soft delete. The canonical URL and aliases remain as a tombstone; the source is never touched."""
        current = self.get(bookmark_id)
        if current["deleted_at"]:
            return
        with dbmod.transaction(self.conn):
            self._set_deleted(bookmark_id, now_utc(), "delete")

    def restore(self, bookmark_id: int) -> None:
        current = self.get(bookmark_id)
        if not current["deleted_at"]:
            return
        with dbmod.transaction(self.conn):
            self._set_deleted(bookmark_id, None, "restore")

    def _set_deleted(self, bookmark_id: int, deleted_at: str | None, kind: str, data: dict | None = None) -> None:
        now = now_utc()
        self.conn.execute("UPDATE bookmarks SET deleted_at=?, updated_at=? WHERE id=?", (deleted_at, now, bookmark_id))
        self._event(bookmark_id, kind, data, now)

    # ------------------------------------------------------------------ enrichment queue (CG3)

    def enqueue_missing(self) -> int:
        """Queue an enrichment job for every live bookmark that has never been processed."""
        now = now_utc()
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO processing_jobs (bookmark_id, kind, state, next_run_at, created_at, updated_at)"
            " SELECT id, 'enrich', 'pending', ?, ?, ? FROM bookmarks"
            " WHERE fetch_status = 'pending' AND deleted_at IS NULL AND merged_into_id IS NULL",
            (now, now, now),
        )
        return cur.rowcount

    def refetch(self, bookmark_ids: Iterable[int]) -> int:
        """Queue bookmarks to be fetched again now (also clears failures and retry waits)."""
        now = now_utc()
        count = 0
        with dbmod.transaction(self.conn):
            for bookmark_id in bookmark_ids:
                if self.conn.execute("SELECT 1 FROM bookmarks WHERE id=?", (bookmark_id,)).fetchone() is None:
                    raise NotFound(f"bookmark {bookmark_id} not found")
                self.conn.execute(
                    "INSERT INTO processing_jobs (bookmark_id, kind, state, next_run_at, created_at, updated_at)"
                    " VALUES (?, 'enrich', 'pending', ?, ?, ?)"
                    " ON CONFLICT (bookmark_id, kind) DO UPDATE SET state='pending', attempts=0, next_run_at=excluded.next_run_at,"
                    " lease_until=NULL, last_error=NULL, updated_at=excluded.updated_at",
                    (bookmark_id, now, now, now),
                )
                self.conn.execute("UPDATE bookmarks SET fetch_status='pending', fetch_error=NULL WHERE id=?", (bookmark_id,))
                count += 1
        return count

    def retry_failed(self) -> int:
        """Queue every failed or blocked bookmark again."""
        ids = [r[0] for r in self.conn.execute(
            f"SELECT id FROM bookmarks WHERE deleted_at IS NULL AND merged_into_id IS NULL AND fetch_status IN ({', '.join('?' for _ in FETCH_FAILED)})",
            FETCH_FAILED,
        )]
        return self.refetch(ids)

    def queue_stats(self) -> dict[str, int]:
        """Enrichment jobs for live bookmarks: due now, waiting to retry, finished, given up."""
        now = now_utc()
        row = self.conn.execute(
            "SELECT"
            " SUM(j.state IN ('pending', 'retry_wait') AND j.next_run_at <= ?) + SUM(j.state = 'leased' AND j.lease_until < ?),"
            " SUM(j.state = 'retry_wait' AND j.next_run_at > ?),"
            " SUM(j.state = 'leased' AND j.lease_until >= ?),"
            " SUM(j.state = 'done'), SUM(j.state = 'dead')"
            " FROM processing_jobs j JOIN bookmarks b ON b.id = j.bookmark_id"
            " WHERE b.deleted_at IS NULL AND b.merged_into_id IS NULL",
            (now, now, now, now),
        ).fetchone()
        return dict(zip(("due", "waiting", "running", "done", "dead"), (v or 0 for v in row), strict=True))

    def adopt_url(self, bookmark_id: int, new_url: str, kind: str, now: str) -> int:
        """Point a bookmark at its resolved URL (a short link's destination, an unwrapped redirect).

        The old canonical stays as an alias (`kind` 'short' or 'redirect'), so seeing the short link again
        still finds this bookmark. If another bookmark already has the destination, the two are merged
        (dedupe tier 2) and the survivor's id is returned. Call inside a transaction.
        """
        current = self.conn.execute("SELECT * FROM bookmarks WHERE id=?", (bookmark_id,)).fetchone()
        canonical = urls.canonicalize(new_url)
        if canonical == current["url_canonical"]:
            self._update_columns(bookmark_id, {"url_resolved": new_url})
            return bookmark_id
        owner, _ = self._find_bookmark(canonical)
        if owner is not None and owner["id"] != bookmark_id:
            return self.merge(bookmark_id, owner["id"], reason="same page after resolving", now=now)
        self.conn.execute("UPDATE url_aliases SET kind=? WHERE url_canonical=?", (kind, current["url_canonical"]))
        self.conn.execute(
            "INSERT OR REPLACE INTO url_aliases (url_canonical, bookmark_id, kind) VALUES (?, ?, 'canonical')", (canonical, bookmark_id)
        )
        self._update_columns(
            bookmark_id,
            {
                "url_resolved": new_url,
                "url_canonical": canonical,
                "url_loose": urls.loose_key(canonical),
                "domain": urls.domain_of(canonical),
                "updated_at": now,
            },
        )
        self._event(bookmark_id, "resolve", {"from": current["url_canonical"], "to": canonical}, now)
        return bookmark_id

    def merge(self, a_id: int, b_id: int, *, reason: str, now: str) -> int:
        """Merge two bookmarks for the same page; returns the survivor's id. Call inside a transaction.

        Survivor: the one with more locked (user-edited) fields, then the earlier, more precise saved_at, then
        the lower id (PLAN05 §7). Sources and aliases move to the survivor; remarks are concatenated; the more
        advanced status wins; gaps are filled from the other. The merged one keeps its row (merged_into_id) with
        a snapshot in its event, so CG5 can un-merge.
        """
        a, b = (self.conn.execute("SELECT * FROM bookmarks WHERE id=?", (i,)).fetchone() for i in (a_id, b_id))
        locks_a, locks_b = len(json.loads(a["locked_fields"])), len(json.loads(b["locked_fields"]))
        if locks_a != locks_b:
            survivor = a if locks_a > locks_b else b
        elif better_saved_at(a["saved_at"], a["saved_at_source"], b["saved_at"], b["saved_at_source"]):
            survivor = b
        elif better_saved_at(b["saved_at"], b["saved_at_source"], a["saved_at"], a["saved_at_source"]):
            survivor = a
        else:
            survivor = a if a["id"] < b["id"] else b
        loser = b if survivor is a else a
        locked = set(json.loads(survivor["locked_fields"]))
        loser_locked = set(json.loads(loser["locked_fields"]))
        updates: dict[str, Any] = {"updated_at": now}
        if "title" not in locked and ("title" in loser_locked or (not survivor["title"] and loser["title"])):
            updates.update(title=loser["title"], title_source=loser["title_source"])
            if "title" in loser_locked:
                locked.add("title")
        if "saved_at" not in locked and better_saved_at(survivor["saved_at"], survivor["saved_at_source"], loser["saved_at"], loser["saved_at_source"]):
            updates.update(saved_at=loser["saved_at"], saved_at_source=loser["saved_at_source"])
        remarks = [r for r in (survivor["remarks"], loser["remarks"]) if r]
        if len(remarks) == 2 and remarks[0] != remarks[1]:
            updates["remarks"] = "\n\n".join(remarks)
        elif remarks:
            updates["remarks"] = remarks[0]
        if _STATUS_RANK.get(loser["status"], 0) > _STATUS_RANK.get(survivor["status"], 0):
            updates["status"] = loser["status"]
        for col in ("description", "site_name", "author", "published_at", "image_url", "content_text", "canonical_hint"):
            if not survivor[col] and loser[col]:
                updates[col] = loser[col]
        updates["locked_fields"] = json.dumps(sorted(locked))
        self._update_columns(survivor["id"], updates)

        snapshot = {
            "bookmark": dict(loser),
            "sources": [r[0] for r in self.conn.execute("SELECT id FROM sources WHERE bookmark_id=?", (loser["id"],))],
            "aliases": [dict(r) for r in self.conn.execute("SELECT * FROM url_aliases WHERE bookmark_id=?", (loser["id"],))],
        }
        self.conn.execute("UPDATE sources SET bookmark_id=? WHERE bookmark_id=?", (survivor["id"], loser["id"]))
        self.conn.execute("UPDATE url_aliases SET bookmark_id=? WHERE bookmark_id=?", (survivor["id"], loser["id"]))
        self.conn.execute("UPDATE url_aliases SET kind='merged' WHERE url_canonical=?", (loser["url_canonical"],))
        self.conn.execute(
            "INSERT OR IGNORE INTO bookmark_tags (bookmark_id, tag_id, assigned_by) SELECT ?, tag_id, assigned_by FROM bookmark_tags WHERE bookmark_id=?",
            (survivor["id"], loser["id"]),
        )
        self.conn.execute("DELETE FROM bookmark_tags WHERE bookmark_id=?", (loser["id"],))
        self.conn.execute("DELETE FROM processing_jobs WHERE bookmark_id=?", (loser["id"],))
        self.conn.execute("UPDATE bookmarks SET merged_into_id=?, updated_at=? WHERE id=?", (survivor["id"], now, loser["id"]))
        self._event(survivor["id"], "merge", {"merged": loser["id"], "reason": reason}, now)
        self._event(loser["id"], "merged_into", {"survivor": survivor["id"], "reason": reason, "snapshot": snapshot}, now)
        return survivor["id"]

    def _event(self, bookmark_id: int, kind: str, data: Any, at: str) -> None:
        self.conn.execute(
            "INSERT INTO bookmark_events (bookmark_id, at, actor, kind, data_json) VALUES (?, ?, ?, ?, ?)",
            (bookmark_id, at, self.actor, kind, json.dumps(data) if data is not None else None),
        )

    # ------------------------------------------------------------------ backup / restore

    def export_backup(self) -> dict[str, Any]:
        tables = {t: [dict(r) for r in self.conn.execute(f"SELECT * FROM {t} ORDER BY rowid")] for t in BACKUP_TABLES}
        return {
            "format": BACKUP_FORMAT,
            "schema_version": dbmod.schema_version(self.conn),
            "exported_at": now_utc(),
            "tables": tables,
        }

    def import_backup(self, data: dict[str, Any]) -> dict[str, int]:
        """Restore a backup into an empty library. Returns row counts per table."""
        if data.get("format") != BACKUP_FORMAT:
            raise ValueError("not a bmk backup file")
        if data.get("schema_version") != dbmod.schema_version(self.conn):
            raise ValueError(
                f"backup schema v{data.get('schema_version')} does not match database v{dbmod.schema_version(self.conn)}"
            )
        if self.conn.execute("SELECT EXISTS (SELECT 1 FROM bookmarks)").fetchone()[0]:
            raise Conflict("target library is not empty; restore into a fresh database (set BMK_DB_PATH)")
        counts = {}
        with dbmod.transaction(self.conn):
            self.conn.execute("PRAGMA defer_foreign_keys = ON")
            for table in BACKUP_TABLES:
                rows = data["tables"].get(table, [])
                for row in rows:
                    cols = ", ".join(row)
                    marks = ", ".join("?" for _ in row)
                    self.conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(row.values()))
                counts[table] = len(rows)
        return counts
