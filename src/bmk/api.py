"""HTTP API and web UI (PLAN05 §10): a thin adapter over bmk.service.Library, bound to 127.0.0.1.

Protection against other local web pages:
- Only Host: 127.0.0.1 / localhost is accepted, which defeats DNS rebinding.
- Every write needs the X-BMK-Token header. The token is in a 0600 file and is injected into the served
  index.html; a cross-origin page can neither read that page nor send the custom header (no CORS).
"""

import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__, changesets, dedupe, doctor, launchd
from . import export as exporter
from . import db as dbmod
from .config import Config, get_config
from .ingest.cloudtabs import CloudTabsError, CloudTabsPermissionDenied, run_import
from .models import STATUSES
from .service import Conflict, Library, NotFound

TOKEN_HEADER = "X-BMK-Token"
WRITE_METHODS = {"POST", "PATCH", "PUT", "DELETE"}
MAX_UPLOAD = 50 * 1024 * 1024


def load_token(config: Config) -> str:
    """Read the API token, creating it (0600) on first use."""
    path = config.token_path
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    token = secrets.token_urlsafe(32)
    path.touch(mode=0o600)
    path.write_text(token + "\n", encoding="utf-8")
    return token


def _worker_class():
    """The enrichment Worker, or None if the "enrich" extra is not installed."""
    try:
        from .enrich.worker import Worker
    except ImportError:
        return None
    return Worker


