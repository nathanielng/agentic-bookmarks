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

from . import __version__, doctor
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


def create_app(config: Config | None = None) -> FastAPI:
    config = config or get_config()
    token = load_token(config)
    dbmod.connect(config.db_path).close()  # create / migrate up front, not on the first request
    web_dir = resources.files("bmk") / "web"

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
    def meta() -> dict[str, Any]:
        with library() as lib:
            return {
                "counts": lib.view_counts(),
                "sources": lib.source_refs(),
                "last_cloudtabs_run": lib.last_run("cloudtabs"),
                "statuses": list(STATUSES),
                "display_tz": config.display_tz,
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
                else:
                    raise ValueError("action must be delete, restore or status")
            return {"ok": True, "count": len(ids)}

    @app.post("/api/ingest/cloudtabs")
    def ingest_cloudtabs() -> JSONResponse:
        with library() as lib:
            try:
                summary = run_import(lib, config)
            except CloudTabsError as exc:
                kind = "permission" if isinstance(exc, CloudTabsPermissionDenied) else "source"
                return JSONResponse({"detail": str(exc), "kind": kind}, status_code=503)
            return JSONResponse({"run_id": summary.run_id, "counts": summary.counts, "line": summary.line()})

    @app.post("/api/bookmarks")
    def add(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        with library() as lib:
            outcome, bookmark_id = lib.add(payload.get("url", ""), title=payload.get("title"), remarks=payload.get("remarks"))
            return {"outcome": outcome, "id": bookmark_id}

    return app
