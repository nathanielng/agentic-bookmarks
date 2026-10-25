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
categories_app = typer.Typer(help="Categories: list, add, rename, remove, assign.", invoke_without_command=True)
app.add_typer(categories_app, name="categories")
ai_app = typer.Typer(help="Local AI: propose categories, then classify bookmarks (nothing changes until approved).", no_args_is_help=True)
app.add_typer(ai_app, name="ai")
changeset_app = typer.Typer(help="Review AI and agent proposals: show, approve, apply, revert.", no_args_is_help=True)
app.add_typer(changeset_app, name="changeset")
dedupe_app = typer.Typer(help="Find and review duplicates (merges can be undone).", no_args_is_help=True)
app.add_typer(dedupe_app, name="dedupe")

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
    """Enrich new bookmarks right after an ingest (and so after every background scan), then look for duplicates."""
    try:
        _enrich_after_ingest(cfg)
    finally:
        from . import dedupe

        lib = _lib()
        try:
            dedupe.scan(lib)
        finally:
            lib.close()


def _enrich_after_ingest(cfg) -> None:
    if not cfg.enrich or cfg.process_budget <= 0:
        return
    try:
        from .enrich.worker import Worker
    except ImportError:  # the "enrich" extra is not installed
        return
    stats = Worker(cfg).run(budget=cfg.process_budget)
    if stats:
        console.print(f"{now_utc()} enrich " + " ".join(f"{k}={v}" for k, v in sorted(stats.items())))


_PROCESS = Annotated[bool, typer.Option("--process/--no-process", help="Then fetch new pages for up to BMK_PROCESS_BUDGET seconds.")]


def _print_file_results(results, tz: str) -> bool:
    """One line per file, warnings and the column picker's preview; True if every file imported or was unchanged."""
    ok = True
    for res in results:
        style = {"imported": "", "unchanged": "dim", "failed": "red", "needs_columns": "yellow"}[res.status]
        line = f"{now_utc()} {res.line()}"
        console.print(f"[{style}]{escape(line)}[/{style}]" if style else escape(line), highlight=False, soft_wrap=True)
        for warning in res.warnings[:10]:
            err.print(f"  [yellow]warning:[/yellow] {escape(warning)}", highlight=False)
        if len(res.warnings) > 10:
            err.print(f"  … {len(res.warnings) - 10} more warnings")
        if res.columns:
            table = Table(*[f"{n}. {h}" for n, h in enumerate(res.columns["headers"], 1)], header_style="bold", title=res.columns["sheet"])
            for row in res.columns["preview"]:
                table.add_row(*[escape(str(v))[:40] for v in row])
            console.print(table)
        ok = ok and res.status in ("imported", "unchanged")
    return ok


@ingest_app.command("file")
def ingest_file(
    paths: Annotated[list[Path], typer.Argument(help="Files or folders (folders are read recursively).")],
    fmt: Annotated[Optional[str], typer.Option("--format", help="rtf, html, markdown, csv or xlsx (default: by extension).")] = None,
    force: Annotated[bool, typer.Option("--force", help="Read files again even if their content was imported before.")] = False,
    include_code: Annotated[bool, typer.Option("--include-code", help="Markdown: also take URLs in code.")] = False,
    url_col: Annotated[Optional[str], typer.Option("--url-col", help="CSV/XLSX: URL column (header or number).")] = None,
    title_col: Annotated[Optional[str], typer.Option("--title-col", help="CSV/XLSX: title column.")] = None,
    date_col: Annotated[Optional[str], typer.Option("--date-col", help="CSV/XLSX: save-date column.")] = None,
    sheets: Annotated[Optional[list[str]], typer.Option("--sheet", help="XLSX: only these sheets (repeatable).")] = None,
    process_after: _PROCESS = True,
) -> None:
    """Import links from RTF, HTML, Markdown, CSV or XLSX files."""
    from .ingest.files import FileOptions, import_paths

    cfg = get_config()
    missing = [str(p) for p in paths if not p.exists()]
    if missing:
        _fail(f"not found: {', '.join(missing)}")
    opts = FileOptions(include_code=include_code, url_col=url_col, title_col=title_col, date_col=date_col, sheets=sheets, tz=cfg.display_tz)
    lib = _lib()
    try:
        results = import_paths(lib, paths, fmt=fmt, force=force, opts=opts)
    finally:
        lib.close()
    if not results:
        _fail("no importable files (.rtf .html .md .txt .csv .tsv .xlsx)")
    ok = _print_file_results(results, cfg.display_tz)
    if process_after and any(r.status == "imported" for r in results):
        _auto_process(cfg)
    if not ok:
        raise typer.Exit(1)


