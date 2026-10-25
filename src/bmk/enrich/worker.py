"""Enrichment worker (PLAN05 §6): drains `processing_jobs`.

Network work runs in a thread pool (at most 2 requests per site at once, a second apart, and
`fetch_concurrency` overall); results are written by the calling thread, one transaction per bookmark.
Jobs are leased, so the background scan and the web server can both run a worker safely.

Per bookmark: unwrap known redirect wrappers → skip pages not worth fetching (Google searches) →
oEmbed where it exists → fetch the page (resolving short links) → adopt the resolved URL, merging with an
existing bookmark for the same page (dedupe tier 2) → store metadata. Locked fields are never touched.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

from .. import db as dbmod
from .. import urls
from ..config import Config, get_config
from ..service import Library
from ..timeutil import now_utc, parse_iso, to_iso
from . import extract
from .fetch import FetchError, Fetcher, Page, classify, is_generic_title, is_login_url, should_skip, unwrap

log = logging.getLogger(__name__)

BACKOFF = (60, 300, 1800, 7200, 43200)  # seconds before retry 1…5
MAX_ATTEMPTS = len(BACKOFF)
MAX_WAIT = 24 * 3600
LEASE_SECONDS = 120
PER_SITE = 2
SITE_GAP = 1.0  # seconds between starting two requests to the same site


@dataclass
class Outcome:
    """What the network work found for one bookmark (computed off the database thread)."""

    status: str  # fetched | partial | skipped | needs_browser | failed | retry
    final_url: str | None = None  # resolved destination to adopt, if any
    redirect_kind: str = "redirect"  # alias kind for the old URL: 'short' | 'redirect'
    meta: extract.Metadata | None = None
    frame_ok: bool | None = None
    http_status: int | None = None
    error: str | None = None
    retry_after: float | None = None


def _resolved(start: str, page: Page) -> str | None:
    """The destination worth adopting from a fetch, or None if the redirects should not change the URL."""
    final = unwrap(page.url)
    if final == start or is_login_url(final):
        return None
    if urls.is_shortener(start):
        return final
    # Other redirects only when every hop was permanent and they did not just land on a site's home page.
    permanent = page.hops and all(code in (301, 308) for code, _ in page.hops)
    went_home = (urlsplit(final).path or "/") == "/" and (urlsplit(start).path or "/") != "/"
    return final if permanent and not went_home else None


def enrich(fetcher: Fetcher, bookmark: dict[str, Any]) -> Outcome:
    """Fetch and read one bookmark. Pure network and parsing: no database access."""
    url_locked = "url" in bookmark["locked_fields"]
    start = (bookmark["url_resolved"] if url_locked and bookmark["url_resolved"] else None) or bookmark["url_original"]
    target = start if url_locked else unwrap(start)
    unwrapped = target if target != start else None
    kind = "short" if urls.is_shortener(start) else "redirect"

    if should_skip(target):
        return Outcome("skipped", final_url=unwrapped, redirect_kind=kind)

    try:
        endpoint = extract.oembed_endpoint(urls.canonicalize(target))  # without tracking parameters
    except ValueError:
        endpoint = None
    if endpoint:
        try:
            page = fetcher.get(endpoint, accept="json")
            classify(page)
            data = extract.parse_oembed(page.body)
            if data:
                meta = extract.from_oembed(data, target)
                return Outcome("fetched", final_url=unwrapped, redirect_kind=kind, meta=meta, http_status=200)
        except FetchError as exc:
            if exc.kind == "blocked":
                return Outcome("failed", final_url=unwrapped, error=str(exc))
            if exc.status == 404 and urlsplit(target).hostname in ("x.com", "twitter.com"):
                return Outcome("failed", final_url=unwrapped, error="the post was deleted or is private", http_status=404)
            # Otherwise fall back to the page itself.

    page = None
    try:
        page = fetcher.get(target)
        classify(page)
    except FetchError as exc:
        final = unwrapped
        if page is not None and not url_locked:
            final = _resolved(start, page) or unwrapped
        status = "failed" if exc.kind == "blocked" else exc.kind
        return Outcome(status, final_url=final, redirect_kind=kind, http_status=exc.status, error=str(exc), retry_after=exc.retry_after)

    final = None if url_locked else (_resolved(start, page) or unwrapped)
    frame_ok = extract.frame_allowed(page.headers)
    if not page.is_html or not page.body:
        what = page.content_type or "an empty response"
        return Outcome("partial", final_url=final, redirect_kind=kind, frame_ok=frame_ok, http_status=page.status, error=f"not a web page ({what})")
    meta = extract.from_html(page.text, page.url)
    status = "fetched" if (meta.title or meta.description or meta.content_text) else "partial"
    return Outcome(status, final_url=final, redirect_kind=kind, meta=meta, frame_ok=frame_ok, http_status=page.status)


# ------------------------------------------------------------------ storing results

_META_COLUMNS = ("description", "site_name", "author", "published_at", "image_url", "content_text", "content_simhash", "canonical_hint")


def _title_replaceable(row) -> bool:
    """A fetched title only fills a gap. The title the browser showed (source) was taken after the page's
    JavaScript ran, so it beats a plain fetch ("Microsoft Forms", "Unsupported client – Canva"); a title
    from an earlier fetch may be refreshed; the user's is never touched."""
    if row["title_source"] == "user":
        return False
    if row["title_source"] in ("html", "oembed"):
        return True
    return is_generic_title(row["title"], row["url_original"]) or urls.title_is_url(row["title"], row["url_original"])


