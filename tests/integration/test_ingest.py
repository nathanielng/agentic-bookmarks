import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from bmk import db
from bmk.models import RawItem
from bmk.service import Conflict, Library


def ingest(lib, *items, source="markdown"):
    return lib.ingest(source, list(items))


def test_new_then_idempotent(lib):
    item = RawItem(url="https://example.com/a", title="A", source_ref="f.md", locator="1")
    first = ingest(lib, item)
    second = ingest(lib, item)
    assert first.counts["new"] == 1
    assert second.counts["unchanged"] == 1 and second.counts["new"] == 0
    assert lib.count() == 1


def test_same_url_from_three_sources_is_one_bookmark(lib):
    url = "https://example.com/article"
    lib.ingest("cloudtabs", [RawItem(url=url, source_ref="iPhone", locator="T1", observed_kind="first_seen")])
    lib.ingest("rtf", [RawItem(url=url + "?utm_source=x", source_ref="a.rtf", locator="3")])
    lib.ingest("markdown", [RawItem(url=url, source_ref="b.md", locator="9")])
    assert lib.count() == 1
    bookmark = lib.list()[0]
    assert {s["source_type"] for s in lib.get(bookmark["id"])["sources"]} == {"cloudtabs", "rtf", "markdown"}


def test_within_batch_duplicates(lib):
    summary = ingest(
        lib,
        RawItem(url="https://x.com/a/status/1?s=20", source_ref="f", locator="1"),
        RawItem(url="https://twitter.com/a/status/1", source_ref="f", locator="2"),
    )
    assert summary.counts["new"] == 1 and summary.counts["matched_exact"] == 1
    assert lib.count() == 1


def test_bad_items_do_not_fail_batch(lib):
    summary = ingest(
        lib,
        RawItem(url="about:blank", locator="1"),
        RawItem(url="not a url", locator="2"),
        RawItem(url="https://ok.example/", locator="3"),
    )
    assert summary.counts == {**summary.counts, "skipped": 1, "invalid": 1, "new": 1}
    outcomes = [i["outcome"] for i in lib.import_items(summary.run_id)]
    assert outcomes == ["skipped", "invalid", "new"]


def test_timestamp_unknown_is_null_never_now(lib):
    ingest(lib, RawItem(url="https://example.com/x", source_ref="f", locator="1"))
    b = lib.list()[0]
    assert b["saved_at"] is None and b["saved_at_source"] == "unknown"
    assert b["imported_at"]


def test_earlier_exact_timestamp_moves_saved_at(lib):
    late = datetime(2026, 10, 20, tzinfo=UTC)
    early = datetime(2026, 10, 1, tzinfo=UTC)
    ingest(lib, RawItem(url="https://e.com/", event_at=late, event_at_kind="exact", source_ref="a", locator="1"))
    ingest(lib, RawItem(url="https://e.com/", event_at=early, event_at_kind="file_mtime", source_ref="b", locator="1"))
    assert lib.list()[0]["saved_at"] == "2026-10-20T00:00:00Z"  # worse precision does not win
    ingest(lib, RawItem(url="https://e.com/", event_at=early, event_at_kind="exact", source_ref="c", locator="1"))
    assert lib.list()[0]["saved_at"] == "2026-10-01T00:00:00Z"


def test_first_seen_initial_only_for_new_source_ref(lib):
    lib.ingest("cloudtabs", [RawItem(url="https://a.com/", source_ref="iPhone", locator="T1", observed_kind="first_seen")])
    lib.ingest("cloudtabs", [RawItem(url="https://b.com/", source_ref="iPhone", locator="T2", observed_kind="first_seen")])
    lib.ingest("cloudtabs", [RawItem(url="https://c.com/", source_ref="iPad", locator="T3", observed_kind="first_seen")])
    by_domain = {b["domain"]: b["saved_at_source"] for b in lib.list()}
    assert by_domain == {"a.com": "first_seen_initial", "b.com": "first_seen", "c.com": "first_seen_initial"}


