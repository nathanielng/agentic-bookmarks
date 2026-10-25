import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from bmk.api import TOKEN_HEADER, create_app  # noqa: E402
from bmk.ingest import cloudtabs as ct  # noqa: E402


@pytest.fixture
def client(config, cloudtabs):
    config.cloudtabs_path = cloudtabs.path
    app = create_app(config)
    c = TestClient(app)
    c.token = config.token_path.read_text().strip()
    c.headers_w = {TOKEN_HEADER: c.token}
    return c


def scan(client):
    r = client.post("/api/ingest/cloudtabs", headers=client.headers_w)
    assert r.status_code == 200, r.text
    return r.json()


def test_token_file_is_private_and_injected_into_ui(client, config):
    assert oct(config.token_path.stat().st_mode & 0o777) == "0o600"
    page = client.get("/")
    assert page.status_code == 200
    assert f'content="{client.token}"' in page.text
    assert client.get("/static/app.js").status_code == 200


def test_writes_need_token(client):
    assert client.post("/api/ingest/cloudtabs").status_code == 403
    assert client.post("/api/ingest/cloudtabs", headers={TOKEN_HEADER: "wrong"}).status_code == 403
    assert client.get("/api/bookmarks").status_code == 200  # reads are open on localhost


def test_foreign_host_rejected(client):
    # DNS rebinding: a page on evil.example resolving to 127.0.0.1 sends its own Host header.
    assert client.get("/api/bookmarks", headers={"Host": "evil.example"}).status_code == 400


def test_scan_browse_edit_delete_restore(client, cloudtabs):
    cloudtabs.open_tab("T1", "https://example.com/a", "Alpha")
    cloudtabs.open_tab("T2", "https://example.com/b", "Beta")
    cloudtabs.open_tab("M1", "https://example.com/a?utm_source=x", "Alpha", device="DEV-MAC")
    result = scan(client)
    assert result["counts"]["new"] == 2 and result["counts"]["matched_exact"] == 1

    page = client.get("/api/bookmarks").json()
    assert page["total"] == 2
    alpha = next(i for i in page["items"] if i["title"] == "Alpha")
    assert sorted(alpha["source_labels"]) == ["Test MacBook", "Test iPhone"]

    r = client.patch(f"/api/bookmarks/{alpha['id']}", json={"title": "Alpha (renamed)"}, headers=client.headers_w)
    assert r.status_code == 200 and "title" in r.json()["locked_fields"]

    beta = next(i for i in page["items"] if i["title"] == "Beta")
    assert client.delete(f"/api/bookmarks/{beta['id']}", headers=client.headers_w).status_code == 200
    meta = client.get("/api/meta").json()
    assert meta["counts"]["active"] == 1 and meta["counts"]["trash"] == 1

    # Poll again: the edit persists and the deleted tab stays deleted.
    result = scan(client)
    assert result["counts"]["suppressed_deleted"] == 1
    assert client.get(f"/api/bookmarks/{alpha['id']}").json()["title"] == "Alpha (renamed)"
    assert client.post(f"/api/bookmarks/{beta['id']}/restore", headers=client.headers_w).status_code == 200
    assert client.get("/api/meta").json()["counts"]["active"] == 2


def test_browse_filters_combine(client, cloudtabs):
    cloudtabs.open_tab("T1", "https://example.com/rust", "Learning Rust")
    cloudtabs.open_tab("T2", "https://example.com/go", "Learning Go")
    cloudtabs.open_tab("M1", "https://example.com/rust-mac", "Rust on the Mac", device="DEV-MAC")
    scan(client)
    by_device = client.get("/api/bookmarks", params={"source_type": "cloudtabs", "source_ref": "Test MacBook"}).json()
    assert [i["title"] for i in by_device["items"]] == ["Rust on the Mac"]
    rust_phone = client.get("/api/bookmarks", params={"q": "rust", "source_ref": "Test iPhone"}).json()
    assert [i["title"] for i in rust_phone["items"]] == ["Learning Rust"]
    by_title = client.get("/api/bookmarks", params={"sort": "title", "desc": "false"}).json()
    assert [i["title"] for i in by_title["items"]] == ["Learning Go", "Learning Rust", "Rust on the Mac"]
    assert client.get("/api/bookmarks", params={"sort": "nope"}).status_code == 400


