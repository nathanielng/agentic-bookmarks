"""Link extraction shared by the file and clipboard ingesters: `<a href>` from HTML, bare URLs from text,
and the quality checks ported from the original RTF script."""

import re
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser

# A bare URL in prose: http(s)://… or www.…, up to whitespace or a character that can't end a URL.
BARE_URL = re.compile(r"""(?i)\b(?:https?://|www\.)[^\s<>"'`\]\[{}|\\^]+""")
_TRAILING = ".,;:!?)'\"”’»…"
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_RTF_ESCAPE = re.compile(r"\\'[0-9a-fA-F]{2}|\\u-?\d+|\\(?:par|pard|fldrslt|fldinst|field|f\d+|fs\d+|cf\d+)\b|HYPERLINK \"")
_DOMAIN_ONLY = re.compile(r"(?i)^(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}(?:[/?#:]\S*)?$")


@dataclass
class Link:
    url: str
    title: str | None = None
    line: int | None = None  # set by line-oriented formats


def trim_url(url: str) -> str:
    """Strip punctuation that ends a sentence rather than the URL; keep a ')' that closes a '(' inside it."""
    while url and url[-1] in _TRAILING:
        if url[-1] == ")" and url.count("(") >= url.count(")"):
            break
        url = url[:-1]
    return url


def with_scheme(text: str) -> str:
    """`www.example.com/x` or `example.com/x` → https://…; anything else is returned stripped."""
    text = text.strip()
    if "://" not in text and _DOMAIN_ONLY.match(text):
        return "https://" + text
    return text


def looks_like_url(text: str) -> bool:
    text = text.strip()
    return bool(text) and (text.lower().startswith(("http://", "https://")) or bool(_DOMAIN_ONLY.match(text)))


def bare_urls(text: str) -> list[str]:
    return [with_scheme(u) for m in BARE_URL.finditer(text) if (u := trim_url(m.group(0)))]


def clean_title(text: str | None) -> str | None:
    """Collapse whitespace, drop control characters and normalise to NFC."""
    if text is None:
        return None
    text = unicodedata.normalize("NFC", _CONTROL.sub("", text))
    return " ".join(text.split()) or None


class _LinkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[Link] = []
        self._href: str | None = None
        self._text: list[str] = []
        self._skip = 0  # inside <style>/<script>/<head>

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script", "head", "title"):
            self._skip += 1
        elif tag == "a" and self._href is None:
            href = dict(attrs).get("href")
            if href and not href.startswith("#"):
                self._href, self._text = href.strip(), []

    def handle_endtag(self, tag):
        if tag in ("style", "script", "head", "title"):
            self._skip = max(0, self._skip - 1)
        elif tag == "a" and self._href is not None:
            self.links.append(Link(self._href, clean_title("".join(self._text))))
            self._href = None

    def handle_data(self, data):
        if self._skip:
            return
        if self._href is not None:
            self._text.append(data)
        else:
            self.links.extend(Link(u) for u in bare_urls(data))


def links_from_html(html: str) -> list[Link]:
    """Every `<a href>` (link text as title) and every bare URL in the text, in document order."""
    parser = _LinkParser()
    parser.feed(html)
    parser.close()
    return parser.links


def quality_problems(links: list[Link]) -> list[str]:
    """The original script's checks: complete rows, no leftover RTF escapes, no control characters,
    and a clean UTF-8 round trip. Returns one message per problem (empty when all pass)."""
    problems = []
    for n, link in enumerate(links, 1):
        if not link.url:
            problems.append(f"#{n}: no URL")
        for name, value in (("url", link.url), ("title", link.title)):
            if not value:
                continue
            if _RTF_ESCAPE.search(value):
                problems.append(f"#{n}: leftover RTF escape in {name}")
            if _CONTROL.search(value):
                problems.append(f"#{n}: control character in {name}")
            try:
                value.encode("utf-8").decode("utf-8")
            except UnicodeError:
                problems.append(f"#{n}: {name} is not valid UTF-8")
    return problems