def _format_alias(fmt: str, help_text: str):
    def command(
        paths: Annotated[list[Path], typer.Argument(help="Files or folders.")],
        force: Annotated[bool, typer.Option("--force", help="Read files again even if imported before.")] = False,
        include_code: Annotated[bool, typer.Option("--include-code", help="Markdown: also take URLs in code.")] = False,
        url_col: Annotated[Optional[str], typer.Option("--url-col", help="CSV/XLSX: URL column (header or number).")] = None,
        title_col: Annotated[Optional[str], typer.Option("--title-col", help="CSV/XLSX: title column.")] = None,
        date_col: Annotated[Optional[str], typer.Option("--date-col", help="CSV/XLSX: save-date column.")] = None,
        sheets: Annotated[Optional[list[str]], typer.Option("--sheet", help="XLSX: only these sheets.")] = None,
        process_after: _PROCESS = True,
    ) -> None:
        ingest_file(paths, fmt, force, include_code, url_col, title_col, date_col, sheets, process_after)

    command.__doc__ = help_text
    return command


for _fmt, _help in (
    ("rtf", "Import links from RTF files (e.g. tabs pasted into TextEdit)."),
    ("markdown", "Import links from Markdown files or a folder of them (iOS Shortcut inbox.md lines keep their time)."),
    ("csv", "Import a CSV of links (the URL column is detected; override with --url-col)."),
    ("xlsx", "Import an Excel workbook (hyperlink targets are read; --sheet picks sheets)."),
):
    ingest_app.command(_fmt)(_format_alias(_fmt, _help))


@ingest_app.command("clipboard")
def ingest_clipboard(process_after: _PROCESS = True) -> None:
    """Import links on the clipboard (copied from Safari, Notes, a web page…): HTML, then RTF, then plain text."""
    from .ingest.files import import_clipboard
    from .ingest.rtf import FileFormatError

    cfg = get_config()
    lib = _lib()
    try:
        result = import_clipboard(lib)
    except FileFormatError as exc:
        _fail(str(exc))
    finally:
        lib.close()
    ok = _print_file_results([result], cfg.display_tz)
    if process_after and result.status == "imported":
        _auto_process(cfg)
    if not ok:
        raise typer.Exit(1)


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
    fmt: Annotated[str, typer.Option("--format", help="json (full backup), jsonl, csv, markdown or html (Netscape).")] = "json",
    view: Annotated[str, typer.Option(help="List to export (not json): active, inbox, kept, starred, trash, ….")] = "active",
) -> None:
    """Export the library. 'json' is a full backup restorable with `bmk import-backup`; the other formats
    export one list for use elsewhere ('html' imports into any browser)."""
    from . import export as exporter

    lib = _lib()
    if fmt == "json":
        text = json.dumps(lib.export_backup(), indent=2, ensure_ascii=False) + "\n"
    else:
        try:
            text = exporter.render(fmt, exporter.rows(lib, view=view))
        except ValueError as exc:
            _fail(str(exc))
    if output:
        output.write_text(text, encoding="utf-8")
        err.print(f"wrote {output}")
    else:
        sys.stdout.write(text)


def _cluster_table(cluster: dict, tz: str) -> Table:
    pair = cluster["pairs"][0]
    table = Table(title=f"{cluster['confidence']:.2f} · tier {cluster['tier']} · {pair['rationale']}", title_justify="left",
                  box=box.SIMPLE_HEAD, header_style="bold")  # fmt: skip
    for col in ("", "ID", "Title", "URL", "Saved", "Sources"):
        table.add_column(col, overflow="fold")
    for b in cluster["bookmarks"]:
        table.add_row(
            "★" if b["id"] == cluster["suggested"] else "", str(b["id"]), escape(b["title"] or "—"), escape(b["url"]),
            display(b["saved_at"], b["saved_at_source"], tz), str(len(b["sources"])),
        )  # fmt: skip
    return table


