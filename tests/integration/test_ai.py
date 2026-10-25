"""AI categorization (CG6): category proposals, classification, changeset review / apply / revert, and the
MCP server. The models are replaced with fakes; all data is synthetic."""

import asyncio
import json
from dataclasses import replace

import pytest
from typer.testing import CliRunner

from bmk import changesets
from bmk.ai import categorize, taxonomy
from bmk.cli import app
from bmk.service import Conflict

TOPICS = ["recipes", "python", "travel"]


def seed(lib, n=12):
    ids = []
    for i in range(n):
        topic = TOPICS[i % 3]
        outcome, bid = lib.add(f"https://{topic}.example.com/page-{i}", title=f"{topic.title()} page {i}")
        assert outcome == "new"
        ids.append(bid)
    return ids


def proposal(names):
    return json.dumps({"categories": [{"name": n, "description": f"Pages about {n.lower()}."} for n in names]})


def taxonomy_changeset(lib, config, names=("Cooking", "Programming", "Travel")):
    return taxonomy.propose(lib, config, generator=lambda _m: proposal(names), lo=2, hi=6)


def make_categories(lib, names=("Cooking", "Programming", "Travel")):
    for name in names:
        lib.add_category(name, f"Pages about {name.lower()}.", "user")


def approve_all(lib, cs_id):
    changesets.decide(lib, cs_id, "approved")
    return changesets.apply(lib, cs_id)


def by_topic(state, _instructions, options):
    """A fake decider: picks the category whose name matches the topic in the URL."""
    pick = {"recipes": "Cooking", "python": "Programming", "travel": "Travel"}
    for topic, name in pick.items():
        if f"{topic}.example.com" in state and name in options:
            return {name: 0.9, "other": 0.1}
    return {"other": 1.0}


# ---------------------------------------------------------------------------------------------- taxonomy


def test_parse_rejects_catch_alls_and_counts():
    with pytest.raises(ValueError, match="catch-all"):
        taxonomy.parse(proposal(["Cooking", "Misc"]), [], 2, 6)
    with pytest.raises(ValueError, match="between 3 and 6"):
        taxonomy.parse(proposal(["Cooking", "Travel"]), [], 3, 6)
    with pytest.raises(ValueError, match="no JSON"):
        taxonomy.parse("sorry, I can't", [], 2, 6)
    got = taxonomy.parse("Here you go:\n" + proposal(["Cooking", "cooking", "Travel"]), [], 2, 6)
    assert [c["name"] for c in got] == ["Cooking", "Travel"]  # duplicates dropped


def test_propose_retries_with_feedback_then_stores_a_pending_changeset(lib, config):
    seed(lib)
    answers = iter(["not json", proposal(["Cooking", "Programming", "Travel"])])
    seen = []

    def generator(messages):
        seen.append(messages[-1]["content"])
        return next(answers)

    cs_id = taxonomy.propose(lib, config, generator=generator, lo=2, hi=6)
    assert "no JSON" in seen[1]  # the reason was fed back
    cs = changesets.get(lib, cs_id)
    assert cs["status"] == "pending" and cs["kind"] == "taxonomy"
    assert [i["new_value"]["name"] for i in cs["items"]] == ["Cooking", "Programming", "Travel"]
    assert lib.categories() == []  # nothing changes before approval


def test_propose_fails_after_three_bad_answers(lib, config):
    seed(lib)
    with pytest.raises(ValueError, match="three times"):
        taxonomy.propose(lib, config, generator=lambda _m: "{}", lo=2, hi=6)
    assert changesets.list_(lib)[0]["status"] == "failed"


def test_approving_part_of_a_taxonomy(lib, config):
    seed(lib)
    cs_id = taxonomy_changeset(lib, config)
    items = changesets.get(lib, cs_id)["items"]
    changesets.decide(lib, cs_id, "approved", item_ids=[items[0]["id"], items[2]["id"]])
    changesets.decide(lib, cs_id, "rejected", item_ids=[items[1]["id"]])
    assert changesets.apply(lib, cs_id) == {"applied": 2, "conflicts": 0, "left": 0}
    assert [c["name"] for c in lib.categories()] == ["Cooking", "Travel"]
    assert changesets.get(lib, cs_id)["status"] == "applied"


