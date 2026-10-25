"""URL validation and conservative canonicalisation (PLAN05 §6.3).

`canonicalize()` produces the tier-1 dedupe key: it only removes differences known to be noise.
`loose_key()` produces the tier-3 key: aggressive, used only to *propose* merges (CG5).
"""

import re
from dataclasses import dataclass
from functools import cache
from importlib import resources
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

import tldextract
import yaml

ALLOWED_SCHEMES = {"http", "https"}
DEFAULT_PORTS = {"http": 80, "https": 443}
_UNRESERVED = re.compile(r"%([0-9A-Fa-f]{2})")
_UNRESERVED_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_QUERY_SAFE = "-._~!$'()*,;:@/?"


class InvalidURL(ValueError):
    """URL cannot be parsed."""


class UnsupportedScheme(ValueError):
    """URL parses but its scheme is not http/https (about:, file:, javascript:, …)."""


@dataclass(frozen=True)
class Rules:
    tracking_params: frozenset[str]
    tracking_prefixes: tuple[str, ...]
    domain_tracking_params: dict[str, frozenset[str]]
    host_aliases: dict[str, str]
    mobile_subdomain_domains: frozenset[str]
    keep_www_domains: frozenset[str]
    drop_fragment_domains: frozenset[str]
    significant_params_default: frozenset[str]
    domain_significant_params: dict[str, frozenset[str]]
    shorteners: frozenset[str]

    @classmethod
    def from_dict(cls, data: dict) -> "Rules":
        tracking = data.get("tracking_params", [])
        return cls(
            tracking_params=frozenset(p for p in tracking if not p.endswith("*")),
            tracking_prefixes=tuple(p[:-1] for p in tracking if p.endswith("*")),
            domain_tracking_params={k: frozenset(v) for k, v in data.get("domain_tracking_params", {}).items()},
            host_aliases=dict(data.get("host_aliases", {})),
            mobile_subdomain_domains=frozenset(data.get("mobile_subdomain_domains", [])),
            keep_www_domains=frozenset(data.get("keep_www_domains", [])),
            drop_fragment_domains=frozenset(data.get("drop_fragment_domains", [])),
            significant_params_default=frozenset(data.get("significant_params_default", [])),
            domain_significant_params={k: frozenset(v) for k, v in data.get("domain_significant_params", {}).items()},
            shorteners=frozenset(data.get("shorteners", [])),
        )


@cache
def default_rules() -> Rules:
    text = resources.files("bmk.rules").joinpath("canonicalize.yaml").read_text(encoding="utf-8")
    return Rules.from_dict(yaml.safe_load(text) or {})


@cache
def _extractor() -> tldextract.TLDExtract:
    # Bundled public-suffix snapshot only: no network, no cache directory.
    return tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)


def registrable_domain(host: str) -> str:
    """e.g. 'forums.news.cnn.co.uk' -> 'cnn.co.uk'. Falls back to the host for IPs/localhost."""
    if not host:
        return ""
    result = _extractor()(host)
    return result.top_domain_under_public_suffix or host


def domain_of(url: str) -> str:
    return registrable_domain((urlsplit(url).hostname or "").rstrip("."))


def validate(url: str) -> str:
    """Return the stripped URL or raise InvalidURL / UnsupportedScheme."""
    if url is None:
        raise InvalidURL("empty URL")
    url = url.strip()
    if not url:
        raise InvalidURL("empty URL")
    if any(ch.isspace() for ch in url):
        raise InvalidURL("URL contains whitespace")
    try:
        parts = urlsplit(url)
        _ = parts.port  # raises ValueError on a bad port
    except ValueError as exc:
        raise InvalidURL(str(exc)) from exc
    if not parts.scheme:
        raise InvalidURL("missing scheme")
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise UnsupportedScheme(f"unsupported scheme '{parts.scheme.lower()}'")
    if not parts.hostname:
        raise InvalidURL("missing host")
    return url


def _normalise_percent(text: str) -> str:
    """Decode percent-escapes of unreserved characters; uppercase the rest."""

    def fix(match: re.Match) -> str:
        char = chr(int(match.group(1), 16))
        return char if char in _UNRESERVED_CHARS else f"%{match.group(1).upper()}"

    return _UNRESERVED.sub(fix, text)


def _normalise_host(host: str, rules: Rules) -> str:
    host = host.lower().rstrip(".")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        pass
    host = rules.host_aliases.get(host, host)
    reg = registrable_domain(host)
    if reg in rules.mobile_subdomain_domains:
        labels = host.split(".")
        if len(labels) > 2 and "m" in labels[:-2]:
            labels.remove("m")
            host = ".".join(labels)
    if host.startswith("www.") and reg not in rules.keep_www_domains:
        host = host[4:]
    return rules.host_aliases.get(host, host)