@dedupe_app.command("scan")
def dedupe_scan() -> None:
    """Look for likely duplicates (tier 3 URL variants, tier 4 same page) and list them for review."""
    from . import dedupe

    lib = _lib()
    stats = dedupe.scan(lib)
    console.print(f"{stats['clusters']} groups to review ({stats['tier3']} URL variants, {stats['tier4']} same-page pairs)")


@dedupe_app.command("show")
def dedupe_show(min_confidence: Annotated[float, typer.Option("--min-confidence")] = 0.0) -> None:
    """Show proposed duplicate groups (★ = suggested one to keep), most confident first."""
    from . import dedupe

    lib = _lib()
    dedupe.scan(lib)
    groups = [c for c in dedupe.clusters(lib) if c["confidence"] >= min_confidence]
    for cluster in groups:
        console.print(_cluster_table(cluster, lib.config.display_tz))
    console.print(f"{len(groups)} groups. Merge with `bmk dedupe merge KEEP OTHER…`, or `bmk dedupe dismiss ID ID…`.")


@dedupe_app.command("merge")
def dedupe_merge(
    keep: Annotated[int, typer.Argument(help="The bookmark to keep.")],
    others: Annotated[list[int], typer.Argument(help="Bookmarks to merge into it.")],
) -> None:
    """Merge bookmarks into the one to keep (sources, remarks, tags and dates are combined)."""
    lib = _lib()
    try:
        result = lib.merge_bookmarks(keep, others)
    except (NotFound, Conflict, ValueError) as exc:
        _fail(str(exc))
    console.print(f"merged {', '.join(map(str, others))} into {keep} (undo: bmk dedupe unmerge {' '.join(map(str, result['merges']))})")


@dedupe_app.command("dismiss")
def dedupe_dismiss(bookmark_ids: list[int]) -> None:
    """Mark bookmarks as different pages; they are never proposed as duplicates again."""
    lib = _lib()
    with dbmod.transaction(lib.conn):
        n = lib.mark_not_duplicates(bookmark_ids)
    console.print(f"remembered {n} pair(s) as not duplicates")


@dedupe_app.command("apply")
def dedupe_apply(
    min_confidence: Annotated[float, typer.Option("--min-confidence", help="Only groups whose every pair is at least this.")] = 0.95,
    tier: Annotated[Optional[int], typer.Option(help="Only this tier (3 = URL variants).")] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Merge without listing first.")] = False,
) -> None:
    """Merge every proposed group above a confidence, each into its suggested bookmark."""
    from . import dedupe

    lib = _lib()
    dedupe.scan(lib)
    groups = [c for c in dedupe.clusters(lib) if c["min_confidence"] >= min_confidence and (tier is None or all(p["tier"] == tier for p in c["pairs"]))]
    if not groups:
        console.print("nothing to merge")
        return
    if not yes:
        for cluster in groups:
            console.print(_cluster_table(cluster, lib.config.display_tz))
        typer.confirm(f"Merge these {len(groups)} groups?", abort=True)
    done = dedupe.apply(lib, min_confidence, tier)
    console.print(f"merged {len(done)} groups (merge ids {', '.join(str(i) for d in done for i in d['merges'])})")


@dedupe_app.command("unmerge")
def dedupe_unmerge(merge_ids: list[int]) -> None:
    """Undo merges (ids from `bmk dedupe history`); the pair is then never proposed again."""
    lib = _lib()
    for merge_id in merge_ids:
        try:
            result = lib.unmerge(merge_id)
        except (NotFound, Conflict) as exc:
            _fail(str(exc))
        kept = f"; kept later edits to {', '.join(result['kept_edits'])}" if result["kept_edits"] else ""
        console.print(f"merge {merge_id} undone: {result['restored']} is separate from {result['survivor']} again{kept}")


@dedupe_app.command("history")
def dedupe_history(limit: Annotated[int, typer.Option("--limit", "-n")] = 30) -> None:
    """Recent merges, automatic and by hand."""
    lib = _lib()
    table = Table("Merge", "Kept", "Merged", "Tier", "Reason", "When", "Undone", box=box.SIMPLE_HEAD, header_style="bold")
    for m in lib.merges(limit=limit):
        table.add_row(
            str(m["id"]), str(m["survivor_id"]), str(m["loser_id"]), str(m["tier"] or ""), m["reason"],
            display(m["merged_at"], "exact", lib.config.display_tz), display(m["reverted_at"], "exact", lib.config.display_tz) if m["reverted_at"] else "",
        )  # fmt: skip
    console.print(table)


