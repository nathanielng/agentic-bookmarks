"""Enrichment against a simulated web (httpx.MockTransport); no real network access."""

import json

import httpx
import pytest

from bmk.enrich import extract, fetch
from bmk.enrich.fetch import FetchError, Fetcher
from bmk.enrich.worker import BACKOFF, Worker
from bmk.models import RawItem

PUBLIC = "93.184.216.34"


class FakeWeb:
    """Routes URL → response; counts requests per URL."""

    def __init__(self):
        self.routes: dict[str, object] = {}
        self.hits: dict[str, int] = {}
        self.private_hosts = {"router.test": "192.168.1.1"}

    def resolve(self, host: str, port: int) -> list[str]:
        return [self.private_hosts.get(host, PUBLIC)]

    def page(self, url, html, status=200, headers=None):
        self.routes[url] = lambda: httpx.Response(status, headers={"content-type": "text/html; charset=utf-8", **(headers or {})}, text=html)

    def redirect(self, url, to, status=301):
        self.routes[url] = lambda: httpx.Response(status, headers={"location": to})

    def json(self, url, data, status=200):
        self.routes[url] = lambda: httpx.Response(status, json=data)

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.hits[url] = self.hits.get(url, 0) + 1
        route = self.routes.get(url)
        return route() if route else httpx.Response(404, text="not here")

    def fetcher(self) -> Fetcher:
        return Fetcher(resolver=self.resolve, transport=httpx.MockTransport(self.handler))


ARTICLE = """<html><head><title>Rust ownership explained | Example Blog</title>
<meta property="og:title" content="Rust ownership explained">
<meta property="og:description" content="A gentle guide to borrowing.">
<meta property="og:site_name" content="Example Blog">
<meta property="og:image" content="/img/cover.png">
<meta name="author" content="A. Writer">
<link rel="canonical" href="https://example.com/rust-ownership">
</head><body><article><h1>Rust ownership explained</h1>
<p>Ownership is Rust's most unique feature, and it enables Rust to make memory safety guarantees without a garbage collector.</p>
<p>Each value in Rust has a variable that is called its owner. There can only be one owner at a time.</p>
<p>When the owner goes out of scope, the value will be dropped, which frees the memory it used.</p>
</article></body></html>"""


@pytest.fixture
def web():
    return FakeWeb()


def run(config, web, **kwargs):
    return Worker(config, fetcher=web.fetcher()).run(**kwargs)


def ingest(lib, *urls_titles):
    items = [RawItem(url=u, title=t, source_ref="Test iPhone", locator=str(i)) for i, (u, t) in enumerate(urls_titles)]
    lib.ingest("cloudtabs", items)
    return {b["url_original"]: b["id"] for b in lib.list(limit=100, view="all")}


# ------------------------------------------------------------------ fetcher


def test_redirects_are_followed_and_capped(web):
    web.redirect("https://a.test/1", "https://a.test/2")
    web.redirect("https://a.test/2", "https://a.test/1")
    f = web.fetcher()
    with pytest.raises(FetchError, match="loop"):
        f.get("https://a.test/1")
    for i in range(12):
        web.redirect(f"https://b.test/{i}", f"https://b.test/{i + 1}")
    with pytest.raises(FetchError, match="more than 10"):
        f.get("https://b.test/0")


def test_redirect_to_a_private_address_is_blocked(web):
    web.redirect("https://evil.test/", "http://router.test/admin")
    with pytest.raises(FetchError) as exc:
        web.fetcher().get("https://evil.test/")
    assert exc.value.kind == "blocked"
    assert "http://router.test/admin" not in web.hits  # never requested


def test_meta_refresh_and_lnkd_interstitial(web):
    web.page("https://t.co/abc", '<meta http-equiv="refresh" content="0;URL=https://example.com/post">')
    web.page(
        "https://lnkd.in/xyz",
        '<p>This link will take you to a page that’s not on LinkedIn</p>'
        '<a data-tracking-control-name="external_url_click" href="https://example.com/post">example.com/post</a>',
    )
    web.page("https://example.com/post", ARTICLE)
    f = web.fetcher()
    assert f.get("https://t.co/abc").url == "https://example.com/post"
    assert f.get("https://lnkd.in/xyz").url == "https://example.com/post"


@pytest.mark.parametrize(
    ("status", "body", "kind"),
    [
        (404, "gone", "failed"),
        (403, "forbidden", "needs_browser"),
        (999, "", "needs_browser"),
        (503, "<title>Just a moment...</title><div id='cf-chl-widget'>", "needs_browser"),
        (503, "maintenance", "retry"),
        (200, "<html><title>Robot Check</title><form action='/errors/validateCaptcha'>", "needs_browser"),
    ],
)
def test_responses_are_classified(web, status, body, kind):
    web.page("https://shop.test/item", body, status=status)
    page = web.fetcher().get("https://shop.test/item")
    with pytest.raises(FetchError) as exc:
        fetch.classify(page)
    assert exc.value.kind == kind


