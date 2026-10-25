"""Markdown files (PLAN03 §5.4): `[title](url)` links, `<https://…>` autolinks, `<a href>` in inline HTML
and bare URLs, from markdown-it tokens. Code spans and fenced/indented code are skipped unless
`include_code`. The locator is the line number.

A line that starts with an ISO timestamp, as the iOS Share Sheet Shortcut writes them, gives its links an
exact save time:  `- 2026-10-25T13:30+08:00 [Title](https://…)`
"""

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from ..models import RawItem
from .links import bare_urls, clean_title, links_from_html

_STAMP = re.compile(r"^\s*(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)(?=\s|$)")


def parse_stamp(text: str, tz: str) -> datetime | None:
    """A leading ISO timestamp; one without an offset is local time in `tz`."""
    m = _STAMP.match(text)
    if not m:
        return None
    try:
        dt = datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=ZoneInfo(tz))


class _Collector:
    def __init__(self, tz: str):
        self.tz = tz
        self.items: list[RawItem] = []
        self.lines: list[int] = []
        self.stamps: dict[int, datetime] = {}

    def add(self, url: str, title: str | None, line: int) -> None:
        self.items.append(RawItem(url=url, title=clean_title(title), locator=f"line {line}"))
        self.lines.append(line)

    def inline(self, token, include_code: bool) -> None:
        line = token.map[0] + 1
        at_line_start = True
        href, text = None, []
        for child in token.children or []:
            kind = child.type
            if kind in ("softbreak", "hardbreak"):
                line += 1
                at_line_start = True
                continue
            if href is not None:  # inside a link: collect its text
                if kind == "link_close" or (kind == "html_inline" and child.content.lower().startswith("</a")):
                    self.add(href, "".join(text), link_line)
                    href = None
                elif kind in ("text", "code_inline"):
                    text.append(child.content)
                elif kind == "image":
                    text.append(child.content)
                continue
            if kind == "link_open":
                href, text, link_line = child.attrs.get("href", ""), [], line
            elif kind == "html_inline" and (found := links_from_html(child.content + "</a>")):
                href, text, link_line = found[0].url, [], line
            elif kind == "text":
                if at_line_start and (stamp := parse_stamp(child.content, self.tz)):
                    self.stamps[line] = stamp
                for url in bare_urls(child.content):
                    self.add(url, None, line)
            elif kind == "code_inline" and include_code:
                for url in bare_urls(child.content):
                    self.add(url, None, line)
            at_line_start = False

    def block(self, token, include_code: bool) -> None:
        if token.type in ("fence", "code_block"):
            if not include_code:
                return
            first = token.map[0] + (2 if token.type == "fence" else 1)  # a fence's content starts after the ``` line
            for offset, text in enumerate(token.content.splitlines()):
                for url in bare_urls(text):
                    self.add(url, None, first + offset)
        elif token.type == "html_block":
            for offset, text in enumerate(token.content.splitlines()):
                for link in links_from_html(text):
                    self.add(link.url, link.title, token.map[0] + 1 + offset)


def parse_markdown(data: bytes, *, include_code: bool = False, tz: str = "UTC") -> tuple[list[RawItem], list[str]]:
    from markdown_it import MarkdownIt

    text = data.decode("utf-8-sig", errors="replace")
    collector = _Collector(tz)
    for token in MarkdownIt("commonmark").parse(text):
        if token.type == "inline":
            collector.inline(token, include_code)
        else:
            collector.block(token, include_code)
    for item, line in zip(collector.items, collector.lines):
        if stamp := collector.stamps.get(line):
            item.event_at, item.event_at_kind = stamp, "exact"
    return collector.items, []
