"""Domain policy over stored rows: visibility, X locators, hidden titles, masks.

Shared by the X writer (:mod:`muninn.ingest.x_store`) and the
browser-bookmark ingest. Stdlib only (no ``httpx``), so ``muninn.ingest``
stays light.

- :func:`policy_hides` is ``DomainPolicy.matches`` plus X's own aliasing:
  ``x.com`` and ``twitter.com`` (and their ``www.``/``mobile.``/``m.``
  forms) are one site whose handle paths are case-insensitive, so a rule
  for ``x.com/someone/*`` also hides ``twitter.com/SomeOne/status/…``.
- **A row is hidden when its own locator is hidden, or when a fetch of it
  ended on a hidden locator** (:func:`row_hidden`): a redirect's final URL
  in ``scrape_results.final_url`` (any pass), or the ``final_locator`` of
  any of its non-withdrawn ``scrape_history`` revisions, with a Wayback
  snapshot URL read as the original it archives. Such a fetch's content
  is the hidden site's, so the row is treated exactly as if its own URL
  were hidden. Only rows ``muninn scrape`` fetches count; an X post's or
  article's stored locator is its own.
- An X post's locator is always the canonical
  ``https://x.com/<handle>/status/<id>`` (``/i/web/status/<id>`` while the
  handle is unknown), whatever URL form first referenced it.
- A hidden post's title is ``@handle`` only; a hidden article has none.
- A hidden child's locator is masked as ``[hidden link]`` in its parent's
  stored text, by patterns built from the child row (:func:`mask_text`),
  so masking is the same whichever writer applies it.
- :func:`apply_policy` (``muninn policy apply``, and the start of every
  ``muninn ingest x``) recomputes all of that from stored rows, with no
  API call. Un-hiding a row restores its visibility (and a post's title,
  from its stored text), but text already masked as ``[hidden link]``
  comes back only with a re-sync (``muninn ingest x --full``).
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

from muninn.consumers.packets.canonical import x_post_item_id
from muninn.intake_records import (
    API_CONTENT_SOURCES,
    SOURCE_X_ARTICLE,
    SOURCE_X_LINK,
    SOURCE_X_POST,
    X_SOURCES,
)
from muninn.revisions import record_revision
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


# A Wayback snapshot URL (``/web/<timestamp><modifier>/<original>``).
_WAYBACK_RE = re.compile(
    r"^https?://(?:web\.)?archive\.org/web/[0-9]{1,14}(?:[a-z]{2}_)?/(.+)$", re.IGNORECASE
)


def archived_original(url: str | None) -> str | None:
    """The original URL a Wayback snapshot URL archives, else None."""
    m = _WAYBACK_RE.match(url or "")
    if m is None:
        return None
    original = m.group(1)
    if not re.match(r"^https?://", original, re.IGNORECASE):
        original = "http://" + original.removeprefix("//")
    return sanitize_url(original).sanitized_url


def locator_hidden(policy: DomainPolicy, url: str | None) -> bool:
    """:func:`policy_hides`, also for the original inside a Wayback URL."""
    return policy_hides(policy, url) or policy_hides(policy, archived_original(url))


def fetched_locators(
    conn: sqlite3.Connection, bookmark_ids: Iterable[int] | None = None
) -> dict[int, set[str]]:
    """``{bookmark_id: every locator a fetch of the row ended on}``: the
    ``final_url`` of each ``scrape_results`` pass and the ``final_locator``
    of each non-withdrawn ``scrape_history`` revision (all rows, or these)."""
    ids = None if bookmark_ids is None else sorted({int(b) for b in bookmark_ids})
    out: dict[int, set[str]] = {}
    queries = [
        "SELECT bookmark_id, final_url FROM scrape_results WHERE final_url IS NOT NULL",
    ]
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'scrape_history'"
    ).fetchone() is not None:
        queries.append(
            "SELECT bookmark_id, final_locator FROM scrape_history "
            "WHERE final_locator IS NOT NULL AND withdrawn_at IS NULL"
        )
    for sql in queries:
        if ids is None:
            rows = conn.execute(sql).fetchall()
        else:
            rows = []
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                rows += conn.execute(
                    f"{sql} AND bookmark_id IN ({','.join('?' * len(chunk))})", chunk
                ).fetchall()
        for bid, locator in rows:
            if bid is not None and locator:
                out.setdefault(int(bid), set()).add(locator)
    return out


def row_hidden(
    policy: DomainPolicy, source: str | None, url: str | None, fetched: Iterable[str] = ()
) -> bool:
    """True when domain policy hides the row: its own locator, or (for a
    row ``muninn scrape`` fetches) any locator a fetch of it ended on."""
    if policy_hides(policy, url):
        return True
    if source in API_CONTENT_SOURCES:
        return False
    return any(locator_hidden(policy, loc) for loc in fetched)


def row_visible_flag(
    conn: sqlite3.Connection, policy: DomainPolicy, bookmark_id: int,
    url: str | None, source: str | None = None,
) -> int:
    """``content_visible`` for a stored row under :func:`row_hidden`."""
    if source is None:
        found = conn.execute(
            "SELECT source FROM bookmarks WHERE bookmark_id = ?", (bookmark_id,)
        ).fetchone()
        source = found[0] if found else None
    fetched = () if source in API_CONTENT_SOURCES else fetched_locators(conn, [bookmark_id]).get(
        int(bookmark_id), ()
    )
    return 0 if row_hidden(policy, source, url, fetched) else 1


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


# ── The re-apply pass ───────────────────────────────────────────────────

@dataclass
class PolicyApplyReport:
    dry_run: bool = False
    checked: int = 0
    newly_hidden: int = 0
    newly_visible: int = 0
    locators_canonicalized: int = 0
    titles_changed: int = 0
    texts_masked: int = 0
    revisions_new: int = 0
    fts_refreshed: int = 0

    @property
    def changed(self) -> bool:
        return any((self.newly_hidden, self.newly_visible, self.locators_canonicalized,
                    self.titles_changed, self.texts_masked, self.fts_refreshed))

    def summary(self) -> str:
        return (
            f"checked={self.checked} newly_hidden={self.newly_hidden} "
            f"newly_visible={self.newly_visible} locators_canonicalized={self.locators_canonicalized} "
            f"titles_changed={self.titles_changed} texts_masked={self.texts_masked} "
            f"new_revisions={self.revisions_new} fts_refreshed={self.fts_refreshed}"
        )


def apply_policy(
    conn: sqlite3.Connection,
    policy: DomainPolicy,
    *,
    sources: Iterable[str] = ALL_SOURCES,
    dry_run: bool = False,
    now: int | None = None,
) -> PolicyApplyReport:
    """Re-apply domain policy to stored rows (no API call, no fetch).

    For every row of ``sources``: canonicalize X post locators, recompute
    ``content_visible`` from the stored ``url`` and the locators its
    fetches ended on (:func:`row_hidden`, as re-ingest and scrape do), and set
    hidden posts' titles to ``@handle`` (hidden articles: no title). Then,
    for every visible X post that links a hidden item, mask the hidden
    locator in its current text (the ``scrape_results`` live row, plus a
    new ``x_api`` revision through the shared revision writer; older
    revisions are left as they are) and in its title and keyword-index
    row. A post that became visible again gets its title back from its
    stored text; text already masked comes back only with a re-sync.

    One transaction, rolled back under ``dry_run`` (the report still says
    what would change). Must be called outside an open transaction.
    """
    if conn.in_transaction:
        raise RuntimeError("apply_policy must run outside an open transaction")
    now = int(time.time()) if now is None else int(now)
    report = PolicyApplyReport(dry_run=dry_run)
    try:
        _apply(conn, policy, tuple(sources), now, report)
    except BaseException:
        conn.rollback()
        raise
    if dry_run:
        conn.rollback()
    else:
        conn.commit()
    return report


def _apply(
    conn: sqlite3.Connection, policy: DomainPolicy, sources: tuple[str, ...], now: int,
    report: PolicyApplyReport,
) -> None:
    if not sources:
        return
    marks = ",".join("?" * len(sources))
    rows = conn.execute(
        "SELECT bookmark_id, source, source_id, url, title, content_visible, source_metadata "
        f"FROM bookmarks WHERE source IN ({marks}) ORDER BY bookmark_id",
        sources,
    ).fetchall()
    touched: set[int] = set()
    revived_posts: set[int] = set()
    fetched = fetched_locators(conn)
    for row in rows:
        report.checked += 1
        bid, source = int(row["bookmark_id"]), row["source"]
        meta = _load_meta(row["source_metadata"])
        url, title, was = row["url"], row["title"], int(row["content_visible"])
        if source == SOURCE_X_POST:
            canonical = canonical_post_locator(row["source_id"], meta, url)
            if canonical and canonical != url:
                url = canonical
                report.locators_canonicalized += 1
        visible = 0 if row_hidden(policy, source, url, fetched.get(bid, ())) else 1
        if visible != was:
            if visible:
                report.newly_visible += 1
            else:
                report.newly_hidden += 1
        if source == SOURCE_X_POST:
            if not visible:
                title = hidden_post_title(meta_handle(meta))
            elif not was:
                revived_posts.add(bid)
        elif source == SOURCE_X_ARTICLE:
            if not visible:
                title = None
            elif not was and title is None:
                title = _article_title(conn, meta, row["source_id"])
        if (url, visible, title) != (row["url"], was, row["title"]):
            conn.execute(
                "UPDATE bookmarks SET url = ?, domain = ?, content_visible = ?, title = ? "
                "WHERE bookmark_id = ?",
                (url, domain_of(url), visible, title, bid),
            )
            if title != row["title"]:
                report.titles_changed += 1
                touched.add(bid)

    parents: set[int] = set()
    if SOURCE_X_POST in sources:
        parents = {
            int(r[0]) for r in conn.execute(
                "SELECT DISTINCT l.parent_bookmark_id FROM item_links l "
                "JOIN bookmarks c ON c.bookmark_id = l.child_bookmark_id "
                "JOIN bookmarks p ON p.bookmark_id = l.parent_bookmark_id "
                "WHERE c.content_visible = 0 AND p.content_visible = 1 AND p.source = ?",
                (SOURCE_X_POST,),
            )
        }
        for bid in sorted(parents | revived_posts):
            _remask_post(conn, bid, now, report, touched)
    for bid in sorted(touched | parents):
        _refresh_fts(conn, bid, report)


def _article_title(conn: sqlite3.Connection, meta: dict[str, Any], article_id: str) -> str | None:
    """A revived article's title, from the post that carried it."""
    via = meta.get("via_post_id")
    if not isinstance(via, str):
        return None
    row = conn.execute(
        "SELECT source_metadata FROM bookmarks WHERE source = ? AND source_id = ?",
        (SOURCE_X_POST, via),
    ).fetchone()
    article = _load_meta(row[0]).get("article") if row else None
    if isinstance(article, dict) and article.get("id") == article_id:
        title = article.get("title")
        return title if isinstance(title, str) and title else None
    return None


