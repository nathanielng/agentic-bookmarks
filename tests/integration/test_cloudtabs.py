import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

from bmk.cli import app
from bmk.ingest import cloudtabs as ct
from bmk.ingest.cloudtabs import CloudTabsIngester, CloudTabsPermissionDenied, CloudTabsSchemaError


def poll(lib, fake, **kw):
    return lib.ingest("cloudtabs", list(CloudTabsIngester(path=fake.path, **kw).read()))


def test_reads_uncheckpointed_wal(cloudtabs):
    cloudtabs.open_tab("T1", "https://example.com/", "Example")
    assert (cloudtabs.path.parent / "CloudTabs.db-wal").stat().st_size > 0
    tabs = CloudTabsIngester(path=cloudtabs.path).tabs()
    assert [(t.device_name, t.title, t.url) for t in tabs] == [("Test iPhone", "Example", "https://example.com/")]


def test_source_files_unchanged(cloudtabs):
    # Only the database and WAL hold data. The -shm file is SQLite's shared-memory lock index, which
    # every reader (Safari included) updates with read marks, so it is excluded.
    cloudtabs.open_tab("T1", "https://example.com/")
    files = [cloudtabs.path, cloudtabs.path.with_name("CloudTabs.db-wal")]
    before = {p.name: p.read_bytes() for p in files}
    list(CloudTabsIngester(path=cloudtabs.path).read())
    after = {p.name: p.read_bytes() for p in files}
    assert before == after


def test_acceptance_flow(lib, cloudtabs):
    """PLAN05 §15 / PLAN03_CLAUDE §15.2 steps 2–6, at the service level."""
    tabs = [
        ("T1", "https://example.com/a", "A"),
        ("T2", "https://example.com/b", "B"),
        ("T3", "https://lnkd.in/p/gfiEPStM", ""),
        ("T4", "https://x.com/someone/status/123?s=20", "Post"),
        ("T5", "https://example.com/c", "C"),
    ]
    for t in tabs:
        cloudtabs.open_tab(*t)

    # 2. first poll: five new bookmarks, approximate timestamps
    s = poll(lib, cloudtabs)
    assert s.counts["new"] == 5
    assert {b["saved_at_source"] for b in lib.list()} == {"first_seen_initial"}
    # 3. second poll: nothing new
    s = poll(lib, cloudtabs)
    assert s.counts["unchanged"] == 5 and lib.count() == 5
    # 4. same post on the Mac with a different tracking param → merged, two sources
    cloudtabs.open_tab("M1", "https://twitter.com/someone/status/123?s=46", "Post", device="DEV-MAC")
    s = poll(lib, cloudtabs)
    assert s.counts["matched_exact"] == 1 and lib.count() == 5
    post = next(b for b in lib.list() if b["domain"] == "x.com")
    assert len(lib.get(post["id"])["sources"]) == 2
    # 5. closing tabs keeps the bookmarks
    cloudtabs.close_tab("T1")
    cloudtabs.close_tab("T2")
    poll(lib, cloudtabs)
    assert lib.count() == 5
    # 6. edit persists, delete stays deleted
    c = next(b for b in lib.list() if b["url_canonical"].endswith("/c"))
    lib.update(c["id"], title="Renamed")
    lib.delete(post["id"])
    s = poll(lib, cloudtabs)
    assert s.counts["suppressed_deleted"] == 2  # the post's two tabs
    assert lib.get(c["id"])["title"] == "Renamed"
    assert lib.count() == 4


def test_last_viewed_time_bounds_saved_at(lib, cloudtabs):
    viewed = datetime(2026, 3, 1, 9, 30, tzinfo=UTC)
    cloudtabs.open_tab("T1", "https://example.com/old", "Old", last_viewed=viewed)
    cloudtabs.open_tab("T2", "https://example.com/unset", "Unset")
    cloudtabs.open_tab("T3", "https://example.com/future", "Future", last_viewed=datetime.now(UTC) + timedelta(days=1))
    poll(lib, cloudtabs)
    by_title = {b["title"]: b for b in lib.list()}
    assert by_title["Old"]["saved_at"] == "2026-03-01T09:30:00Z"
    assert {b["saved_at_source"] for b in by_title.values()} == {"first_seen_initial"}
    # No usable last_viewed_time: fall back to the poll time.
    assert by_title["Unset"]["saved_at"] == by_title["Future"]["saved_at"] > "2026-03-01"

    # A tab opened after the first poll but last viewed before this poll uses the earlier time too.
    later = (datetime.now(UTC) - timedelta(minutes=5)).replace(microsecond=0)  # whole seconds survive the float round trip
    cloudtabs.open_tab("T4", "https://example.com/new", "New", last_viewed=later)
    poll(lib, cloudtabs)
    new = next(b for b in lib.list() if b["title"] == "New")
    assert new["saved_at_source"] == "first_seen"
    assert new["saved_at"] == later.isoformat().replace("+00:00", "Z")