def test_rate_limit_carries_retry_after(web):
    web.page("https://busy.test/", "slow down", status=429, headers={"retry-after": "120"})
    with pytest.raises(FetchError) as exc:
        fetch.classify(web.fetcher().get("https://busy.test/"))
    assert exc.value.kind == "retry" and exc.value.retry_after == 120


def test_unwrap_known_wrappers():
    assert fetch.unwrap("https://www.google.com/url?q=https://example.com/a&sa=D") == "https://example.com/a"
    assert fetch.unwrap("https://l.facebook.com/l.php?u=https%3A%2F%2Fexample.com%2Fb&h=x") == "https://example.com/b"
    assert fetch.unwrap("https://www.linkedin.com/authwall?trk=x&sessionRedirect=https%3A%2F%2Fwww.linkedin.com%2Fposts%2Fabc") == (
        "https://www.linkedin.com/posts/abc"
    )
    assert fetch.unwrap("https://example.com/url?q=https://other.com/") == "https://example.com/url?q=https://other.com/"


# ------------------------------------------------------------------ extraction


def test_html_metadata():
    meta = extract.from_html(ARTICLE, "https://example.com/rust-ownership?utm_source=x")
    assert meta.title == "Rust ownership explained"
    assert meta.description == "A gentle guide to borrowing."
    assert meta.site_name == "Example Blog" and meta.author == "A. Writer"
    assert meta.image_url == "https://example.com/img/cover.png"
    assert meta.canonical_hint == "https://example.com/rust-ownership"
    assert "garbage collector" in meta.content_text
    assert len(meta.content_simhash) == 16


def test_generic_titles_are_ignored():
    meta = extract.from_html("<title>Just a moment...</title><body>hi</body>", "https://x.test/")
    assert meta.title is None
    assert fetch.is_generic_title("example.com", "https://www.example.com/a")


def test_canonical_hint_only_from_the_same_site():
    html = '<link rel="canonical" href="https://other.com/page"><title>T</title>'
    assert extract.from_html(html, "https://example.com/page").canonical_hint is None


def test_frame_policy():
    assert extract.frame_allowed(httpx.Headers({}))
    assert not extract.frame_allowed(httpx.Headers({"x-frame-options": "SAMEORIGIN"}))
    assert not extract.frame_allowed(httpx.Headers({"content-security-policy": "default-src 'self'; frame-ancestors 'self'"}))
    assert extract.frame_allowed(httpx.Headers({"content-security-policy": "frame-ancestors *"}))


def test_x_post_via_oembed():
    data = {
        "author_name": "Some Dev",
        "html": '<blockquote class="twitter-tweet"><p lang="en">Shipping a new parser today &amp; it is fast.</p>'
        '&mdash; Some Dev (@somedev) <a href="https://twitter.com/somedev/status/1">October 1, 2026</a></blockquote>',
        "provider_name": "Twitter",
    }
    meta = extract.from_oembed(data, "https://x.com/somedev/status/1")
    assert meta.title == "Some Dev on X: “Shipping a new parser today & it is fast.”"
    assert meta.published_at == "October 1, 2026" and meta.title_source == "oembed"


def test_simhash_is_stable_and_close_for_similar_text():
    a = extract.simhash("the quick brown fox jumps over the lazy dog " * 20)
    b = extract.simhash("the quick brown fox jumps over the lazy dog " * 20 + "today")
    assert a == extract.simhash("the quick brown fox jumps over the lazy dog " * 20)
    assert bin(int(a, 16) ^ int(b, 16)).count("1") <= 6


# ------------------------------------------------------------------ worker + library


def test_enriches_new_bookmarks_and_respects_locked_title(lib, config, web):
    web.page("https://example.com/rust-ownership", ARTICLE, headers={"x-frame-options": "DENY"})
    web.page("https://example.com/other", ARTICLE.replace("Rust ownership explained", "Other page"))
    ids = ingest(lib, ("https://example.com/rust-ownership", ""), ("https://example.com/other", "Other"))
    lib.update(ids["https://example.com/other"], title="My own title")

    stats = run(config, web)
    assert stats["fetched"] == 2
    a = lib.get(ids["https://example.com/rust-ownership"])
    assert a["title"] == "Rust ownership explained" and a["title_source"] == "html"
    assert a["fetch_status"] == "fetched" and a["frame_ok"] == 0
    assert a["description"] == "A gentle guide to borrowing." and "garbage collector" in a["content_text"]
    assert a["job"]["state"] == "done"
    assert lib.get(ids["https://example.com/other"])["title"] == "My own title"
    assert lib.search("garbage collector")  # content text is searchable

    assert run(config, web) == {}  # nothing left to do
    assert web.hits["https://example.com/rust-ownership"] == 1