def _remask_post(
    conn: sqlite3.Connection, bid: int, now: int, report: PolicyApplyReport, touched: set[int]
) -> None:
    row = conn.execute(
        "SELECT source_id, title, captured_at, source_metadata FROM bookmarks WHERE bookmark_id = ?",
        (bid,),
    ).fetchone()
    meta = _load_meta(row["source_metadata"])
    patterns = hidden_child_patterns(conn, bid)
    live = conn.execute(
        "SELECT content_text, extraction_quality FROM scrape_results "
        "WHERE bookmark_id = ? AND pass = 'live'",
        (bid,),
    ).fetchone()
    text = live["content_text"] if live is not None else None
    if text:
        masked = mask_text(text, patterns)
        if masked != text:
            conn.execute(
                "UPDATE scrape_results SET content_text = ? WHERE bookmark_id = ? AND pass = 'live'",
                (masked, bid),
            )
            report.texts_masked += 1
            published = meta.get("published_at")
            added = record_revision(
                conn,
                bookmark_id=bid,
                item_ref=x_post_item_id(row["source_id"]),
                text=masked,
                coverage="full" if live["extraction_quality"] == "ok" else "partial",
                retrieval_method="x_api",
                extractor_name=X_TEXT_EXTRACTOR[0],
                extractor_version=X_TEXT_EXTRACTOR[1],
                retrieved_at=now,
                published_at=published if isinstance(published, int) else None,
                observed_at=(
                    int(row["captured_at"]) if meta.get("bookmarked", True) is not False else None
                ),
            )
            report.revisions_new += added is not None
            touched.add(bid)
        title = post_title_for(meta_handle(meta), masked)
    else:
        title = mask_text(row["title"], patterns) if row["title"] else row["title"]
    if title != row["title"]:
        conn.execute("UPDATE bookmarks SET title = ? WHERE bookmark_id = ?", (title, bid))
        report.titles_changed += 1
        touched.add(bid)


