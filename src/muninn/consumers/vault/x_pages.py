"""X-lane vault pages: X posts, X Articles and the items posts link to.

Rows with ``source`` in :data:`X_SOURCES` (contract
``muninn-research-intake`` section 10.1) compile into the same
``wiki/bookmarks/`` namespace as browser bookmarks, through their own
template (``x_item_page.md.j2``), so browser pages stay byte-identical.

What the DB holds for these rows (contract sections 3.1 and 10):

- ``bookmarks``: ``source='x'`` (``source_id`` = post ID), ``'x_article'``
  (article ID) or ``'x_link'`` (sha256 of the sanitized locator, which is
  ``bookmarks.url``). ``captured_at`` is the first sync that saw the item.
  ``source_metadata`` carries post metadata (author, publication time,
  language, last observation), never the text.
- ``scrape_results(pass='live')``: the current text, for a post the API's
  post text, and ``fetched_at`` its retrieval time.
- ``scrape_history``: every distinct revision, with ``published_at``.
- ``item_links``: explicit parent → child relations, rendered here as
  bidirectional wikilinks.
- ``assessments``: optional; the latest disposition goes in frontmatter.

The three times stay separate (contract section 8): ``published_at`` (the
source's publication time, never inferred from the bookmark),
``observed_at`` (the bookmark observation, an upper bound for X since the
API reports no bookmark time) and ``retrieved_at`` (when the current text
was fetched).

Privacy: hidden rows (``content_visible = 0``) get no page, no wikilink
and no display text, and a hidden link's locator is masked out of the
post text that would otherwise quote it.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from muninn.consumers.packets.canonical import (
    link_item_id,
    x_article_item_id,
    x_post_item_id,
)
from muninn.intake_records import HIDDEN_LINK_TEXT, mask_locators, mask_title, mentions_locator

X_SOURCES = frozenset({"x", "x_article", "x_link"})

# Children listed in this order on a parent page, then by entity position.
_RELATION_ORDER = {"article": 0, "quotes": 1, "replies_to": 2, "links_to": 3}
_RELATION_LABELS = {
    "article": "article",
    "quotes": "quotes",
    "replies_to": "replies to",
    "links_to": "links to",
}
_INVERSE_LABELS = {
    "article": "article of",
    "quotes": "quoted by",
    "replies_to": "replied to by",
    "links_to": "linked from",
}

# enrichment_source → the scrape pass it names (as in muninn.enrich.pipeline).
_SOURCE_TO_PASS = {
    "at_capture": "at_capture",
    "recent_archive": "recent_archive",
    "live_fallback": "live",
}

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_LINK_TEXT_UNSAFE = re.compile(r"[\[\]|\r\n]+")
_TITLE_TEXT_CHARS = 60
_SLUG_TEXT_CHARS = 60


# ── small helpers ─────────────────────────────────────────────────


def _slugify(value: str, limit: int | None = None) -> str:
    s = _SLUG_RE.sub("-", value.lower()).strip("-")
    if limit is not None and len(s) > limit:
        s = s[:limit].rstrip("-")
    return s


def iso_utc(epoch: int | None) -> str | None:
    """Epoch seconds → ``2026-09-30T14:05:00Z`` (contract time format)."""
    if epoch is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(epoch)))


def _to_epoch(value: Any) -> int | None:
    """Accept epoch seconds or an ISO-8601 string; anything else is None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if text.isdigit():
            return int(text)
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    return None