def _is_tracking(name: str, reg_domain: str, rules: Rules) -> bool:
    lowered = name.lower()
    if name in rules.tracking_params or lowered in rules.tracking_params:
        return True
    if any(lowered.startswith(prefix) for prefix in rules.tracking_prefixes):
        return True
    return name in rules.domain_tracking_params.get(reg_domain, frozenset())


def _clean_fragment(fragment: str, reg_domain: str, rules: Rules) -> str:
    if ":~:" in fragment:  # text fragments are never part of identity
        fragment = fragment.split(":~:", 1)[0]
    if not fragment:
        return ""
    if fragment.startswith(("/", "!/")):  # SPA hash routes identify content
        return fragment
    if reg_domain in rules.drop_fragment_domains:
        return ""
    return fragment


def canonicalize(url: str, rules: Rules | None = None) -> str:
    """Tier-1 canonical form. Raises InvalidURL / UnsupportedScheme."""
    rules = rules or default_rules()
    parts = urlsplit(validate(url))
    scheme = parts.scheme.lower()
    host = _normalise_host(parts.hostname or "", rules)
    path = parts.path
    query_pairs = parse_qsl(parts.query, keep_blank_values=True)

    # youtu.be/<id> -> youtube.com/watch?v=<id>
    if host == "youtu.be" and len(path.strip("/")) > 0:
        query_pairs = [("v", path.strip("/").split("/")[0]), *query_pairs]
        host, path = "youtube.com", "/watch"

    reg = registrable_domain(host)
    port = parts.port
    netloc = host
    if parts.username:
        userinfo = parts.username + (f":{parts.password}" if parts.password else "")
        netloc = f"{userinfo}@{netloc}"
    if port and port != DEFAULT_PORTS.get(scheme):
        netloc = f"{netloc}:{port}"

    path = _normalise_percent(path)
    path = re.sub(r"/{2,}", "/", path) or "/"

    kept = [(k, v) for k, v in query_pairs if not _is_tracking(k, reg, rules)]
    kept.sort()
    query = urlencode(kept, quote_via=quote, safe=_QUERY_SAFE)

    fragment = _normalise_percent(_clean_fragment(parts.fragment, reg, rules))
    return urlunsplit((scheme, netloc, path, query, fragment))


def loose_key(canonical_url: str, rules: Rules | None = None) -> str:
    """Tier-3 key: scheme-less, no trailing slash/index page/AMP, only content-identifying params."""
    rules = rules or default_rules()
    parts = urlsplit(canonical_url)
    host = (parts.hostname or "").lower()
    if host.startswith("amp."):
        host = host[4:]
    if host.startswith("www."):
        host = host[4:]
    reg = registrable_domain(host)

    path = parts.path
    path = re.sub(r"/(index\.(html?|php))$", "/", path)
    path = re.sub(r"/amp/?$", "/", path)
    path = path.rstrip("/")

    significant = rules.domain_significant_params.get(host) or rules.domain_significant_params.get(reg) or rules.significant_params_default
    pairs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k in significant]
    query = urlencode(sorted(pairs), quote_via=quote, safe=_QUERY_SAFE)
    fragment = parts.fragment if parts.fragment.startswith(("/", "!/")) else ""

    key = host + path
    if query:
        key += "?" + query
    if fragment:
        key += "#" + fragment
    return key


def is_shortener(url: str, rules: Rules | None = None) -> bool:
    rules = rules or default_rules()
    host = (urlsplit(url).hostname or "").lower()
    return host in rules.shorteners


_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u200e\u200f\u2066\u2067\u2068\u2069\ufeff"))


def title_is_url(title: str | None, url: str) -> bool:
    """True if a "title" is just the URL itself, as Safari reports for tabs that never finished loading.

    Matches the full URL or the URL without its scheme, `www.`, query or fragment, ignoring case and a
    trailing slash.
    """
    if not title:
        return False
    # Safari sometimes wraps the URL in invisible direction marks (U+200E) or zero-width characters.
    norm = lambda s: unquote(s.translate(_INVISIBLE).strip()).lower().rstrip("/")  # noqa: E731
    t = norm(title)
    full = norm(url)
    bare = full.split("://", 1)[-1]
    forms = {full, bare, bare.removeprefix("www.")}
    forms |= {f.split("#", 1)[0].split("?", 1)[0].rstrip("/") for f in list(forms)}
    return t in forms or t.removeprefix("www.") in forms


def fallback_title(url: str) -> str:
    """Domain + path, used for display when nothing better exists."""
    parts = urlsplit(url)
    return unquote((parts.hostname or "") + (parts.path if parts.path != "/" else ""))
