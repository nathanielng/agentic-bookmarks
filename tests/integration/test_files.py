"""File ingesters (RTF, HTML, Markdown, CSV, XLSX), the clipboard path and exports. All data is synthetic."""

import io
import json
import shutil
import subprocess

import pytest
from typer.testing import CliRunner

from bmk import export as exporter
from bmk.cli import app
from bmk.ingest.cloudtabs import CloudTabsIngester
from bmk.ingest.files import FileOptions, import_clipboard, import_data, import_paths
from bmk.ingest.markdown import parse_markdown
from bmk.ingest.rtf import FileFormatError, parse_rtf
from bmk.ingest.table import ColumnsNeeded, parse_table, preview

needs_textutil = pytest.mark.skipif(shutil.which("textutil") is None, reason="textutil is macOS-only")

# Hand-written RTF in the shape Cocoa writes: HYPERLINK fields, code-page and \uN escapes.
HAND_RTF = rb"""{\rtf1\ansi\ansicpg1252\cocoartf2822
{\fonttbl\f0\fswiss\fcharset0 Helvetica;}
\pard\f0\fs24 {\field{\*\fldinst{HYPERLINK "https://example.com/one"}}{\fldrslt Don\'92t \'93panic\'94}}\
{\field{\*\fldinst{HYPERLINK "https://example.com/two?utm_source=feed"}}{\fldrslt Caf\'e9 \uc1\u8212 x guide}}\
plain https://example.org/bare and {\field{\*\fldinst{HYPERLINK "about:blank"}}{\fldrslt blank}}}
"""

HTML_PAGE = """<!doctype html><meta charset="utf-8"><ul>
<li><a href="https://example.com/article">Don’t panic — “quotes” café 日本 😀</a></li>
<li><a href="https://example.net/second">Second link</a></li>
<li><a href="https://www.google.com/search?q=example">example - Google Search</a></li>
</ul>"""


def make_rtf(tmp_path, html: str = HTML_PAGE):
    """An RTF written by Cocoa itself, the way TextEdit or Notes would save pasted links."""
    src = tmp_path / "page.html"
    src.write_text(html, encoding="utf-8")
    out = tmp_path / "tabs.rtf"
    subprocess.run(["textutil", "-convert", "rtf", str(src), "-output", str(out)], check=True, capture_output=True)
    return out


def sources_of(lib, bookmark_id):
    return lib.get(bookmark_id)["sources"]


# ---------------------------------------------------------------------------------------------- RTF


@needs_textutil
def test_rtf_hand_written_escapes():
    items, problems = parse_rtf(HAND_RTF)
    assert [(i.url, i.title, i.locator) for i in items] == [
        ("https://example.com/one", "Don’t “panic”", "#1"),
        ("https://example.com/two?utm_source=feed", "Café — guide", "#2"),
        ("https://example.org/bare", None, "#3"),
        ("about:blank", "blank", "#4"),
    ]
    assert problems == []


@needs_textutil
def test_rtf_from_cocoa_passes_quality_checks(lib, tmp_path):
    rtf = make_rtf(tmp_path)
    items, problems = parse_rtf(rtf.read_bytes())
    assert problems == []
    assert items[0].title == "Don’t panic — “quotes” café 日本 😀"
    for item in items:  # the original script's checks, on what is stored
        assert item.url and item.title
        assert "\\'" not in item.title and "\\u" not in item.title
        assert item.title.encode("utf-8").decode("utf-8") == item.title

    [result] = import_paths(lib, [rtf], opts=FileOptions())
    assert result.status == "imported" and result.summary.counts["new"] == 3
    b = lib.list()[0]
    assert b["saved_at_source"] == "file_birthtime"
    src = sources_of(lib, b["id"])[0]
    assert src["source_type"] == "rtf" and src["source_ref"] == str(rtf.resolve()) and src["locator"].startswith("#")