def create_app(config: Config | None = None, background: bool = False) -> FastAPI:
    """`background` runs the enrichment worker in a thread (bmk serve); tests leave it off."""
    config = config or get_config()
    token = load_token(config)
    dbmod.connect(config.db_path).close()  # create / migrate up front, not on the first request
    web_dir = resources.files("bmk") / "web"
    Worker = _worker_class() if config.enrich else None
    worker = None
    if background and Worker is not None:
        from .enrich.worker import BackgroundWorker

        worker = BackgroundWorker(config)
        worker.start()

    def kick() -> None:
        if worker is not None:
            worker.kick()

    def after_ingest() -> None:
        """New bookmarks: look for duplicates now, and fetch their pages in the background."""
        with library() as lib:
            dedupe.scan(lib)
        kick()

    app = FastAPI(title="bmk", version=__version__, docs_url=None, redoc_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])

    @app.middleware("http")
    async def require_token(request: Request, call_next):
        if request.method in WRITE_METHODS and request.url.path.startswith("/api/"):
            if not secrets.compare_digest(request.headers.get(TOKEN_HEADER, ""), token):
                return JSONResponse({"detail": "missing or wrong API token"}, status_code=403)
        return await call_next(request)

    @contextmanager
    def library() -> Iterator[Library]:
        # One connection per request, opened in the worker thread that uses it.
        lib = Library(dbmod.connect(config.db_path, migrate=False), config, actor="web")
        try:
            yield lib
        except NotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        finally:
            lib.close()

    # ------------------------------------------------------------------ UI

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index() -> str:
        html = (web_dir / "index.html").read_text(encoding="utf-8")
        return html.replace("{{BMK_TOKEN}}", token).replace("{{BMK_VERSION}}", __version__)

    app.mount("/static", StaticFiles(directory=str(web_dir)), name="static")

    # ------------------------------------------------------------------ read

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "version": __version__}

    @app.get("/api/meta")
    def meta(view: str = "active", source_type: str | None = None, source_ref: str | None = None, q: str | None = None) -> dict[str, Any]:
        """Sidebar data. List counts are within the selected source, source counts within the selected list."""
        with library() as lib:
            return {
                "counts": lib.view_counts(source_type, source_ref, q),
                "sources": lib.source_refs(view),
                "last_cloudtabs_run": lib.last_run("cloudtabs"),
                "statuses": list(STATUSES),
                "display_tz": config.display_tz,
                "queue": lib.queue_stats(),
                "enrich": Worker is not None,
                "worker_running": bool(worker and worker.running),
                "duplicates": dedupe.cluster_count(lib),
                "categories": lib.categories(view),
                "uncategorised": lib.uncategorised_count(view),
                "pending_changesets": lib.conn.execute("SELECT COUNT(*) FROM changesets WHERE status IN ('pending', 'partial')").fetchone()[0],
                "ai_jobs_active": lib.conn.execute("SELECT COUNT(*) FROM ai_jobs WHERE state IN ('queued', 'running')").fetchone()[0],
            }

    @app.get("/api/bookmarks")
    def bookmarks(
        view: str = "active",
        q: str | None = None,
        status: str | None = None,
        source_type: str | None = None,
        source_ref: str | None = None,
        category: str | None = None,
        sort: str = "saved_at",
        desc: bool = True,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        with library() as lib:
            return lib.browse(
                view=view, q=q, status=status, source_type=source_type, source_ref=source_ref,
                category=category if category in (None, "none") else int(category), sort=sort, desc=desc, limit=min(max(limit, 1), 500), offset=max(offset, 0),
            )

    @app.get("/api/bookmarks/{bookmark_id}")
    def bookmark(bookmark_id: int) -> dict[str, Any]:
        with library() as lib:
            return lib.get(bookmark_id)

    @app.get("/api/runs")
    def runs(limit: int = 20) -> list[dict[str, Any]]:
        with library() as lib:
            return lib.import_runs(limit)

    @app.get("/api/runs/{run_id}/items")
    def run_items(run_id: int) -> list[dict[str, Any]]:
        with library() as lib:
            return lib.import_items(run_id)

    @app.get("/api/doctor")
    def health_checks() -> list[dict[str, Any]]:
        return [c.as_dict() for c in doctor.run_checks(config)]

    @app.get("/api/log")
    def scan_log(lines: int = 200) -> dict[str, Any]:
        return launchd.read_log(max(1, min(lines, 2000)))

    @app.get("/api/export")
    def export(
        format: str = "json", view: str = "active", q: str | None = None, source_type: str | None = None, source_ref: str | None = None
    ) -> Response:
        """`json` is the full backup; the other formats export one list (the UI passes its current filters)."""
        if format == "json":
            with library() as lib:
                data = lib.export_backup()
            return JSONResponse(data, headers={"Content-Disposition": 'attachment; filename="bmk-backup.json"'})
        if format not in exporter.FORMATS:
            raise HTTPException(400, f"format must be one of json, {', '.join(exporter.FORMATS)}")
        media, ext = exporter.FORMATS[format]
        with library() as lib:
            text = exporter.render(format, exporter.rows(lib, view=view, q=q, source_type=source_type, source_ref=source_ref))
        name = f"bookmarks-{view}.{ext}"
        return Response(text, media_type=f"{media}; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{name}"'})

    # ------------------------------------------------------------------ write

    @app.patch("/api/bookmarks/{bookmark_id}")
    def edit(bookmark_id: int, fields: dict[str, Any] = Body(...)) -> dict[str, Any]:
        with library() as lib:
            return lib.update(bookmark_id, **fields)

    @app.delete("/api/bookmarks/{bookmark_id}")
    def delete(bookmark_id: int) -> dict[str, Any]:
        with library() as lib:
            lib.delete(bookmark_id)
            return {"ok": True}

    @app.post("/api/bookmarks/{bookmark_id}/restore")
    def restore(bookmark_id: int) -> dict[str, Any]:
        with library() as lib:
            lib.restore(bookmark_id)
            return {"ok": True}

    @app.post("/api/bookmarks/bulk")
    def bulk(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        ids = [int(i) for i in payload.get("ids", [])]
        action = payload.get("action")
        with library() as lib:
            for bookmark_id in ids:
                if action == "delete":
                    lib.delete(bookmark_id)
                elif action == "restore":
                    lib.restore(bookmark_id)
                elif action == "status":
                    lib.update(bookmark_id, status=payload.get("status"))
                elif action == "star":
                    lib.update(bookmark_id, starred=bool(payload.get("starred", True)))
                elif action == "refetch":
                    lib.refetch([bookmark_id])
                elif action == "category":
                    lib.update(bookmark_id, category_id=payload.get("category_id") or 0)
                else:
                    raise ValueError("action must be delete, restore, status, star, category or refetch")
        if action == "refetch":
            kick()
        return {"ok": True, "count": len(ids)}

    @app.post("/api/bookmarks/{bookmark_id}/refetch")
    def refetch(bookmark_id: int) -> dict[str, Any]:
        """Fetch one bookmark again now and return it (waits up to ~40 s)."""
        if Worker is None:
            raise HTTPException(503, 'enrichment is off or the "enrich" extra is not installed')
        with library() as lib:
            lib.refetch([bookmark_id])
        Worker(config).run(only_ids=[bookmark_id], budget=40)
        with library() as lib:
            result = lib.get(bookmark_id)
            if result["merged_into_id"]:
                result = lib.get(result["merged_into_id"]) | {"merged_from": bookmark_id}
            return result

    @app.post("/api/process")
    def process() -> dict[str, Any]:
        """Start the background worker now (it also runs every few minutes)."""
        kick()
        with library() as lib:
            return {"started": worker is not None, "queue": lib.queue_stats()}

    @app.post("/api/ingest/cloudtabs")
    def ingest_cloudtabs() -> JSONResponse:
        with library() as lib:
            try:
                summary = run_import(lib, config)
            except CloudTabsError as exc:
                kind = "permission" if isinstance(exc, CloudTabsPermissionDenied) else "source"
                return JSONResponse({"detail": str(exc), "kind": kind}, status_code=503)
        after_ingest()
        return JSONResponse({"run_id": summary.run_id, "counts": summary.counts, "line": summary.line()})

    # ------------------------------------------------------------------ duplicates

    @app.get("/api/duplicates")
    def duplicates() -> dict[str, Any]:
        with library() as lib:
            return {"clusters": dedupe.clusters(lib)}

    @app.post("/api/duplicates/scan")
    def duplicates_scan() -> dict[str, Any]:
        with library() as lib:
            return dedupe.scan(lib)

    @app.post("/api/duplicates/merge")
    def duplicates_merge(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """{keep: id, ids: [all ids in the group]}: merge the others into `keep`."""
        keep = payload.get("keep")
        if not isinstance(keep, int):
            raise HTTPException(400, "keep must be a bookmark id")
        with library() as lib:
            return lib.merge_bookmarks(keep, [int(i) for i in payload.get("ids", [])], reason=payload.get("reason") or "merged by hand")

    @app.post("/api/duplicates/dismiss")
    def duplicates_dismiss(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        with library() as lib, dbmod.transaction(lib.conn):
            return {"pairs": lib.mark_not_duplicates([int(i) for i in payload.get("ids", [])])}

    @app.post("/api/duplicates/apply")
    def duplicates_apply(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        with library() as lib:
            done = dedupe.apply(lib, float(payload.get("min_confidence", 0.95)), payload.get("tier"))
        return {"groups": len(done), "merges": [i for d in done for i in d["merges"]]}

    @app.post("/api/merges/{merge_id}/unmerge")
    def unmerge(merge_id: int, remember: bool = True) -> dict[str, Any]:
        """`remember=false` (the Undo right after a merge) leaves the pair proposable; otherwise it is
        recorded as not duplicates."""
        with library() as lib:
            result = lib.unmerge(merge_id, remember=remember)
            dedupe.scan(lib)
            return result

    async def upload(request: Request) -> bytes:
        data = await request.body()
        if len(data) > MAX_UPLOAD:
            raise HTTPException(413, f"file is larger than {MAX_UPLOAD // 2**20} MB")
        return data

    @app.post("/api/import/preview")
    async def import_preview(request: Request, name: str, sheet: list[str] | None = Query(None)) -> dict[str, Any]:
        """Detected columns and the first rows of a CSV/XLSX upload (body = the file), for the column picker."""
        from .ingest import files, table

        data = await upload(request)
        fmt = files.format_of(name)
        if fmt not in ("csv", "xlsx"):
            return {"format": fmt, "sheets": []}
        try:
            return {"format": fmt, "sheets": await run_in_threadpool(table.preview, data, fmt, sheet)}
        except table.FileFormatError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/import")
    async def import_file(
        request: Request,
        name: str,
        format: str | None = None,
        force: bool = False,
        include_code: bool = False,
        url_col: str | None = None,
        title_col: str | None = None,
        date_col: str | None = None,
        sheet: list[str] | None = Query(None),
        mtime: float | None = None,
    ) -> JSONResponse:
        """Import one uploaded file (body = its bytes). `mtime` (ms, from the browser) dates its links."""
        from datetime import UTC, datetime

        from .ingest.files import FileOptions, import_data

        data = await upload(request)
        opts = FileOptions(
            include_code=include_code, url_col=url_col, title_col=title_col, date_col=date_col, sheets=sheet, tz=config.display_tz
        )
        when = (datetime.fromtimestamp(mtime / 1000, UTC), "file_mtime") if mtime else None

        def work():
            with library() as lib:
                return import_data(lib, data, name=name.rsplit("/", 1)[-1], fmt=format, when=when, force=force, opts=opts)

        result = await run_in_threadpool(work)
        if result.status == "imported":
            after_ingest()
        code = {"imported": 200, "unchanged": 200, "needs_columns": 409}.get(result.status, 422)
        return JSONResponse({**result.as_dict(), "detail": result.error}, status_code=code)

    @app.post("/api/ingest/clipboard")
    def ingest_clipboard() -> JSONResponse:
        from .ingest.files import import_clipboard
        from .ingest.rtf import FileFormatError

        with library() as lib:
            try:
                result = import_clipboard(lib)
            except FileFormatError as exc:
                return JSONResponse({"detail": str(exc)}, status_code=503)
        if result.status == "imported":
            after_ingest()
        return JSONResponse({**result.as_dict(), "detail": result.error}, status_code=200 if result.status == "imported" else 422)

    # ------------------------------------------------------------------ categories

    @app.get("/api/categories")
    def categories(view: str = "active") -> dict[str, Any]:
        with library() as lib:
            return {"categories": lib.categories(view), "uncategorised": lib.uncategorised_count(view)}

    @app.post("/api/categories")
    def category_add(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        with library() as lib:
            return lib.add_category(payload.get("name", ""), payload.get("description"))

    @app.patch("/api/categories/{category_id}")
    def category_edit(category_id: int, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        with library() as lib:
            return lib.update_category(category_id, payload.get("name"), payload.get("description"), payload.get("position"))

    @app.delete("/api/categories/{category_id}")
    def category_delete(category_id: int) -> dict[str, Any]:
        with library() as lib:
            return {"uncategorised": lib.delete_category(category_id)}

    # ------------------------------------------------------------------ AI proposals (changesets)

    @app.get("/api/changesets")
    def changeset_list(status: str | None = None) -> dict[str, Any]:
        with library() as lib:
            return {"changesets": changesets.list_(lib, status)}

    @app.get("/api/changesets/{changeset_id}")
    def changeset_get(changeset_id: int) -> dict[str, Any]:
        with library() as lib:
            return changesets.get(lib, changeset_id)

    @app.post("/api/changesets/{changeset_id}/decide")
    def changeset_decide(changeset_id: int, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """{decision: approved|rejected|pending, item_ids?: [...], min_confidence?: 0.8, new_values?: {item_id: value}}"""
        decision = payload.get("decision")
        if decision not in ("approved", "rejected", "pending"):
            raise HTTPException(400, "decision must be approved, rejected or pending")
        ids = payload.get("item_ids")
        new_values = {int(k): v for k, v in (payload.get("new_values") or {}).items()}
        with library() as lib:
            count = changesets.decide(lib, changeset_id, decision, [int(i) for i in ids] if ids is not None else None,
                                      payload.get("min_confidence"), new_values)  # fmt: skip
            return {"changed": count, "changeset": changesets.get(lib, changeset_id)}

    @app.post("/api/changesets/{changeset_id}/items/{item_id}/trash")
    def changeset_item_trash(changeset_id: int, item_id: int) -> dict[str, Any]:
        """Move the proposal's bookmark to Trash and drop the proposal; returns the bookmark id (for Undo → restore)."""
        with library() as lib:
            return {"bookmark_id": changesets.trash_item(lib, changeset_id, item_id)}

    @app.post("/api/changesets/{changeset_id}/apply")
    def changeset_apply(changeset_id: int) -> dict[str, Any]:
        with library() as lib:
            return changesets.apply(lib, changeset_id)

    @app.post("/api/changesets/{changeset_id}/revert")
    def changeset_revert(changeset_id: int) -> dict[str, Any]:
        with library() as lib:
            return changesets.revert(lib, changeset_id)

    @app.get("/api/ai/review-stats")
    def ai_review_stats() -> dict[str, Any]:
        with library() as lib:
            return changesets.review_stats(lib)

    @app.get("/api/ai/status")
    def ai_status() -> dict[str, Any]:
        from .ai import models

        return {"parts": models.status(config)}

    @app.get("/api/ai/jobs")
    def ai_jobs(limit: int = 20) -> dict[str, Any]:
        from .ai import jobs

        with library() as lib:
            return {"jobs": jobs.list_(lib, limit), "rate": jobs.rate(lib, "categorize")}

    @app.post("/api/ai/jobs")
    def ai_job_start(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """{kind: taxonomy|categorize, params?: {...}}: queue a job; a detached runner (`bmk ai run-jobs`) runs it,
        so it carries on if the server stops. Check it with GET /api/ai/jobs/{id}."""
        from .ai import jobs

        kind = payload.get("kind")
        params = payload.get("params") or {}
        if kind == "taxonomy":
            params = {"min": int(params.get("min", 8)), "max": int(params.get("max", 15))}
        elif kind == "categorize":
            params = {"recheck_ai": bool(params.get("recheck_ai"))}
        with library() as lib:
            job_id = jobs.create(lib, kind, params, author="web")
        jobs.spawn(config)
        with library() as lib:
            return jobs.get(lib, job_id)

    @app.get("/api/ai/jobs/{job_id}")
    def ai_job(job_id: int) -> dict[str, Any]:
        from .ai import jobs

        with library() as lib:
            return jobs.get(lib, job_id)

    @app.post("/api/ai/jobs/{job_id}/{action}")
    def ai_job_control(job_id: int, action: str) -> dict[str, Any]:
        """pause | resume | cancel"""
        from .ai import jobs

        with library() as lib:
            job = jobs.control(lib, job_id, action)
        if action == "resume":
            jobs.spawn(config)
            with library() as lib:
                job = jobs.get(lib, job_id)
        return job

    @app.post("/api/bookmarks")
    def add(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        with library() as lib:
            outcome, bookmark_id = lib.add(payload.get("url", ""), title=payload.get("title"), remarks=payload.get("remarks"), saved_at=payload.get("saved_at"))
        after_ingest()
        return {"outcome": outcome, "id": bookmark_id}

    return app
