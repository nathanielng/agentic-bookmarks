"""Safari iCloud Tabs ingester (PLAN05 §4.2).

CloudTabs.db is undocumented and changes between macOS versions, so the schema is introspected at
runtime and only url, title, device_uuid → device_name, tab_uuid and last_viewed_time are used (all but
url and title optional). Safari's files are
never written: the database is snapshotted with the SQLite backup API (or, failing that, the .db, -wal
and -shm files are copied together) and the snapshot is read.
"""

import logging
import os
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from ..config import CLOUDTABS_CANDIDATES
from ..models import RawItem

log = logging.getLogger(__name__)

FDA_HELP = (
    "macOS blocked access to Safari's data. Grant Full Disk Access to the app running bmk:\n"
    "  System Settings → Privacy & Security → Full Disk Access → enable your terminal app\n"
    "  (Terminal, iTerm, VS Code, …), then quit and reopen it.\n"
    "  For the background scan (bmk launchd), add this Python instead (+, then ⌘⇧G to paste the path):\n"
    f"  {os.path.realpath(sys.executable)}\n"
    "  Shortcut: open 'x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles'"
)


class CloudTabsError(RuntimeError):
    pass


class CloudTabsNotFound(CloudTabsError):
    pass


class CloudTabsPermissionDenied(CloudTabsError):
    def __init__(self, path: Path, detail: str = ""):
        super().__init__(f"Permission denied reading {path}{f' ({detail})' if detail else ''}.\n{FDA_HELP}")
        self.path = path


class CloudTabsSchemaError(CloudTabsError):
    pass


@dataclass
class Tab:
    url: str
    title: str | None
    device_uuid: str | None
    device_name: str | None
    tab_uuid: str | None
    last_viewed: datetime | None = None
    extra: dict = field(default_factory=dict)


MAC_EPOCH_OFFSET = 978307200  # seconds between 1970-01-01 and 2001-01-01 (Mac absolute time)


def _from_mac_time(value) -> datetime | None:
    """Mac absolute time → UTC datetime. 0, NULL and non-numbers mean 'not set'."""
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    return datetime.fromtimestamp(value + MAC_EPOCH_OFFSET, UTC)


def _is_permission_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return isinstance(exc, PermissionError) or "operation not permitted" in text or "authorization denied" in text


def locate(explicit: Path | None = None) -> Path:
    """Return the CloudTabs.db path, raising NotFound or PermissionDenied with guidance."""
    candidates = [explicit] if explicit else CLOUDTABS_CANDIDATES
    denied: Path | None = None
    for path in candidates:
        try:
            if path.is_file():
                return path
            # is_file() returns False on EPERM; probe the directory to tell "missing" from "blocked".
            path.parent.stat()
            list(path.parent.iterdir())
        except OSError as exc:
            if _is_permission_error(exc):
                denied = path
                continue
    if denied:
        raise CloudTabsPermissionDenied(denied)
    raise CloudTabsNotFound(
        "CloudTabs.db not found. Checked:\n  " + "\n  ".join(str(p) for p in candidates)
        + "\nIs iCloud Tabs enabled (System Settings → Apple Account → iCloud → Safari)? Set BMK_CLOUDTABS_PATH to override."
    )


def snapshot(path: Path) -> sqlite3.Connection:
    """Consistent, read-only point-in-time copy of Safari's database, held in memory.

    The .db and -wal files are never modified. Like any SQLite reader, a read-only connection still
    takes read marks in the -shm lock index; that is the same coordination Safari's own readers do.
    """
    mem = sqlite3.connect(":memory:")
    try:
        src = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True)
        try:
            src.backup(mem)
            return mem
        finally:
            src.close()
    except sqlite3.Error as exc:
        if _is_permission_error(exc):
            raise CloudTabsPermissionDenied(path, str(exc)) from exc
        log.info("Read-only open of %s failed (%s); falling back to copying db+wal+shm", path, exc)
    return _snapshot_by_copy(path, mem)


