"""Fetching pages for enrichment (PLAN05 §6.1–6.2).

Every request goes through the SSRF guard, carries no cookies or credentials, follows at most 10 redirects
(HTTP, meta refresh, simple JS `location=` stubs and LinkedIn's lnkd.in interstitial), and stops at 5 MB or
20 s. Problems are raised as FetchError with a `kind` that tells the worker what to do next:

  retry          transient (timeout, connection error, 5xx, 429): try again later with backoff
  needs_browser  bot check, login wall, 401/403/999: only a real browser could get it; never retried blindly
  failed         permanent (404/410, redirect loop, not a web page)
  blocked        the URL or a redirect points at a non-public address
"""

import email.utils
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache
from html import unescape
from http.cookiejar import CookieJar, DefaultCookiePolicy
from importlib import resources
from urllib.parse import parse_qs, urljoin, urlsplit

import httpcore
import httpx
import yaml

from .. import __version__
from . import netguard

MAX_HOPS = 10
MAX_BYTES = 5 * 1024 * 1024
ERROR_BODY_BYTES = 256 * 1024
TOTAL_TIMEOUT = 20.0
TIMEOUT = httpx.Timeout(10.0, connect=5.0)
REDIRECT_CODES = {301, 302, 303, 307, 308}
HTML_TYPES = {"text/html", "application/xhtml+xml"}
# Browser-compatible (many sites serve an empty shell to unknown agents) but identifies itself as bmk.
USER_AGENT = f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15 bmk/{__version__}"


