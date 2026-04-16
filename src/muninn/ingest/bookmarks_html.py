"""Netscape Bookmark File Format parser.

Yields one `ParsedBookmark` per `<A HREF=…>` entry, with the surrounding
folder hierarchy reconstructed from the `<DL>`/`<DT>`/`<H3>` tree.

We use ``html.parser`` from the stdlib rather than a heavy DOM library —
the format is shallow and Chrome's exporter is consistent. The folder
stack is updated on `<H3>` and popped when the matching `<DL>` closes
(Netscape's nesting rule: each `<H3>` is followed by a `<DL>` containing
its children; closing the `</DL>` pops the folder).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Iterable


@dataclass
class ParsedBookmark:
    raw_url: str
    title: str | None
    add_date: int | None              # epoch seconds, from ADD_DATE attr
    last_modified: int | None         # epoch seconds, from LAST_MODIFIED attr
    folder_path: list[str] = field(default_factory=list)
    icon_uri: str | None = None
    tags: str | None = None           # raw TAGS attr; comma-separated string

    @property
    def era_label(self) -> str | None:
        """Top-level folder name. None for root-level bookmarks."""
        return self.folder_path[0] if self.folder_path else None


class _NetscapeHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.bookmarks: list[ParsedBookmark] = []

        # Folder stack and per-frame "did we open a DL after this H3?" flag.
        # Netscape format: <DT><H3>Folder</H3><DL>…children…</DL>
        # We push the folder name on H3 close; on the next DL open we mark
        # the frame as "owned" so closing that DL pops the folder.
        self._folder_stack: list[str] = []
        # Stack of bools, one per <DL> currently open: True iff that DL
        # corresponds to a folder we pushed.
        self._dl_owns_folder: list[bool] = []

        self._pending_h3_owns_next_dl = False

        self._in_a = False
        self._a_attrs: dict[str, str] = {}
        self._a_text_parts: list[str] = []

        self._in_h3 = False
        self._h3_text_parts: list[str] = []

    # ── tags ─────────────────────────────────────────────────────────
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if t == "a":
            self._in_a = True
            self._a_attrs = {k.lower(): (v or "") for k, v in attrs}
            self._a_text_parts = []
        elif t == "h3":
            self._in_h3 = True
            self._h3_text_parts = []
        elif t == "dl":
            # If the most recent H3 was a folder, this DL is its container.
            if self._pending_h3_owns_next_dl:
                self._dl_owns_folder.append(True)
                self._pending_h3_owns_next_dl = False
            else:
                self._dl_owns_folder.append(False)

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t == "a" and self._in_a:
            self._in_a = False
            href = self._a_attrs.get("href", "")
            if href:
                self.bookmarks.append(self._build_bookmark(href))
            self._a_attrs = {}
            self._a_text_parts = []
        elif t == "h3" and self._in_h3:
            self._in_h3 = False
            name = "".join(self._h3_text_parts).strip()
            self._folder_stack.append(name)
            self._pending_h3_owns_next_dl = True
            self._h3_text_parts = []
        elif t == "dl":
            if self._dl_owns_folder:
                owned = self._dl_owns_folder.pop()
                if owned and self._folder_stack:
                    self._folder_stack.pop()

    def handle_data(self, data: str) -> None:
        if self._in_a:
            self._a_text_parts.append(data)
        elif self._in_h3:
            self._h3_text_parts.append(data)

    # ── helpers ──────────────────────────────────────────────────────
    def _build_bookmark(self, href: str) -> ParsedBookmark:
        title = "".join(self._a_text_parts).strip() or None
        add_date = _parse_epoch(self._a_attrs.get("add_date"))
        last_modified = _parse_epoch(self._a_attrs.get("last_modified"))
        icon_uri = self._a_attrs.get("icon_uri") or self._a_attrs.get("icon") or None
        tags = self._a_attrs.get("tags") or None
        return ParsedBookmark(
            raw_url=href,
            title=title,
            add_date=add_date,
            last_modified=last_modified,
            folder_path=list(self._folder_stack),
            icon_uri=icon_uri,
            tags=tags,
        )


def _parse_epoch(raw: str | None) -> int | None:
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def parse_bookmarks_html(html_content: str) -> list[ParsedBookmark]:
    """Parse Netscape Bookmark HTML, returning one `ParsedBookmark` per `<A>`."""
    parser = _NetscapeHTMLParser()
    parser.feed(html_content)
    parser.close()
    return parser.bookmarks


def iter_bookmarks_html(html_content: str) -> Iterable[ParsedBookmark]:
    """Same as :func:`parse_bookmarks_html` but exposes the iterator shape
    for callers that prefer streaming. The parser still buffers, so this is
    a convenience wrapper for now."""
    yield from parse_bookmarks_html(html_content)
