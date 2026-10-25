"""Dedupe review (CG5): tier 3–4 proposals, guards, merge / un-merge, not_duplicates, recanonicalise.
The §7.8 acceptance tests are marked; all data is synthetic."""

import json
from importlib import resources

import pytest
from typer.testing import CliRunner

from bmk import dedupe, urls
from bmk import db as dbmod
from bmk.cli import app
from bmk.ingest.cloudtabs import CloudTabsIngester
from bmk.models import RawItem
from bmk.service import Conflict

TEXT = " ".join(f"word{i} sentence{i % 7} topic{i % 3}" for i in range(200))  # long enough to compare
OTHER_TEXT = " ".join(f"other{i} words{i % 5}" for i in range(200))


def add(lib, url, title=None, **cols):
    outcome, bid = lib.add(url, title=title)
    assert outcome == "new", (url, outcome)
    if cols:
        lib._update_columns(bid, cols)
    return bid


def with_text(text):
    from bmk.enrich.extract import simhash

    return {"content_text": text, "content_simhash": simhash(text)}


def pairs(lib):
    dedupe.scan(lib)
    return {(r["a_id"], r["b_id"]): dict(r) for r in lib.conn.execute("SELECT * FROM duplicate_candidates")}


def snapshot_row(lib, bid):
    row = dict(lib.conn.execute("SELECT * FROM bookmarks WHERE id=?", (bid,)).fetchone())
    row.pop("updated_at")
    return row


# ---------------------------------------------------------------------------------------------- §7.8


def test_tracking_params_only_is_a_tier1_match(lib):
    """§7.8: ?utm_source=a vs ?utm_source=b → one bookmark at ingest."""
    lib.ingest("rtf", [RawItem("https://example.com/p?utm_source=a", locator="#1"), RawItem("https://example.com/p?utm_source=b", locator="#2")])
    assert lib.count() == 1


def test_significant_params_are_never_proposed(lib):
    """§7.8: youtube.com/watch?v=A vs ?v=B → never proposed, even with the same title and page text."""
    a = add(lib, "https://www.youtube.com/watch?v=AAAAAAAAAAA", "Some talk about agents", **with_text(TEXT))
    b = add(lib, "https://www.youtube.com/watch?v=BBBBBBBBBBB", "Some talk about agents", **with_text(TEXT))
    assert a != b and pairs(lib) == {}


def test_trailing_slash_and_protocol_is_a_tier3_proposal(lib):
    """§7.8: http://example.com/p/ vs https://www.example.com/p → proposed (tier 3), not merged."""
    a = add(lib, "http://example.com/p/")
    b = add(lib, "https://www.example.com/p")
    found = pairs(lib)
    assert found[(a, b)]["tier"] == 3 and found[(a, b)]["confidence"] >= 0.95
    assert lib.count() == 2


def test_merge_unmerge_round_trip(lib):
    """§7.8: merge then un-merge → both rows restored (except updated_at); the pair is never proposed again."""
    a = add(lib, "http://example.com/post/", "Post", **with_text(TEXT))
    b = add(lib, "https://www.example.com/post?ref=nl")
    lib.update(b, remarks="from the newsletter")
    lib.update(a, remarks="read this")
    lib.update(b, starred=True)
    tag = lib.conn.execute("INSERT INTO tags (name) VALUES ('ai')").lastrowid
    lib.conn.execute("INSERT INTO bookmark_tags (bookmark_id, tag_id, assigned_by) VALUES (?, ?, 'user')", (b, tag))
    before = {i: snapshot_row(lib, i) for i in (a, b)}
    sources_before = {i: [s["id"] for s in lib.get(i)["sources"]] for i in (a, b)}
    aliases_before = {i: lib.get(i)["aliases"] for i in (a, b)}

    assert (a, b) in pairs(lib)
    result = lib.merge_bookmarks(a, [b])
    merged = lib.get(a)
    assert merged["remarks"] == "read this\n---\nfrom the newsletter"
    assert merged["starred"] == 1 and merged["status"] == "kept" and merged["tags"] == ["ai"]
    assert len(merged["sources"]) == 2 and lib.get(b)["merged_into_id"] == a
    assert pairs(lib) == {}

    undone = lib.unmerge(result["merges"][0])
    assert undone["kept_edits"] == []
    assert {i: snapshot_row(lib, i) for i in (a, b)} == before
    assert {i: [s["id"] for s in lib.get(i)["sources"]] for i in (a, b)} == sources_before
    assert {i: lib.get(i)["aliases"] for i in (a, b)} == aliases_before
    assert lib.get(a)["tags"] == [] and lib.get(b)["tags"] == ["ai"]
    assert pairs(lib) == {}  # remembered as not duplicates
    with pytest.raises(Conflict):
        lib.unmerge(result["merges"][0])


