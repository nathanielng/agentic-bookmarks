"""RTF and HTML files, and the clipboard (PLAN03 §5.3).

RTF goes through macOS `textutil` to HTML, which handles code-page escapes, `\\uN` and nested groups;
then `<a href>` links (link text as title) and bare URLs are taken in document order. The locator is the
link's ordinal position, `#1`, `#2`, ….
"""

import subprocess

from ..models import RawItem
from .links import Link, bare_urls, clean_title, links_from_html, quality_problems


class FileFormatError(ValueError):
    """The file can't be read as the format it claims to be; reported for that file only."""


def rtf_to_html(data: bytes) -> str:
    try:
        proc = subprocess.run(
            ["textutil", "-stdin", "-format", "rtf", "-convert", "html", "-stdout"],
            input=data,
            capture_output=True,
            timeout=60,
        )
    except FileNotFoundError as exc:
        raise FileFormatError("textutil not found (RTF import needs macOS)") from exc
    except subprocess.TimeoutExpired as exc:
        raise FileFormatError("textutil timed out") from exc
    # textutil exits 0 even when it can't read the input; the only signal is stderr and empty output.
    out = proc.stdout.decode("utf-8", errors="replace")
    if proc.returncode != 0 or "<html" not in out.lower():
        detail = proc.stderr.decode("utf-8", errors="replace").strip() or "no output"
        raise FileFormatError(f"not a readable RTF file ({detail.replace('stdin', 'input')})")
    return out


def items_from_links(links: list[Link]) -> tuple[list[RawItem], list[str]]:
    items = [RawItem(url=link.url, title=clean_title(link.title), locator=f"#{n}") for n, link in enumerate(links, 1)]
    return items, quality_problems(links)


def parse_rtf(data: bytes) -> tuple[list[RawItem], list[str]]:
    if not data.lstrip().startswith(b"{\\rtf"):
        raise FileFormatError("not an RTF file (no {\\rtf header)")
    return items_from_links(links_from_html(rtf_to_html(data)))


def parse_html(data: bytes) -> tuple[list[RawItem], list[str]]:
    return items_from_links(links_from_html(data.decode("utf-8", errors="replace")))


# ---------------------------------------------------------------------------------------------- clipboard


def read_clipboard() -> tuple[str, bytes]:
    """The richest flavour on the general pasteboard: ('html'|'rtf'|'text', data). Needs the macos extra."""
    try:
        from AppKit import NSPasteboard, NSPasteboardTypeHTML, NSPasteboardTypeRTF, NSPasteboardTypeString
    except ImportError as exc:
        raise FileFormatError("clipboard import needs pyobjc: uv pip install 'bmk[macos]'") from exc
    board = NSPasteboard.generalPasteboard()
    for kind, flavour in (("html", NSPasteboardTypeHTML), ("rtf", NSPasteboardTypeRTF)):
        data = board.dataForType_(flavour)
        if data is not None and data.length():
            return kind, bytes(data)
    text = board.stringForType_(NSPasteboardTypeString)
    return "text", (text or "").encode("utf-8")


def parse_clipboard(kind: str, data: bytes) -> tuple[list[RawItem], list[str]]:
    if kind == "rtf":
        return parse_rtf(data)
    if kind == "html":
        return parse_html(data)
    return items_from_links([Link(u) for u in bare_urls(data.decode("utf-8", errors="replace"))])
