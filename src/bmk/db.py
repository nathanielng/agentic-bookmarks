"""SQLite connection and numbered migrations (tracked with PRAGMA user_version)."""

import logging
import re
import sqlite3
from importlib import resources
from pathlib import Path

log = logging.getLogger(__name__)

_MIGRATION_RE = re.compile(r"^(\d{3})_[\w-]+\.sql$")


def _migrations() -> list[tuple[int, str, str]]:
    found = []
    for entry in resources.files("bmk.migrations").iterdir():
        match = _MIGRATION_RE.match(entry.name)
        if match:
            found.append((int(match.group(1)), entry.name, entry.read_text(encoding="utf-8")))
    return sorted(found)


def latest_version() -> int:
    migrations = _migrations()
    return migrations[-1][0] if migrations else 0


def connect(db_path: Path | str, *, migrate: bool = True) -> sqlite3.Connection:
    """Open the library database, creating its directory (0700) and applying migrations."""
    if str(db_path) != ":memory:":
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    conn = sqlite3.connect(db_path, isolation_level=None)  # autocommit; transactions are explicit
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if str(db_path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    if migrate:
        apply_migrations(conn)
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def apply_migrations(conn: sqlite3.Connection) -> list[int]:
    current = schema_version(conn)
    applied = []
    for version, name, sql in _migrations():
        if version <= current:
            continue
        log.info("Applying migration %s", name)
        # executescript() commits any open transaction; wrap the script so it is all-or-nothing.
        try:
            conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;")
        except sqlite3.Error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        applied.append(version)
    return applied


class transaction:
    """Context manager for an explicit BEGIN IMMEDIATE … COMMIT/ROLLBACK block."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.conn.execute("COMMIT")
        else:
            self.conn.execute("ROLLBACK")