def test_immediate_undo_puts_the_pair_back_for_review(lib):
    a = add(lib, "http://example.com/oops/")
    b = add(lib, "https://example.com/oops")
    merge_id = lib.merge_bookmarks(a, [b])["merges"][0]
    lib.unmerge(merge_id, remember=False)
    assert (a, b) in pairs(lib)


def test_tombstone_suppression(lib, cloudtabs):
    """§7.8: a deleted CloudTabs item polled three times stays deleted, with three suppressed_deleted outcomes."""
    cloudtabs.open_tab("T1", "https://example.com/gone", "Gone")
    lib.ingest("cloudtabs", list(CloudTabsIngester(path=cloudtabs.path).read()))
    bid = lib.list()[0]["id"]
    lib.delete(bid)
    for _ in range(3):
        lib.ingest("cloudtabs", list(CloudTabsIngester(path=cloudtabs.path).read()))
    outcomes = [r[0] for r in lib.conn.execute("SELECT outcome FROM import_items WHERE bookmark_id=? ORDER BY id", (bid,))]
    assert outcomes == ["new", "suppressed_deleted", "suppressed_deleted", "suppressed_deleted"]
    assert lib.get(bid)["deleted_at"]


# ---------------------------------------------------------------------------------------------- tier 4 and guards


def test_canonical_hint_and_similar_text(lib):
    a = add(lib, "https://example.com/2026/10/post-title", "Post title")
    b = add(lib, "https://example.com/blog/category/post-title", "Post title", canonical_hint="https://example.com/2026/10/post-title?utm_source=x")
    c = add(lib, "https://example.org/a", **with_text(TEXT))
    d = add(lib, "https://example.org/b", "Same article", **with_text(TEXT))
    found = pairs(lib)
    assert found[(a, b)]["tier"] == 4 and found[(a, b)]["confidence"] == 0.9
    assert "canonical" in found[(a, b)]["rationale"]
    assert found[(c, d)]["rationale"] == "Page text is identical"


def test_shared_boilerplate_with_different_titles_is_not_proposed(lib):
    # Two different articles behind the same cookie wall extract to the same text.
    add(lib, "https://news.example.com/2026/a", "Memory prices keep rising", **with_text(TEXT))
    add(lib, "https://news.example.com/2026/b", "A model vendor changes its plans", **with_text(TEXT))
    # Too little text to compare, even with matching titles missing.
    add(lib, "https://code.example.com/x/one", **with_text("short page text only"))
    add(lib, "https://code.example.com/y/two", **with_text("short page text only"))
    assert pairs(lib) == {}


def test_title_matching_guards(lib):
    a = add(lib, "https://shop.example.com/", "Example Shop - Simplifying Smart Things")
    b = add(lib, "https://shop.example.com/about", "Example Shop - Simplifying Smart Things")
    found = pairs(lib)
    assert found[(a, b)]["confidence"] == 0.5 and found[(a, b)]["tier"] == 4  # title alone: low confidence
    # A title on more than three pages is the site's title, not a page's.
    for path in ("/c", "/d", "/e", "/f"):
        add(lib, f"https://app.example.net{path}", "Example App — design your plan")
    # Generic and site-name titles are ignored.
    add(lib, "https://social.example.org/1", "LinkedIn")
    add(lib, "https://social.example.org/2", "LinkedIn")
    add(lib, "https://example.io/a", "example.io")
    add(lib, "https://example.io/b", "example.io")
    assert set(pairs(lib)) == {(a, b)}


def test_dismissed_pairs_stay_dismissed(lib):
    a = add(lib, "http://example.com/x")
    b = add(lib, "https://example.com/x/")
    c = add(lib, "https://www.example.com/x")
    [cluster] = (dedupe.scan(lib), dedupe.clusters(lib))[1]
    assert cluster["ids"] == [a, b, c] and cluster["suggested"] == a
    with dbmod.transaction(lib.conn):
        lib.mark_not_duplicates([a, b, c])
    assert pairs(lib) == {} and dedupe.clusters(lib) == []