@dedupe_app.command("recanonicalise")
def dedupe_recanonicalise(dry_run: Annotated[bool, typer.Option("--dry-run", help="Report without changing anything.")] = False) -> None:
    """Recompute canonical URLs after editing rules/canonicalize.yaml; bookmarks that now match are merged."""
    from . import dedupe

    lib = _lib()
    result = dedupe.recanonicalise(lib, dry_run=dry_run)
    for change in result["changes"]:
        what = f"merge into {change['merge_with']}" if "merge_with" in change else "update"
        console.print(f"#{change['id']}: {escape(change['from'])} → {escape(change['to'])} ({what})", highlight=False)
    prefix = "would change" if dry_run else "changed"
    console.print(f"{prefix}: {result['canonical']} canonical URLs, {result['loose']} loose keys, {result['merged']} merges, {result['aliases']} new aliases")


# ------------------------------------------------------------------ categories


@categories_app.callback()
def categories_list(ctx: typer.Context) -> None:
    """List categories with their bookmark counts."""
    if ctx.invoked_subcommand:
        return
    lib = _lib()
    table = Table("ID", "Category", "Bookmarks", "Description", "By", box=box.SIMPLE_HEAD, header_style="bold")
    for c in lib.categories():
        table.add_row(str(c["id"]), escape(c["name"]), str(c["bookmarks"]), escape(c["description"] or ""), c["created_by"] or "")
    console.print(table)
    console.print(f"{lib.uncategorised_count()} bookmarks without a category")


@categories_app.command("add")
def categories_add(name: str, description: Annotated[Optional[str], typer.Option("--description", "-d")] = None) -> None:
    lib = _lib()
    try:
        c = lib.add_category(name, description)
    except (Conflict, ValueError) as exc:
        _fail(str(exc))
    console.print(f"added category {c['id']}: {escape(c['name'])}")


@categories_app.command("edit")
def categories_edit(
    category_id: int,
    name: Annotated[Optional[str], typer.Option("--name")] = None,
    description: Annotated[Optional[str], typer.Option("--description", "-d")] = None,
) -> None:
    """Rename a category or change its description (the classifier reads the description)."""
    lib = _lib()
    try:
        c = lib.update_category(category_id, name=name, description=description)
    except (NotFound, Conflict, ValueError) as exc:
        _fail(str(exc))
    console.print(f"category {c['id']}: {escape(c['name'])} — {escape(c['description'] or '')}")


@categories_app.command("rm")
def categories_rm(category_id: int) -> None:
    """Remove a category; its bookmarks become uncategorised."""
    lib = _lib()
    try:
        n = lib.delete_category(category_id)
    except NotFound as exc:
        _fail(str(exc))
    console.print(f"removed category {category_id}; {n} bookmarks are now uncategorised")


@categories_app.command("import")
def categories_import(path: Annotated[Optional[Path], typer.Argument(help="Default: categories.json next to the library.")] = None) -> None:
    """Add the categories from a categories.json (e.g. saved from another library); existing names are skipped."""
    lib = _lib()
    try:
        added = lib.load_categories_file(path)
    except (OSError, ValueError) as exc:
        _fail(str(exc))
    console.print(f"added {len(added)} categories" + (f": {escape(', '.join(added))}" if added else ""))
    console.print(f"saved to {lib.config.categories_path}")


@categories_app.command("set")
def categories_set(bookmark_ids: list[int], category: Annotated[str, typer.Option("--category", "-c", help="Name, id, or 'none'.")]) -> None:
    """Set the category of bookmarks yourself (AI never changes these)."""
    lib = _lib()
    if category.lower() == "none":
        cid = 0
    elif category.isdigit():
        cid = int(category)
    elif found := lib.category_by_name(category):
        cid = found["id"]
    else:
        _fail(f"no category '{category}'")
    for bid in bookmark_ids:
        try:
            lib.update(bid, category_id=cid)
        except NotFound as exc:
            _fail(str(exc))
    console.print(f"set {len(bookmark_ids)} bookmark(s)")