def test_short_link_resolves_and_merges_with_existing_page(lib, config, web):
    """PLAN03 §15.3: the lnkd.in tab shows its real destination; if that page exists already, they merge."""
    web.page(
        "https://lnkd.in/abc",
        '<a data-tracking-control-name="external_url_click" href="https://example.com/rust-ownership?utm_source=linkedin">x</a>',
    )
    web.page("https://example.com/rust-ownership?utm_source=linkedin", ARTICLE)
    web.page("https://example.com/rust-ownership", ARTICLE)
    ids = ingest(lib, ("https://example.com/rust-ownership", "Rust"), ("https://lnkd.in/abc", "LinkedIn"))
    survivor_id = ids["https://example.com/rust-ownership"]
    lib.update(survivor_id, remarks="read later")
    lib.update(ids["https://lnkd.in/abc"], remarks="from a LinkedIn post")

    run(config, web)
    survivor = lib.get(survivor_id)
    short = lib.get(ids["https://lnkd.in/abc"])
    assert short["merged_into_id"] == survivor_id
    assert len(survivor["sources"]) == 2
    assert survivor["remarks"] == "read later\n\nfrom a LinkedIn post"
    assert {a["url_canonical"]: a["kind"] for a in survivor["aliases"]}["https://lnkd.in/abc"] == "merged"
    assert lib.count("active") == 1

    # Seeing the short link again still lands on the survivor.
    out = lib.ingest("cloudtabs", [RawItem(url="https://lnkd.in/abc", source_ref="Test MacBook", locator="9")])
    assert out.counts["matched_exact"] == 1
    assert lib.get(survivor_id)["sources"][-1]["source_ref"] == "Test MacBook"


def test_short_link_to_a_new_page_adopts_the_destination(lib, config, web):
    web.page("https://lnkd.in/new", '<a data-tracking-control-name="external_url_click" href="https://example.com/fresh">x</a>')
    web.page("https://example.com/fresh", ARTICLE)
    ids = ingest(lib, ("https://lnkd.in/new", "LinkedIn"))
    run(config, web)
    b = lib.get(ids["https://lnkd.in/new"])
    assert b["url_canonical"] == "https://example.com/fresh" and b["domain"] == "example.com"
    assert {a["url_canonical"]: a["kind"] for a in b["aliases"]} == {"https://lnkd.in/new": "short", "https://example.com/fresh": "canonical"}
    assert b["title"] == "Rust ownership explained"  # "LinkedIn" says nothing, so the fetched title replaces it


def test_redirect_to_private_address_fails_without_retrying(lib, config, web):
    web.redirect("https://evil.test/x", "http://router.test/")
    ids = ingest(lib, ("https://evil.test/x", "Evil"))
    run(config, web)
    b = lib.get(ids["https://evil.test/x"])
    assert b["fetch_status"] == "failed" and "non-public" in b["fetch_error"]
    assert b["job"]["state"] == "dead"
    assert lib.count("failed") == 1


def test_blocked_site_goes_to_needs_browser_once(lib, config, web):
    web.page("https://www.amazon.com/dp/B1", "<title>Robot Check</title><form action='/errors/validateCaptcha'>")
    ids = ingest(lib, ("https://www.amazon.com/dp/B1", "Kindle"))
    run(config, web)
    run(config, web)
    b = lib.get(ids["https://www.amazon.com/dp/B1"])
    assert b["fetch_status"] == "needs_browser" and b["title"] == "Kindle"
    assert web.hits["https://www.amazon.com/dp/B1"] == 1  # no retry storm


def test_transient_errors_back_off_then_give_up(lib, config, web):
    web.page("https://flaky.test/a", "down", status=503)
    ids = ingest(lib, ("https://flaky.test/a", "Flaky"))
    bid = ids["https://flaky.test/a"]
    run(config, web)
    job = lib.get(bid)["job"]
    assert job["state"] == "retry_wait" and job["attempts"] == 1
    assert lib.get(bid)["fetch_status"] == "pending" and lib.count("unprocessed") == 1
    run(config, web)
    assert web.hits["https://flaky.test/a"] == 1  # not due yet
    for _ in range(len(BACKOFF)):
        lib.conn.execute("UPDATE processing_jobs SET next_run_at='2000-01-01T00:00:00Z'")
        run(config, web)
    b = lib.get(bid)
    assert b["fetch_status"] == "failed" and b["job"]["state"] == "dead" and b["job"]["attempts"] == len(BACKOFF)