# ---------------------------------------------------------------------------------------------- categorize


def test_categorize_proposes_and_skips_user_choices(lib, config):
    ids = seed(lib)
    approve_all(lib, taxonomy_changeset(lib, config))
    travel = lib.category_by_name("Travel")["id"]
    lib.update(ids[0], category_id=travel)  # the user's own (wrong) choice stays

    cs_id = categorize.run(lib, config, choose=by_topic).changeset_id
    cs = changesets.get(lib, cs_id)
    assert cs["status"] == "pending"
    assert ids[0] not in [i["bookmark_id"] for i in cs["items"]]
    assert len(cs["items"]) == len(ids) - 1
    assert categorize.run(lib, config, choose=by_topic).changeset_id is None  # already waiting in a proposal

    changesets.decide(lib, cs_id, "approved", min_confidence=0.8)
    assert changesets.apply(lib, cs_id)["applied"] == len(ids) - 1
    b = lib.get(ids[1])
    assert (b["category_id"], b["category_assigned_by"]) == (lib.category_by_name("Programming")["id"], "ai")
    assert lib.get(ids[0])["category_assigned_by"] == "user"


def test_other_is_not_proposed(lib, config):
    seed(lib, 3)
    lib.add("https://unrelated.example.org/x", title="Something else")
    make_categories(lib, ("Cooking", "Programming"))
    cs = changesets.get(lib, categorize.run(lib, config, choose=by_topic).changeset_id)
    assert len(cs["items"]) == 2 and "fit no category" in cs["summary"]


def test_changing_the_category_before_approving(lib, config):
    ids = seed(lib, 3)
    make_categories(lib)
    cs_id = categorize.run(lib, config, choose=by_topic, ids=[ids[0]]).changeset_id
    item = changesets.get(lib, cs_id)["items"][0]
    travel = lib.category_by_name("Travel")["id"]
    changesets.decide(lib, cs_id, "approved", item_ids=[item["id"]], new_values={item["id"]: travel})
    changesets.apply(lib, cs_id)
    assert lib.get(ids[0])["category_id"] == travel


def test_conflict_when_the_user_changed_it_meanwhile(lib, config):
    ids = seed(lib, 3)
    make_categories(lib)
    cs_id = categorize.run(lib, config, choose=by_topic).changeset_id
    lib.update(ids[0], category_id=lib.category_by_name("Travel")["id"])
    result = approve_all(lib, cs_id)
    assert result["conflicts"] == 1 and result["applied"] == 2
    assert lib.get(ids[0])["category_id"] == lib.category_by_name("Travel")["id"]
    assert changesets.get(lib, cs_id)["status"] == "partial"


def test_revert_restores_and_leaves_later_edits(lib, config):
    ids = seed(lib, 3)
    make_categories(lib)
    cs_id = categorize.run(lib, config, choose=by_topic).changeset_id
    approve_all(lib, cs_id)
    lib.update(ids[1], category_id=lib.category_by_name("Travel")["id"])  # edited after applying
    assert changesets.revert(lib, cs_id) == {"reverted": 2, "skipped": 1}
    assert lib.get(ids[0])["category_id"] is None
    assert lib.get(ids[1])["category_assigned_by"] == "user"
    with pytest.raises(Conflict):
        changesets.revert(lib, cs_id)


def test_reverting_a_taxonomy_keeps_categories_in_use(lib, config):
    ids = seed(lib)
    tax = taxonomy_changeset(lib, config)
    approve_all(lib, tax)
    lib.update(ids[0], category_id=lib.category_by_name("Cooking")["id"])
    assert changesets.revert(lib, tax) == {"reverted": 2, "skipped": 1}
    assert [c["name"] for c in lib.categories()] == ["Cooking"]