# ------------------------------------------------------------------ AI


@ai_app.command("status")
def ai_status() -> None:
    """Which models and runtimes are ready."""
    from .ai.models import status

    for row in status(get_config()):
        mark = "[green]OK  [/green]" if row["ok"] else "[red]MISSING[/red]"
        console.print(f"{mark} {row['name']}: {escape(row['id'])} · {escape(row['detail'])}", highlight=False, soft_wrap=True)


@ai_app.command("download")
def ai_download(only: Annotated[Optional[str], typer.Option(help="llm or decider (default: both).")] = None, yes: Annotated[bool, typer.Option("--yes", "-y")] = False) -> None:
    """Download the configured models into the Hugging Face cache."""
    from .ai.models import download

    cfg = get_config()
    what = {"llm": cfg.llm_model, "decider": f"{cfg.decider_repo}/{cfg.decider_file}"}
    names = [what[only]] if only else list(what.values())
    if not yes:
        typer.confirm(f"Download {', '.join(names)} from Hugging Face (several GB)?", abort=True)
    for path in download(cfg, only or "all"):
        console.print(f"ready: {path}")


@ai_app.command("taxonomy")
def ai_taxonomy(
    min_categories: Annotated[int, typer.Option("--min")] = 8,
    max_categories: Annotated[int, typer.Option("--max")] = 15,
    background: Annotated[bool, typer.Option("--background", "-b", help="Run as a detached job; check it with `bmk ai jobs`.")] = False,
) -> None:
    """Propose a category list from a sample of the library (local LLM). Review it with `bmk changeset`."""
    _ai_job("taxonomy", {"min": min_categories, "max": max_categories}, background)


@ai_app.command("categorize")
def ai_categorize(
    bookmark_ids: Annotated[Optional[list[int]], typer.Argument(help="Only these bookmarks.")] = None,
    recheck_ai: Annotated[bool, typer.Option("--recheck-ai", help="Also re-check categories the AI set before.")] = False,
    limit: Annotated[Optional[int], typer.Option("--limit", "-n")] = None,
    background: Annotated[bool, typer.Option("--background", "-b", help="Run as a detached job; check it with `bmk ai jobs`.")] = False,
) -> None:
    """Classify uncategorised bookmarks with the local decision model into a changeset for review.
    Ctrl-C pauses; `bmk ai resume <job>` continues."""
    _ai_job("categorize", {"recheck_ai": recheck_ai, "ids": bookmark_ids, "limit": limit}, background)


def _ai_job(kind: str, params: dict, background: bool) -> None:
    from .ai import jobs

    lib = _lib()
    try:
        job_id = jobs.create(lib, kind, params, author="cli")
    except ValueError as exc:
        _fail(str(exc))
    if background:
        started = jobs.spawn(lib.config)
        console.print(f"job {job_id} {'started' if started else 'queued (another job is running)'}; check it with `bmk ai jobs`")
        return
    job = jobs.claim(lib, job_id)
    if job is None:
        console.print(f"job {job_id} queued: another job is running (see `bmk ai jobs`)")
        return

    def progress(j: dict) -> None:
        if j["kind"] == "taxonomy":
            err.print(f"… {j['message']}")
        elif j["done"] % 25 == 0 or j["done"] == j["total"]:
            left = j["stats"].get("seconds_left")
            err.print(f"… {j['done']}/{j['total']} · {j['message']}" + (f" · ~{max(1, round(left / 60))} min left" if left else ""))

    try:
        job = jobs.execute(lib, lib.config, job, on_progress=progress)
    except KeyboardInterrupt:
        job = jobs.get(lib, job_id)
        console.print(f"job {job_id} {job['state']}" + (f"; continue with `bmk ai resume {job_id}`" if job["state"] == "paused" else ""))
        raise typer.Exit(130) from None
    _show_job(job)
    if job["changeset_id"] and job["state"] in ("succeeded", "succeeded_with_errors"):
        if kind == "taxonomy":
            _show_changeset(lib, job["changeset_id"])
        cs = job["changeset_id"]
        console.print(f"Review: `bmk changeset show {cs}`, approve (`--all` or `--min-confidence 0.9`), then `bmk changeset apply {cs}`.")
    jobs.spawn(lib.config)  # anything queued meanwhile
    if job["state"] == "failed":
        raise typer.Exit(1)


