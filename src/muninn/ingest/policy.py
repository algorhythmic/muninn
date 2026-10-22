"""Domain policy over stored rows: visibility, X locators, hidden titles, masks.

Shared by the X writer (:mod:`muninn.ingest.x_store`) and the
browser-bookmark ingest. Stdlib only (no ``httpx``), so ``muninn.ingest``
stays light.

- :func:`policy_hides` is ``DomainPolicy.matches`` plus X's own aliasing:
  ``x.com`` and ``twitter.com`` (and their ``www.``/``mobile.``/``m.``
  forms) are one site whose handle paths are case-insensitive, so a rule
  for ``x.com/someone/*`` also hides ``twitter.com/SomeOne/status/…``.
- An X post's locator is always the canonical
  ``https://x.com/<handle>/status/<id>`` (``/i/web/status/<id>`` while the
  handle is unknown), whatever URL form first referenced it.
- A hidden post's title is ``@handle`` only; a hidden article has none.
- A hidden child's locator is masked as ``[hidden link]`` in its parent's
  stored text, by patterns built from the child row (:func:`mask_text`),
  so masking is the same whichever writer applies it.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

from muninn.intake_records import SOURCE_X_ARTICLE, SOURCE_X_LINK, SOURCE_X_POST, X_SOURCES
from muninn.sanitize import sanitize_url
from muninn.scrape.domain_policy import DomainPolicy

HIDDEN_LINK = "[hidden link]"
# Extractor recorded on x_api revisions: note_tweet text preferred over
# text, t.co links replaced by their sanitized locators, then muninn-text/1.
X_TEXT_EXTRACTOR = ("muninn-x-post-text", "1")
X_ARTICLE_EXTRACTOR = ("muninn-x-article-text", "1")

SOURCE_BOOKMARKS_HTML = "bookmarks_html"
ALL_SOURCES: tuple[str, ...] = (SOURCE_BOOKMARKS_HTML, *X_SOURCES)

X_HOSTS = frozenset({
    "x.com", "www.x.com", "mobile.x.com", "m.x.com",
    "twitter.com", "www.twitter.com", "mobile.twitter.com", "m.twitter.com",
})
_X_HOST = r"(?:www\.|mobile\.|m\.)?(?:x|twitter)\.com"
_HANDLE = r"[A-Za-z0-9_]{1,15}"
_HANDLE_RE = re.compile(rf"^{_HANDLE}$")
# A masked locator ends where only trailing punctuation, then whitespace or
# the end of the text, follows: never inside a longer URL.
_END = r"(?=[.,;:!?)\]}'\"]*(?:\s|$))"
_GITHUB_ROOT = re.compile(r"^https://github\.com/([^/]+)/([^/]+)$")


# ── Visibility ──────────────────────────────────────────────────────────

def policy_hides(policy: DomainPolicy, url: str | None) -> bool:
    """True when domain policy hides ``url`` (→ ``content_visible = 0``)."""
    if not url:
        return False
    if policy.matches(url):
        return True
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if (parts.hostname or "").lower() not in X_HOSTS:
        return False
    for alias in ("x.com", "twitter.com"):
        for path in {parts.path, parts.path.lower()}:
            if policy.matches(urlunsplit(("https", alias, path, "", ""))):
                return True
    return False


def visible_flag(policy: DomainPolicy, url: str | None) -> int:
    return 0 if policy_hides(policy, url) else 1


def domain_of(url: str | None) -> str | None:
    if not url:
        return None
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    return host.lower() if host else None


# ── X post locators ─────────────────────────────────────────────────────

def post_locator(post_id: str, handle: str | None) -> str | None:
    raw = (
        f"https://x.com/{handle}/status/{post_id}" if handle
        else f"https://x.com/i/web/status/{post_id}"
    )
    return sanitize_url(raw).sanitized_url


def handle_from_locator(url: str | None) -> str | None:
    """The handle in an X ``/<handle>/status/<id>`` (or ``/article/``) URL."""
    if not url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if (parts.hostname or "").lower() not in X_HOSTS:
        return None
    segs = [s for s in parts.path.split("/") if s]
    if (
        len(segs) >= 3 and segs[1] in ("status", "article")
        and segs[0].lower() != "i" and _HANDLE_RE.match(segs[0])
    ):
        return segs[0]
    return None


def meta_handle(meta: dict[str, Any]) -> str | None:
    author = meta.get("author")
    if isinstance(author, dict):
        name = author.get("username")
        if isinstance(name, str) and name:
            return name
    return None


def canonical_post_locator(post_id: str, meta: dict[str, Any], url: str | None) -> str | None:
    """The API's handle wins; else the handle of the URL that referenced the post."""
    return post_locator(post_id, meta_handle(meta) or handle_from_locator(url))