def _snapshot_by_copy(path: Path, mem: sqlite3.Connection) -> sqlite3.Connection:
    with tempfile.TemporaryDirectory(prefix="bmk-cloudtabs-") as tmp:
        dest = Path(tmp) / path.name
        try:
            for suffix in ("", "-wal", "-shm"):
                src_file = Path(f"{path}{suffix}")
                if src_file.exists():
                    shutil.copy2(src_file, f"{dest}{suffix}")
        except OSError as exc:
            if _is_permission_error(exc):
                raise CloudTabsPermissionDenied(path, str(exc)) from exc
            raise CloudTabsError(f"could not copy {path}: {exc}") from exc
        copy = sqlite3.connect(dest)
        try:
            copy.backup(mem)
        finally:
            copy.close()
    return mem


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def read_tabs(conn: sqlite3.Connection) -> list[Tab]:
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "cloud_tabs" not in tables:
        raise CloudTabsSchemaError(f"no cloud_tabs table; found: {', '.join(sorted(tables)) or 'none'}")
    tab_cols = set(_columns(conn, "cloud_tabs"))
    missing = {"url", "title"} - tab_cols
    if missing:
        raise CloudTabsSchemaError(f"cloud_tabs is missing columns: {', '.join(sorted(missing))}")

    select = ["t.url", "t.title"]
    select.append("t.device_uuid" if "device_uuid" in tab_cols else "NULL")
    select.append("t.tab_uuid" if "tab_uuid" in tab_cols else "NULL")
    select.append("t.last_viewed_time" if "last_viewed_time" in tab_cols else "NULL")
    join = ""
    if "cloud_tab_devices" in tables and "device_uuid" in tab_cols:
        dev_cols = set(_columns(conn, "cloud_tab_devices"))
        if {"device_uuid", "device_name"} <= dev_cols:
            select.append("d.device_name")
            join = " LEFT JOIN cloud_tab_devices d ON d.device_uuid = t.device_uuid"
    if not join:
        select.append("NULL")
    order = " ORDER BY t.rowid"
    rows = conn.execute(f"SELECT {', '.join(select)} FROM cloud_tabs t{join}{order}").fetchall()
    return [
        Tab(url=r[0], title=r[1], device_uuid=r[2], tab_uuid=r[3], last_viewed=_from_mac_time(r[4]), device_name=r[5])
        for r in rows
        if r[0]
    ]


def run_import(lib, config, path: Path | None = None, devices: list[str] | None = None, exclude: list[str] | None = None):
    """One poll for the CLI, API and launchd agent. A source that can't be read is recorded as a failed run
    (so the UI can show it) and the CloudTabsError is re-raised."""
    ingester = CloudTabsIngester(
        path=path or config.cloudtabs_path,
        devices=devices or config.cloudtabs_devices,
        exclude_devices=exclude or config.cloudtabs_exclude_devices,
    )
    params = {"path": str(ingester.path) if ingester.path else None, "devices": ingester.devices, "exclude": ingester.exclude_devices}
    try:
        items = list(ingester.read())
    except CloudTabsError as exc:
        lib.record_failed_run(ingester.source_type, str(exc), params)
        raise
    return lib.ingest(ingester.source_type, items, params)


@dataclass
class CloudTabsIngester:
    path: Path | None = None
    devices: list[str] = field(default_factory=list)
    exclude_devices: list[str] = field(default_factory=list)
    source_type: str = "cloudtabs"

    def _wanted(self, device: str) -> bool:
        lowered = device.lower()
        if self.devices and lowered not in {d.lower() for d in self.devices}:
            return False
        return lowered not in {d.lower() for d in self.exclude_devices}

    def tabs(self) -> list[Tab]:
        conn = snapshot(locate(self.path))
        try:
            return read_tabs(conn)
        finally:
            conn.close()

    def read(self) -> Iterator[RawItem]:
        for tab in self.tabs():
            device = tab.device_name or tab.device_uuid or "unknown device"
            if not self._wanted(device):
                continue
            yield RawItem(
                url=tab.url,
                title=(tab.title or "").strip() or None,
                source_ref=device,
                # tab_uuid identifies the tab; fall back to the device UUID so the source row is still stable.
                locator=tab.tab_uuid or tab.device_uuid,
                observed_kind="first_seen",
                # A tab can't have been viewed before it was opened, so this bounds the open time.
                observed_by=tab.last_viewed,
            )