def _timing(job: dict) -> str:
    """e.g. '22m 30s · 3.0 s/item · 950 tokens/item · 340 tokens/s · Apple M5, 24 GB'."""
    st = job["stats"]
    if not st["run_seconds"]:
        return ""
    m, s = divmod(round(st["run_seconds"]), 60)
    parts = [f"{m}m {s:02d}s" if m else f"{s}s"]
    if "seconds_per_item" in st and job["kind"] == "categorize":
        parts.append(f"{st['seconds_per_item']} s/item")
    if "tokens_per_item" in st:
        parts.append(f"{st['tokens_per_item']} tokens/item")
    if "tokens_per_second" in st:
        parts.append(f"{st['tokens_per_second']} tokens/s")
    if "seconds_left" in st:
        parts.append(f"~{max(1, round(st['seconds_left'] / 60))} min left")
    if st["hardware"]:
        parts.append(st["hardware"])
    return " · ".join(parts)


def _show_job(job: dict) -> None:
    progress = f"{job['done']}/{job['total']}" if job["total"] else ""
    errors = f" · {job['errors']} errors" if job["errors"] else ""
    console.print(f"job {job['id']} · {job['kind']} · [bold]{job['state']}[/bold] {progress}{errors} · {escape(job['message'] or '')}", highlight=False)
    if timing := _timing(job):
        console.print(f"[dim]{timing}[/dim]", highlight=False)
    if job["error"]:
        console.print(f"[red]{escape(job['error'])}[/red]")


@ai_app.command("jobs")
def ai_jobs(limit: Annotated[int, typer.Option("--limit", "-n")] = 10) -> None:
    """AI jobs and their state: queued, running, paused, succeeded, succeeded_with_errors, failed, cancelled."""
    from .ai import jobs

    lib = _lib()
    table = Table("Job", "Kind", "State", "Progress", "Errors", "Changeset", "Started", "Time", "Message", box=box.SIMPLE_HEAD, header_style="bold")
    for j in jobs.list_(lib, limit):
        table.add_row(str(j["id"]), j["kind"], j["state"], f"{j['done']}/{j['total']}" if j["total"] else "", str(j["errors"] or ""),
                      str(j["changeset_id"] or ""), display(j["started_at"], "exact", lib.config.display_tz, short=True) if j["started_at"] else "",
                      _timing(j), escape(j["error"] if j["state"] == "failed" else j["message"] or ""))  # fmt: skip
    console.print(table)


def _job_control(job_id: int, action: str) -> None:
    from .ai import jobs

    lib = _lib()
    try:
        job = jobs.control(lib, job_id, action)
    except (NotFound, Conflict, ValueError) as exc:
        _fail(str(exc))
    if action == "resume":
        jobs.spawn(lib.config)
        job = jobs.get(lib, job_id)
    _show_job(job)


@ai_app.command("pause")
def ai_pause(job_id: int) -> None:
    """Pause a classification job after the current bookmark."""
    _job_control(job_id, "pause")


@ai_app.command("resume")
def ai_resume(job_id: int) -> None:
    """Continue a paused job in the background."""
    _job_control(job_id, "resume")


@ai_app.command("cancel")
def ai_cancel(job_id: int) -> None:
    """Stop a job; what it proposed so far still goes to review."""
    _job_control(job_id, "cancel")


@ai_app.command("run-jobs", hidden=True)
def ai_run_jobs() -> None:
    """Run queued jobs one after another (started detached by the web UI and --background)."""
    import signal

    from .ai import jobs

    def stop(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)  # cancel (category list) or shutdown: end the job cleanly
    lib = _lib()
    try:
        for job in jobs.run_queued(lib, lib.config):
            print(f"job {job['id']} {job['kind']}: {job['state']} · {job['message'] or ''} {job['error'] or ''}", flush=True)
    except KeyboardInterrupt:
        pass


# ------------------------------------------------------------------ changesets


