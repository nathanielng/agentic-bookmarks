import pytest

from bmk.urls import InvalidURL, UnsupportedScheme, canonicalize, domain_of, loose_key, title_is_url


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("https://x.com/a/status/1?s=20", "https://twitter.com/a/status/1"),
        ("https://x.com/a/status/1?s=20&t=abc", "https://mobile.twitter.com/a/status/1?s=46"),
        ("https://example.com/p?utm_source=a", "https://example.com/p?utm_source=b&utm_medium=email"),
        ("https://example.com/p?b=2&a=1", "https://example.com/p?a=1&b=2"),
        ("HTTPS://WWW.EXAMPLE.COM:443/p", "https://example.com/p"),
        ("https://example.com//a//b", "https://example.com/a/b"),
        ("https://example.com/%7euser", "https://example.com/~user"),
        ("https://example.com/p?", "https://example.com/p"),
        ("https://example.com", "https://example.com/"),
        ("https://youtu.be/abc", "https://www.youtube.com/watch?v=abc&si=xyz"),
        ("https://m.youtube.com/watch?v=abc", "https://youtube.com/watch?v=abc&feature=share"),
        ("https://en.m.wikipedia.org/wiki/Foo", "https://en.wikipedia.org/wiki/Foo"),
        ("https://www.linkedin.com/posts/x?trk=public_post", "https://linkedin.com/posts/x?trackingId=abc"),
        ("https://example.com/p#:~:text=hello", "https://example.com/p"),
        ("https://example.com/p?fbclid=1&gclid=2", "https://example.com/p"),
    ],
)
def test_equivalent(a, b):
    assert canonicalize(a) == canonicalize(b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("https://youtube.com/watch?v=A", "https://youtube.com/watch?v=B"),
        ("https://example.com/p?id=1", "https://example.com/p?id=2"),
        ("https://example.com/p?unknown=1", "https://example.com/p"),  # unknown params are kept
        ("https://example.com/p", "https://example.com/p/"),  # trailing slash is tier 3, not tier 1
        ("http://example.com/p", "https://example.com/p"),  # scheme is tier 3, not tier 1
        ("https://example.com/page#section", "https://example.com/page"),  # fragments kept by default
        ("https://app.example.com/#/a", "https://app.example.com/#/b"),  # SPA routes
        ("https://example.com/P", "https://example.com/p"),  # paths are case-sensitive
    ],
)
def test_distinct(a, b):
    assert canonicalize(a) != canonicalize(b)


def test_fragment_dropped_on_listed_domain():
    assert canonicalize("https://x.com/a/status/1#m") == "https://x.com/a/status/1"


def test_spa_fragment_kept_even_on_drop_domain():
    assert canonicalize("https://x.com/#/route") == "https://x.com/#/route"


def test_idn_host_punycoded():
    assert canonicalize("https://bücher.example/") == "https://xn--bcher-kva.example/"


def test_non_default_port_kept():
    assert canonicalize("http://example.com:8080/") == "http://example.com:8080/"


@pytest.mark.parametrize("url", ["about:blank", "file:///etc/passwd", "javascript:alert(1)", "favorites://", "mailto:a@b.c"])
def test_unsupported_schemes(url):
    with pytest.raises(UnsupportedScheme):
        canonicalize(url)


@pytest.mark.parametrize("url", ["", "   ", "example.com/no-scheme", "https://", "https://exa mple.com/", "http://example.com:99999/"])
def test_invalid(url):
    with pytest.raises(InvalidURL):
        canonicalize(url)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("http://example.com/p/", "https://www.example.com/p?ref=nl"),
        ("https://example.com/post/amp", "https://example.com/post"),
        ("https://example.com/dir/index.html", "https://example.com/dir/"),
    ],
)
def test_loose_key_matches_variants(a, b):
    assert loose_key(canonicalize(a)) == loose_key(canonicalize(b))


def test_loose_key_keeps_significant_params():
    assert loose_key(canonicalize("https://youtube.com/watch?v=A")) != loose_key(canonicalize("https://youtube.com/watch?v=B"))
    assert loose_key(canonicalize("https://news.ycombinator.com/item?id=1")) != loose_key(
        canonicalize("https://news.ycombinator.com/item?id=2")
    )


CNA = "https://www.channelnewsasia.com/business/amazon-cloud-capacity-6488181"


@pytest.mark.parametrize(
    "title",
    [
        "www.channelnewsasia.com/business/amazon-cloud-capacity-6488181",  # as Safari shows an unloaded tab
        CNA,
        "channelnewsasia.com/business/amazon-cloud-capacity-6488181/",
        "WWW.ChannelNewsAsia.com/business/amazon-cloud-capacity-6488181",
    ],
)
def test_title_is_url(title):
    assert title_is_url(title, CNA)
    assert title_is_url("example.com/p", "https://example.com/p?utm_source=x#top")


@pytest.mark.parametrize("title", [None, "", "Amazon cloud capacity sold out", "channelnewsasia.com", "Kindle"])
def test_title_is_not_url(title):
    assert not title_is_url(title, CNA)


def test_domain_of():
    assert domain_of("https://forums.news.cnn.co.uk/x") == "cnn.co.uk"
    assert domain_of("https://lnkd.in/p/x") == "lnkd.in"
    assert domain_of("http://127.0.0.1:8000/") == "127.0.0.1"


def test_title_is_url_ignores_invisible_marks():
    assert title_is_url("\u200egithub.com/example/repo", "https://github.com/example/repo")