def apply(lib: Library, job_id: int, bookmark_id: int, attempts: int, outcome: Outcome) -> int:
    """Write one outcome. Returns the bookmark id that holds the result (differs after a merge)."""
    now = now_utc()
    with dbmod.transaction(lib.conn):
        row = lib.conn.execute("SELECT * FROM bookmarks WHERE id=?", (bookmark_id,)).fetchone()
        if row is None or row["merged_into_id"] is not None:
            lib.conn.execute("UPDATE processing_jobs SET state='done', lease_until=NULL, updated_at=? WHERE id=?", (now, job_id))
            return bookmark_id

        if outcome.status == "retry" and attempts + 1 < MAX_ATTEMPTS:
            wait_s = min(MAX_WAIT, max(BACKOFF[attempts], outcome.retry_after or 0))
            lib.conn.execute(
                "UPDATE processing_jobs SET state='retry_wait', attempts=?, next_run_at=?, lease_until=NULL, last_error=?, updated_at=? WHERE id=?",
                (attempts + 1, to_iso(parse_iso(now) + timedelta(seconds=wait_s)), outcome.error, now, job_id),
            )
            lib.conn.execute("UPDATE bookmarks SET fetch_error=? WHERE id=?", (outcome.error, bookmark_id))
            return bookmark_id

        locked = set(json.loads(row["locked_fields"]))
        target_id = bookmark_id
        if outcome.final_url and "url" not in locked:
            target_id = lib.adopt_url(bookmark_id, outcome.final_url, outcome.redirect_kind, now)

        status = "failed" if outcome.status == "retry" else outcome.status  # retries used up
        updates: dict[str, Any] = {
            "fetch_status": status,
            "fetch_error": outcome.error,
            "http_status": outcome.http_status,
            "last_fetched_at": now,
            "metadata_stale": 0,
            "updated_at": now,
        }
        if outcome.frame_ok is not None:
            updates["frame_ok"] = int(outcome.frame_ok)
        changed = []
        target = lib.conn.execute("SELECT * FROM bookmarks WHERE id=?", (target_id,)).fetchone()
        target_locked = set(json.loads(target["locked_fields"]))
        if target_id != bookmark_id and target["fetch_status"] in ("fetched", "partial") and status not in ("fetched", "partial"):
            del updates["fetch_status"], updates["fetch_error"]  # the survivor already has a good result
        meta = outcome.meta
        if meta is not None:
            for col in _META_COLUMNS:
                value = getattr(meta, col)
                if value and col not in target_locked and value != target[col]:
                    updates[col] = value
                    changed.append(col)
            if meta.title and "title" not in target_locked and meta.title != target["title"] and _title_replaceable(target):
                updates.update(title=meta.title, title_source=meta.title_source)
                changed.append("title")
        cols = ", ".join(f"{k}=?" for k in updates)
        lib.conn.execute(f"UPDATE bookmarks SET {cols} WHERE id=?", (*updates.values(), target_id))
        lib.conn.execute(
            "UPDATE processing_jobs SET state=?, attempts=?, lease_until=NULL, last_error=?, updated_at=? WHERE id=?",
            ("done" if status in ("fetched", "partial", "skipped") else "dead", attempts + 1, outcome.error, now, job_id),
        )
        data = {"status": status, "changed": changed}
        if outcome.error:
            data["error"] = outcome.error
        lib._event(target_id, "enrich", data, now)  # noqa: SLF001
        return target_id


# ------------------------------------------------------------------ the queue


