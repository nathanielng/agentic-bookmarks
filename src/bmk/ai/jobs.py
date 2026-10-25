"""AI jobs: each run (category list, classification) is a job in its own process, separate from the web
server and the terminal that started it, with a state you can check and control.

States:
- queued: waiting; one job runs at a time, the next starts when the running one ends.
- running: a process is working on it (pid, done/total, heartbeat).
- paused: stopped between bookmarks at your request; resume continues where it left off. Its proposal
  stays 'running' meanwhile, so nothing is proposed twice.
- succeeded / succeeded_with_errors (some bookmarks could not be classified) / failed / cancelled: ended;
  whatever it proposed waits in AI review either way.

A running job whose process has gone (a crash, a reboot) is marked failed by `reap`. Pausing applies to
classification; a category-list job is a single model call, so it can only be cancelled.
"""

import json
import os
import platform
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Any

from .. import changesets
from .. import db as dbmod
from ..config import LOG_DIR
from ..service import Conflict, Library, NotFound
from ..timeutil import now_utc

KINDS = ("taxonomy", "categorize")
ACTIVE = ("queued", "running", "paused")
ENDED = ("succeeded", "succeeded_with_errors", "failed", "cancelled")


def _alive(pid: Any) -> bool:
    try:
        os.kill(int(pid), 0)
    except (TypeError, ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True
    return True


def _row(lib: Library, job_id: int) -> dict[str, Any]:
    row = lib.conn.execute("SELECT * FROM ai_jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise NotFound(f"job {job_id} not found")
    job = dict(row)
    job["params"] = json.loads(job.pop("params_json") or "{}")
    job["stats"] = stats(job)
    return job


def stats(job: dict[str, Any]) -> dict[str, Any]:
    """Timing for estimates: seconds and input tokens per item, model throughput, time left (if running)."""
    done = job["done"] or 0
    out: dict[str, Any] = {"run_seconds": round(job["run_seconds"] or 0, 1), "hardware": job["hardware"]}
    if done and job["run_seconds"]:
        out["seconds_per_item"] = round(job["run_seconds"] / done, 2)
        if job["total"] and job["state"] in ACTIVE:
            out["seconds_left"] = round((job["total"] - done) * job["run_seconds"] / done)
    if done and job["tokens_in"]:
        out["tokens_per_item"] = round(job["tokens_in"] / done)
    if job["model_seconds"] and job["tokens_in"]:
        out["tokens_per_second"] = round(job["tokens_in"] / job["model_seconds"])
    return out


def hardware() -> str:
    """The chip and memory, e.g. 'Apple M5, 24 GB' (timings depend on them)."""
    try:
        chip = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True, timeout=5).stdout.strip()
        mem = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5).stdout.strip())
        return f"{chip or platform.machine()}, {round(mem / 2**30)} GB"
    except (OSError, ValueError, subprocess.SubprocessError):
        return platform.machine()


def rate(lib: Library, kind: str = "categorize") -> dict[str, Any] | None:
    """Seconds per item from the latest finished job of this kind on this machine: to estimate a new run."""
    machine = hardware()
    row = lib.conn.execute(
        "SELECT id FROM ai_jobs WHERE kind=? AND state IN ('succeeded', 'succeeded_with_errors') AND done > 0 AND run_seconds > 0"
        " AND hardware=? ORDER BY id DESC LIMIT 1",
        (kind, machine),
    ).fetchone()
    if row is None:
        return None
    job = _row(lib, row["id"])
    return {"job": job["id"], "hardware": machine, **{k: v for k, v in job["stats"].items() if k != "seconds_left"}}


def _set(lib: Library, job_id: int, **fields: Any) -> None:
    cols = ", ".join(f"{k}=?" for k in fields)
    lib.conn.execute(f"UPDATE ai_jobs SET {cols} WHERE id=?", (*fields.values(), job_id))


def create(lib: Library, kind: str, params: dict | None = None, author: str = "cli") -> int:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    if kind == "categorize" and not lib.categories():
        raise ValueError("there are no categories yet; propose them first (bmk ai taxonomy) and approve them")
    cur = lib.conn.execute(
        "INSERT INTO ai_jobs (kind, state, params_json, author, created_at) VALUES (?, 'queued', ?, ?, ?)",
        (kind, json.dumps(params or {}), author, now_utc()),
    )
    return cur.lastrowid


def get(lib: Library, job_id: int) -> dict[str, Any]:
    reap(lib)
    return _row(lib, job_id)


def list_(lib: Library, limit: int = 20) -> list[dict[str, Any]]:
    reap(lib)
    return [_row(lib, r[0]) for r in lib.conn.execute("SELECT id FROM ai_jobs ORDER BY id DESC LIMIT ?", (limit,))]


