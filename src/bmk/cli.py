"""`bmk` command line: a thin adapter over bmk.service.Library."""

import json
import logging
import sys
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich import box
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from . import __version__, urls
from .config import get_config
from .ingest.cloudtabs import CloudTabsError, CloudTabsIngester
from .service import Conflict, Library, NotFound
from .timeutil import display

app = typer.Typer(help="Local-first bookmarks manager.", no_args_is_help=True, add_completion=False)
ingest_app = typer.Typer(help="Import bookmarks from a source.", no_args_is_help=True)
app.add_typer(ingest_app, name="ingest")

console = Console()
err = Console(stderr=True)


def _lib() -> Library:
    return Library.open(actor="cli")


def _fail(message: str, code: int = 1) -> None:
    err.print(f"[red]error:[/red] {message}", highlight=False, soft_wrap=True)
    raise typer.Exit(code)


def _table(rows: list[dict], tz: str) -> Table:
    table = Table(show_lines=False, header_style="bold")
    for col in ("ID", "Saved at", "Title", "Domain", "Status"):
        table.add_column(col, overflow="fold" if col == "Title" else "ellipsis", no_wrap=col != "Title")
    for r in rows:
        table.add_row(
            str(r["id"]),
            display(r["saved_at"], r["saved_at_source"], tz),
            r["title"] or f"[dim]{escape(urls.fallback_title(r['url']))}[/dim]",
            r["domain"] or "",
            r["status"] + (" (deleted)" if r["deleted_at"] else ""),
        )
    return table


@app.callback()
def main(verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")] = False) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")


@app.command()
def version() -> None:
    """Show the version and database location."""
    cfg = get_config()
    console.print(f"bmk {__version__}\ndatabase: {cfg.db_path}")


@app.command()
def init() -> None:
    """Create the database (or apply pending migrations)."""
    lib = _lib()
    from .db import schema_version

    console.print(f"database ready: {lib.config.db_path} (schema v{schema_version(lib.conn)})")


@ingest_app.command("cloudtabs")
def ingest_cloudtabs(
    path: Annotated[Optional[Path], typer.Option(help="CloudTabs.db path (default: auto-detect).")] = None,
    devices: Annotated[Optional[list[str]], typer.Option("--device", help="Only these device names (repeatable).")] = None,
    exclude: Annotated[Optional[list[str]], typer.Option("--exclude-device", help="Skip these device names (repeatable).")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="List tabs without importing.")] = False,
) -> None:
    """Import open tabs from Safari iCloud Tabs (CloudTabs.db)."""
    cfg = get_config()
    ingester = CloudTabsIngester(
        path=path or cfg.cloudtabs_path,
        devices=devices or cfg.cloudtabs_devices,
        exclude_devices=exclude or cfg.cloudtabs_exclude_devices,
    )
    params = {"path": str(ingester.path) if ingester.path else None, "devices": ingester.devices, "exclude": ingester.exclude_devices}
    try:
        items = list(ingester.read())
    except CloudTabsError as exc:
        if not dry_run:
            _lib().record_failed_run("cloudtabs", str(exc), params)
        _fail(str(exc))
    if dry_run:
        for item in items:
            console.print(f"{item.source_ref} | {item.title or ''} | {item.url}", highlight=False, soft_wrap=True)
        console.print(f"{len(items)} tabs")
        return
    summary = _lib().ingest("cloudtabs", items, params)
    console.print(summary.line())


@app.command()
def add(
    url: str,
    title: Annotated[Optional[str], typer.Option()] = None,
    remarks: Annotated[Optional[str], typer.Option()] = None,
) -> None:
    """Add a single URL."""
    try:
        outcome, bookmark_id = _lib().add(url, title=title, remarks=remarks)
    except ValueError as exc:
        _fail(str(exc))
    console.print(f"{outcome}: bookmark {bookmark_id}")


