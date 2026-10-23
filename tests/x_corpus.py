"""Synthetic X-lane corpus, seeded the way `muninn ingest x` stores it.

Row shapes follow the real writer (:mod:`muninn.ingest.x_store`, contract
``docs/contracts/MUNINN-RESEARCH-INTAKE.md`` section 10.1) followed by
`muninn scrape --source x_link` for the linked pages.
``tests/integration/test_x_corpus_fidelity.py`` runs the writer on the
connector fixtures and checks that these rows have the same columns and
``source_metadata`` keys, so the vault and recall tests built on this seed
can't drift from what ingest really writes.

Rows (every value synthetic):

- **Bookmarked post** — ``bookmarks(source='x', source_id=<post id>,
  title='@<handle>: <first 80 chars>', url=<sanitized post URL>,
  captured_at=<first sync that saw it>, enrichment_source='live_fallback')``.
  ``source_metadata`` holds ``kind='x_post'``, ``author``
  (``id``/``username``/``name``, the API's spelling), ``published_at``
  (epoch seconds), ``lang``, ``conversation_id``, ``possibly_sensitive``,
  ``media_keys``, ``is_long_post``, ``referenced``, ``article``,
  ``bookmarked=true``, the sync keys (``first_sync_run_id``,
  ``last_sync_run_id``, ``last_observed_at``) and ``list_position`` —
  never the text. The text is the ``scrape_results(pass='live')`` row
  and a ``scrape_history(retrieval_method='x_api')`` revision with
  extractor ``muninn-x-post-text/1``.
- **Quoted post** — another ``source='x'`` row with
  ``bookmarked=false`` and no sync keys, joined by
  ``item_links(relation='quotes', locator_resolution='api_reference')``.
- **X Article** — ``bookmarks(source='x_article', source_id=<article id>)``
  (``kind``, ``author``, ``published_at``, ``lang``, ``via_post_id``,
  ``has_body``) + ``item_links(relation='article', position=0,
  locator_resolution='api_reference')``; its body is an ``x_api``
  revision with extractor ``muninn-x-article-text/1``.
- **Linked items** — ``bookmarks(source='x_link',
  source_id=sha256(<sanitized locator>), url=<that locator>)`` with
  ``source_metadata`` ``item_id``/``kind`` (+ ``repository`` for a GitHub
  root) + ``item_links(relation='links_to', position=<URL entity index>,
  locator_resolution='unwound')``; content from a ``live`` HTTP fetch
  (`muninn scrape`). One is a GitHub repository root
  (``github:repo:<owner>/<name>``), one a web page (``web:<sha256>``) and
  one is hidden by domain policy (``content_visible=0``, never scraped, so
  ``enrichment_source`` stays NULL).
- Post text has each ``t.co`` link replaced by its canonical locator, and
  a link that domain policy hid at ingest by ``[hidden link]``. With
  ``hidden_at_ingest=False`` the post text still quotes the hidden link's
  locator instead: the state after a policy change hides an item the last
  sync wrote unhidden, which the compiler must mask.

Deliberate deviation: the post's retrieval time is 5 s after its
observation, where one real sync stamps both with the page time, so a
test can tell the three times apart.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from muninn.consumers.packets.canonical import (
    link_item_id,
    locator_source_id,
    new_ulid,
    normalize_text,
    x_article_item_id,
    x_post_item_id,
)
from muninn.ingest.x_store import HIDDEN_LINK, X_ARTICLE_EXTRACTOR, X_TEXT_EXTRACTOR
from muninn.revisions import record_revision
from muninn.scrape.extract import EXTRACTOR_NAME, EXTRACTOR_VERSION

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
FIRST_SYNC_RUN = new_ulid(OBSERVED_AT * 1000)
LAST_SYNC_RUN = new_ulid(LAST_OBSERVED_AT * 1000)

ARTICLE_TITLE = "A synthetic long-form article"


def post_text(*, hidden_at_ingest: bool = True) -> str:
    hidden = HIDDEN_LINK if hidden_at_ingest else HIDDEN_URL
    return (
        "Thread on evaluating retrieval with the quillwort benchmark.\n\n"
        f"Notes: {WEB_URL}\n"
        f"Code: {REPO_URL}\n"
        f"Draft plan: {hidden}"
    )


POST_TEXT = post_text()
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


def post_title(handle: str, text: str, limit: int = 80) -> str:
    """The writer's title rule: ``@handle: <first ~80 characters>``."""
    flat = " ".join(text.split())
    snippet = flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"
    return f"@{handle}: {snippet}"


