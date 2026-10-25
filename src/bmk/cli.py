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
from . import db as dbmod
from .config import get_config
from .ingest.cloudtabs import CloudTabsError, CloudTabsIngester, run_import
from .service import Conflict, Library, NotFound
from .timeutil import display, now_utc

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
    process_after: Annotated[
        bool, typer.Option("--process/--no-process", help="Then fetch new pages for up to BMK_PROCESS_BUDGET seconds.")
    ] = True,
) -> None:
    """Import open tabs from Safari iCloud Tabs (CloudTabs.db)."""
    cfg = get_config()
    if dry_run:
        ingester = CloudTabsIngester(
            path=path or cfg.cloudtabs_path,
            devices=devices or cfg.cloudtabs_devices,
            exclude_devices=exclude or cfg.cloudtabs_exclude_devices,
        )
        try:
            items = list(ingester.read())
        except CloudTabsError as exc:
            _fail(str(exc))
        for item in items:
            console.print(f"{item.source_ref} | {item.title or ''} | {item.url}", highlight=False, soft_wrap=True)
        console.print(f"{len(items)} tabs")
        return
    try:
        summary = run_import(_lib(), cfg, path, devices, exclude)
    except CloudTabsError as exc:
        _fail(str(exc))
    console.print(f"{now_utc()} {summary.line()}")
    if process_after:
        _auto_process(cfg)


def _auto_process(cfg) -> None:
    """Enrich new bookmarks right after an ingest, and so after every background scan."""
    if not cfg.enrich or cfg.process_budget <= 0:
        return
    try:
        from .enrich.worker import Worker
    except ImportError:  # the "enrich" extra is not installed
        return
    stats = Worker(cfg).run(budget=cfg.process_budget)
    if stats:
        console.print(f"{now_utc()} enrich " + " ".join(f"{k}={v}" for k, v in sorted(stats.items())))


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
    view: Annotated[str, typer.Option(help="active | inbox | kept | trash | date_unknown | all")] = "active",
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
    status: Annotated[Optional[str], typer.Option(help="inbox | kept")] = None,
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
def keep(bookmark_ids: list[int]) -> None:
    """Keep bookmarks: take them out of the Inbox into Kept."""
    lib = _lib()
    for bookmark_id in bookmark_ids:
        try:
            lib.update(bookmark_id, status="kept")
        except NotFound as exc:
            _fail(str(exc))
        console.print(f"kept #{bookmark_id}")


@app.command()
def star(
    bookmark_ids: list[int],
    off: Annotated[bool, typer.Option("--off", help="Remove the star (the bookmark stays kept).")] = False,
) -> None:
    """Star bookmarks: keep them and add them to the Starred list."""
    lib = _lib()
    for bookmark_id in bookmark_ids:
        try:
            lib.update(bookmark_id, starred=not off)
        except NotFound as exc:
            _fail(str(exc))
        console.print(f"{'unstarred' if off else 'starred'} #{bookmark_id}")


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


_OUTCOME_STYLE = {"fetched": "green", "partial": "yellow", "skipped": "dim", "needs_browser": "magenta", "failed": "red", "retry": "yellow"}


@app.command()
def process(
    bookmark_ids: Annotated[Optional[list[int]], typer.Argument(help="Only these bookmarks (fetched again even if done).")] = None,
    limit: Annotated[Optional[int], typer.Option("--limit", "-n", help="Stop after this many bookmarks.")] = None,
    budget: Annotated[Optional[float], typer.Option(help="Stop after this many seconds.")] = None,
    retry_failed: Annotated[bool, typer.Option("--retry-failed", help="Queue failed and blocked bookmarks again first.")] = False,
) -> None:
    """Fetch pages: resolve short links and add titles, summaries, images and text."""
    cfg = get_config()
    if not cfg.enrich:
        _fail("enrichment is turned off (BMK_ENRICH=false)")
    try:
        from .enrich.worker import Worker
    except ImportError:
        _fail('enrichment needs the "enrich" extra: uv pip install -e ".[enrich]"')
    lib = _lib()
    try:
        if retry_failed:
            console.print(f"queued {lib.retry_failed()} failed or blocked bookmarks again")
        if bookmark_ids:
            lib.refetch(bookmark_ids)
        with dbmod.transaction(lib.conn):
            lib.enqueue_missing()
        queue = lib.queue_stats()
    except NotFound as exc:
        _fail(str(exc))
    finally:
        lib.close()
    console.print(f"{queue['due']} to fetch, {queue['waiting']} waiting to retry")

    def show(bookmark_id: int, outcome) -> None:
        detail = outcome.error or (outcome.meta.title if outcome.meta and outcome.meta.title else "") or ""
        style = _OUTCOME_STYLE.get(outcome.status, "")
        console.print(f"#{bookmark_id:<5} [{style}]{outcome.status:<13}[/{style}] {escape(detail[:100])}", highlight=False)

    stats = Worker(cfg).run(budget=budget, max_jobs=limit, only_ids=bookmark_ids, on_result=show)
    console.print(" ".join(f"{k}={v}" for k, v in sorted(stats.items())) or "nothing to do")


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
        table.add_column(col, no_wrap=True, min_width=len(col), justify="left" if col in ("Source", "Started") else "right")
    table.add_column("Error", no_wrap=True, overflow="ellipsis", min_width=5, max_width=60)
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


