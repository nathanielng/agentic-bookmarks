"""Exports for use elsewhere: JSONL, CSV, Markdown and Netscape bookmark HTML (what browsers import).

The JSON backup (`Library.export_backup`) is the restorable format; these are one-way and carry the
bookmark fields people use, for one list (view) at a time.
"""

import csv
import html
import io
import json
from collections.abc import Iterator
from typing import Any

from .timeutil import parse_iso

FORMATS = {
    "jsonl": ("application/x-ndjson", "jsonl"),
    "csv": ("text/csv", "csv"),
    "markdown": ("text/markdown", "md"),
    "html": ("text/html", "html"),
}
FIELDS = (
    "id", "url", "title", "description", "domain", "saved_at", "saved_at_source", "status", "starred",
    "tags", "remarks", "site_name", "author", "published_at", "fetch_status",
)  # fmt: skip


def rows(lib, *, view: str = "active", q: str | None = None, source_type: str | None = None, source_ref: str | None = None) -> Iterator[dict[str, Any]]:
    """Every bookmark in the view (oldest saved first), with its tags, in FIELDS."""
    offset, page = 0, 500
    while True:
        batch = lib.browse(view=view, q=q, source_type=source_type, source_ref=source_ref, sort="saved_at", desc=False, limit=page, offset=offset)
        ids = [r["id"] for r in batch["items"]]
        tags: dict[int, list[str]] = {}
        if ids:
            marks = ",".join("?" * len(ids))
            for bid, name in lib.conn.execute(
                f"SELECT bt.bookmark_id, t.name FROM bookmark_tags bt JOIN tags t ON t.id = bt.tag_id WHERE bt.bookmark_id IN ({marks})"
                " ORDER BY t.name",
                ids,
            ):
                tags.setdefault(bid, []).append(name)
        for r in batch["items"]:
            out = {k: r.get(k) for k in FIELDS}
            out["starred"] = bool(r.get("starred"))
            out["tags"] = tags.get(r["id"], [])
            yield out
        offset += page
        if offset >= batch["total"] or not ids:
            return


def to_jsonl(items) -> str:
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in items)


def to_csv(items) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=FIELDS, lineterminator="\n")
    writer.writeheader()
    for r in items:
        writer.writerow({**r, "tags": ", ".join(r["tags"]), "starred": "yes" if r["starred"] else ""})
    return "\ufeff" + buf.getvalue()  # a BOM so Excel reads it as UTF-8


def _md_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]").replace("\n", " ")


def _md_url(url: str) -> str:
    return url.replace(" ", "%20").replace("(", "%28").replace(")", "%29")


def to_markdown(items, title: str = "Bookmarks") -> str:
    """One list item per bookmark. Exact save times are written in the iOS Shortcut form
    (`- 2026-10-25T13:30:00Z [Title](URL)`), so a re-import keeps them; approximate ones are left out."""
    lines = [f"# {title}", ""]
    for r in items:
        stamp = f"{r['saved_at']} " if r["saved_at"] and r["saved_at_source"] == "exact" else ""
        star = " ★" if r["starred"] else ""
        link = f"[{_md_text(r['title'])}]({_md_url(r['url'])})" if r["title"] else f"<{_md_url(r['url'])}>"
        lines.append(f"- {stamp}{link}{star}")
        for extra in (r["description"], r["remarks"]):
            if extra:
                lines.append(f"  > {_md_text(extra.strip())}")
    return "\n".join(lines) + "\n"


def to_netscape(items, title: str = "Bookmarks") -> str:
    """The Netscape bookmark file every browser can import. Starred bookmarks get a `starred` tag."""
    esc = html.escape
    out = [
        "<!DOCTYPE NETSCAPE-Bookmark-file-1>",
        '<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">',
        f"<TITLE>{esc(title)}</TITLE>",
        f"<H1>{esc(title)}</H1>",
        "<DL><p>",
    ]
    for r in items:
        attrs = f'HREF="{esc(r["url"])}"'
        if r["saved_at"]:
            attrs += f' ADD_DATE="{int(parse_iso(r["saved_at"]).timestamp())}"'
        tags = r["tags"] + (["starred"] if r["starred"] else [])
        if tags:
            attrs += f' TAGS="{esc(",".join(tags))}"'
        out.append(f"    <DT><A {attrs}>{esc(r['title'] or r['url'])}</A>")
        if note := (r["remarks"] or r["description"]):
            out.append(f"    <DD>{esc(note.strip())}")
    out.append("</DL><p>")
    return "\n".join(out) + "\n"


def render(fmt: str, items, title: str = "Bookmarks") -> str:
    if fmt == "jsonl":
        return to_jsonl(items)
    if fmt == "csv":
        return to_csv(items)
    if fmt == "markdown":
        return to_markdown(items, title)
    if fmt == "html":
        return to_netscape(items, title)
    raise ValueError(f"format must be one of json, {', '.join(FORMATS)}")