def test_merge_items_apply_and_revert(lib, config):
    ids = seed(lib, 3)
    cs_id = changesets.create(lib, "agent", author="test", summary="merge")
    changesets.add_item(lib, cs_id, "merge", bookmark_id=ids[1], target_id=ids[0], rationale="same page")
    approve_all(lib, cs_id)
    assert lib.conn.execute("SELECT merged_into_id FROM bookmarks WHERE id=?", (ids[1],)).fetchone()[0] == ids[0]
    assert changesets.revert(lib, cs_id)["reverted"] == 1
    assert lib.conn.execute("SELECT merged_into_id FROM bookmarks WHERE id=?", (ids[1],)).fetchone()[0] is None


# ---------------------------------------------------------------------------------------------- CLI


def test_cli_categories_and_changesets(lib, config, monkeypatch):
    seed(lib)
    monkeypatch.setenv("BMK_DB_PATH", str(config.db_path))
    run = CliRunner().invoke
    assert run(app, ["categories", "add", "Cooking", "--description", "Food and recipes."]).exit_code == 0
    cs_id = taxonomy.propose(lib, config, generator=lambda _m: proposal(["Programming", "Travel"]), lo=2, hi=6)
    out = run(app, ["changeset", "show", str(cs_id)])
    assert out.exit_code == 0 and "Programming" in out.output
    assert run(app, ["changeset", "approve", str(cs_id), "--all"]).exit_code == 0
    assert run(app, ["changeset", "apply", str(cs_id)]).exit_code == 0
    out = run(app, ["categories"])
    assert all(n in out.output for n in ("Cooking", "Programming", "Travel"))
    bid = lib.browse(view="inbox")["items"][0]["id"]
    assert run(app, ["categories", "set", str(bid), "--category", "Travel"]).exit_code == 0
    assert lib.get(bid)["category_assigned_by"] == "user"


# ---------------------------------------------------------------------------------------------- MCP


def call(server, name, **args):
    result = asyncio.run(server.call_tool(name, args))
    if result.is_error:
        raise RuntimeError(result.content[0].text)
    data = result.structured_content
    return data["result"] if set(data) == {"result"} else data


def test_mcp_reads_and_proposes_without_changing(lib, config):
    pytest.importorskip("mcp")
    from bmk.mcp_server import build_server

    ids = seed(lib, 3)
    lib.add_category("Cooking", "Food and recipes.", "user")
    server = build_server(config)
    tools = {t.name for t in asyncio.run(server.list_tools())}
    assert "propose_categories" in tools and "add_bookmark" not in tools and not any("apply" in t for t in tools)

    hits = call(server, "search_bookmarks", query="recipes")
    assert [h["id"] for h in hits] == [ids[0]]
    assert call(server, "get_bookmark", bookmark_id=ids[0])["url"].startswith("https://recipes.example.com")
    assert call(server, "list_categories")["uncategorised"] == 3

    out = call(server, "propose_categories", assignments=[{"bookmark_id": ids[0], "category": "Cooking", "confidence": 0.9}])
    assert lib.get(ids[0])["category_id"] is None  # waiting for approval
    cs = changesets.get(lib, out["changeset_id"])
    assert (cs["kind"], cs["status"], cs["author"]) == ("agent", "pending", "mcp")

    lib.update(ids[1], category_id=lib.category_by_name("Cooking")["id"])
    with pytest.raises(Exception, match="the user set"):
        call(server, "propose_categories", assignments=[{"bookmark_id": ids[1], "category": "Cooking"}])
    with pytest.raises(Exception, match="no category"):
        call(server, "propose_categories", assignments=[{"bookmark_id": ids[2], "category": "Nope"}])

    out = call(server, "propose_merge", keep_id=ids[0], other_ids=[ids[2]], rationale="test")
    assert changesets.get(lib, out["changeset_id"])["items"][0]["op"] == "merge"


def test_mcp_writes_only_when_allowed(lib, config):
    pytest.importorskip("mcp")
    from bmk.mcp_server import build_server

    server = build_server(replace(config, mcp_allow_writes=True))
    out = call(server, "add_bookmark", url="https://new.example.com/a", title="New")
    assert out["outcome"] == "new"
    assert call(server, "triage_bookmark", bookmark_id=out["id"], action="star")["starred"] is True
    call(server, "triage_bookmark", bookmark_id=out["id"], action="trash")
    assert lib.get(out["id"])["deleted_at"] is not None


# ---------------------------------------------------------------------------------------------- API


