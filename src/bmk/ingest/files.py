"""Import files and folders: format by extension, one import run per file, files skipped by content hash.

Each file's links get the file's creation time as an approximate save time (`file_birthtime`, or
`file_mtime` when there is none) unless the file states its own (iOS Shortcut lines, a CSV date column).
A file that can't be read is recorded as a failed run and reported; the other files still import.
"""

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..models import ImportSummary, RawItem
from ..timeutil import now_utc
from . import markdown, rtf, table
from .rtf import FileFormatError
from .table import ColumnsNeeded

log = logging.getLogger(__name__)

FORMATS = {
    ".rtf": "rtf",
    ".html": "html",
    ".htm": "html",
    ".md": "markdown",
    ".markdown": "markdown",
    ".txt": "markdown",
    ".csv": "csv",
    ".tsv": "csv",
    ".xlsx": "xlsx",
    ".xlsm": "xlsx",
}


@dataclass
class FileOptions:
    include_code: bool = False
    url_col: str | None = None
    title_col: str | None = None
    date_col: str | None = None
    sheets: list[str] | None = None
    tz: str = "UTC"

    def params(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v not in (None, False, []) and k != "tz"}


@dataclass
class FileResult:
    name: str
    source_type: str | None
    status: str  # imported | unchanged | failed | needs_columns
    summary: ImportSummary | None = None
    error: str | None = None
    warnings: list[str] = field(default_factory=list)
    columns: dict[str, Any] | None = None  # needs_columns: what the column picker shows

    def as_dict(self) -> dict[str, Any]:
        out = {k: v for k, v in self.__dict__.items() if k != "summary"}
        if self.summary:
            out.update(run_id=self.summary.run_id, counts={k: v for k, v in self.summary.counts.items() if v})
        return out

    def line(self) -> str:
        if self.summary:
            return f"{self.name}: {self.summary.line()}"
        return f"{self.name}: {self.status}" + (f" ({self.error})" if self.error else "")


def format_of(name: str) -> str | None:
    return FORMATS.get(Path(name).suffix.lower())


def parse(fmt: str, data: bytes, opts: FileOptions) -> tuple[list[RawItem], list[str]]:
    if fmt == "rtf":
        return rtf.parse_rtf(data)
    if fmt == "html":
        return rtf.parse_html(data)
    if fmt == "markdown":
        return markdown.parse_markdown(data, include_code=opts.include_code, tz=opts.tz)
    if fmt in ("csv", "xlsx"):
        return table.parse_table(
            data, fmt, url_col=opts.url_col, title_col=opts.title_col, date_col=opts.date_col, sheets=opts.sheets, tz=opts.tz
        )
    raise FileFormatError(f"unknown format '{fmt}' (known: {', '.join(sorted(set(FORMATS.values())))})")


def expand(paths: list[Path]) -> list[Path]:
    """Files as given, and every known file under each folder (recursively, hidden ones skipped)."""
    out = []
    for path in paths:
        if path.is_dir():
            out += sorted(
                p
                for p in path.rglob("*")
                if p.is_file() and p.suffix.lower() in FORMATS and not any(part.startswith(".") for part in p.relative_to(path).parts)
            )
        else:
            out.append(path)
    return out


def file_time(path: Path) -> tuple[datetime, str]:
    st = path.stat()
    if birth := getattr(st, "st_birthtime", None):
        return datetime.fromtimestamp(birth, UTC), "file_birthtime"
    return datetime.fromtimestamp(st.st_mtime, UTC), "file_mtime"


def import_data(
    lib,
    data: bytes,
    *,
    name: str,
    fmt: str | None = None,
    when: tuple[datetime, str] | None = None,
    force: bool = False,
    opts: FileOptions | None = None,
) -> FileResult:
    """Import one file's bytes. `name` is the source_ref (a path, or an uploaded file's name)."""
    opts = opts or FileOptions()
    fmt = fmt or format_of(name)
    if fmt is None:
        return FileResult(name, None, "failed", error=f"unknown file type (known: {', '.join(sorted(FORMATS))})")
    digest = hashlib.sha256(data).hexdigest()
    params = {"path": name, "sha256": digest, **opts.params()}
    if not force:
        seen = lib.conn.execute("SELECT path FROM ingested_files WHERE source_type=? AND sha256=?", (fmt, digest)).fetchone()
        if seen is not None:
            note = "" if seen["path"] == name else f"same content as {seen['path']}"
            return FileResult(name, fmt, "unchanged", error=note or None)
    try:
        items, warnings = parse(fmt, data, opts)
    except ColumnsNeeded as exc:
        return FileResult(name, fmt, "needs_columns", error=str(exc), columns=exc.as_dict())
    except FileFormatError as exc:
        lib.record_failed_run(fmt, f"{name}: {exc}", params)
        return FileResult(name, fmt, "failed", error=str(exc))
    except Exception as exc:  # a parser bug must not stop the other files
        log.exception("Failed to parse %s", name)
        lib.record_failed_run(fmt, f"{name}: {exc}", params)
        return FileResult(name, fmt, "failed", error=f"internal error: {exc}")
    for item in items:
        item.source_ref = name
        if item.event_at is None and when is not None:
            item.event_at, item.event_at_kind = when
    if warnings:
        params["warnings"] = warnings[:50]
    summary = lib.ingest(fmt, items, params)
    lib.conn.execute(
        "INSERT INTO ingested_files (source_type, sha256, path, size, import_run_id, ingested_at) VALUES (?, ?, ?, ?, ?, ?)"
        " ON CONFLICT (source_type, sha256) DO UPDATE SET path=excluded.path, import_run_id=excluded.import_run_id,"
        " ingested_at=excluded.ingested_at",
        (fmt, digest, name, len(data), summary.run_id, now_utc()),
    )
    return FileResult(name, fmt, "imported", summary=summary, warnings=warnings)


def import_paths(lib, paths: list[Path], *, fmt: str | None = None, force: bool = False, opts: FileOptions | None = None) -> list[FileResult]:
    results = []
    for path in expand(paths):
        name = str(path.expanduser().resolve())
        try:
            data = path.read_bytes()
            when = file_time(path)
        except OSError as exc:
            results.append(FileResult(name, fmt or format_of(name), "failed", error=exc.strerror or str(exc)))
            continue
        results.append(import_data(lib, data, name=name, fmt=fmt, when=when, force=force, opts=opts))
    return results


def import_clipboard(lib, kind: str | None = None, data: bytes | None = None) -> FileResult:
    """Links on the clipboard (HTML, then RTF, then plain text), e.g. tabs copied from Safari.

    Copying is an observation, not a stated save time, so the items are `first_seen` now.
    """
    if data is None:
        kind, data = rtf.read_clipboard()
    stamp = now_utc()
    try:
        items, warnings = rtf.parse_clipboard(kind, data)
    except FileFormatError as exc:
        lib.record_failed_run("clipboard", str(exc), {"flavour": kind})
        return FileResult("clipboard", "clipboard", "failed", error=str(exc))
    for item in items:
        item.source_ref, item.locator, item.observed_kind = "clipboard", f"{stamp} {item.locator}", "first_seen"
    params = {"flavour": kind, **({"warnings": warnings[:50]} if warnings else {})}
    summary = lib.ingest("clipboard", items, params)
    return FileResult("clipboard", "clipboard", "imported", summary=summary, warnings=warnings)
