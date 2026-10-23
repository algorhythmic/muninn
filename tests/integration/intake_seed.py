"""Seed X Research Intake rows directly, following contract §10.1.

ALL DATA IS SYNTHETIC (invented IDs, handles, reserved example hosts).

The X ingest writer (RI-04) is built separately; until it lands, the
suitability and packet tests seed the rows it will write. Every row shape
here follows ``docs/contracts/MUNINN-RESEARCH-INTAKE.md`` §10.1 so the
helper can be cross-checked against the real writer:

* post: ``bookmarks(source='x', source_id=<post id>)``, ``url`` the
  sanitized post URL, ``captured_at`` = first sync that saw it,
  ``enrichment_source='live_fallback'``, structured metadata (never the
  text) in ``source_metadata``;
* post text: ``scrape_results(pass='live')`` (current) plus one
  ``scrape_history`` row per distinct text, ``retrieval_method='x_api'``,
  no ``scrape_pass``; deleted/blocked posts get an empty-content revision
  (coverage ``deleted``/``blocked``) and ``scrape_status`` ``failed`` /
  ``auth_required``;
* linked page or repo: ``bookmarks(source='x_link', source_id=sha256(locator))``
  where the locator is ``sanitize_url`` of ``unwound_url``, then
  ``expanded_url``, then the t.co ``url``; ``item_links(relation='links_to',
  position=i)``; its live fetch in ``scrape_results(pass='live')`` and
  ``scrape_history(retrieval_method='live', scrape_pass='live')``.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from urllib.parse import urlsplit

from muninn.consumers.packets import (
    content_digest,
    link_item_id,
    locator_source_id,
    new_ulid,
    normalize_text,
    x_post_item_id,
)
from muninn.sanitize import sanitize_url

X_EXTRACTOR = ("muninn.ingest.x", "0.1.0")
WEB_EXTRACTOR = ("muninn.scrape.extract", "selectolax-0.4.13")
_STATUS_FOR = {"full": "ok", "partial": "partial", "blocked": "auth_required", "deleted": "failed", "empty": "ok"}


@dataclass
class Seeded:
    bookmark_id: int
    item_id: str


def _ulid(ts: int) -> str:
    return new_ulid(ts * 1000)


def add_revision(
    conn: sqlite3.Connection,
    item: Seeded,
    text: str,
    *,
    coverage: str = "full",
    retrieval_method: str,
    retrieved_at: int,
    published_at: int | None = None,
    observed_at: int | None = None,
    http_status: int | None = None,
    final_locator: str | None = None,
) -> int:
    """One content revision and the matching current ``scrape_results`` row.

    Unchanged content (same item, digest and coverage) adds no revision,
    exactly as the contract requires; the current row is upserted.
    """
    norm = normalize_text(text) if coverage in ("full", "partial") else ""
    digest = content_digest(norm)
    extractor = X_EXTRACTOR if retrieval_method == "x_api" else WEB_EXTRACTOR
    conn.execute(
        """
        INSERT OR IGNORE INTO scrape_history (
            revision_ulid, bookmark_id, item_ref, content_sha256, normalization, char_count,
            content_text, coverage, retrieval_method, scrape_pass, extractor_name,
            extractor_version, http_status, final_locator, published_at, retrieved_at, observed_at
        ) VALUES (?, ?, ?, ?, 'muninn-text/1', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _ulid(retrieved_at), item.bookmark_id, item.item_id, digest, len(norm),
            norm or None, coverage, retrieval_method,
            None if retrieval_method == "x_api" else "live",
            extractor[0], extractor[1], http_status, final_locator,
            published_at, retrieved_at, observed_at,
        ),
    )
    conn.execute(
        """
        INSERT INTO scrape_results (
            bookmark_id, pass, fetched_at, http_status, scrape_status,
            extraction_quality, content_text, final_url
        ) VALUES (?, 'live', ?, ?, ?, ?, ?, ?)
        ON CONFLICT (bookmark_id, pass) DO UPDATE SET
            fetched_at = excluded.fetched_at, http_status = excluded.http_status,
            scrape_status = excluded.scrape_status, extraction_quality = excluded.extraction_quality,
            content_text = excluded.content_text, final_url = excluded.final_url
        """,
        (
            item.bookmark_id, retrieved_at, http_status, _STATUS_FOR[coverage],
            "ok" if coverage == "full" else ("partial" if coverage == "partial" else None),
            norm or None, final_locator,
        ),
    )
    conn.commit()
    return conn.execute(
        "SELECT scrape_history_id FROM scrape_history WHERE item_ref = ? AND content_sha256 = ? AND coverage = ?",
        (item.item_id, digest, coverage),
    ).fetchone()[0]