def parse_metadata(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _first(mapping: dict, *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return None


def author_from(meta: dict) -> dict | None:
    """``source_metadata.author`` in either the X API's spelling
    (``id``/``username``/``name``) or the contract's
    (``platform_id``/``handle``/``display_name``)."""
    author = meta.get("author")
    if not isinstance(author, dict):
        return None
    handle = _first(author, "handle", "username")
    out = {
        "handle": str(handle).lstrip("@") if handle is not None else None,
        "display_name": _first(author, "display_name", "name"),
        "platform_id": _first(author, "platform_id", "id"),
    }
    if out["platform_id"] is not None:
        out["platform_id"] = str(out["platform_id"])
    return out if any(v is not None for v in out.values()) else None


def _link_text(value: str) -> str:
    return _LINK_TEXT_UNSAFE.sub(" ", value).strip()


def _one_line(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 3].rstrip() + "..."


# ── identity and slugs ────────────────────────────────────────────


def item_id_for(row: sqlite3.Row) -> str | None:
    """The contract's typed export ID (section 3.1), or None if malformed."""
    source, source_id = row["source"], str(row["source_id"])
    try:
        if source == "x":
            return x_post_item_id(source_id)
        if source == "x_article":
            return x_article_item_id(source_id)
    except ValueError:
        return None
    if source == "x_link" and row["url"]:
        return link_item_id(row["url"])
    return None


def item_kind_for(row: sqlite3.Row, item_id: str | None = None) -> str:
    """``x_post``, ``x_article``, ``github_repo`` or ``web_page``."""
    if row["source"] == "x":
        return "x_post"
    if row["source"] == "x_article":
        return "x_article"
    item_id = item_id if item_id is not None else item_id_for(row)
    if item_id and item_id.startswith("github:repo:"):
        return "github_repo"
    return "web_page"


def x_slug(row: sqlite3.Row) -> str:
    """Readable slug built only from the item's identity, so it doesn't
    move when a title or summary changes:

    - post: ``x-<handle>-<post id>`` (``x-post-<id>`` without a handle)
    - article: ``x-article-<article id>``
    - repository: ``github-<owner>-<name>``
    - web page: ``<host and path, ≤60 chars>-<first 8 of source_id>``

    The compiler appends ``-b<bookmark_id>`` if two pages would collide.
    """
    source, source_id = row["source"], str(row["source_id"])
    if source == "x":
        author = author_from(parse_metadata(row["source_metadata"])) or {}
        handle = _slugify(author.get("handle") or "")
        return f"x-{handle}-{_slugify(source_id)}" if handle else f"x-post-{_slugify(source_id)}"
    if source == "x_article":
        return f"x-article-{_slugify(source_id)}"
    item_id = item_id_for(row)
    if item_id and item_id.startswith("github:repo:"):
        owner_name = item_id.removeprefix("github:repo:")
        return "github-" + _slugify(owner_name.replace("/", "-"))
    base = ""
    if row["url"]:
        parts = urlsplit(row["url"])
        host = (parts.hostname or "").removeprefix("www.")
        base = _slugify(f"{host} {parts.path}", _SLUG_TEXT_CHARS)
    if not base:
        base = _slugify(row["title"] or "", _SLUG_TEXT_CHARS) or "link"
    return f"{base}-{_slugify(source_id)[:8] or row['bookmark_id']}"


# ── the link graph ────────────────────────────────────────────────


@dataclass(frozen=True)
class Edge:
    relation: str
    position: int
    other_id: int


@dataclass
class LinkGraph:
    """``item_links`` restricted to visible endpoints, both directions."""

    children: dict[int, list[Edge]] = field(default_factory=dict)
    parents: dict[int, list[Edge]] = field(default_factory=dict)
    hidden_child_urls: dict[int, set[str]] = field(default_factory=dict)

    def neighbours(self, bookmark_id: int) -> set[int]:
        return {e.other_id for e in self.children.get(bookmark_id, [])} | {
            e.other_id for e in self.parents.get(bookmark_id, [])
        }


def has_table(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        is not None
    )


def load_link_graph(conn: sqlite3.Connection, visible_ids: set[int]) -> LinkGraph:
    graph = LinkGraph()
    if not has_table(conn, "item_links"):
        return graph
    hidden_children: dict[int, set[int]] = {}
    seen: set[tuple[int, str, int]] = set()
    for r in conn.execute(
        "SELECT parent_bookmark_id AS p, child_bookmark_id AS c, relation, position "
        "FROM item_links "
        "WHERE parent_bookmark_id IS NOT NULL AND child_bookmark_id IS NOT NULL "
        "ORDER BY parent_bookmark_id, position, item_link_id"
    ):
        p, c, relation = r["p"], r["c"], r["relation"]
        if p not in visible_ids:
            continue  # a hidden parent exposes nothing, in either direction
        if c not in visible_ids:
            hidden_children.setdefault(p, set()).add(c)
            continue
        if (p, relation, c) in seen:
            continue  # the same item linked twice from one post: list it once
        seen.add((p, relation, c))
        graph.children.setdefault(p, []).append(Edge(relation, r["position"], c))
        graph.parents.setdefault(c, []).append(Edge(relation, r["position"], p))

    order = lambda e: (_RELATION_ORDER.get(e.relation, 99), e.position, e.other_id)  # noqa: E731
    for edges in graph.children.values():
        edges.sort(key=order)
    for edges in graph.parents.values():
        edges.sort(key=lambda e: (_RELATION_ORDER.get(e.relation, 99), e.other_id))

    for parent, child_ids in hidden_children.items():
        placeholders = ",".join("?" * len(child_ids))
        urls = {
            row["url"]
            for row in conn.execute(
                f"SELECT url FROM bookmarks WHERE bookmark_id IN ({placeholders}) "
                "AND url IS NOT NULL AND url != ''",
                sorted(child_ids),
            )
        }
        if urls:
            graph.hidden_child_urls[parent] = urls
    return graph


def mask_hidden_links(text: str, urls: set[str]) -> str:
    """Replace hidden links' locators in text with ``[hidden link]``.

    Case-insensitive, with or without scheme and ``www.``: a repository's
    stored locator is lowercase while the post text keeps the author's
    case. See :func:`muninn.intake_records.mask_locators`."""
    return mask_locators(text, urls)


def mask_hidden_title(title: str, urls: set[str]) -> str:
    """:func:`mask_hidden_links` for a post title. The writer's title is
    the first ~80 characters of the text, so it can also end in a
    truncated locator (``… https://intra…``); a final token that is a
    prefix of a hidden locator is masked too."""
    return mask_title(title, urls)


def enrichment_quotes_hidden(urls: set[str], *texts: Any) -> bool:
    """True when enrichment output (summary, key quotes, entities: strings
    or JSON-able lists) quotes one of these hidden locators.

    Such output was written before a policy change hid the link, and its
    prose can paraphrase the hidden page, not just quote its locator, so
    consumers withhold it whole (:func:`withhold_enrichment`) rather than
    only masking the locator."""
    if not urls:
        return False
    for value in texts:
        if value is None:
            continue
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        if mentions_locator(text, urls):
            return True
    return False


ENRICHMENT_WITHHELD = (
    "_Summary withheld: it was written before a linked item was hidden by domain "
    "policy. It returns when the item is enriched again._"
)


def withhold_enrichment(
    urls: set[str], summary: Any, key_quotes: Any, entities: Any
) -> tuple[Any, Any, Any, bool]:
    """``(summary, key_quotes, entities, withheld)`` for display.

    When any of the three quotes a hidden locator, all three are withheld:
    the summary becomes None and the lists empty (same type as given: a
    JSON string stays a JSON string). Otherwise they are returned
    unchanged: nothing in them quotes a hidden locator."""
    if enrichment_quotes_hidden(urls, summary, key_quotes, entities):
        empty = lambda v: "[]" if isinstance(v, str) else []  # noqa: E731
        return None, empty(key_quotes), empty(entities), True
    return summary, key_quotes, entities, False


def hidden_child_urls(conn: sqlite3.Connection, bookmark_id: int) -> set[str]:
    """Locators of the hidden items a row links to (for one-off lookups;
    the compiler gets the same set from :func:`load_link_graph`)."""
    if not has_table(conn, "item_links"):
        return set()
    return {
        row["url"]
        for row in conn.execute(
            "SELECT b.url FROM item_links l JOIN bookmarks b ON b.bookmark_id = l.child_bookmark_id "
            "WHERE l.parent_bookmark_id = ? AND b.content_visible = 0 "
            "AND b.url IS NOT NULL AND b.url != ''",
            (bookmark_id,),
        )
    }


# ── per-page view ─────────────────────────────────────────────────


def _scrape_row(conn: sqlite3.Connection, bookmark_id: int, pass_: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT pass, fetched_at, scrape_status, content_text FROM scrape_results "
        "WHERE bookmark_id = ? AND pass = ?",
        (bookmark_id, pass_),
    ).fetchone()


def selected_scrape_pass(row: sqlite3.Row, *, for_body: bool = False) -> str:
    """Canonical enrichment-selected capture, or live when none is selected.

    A post's source text is always the current API/live text; its enrichment
    retrieval metadata can still name another pass. Index projections use
    ``for_body=True`` to retain that distinction without concatenating captures.
    """
    if for_body and row["source"] == "x":
        return "live"
    return _SOURCE_TO_PASS.get(row["enrichment_source"] or "", "live")


def _history_published_at(conn: sqlite3.Connection, bookmark_id: int) -> int | None:
    if not has_table(conn, "scrape_history"):
        return None
    row = conn.execute(
        "SELECT published_at FROM scrape_history "
        "WHERE bookmark_id = ? AND published_at IS NOT NULL "
        "ORDER BY retrieved_at DESC, scrape_history_id DESC LIMIT 1",
        (bookmark_id,),
    ).fetchone()
    return row["published_at"] if row else None


def latest_assessment(conn: sqlite3.Connection, bookmark_id: int) -> dict | None:
    if not has_table(conn, "assessments"):
        return None
    row = conn.execute(
        "SELECT disposition, assessed_at FROM assessments WHERE bookmark_id = ? "
        "ORDER BY assessed_at DESC, assessment_id DESC LIMIT 1",
        (bookmark_id,),
    ).fetchone()
    if row is None:
        return None
    return {"disposition": row["disposition"], "assessed_at": iso_utc(row["assessed_at"])}


def display_name(row: sqlite3.Row) -> str:
    """Link text for a page: ``@handle`` for a post, else title, else locator."""
    if row["source"] == "x":
        author = author_from(parse_metadata(row["source_metadata"])) or {}
        if author.get("handle"):
            return _link_text(f"@{author['handle']}")
        return _link_text(f"X post {row['source_id']}")
    return _link_text(row["title"] or row["url"] or str(row["source_id"]))


def build_view(
    conn: sqlite3.Connection,
    bm: sqlite3.Row,
    *,
    rows_by_id: dict[int, sqlite3.Row],
    slug_by_id: dict[int, str],
    graph: LinkGraph,
) -> dict:
    """Everything ``x_item_page.md.j2`` renders for one X-lane row."""
    bid = bm["bookmark_id"]
    meta = parse_metadata(bm["source_metadata"])
    item_id = item_id_for(bm)
    kind = item_kind_for(bm, item_id)
    is_post = kind == "x_post"
    author = author_from(meta) if bm["source"] in ("x", "x_article") else None

    # The current retrieval: the pass the enrichment read, else 'live'.
    canonical_pass = selected_scrape_pass(bm)
    scrape = _scrape_row(conn, bid, canonical_pass) or _scrape_row(conn, bid, "live")

    text = None
    if is_post:
        live = _scrape_row(conn, bid, "live")
        if live is not None and live["content_text"]:
            text = mask_hidden_links(live["content_text"], graph.hidden_child_urls.get(bid, set()))

    published = _to_epoch(meta.get("published_at"))
    if published is None:
        published = _history_published_at(conn, bid)

    # Observation: for a bookmarked post, the first sync that saw it
    # (captured_at). A post muninn holds only because a bookmark quotes it
    # (source_metadata.bookmarked == false) has no observation. Linked
    # items are never bookmarked themselves: captured_at is first_seen_at.
    bookmarked = is_post and meta.get("bookmarked") is not False
    times = {
        "published_at": iso_utc(published),
        "observed_at": iso_utc(bm["captured_at"]) if bookmarked else None,
        "last_observed_at": iso_utc(_to_epoch(meta.get("last_observed_at"))) if bookmarked else None,
        "first_seen_at": None if bookmarked else iso_utc(bm["captured_at"]),
        "retrieved_at": iso_utc(scrape["fetched_at"]) if scrape is not None else None,
    }

    if is_post:
        handle = (author or {}).get("handle")
        if bm["title"]:
            title = mask_hidden_title(bm["title"], graph.hidden_child_urls.get(bid, set()))
        elif handle and text:
            title = f"@{handle}: {_one_line(text, _TITLE_TEXT_CHARS)}"
        elif handle:
            title = f"@{handle} on X"
        else:
            title = f"X post {bm['source_id']}"
        heading = f"@{handle}" if handle else f"X post {bm['source_id']}"
        if handle and author and author.get("display_name"):
            heading += f" ({author['display_name']})"
    else:
        title = bm["title"] or bm["url"] or "(untitled)"
        heading = title

    def _edges(edges: list[Edge], labels: dict[str, str]) -> list[dict]:
        return [
            {
                "label": labels.get(e.relation, e.relation),
                "slug": slug_by_id[e.other_id],
                "text": display_name(rows_by_id[e.other_id]),
            }
            for e in edges
            if e.other_id in slug_by_id
        ]

    return {
        "kind": kind,
        "item_id": item_id,
        "title": title,
        "heading": heading,
        "author": author,
        "language": _first(meta, "lang", "language"),
        "times": times,
        "observed_at_basis": "first_seen_by_sync" if bookmarked else None,
        "retrieval_status": scrape["scrape_status"] if scrape is not None else None,
        "post_text": text,
        "assessment": latest_assessment(conn, bid),
        "links": _edges(graph.children.get(bid, []), _RELATION_LABELS),
        "linked_from": _edges(graph.parents.get(bid, []), _INVERSE_LABELS),
    }


__all__ = [
    "ENRICHMENT_WITHHELD",
    "HIDDEN_LINK_TEXT",
    "X_SOURCES",
    "LinkGraph",
    "build_view",
    "enrichment_quotes_hidden",
    "hidden_child_urls",
    "item_id_for",
    "item_kind_for",
    "iso_utc",
    "load_link_graph",
    "mask_hidden_links",
    "mask_hidden_title",
    "withhold_enrichment",
    "x_slug",
]