def test_user_edit_locks_and_survives_reingest(lib):
    item = RawItem(url="https://e.com/", title="Source title", source_ref="f", locator="1")
    ingest(lib, item)
    bid = lib.list()[0]["id"]
    lib.update(bid, title="My title")
    ingest(lib, RawItem(url="https://e.com/", title="Another title", source_ref="g", locator="1"))
    b = lib.get(bid)
    assert b["title"] == "My title" and b["title_source"] == "user"
    assert "title" in b["locked_fields"]
    assert b["events"][0]["kind"] == "edit"
    assert json.loads(b["events"][0]["data_json"]) == {"title": ["Source title", "My title"]}


def test_title_filled_when_missing(lib):
    ingest(lib, RawItem(url="https://e.com/", source_ref="f", locator="1"))
    ingest(lib, RawItem(url="https://e.com/", title="Found later", source_ref="g", locator="1"))
    assert lib.list()[0]["title"] == "Found later"


def test_tombstone_suppresses_resighting(lib):
    item = RawItem(url="https://e.com/", source_ref="iPhone", locator="T1", observed_kind="first_seen")
    lib.ingest("cloudtabs", [item])
    bid = lib.list()[0]["id"]
    lib.delete(bid)
    for _ in range(3):
        summary = lib.ingest("cloudtabs", [item])
        assert summary.counts["suppressed_deleted"] == 1
    assert lib.count() == 0 and lib.count("trash") == 1
    # Another device opening the same URL is still a re-sighting, not a new save.
    other = lib.ingest("cloudtabs", [RawItem(url="https://e.com/", source_ref="iPad", locator="T9", observed_kind="first_seen")])
    assert other.counts["suppressed_deleted"] == 1


def test_new_save_after_delete_is_flagged_not_restored(lib):
    ingest(lib, RawItem(url="https://e.com/", source_ref="inbox.md", locator="1"))
    bid = lib.list()[0]["id"]
    lib.delete(bid)
    later = datetime.now(UTC) + timedelta(minutes=5)
    summary = ingest(lib, RawItem(url="https://e.com/", event_at=later, event_at_kind="exact", source_ref="inbox.md", locator="7"))
    assert summary.counts["suppressed_resaved"] == 1
    assert lib.get(bid)["deleted_at"] is not None


def test_new_save_after_delete_restores_when_enabled(lib):
    lib.config.resurrect_on_new_save = True
    ingest(lib, RawItem(url="https://e.com/", source_ref="inbox.md", locator="1"))
    bid = lib.list()[0]["id"]
    lib.delete(bid)
    later = datetime.now(UTC) + timedelta(minutes=5)
    summary = ingest(lib, RawItem(url="https://e.com/", event_at=later, event_at_kind="exact", source_ref="inbox.md", locator="7"))
    assert summary.counts["matched_exact"] == 1
    assert lib.get(bid)["deleted_at"] is None


def test_restore(lib):
    ingest(lib, RawItem(url="https://e.com/", source_ref="f", locator="1"))
    bid = lib.list()[0]["id"]
    lib.delete(bid)
    lib.restore(bid)
    assert lib.count() == 1
    assert [e["kind"] for e in lib.get(bid)["events"]] == ["delete", "restore"]


def test_url_edit_keeps_old_alias_and_detects_conflict(lib):
    ingest(lib, RawItem(url="https://e.com/old", source_ref="f", locator="1"), RawItem(url="https://e.com/other", source_ref="f", locator="2"))
    old = next(b for b in lib.list() if b["url_canonical"].endswith("/old"))
    b = lib.update(old["id"], url="https://e.com/new")
    assert b["url"] == "https://e.com/new" and b["metadata_stale"] == 1 and "url" in b["locked_fields"]
    # Re-ingesting the old URL maps to the same bookmark via the alias table.
    summary = ingest(lib, RawItem(url="https://e.com/old", source_ref="g", locator="1"))
    assert summary.counts["matched_alias"] == 1
    with pytest.raises(Conflict):
        lib.update(old["id"], url="https://e.com/other")