@needs_textutil
def test_malformed_files_are_reported_per_file(lib, tmp_path):
    good = make_rtf(tmp_path)
    (tmp_path / "broken.rtf").write_bytes(b"{\\rtf1 not closed {\\field")
    (tmp_path / "fake.rtf").write_text("just text")
    (tmp_path / "binary.rtf").write_bytes(b"\x00\x01\xff\xfe")
    results = import_paths(lib, [tmp_path / "fake.rtf", tmp_path / "binary.rtf", good])
    assert [r.status for r in results] == ["failed", "failed", "imported"]
    assert "not an RTF file" in results[0].error
    runs = lib.import_runs(10)
    assert sum(1 for r in runs if r["error"]) == 2  # each failure is in the import history


def test_html_file(lib, tmp_path):
    page = tmp_path / "links.html"
    page.write_text(HTML_PAGE, encoding="utf-8")
    [result] = import_paths(lib, [page])
    assert result.source_type == "html" and result.summary.counts["new"] == 3


# ---------------------------------------------------------------------------------------------- Markdown

MARKDOWN = """# Reading list

- 2026-10-25T13:30+08:00 [Shared *from* phone](https://example.com/shared)
- 2026-10-24 09:15 https://example.com/naive-local
- [Plain item](https://example.com/plain) and <https://example.com/auto>

Prose with https://example.org/bare, and `https://example.org/inline-code`.

```
https://example.org/fenced
```

    https://example.org/indented
<a href="https://example.org/html">html link</a>
"""


def test_markdown_links_lines_and_code_skipping():
    items, _ = parse_markdown(MARKDOWN.encode(), tz="Asia/Singapore")
    got = [(i.locator, i.url, i.title) for i in items]
    assert got == [
        ("line 3", "https://example.com/shared", "Shared from phone"),
        ("line 4", "https://example.com/naive-local", None),
        ("line 5", "https://example.com/plain", "Plain item"),
        ("line 5", "https://example.com/auto", "https://example.com/auto"),
        ("line 7", "https://example.org/bare", None),
        ("line 14", "https://example.org/html", "html link"),
    ]
    assert items[0].event_at.isoformat() == "2026-10-25T13:30:00+08:00" and items[0].event_at_kind == "exact"
    assert items[1].event_at.isoformat() == "2026-10-24T09:15:00+08:00"  # no offset: local time
    assert items[2].event_at is None

    with_code = [i.url for i in parse_markdown(MARKDOWN.encode(), include_code=True)[0]]
    assert {"https://example.org/inline-code", "https://example.org/fenced", "https://example.org/indented"} <= set(with_code)
    fenced = next(i for i in parse_markdown(MARKDOWN.encode(), include_code=True)[0] if i.url.endswith("fenced"))
    assert fenced.locator == "line 10"


def test_markdown_folder_by_content_hash(lib, tmp_path):
    notes = tmp_path / "notes"
    (notes / "sub").mkdir(parents=True)
    (notes / ".hidden").mkdir()
    inbox = notes / "inbox.md"
    inbox.write_text("- 2026-10-25T13:30+08:00 [One](https://example.com/one)\n")
    (notes / "sub" / "copy.md").write_text(inbox.read_text())  # same content, other name
    (notes / ".hidden" / "x.md").write_text("[Hidden](https://example.com/hidden)\n")
    (notes / "image.png").write_bytes(b"\x89PNG")

    first = import_paths(lib, [notes])
    assert [(r.name.rsplit("/", 1)[-1], r.status) for r in first] == [("inbox.md", "imported"), ("copy.md", "unchanged")]
    assert "same content as" in first[1].error
    assert [r.status for r in import_paths(lib, [notes])] == ["unchanged", "unchanged"]

    # The Shortcut appends a line: the file is read again; the first line's source is not duplicated.
    inbox.write_text(inbox.read_text() + "- 2026-10-25T14:00+08:00 [Two](https://example.com/two)\n")
    again = import_paths(lib, [inbox])[0]
    assert again.summary.counts == {**again.summary.counts, "new": 1, "unchanged": 1}
    one = next(b for b in lib.list() if b["title"] == "One")
    assert one["saved_at"] == "2026-10-25T05:30:00Z" and one["saved_at_source"] == "exact"
    assert len(sources_of(lib, one["id"])) == 1

    forced = import_paths(lib, [notes / "sub" / "copy.md"], force=True)[0]
    assert forced.status == "imported"