def active(lib: Library) -> list[dict[str, Any]]:
    reap(lib)
    marks = ",".join("?" * len(ACTIVE))
    return [_row(lib, r[0]) for r in lib.conn.execute(f"SELECT id FROM ai_jobs WHERE state IN ({marks}) ORDER BY id", ACTIVE)]


def _close_changeset(lib: Library, changeset_id: int | None) -> None:
    """The job ended without finishing: what it proposed so far goes to review; an empty proposal fails."""
    if changeset_id is None:
        return
    cs = lib.conn.execute("SELECT status FROM changesets WHERE id=?", (changeset_id,)).fetchone()
    if cs is None or cs["status"] != "running":
        return
    items = lib.conn.execute("SELECT COUNT(*) FROM changeset_items WHERE changeset_id=?", (changeset_id,)).fetchone()[0]
    changesets.set_status(lib, changeset_id, "pending" if items else "failed",
                          summary=f"{items} proposed before the run stopped" if items else None)  # fmt: skip


def reap(lib: Library) -> int:
    """Running jobs whose process has gone are marked failed."""
    gone = [r for r in lib.conn.execute("SELECT id, pid, changeset_id FROM ai_jobs WHERE state='running'").fetchall() if not _alive(r["pid"])]
    for r in gone:
        _set(lib, r["id"], state="failed", error="the job's process stopped unexpectedly", finished_at=now_utc(), pid=None)
        _close_changeset(lib, r["changeset_id"])
    return len(gone)


def control(lib: Library, job_id: int, action: str) -> dict[str, Any]:
    """pause / resume / cancel. A running job acts on pause or cancel between bookmarks."""
    job = get(lib, job_id)
    state = job["state"]
    if action == "pause":
        if job["kind"] != "categorize":
            raise Conflict("only classification jobs can be paused; cancel instead")
        if state == "running":
            _set(lib, job_id, control="pause", message="pausing after the current bookmark…")
        elif state == "queued":
            _set(lib, job_id, state="paused", message="paused before it started")
        else:
            raise Conflict(f"job {job_id} is {state}")
    elif action == "resume":
        if state != "paused":
            raise Conflict(f"job {job_id} is {state}; only a paused job can be resumed")
        _set(lib, job_id, state="queued", control=None, message="queued to resume")
    elif action == "cancel":
        if state == "running":
            _set(lib, job_id, control="cancel", message="cancelling…")
            if job["kind"] == "taxonomy" and _alive(job["pid"]):
                os.kill(job["pid"], signal.SIGTERM)  # one long model call: nothing to wait for
        elif state in ("queued", "paused"):
            _set(lib, job_id, state="cancelled", control=None, finished_at=now_utc(), message="cancelled")
            _close_changeset(lib, job["changeset_id"])
        else:
            raise Conflict(f"job {job_id} is {state}")
    else:
        raise ValueError("action must be pause, resume or cancel")
    return get(lib, job_id)


# ---------------------------------------------------------------------------------------------- running


def spawn(config) -> subprocess.Popen | None:
    """Start a detached runner for the queued jobs, unless one is already running."""
    lib = Library(dbmod.connect(config.db_path, migrate=False), config)
    try:
        jobs = active(lib)
    finally:
        lib.close()
    if any(j["state"] == "running" for j in jobs) or not any(j["state"] == "queued" for j in jobs):
        return None
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_DIR / "ai.log", "ab") as log:
        return subprocess.Popen(
            [sys.executable, "-m", "bmk", "ai", "run-jobs"], stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            env={**os.environ, "BMK_DB_PATH": str(config.db_path)}, start_new_session=True,  # outlives the server
        )  # fmt: skip


def claim(lib: Library, job_id: int | None = None) -> dict[str, Any] | None:
    """Take the next queued job (or `job_id`) for this process; None if another job is running."""
    reap(lib)
    with dbmod.transaction(lib.conn):
        if lib.conn.execute("SELECT 1 FROM ai_jobs WHERE state='running'").fetchone():
            return None
        if job_id is None:
            row = lib.conn.execute("SELECT id FROM ai_jobs WHERE state='queued' ORDER BY id LIMIT 1").fetchone()
        else:
            row = lib.conn.execute("SELECT id FROM ai_jobs WHERE id=? AND state='queued'", (job_id,)).fetchone()
        if row is None:
            return None
        now = now_utc()
        lib.conn.execute(
            "UPDATE ai_jobs SET state='running', pid=?, control=NULL, error=NULL, started_at=COALESCE(started_at, ?), heartbeat_at=?,"
            " hardware=? WHERE id=?",
            (os.getpid(), now, now, hardware(), row["id"]),
        )
    return _row(lib, row["id"])