def _insert_item(
    conn: sqlite3.Connection,
    *,
    source: str,
    source_id: str,
    url: str,
    domain: str,
    title: str | None,
    captured_at: int,
    metadata: dict,
    visible: bool = True,
    enrichment_source: str | None = "live_fallback",
) -> int:
    cur = conn.execute(
        "INSERT INTO bookmarks (source, source_id, captured_at, title, url, domain, "
        "  source_metadata, redacted_param_count, redacted_param_names, path_redacted, "
        "  content_visible, enrichment_source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL, 0, ?, ?)",
        (
            source,
            source_id,
            captured_at,
            title,
            url,
            domain,
            json.dumps(metadata, sort_keys=True, ensure_ascii=False),
            1 if visible else 0,
            enrichment_source,
        ),
    )
    return int(cur.lastrowid)


def _insert_api_content(
    conn: sqlite3.Connection,
    *,
    bookmark_id: int,
    item_ref: str,
    text: str,
    extractor: tuple[str, str],
    final_url: str | None,
    retrieved_at: int,
    published_at: int | None,
    observed_at: int | None,
) -> None:
    """As ``XPageWriter.write_content``: the live row + an x_api revision."""
    normalized = normalize_text(text)
    conn.execute(
        "INSERT INTO scrape_results (bookmark_id, pass, fetched_at, final_url, http_status, "
        "  scrape_status, extraction_quality, content_text) "
        "VALUES (?, 'live', ?, ?, NULL, 'ok', 'ok', ?)",
        (bookmark_id, retrieved_at, final_url, normalized),
    )
    record_revision(
        conn, bookmark_id=bookmark_id, item_ref=item_ref, text=normalized, coverage="full",
        retrieval_method="x_api", extractor_name=extractor[0], extractor_version=extractor[1],
        retrieved_at=retrieved_at, published_at=published_at, observed_at=observed_at,
    )


def _insert_scraped_content(
    conn: sqlite3.Connection, *, bookmark_id: int, item_ref: str, url: str, text: str
) -> None:
    """As `muninn scrape` for an ``x_link`` row whose live pass succeeded."""
    normalized = normalize_text(text)
    conn.execute(
        "INSERT INTO scrape_results (bookmark_id, pass, fetched_at, final_url, http_status, "
        "  scrape_status, extraction_quality, content_text, content_html) "
        "VALUES (?, 'live', ?, ?, 200, 'ok', 'ok', ?, ?)",
        (bookmark_id, LINK_FETCHED_AT, url, normalized, f"<html><body><p>{text}</p></body></html>"),
    )
    record_revision(
        conn, bookmark_id=bookmark_id, item_ref=item_ref, text=normalized, coverage="full",
        retrieval_method="live", scrape_pass="live", extractor_name=EXTRACTOR_NAME,
        extractor_version=EXTRACTOR_VERSION, http_status=200, final_locator=None,
        retrieved_at=LINK_FETCHED_AT, observed_at=None,
    )


def _link(
    conn: sqlite3.Connection,
    *,
    parent: int,
    child: int,
    child_ref: str,
    relation: str,
    position: int,
    resolution: str,
    entity_status: int | None = None,
) -> None:
    conn.execute(
        "INSERT INTO item_links (parent_bookmark_id, child_bookmark_id, parent_ref, "
        "  child_ref, relation, position, locator_resolution, entity_status, first_seen_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (parent, child, x_post_item_id(POST_ID), child_ref, relation, position,
         resolution, entity_status, OBSERVED_AT),
    )


def _post_meta(author: dict, published_at: int, post_id: str) -> dict:
    return {
        "kind": "x_post",
        "author": author,
        "published_at": published_at,
        "lang": "en",
        "conversation_id": post_id,
        "is_long_post": False,
        "media_keys": [],
        "possibly_sensitive": False,
    }