def _show_changeset(lib, cs_id: int) -> None:
    from . import changesets

    cs = changesets.get(lib, cs_id)
    console.print(f"[bold]changeset {cs['id']}[/bold] · {cs['kind']} · {cs['status']} · {cs['model'] or ''} · {escape(cs['summary'] or '')}", highlight=False)
    if cs["error"]:
        console.print(f"[red]{escape(cs['error'])}[/red]")
    table = Table("Item", "Decision", "Conf.", "Bookmark", "Change", "Why", box=box.SIMPLE_HEAD, header_style="bold")
    for i in cs["items"]:
        if i["op"] == "add_category":
            change = f"+ {i['new_value']['name']}: {i['new_value'].get('description', '')}"
            target = ""
        elif i["op"] == "set_category":
            change = f"{i['old_name'] or '—'} → {i['new_name'] or '—'}"
            target = f"#{i['bookmark_id']} {(i['title'] or i['url'] or '')[:50]}"
        else:
            change = f"merge into #{i['target_id']}"
            target = f"#{i['bookmark_id']}"
        conf = f"{i['confidence']:.0%}" if i["confidence"] is not None else ""
        table.add_row(str(i["id"]), i["decision"], conf, escape(target), escape(change), escape(i["rationale"] or i["result"] or ""))
    console.print(table)


@changeset_app.command("list")
def changeset_list(status: Annotated[Optional[str], typer.Option()] = None) -> None:
    from . import changesets

    lib = _lib()
    table = Table("ID", "Kind", "Status", "Items", "Created", "Summary", box=box.SIMPLE_HEAD, header_style="bold")
    for cs in changesets.list_(lib, status):
        items = " ".join(f"{k}={v}" for k, v in sorted(cs["counts"].items()))
        table.add_row(str(cs["id"]), cs["kind"], cs["status"], items, display(cs["created_at"], "exact", lib.config.display_tz), escape(cs["summary"] or ""))
    console.print(table)


@changeset_app.command("show")
def changeset_show(changeset_id: int) -> None:
    from .service import NotFound as _NF

    try:
        _show_changeset(_lib(), changeset_id)
    except _NF as exc:
        _fail(str(exc))


def _decide(changeset_id: int, decision: str, items: list[int] | None, min_confidence: float | None, all_items: bool) -> None:
    from . import changesets

    if not items and min_confidence is None and not all_items:
        _fail("give item ids, --min-confidence or --all")
    lib = _lib()
    try:
        n = changesets.decide(lib, changeset_id, decision, items or None, min_confidence)
    except (NotFound, Conflict, ValueError) as exc:
        _fail(str(exc))
    console.print(f"{decision} {n} item(s)")


@changeset_app.command("approve")
def changeset_approve(
    changeset_id: int,
    items: Annotated[Optional[list[int]], typer.Argument(help="Item ids (default: see --all / --min-confidence).")] = None,
    min_confidence: Annotated[Optional[float], typer.Option("--min-confidence")] = None,
    all_items: Annotated[bool, typer.Option("--all")] = False,
) -> None:
    """Approve items (they are applied by `bmk changeset apply`)."""
    _decide(changeset_id, "approved", items, min_confidence, all_items)


@changeset_app.command("reject")
def changeset_reject(
    changeset_id: int,
    items: Annotated[Optional[list[int]], typer.Argument()] = None,
    all_items: Annotated[bool, typer.Option("--all")] = False,
) -> None:
    _decide(changeset_id, "rejected", items, None, all_items)


@changeset_app.command("apply")
def changeset_apply(changeset_id: int) -> None:
    """Apply the approved items in one transaction; items that changed since become conflicts."""
    from . import changesets

    lib = _lib()
    try:
        r = changesets.apply(lib, changeset_id)
    except (NotFound, Conflict) as exc:
        _fail(str(exc))
    console.print(f"applied {r['applied']}, conflicts {r['conflicts']}, still to decide {r['left']}")


@changeset_app.command("revert")
def changeset_revert(changeset_id: int) -> None:
    """Undo an applied changeset (items changed since are left alone)."""
    from . import changesets

    lib = _lib()
    try:
        r = changesets.revert(lib, changeset_id)
    except (NotFound, Conflict) as exc:
        _fail(str(exc))
    console.print(f"reverted {r['reverted']}, skipped {r['skipped']} (changed since)")


@app.command()
def mcp() -> None:
    """Run the MCP server on stdio (for Claude Code / Claude Desktop). Read and propose tools are always on;
    single-item writes need BMK_MCP_ALLOW_WRITES=true."""
    from .mcp_server import main

    main()


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