# ---------------------------------------------------------------------------------------------- CSV / XLSX


def test_csv_header_detection_remarks_dates_and_invalid_rows(lib):
    data = (
        "Name,Link,Notes,Added\n"
        'Alpha,https://example.com/a,"first, quoted\nover two lines",2025-01-02\n'
        "Beta,example.com/b,,03/02/2025\n"
        "Gamma,not a url,bad,\n"
        ",,,\n"
    ).encode()
    result = import_data(lib, data, name="list.csv", opts=FileOptions(tz="Asia/Singapore"))
    assert result.summary.counts == {**result.summary.counts, "new": 2, "invalid": 1}
    items = {i["raw_url"]: i for i in lib.import_items(result.summary.run_id)}
    assert items["not a url"]["outcome"] == "invalid" and items["not a url"]["locator"] == "row 4"
    alpha = next(b for b in lib.list() if b["title"] == "Alpha")
    assert alpha["remarks"] == "Notes: first, quoted\nover two lines"
    assert alpha["saved_at"] == "2025-01-01T16:00:00Z"
    beta = next(b for b in lib.list() if b["title"] == "Beta")
    assert beta["url_canonical"] == "https://example.com/b" and beta["saved_at"] == "2025-02-02T16:00:00Z"  # day first


def test_csv_without_header_uses_url_ratio_and_sniffs_delimiter():
    data = "https://example.com/1;one\nhttps://example.com/2;two\nexample.com/3;three\n".encode()
    items, _ = parse_table(data, "csv")
    assert [i.url for i in items] == ["https://example.com/1", "https://example.com/2", "https://example.com/3"]
    assert items[0].locator == "row 1" and items[0].extra == {"column 2": "one"}


def test_csv_cp1252_fallback_warns():
    data = "Title,URL\nCafé “menu”,https://example.com/menu\n".encode("cp1252")
    items, warnings = parse_table(data, "csv")
    assert items[0].title == "Café “menu”"
    assert any("cp1252" in w for w in warnings)


def test_csv_ambiguous_columns_and_overrides():
    data = "Primary URL,Website,Comment\nhttps://example.com/a,https://example.org/a,x\nhttps://example.com/b,https://example.org/b,y\n"
    with pytest.raises(ColumnsNeeded) as exc:
        parse_table(data.encode(), "csv")
    assert exc.value.headers == ["Primary URL", "Website", "Comment"] and len(exc.value.preview) == 2
    items, _ = parse_table(data.encode(), "csv", url_col="Website", title_col="3")
    assert [(i.url, i.title) for i in items][0] == ("https://example.org/a", "x")
    with pytest.raises(FileFormatError, match="no column"):
        parse_table(data.encode(), "csv", url_col="Nope")
    [sheet] = preview(data.encode(), "csv")
    assert sheet["detected"] is None and sheet["headers"][0] == "Primary URL"


def test_csv_no_url_column_needs_columns_result(lib):
    result = import_data(lib, b"a,b\nx,y\n", name="words.csv")
    assert result.status == "needs_columns" and result.columns["headers"] == ["a", "b"]