def test_url_as_title_is_not_stored_as_title(lib, cloudtabs):
    url = "https://www.channelnewsasia.com/business/amazon-6488181"
    cloudtabs.open_tab("T1", url, "www.channelnewsasia.com/business/amazon-6488181")
    poll(lib, cloudtabs)
    b = lib.get(lib.list()[0]["id"])
    assert b["title"] is None and b["title_source"] is None
    assert b["sources"][0]["raw_title"] == "www.channelnewsasia.com/business/amazon-6488181"  # provenance kept
    # The same page later open with a real title on another device fills the gap.
    cloudtabs.open_tab("M1", url, "Amazon cloud capacity sold out", device="DEV-MAC")
    poll(lib, cloudtabs)
    assert lib.get(b["id"])["title"] == "Amazon cloud capacity sold out"


def test_device_filters(lib, cloudtabs):
    cloudtabs.open_tab("T1", "https://a.com/", device="DEV-PHONE")
    cloudtabs.open_tab("M1", "https://b.com/", device="DEV-MAC")
    assert [i.url for i in CloudTabsIngester(path=cloudtabs.path, exclude_devices=["test macbook"]).read()] == ["https://a.com/"]
    assert [i.url for i in CloudTabsIngester(path=cloudtabs.path, devices=["Test MacBook"]).read()] == ["https://b.com/"]


def test_schema_introspection_tolerates_missing_optional_columns(tmp_path):
    path = tmp_path / "CloudTabs.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE cloud_tabs (url TEXT, title TEXT)")
    conn.execute("INSERT INTO cloud_tabs VALUES ('https://a.com/', 'A')")
    conn.commit()
    conn.close()
    items = list(CloudTabsIngester(path=path).read())
    assert items[0].source_ref == "unknown device" and items[0].url == "https://a.com/"


def test_schema_error(tmp_path):
    path = tmp_path / "CloudTabs.db"
    sqlite3.connect(path).execute("CREATE TABLE something_else (x)").connection.commit()
    with pytest.raises(CloudTabsSchemaError):
        list(CloudTabsIngester(path=path).read())


def test_permission_denied_is_reported(monkeypatch, tmp_path):
    target = tmp_path / "Safari" / "CloudTabs.db"

    def denied(self, *a, **k):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(type(target), "is_file", denied)
    with pytest.raises(CloudTabsPermissionDenied, match="Full Disk Access"):
        ct.locate(target)


@pytest.fixture(autouse=True)
def wide_terminal(monkeypatch):
    monkeypatch.setenv("COLUMNS", "250")


def test_cli_end_to_end(monkeypatch, tmp_path, cloudtabs):
    cloudtabs.open_tab("T1", "https://example.com/a", "Alpha")
    monkeypatch.setenv("BMK_DB_PATH", str(tmp_path / "cli.db"))
    monkeypatch.setenv("BMK_CLOUDTABS_PATH", str(cloudtabs.path))
    runner = CliRunner()

    result = runner.invoke(app, ["ingest", "cloudtabs"])
    assert result.exit_code == 0, result.output
    assert "new=1" in result.output
    result = runner.invoke(app, ["list"])
    assert "Alpha" in result.output and "~" in result.output
    result = runner.invoke(app, ["search", "alpha", "--json"])
    assert '"title": "Alpha"' in result.output
    assert runner.invoke(app, ["edit", "1", "--title", "Renamed"]).exit_code == 0
    assert runner.invoke(app, ["rm", "1"]).exit_code == 0
    result = runner.invoke(app, ["ingest", "cloudtabs"])
    assert "suppressed_deleted=1" in result.output
    result = runner.invoke(app, ["runs"])
    assert result.exit_code == 0 and "cloudtabs" in result.output

    backup = tmp_path / "backup.json"
    assert runner.invoke(app, ["export", "-o", str(backup)]).exit_code == 0
    monkeypatch.setenv("BMK_DB_PATH", str(tmp_path / "restored.db"))
    result = runner.invoke(app, ["import-backup", str(backup)])
    assert result.exit_code == 0 and "bookmarks=1" in result.output
    assert "Renamed" in runner.invoke(app, ["show", "1"]).output


def test_cli_missing_database_fails_cleanly(monkeypatch, tmp_path):
    monkeypatch.setenv("BMK_DB_PATH", str(tmp_path / "cli.db"))
    monkeypatch.setenv("BMK_CLOUDTABS_PATH", str(tmp_path / "nope" / "CloudTabs.db"))
    result = CliRunner().invoke(app, ["ingest", "cloudtabs"])
    assert result.exit_code == 1
    assert "not found" in result.output
    runs = CliRunner().invoke(app, ["runs"]).output
    assert "CloudTabs.db not found" in runs
