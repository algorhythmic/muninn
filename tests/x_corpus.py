"""Synthetic X-lane corpus, seeded the way the X adapter stores it.

Follows ``docs/contracts/MUNINN-RESEARCH-INTAKE.md`` section 10.1 (the X
adapter mapping) and section 3.1 (item identity) over migrations 001 and
002. The real writer (RI-04, ``muninn ingest x``) is being built in
parallel; this helper is the compile/index side's statement of what it
expects, to be cross-checked against that writer.

Row shapes assumed (every value synthetic):

- **Bookmarked post** — ``bookmarks(source='x', source_id=<post id>,
  title=NULL, url=<sanitized post URL>, captured_at=<first sync that saw
  it>, enrichment_source='live_fallback')``. ``source_metadata`` holds
  ``author`` (``id``/``username``/``name``, the API's spelling),
  ``published_at`` (epoch seconds), ``lang``, ``conversation_id``,
  ``possibly_sensitive``, ``media_keys``, ``is_long_post`` and
  ``last_observed_at`` — never the text. The text is the
  ``scrape_results(pass='live')`` row (``fetched_at`` = retrieval time)
  and a ``scrape_history(retrieval_method='x_api')`` revision.
- **Quoted post** — another ``source='x'`` row joined by
  ``item_links(relation='quotes', locator_resolution='api_reference')``.
  It was not bookmarked, which this corpus records as
  ``source_metadata.bookmarked = false`` (a proposed key: the contract
  does not yet say how a referenced-only post is marked).
- **X Article** — ``bookmarks(source='x_article', source_id=<article id>)``
  + ``item_links(relation='article', position=0)``; its body is an
  ``x_api`` revision.
- **Linked items** — ``bookmarks(source='x_link',
  source_id=sha256(<sanitized locator>), url=<that locator>)`` +
  ``item_links(relation='links_to', position=<URL entity index>,
  locator_resolution='unwound')``; content from a ``live`` HTTP fetch.
  One is a GitHub repository root (``github:repo:<owner>/<name>``), one a
  web page (``web:<sha256>``) and one is hidden by domain policy
  (``content_visible=0``, ``enrichment_source='none'``, never scraped).
- Post text has each ``t.co`` link replaced by its canonical locator
  (section 10, proposed), so the hidden link's locator appears in it.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from muninn.consumers.packets.canonical import (
    TEXT_NORMALIZATION,
    content_digest,
    link_item_id,
    locator_source_id,
    new_ulid,
    normalize_text,
    x_article_item_id,
    x_post_item_id,
)

POST_ID = "1900000000000000001"
QUOTED_POST_ID = "1900000000000000002"
ARTICLE_ID = "1900000000000000501"
AUTHOR_A = {"id": "1900000000000000901", "username": "synth_author_a", "name": "Synthetic Author A"}
AUTHOR_B = {"id": "1900000000000000902", "username": "synth_author_b", "name": "Synthetic Author B"}

REPO_URL = "https://github.com/synthetic-org/synthetic-repo"
WEB_URL = "https://example.com/notes/retrieval-eval"
HIDDEN_URL = "https://intranet.example/private/roadmap"

# Times kept deliberately far apart so a conflation shows up in tests.
PUBLISHED_AT = 1_759_241_100   # 2025-09-30T14:05:00Z: the post's created_at
OBSERVED_AT = 1_760_947_200    # 2025-10-20T08:00:00Z: first sync that saw the bookmark
RETRIEVED_AT = 1_760_947_205   # 2025-10-20T08:00:05Z: the API response
LAST_OBSERVED_AT = 1_761_026_400  # 2025-10-21T06:00:00Z: a later sync
LINK_FETCHED_AT = 1_760_947_400   # live fetch of the linked pages
QUOTED_PUBLISHED_AT = 1_757_669_400  # 2025-09-12T09:30:00Z
ARTICLE_PUBLISHED_AT = 1_759_240_800  # 2025-09-30T14:00:00Z

POST_TEXT = (
    "Thread on evaluating retrieval with the quillwort benchmark.\n\n"
    f"Notes: {WEB_URL}\n"
    f"Code: {REPO_URL}\n"
    f"Draft plan: {HIDDEN_URL}"
)
QUOTED_TEXT = "Retrieval metrics without a held-out set are mostly vibes."
ARTICLE_TEXT = (
    "A synthetic long-form article about recall at k, judged relevance, "
    "and why the quillwort benchmark keeps a frozen query set."
)
REPO_TEXT = "synthetic-repo: a synthetic evaluation harness for hybrid retrieval."
WEB_TEXT = "Synthetic notes on retrieval evaluation: recall, precision and nDCG."


@dataclass(frozen=True)
class XCorpus:
    post: int
    quoted: int
    article: int
    repo: int
    web: int
    hidden: int


def _insert_item(
    conn: sqlite3.Connection,
    *,
    source: str,
    source_id: str,
    url: str,
    domain: str,
    title: str | None,
    captured_at: int,
    metadata: dict | None,
    visible: bool = True,
    enrichment_source: str = "live_fallback",
) -> int:
    cur = conn.execute(
        "INSERT INTO bookmarks (source, source_id, captured_at, title, url, domain, "
        "  source_metadata, content_visible, enrichment_source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            source,
            source_id,
            captured_at,
            title,
            url,
            domain,
            json.dumps(metadata) if metadata is not None else None,
            1 if visible else 0,
            enrichment_source,
        ),
    )
    return int(cur.lastrowid)


def _insert_content(
    conn: sqlite3.Connection,
    *,
    bookmark_id: int,
    item_ref: str,
    text: str,
    retrieval_method: str,
    final_url: str,
    fetched_at: int,
    published_at: int | None,
    observed_at: int | None,
) -> None:
    """Current state in scrape_results(pass='live') + one history revision."""
    normalized = normalize_text(text)
    conn.execute(
        "INSERT INTO scrape_results (bookmark_id, pass, fetched_at, final_url, "
        "  http_status, scrape_status, extraction_quality, content_text) "
        "VALUES (?, 'live', ?, ?, 200, 'ok', 'ok', ?)",
        (bookmark_id, fetched_at, final_url, normalized),
    )
    extractor = ("x_api_v2", "2") if retrieval_method == "x_api" else ("trafilatura", "2.0.0")
    conn.execute(
        "INSERT INTO scrape_history (revision_ulid, bookmark_id, item_ref, content_sha256, "
        "  normalization, char_count, content_text, coverage, retrieval_method, scrape_pass, "
        "  extractor_name, extractor_version, http_status, final_locator, published_at, "
        "  retrieved_at, observed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'full', ?, 'live', ?, ?, 200, ?, ?, ?, ?)",
        (
            new_ulid(fetched_at * 1000),
            bookmark_id,
            item_ref,
            content_digest(normalized),
            TEXT_NORMALIZATION,
            len(normalized),
            normalized,
            retrieval_method,
            extractor[0],
            extractor[1],
            final_url,
            published_at,
            fetched_at,
            observed_at,
        ),
    )


def _link(
    conn: sqlite3.Connection,
    *,
    parent: int,
    child: int,
    child_ref: str,
    relation: str,
    position: int,
    resolution: str | None,
    entity_status: int | None = None,
) -> None:
    conn.execute(
        "INSERT INTO item_links (parent_bookmark_id, child_bookmark_id, parent_ref, "
        "  child_ref, relation, position, locator_resolution, entity_status, first_seen_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (parent, child, x_post_item_id(POST_ID), child_ref, relation, position,
         resolution, entity_status, OBSERVED_AT),
    )


def seed_x_corpus(conn: sqlite3.Connection) -> XCorpus:
    """Insert one bookmarked post with its quote, article and links."""
    post_ref = x_post_item_id(POST_ID)
    post = _insert_item(
        conn,
        source="x",
        source_id=POST_ID,
        url=f"https://x.com/{AUTHOR_A['username']}/status/{POST_ID}",
        domain="x.com",
        title=None,
        captured_at=OBSERVED_AT,
        metadata={
            "author": AUTHOR_A,
            "published_at": PUBLISHED_AT,
            "lang": "en",
            "conversation_id": POST_ID,
            "possibly_sensitive": False,
            "media_keys": [],
            "is_long_post": False,
            "last_observed_at": LAST_OBSERVED_AT,
        },
    )
    _insert_content(
        conn, bookmark_id=post, item_ref=post_ref, text=POST_TEXT, retrieval_method="x_api",
        final_url=f"https://x.com/{AUTHOR_A['username']}/status/{POST_ID}",
        fetched_at=RETRIEVED_AT, published_at=PUBLISHED_AT, observed_at=OBSERVED_AT,
    )

    quoted_ref = x_post_item_id(QUOTED_POST_ID)
    quoted = _insert_item(
        conn,
        source="x",
        source_id=QUOTED_POST_ID,
        url=f"https://x.com/{AUTHOR_B['username']}/status/{QUOTED_POST_ID}",
        domain="x.com",
        title=None,
        captured_at=OBSERVED_AT,
        metadata={
            "author": AUTHOR_B,
            "published_at": QUOTED_PUBLISHED_AT,
            "lang": "en",
            "conversation_id": QUOTED_POST_ID,
            "bookmarked": False,
        },
    )
    _insert_content(
        conn, bookmark_id=quoted, item_ref=quoted_ref, text=QUOTED_TEXT,
        retrieval_method="x_api",
        final_url=f"https://x.com/{AUTHOR_B['username']}/status/{QUOTED_POST_ID}",
        fetched_at=RETRIEVED_AT, published_at=QUOTED_PUBLISHED_AT, observed_at=None,
    )

    article_ref = x_article_item_id(ARTICLE_ID)
    article = _insert_item(
        conn,
        source="x_article",
        source_id=ARTICLE_ID,
        url=f"https://x.com/i/article/{ARTICLE_ID}",
        domain="x.com",
        title="A synthetic long-form article",
        captured_at=OBSERVED_AT,
        metadata={"author": AUTHOR_A, "published_at": ARTICLE_PUBLISHED_AT},
    )
    _insert_content(
        conn, bookmark_id=article, item_ref=article_ref, text=ARTICLE_TEXT,
        retrieval_method="x_api", final_url=f"https://x.com/i/article/{ARTICLE_ID}",
        fetched_at=RETRIEVED_AT, published_at=ARTICLE_PUBLISHED_AT, observed_at=None,
    )

    links = {}
    for key, url, domain, title, text in (
        ("web", WEB_URL, "example.com", "Synthetic notes on retrieval evaluation", WEB_TEXT),
        ("repo", REPO_URL, "github.com", "synthetic-org/synthetic-repo", REPO_TEXT),
        ("hidden", HIDDEN_URL, "intranet.example", "Private roadmap", None),
    ):
        links[key] = _insert_item(
            conn,
            source="x_link",
            source_id=locator_source_id(url),
            url=url,
            domain=domain,
            title=title,
            captured_at=OBSERVED_AT,
            metadata=None,
            visible=key != "hidden",
            enrichment_source="none" if key == "hidden" else "live_fallback",
        )
        if text is not None:
            _insert_content(
                conn, bookmark_id=links[key], item_ref=link_item_id(url), text=text,
                retrieval_method="live", final_url=url, fetched_at=LINK_FETCHED_AT,
                published_at=None, observed_at=None,
            )

    _link(conn, parent=post, child=article, child_ref=article_ref, relation="article",
          position=0, resolution=None)
    _link(conn, parent=post, child=quoted, child_ref=quoted_ref, relation="quotes",
          position=0, resolution="api_reference")
    for position, key in enumerate(("web", "repo", "hidden")):
        url = {"web": WEB_URL, "repo": REPO_URL, "hidden": HIDDEN_URL}[key]
        _link(conn, parent=post, child=links[key], child_ref=link_item_id(url),
              relation="links_to", position=position, resolution="unwound", entity_status=200)
    conn.commit()
    return XCorpus(
        post=post, quoted=quoted, article=article,
        repo=links["repo"], web=links["web"], hidden=links["hidden"],
    )