def _workbook():
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Reading"
    ws.append(["Page", "Saved", "Why"])
    ws.append(["Shown text", "2025-03-04 10:00", "because"])
    ws["A2"].hyperlink = "https://example.com/from-hyperlink"
    ws.append(["https://example.com/plain-value", None, None])
    other = wb.create_sheet("Other")
    other.append(["Link"])
    other.append(["https://example.org/other"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_xlsx_hyperlinks_sheets_and_locators(lib):
    data = _workbook()
    items, _ = parse_table(data, "xlsx", tz="UTC")
    assert [(i.url, i.title, i.locator) for i in items] == [
        ("https://example.com/from-hyperlink", "Shown text", "Reading!R2"),
        ("https://example.com/plain-value", None, "Reading!R3"),
        ("https://example.org/other", None, "Other!R2"),
    ]
    assert items[0].event_at.isoformat() == "2025-03-04T10:00:00+00:00" and items[0].extra == {"Why": "because"}
    only, _ = parse_table(data, "xlsx", sheets=["Other"])
    assert [i.locator for i in only] == ["Other!R2"]
    with pytest.raises(FileFormatError, match="no sheet"):
        parse_table(data, "xlsx", sheets=["Missing"])
    result = import_data(lib, b"PK\x03\x04 not really a zip", name="bad.xlsx")
    assert result.status == "failed" and "XLSX" in result.error


# ---------------------------------------------------------------------------------------------- clipboard


def test_clipboard_html_flavour(lib):
    result = import_clipboard(lib, "html", HTML_PAGE.encode())
    assert result.status == "imported" and result.summary.counts["new"] == 3
    src = sources_of(lib, lib.list()[0]["id"])[0]
    assert src["source_type"] == "clipboard" and src["source_ref"] == "clipboard"
    text = import_clipboard(lib, "text", b"copied https://example.com/text-only and more")
    assert text.summary.counts["new"] == 1


# ---------------------------------------------------------------------------------------------- acceptance


@needs_textutil
def test_same_url_from_rtf_markdown_and_cloudtabs_is_one_bookmark_with_three_sources(lib, tmp_path, cloudtabs):
    """CG4 exit criterion (PLAN05 §16)."""
    url = "https://example.com/article"
    rtf = make_rtf(tmp_path, f'<meta charset="utf-8"><a href="{url}?utm_source=newsletter">Article from RTF</a>')
    md = tmp_path / "inbox.md"
    md.write_text(f"- 2026-10-20T08:00+08:00 [Article from Markdown]({url}#:~:text=highlight)\n")
    cloudtabs.open_tab("T1", f"{url}?utm_medium=social&fbclid=abc", "Article from Safari")

    assert [r.status for r in import_paths(lib, [rtf, md])] == ["imported", "imported"]
    lib.ingest("cloudtabs", list(CloudTabsIngester(path=cloudtabs.path).read()))

    [bookmark] = lib.list()
    detail = lib.get(bookmark["id"])
    assert sorted(s["source_type"] for s in detail["sources"]) == ["cloudtabs", "markdown", "rtf"]
    assert detail["title"] == "Article from RTF"  # the first source's title; later ones only fill gaps
    assert detail["saved_at"] == "2026-10-20T00:00:00Z" and detail["saved_at_source"] == "exact"  # best precision wins


# ---------------------------------------------------------------------------------------------- exports


def _seed(lib):
    lib.add("https://example.com/a", title="Alpha [draft]", remarks="note one")
    lib.add("https://example.com/b(1)")
    b = lib.list()
    lib.update(next(x["id"] for x in b if x["title"] == "Alpha [draft]"), starred=True)


def test_exports(lib):
    _seed(lib)
    rows = list(exporter.rows(lib))
    assert [r["url"] for r in rows] == ["https://example.com/a", "https://example.com/b(1)"]

    lines = exporter.to_jsonl(rows).splitlines()
    assert json.loads(lines[0])["starred"] is True and json.loads(lines[0])["status"] == "kept"

    csv_text = exporter.to_csv(rows)
    assert csv_text.startswith("﻿id,url,title") and "yes" in csv_text

    md = exporter.to_markdown(rows)
    assert "[Alpha \\[draft\\]](https://example.com/a) ★" in md and "<https://example.com/b%281%29>" in md
    assert "  > note one" in md

    html = exporter.to_netscape(rows)
    assert html.startswith("<!DOCTYPE NETSCAPE-Bookmark-file-1>")
    assert 'HREF="https://example.com/a"' in html and 'TAGS="starred"' in html and "ADD_DATE=" in html
    assert "<DD>note one" in html

    starred = list(exporter.rows(lib, view="starred"))
    assert len(starred) == 1


def test_markdown_export_reimports_with_exact_times(lib, config, tmp_path):
    _seed(lib)
    md = exporter.render("markdown", exporter.rows(lib))
    items, _ = parse_markdown(md.encode())
    originals = {b["url_canonical"]: b["saved_at"] for b in lib.list()}
    assert {i.url: i.event_at.strftime("%Y-%m-%dT%H:%M:%SZ") for i in items} == {
        "https://example.com/a": originals["https://example.com/a"],
        "https://example.com/b%281%29": originals["https://example.com/b(1)"],
    }


def test_netscape_export_reimports_as_html(lib, tmp_path):
    _seed(lib)
    page = tmp_path / "bookmarks.html"
    page.write_text(exporter.render("html", exporter.rows(lib)), encoding="utf-8")
    [result] = import_paths(lib, [page])
    assert result.summary.counts["matched_exact"] == 2


# ---------------------------------------------------------------------------------------------- CLI


@pytest.fixture
def cli_env(monkeypatch, tmp_path):
    monkeypatch.setenv("COLUMNS", "250")
    monkeypatch.setenv("BMK_DB_PATH", str(tmp_path / "cli.db"))
    monkeypatch.setenv("BMK_ENRICH", "false")
    return CliRunner()


def test_cli_ingest_files_and_export(cli_env, tmp_path):
    runner = cli_env
    md = tmp_path / "links.md"
    md.write_text("[One](https://example.com/one)\n")
    csv_file = tmp_path / "list.csv"
    csv_file.write_text("a,b\nx,y\n")

    result = runner.invoke(app, ["ingest", "markdown", str(md)])
    assert result.exit_code == 0 and "new=1" in result.output, result.output
    assert "unchanged" in runner.invoke(app, ["ingest", "file", str(md)]).output

    result = runner.invoke(app, ["ingest", "csv", str(csv_file)])
    assert result.exit_code == 1 and "choose the URL column" in result.output
    result = runner.invoke(app, ["ingest", "file", str(tmp_path / "missing.md")])
    assert result.exit_code == 1

    out = tmp_path / "out.html"
    assert runner.invoke(app, ["export", "--format", "html", "-o", str(out)]).exit_code == 0
    assert "https://example.com/one" in out.read_text()
    assert runner.invoke(app, ["export", "--format", "nope"]).exit_code == 1


# ---------------------------------------------------------------------------------------------- API


def test_api_upload_picker_and_export(config, cloudtabs):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from bmk.api import TOKEN_HEADER, create_app

    config.cloudtabs_path = cloudtabs.path
    client = TestClient(create_app(config))
    w = {TOKEN_HEADER: config.token_path.read_text().strip(), "Content-Type": "application/octet-stream"}
    data = b"Primary URL,Website\nhttps://example.com/a,https://example.org/a\n"

    assert client.post("/api/import", params={"name": "x.csv"}, content=data).status_code == 403
    pv = client.post("/api/import/preview", params={"name": "x.csv"}, content=data, headers=w).json()
    assert pv["format"] == "csv" and pv["sheets"][0]["detected"] is None

    r = client.post("/api/import", params={"name": "x.csv"}, content=data, headers=w)
    assert r.status_code == 409 and r.json()["columns"]["headers"] == ["Primary URL", "Website"]
    r = client.post("/api/import", params={"name": "x.csv", "url_col": "Website", "mtime": 1761370000000}, content=data, headers=w)
    assert r.status_code == 200 and r.json()["counts"] == {"new": 1}
    b = client.get("/api/bookmarks", params={"view": "inbox"}).json()["items"][0]
    assert b["url"] == "https://example.org/a" and b["saved_at_source"] == "file_mtime"
    assert client.post("/api/import", params={"name": "x.csv", "url_col": "Website"}, content=data, headers=w).json()["status"] == "unchanged"
    assert client.post("/api/import", params={"name": "x.doc"}, content=b"?", headers=w).status_code == 422

    r = client.get("/api/export", params={"format": "html", "view": "inbox"})
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"] and "example.org/a" in r.text
    assert client.get("/api/export", params={"format": "csv", "q": "nothing-matches"}).text.count("\n") == 1
    assert client.get("/api/export", params={"format": "bogus"}).status_code == 400
    assert client.get("/api/export").json()["format"] == "bmk-backup"