def test_survivor_rule_and_choice(lib):
    a = add(lib, "http://example.com/s")
    b = add(lib, "https://example.com/s/")
    lib.update(b, title="My title")  # b has a locked field, so it is suggested
    dedupe.scan(lib)
    assert dedupe.clusters(lib)[0]["suggested"] == b
    result = lib.merge_bookmarks(a, [b])  # the user keeps a anyway
    kept = lib.get(a)
    assert result["survivor"] == a and kept["title"] == "My title" and "title" in kept["locked_fields"]


def test_apply_merges_only_confident_groups(lib):
    add(lib, "http://example.com/one/")
    add(lib, "https://example.com/one")
    add(lib, "https://shop.example.com/", "Example Shop - Simplifying Smart Things")
    add(lib, "https://shop.example.com/about", "Example Shop - Simplifying Smart Things")
    dedupe.scan(lib)
    done = dedupe.apply(lib, 0.95)
    assert len(done) == 1 and lib.count() == 3
    assert [c["confidence"] for c in dedupe.clusters(lib)] == [0.5]


def test_unmerge_keeps_later_edits_and_returns_new_sources(lib):
    a = add(lib, "https://example.com/k")
    b = add(lib, "http://www.example.com/k/", "Title from b")
    result = lib.merge_bookmarks(a, [b])
    assert lib.get(a)["title"] == "Title from b"
    lib.update(a, title="Edited after the merge")
    # The merged-away URL is seen again after the merge: it attaches to the survivor through its alias...
    lib.ingest("cloudtabs", [RawItem("http://www.example.com/k/?utm_source=again", title="again", source_ref="Phone", locator="t9", observed_kind="first_seen")])
    assert len(lib.get(a)["sources"]) == 3
    undone = lib.unmerge(result["merges"][0])
    assert undone["kept_edits"] == ["title"]
    assert lib.get(a)["title"] == "Edited after the merge"
    # ...and goes back with it when the merge is undone.
    assert sorted(s["source_type"] for s in lib.get(b)["sources"]) == ["cloudtabs", "manual"]
    assert [s["source_type"] for s in lib.get(a)["sources"]] == ["manual"]


def test_cannot_undo_a_merge_whose_survivor_was_merged_later(lib):
    a = add(lib, "https://example.com/m1")
    b = add(lib, "https://example.com/m2")
    c = add(lib, "https://example.com/m3")
    first = lib.merge_bookmarks(a, [b])["merges"][0]
    second = lib.merge_bookmarks(c, [a])["merges"][0]
    with pytest.raises(Conflict, match=f"undo merge {second} first"):
        lib.unmerge(first)
    lib.unmerge(second)
    lib.unmerge(first)
    assert all(lib.get(i)["merged_into_id"] is None for i in (a, b, c))


def test_legacy_cg3_merge_is_listed_and_can_be_undone(lib):
    a = add(lib, "https://example.com/article", "Article")
    b = add(lib, "https://lnkd.in/xyz")
    # A CG3 merge: snapshot of the merged-away row in a merged_into event, no merges row.
    loser = dict(lib.conn.execute("SELECT * FROM bookmarks WHERE id=?", (b,)).fetchone())
    srcs = [r[0] for r in lib.conn.execute("SELECT id FROM sources WHERE bookmark_id=?", (b,))]
    aliases = [dict(r) for r in lib.conn.execute("SELECT * FROM url_aliases WHERE bookmark_id=?", (b,))]
    lib.conn.execute("UPDATE sources SET bookmark_id=? WHERE bookmark_id=?", (a, b))
    lib.conn.execute("UPDATE url_aliases SET bookmark_id=?, kind='merged' WHERE bookmark_id=?", (a, b))
    lib.conn.execute("UPDATE bookmarks SET merged_into_id=? WHERE id=?", (a, b))
    lib._event(b, "merged_into", {"survivor": a, "reason": "same page after resolving", "snapshot": {"bookmark": loser, "sources": srcs, "aliases": aliases}}, "2026-10-25T00:00:00Z")
    backfill = resources.files("bmk.migrations").joinpath("006_dedupe.sql").read_text().split("-- CG3's automatic")[1]
    lib.conn.executescript("\n".join(backfill.splitlines()[2:]))

    [m] = lib.merges()
    assert (m["survivor_id"], m["loser_id"], m["tier"]) == (a, b, 2)
    lib.unmerge(m["id"])
    assert lib.get(b)["merged_into_id"] is None and len(lib.get(b)["sources"]) == 1
    assert lib.conn.execute("SELECT bookmark_id FROM url_aliases WHERE url_canonical=?", (urls.canonicalize("https://lnkd.in/xyz"),)).fetchone()[0] == b