def test_status_validation(lib):
    ingest(lib, RawItem(url="https://e.com/", source_ref="f", locator="1"))
    bid = lib.list()[0]["id"]
    with pytest.raises(ValueError):
        lib.update(bid, status="bogus")
    assert lib.update(bid, status="kept")["status"] == "kept"


def test_search(lib):
    ingest(
        lib,
        RawItem(url="https://e.com/1", title="Agentic coding with Bedrock", source_ref="f", locator="1"),
        RawItem(url="https://e.com/2", title="Sourdough recipes", source_ref="f", locator="2"),
    )
    assert [r["title"] for r in lib.search("bedr")] == ["Agentic coding with Bedrock"]
    assert lib.search('"unbalanced') == []  # quotes in user input never raise
    bid = lib.search("sourdough")[0]["id"]
    lib.update(bid, remarks="try the rye one")
    assert lib.search("rye")[0]["id"] == bid
    lib.delete(bid)
    assert lib.search("sourdough") == []


def test_manual_add(lib):
    outcome, bid = lib.add("https://e.com/x", title="X", remarks="note")
    assert outcome == "new"
    b = lib.get(bid)
    assert b["saved_at_source"] == "exact" and b["remarks"] == "note"
    with pytest.raises(ValueError):
        lib.add("javascript:alert(1)")


def test_backup_round_trip(lib, tmp_path, config):
    ingest(lib, RawItem(url="https://e.com/1", title="One", source_ref="f", locator="1", extra={"note": "hello"}))
    lib.update(lib.list()[0]["id"], title="Edited")
    ingest(lib, RawItem(url="https://e.com/2", source_ref="f", locator="2"))
    lib.delete(lib.list()[0]["id"])
    backup = lib.export_backup()

    fresh = Library(db.connect(tmp_path / "fresh.db"), config)
    fresh.import_backup(json.loads(json.dumps(backup)))
    again = fresh.export_backup()
    backup.pop("exported_at"), again.pop("exported_at")
    assert again == backup
    assert fresh.search("edited")[0]["title"] == "Edited"  # FTS rebuilt by triggers
    with pytest.raises(Conflict):
        fresh.import_backup(backup | {"exported_at": "x"})
    fresh.close()


def test_migrations_idempotent(config):
    conn = db.connect(config.db_path)
    assert db.schema_version(conn) == db.latest_version()
    assert db.apply_migrations(conn) == []
    conn.close()


def test_migration_002_maps_old_statuses_to_kept(tmp_path):
    path = tmp_path / "v1.db"
    conn = sqlite3.connect(path)
    conn.executescript(db._migrations()[0][2] + "\nPRAGMA user_version = 1;")
    now = "2026-10-25T00:00:00Z"
    for i, status in enumerate(["inbox", "read", "archived"], 1):
        conn.execute(
            "INSERT INTO bookmarks (id, url_original, url_canonical, title, imported_at, updated_at, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (i, f"https://e.com/{i}", f"https://e.com/{i}", f"zebra {status}", now, now, status),
        )
        conn.execute(
            "INSERT INTO sources (bookmark_id, source_type, raw_url, first_seen_at, last_seen_at) VALUES (?, 'x', ?, ?, ?)",
            (i, f"https://e.com/{i}", now, now),
        )
    conn.commit()
    conn.close()

    conn = db.connect(path)
    assert db.schema_version(conn) == db.latest_version()
    assert dict(conn.execute("SELECT id, status FROM bookmarks").fetchall()) == {1: "inbox", 2: "kept", 3: "kept"}
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("SELECT count(*) FROM sources").fetchone()[0] == 3
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE bookmarks SET status = 'read' WHERE id = 1")
    lib = Library(conn)
    assert {b["id"] for b in lib.search("zebra")} == {1, 2, 3}  # FTS index rebuilt
    lib.update(1, title="giraffe")
    assert [b["id"] for b in lib.search("giraffe")] == [1]  # FTS triggers recreated
    assert lib.count("kept") == 2 and lib.count("inbox") == 1
    conn.close()
