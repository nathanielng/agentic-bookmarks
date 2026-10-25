from bmk.ingest.links import Link, bare_urls, clean_title, links_from_html, looks_like_url, quality_problems, trim_url, with_scheme


def test_trim_url_keeps_balanced_parentheses():
    assert trim_url("https://example.com/a.") == "https://example.com/a"
    assert trim_url("https://example.com/a),") == "https://example.com/a"
    assert trim_url("https://en.example.org/wiki/A_(b)") == "https://en.example.org/wiki/A_(b)"
    assert trim_url("https://en.example.org/wiki/A_(b)).") == "https://en.example.org/wiki/A_(b)"


def test_bare_urls_in_prose():
    text = "See https://example.com/x?y=1, then www.example.org/page. Not mail@example.net or <https://example.net/z>"
    assert bare_urls(text) == ["https://example.com/x?y=1", "https://www.example.org/page", "https://example.net/z"]


def test_with_scheme_and_url_likeness():
    assert with_scheme("example.com/path") == "https://example.com/path"
    assert with_scheme(" www.example.com ") == "https://www.example.com"
    assert with_scheme("not a url") == "not a url"
    assert looks_like_url("http://example.com") and looks_like_url("example.co.uk/x")
    assert not looks_like_url("Example title") and not looks_like_url("")


def test_links_from_html_in_order_with_text_titles():
    html = """<html><head><title>https://ignored.example</title><style>a{}</style></head><body>
    <p><a href="https://example.com/1">First &amp; <b>bold</b></a> then https://example.com/2.</p>
    <a href="#top">anchor</a><a href="about:blank">blank</a></body></html>"""
    links = links_from_html(html)
    assert [(link.url, link.title) for link in links] == [
        ("https://example.com/1", "First & bold"),
        ("https://example.com/2", None),
        ("about:blank", "blank"),  # dropped later as an unsupported scheme, and recorded as skipped
    ]


def test_clean_title():
    assert clean_title("  A\u0007 title\n with   gaps ") == "A title with gaps"
    assert clean_title("café") == "café"
    assert clean_title("   ") is None


def test_quality_checks():
    assert quality_problems([Link("https://example.com", "Don’t “quote” 日本")]) == []
    problems = quality_problems([Link("", None), Link("https://example.com", "Don\\'92t"), Link("https://example.com", "a\x01b")])
    assert problems == ["#1: no URL", "#2: leftover RTF escape in title", "#3: control character in title"]
    assert quality_problems([Link("https://example.com", "bad \udc80")]) == ["#1: title is not valid UTF-8"]
