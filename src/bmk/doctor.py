"""`bmk doctor`: health checks with actionable fixes (PLAN05 §12).

Each check returns ok / warn / fail, a one-line detail and, when not ok, how to fix it. The CLI prints
them; the API serves them to the UI's Health panel.
"""

import importlib.util
import stat
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from . import db as dbmod
from . import launchd
from .config import Config
from .ingest.cloudtabs import FDA_HELP, CloudTabsError, CloudTabsPermissionDenied, locate, read_tabs, snapshot
from .timeutil import parse_iso

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass
class Check:
    name: str
    status: str
    detail: str
    fix: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def check_database(config: Config) -> Check:
    try:
        conn = dbmod.connect(config.db_path)
    except Exception as exc:  # noqa: BLE001 - any failure here is the finding
        return Check("Database", FAIL, f"cannot open {config.db_path}: {exc}", "Check the path and permissions (BMK_DB_PATH).")
    try:
        result = conn.execute("PRAGMA quick_check").fetchone()[0]
        version = dbmod.schema_version(conn)
        count = conn.execute("SELECT COUNT(*) FROM bookmarks WHERE deleted_at IS NULL").fetchone()[0]
    finally:
        conn.close()
    if result != "ok":
        return Check("Database", FAIL, f"integrity check: {result}", "Restore from a backup (bmk import-backup).")
    detail = f"{config.db_path} · schema v{version} · {count} bookmarks"
    mode = stat.S_IMODE(config.db_path.parent.stat().st_mode)
    if mode & 0o077:
        return Check("Database", WARN, f"{detail} · folder is {oct(mode)}", f"chmod 700 '{config.db_path.parent}'")
    return Check("Database", OK, detail)


def check_cloudtabs(config: Config) -> Check:
    try:
        path = locate(config.cloudtabs_path)
        conn = snapshot(path)
        try:
            tabs = read_tabs(conn)
        finally:
            conn.close()
    except CloudTabsPermissionDenied:
        return Check("iCloud Tabs access", FAIL, "macOS blocked access to Safari's data (Full Disk Access).", FDA_HELP)
    except CloudTabsError as exc:
        return Check("iCloud Tabs access", FAIL, str(exc).split("\n")[0], str(exc))
    devices = len({t.device_uuid for t in tabs})
    return Check("iCloud Tabs access", OK, f"{len(tabs)} tabs across {devices} devices · {path}")


def check_last_scan(config: Config) -> Check:
    conn = dbmod.connect(config.db_path)
    try:
        row = conn.execute("SELECT * FROM import_runs WHERE source_type='cloudtabs' ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    if row is None:
        return Check("Last iCloud Tabs scan", WARN, "never run", "Run: bmk ingest cloudtabs")
    when = parse_iso(row["started_at"])
    age_min = int((datetime.now(UTC) - when).total_seconds() // 60)
    detail = f"run {row['id']}, {age_min} min ago"
    if row["error"]:
        first, _, rest = row["error"].partition("\n")
        return Check("Last iCloud Tabs scan", FAIL, f"{detail}: {first}", rest)
    detail += f" · {row['items_new']} new, {row['items_seen']} seen"
    if age_min * 60 > 2 * config.poll_interval:
        return Check("Last iCloud Tabs scan", WARN, f"{detail} (older than two poll intervals)", "Check the background scan below.")
    return Check("Last iCloud Tabs scan", OK, detail)


def check_agent(run: launchd.Runner | None = None) -> Check:
    st = launchd.status(run) if run else launchd.status()
    python = launchd.interpreter()
    fda = f"Full Disk Access must be granted to {python} (System Settings → Privacy & Security → Full Disk Access → +, ⌘⇧G to paste the path)."
    if not st.installed and not st.loaded:
        return Check("Background scan (launchd)", WARN, "not installed", f"Run: bmk launchd install\n{fda}")
    if not st.loaded:
        return Check("Background scan (launchd)", FAIL, f"{st.plist_path} exists but is not loaded", "Run: bmk launchd install")
    if st.last_exit not in (None, 0):
        return Check(
            "Background scan (launchd)", FAIL, f"last run exited with code {st.last_exit}",
            f"See {launchd.LOG_PATH}. If it mentions Full Disk Access: {fda}",
        )
    return Check("Background scan (launchd)", OK, f"loaded{' · ' + st.detail if st.detail else ''} · interpreter {python}")


def check_web() -> Check:
    if importlib.util.find_spec("fastapi") and importlib.util.find_spec("uvicorn"):
        return Check("Web UI dependencies", OK, "fastapi and uvicorn installed")
    return Check("Web UI dependencies", WARN, "fastapi/uvicorn missing", 'uv pip install -e ".[web]"')


def run_checks(config: Config, run: launchd.Runner | None = None) -> list[Check]:
    database = check_database(config)
    checks = [database, check_cloudtabs(config)]
    if database.status != FAIL:
        checks.append(check_last_scan(config))
    checks += [check_agent(run), check_web()]
    return checks