def test_google_searches_are_not_fetched(lib, config, web):
    ids = ingest(lib, ("https://www.google.com/search?q=rust+ownership", "rust ownership - Google Search"))
    run(config, web)
    assert lib.get(ids["https://www.google.com/search?q=rust+ownership"])["fetch_status"] == "skipped"
    assert web.hits == {}


def test_refetch_and_retry_failed(lib, config, web):
    web.page("https://example.com/p", "gone", status=404)
    ids = ingest(lib, ("https://example.com/p", "P"))
    run(config, web)
    assert lib.get(ids["https://example.com/p"])["fetch_status"] == "failed"
    web.page("https://example.com/p", ARTICLE)
    assert lib.retry_failed() == 1
    run(config, web)
    assert lib.get(ids["https://example.com/p"])["fetch_status"] == "fetched"


def test_deleted_bookmarks_are_not_fetched(lib, config, web):
    web.page("https://example.com/d", ARTICLE)
    ids = ingest(lib, ("https://example.com/d", "D"))
    lib.delete(ids["https://example.com/d"])
    run(config, web)
    assert web.hits == {}


def test_x_posts_use_oembed(lib, config, web):
    oembed = "https://publish.twitter.com/oembed?omit_script=1&dnt=true&url=https%3A%2F%2Fx.com%2Fdev%2Fstatus%2F42"
    web.json(oembed, {"author_name": "Dev", "html": "<blockquote><p>Hello world from a post</p>&mdash; Dev <a href='#'>Oct 1</a></blockquote>"})
    ids = ingest(lib, ("https://x.com/dev/status/42?s=20", "Dev on X"))
    run(config, web)
    b = lib.get(ids["https://x.com/dev/status/42?s=20"])
    assert b["title"] == "Dev on X" and b["description"] == "Hello world from a post"
    assert list(web.hits) == [oembed]


def test_events_record_what_changed(lib, config, web):
    web.page("https://example.com/rust-ownership", ARTICLE)
    ids = ingest(lib, ("https://example.com/rust-ownership", None))
    run(config, web)
    event = lib.get(ids["https://example.com/rust-ownership"])["events"][-1]
    assert event["kind"] == "enrich" and event["actor"] == "enrich"
    assert {"title", "description", "content_text"} <= set(json.loads(event["data_json"])["changed"])


def test_api_refetch_and_lists(lib, config, web, monkeypatch):
    from fastapi.testclient import TestClient

    from bmk.api import TOKEN_HEADER, create_app
    from bmk.enrich import worker as worker_mod

    monkeypatch.setattr(worker_mod, "Fetcher", web.fetcher)
    web.page("https://example.com/a", "gone", status=404)
    ids = ingest(lib, ("https://example.com/a", "A"), ("https://example.com/b", "B"))
    client = TestClient(create_app(config))
    headers = {TOKEN_HEADER: config.token_path.read_text().strip()}
    assert client.get("/api/meta").json()["counts"]["unprocessed"] == 2

    result = client.post(f"/api/bookmarks/{ids['https://example.com/a']}/refetch", headers=headers).json()
    assert result["fetch_status"] == "failed" and "404" in result["fetch_error"]
    assert web.hits == {"https://example.com/a": 1}  # only the one asked for
    meta = client.get("/api/meta").json()
    assert meta["counts"]["failed"] == 1 and meta["counts"]["unprocessed"] == 1 and meta["enrich"] is True
    assert [b["id"] for b in client.get("/api/bookmarks", params={"view": "failed"}).json()["items"]] == [ids["https://example.com/a"]]

    web.page("https://example.com/a", ARTICLE)
    result = client.post(f"/api/bookmarks/{ids['https://example.com/a']}/refetch", headers=headers).json()
    assert result["fetch_status"] == "fetched" and result["description"] == "A gentle guide to borrowing."
    assert client.post("/api/bookmarks/bulk", json={"ids": [ids["https://example.com/b"]], "action": "refetch"}, headers=headers).status_code == 200


def test_fetched_title_only_replaces_missing_or_generic_titles(lib, config, web):
    web.page("https://forms.test/a", "<title>Microsoft Forms</title>")
    web.page("https://example.com/rust-ownership", ARTICLE)
    web.page("https://example.com/b", ARTICLE)
    ids = ingest(
        lib,
        ("https://forms.test/a", "Event Sign-Up"),
        ("https://example.com/rust-ownership", "https://example.com/rust-ownership"),
        ("https://example.com/b", "Just a moment..."),
    )
    run(config, web)
    assert lib.get(ids["https://forms.test/a"])["title"] == "Event Sign-Up"
    assert lib.get(ids["https://example.com/b"])["title"] == "Rust ownership explained"
    assert lib.get(ids["https://example.com/rust-ownership"])["title"] == "Rust ownership explained"