def seed_x_post(
    conn: sqlite3.Connection,
    post_id: str,
    text: str,
    *,
    captured_at: int,
    published_at: int,
    handle: str = "synth_author_a",
    author_id: str = "1900000000000000901",
    coverage: str = "full",
    content_visible: int = 1,
    extra_metadata: dict | None = None,
) -> Seeded:
    metadata = {
        "author": {"id": author_id, "username": handle, "name": "Synthetic Author"},
        "published_at": published_at,
        "lang": "en",
        "conversation_id": post_id,
        "possibly_sensitive": False,
        "is_long_post": False,
        "last_observed_at": captured_at,
        **(extra_metadata or {}),
    }
    url = sanitize_url(f"https://x.com/{handle}/status/{post_id}").sanitized_url
    cur = conn.execute(
        """
        INSERT INTO bookmarks (
            source, source_id, captured_at, title, url, domain, source_metadata,
            content_visible, enrichment_source
        ) VALUES ('x', ?, ?, NULL, ?, 'x.com', ?, ?, 'live_fallback')
        """,
        (post_id, captured_at, url, json.dumps(metadata), content_visible),
    )
    item = Seeded(cur.lastrowid, x_post_item_id(post_id))
    add_revision(
        conn, item, text, coverage=coverage, retrieval_method="x_api",
        retrieved_at=captured_at, published_at=published_at, observed_at=captured_at,
    )
    return item


def seed_link(
    conn: sqlite3.Connection,
    parent: Seeded,
    *,
    position: int,
    unwound_url: str | None = None,
    expanded_url: str | None = None,
    short_url: str = "https://t.co/Synth0000",
    text: str = "",
    coverage: str = "full",
    title: str | None = None,
    retrieved_at: int,
    http_status: int | None = 200,
    content_visible: int = 1,
    entity_status: int | None = None,
) -> Seeded:
    if unwound_url:
        raw, resolution = unwound_url, "unwound"
    elif expanded_url:
        raw, resolution = expanded_url, "expanded"
    else:
        raw, resolution = short_url, "short"
    res = sanitize_url(raw)
    locator = res.sanitized_url
    assert locator, raw
    child_ref = link_item_id(locator)
    cur = conn.execute(
        """
        INSERT INTO bookmarks (
            source, source_id, captured_at, title, url, domain, redacted_param_count,
            redacted_param_names, path_redacted, content_visible, enrichment_source
        ) VALUES ('x_link', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'live_fallback')
        """,
        (
            locator_source_id(locator), retrieved_at, title, locator, urlsplit(locator).hostname,
            res.redacted_param_count, json.dumps(res.redacted_param_names), int(res.path_redacted),
            content_visible,
        ),
    )
    child = Seeded(cur.lastrowid, child_ref)
    conn.execute(
        """
        INSERT INTO item_links (
            parent_bookmark_id, child_bookmark_id, parent_ref, child_ref, relation, position,
            locator_resolution, entity_status, first_seen_at
        ) VALUES (?, ?, ?, ?, 'links_to', ?, ?, ?, ?)
        """,
        (parent.bookmark_id, child.bookmark_id, parent.item_id, child_ref, position, resolution,
         entity_status, retrieved_at),
    )
    add_revision(
        conn, child, text, coverage=coverage, retrieval_method="live", retrieved_at=retrieved_at,
        observed_at=None, http_status=http_status,
    )
    return child