def _refresh_fts(conn: sqlite3.Connection, bid: int, report: PolicyApplyReport) -> None:
    """Rewrite the row's keyword-index entry with its current title and the
    hidden children's locators masked (summary and indexed text). The
    ``enriched`` row and the Qdrant point are not touched: the changed text
    makes the next ``muninn enrich`` redo them."""
    old = conn.execute(
        "SELECT title, summary, content_text, tags FROM fts_bookmarks WHERE rowid = ?", (bid,)
    ).fetchone()
    if old is None:
        return
    title = conn.execute("SELECT title FROM bookmarks WHERE bookmark_id = ?", (bid,)).fetchone()[0]
    patterns = hidden_child_patterns(conn, bid)
    new = (
        title or "",
        mask_text(old[1] or "", patterns),
        mask_text(old[2] or "", patterns),
        old[3],
    )
    if new == tuple(old):
        return
    conn.execute("DELETE FROM fts_bookmarks WHERE rowid = ?", (bid,))
    conn.execute(
        "INSERT INTO fts_bookmarks (rowid, title, summary, content_text, tags) VALUES (?, ?, ?, ?, ?)",
        (bid, *new),
    )
    report.fts_refreshed += 1


__all__ = [
    "ALL_SOURCES",
    "HIDDEN_LINK",
    "PolicyApplyReport",
    "X_ARTICLE_EXTRACTOR",
    "X_HOSTS",
    "X_TEXT_EXTRACTOR",
    "apply_policy",
    "archived_original",
    "canonical_post_locator",
    "child_mask_patterns",
    "domain_of",
    "fetched_locators",
    "handle_from_locator",
    "hidden_child_patterns",
    "hidden_post_title",
    "locator_hidden",
    "mask_text",
    "meta_handle",
    "policy_hides",
    "post_locator",
    "post_title_for",
    "row_hidden",
    "row_visible_flag",
    "title_from_text",
    "visible_flag",
]