class FetchError(Exception):
    def __init__(self, kind: str, message: str, *, status: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.retry_after = retry_after


@dataclass
class Page:
    url: str  # after all redirects
    status: int
    headers: httpx.Headers
    body: bytes
    hops: list[tuple[int, str]] = field(default_factory=list)  # (HTTP status, or 0 for an HTML redirect; next URL)

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip().lower()

    @property
    def is_html(self) -> bool:
        return self.content_type in HTML_TYPES or (not self.content_type and self.body.lstrip()[:1] == b"<")

    @property
    def text(self) -> str:
        return decode(self.body, self.headers.get("content-type", ""))


# ------------------------------------------------------------------ rules


@dataclass(frozen=True)
class EnrichRules:
    redirect_wrappers: tuple[dict, ...]
    fetch_skip: tuple[dict, ...]
    challenge_markers: tuple[str, ...]
    login_paths: tuple[str, ...]
    generic_titles: frozenset[str]


@cache
def rules() -> EnrichRules:
    data = yaml.safe_load(resources.files("bmk.rules").joinpath("enrich.yaml").read_text(encoding="utf-8")) or {}
    return EnrichRules(
        redirect_wrappers=tuple(data.get("redirect_wrappers", [])),
        fetch_skip=tuple(data.get("fetch_skip", [])),
        challenge_markers=tuple(m.lower() for m in data.get("challenge_markers", [])),
        login_paths=tuple(data.get("login_paths", [])),
        generic_titles=frozenset(t.lower() for t in data.get("generic_titles", [])),
    )


def _matches(rule: dict, url: str) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    domain = rule["domain"].lower()
    return (host == domain or host.endswith("." + domain)) and (parts.path or "/").startswith(rule.get("path", "/"))


def unwrap(url: str) -> str:
    """Strip known redirect wrappers (google.com/url?q=…, l.facebook.com/l.php?u=…) without a network call."""
    for _ in range(5):  # wrappers can be nested
        for rule in rules().redirect_wrappers:
            if _matches(rule, url):
                query = parse_qs(urlsplit(url).query)
                target = next((query[p][0] for p in rule["params"] if query.get(p)), None)
                if target and target.startswith(("http://", "https://")):
                    url = target
                    break
        else:
            return url
    return url


def should_skip(url: str) -> bool:
    return any(_matches(rule, url) for rule in rules().fetch_skip)


def is_login_url(url: str) -> bool:
    path = (urlsplit(url).path or "/").lower()
    return any(path == p or path.startswith(p + "/") for p in rules().login_paths)


def is_generic_title(title: str | None, url: str = "") -> bool:
    if not title or not title.strip():
        return True
    t = title.strip().lower()
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    return t in rules().generic_titles or (bool(host) and t in (host, "www." + host))


# ------------------------------------------------------------------ decoding and HTML redirects

_META_CHARSET = re.compile(rb"""<meta[^>]+charset=["']?([\w-]+)""", re.I)
_META_REFRESH = re.compile(
    r"""<meta[^>]+http-equiv=["']?refresh["']?[^>]*content=["']?\s*(\d+)\s*[;,]\s*url\s*=\s*['"]?([^"'>\s]+)""", re.I
)
_META_REFRESH_REV = re.compile(
    r"""<meta[^>]+content=["']?\s*(\d+)\s*[;,]\s*url\s*=\s*['"]?([^"'>\s]+)[^>]*http-equiv=["']?refresh""", re.I
)
_JS_LOCATION = re.compile(r"""(?:window\.|document\.)?location(?:\.href)?\s*(?:=|\.replace\(|\.assign\()\s*["']([^"']+)["']""")
_LNKD_EXTERNAL = re.compile(r"""<a[^>]+data-tracking-control-name=["']external_url_click["'][^>]*href=["']([^"']+)""", re.I)
_ANCHOR = re.compile(r"""<a[^>]+href=["'](https?://[^"']+)""", re.I)


def decode(body: bytes, content_type: str = "") -> str:
    charset = None
    if "charset=" in content_type.lower():
        charset = content_type.lower().split("charset=", 1)[1].split(";")[0].strip().strip('"')
    if not charset and (m := _META_CHARSET.search(body[:4096])):
        charset = m.group(1).decode("ascii", "ignore")
    try:
        return body.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def html_redirect(url: str, html: str) -> str | None:
    """The next URL when a 200 page is only a redirect stub (shortener interstitial, meta refresh, JS)."""
    host = (urlsplit(url).hostname or "").lower()
    if host == "lnkd.in":
        # LinkedIn's "This link will take you to a page that's not on LinkedIn" interstitial.
        m = _LNKD_EXTERNAL.search(html)
        if m:
            return urljoin(url, unescape(m.group(1)))
        for href in _ANCHOR.findall(html):
            target = (urlsplit(unescape(href)).hostname or "").lower()
            if target and not target.endswith(("linkedin.com", "lnkd.in", "licdn.com")):
                return unescape(href)
        return None
    head = html[:20000]
    m = _META_REFRESH.search(head) or _META_REFRESH_REV.search(head)
    if m and int(m.group(1)) <= 5:  # long refreshes are auto-reloads, not redirects
        return urljoin(url, unescape(m.group(2)))
    if len(html) < 8000:  # only tiny pages are redirect stubs; real pages mention location= in scripts
        m = _JS_LOCATION.search(html)
        if m:
            return urljoin(url, unescape(m.group(1)))
    return None


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def looks_like_challenge(status: int, body: bytes) -> bool:
    if status == 200 and len(body) > 60000:
        return False
    text = body[:65536].decode("utf-8", errors="ignore").lower()
    return any(marker in text for marker in rules().challenge_markers)


def classify(page: Page) -> None:
    """Raise FetchError unless the page is a usable response."""
    status = page.status
    if status == 200:
        if page.hops and is_login_url(page.url):
            raise FetchError("needs_browser", f"redirected to a login page ({page.url})", status=status)
        if page.is_html and looks_like_challenge(status, page.body):
            raise FetchError("needs_browser", "the site answered with a bot check", status=status)
        return
    if status == 202 or (200 < status < 300 and not page.body):
        # AWS WAF and similar answer 202 with a script-only challenge instead of the page.
        raise FetchError("needs_browser", f"HTTP {status}: the site answered with a bot check", status=status)
    if 200 < status < 300:
        return
    if status in (404, 410):
        raise FetchError("failed", f"HTTP {status}: the page no longer exists", status=status)
    if status == 429:
        raise FetchError("retry", "HTTP 429: rate limited", status=status, retry_after=parse_retry_after(page.headers.get("retry-after")))
    if status in (401, 403, 451, 999) or looks_like_challenge(status, page.body):
        raise FetchError("needs_browser", f"HTTP {status}: blocked or login required", status=status)
    if status >= 500:
        raise FetchError("retry", f"HTTP {status}: server error", status=status, retry_after=parse_retry_after(page.headers.get("retry-after")))
    raise FetchError("failed", f"HTTP {status}", status=status)


# ------------------------------------------------------------------ client


class Fetcher:
    def __init__(self, resolver: netguard.Resolver = netguard.system_resolver, transport: httpx.BaseTransport | None = None):
        self.resolver = resolver
        if transport is None:
            transport = httpx.HTTPTransport()
            # httpx has no option for a custom network backend; swap in a pool that connects only to vetted IPs.
            transport._pool = httpcore.ConnectionPool(  # noqa: SLF001
                ssl_context=transport._pool._ssl_context,  # noqa: SLF001
                max_connections=20,
                network_backend=netguard.GuardedBackend(resolver),
            )
        self.client = httpx.Client(
            transport=transport,
            follow_redirects=False,
            timeout=TIMEOUT,
            cookies=CookieJar(policy=DefaultCookiePolicy(allowed_domains=[])),  # never store or send cookies
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en;q=0.9, *;q=0.5"},
        )

    def close(self) -> None:
        self.client.close()

    def get(self, url: str, *, accept: str = "html") -> Page:
        """GET `url`, following redirects (each hop re-checked by the SSRF guard)."""
        current, hops, seen = url, [], set()
        for _ in range(MAX_HOPS + 1):
            if current in seen:
                raise FetchError("failed", f"redirect loop at {current}")
            seen.add(current)
            try:
                netguard.check_url(current, self.resolver)
            except netguard.BlockedAddress as exc:
                raise FetchError("blocked", str(exc)) from exc
            except httpcore.ConnectError as exc:  # DNS lookup failed
                raise FetchError("retry", str(exc)) from exc
            status, headers, body = self._request(current, accept)
            if status in REDIRECT_CODES:
                location = headers.get("location")
                if not location:
                    raise FetchError("failed", f"HTTP {status} without a Location header", status=status)
                current = urljoin(current, location.strip())
                hops.append((status, current))
                continue
            page = Page(current, status, headers, body, hops)
            if status == 200 and accept == "html" and page.is_html:
                nxt = html_redirect(current, page.text)
                if nxt and nxt != current:
                    current = nxt
                    hops.append((0, current))
                    continue
            return page
        raise FetchError("failed", f"more than {MAX_HOPS} redirects")

    def _request(self, url: str, accept: str) -> tuple[int, httpx.Headers, bytes]:
        accept_header = "application/json" if accept == "json" else "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5"
        deadline = time.monotonic() + TOTAL_TIMEOUT
        try:
            with self.client.stream("GET", url, headers={"Accept": accept_header}) as r:
                if r.status_code in REDIRECT_CODES:
                    return r.status_code, r.headers, b""
                ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                wanted = ctype in HTML_TYPES or not ctype or (accept == "json" and "json" in ctype)
                if r.status_code == 200 and not wanted:
                    return r.status_code, r.headers, b""  # a PDF, image, …: resolved, but nothing to read
                limit = MAX_BYTES if r.status_code == 200 else ERROR_BODY_BYTES
                body = bytearray()
                for chunk in r.iter_bytes():
                    body.extend(chunk)
                    if len(body) >= limit:
                        break
                    if time.monotonic() > deadline:
                        raise FetchError("retry", f"timed out after {TOTAL_TIMEOUT:.0f} s")
                return r.status_code, r.headers, bytes(body[:limit])
        except netguard.BlockedAddress as exc:
            raise FetchError("blocked", str(exc)) from exc
        except httpx.TimeoutException as exc:
            raise FetchError("retry", f"timed out ({type(exc).__name__})") from exc
        except httpx.TransportError as exc:
            raise FetchError("retry", f"connection failed: {exc}") from exc
