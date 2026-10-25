"""Metadata from fetched pages (PLAN05 §6.4).

Order of preference: oEmbed (YouTube, X, Vimeo, Spotify), then OpenGraph/Twitter tags, then trafilatura's
reading of the page, then `<title>`. Generic titles ("Just a moment...", "LinkedIn") are never used.
"""

import hashlib
import json
import logging
import re
from dataclasses import dataclass, fields
from html import unescape
from urllib.parse import quote, urljoin, urlsplit

import httpx
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from .. import urls
from .fetch import is_generic_title

logging.getLogger("trafilatura").setLevel(logging.ERROR)
logging.getLogger("htmldate").setLevel(logging.ERROR)

MAX_TEXT = 100_000
MAX_FIELD = 2_000


@dataclass
class Metadata:
    title: str | None = None
    title_source: str | None = None  # 'oembed' | 'html'
    description: str | None = None
    site_name: str | None = None
    author: str | None = None
    published_at: str | None = None
    image_url: str | None = None
    content_text: str | None = None
    canonical_hint: str | None = None
    content_simhash: str | None = None

    def filled(self) -> dict[str, str]:
        return {f.name: getattr(self, f.name) for f in fields(self) if getattr(self, f.name)}


def _clean(value: str | None, limit: int = MAX_FIELD) -> str | None:
    if not value:
        return None
    text = re.sub(r"\s+", " ", unescape(value)).strip()
    return text[:limit] or None


def _http_url(base: str, value: str | None) -> str | None:
    if not value:
        return None
    absolute = urljoin(base, value.strip())
    return absolute if absolute.startswith(("http://", "https://")) else None


# ------------------------------------------------------------------ oEmbed


def oembed_endpoint(url: str) -> str | None:
    """The oEmbed URL for pages whose HTML is useless without JavaScript (or richer via oEmbed)."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    path = parts.path or "/"
    q = quote(url, safe="")
    if host in ("youtube.com", "youtu.be") and (path.startswith(("/watch", "/shorts/", "/live/")) or host == "youtu.be"):
        return f"https://www.youtube.com/oembed?format=json&url={q}"
    if host in ("x.com", "twitter.com") and re.match(r"^/[^/]+/status/\d+", path):
        return f"https://publish.twitter.com/oembed?omit_script=1&dnt=true&url={q}"
    if host == "vimeo.com" and re.match(r"^/\d+", path):
        return f"https://vimeo.com/api/oembed.json?url={q}"
    if host == "open.spotify.com":
        return f"https://open.spotify.com/oembed?url={q}"
    return None


def from_oembed(data: dict, url: str) -> Metadata:
    meta = Metadata(
        site_name=_clean(data.get("provider_name")),
        author=_clean(data.get("author_name")),
        image_url=_http_url(url, data.get("thumbnail_url")),
    )
    host = (urlsplit(url).hostname or "").lower()
    if host.endswith(("x.com", "twitter.com")) and data.get("html"):
        # The post text is the first <p> of the embed blockquote; the last link's text is the date.
        tree = HTMLParser(data["html"])
        p = tree.css_first("blockquote p")
        text = _clean(p.text(separator=" ")) if p else None
        links = tree.css("blockquote > a")
        if text:
            meta.description = text
            meta.content_text = text
            short = text if len(text) <= 120 else text[:117].rstrip() + "…"
            meta.title = f"{meta.author or 'Post'} on X: “{short}”"
        if links:
            meta.published_at = _clean(links[-1].text())
        meta.site_name = "X"
    else:
        meta.title = _clean(data.get("title"), 500)
    if meta.title:
        meta.title_source = "oembed"
    return meta


def parse_oembed(body: bytes) -> dict | None:
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


# ------------------------------------------------------------------ HTML


def from_html(html: str, url: str) -> Metadata:
    tree = HTMLParser(html)

    def meta(*names: str) -> str | None:
        for name in names:
            for attr in ("property", "name", "itemprop"):
                node = tree.css_first(f'meta[{attr}="{name}"]')
                if node is not None and (content := _clean(node.attributes.get("content"))):
                    return content
        return None

    title_node = tree.css_first("title")
    html_title = _clean(title_node.text(), 500) if title_node is not None else None
    og_title = meta("og:title", "twitter:title")
    title = next((t for t in (og_title, html_title) if t and not is_generic_title(t, url)), None)

    canonical = None
    link = tree.css_first('link[rel="canonical"]')
    if link is not None and (href := _http_url(url, link.attributes.get("href"))):
        # Only trusted from the same site: a canonical pointing elsewhere is not evidence of a duplicate.
        if urls.registrable_domain(urlsplit(href).hostname or "") == urls.registrable_domain(urlsplit(url).hostname or ""):
            canonical = href

    author = meta("author", "article:author", "twitter:creator")
    if author and author.startswith(("http://", "https://")):
        author = None

    result = Metadata(
        title=title,
        title_source="html" if title else None,
        description=meta("og:description", "description", "twitter:description"),
        site_name=meta("og:site_name", "application-name"),
        author=author,
        published_at=meta("article:published_time", "og:published_time", "datePublished", "date"),
        image_url=_http_url(url, meta("og:image", "og:image:url", "twitter:image", "twitter:image:src")),
        canonical_hint=canonical,
    )

    doc = _trafilatura(html, url)
    if doc is not None:
        result.content_text = (doc.get("text") or "")[:MAX_TEXT] or None
        result.description = result.description or _clean(doc.get("description"))
        result.author = result.author or _clean(doc.get("author"), 300)
        result.published_at = result.published_at or _clean(doc.get("date"), 40)
        result.site_name = result.site_name or _clean(doc.get("sitename"), 300)
        if not result.title and (t := _clean(doc.get("title"), 500)) and not is_generic_title(t, url):
            result.title, result.title_source = t, "html"
    result.content_simhash = simhash(result.content_text)
    return result


def _trafilatura(html: str, url: str) -> dict | None:
    import trafilatura  # slow to import; only needed by the worker

    try:
        doc = trafilatura.bare_extraction(html, url=url, with_metadata=True, include_comments=False, favor_precision=True)
    except Exception:  # trafilatura can fail on odd markup; metadata from tags is still useful
        logging.getLogger(__name__).debug("trafilatura failed on %s", url, exc_info=True)
        return None
    if doc is None:
        return None
    return doc if isinstance(doc, dict) else doc.as_dict()


# ------------------------------------------------------------------ headers and hashing


def frame_allowed(headers: httpx.Headers) -> bool:
    """False if the site forbids being shown inside another page (X-Frame-Options or CSP frame-ancestors)."""
    xfo = headers.get("x-frame-options", "").lower()
    if "deny" in xfo or "sameorigin" in xfo:
        return False
    for policy in headers.get_list("content-security-policy"):
        for directive in policy.split(";"):
            name, _, value = directive.strip().partition(" ")
            if name.lower() == "frame-ancestors":
                sources = value.split()
                return "*" in sources
    return True


def simhash(text: str | None) -> str | None:
    """64-bit simhash over word 3-grams (near-duplicate detection, CG5)."""
    words = re.findall(r"\w+", (text or "").lower())
    if len(words) < 3:
        return None
    weights = [0] * 64
    for i in range(len(words) - 2):
        h = int.from_bytes(hashlib.blake2b(" ".join(words[i : i + 3]).encode(), digest_size=8).digest(), "big")
        for bit in range(64):
            weights[bit] += 1 if h >> bit & 1 else -1
    return f"{sum(1 << bit for bit in range(64) if weights[bit] > 0):016x}"