@app.command()
def serve(
    port: Annotated[Optional[int], typer.Option(help="Port on 127.0.0.1 (default: BMK_PORT or 8765).")] = None,
    open_browser: Annotated[bool, typer.Option("--open", help="Open the UI in the default browser.")] = False,
) -> None:
    """Run the web UI and API on 127.0.0.1."""
    try:
        import uvicorn

        from .api import create_app
    except ImportError:
        _fail('the web UI needs the "web" extra: uv pip install -e ".[web]"')
    cfg = get_config()
    port = port or cfg.port
    url = f"http://127.0.0.1:{port}/"
    console.print(f"bmk UI at {url}  (Ctrl-C to stop)")
    if open_browser:
        import threading
        import webbrowser

        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    uvicorn.run(create_app(cfg, background=True), host="127.0.0.1", port=port, log_level="warning")


_STATUS_STYLE = {"ok": "[green]OK  [/green]", "warn": "[yellow]WARN[/yellow]", "fail": "[red]FAIL[/red]"}


@app.command()
def doctor() -> None:
    """Check database, Full Disk Access, the last scan and the background agent."""
    from .doctor import FAIL, run_checks

    checks = run_checks(get_config())
    for c in checks:
        console.print(f"{_STATUS_STYLE[c.status]} {c.name}: {c.detail}", highlight=False, soft_wrap=True)
        if c.status != "ok" and c.fix:
            for line in c.fix.splitlines():
                console.print(f"       {line}", highlight=False, soft_wrap=True)
    if any(c.status == FAIL for c in checks):
        raise typer.Exit(1)


launchd_app = typer.Typer(help="Background iCloud Tabs scan (launchd agent).", no_args_is_help=True)
app.add_typer(launchd_app, name="launchd")


@launchd_app.command("install")
def launchd_install() -> None:
    """Install (or update) the agent; it scans every BMK_POLL_INTERVAL seconds and at login."""
    from . import launchd

    cfg = get_config()
    try:
        st = launchd.install(cfg)
    except RuntimeError as exc:
        _fail(str(exc))
    console.print(f"installed {st.plist_path} (every {cfg.poll_interval // 60} min, log: {launchd.LOG_PATH})", highlight=False)
    console.print(
        f"Grant Full Disk Access to [bold]{launchd.interpreter()}[/bold]\n"
        "  System Settings → Privacy & Security → Full Disk Access → + → ⌘⇧G and paste the path.\n"
        "  (Granting your terminal is not enough: launchd runs this interpreter directly.)\n"
        "Then check with: bmk doctor",
        highlight=False,
    )


@launchd_app.command("uninstall")
def launchd_uninstall() -> None:
    """Stop and remove the agent."""
    from . import launchd

    launchd.uninstall()
    console.print("uninstalled")


@launchd_app.command("status")
def launchd_status() -> None:
    """Show whether the agent is installed and loaded."""
    from . import launchd

    st = launchd.status()
    console.print(
        f"installed: {st.installed}  loaded: {st.loaded}  last exit: {st.last_exit if st.last_exit is not None else '—'}"
        f"{'  ' + st.detail if st.detail else ''}\nplist: {st.plist_path}\nlog: {launchd.LOG_PATH}",
        highlight=False,
    )


if __name__ == "__main__":
    app()