def test_bulk_actions(client, cloudtabs):
    for n in range(3):
        cloudtabs.open_tab(f"T{n}", f"https://example.com/{n}", f"Page {n}")
    scan(client)
    ids = [i["id"] for i in client.get("/api/bookmarks").json()["items"]]
    r = client.post("/api/bookmarks/bulk", json={"ids": ids[:2], "action": "status", "status": "read"}, headers=client.headers_w)
    assert r.status_code == 200
    assert client.get("/api/meta").json()["counts"]["inbox"] == 1
    client.post("/api/bookmarks/bulk", json={"ids": ids, "action": "delete"}, headers=client.headers_w)
    assert client.get("/api/meta").json()["counts"]["trash"] == 3
    bad = client.post("/api/bookmarks/bulk", json={"ids": ids, "action": "explode"}, headers=client.headers_w)
    assert bad.status_code == 400


def test_errors_map_to_http_codes(client, cloudtabs):
    assert client.get("/api/bookmarks/999").status_code == 404
    cloudtabs.open_tab("T1", "https://example.com/a", "A")
    cloudtabs.open_tab("T2", "https://example.com/b", "B")
    scan(client)
    a, b = sorted(i["id"] for i in client.get("/api/bookmarks").json()["items"])
    clash = client.patch(f"/api/bookmarks/{a}", json={"url": "https://example.com/b"}, headers=client.headers_w)
    assert clash.status_code == 409
    assert client.patch(f"/api/bookmarks/{a}", json={"bogus": 1}, headers=client.headers_w).status_code == 400


def test_permission_error_is_reported_and_shown(client, monkeypatch):
    """Acceptance step 8: a clear, actionable error in the UI (scan response + banner via /api/meta)."""

    def denied(explicit=None):
        raise ct.CloudTabsPermissionDenied(explicit or ct.CLOUDTABS_CANDIDATES[0])

    monkeypatch.setattr(ct, "locate", denied)
    r = client.post("/api/ingest/cloudtabs", headers=client.headers_w)
    assert r.status_code == 503 and r.json()["kind"] == "permission"
    assert "Full Disk Access" in r.json()["detail"]
    last = client.get("/api/meta").json()["last_cloudtabs_run"]
    assert "Full Disk Access" in last["error"]
    runs = client.get("/api/runs").json()
    assert runs[0]["error"] and runs[0]["items_seen"] == 0


def test_runs_and_items(client, cloudtabs):
    cloudtabs.open_tab("T1", "https://example.com/a", "A")
    cloudtabs.open_tab("T2", "about:blank", "Blank")
    run_id = scan(client)["run_id"]
    items = client.get(f"/api/runs/{run_id}/items").json()
    assert sorted(i["outcome"] for i in items) == ["new", "skipped"]


def test_doctor_and_export(client, cloudtabs):
    cloudtabs.open_tab("T1", "https://example.com/a", "A")
    scan(client)
    checks = {c["name"]: c for c in client.get("/api/doctor").json()}
    assert checks["Database"]["status"] == "ok"
    assert checks["iCloud Tabs access"]["status"] == "ok"
    assert checks["Last iCloud Tabs scan"]["status"] == "ok"
    backup = client.get("/api/export")
    assert backup.json()["format"] == "bmk-backup"
    assert "attachment" in backup.headers["content-disposition"]


def test_add_url(client):
    r = client.post("/api/bookmarks", json={"url": "https://example.com/manual", "title": "Manual"}, headers=client.headers_w)
    assert r.json()["outcome"] == "new"
    bad = client.post("/api/bookmarks", json={"url": "javascript:alert(1)"}, headers=client.headers_w)
    assert bad.status_code == 400