def seed_x_corpus(conn: sqlite3.Connection, *, hidden_at_ingest: bool = True) -> XCorpus:
    """Insert one bookmarked post with its quote, article and links."""
    text = post_text(hidden_at_ingest=hidden_at_ingest)
    post_url = f"https://x.com/{AUTHOR_A['username']}/status/{POST_ID}"
    post = _insert_item(
        conn,
        source="x",
        source_id=POST_ID,
        url=post_url,
        domain="x.com",
        title=post_title(AUTHOR_A["username"], text),
        captured_at=OBSERVED_AT,
        metadata={
            **_post_meta(AUTHOR_A, PUBLISHED_AT, POST_ID),
            "referenced": [{"type": "quoted", "id": QUOTED_POST_ID}],
            "article": {"id": ARTICLE_ID, "title": ARTICLE_TITLE, "has_body": True},
            "bookmarked": True,
            "first_sync_run_id": FIRST_SYNC_RUN,
            "last_sync_run_id": LAST_SYNC_RUN,
            "last_observed_at": LAST_OBSERVED_AT,
            "list_position": 0,
        },
    )
    _insert_api_content(
        conn, bookmark_id=post, item_ref=x_post_item_id(POST_ID), text=text,
        extractor=X_TEXT_EXTRACTOR, final_url=post_url, retrieved_at=RETRIEVED_AT,
        published_at=PUBLISHED_AT, observed_at=OBSERVED_AT,
    )

    quoted_ref = x_post_item_id(QUOTED_POST_ID)
    quoted_url = f"https://x.com/{AUTHOR_B['username']}/status/{QUOTED_POST_ID}"
    quoted = _insert_item(
        conn,
        source="x",
        source_id=QUOTED_POST_ID,
        url=quoted_url,
        domain="x.com",
        title=post_title(AUTHOR_B["username"], QUOTED_TEXT),
        captured_at=OBSERVED_AT,
        metadata={**_post_meta(AUTHOR_B, QUOTED_PUBLISHED_AT, QUOTED_POST_ID), "bookmarked": False},
    )
    _insert_api_content(
        conn, bookmark_id=quoted, item_ref=quoted_ref, text=QUOTED_TEXT,
        extractor=X_TEXT_EXTRACTOR, final_url=quoted_url, retrieved_at=RETRIEVED_AT,
        published_at=QUOTED_PUBLISHED_AT, observed_at=None,
    )

    article_ref = x_article_item_id(ARTICLE_ID)
    article = _insert_item(
        conn,
        source="x_article",
        source_id=ARTICLE_ID,
        url=f"https://x.com/i/article/{ARTICLE_ID}",
        domain="x.com",
        title=ARTICLE_TITLE,
        captured_at=OBSERVED_AT,
        metadata={
            "kind": "x_article",
            "author": AUTHOR_A,
            "published_at": ARTICLE_PUBLISHED_AT,
            "lang": "en",
            "via_post_id": POST_ID,
            "has_body": True,
        },
    )
    _insert_api_content(
        conn, bookmark_id=article, item_ref=article_ref, text=ARTICLE_TEXT,
        extractor=X_ARTICLE_EXTRACTOR, final_url=None, retrieved_at=RETRIEVED_AT,
        published_at=ARTICLE_PUBLISHED_AT, observed_at=None,
    )

    links = {}
    for key, url, domain, title, content in (
        ("web", WEB_URL, "example.com", "Synthetic notes on retrieval evaluation", WEB_TEXT),
        ("repo", REPO_URL, "github.com", "synthetic-org/synthetic-repo", REPO_TEXT),
        # The writer stores no title for a hidden link (and policy apply
        # clears one); this one models a row titled before it was hidden,
        # so consumers are checked against a stored title too.
        ("hidden", HIDDEN_URL, "intranet.example", "Private roadmap", None),
    ):
        item_id = link_item_id(url)
        meta: dict = {"item_id": item_id}
        if item_id.startswith("github:repo:"):
            owner, _, name = item_id.removeprefix("github:repo:").partition("/")
            meta.update(kind="github_repo", repository={"owner": owner, "name": name})
        else:
            meta["kind"] = "web_page"
        links[key] = _insert_item(
            conn,
            source="x_link",
            source_id=locator_source_id(url),
            url=url,
            domain=domain,
            title=title,
            captured_at=OBSERVED_AT,
            metadata=meta,
            visible=key != "hidden",
            enrichment_source=None if key == "hidden" else "live_fallback",
        )
        if content is not None:
            _insert_scraped_content(
                conn, bookmark_id=links[key], item_ref=item_id, url=url, text=content
            )

    _link(conn, parent=post, child=article, child_ref=article_ref, relation="article",
          position=0, resolution="api_reference")
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