class Worker:
    def __init__(self, config: Config | None = None, fetcher: Fetcher | None = None):
        self.config = config or get_config()
        self._fetcher = fetcher

    def run(
        self,
        *,
        budget: float | None = None,
        max_jobs: int | None = None,
        only_ids: Iterable[int] | None = None,
        on_result: Callable[[int, Outcome], None] | None = None,
    ) -> Counter:
        """Process due jobs until none are left, `budget` seconds pass or `max_jobs` have started."""
        stats: Counter = Counter()
        if not self.config.enrich:
            return stats
        only = list(only_ids) if only_ids is not None else None
        fetcher = self._fetcher or Fetcher()
        lib = Library.open(self.config, actor="enrich")
        pool = ThreadPoolExecutor(max(1, self.config.fetch_concurrency), thread_name_prefix="bmk-fetch")
        inflight: dict[Future, tuple[int, int, int, str]] = {}
        active: Counter = Counter()
        last_start: dict[str, float] = {}
        deadline = time.monotonic() + budget if budget else None
        started = 0
        try:
            with dbmod.transaction(lib.conn):
                lib.enqueue_missing()
            while True:
                stopping = (deadline is not None and time.monotonic() > deadline) or (max_jobs is not None and started >= max_jobs)
                while not stopping and len(inflight) < self.config.fetch_concurrency:
                    now = time.monotonic()
                    busy = {d for d, n in active.items() if n >= PER_SITE} | {d for d, t in last_start.items() if now - t < SITE_GAP}
                    leased = self._lease(lib, busy, only)
                    if leased is None:
                        break
                    job_id, bookmark, attempts = leased
                    site = bookmark["domain"] or ""
                    active[site] += 1
                    last_start[site] = now
                    inflight[pool.submit(enrich, fetcher, bookmark)] = (job_id, bookmark["id"], attempts, site)
                    started += 1
                    stopping = max_jobs is not None and started >= max_jobs
                if not inflight:
                    if stopping or not self._due(lib, only):
                        break
                    time.sleep(0.2)  # due jobs are waiting for a site's politeness gap
                    continue
                done, _ = wait(list(inflight), timeout=0.5, return_when=FIRST_COMPLETED)
                for future in done:
                    job_id, bookmark_id, attempts, site = inflight.pop(future)
                    active[site] -= 1
                    try:
                        outcome = future.result()
                    except Exception as exc:  # a bug in parsing must not stop the queue
                        log.exception("enrichment crashed for bookmark %s", bookmark_id)
                        outcome = Outcome("retry", error=f"internal error: {exc}")
                    if outcome.status == "retry" and outcome.retry_after:
                        self._cool_down(lib, site, outcome.retry_after)
                    try:
                        apply(lib, job_id, bookmark_id, attempts, outcome)
                    except Exception:
                        log.exception("could not store enrichment for bookmark %s", bookmark_id)
                        outcome = Outcome("retry", error="could not store the result")
                    stats[outcome.status] += 1
                    if on_result:
                        on_result(bookmark_id, outcome)
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
            lib.close()
            if self._fetcher is None:
                fetcher.close()
        return stats

    @staticmethod
    def _due_sql(only: list[int] | None) -> tuple[str, list[Any]]:
        now = now_utc()
        sql = (
            " FROM processing_jobs j JOIN bookmarks b ON b.id = j.bookmark_id"
            " WHERE j.kind = 'enrich' AND b.deleted_at IS NULL AND b.merged_into_id IS NULL"
            " AND ((j.state IN ('pending', 'retry_wait') AND j.next_run_at <= ?) OR (j.state = 'leased' AND j.lease_until < ?))"
        )
        args: list[Any] = [now, now]
        if only is not None:
            sql += f" AND j.bookmark_id IN ({', '.join('?' for _ in only) or 'NULL'})"
            args += only
        return sql, args

    def _due(self, lib: Library, only: list[int] | None) -> bool:
        sql, args = self._due_sql(only)
        return lib.conn.execute(f"SELECT EXISTS (SELECT 1{sql})", args).fetchone()[0] == 1

    def _lease(self, lib: Library, busy: set[str], only: list[int] | None) -> tuple[int, dict[str, Any], int] | None:
        sql, args = self._due_sql(only)
        if busy:
            sql += f" AND COALESCE(b.domain, '') NOT IN ({', '.join('?' for _ in busy)})"
            args += sorted(busy)
        with dbmod.transaction(lib.conn):
            row = lib.conn.execute(
                f"SELECT j.id AS job_id, j.attempts, b.id, b.url_original, b.url_resolved, b.domain, b.locked_fields{sql}"
                " ORDER BY j.next_run_at, j.id LIMIT 1",
                args,
            ).fetchone()
            if row is None:
                return None
            now = now_utc()
            lib.conn.execute(
                "UPDATE processing_jobs SET state='leased', lease_until=?, updated_at=? WHERE id=?",
                (to_iso(parse_iso(now) + timedelta(seconds=LEASE_SECONDS)), now, row["job_id"]),
            )
        bookmark = dict(row)
        bookmark["locked_fields"] = json.loads(bookmark["locked_fields"])
        return row["job_id"], bookmark, row["attempts"]

    @staticmethod
    def _cool_down(lib: Library, site: str, seconds: float) -> None:
        """A site said "slow down": push its other due jobs back too, so they are not tried meanwhile."""
        until = to_iso(parse_iso(now_utc()) + timedelta(seconds=min(MAX_WAIT, seconds)))
        with dbmod.transaction(lib.conn):
            lib.conn.execute(
                "UPDATE processing_jobs SET next_run_at=? WHERE state IN ('pending', 'retry_wait') AND next_run_at < ?"
                " AND bookmark_id IN (SELECT id FROM bookmarks WHERE COALESCE(domain, '') = ?)",
                (until, until, site),
            )


class BackgroundWorker:
    """Runs the worker in a thread inside `bmk serve`: on demand (`kick`) and every few minutes for retries."""

    def __init__(self, config: Config, interval: float = 300.0):
        self.config = config
        self.interval = interval
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="bmk-enrich", daemon=True)
        self.running = False

    def start(self) -> None:
        self._thread.start()

    def kick(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.running = True
            try:
                Worker(self.config).run(budget=600)
            except Exception:
                log.exception("background enrichment failed")
            finally:
                self.running = False
            self._wake.wait(self.interval)
            self._wake.clear()