def execute(lib: Library, config, job: dict[str, Any], *, choose=None, generator=None,
            on_progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:  # fmt: skip
    """Run a claimed job to its end (or pause); returns the job as it ended."""
    from . import categorize, taxonomy

    job_id, params = job["id"], job["params"]
    say = on_progress or (lambda _job: None)
    began, before_run = time.monotonic(), job["run_seconds"] or 0.0

    def elapsed() -> float:
        return before_run + time.monotonic() - began

    try:
        if job["kind"] == "taxonomy":
            def created(cs_id: int) -> None:
                _set(lib, job_id, changeset_id=cs_id)

            def note(message: str) -> None:
                _set(lib, job_id, message=message, heartbeat_at=now_utc(), run_seconds=elapsed())
                say(_row(lib, job_id))

            cs_id = taxonomy.propose(lib, config, generator=generator, lo=params.get("min", taxonomy.MIN_CATEGORIES),
                                     hi=params.get("max", taxonomy.MAX_CATEGORIES), author=job["author"],
                                     on_progress=note, on_created=created)  # fmt: skip
            n = lib.conn.execute("SELECT COUNT(*) FROM changeset_items WHERE changeset_id=?", (cs_id,)).fetchone()[0]
            _set(lib, job_id, state="succeeded", done=1, total=1, message=f"{n} categories proposed", finished_at=now_utc(), run_seconds=elapsed())
        else:
            before_done, before_errors = job["done"], job["errors"]
            before_tokens, before_model = job["tokens_in"], job["model_seconds"]

            def progress(r) -> None:
                _set(lib, job_id, changeset_id=r.changeset_id, done=before_done + r.processed, total=before_done + r.total,
                     errors=before_errors + r.errors, error=r.last_error, heartbeat_at=now_utc(), run_seconds=elapsed(),
                     tokens_in=before_tokens + r.tokens_in, model_seconds=before_model + r.model_seconds,
                     message=f"{r.proposed} proposed, {r.no_fit} fit no category")  # fmt: skip
                say(_row(lib, job_id))

            def should_stop() -> bool:
                return lib.conn.execute("SELECT control FROM ai_jobs WHERE id=?", (job_id,)).fetchone()[0] is not None

            r = categorize.run(lib, config, choose, recheck_ai=params.get("recheck_ai", False), ids=params.get("ids"),
                               limit=params.get("limit"), author=job["author"], changeset_id=job["changeset_id"],
                               on_progress=progress, should_stop=should_stop)  # fmt: skip
            errors = before_errors + r.errors
            if r.changeset_id is None:
                _set(lib, job_id, state="succeeded", done=0, total=0, message="nothing to classify", finished_at=now_utc())
            elif r.stopped:
                wanted = lib.conn.execute("SELECT control FROM ai_jobs WHERE id=?", (job_id,)).fetchone()[0]
                if wanted == "pause":
                    _set(lib, job_id, state="paused", control=None, pid=None, changeset_id=r.changeset_id,
                         message=f"paused at {before_done + r.processed} of {before_done + r.total}")  # fmt: skip
                else:
                    _set(lib, job_id, state="cancelled", control=None, finished_at=now_utc(), message="cancelled")
                    _close_changeset(lib, r.changeset_id)
            else:
                summary = lib.conn.execute("SELECT summary FROM changesets WHERE id=?", (r.changeset_id,)).fetchone()[0]
                _set(lib, job_id, state="succeeded_with_errors" if errors else "succeeded", changeset_id=r.changeset_id,
                     done=before_done + r.processed, total=before_done + r.total, errors=errors, message=summary,
                     finished_at=now_utc())  # fmt: skip
    except KeyboardInterrupt:  # Ctrl-C on a foreground run
        cancelled = job["kind"] == "taxonomy"
        _set(lib, job_id, state="cancelled" if cancelled else "paused", pid=None, control=None,
             message="cancelled" if cancelled else "interrupted; resume to continue")  # fmt: skip
        if cancelled:
            _close_changeset(lib, _row(lib, job_id)["changeset_id"])
        raise
    except Exception as exc:  # noqa: BLE001 - recorded on the job
        wanted = lib.conn.execute("SELECT control FROM ai_jobs WHERE id=?", (job_id,)).fetchone()[0]
        state = "cancelled" if wanted == "cancel" else "failed"
        _set(lib, job_id, state=state, control=None, error=None if state == "cancelled" else str(exc), finished_at=now_utc(),
             message="cancelled" if state == "cancelled" else "failed")  # fmt: skip
        _close_changeset(lib, _row(lib, job_id)["changeset_id"])
    _set(lib, job_id, pid=None, run_seconds=elapsed())
    return _row(lib, job_id)


def run_queued(lib: Library, config, **kwargs) -> list[dict[str, Any]]:
    """Run queued jobs one after another until none is left (or another process is running one)."""
    done = []
    while (job := claim(lib)) is not None:
        done.append(execute(lib, config, job, **kwargs))
    return done