# ── Titles ──────────────────────────────────────────────────────────────

def title_from_text(text: str | None, limit: int = 80) -> str | None:
    if not text:
        return None
    first = " ".join(text.split())
    if not first:
        return None
    return first if len(first) <= limit else first[: limit - 1].rstrip() + "…"


def post_title_for(handle: str | None, text: str | None) -> str | None:
    """``@handle: <first ~80 characters>`` so handles are keyword-searchable."""
    snippet = title_from_text(text)
    if handle:
        return f"@{handle}: {snippet}" if snippet else f"@{handle}"
    return snippet


def hidden_post_title(handle: str | None) -> str | None:
    """A hidden post keeps only its author's handle as a title."""
    return f"@{handle}" if handle else None


# ── Masking hidden children in parent text ──────────────────────────────

def child_mask_patterns(
    source: str, source_id: str, url: str | None, meta: dict[str, Any] | None = None
) -> list[re.Pattern[str]]:
    """Patterns matching every locator form a parent's text can quote for
    this (hidden) child row."""
    meta = meta or {}
    pats: list[re.Pattern[str]] = []
    if source == SOURCE_X_POST and source_id.isdigit():
        pats.append(re.compile(
            rf"https?://{_X_HOST}/(?:i/web|{_HANDLE})/status/{source_id}(?![0-9])(?:[/?#]\S*?)?{_END}",
            re.IGNORECASE,
        ))
    elif source == SOURCE_X_ARTICLE and source_id.isdigit():
        pats.append(re.compile(
            rf"https?://{_X_HOST}/(?:i|{_HANDLE})/article/{source_id}(?![0-9])(?:[/?#]\S*?)?{_END}",
            re.IGNORECASE,
        ))
    if url:
        root = _GITHUB_ROOT.match(url)
        if source == SOURCE_X_LINK and root and meta.get("kind") == "github_repo":
            # The text quotes the link as written: any case, `.git`, a trailing slash.
            owner, name = (re.escape(p) for p in root.groups())
            pats.append(re.compile(
                rf"https?://(?:www\.)?github\.com/{owner}/{name}(?:\.git)?/?{_END}", re.IGNORECASE
            ))
        pats.append(re.compile(re.escape(url) + _END))
    return pats


def mask_text(text: str, patterns: Iterable[re.Pattern[str]]) -> str:
    for pattern in patterns:
        text = pattern.sub(HIDDEN_LINK, text)
    return text


def _load_meta(raw: str | None) -> dict[str, Any]:
    try:
        meta = json.loads(raw) if raw else {}
    except ValueError:
        meta = {}
    return meta if isinstance(meta, dict) else {}


def hidden_child_patterns(conn: sqlite3.Connection, parent_id: int) -> list[re.Pattern[str]]:
    """Mask patterns for every hidden item the row links to."""
    rows = conn.execute(
        "SELECT DISTINCT c.source, c.source_id, c.url, c.source_metadata FROM item_links l "
        "JOIN bookmarks c ON c.bookmark_id = l.child_bookmark_id "
        "WHERE l.parent_bookmark_id = ? AND c.content_visible = 0",
        (parent_id,),
    ).fetchall()
    pats: list[re.Pattern[str]] = []
    for row in rows:
        pats += child_mask_patterns(row[0], row[1], row[2], _load_meta(row[3]))
    return pats


__all__ = [
    "ALL_SOURCES",
    "HIDDEN_LINK",
    "X_ARTICLE_EXTRACTOR",
    "X_HOSTS",
    "X_TEXT_EXTRACTOR",
    "canonical_post_locator",
    "child_mask_patterns",
    "domain_of",
    "handle_from_locator",
    "hidden_child_patterns",
    "hidden_post_title",
    "mask_text",
    "meta_handle",
    "policy_hides",
    "post_locator",
    "post_title_for",
    "title_from_text",
    "visible_flag",
]