def test_api_categories_and_review(lib, config):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from bmk.api import TOKEN_HEADER, create_app

    ids = seed(lib, 3)
    client = TestClient(create_app(config))
    w = {TOKEN_HEADER: client.get("/").text.split('name="bmk-token" content="')[1].split('"')[0]}

    cat = client.post("/api/categories", json={"name": "Cooking", "description": "Food."}, headers=w).json()
    assert client.post("/api/categories", json={"name": "cooking"}, headers=w).status_code == 409
    make_categories(lib, ("Programming", "Travel"))
    meta = client.get("/api/meta").json()
    assert [c["name"] for c in meta["categories"]] == ["Cooking", "Programming", "Travel"] and meta["uncategorised"] == 3

    cs_id = categorize.run(lib, config, choose=by_topic).changeset_id
    assert client.get("/api/meta").json()["pending_changesets"] == 1
    items = client.get(f"/api/changesets/{cs_id}").json()["items"]
    r = client.post(f"/api/changesets/{cs_id}/decide", json={"decision": "approved", "min_confidence": 0.8}, headers=w)
    assert r.json()["changed"] == 3
    r = client.post(f"/api/changesets/{cs_id}/decide", json={"decision": "rejected", "item_ids": [items[0]["id"]]}, headers=w)
    assert client.post(f"/api/changesets/{cs_id}/apply", headers=w).json() == {"applied": 2, "conflicts": 0, "left": 0}
    assert client.get("/api/bookmarks", params={"category": cat["id"], "view": "active"}).json()["total"] == 1
    assert client.get("/api/bookmarks", params={"category": "none", "view": "active"}).json()["total"] == 1
    assert client.post(f"/api/changesets/{cs_id}/revert", headers=w).json()["reverted"] == 2

    r = client.post("/api/bookmarks/bulk", json={"ids": ids, "action": "category", "category_id": cat["id"]}, headers=w)
    assert r.status_code == 200 and lib.get(ids[2])["category_assigned_by"] == "user"
    assert client.delete(f"/api/categories/{cat['id']}", headers=w).json() == {"uncategorised": 3}


def test_category_list_is_mirrored_to_a_settings_file(lib, config, tmp_path):
    ids = seed(lib)
    approve_all(lib, taxonomy_changeset(lib, config))
    saved = json.loads(config.categories_path.read_text())
    assert config.categories_path.parent == config.db_path.parent
    assert [c["name"] for c in saved["categories"]] == ["Cooking", "Programming", "Travel"]
    lib.update_category(lib.category_by_name("Travel")["id"], description="Trips.")
    lib.delete_category(lib.category_by_name("Cooking")["id"])
    saved = json.loads(config.categories_path.read_text())
    assert saved["categories"] == [{"name": "Programming", "description": "Pages about programming."}, {"name": "Travel", "description": "Trips."}]

    from bmk import db as dbmod
    from bmk.service import Library

    other = Library(dbmod.connect(tmp_path / "other" / "bookmarks.db"), replace(config, db_path=tmp_path / "other" / "bookmarks.db"))
    assert other.load_categories_file(config.categories_path) == ["Programming", "Travel"]
    assert other.load_categories_file(config.categories_path) == []
    other.close()
    assert ids


# ---------------------------------------------------------------------------------------------- jobs


def run_job(lib, config, kind, params=None, **kwargs):
    from bmk.ai import jobs

    job_id = jobs.create(lib, kind, params or {}, author="test")
    job = jobs.claim(lib, job_id)
    return jobs.execute(lib, config, job, **kwargs)


def test_job_succeeds_and_hands_over_the_changeset(lib, config):
    seed(lib, 6)
    make_categories(lib)
    seen = []
    config = replace(config, auto_approve_min=0)  # review everything
    job = run_job(lib, config, "categorize", choose=by_topic, on_progress=lambda j: seen.append((j["state"], j["done"])))
    assert (job["state"], job["done"], job["total"], job["errors"], job["pid"]) == ("succeeded", 6, 6, 0, None)
    assert seen[0] == ("running", 1)
    assert changesets.get(lib, job["changeset_id"])["status"] == "pending"