def test_recanonicalise_after_a_rule_change(lib, monkeypatch):
    a = add(lib, "https://example.com/r?campaign=1")
    b = add(lib, "https://example.com/r?campaign=2")
    assert lib.count() == 2
    rules = urls.default_rules()
    changed = urls.Rules(**{**rules.__dict__, "tracking_params": rules.tracking_params | {"campaign"}})
    monkeypatch.setattr(urls, "default_rules", lambda: changed)

    preview = dedupe.recanonicalise(lib, dry_run=True)
    assert preview["merged"] == 1 and preview["canonical"] == 1 and lib.count() == 2
    result = dedupe.recanonicalise(lib)
    assert result["merged"] == 1 and lib.count() == 1
    [survivor] = lib.list()
    assert survivor["url_canonical"] == "https://example.com/r" and {a, b} >= {survivor["id"]}
    assert lib.merges()[0]["tier"] == 1


# ---------------------------------------------------------------------------------------------- CLI and API


def test_cli_dedupe(monkeypatch, tmp_path):
    monkeypatch.setenv("COLUMNS", "250")
    monkeypatch.setenv("BMK_DB_PATH", str(tmp_path / "cli.db"))
    monkeypatch.setenv("BMK_ENRICH", "false")
    runner = CliRunner()
    runner.invoke(app, ["add", "http://example.com/c/"])
    runner.invoke(app, ["add", "https://www.example.com/c"])
    assert "1 groups to review" in runner.invoke(app, ["dedupe", "scan"]).output
    out = runner.invoke(app, ["dedupe", "show"]).output
    assert "URL" in out and "★" in out
    result = runner.invoke(app, ["dedupe", "apply", "--yes"])
    assert result.exit_code == 0 and "merged 1 groups" in result.output
    assert "same" in runner.invoke(app, ["dedupe", "history"]).output.lower()
    result = runner.invoke(app, ["dedupe", "unmerge", "1"])
    assert result.exit_code == 0 and "undone" in result.output
    assert "0 groups" in runner.invoke(app, ["dedupe", "scan"]).output
    assert runner.invoke(app, ["dedupe", "unmerge", "1"]).exit_code == 1
    assert "would change" in runner.invoke(app, ["dedupe", "recanonicalise", "--dry-run"]).output


def test_api_duplicates(config, cloudtabs):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from bmk.api import TOKEN_HEADER, create_app

    config.cloudtabs_path = cloudtabs.path
    client = TestClient(create_app(config))
    w = {TOKEN_HEADER: config.token_path.read_text().strip()}
    ids = [client.post("/api/bookmarks", json={"url": u}, headers=w).json()["id"] for u in ("http://example.com/q/", "https://example.com/q", "https://example.com/z")]
    assert client.get("/api/meta").json()["duplicates"] == 1  # scanned after each add
    [cluster] = client.get("/api/duplicates").json()["clusters"]
    assert cluster["ids"] == ids[:2] and cluster["bookmarks"][0]["sources"]

    r = client.post("/api/duplicates/merge", json={"keep": ids[1], "ids": cluster["ids"]}, headers=w)
    assert r.status_code == 200 and r.json()["survivor"] == ids[1]
    detail = client.get(f"/api/bookmarks/{ids[1]}").json()
    assert detail["merges"][0]["loser_id"] == ids[0]
    assert client.post("/api/duplicates/merge", json={"keep": ids[1], "ids": [ids[0]]}, headers=w).status_code == 409
    assert client.post(f"/api/merges/{r.json()['merges'][0]}/unmerge", headers=w).json()["restored"] == ids[0]
    assert client.post("/api/duplicates/scan", headers=w).json()["pairs"] == 0

    client.post("/api/bookmarks", json={"url": "https://www.example.com/z/"}, headers=w)
    assert client.post("/api/duplicates/dismiss", json={"ids": [ids[2], ids[2] + 1]}, headers=w).json() == {"pairs": 1}
    assert client.get("/api/duplicates").json()["clusters"] == []
    assert client.post("/api/duplicates/apply", json={}, headers=w).json() == {"groups": 0, "merges": []}
    assert json.loads(json.dumps(client.get("/api/meta").json()))["duplicates"] == 0
