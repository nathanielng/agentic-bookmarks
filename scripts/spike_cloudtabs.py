#!/usr/bin/env python3
"""Milestone 0 spike: inspect Safari's CloudTabs.db (and SafariTabs.db) read-only.

Prints `device | title | url` for every tab, and optionally:
  --report docs/cloudtabs-schema.md      schema findings (no URLs or titles), for the repo
  --make-fixture tests/fixtures/x.db     a sanitised copy: real schema, synthetic rows

Requires Full Disk Access for the terminal app. Never writes to Safari's files.
"""

import argparse
import platform
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bmk.ingest.cloudtabs import CloudTabsError, locate, read_tabs, snapshot  # noqa: E402

SAFARITABS_CANDIDATES = [
    Path.home() / "Library/Containers/com.apple.Safari/Data/Library/Safari/SafariTabs.db",
    Path.home() / "Library/Safari/SafariTabs.db",
]


def safari_version() -> str:
    try:
        out = subprocess.run(
            ["defaults", "read", "/Applications/Safari.app/Contents/Info", "CFBundleShortVersionString"],
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def describe(conn: sqlite3.Connection) -> list[str]:
    """Markdown lines describing every table: columns, types and row counts."""
    lines = []
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    for table in tables:
        count = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        lines.append(f"### `{table}` ({count} rows)\n")
        lines.append("| column | type | pk | not null | example kind |")
        lines.append("|---|---|---|---|---|")
        sample = conn.execute(f'SELECT * FROM "{table}" LIMIT 1').fetchone()
        for i, col in enumerate(conn.execute(f'PRAGMA table_info("{table}")').fetchall()):
            kind = "—"
            if sample is not None:
                value = sample[i]
                kind = type(value).__name__ if value is not None else "NULL"
                if isinstance(value, (int, float)) and 6e8 < value < 1e9:
                    kind += " (looks like Mac absolute time: s since 2001)"
                elif isinstance(value, (int, float)) and 1.4e9 < value < 2e9:
                    kind += " (looks like Unix time)"
                elif isinstance(value, bytes):
                    kind += f" ({len(value)} bytes, starts {value[:4].hex()})"
            lines.append(f"| {col[1]} | {col[2] or ''} | {col[5] or ''} | {'yes' if col[3] else ''} | {kind} |")
        lines.append("")
    return lines


MAC_EPOCH_OFFSET = 978307200  # seconds between 1970-01-01 and 2001-01-01 (Mac absolute time)


def _placeholder(declared_type: str):
    """A neutral value for a NOT NULL column with no default, by declared type affinity."""
    t = (declared_type or "").upper()
    if "BLOB" in t:
        return b""
    if "INT" in t or "BOOL" in t:
        return 0
    if "REAL" in t or "FLOA" in t or "DOUB" in t:
        return 0.0
    return ""


def _synthetic_row(conn: sqlite3.Connection, table: str, values: dict) -> dict:
    """Given values, plus placeholders for NOT NULL columns without a default; other columns are
    omitted so SQLite applies their default (or NULL)."""
    row = {}
    for _cid, name, ctype, notnull, default, _pk in conn.execute(f'PRAGMA table_info("{table}")'):
        if name in values:
            row[name] = values[name]
        elif notnull and default is None:
            row[name] = _placeholder(ctype)
    return row


def _insert(out: sqlite3.Connection, table: str, row: dict) -> None:
    out.execute(f'INSERT INTO "{table}" ({", ".join(row)}) VALUES ({", ".join("?" * len(row))})', list(row.values()))


def make_fixture(conn: sqlite3.Connection, dest: Path) -> None:
    """Copy the real schema with synthetic rows (no real URLs, titles, devices or blobs)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    out = sqlite3.connect(dest)
    for (sql,) in conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"):
        out.execute(sql)
    for (sql,) in conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"):
        out.execute(sql)
    base = datetime(2026, 10, 1, tzinfo=UTC).timestamp() - MAC_EPOCH_OFFSET
    devices = [("DEV-PHONE", "Test iPhone"), ("DEV-MAC", "Test Mac")]
    for uuid, name in devices:
        _insert(out, "cloud_tab_devices", _synthetic_row(conn, "cloud_tab_devices", {
            "device_uuid": uuid, "device_name": name, "last_modified": base}))
    tabs = [
        ("DEV-PHONE", "Example Domain", "https://example.com/"),
        ("DEV-PHONE", "", "https://lnkd.in/p/gfiEPStM"),
        ("DEV-PHONE", "A post on X", "https://x.com/someone/status/123?s=20"),
        ("DEV-PHONE", "Not a web page", "about:blank"),
        ("DEV-MAC", "A post on X", "https://twitter.com/someone/status/123?s=46"),
    ]
    for i, (dev, title, url) in enumerate(tabs):
        _insert(out, "cloud_tabs", _synthetic_row(conn, "cloud_tabs", {
            "tab_uuid": f"TAB-{i}", "device_uuid": dev, "title": title, "url": url, "last_viewed_time": base + i * 3600}))
    out.commit()
    out.close()


def timestamp_ranges(conn: sqlite3.Connection) -> list[str]:
    """Min/max of REAL columns that look like Mac absolute time, as UTC — to help pin down their meaning."""
    lines = []
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'cloud_tab%'")]
    for table in tables:
        for col in conn.execute(f'PRAGMA table_info("{table}")').fetchall():
            if (col[2] or "").upper() != "REAL":
                continue
            lo, hi, n = conn.execute(f'SELECT MIN("{col[1]}"), MAX("{col[1]}"), COUNT("{col[1]}") FROM "{table}"').fetchone()
            if lo is None:
                continue
            fmt = lambda v: datetime.fromtimestamp(v + MAC_EPOCH_OFFSET, UTC).isoformat(timespec="seconds")
            lines.append(f"| `{table}.{col[1]}` | {n} | {fmt(lo)} | {fmt(hi)} |")
    if not lines:
        return []
    return ["## Timestamp columns (interpreted as Mac absolute time)", "",
            "| column | non-null | min (UTC) | max (UTC) |", "|---|---|---|---|", *lines, ""]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--path", type=Path, help="CloudTabs.db path (default: auto-detect)")
    parser.add_argument("--report", type=Path, help="write schema findings (Markdown) here")
    parser.add_argument("--make-fixture", type=Path, help="write a sanitised fixture database here")
    parser.add_argument("--limit", type=int, default=0, help="print at most N tabs (0 = all)")
    args = parser.parse_args()

    try:
        path = locate(args.path)
        conn = snapshot(path)
        tabs = read_tabs(conn)
    except CloudTabsError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 2

    shown = tabs[: args.limit] if args.limit else tabs
    for tab in shown:
        print(f"{tab.device_name or tab.device_uuid} | {tab.title or ''} | {tab.url}")
    print(f"\n{len(tabs)} tabs from {path}", file=sys.stderr)

    if args.report:
        lines = [
            "# CloudTabs.db findings (Milestone 0 spike)",
            "",
            f"- Generated: {datetime.now(UTC).isoformat(timespec='seconds')}",
            f"- macOS {platform.mac_ver()[0]}, Safari {safari_version()}",
            f"- Path: `{str(path).replace(str(Path.home()), '~')}`",
            f"- Tabs: {len(tabs)} across {len({t.device_uuid for t in tabs})} devices",
            "- Read via SQLite backup API snapshot (Safari's files untouched)",
            "",
            "## Schema",
            "",
            *describe(conn),
            *timestamp_ranges(conn),
        ]
        for st_path in SAFARITABS_CANDIDATES:
            try:
                st = snapshot(st_path) if st_path.is_file() else None
            except (CloudTabsError, OSError):
                st = None
            if st is not None:
                lines += ["## SafariTabs.db (local tabs & tab groups — possible later source)", "",
                          f"- Path: `{str(st_path).replace(str(Path.home()), '~')}`", "", *describe(st)]
                st.close()
                break
        lines += ["## Open questions", "", "- Meaning of any timestamp columns above (verify against a tab opened at a known time).", ""]
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text("\n".join(lines), encoding="utf-8")
        print(f"wrote {args.report}", file=sys.stderr)

    if args.make_fixture:
        make_fixture(conn, args.make_fixture)
        print(f"wrote {args.make_fixture}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