def test_job_with_some_failures_succeeds_with_errors(lib, config):
    seed(lib, 6)
    make_categories(lib)
    calls = []

    def flaky(state, instructions, options):
        calls.append(1)
        if len(calls) == 2:
            raise TimeoutError("decider timed out")
        return by_topic(state, instructions, options)

    job = run_job(lib, config, "categorize", choose=flaky)
    assert (job["state"], job["errors"]) == ("succeeded_with_errors", 1)
    assert "timed out" in job["error"]
    assert len(changesets.get(lib, job["changeset_id"])["items"]) == 5


def test_job_fails_when_the_model_keeps_failing(lib, config):
    seed(lib, 9)
    make_categories(lib)

    def broken(*_args):
        raise ConnectionError("llama-server is down")

    job = run_job(lib, config, "categorize", choose=broken)
    assert job["state"] == "failed" and "in a row" in job["error"]
    assert changesets.get(lib, job["changeset_id"])["status"] == "failed"  # nothing was proposed


def test_pause_and_resume_continue_where_it_stopped(lib, config):
    from bmk.ai import jobs

    seed(lib, 9)
    config = replace(config, auto_approve_min=0)
    make_categories(lib, ("Cooking", "Programming"))  # travel pages are 'other': no item, must not be redone
    calls = []

    def counting(state, instructions, options):
        calls.append(state)
        return by_topic(state, instructions, options)

    def pause_at_4(job):
        if job["done"] == 4:
            jobs.control(lib, job["id"], "pause")

    job = run_job(lib, config, "categorize", choose=counting, on_progress=pause_at_4)
    assert (job["state"], job["done"]) == ("paused", 4)
    assert changesets.get(lib, job["changeset_id"])["status"] == "running"  # not reviewable half-done

    jobs.control(lib, job["id"], "resume")
    job = jobs.execute(lib, config, jobs.claim(lib), choose=counting)
    assert (job["state"], job["done"], job["total"]) == ("succeeded", 9, 9)
    assert len(calls) == 9 and len(set(calls)) == 9  # every bookmark exactly once
    cs = changesets.get(lib, job["changeset_id"])
    assert cs["status"] == "pending" and len(cs["items"]) == 6


def test_cancel_keeps_what_was_proposed(lib, config):
    from bmk.ai import jobs

    seed(lib, 6)
    make_categories(lib)
    job = run_job(lib, config, "categorize", choose=by_topic,
                  on_progress=lambda j: j["done"] == 2 and jobs.control(lib, j["id"], "cancel"))  # fmt: skip
    assert job["state"] == "cancelled"
    cs = changesets.get(lib, job["changeset_id"])
    assert cs["status"] == "pending" and len(cs["items"]) == 2


def test_one_job_at_a_time_and_dead_processes_are_reaped(lib, config):
    from bmk.ai import jobs

    seed(lib, 3)
    make_categories(lib)
    first = jobs.create(lib, "categorize", author="test")
    second = jobs.create(lib, "categorize", author="test")
    assert jobs.claim(lib)["id"] == first
    assert jobs.claim(lib) is None  # first is running (this process is alive)
    lib.conn.execute("UPDATE ai_jobs SET pid=999999 WHERE id=?", (first,))  # its process died
    assert jobs.get(lib, first)["state"] == "failed"
    assert jobs.claim(lib)["id"] == second
    with pytest.raises(Conflict):
        jobs.control(lib, first, "resume")


def test_taxonomy_job(lib, config):
    seed(lib)
    job = run_job(lib, config, "taxonomy", {"min": 2, "max": 6}, generator=lambda _m: proposal(["Cooking", "Travel"]))
    assert (job["state"], job["message"]) == ("succeeded", "2 categories proposed")
    assert changesets.get(lib, job["changeset_id"])["kind"] == "taxonomy"
    from bmk.ai import jobs

    with pytest.raises(Conflict, match="only classification"):
        jobs.control(lib, jobs.create(lib, "taxonomy", author="test"), "pause")