@app.command("list")
def list_cmd(
    limit: Annotated[int, typer.Option("--limit", "-n")] = 50,
    offset: int = 0,
    view: Annotated[str, typer.Option(help="active | trash | date_unknown | all")] = "active",
    source: Annotated[Optional[str], typer.Option(help="Filter by source type, e.g. cloudtabs.")] = None,
    device: Annotated[Optional[str], typer.Option(help="Filter by source ref, e.g. a device name.")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List bookmarks, newest first (unknown dates last)."""
    lib = _lib()
    try:
        rows = lib.list(limit=limit, offset=offset, view=view, source_type=source, source_ref=device)
    except ValueError as exc:
        _fail(str(exc))
    if as_json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return
    console.print(_table(rows, lib.config.display_tz))
    console.print(f"{len(rows)} of {lib.count(view)}")


@app.command()
def search(
    query: str,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 50,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Full-text search over title, description, text, remarks and URL."""
    lib = _lib()
    rows = lib.search(query, limit=limit)
    if as_json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return
    console.print(_table(rows, lib.config.display_tz))


@app.command()
def show(bookmark_id: int, as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Show one bookmark with its sources, aliases and history."""
    lib = _lib()
    try:
        b = lib.get(bookmark_id)
    except NotFound as exc:
        _fail(str(exc))
    if as_json:
        print(json.dumps(b, indent=2, ensure_ascii=False))
        return
    tz = lib.config.display_tz
    console.print(f"[bold]{b['title'] or '(untitled)'}[/bold]  #{b['id']}", highlight=False)
    console.print(f"url:        {b['url']}", highlight=False, soft_wrap=True)
    console.print(f"canonical:  {b['url_canonical']}", highlight=False, soft_wrap=True)
    console.print(f"saved at:   {display(b['saved_at'], b['saved_at_source'], tz)} ({b['saved_at_source']})")
    console.print(f"status:     {b['status']}{'  [red]DELETED ' + b['deleted_at'] + '[/red]' if b['deleted_at'] else ''}")
    console.print(f"locked:     {', '.join(b['locked_fields']) or '—'}")
    if b["remarks"]:
        console.print(f"remarks:    {b['remarks']}", highlight=False)
    console.print("sources:")
    for s in b["sources"]:
        console.print(
            f"  - {s['source_type']} · {s['source_ref'] or '—'} · seen {display(s['first_seen_at'], 'exact', tz)} → "
            f"{display(s['last_seen_at'], 'exact', tz)}",
            highlight=False,
        )
    if b["events"]:
        console.print("history:")
        for e in b["events"]:
            console.print(f"  - {display(e['at'], 'exact', tz)} {e['actor']} {e['kind']} {e['data_json'] or ''}", highlight=False)


@app.command()
def edit(
    bookmark_id: int,
    title: Annotated[Optional[str], typer.Option()] = None,
    url: Annotated[Optional[str], typer.Option()] = None,
    remarks: Annotated[Optional[str], typer.Option()] = None,
    saved_at: Annotated[Optional[str], typer.Option(help="ISO-8601, e.g. 2026-10-01T09:00+08:00")] = None,
    status: Annotated[Optional[str], typer.Option(help="inbox | read | archived")] = None,
) -> None:
    """Edit fields. Edited fields are locked against automatic overwrites."""
    try:
        b = _lib().update(bookmark_id, title=title, url=url, remarks=remarks, saved_at=saved_at, status=status)
    except (NotFound, Conflict, ValueError) as exc:
        _fail(str(exc))
    console.print(f"updated #{b['id']} (locked: {', '.join(b['locked_fields']) or '—'})")


@app.command("rm")
def rm(bookmark_ids: list[int]) -> None:
    """Move bookmarks to Trash (soft delete; the source is never touched)."""
    lib = _lib()
    for bookmark_id in bookmark_ids:
        try:
            lib.delete(bookmark_id)
        except NotFound as exc:
            _fail(str(exc))
        console.print(f"deleted #{bookmark_id}")


@app.command()
def restore(bookmark_ids: list[int]) -> None:
    """Restore bookmarks from Trash."""
    lib = _lib()
    for bookmark_id in bookmark_ids:
        try:
            lib.restore(bookmark_id)
        except NotFound as exc:
            _fail(str(exc))
        console.print(f"restored #{bookmark_id}")


@app.command()
def runs(
    run_id: Annotated[Optional[int], typer.Argument(help="Show item outcomes for this run.")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 20,
) -> None:
    """Show recent import runs, or the per-item outcomes of one run."""
    lib = _lib()
    if run_id is not None:
        table = Table("Outcome", "Locator", "URL", "Error", header_style="bold")
        for i in lib.import_items(run_id):
            table.add_row(i["outcome"], i["locator"] or "", i["raw_url"] or "", i["error"] or "")
        console.print(table)
        return
    # Short labels and a borderless table so a run fits on one line in an 80-column terminal.
    table = Table(box=box.SIMPLE_HEAD, header_style="bold")
    for col in ("Run", "Source", "Started", "Seen", "New", "Match", "Same", "Skip/Inv", "Supp"):
        table.add_column(col, no_wrap=True, justify="left" if col in ("Source", "Started") else "right")
    table.add_column("Error", no_wrap=True, overflow="ellipsis", max_width=60)
    for r in lib.import_runs(limit):
        table.add_row(
            str(r["id"]), r["source_type"], display(r["started_at"], "exact", lib.config.display_tz, short=True), str(r["items_seen"]),
            str(r["items_new"]), str(r["items_matched"]), str(r["items_unchanged"]),
            f"{r['items_skipped']}/{r['items_invalid']}", str(r["items_suppressed"]), (r["error"] or "").split("\n")[0],
        )
    console.print(table)


@app.command()
def export(
    output: Annotated[Optional[Path], typer.Option("--output", "-o", help="File to write (default: stdout).")] = None,
    fmt: Annotated[str, typer.Option("--format", help="json (full backup)")] = "json",
) -> None:
    """Export the library. 'json' is a full backup restorable with `bmk import-backup`."""
    if fmt != "json":
        _fail(f"format '{fmt}' is not available yet (CG4); use json")
    text = json.dumps(_lib().export_backup(), indent=2, ensure_ascii=False)
    if output:
        output.write_text(text + "\n", encoding="utf-8")
        err.print(f"wrote {output}")
    else:
        sys.stdout.write(text + "\n")


@app.command("import-backup")
def import_backup(file: Path) -> None:
    """Restore a JSON backup into an empty database (point BMK_DB_PATH at a fresh file)."""
    data = json.loads(file.read_text(encoding="utf-8"))
    try:
        counts = _lib().import_backup(data)
    except (Conflict, ValueError) as exc:
        _fail(str(exc))
    console.print(", ".join(f"{k}={v}" for k, v in counts.items()))


if __name__ == "__main__":
    app()
