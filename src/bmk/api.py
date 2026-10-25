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

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__, doctor, launchd
from . import db as dbmod
from .config import Config, get_config
from .ingest.cloudtabs import CloudTabsError, CloudTabsPermissionDenied, run_import
from .models import STATUSES
from .service import Conflict, Library, NotFound

TOKEN_HEADER = "X-BMK-Token"
WRITE_METHODS = {"POST", "PATCH", "PUT", "DELETE"}


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
    def meta(view: str = "active", source_type: str | None = None, source_ref: str | None = None) -> dict[str, Any]:
        """Sidebar data. List counts are within the selected source, source counts within the selected list."""
        with library() as lib:
            return {
                "counts": lib.view_counts(source_type, source_ref),
                "sources": lib.source_refs(view),
                "last_cloudtabs_run": lib.last_run("cloudtabs"),
                "statuses": list(STATUSES),
                "display_tz": config.display_tz,
                "queue": lib.queue_stats(),
                "enrich": Worker is not None,
                "worker_running": bool(worker and worker.running),
            }

    @app.get("/api/bookmarks")
    def bookmarks(
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
        with library() as lib:
            return lib.browse(
                view=view, q=q, status=status, source_type=source_type, source_ref=source_ref,
                sort=sort, desc=desc, limit=min(max(limit, 1), 500), offset=max(offset, 0),
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
    def export() -> JSONResponse:
        with library() as lib:
            data = lib.export_backup()
        return JSONResponse(data, headers={"Content-Disposition": 'attachment; filename="bmk-backup.json"'})

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
                else:
                    raise ValueError("action must be delete, restore, status, star or refetch")
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
        kick()
        return JSONResponse({"run_id": summary.run_id, "counts": summary.counts, "line": summary.line()})

    @app.post("/api/bookmarks")
    def add(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        with library() as lib:
            outcome, bookmark_id = lib.add(payload.get("url", ""), title=payload.get("title"), remarks=payload.get("remarks"))
        kick()
        return {"outcome": outcome, "id": bookmark_id}

    return app