def test_api_jobs(lib, config, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from bmk.ai import jobs
    from bmk.api import TOKEN_HEADER, create_app

    spawned = []
    monkeypatch.setattr(jobs, "spawn", lambda cfg: spawned.append(cfg))
    seed(lib, 3)
    make_categories(lib)
    client = TestClient(create_app(config))
    w = {TOKEN_HEADER: config.token_path.read_text().strip()}
    job = client.post("/api/ai/jobs", json={"kind": "categorize"}, headers=w).json()
    assert job["state"] == "queued" and spawned
    assert client.get("/api/meta").json()["ai_jobs_active"] == 1
    assert client.post(f"/api/ai/jobs/{job['id']}/pause", headers=w).json()["state"] == "paused"
    assert client.post(f"/api/ai/jobs/{job['id']}/resume", headers=w).json()["state"] == "queued"
    assert client.post(f"/api/ai/jobs/{job['id']}/cancel", headers=w).json()["state"] == "cancelled"
    assert client.post(f"/api/ai/jobs/{job['id']}/resume", headers=w).status_code == 409
    assert client.post("/api/ai/jobs", json={"kind": "nope"}, headers=w).status_code == 400


def test_jobs_record_timing_for_estimates(lib, config, monkeypatch):
    from bmk.ai import jobs

    monkeypatch.setattr(jobs, "hardware", lambda: "Test chip, 8 GB")
    seed(lib, 4)
    make_categories(lib)

    class FakeDecider:
        last_tokens, last_seconds = 0, 0.0

        def choose(self, state, instructions, options):
            self.last_tokens, self.last_seconds = 500, 0.25
            return by_topic(state, instructions, options)

    job = run_job(lib, config, "categorize", choose=FakeDecider().choose)
    st = job["stats"]
    assert (job["tokens_in"], job["model_seconds"], job["hardware"]) == (2000, 1.0, "Test chip, 8 GB")
    assert st["tokens_per_item"] == 500 and st["tokens_per_second"] == 2000 and "seconds_left" not in st
    assert jobs.rate(lib)["job"] == job["id"]


# ---------------------------------------------------------------------------------------------- auto-approve and the review log


def graded_choose(state, _instructions, options):
    """Confident for recipes (0.95), unsure for python (0.6, close to travel), 'other' for travel."""
    if "recipes.example.com" in state:
        return {"Cooking": 0.95, "other": 0.05}
    if "python.example.com" in state:
        return {"Programming": 0.6, "Travel": 0.35, "other": 0.05}
    return {"other": 1.0}


def test_auto_approve_applies_only_the_confident_ones(lib, config):
    ids = seed(lib, 9)
    make_categories(lib)
    job = run_job(lib, replace(config, auto_approve_min=0.9), "categorize", choose=graded_choose)
    cs = changesets.get(lib, job["changeset_id"])
    assert cs["status"] == "partial" and "3 applied automatically" in cs["summary"] and "3 left to review" in cs["summary"]
    cooking = lib.category_by_name("Cooking")["id"]
    assert [i for i in ids if lib.get(i)["category_id"] == cooking] == ids[0::3]
    assert lib.get(ids[0])["category_assigned_by"] == "ai"
    assert lib.get(ids[1])["category_id"] is None  # python pages wait for a person
    assert cs["counts"] == {"applied": 3, "pending": 3}
    log = [dict(r) for r in lib.conn.execute("SELECT outcome, decided_by FROM review_log")]
    assert log == [{"outcome": "auto_approved", "decided_by": "auto"}] * 3


def test_auto_approve_off_leaves_everything_for_review(lib, config):
    seed(lib, 6)
    make_categories(lib)
    job = run_job(lib, replace(config, auto_approve_min=0), "categorize", choose=graded_choose)
    assert changesets.get(lib, job["changeset_id"])["status"] == "pending"
    assert lib.conn.execute("SELECT COUNT(*) FROM review_log").fetchone()[0] == 0


def test_human_decisions_are_logged_and_corrections_counted(lib, config):
    ids = seed(lib, 9)
    make_categories(lib)
    job = run_job(lib, replace(config, auto_approve_min=0.9), "categorize", choose=graded_choose)
    cs_id = job["changeset_id"]
    items = {i["bookmark_id"]: i for i in changesets.get(lib, cs_id)["items"]}
    unsure = [items[b] for b in ids[1::3]]
    travel = lib.category_by_name("Travel")["id"]
    changesets.decide(lib, cs_id, "approved", item_ids=[unsure[0]["id"]])  # agrees
    changesets.decide(lib, cs_id, "approved", item_ids=[unsure[1]["id"]], new_values={unsure[1]["id"]: travel})  # corrects
    changesets.decide(lib, cs_id, "rejected", item_ids=[unsure[2]["id"]])
    changesets.apply(lib, cs_id)
    human = [(r["outcome"], r["proposed"], r["final"]) for r in lib.conn.execute("SELECT * FROM review_log WHERE decided_by='human' ORDER BY id")]
    assert human == [("approved", "Programming", "Programming"), ("corrected", "Programming", "Travel"), ("rejected", "Programming", "Programming")]

    lib.update(ids[0], category_id=travel)  # after applying, the user fixes an auto-approved one
    assert lib.conn.execute("SELECT outcome, proposed, final FROM review_log ORDER BY id DESC LIMIT 1").fetchone()[:] == ("corrected_after_apply", "Cooking", "Travel")

    stats = changesets.review_stats(lib)
    top = stats["bands"][0]
    assert (top["decided"], top["auto"], top["wrong"]) == (3, 3, 1)  # one of three auto-approved was fixed later
    assert {(c["proposed"], c["final"]): c["count"] for c in stats["confusions"]} == {("Programming", "Travel"): 1, ("Programming", "(none)"): 1, ("Cooking", "Travel"): 1}
    labels = changesets.export_labels(lib)
    assert {"proposed": "Programming", "label": "Travel", "outcome": "corrected"}.items() <= next(l for l in labels if l["outcome"] == "corrected").items()
    assert any(l["outcome"] == "rejected" and l["label"] is None for l in labels)


def test_the_rule_never_overrides_a_person(lib, config):
    seed(lib, 6)
    make_categories(lib)
    cs_id = categorize.run(lib, config, choose=graded_choose).changeset_id
    item = next(i for i in changesets.get(lib, cs_id)["items"] if i["confidence"] >= 0.9)
    changesets.decide(lib, cs_id, "rejected", item_ids=[item["id"]])
    assert changesets.auto_apply(lib, cs_id, 0.9)["approved"] == 1  # the other confident one
    assert [i["decision"] for i in changesets.get(lib, cs_id)["items"] if i["id"] == item["id"]] == ["rejected"]


def test_trashing_a_proposal_moves_the_bookmark_to_trash_without_logging(lib, config):
    ids = seed(lib, 3)
    make_categories(lib)
    cs_id = categorize.run(lib, config, choose=by_topic).changeset_id
    item = changesets.get(lib, cs_id)["items"][0]
    assert changesets.trash_item(lib, cs_id, item["id"]) == item["bookmark_id"]
    assert lib.get(item["bookmark_id"])["deleted_at"] is not None
    after = next(i for i in changesets.get(lib, cs_id)["items"] if i["id"] == item["id"])
    assert (after["decision"], after["result"]) == ("rejected", "moved to Trash")
    assert lib.conn.execute("SELECT COUNT(*) FROM review_log").fetchone()[0] == 0
    changesets.decide(lib, cs_id, "approved")
    assert changesets.apply(lib, cs_id)["applied"] == 2  # the trashed one is not applied
    with pytest.raises(Conflict):
        changesets.trash_item(lib, cs_id, next(i["id"] for i in changesets.get(lib, cs_id)["items"] if i["decision"] == "applied"))
    assert ids


def test_search_counts_show_where_matches_are(lib):
    a = lib.add("https://one.example.com/x", title="We ran 94,000 trials of the thing")[1]
    lib.add("https://two.example.com/y", title="Something else entirely")
    lib.update(a, status="kept")
    counts = lib.view_counts(q="94,000")
    assert (counts["inbox"], counts["kept"], counts["active"], counts["trash"]) == (0, 1, 1, 0)
    assert lib.browse(view="inbox", q="94,000")["total"] == 0 and lib.browse(view="kept", q="94,000")["total"] == 1
    assert lib.view_counts()["inbox"] == 1  # without a search the counts are unchanged
