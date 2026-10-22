"""Append-only content revisions (``scrape_history``), shared by every writer.

One row per distinct ``(item_ref, content_sha256, coverage)``: a re-fetch of
unchanged content hits the 002 unique key and adds nothing. The digest is
sha256 over the ``muninn-text/1`` normalized text. Captures without readable
content (``blocked``, ``deleted``, ``empty``) carry the empty-string digest,
``char_count`` 0 and no text (contract §4.4).

Writers: ``muninn ingest x`` (``retrieval_method='x_api'``) and
``muninn scrape`` (``live`` / ``archive``). Stdlib only.
"""

from __future__ import annotations

import sqlite3
from typing import Literal

from muninn.consumers.packets.canonical import (
    TEXT_NORMALIZATION,
    content_digest,
    new_ulid,
    normalize_text,
)

Coverage = Literal["full", "partial", "blocked", "deleted", "empty"]
RetrievalMethod = Literal["x_api", "live", "archive"]
NO_CONTENT: tuple[str, ...] = ("blocked", "deleted", "empty")

_INSERT_SQL = """
    INSERT INTO scrape_history (
        revision_ulid, bookmark_id, item_ref, content_sha256, normalization,
        char_count, content_text, coverage, retrieval_method, scrape_pass,
        extractor_name, extractor_version, http_status, final_locator,
        archive_snapshot_at, published_at, retrieved_at, observed_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT (item_ref, content_sha256, coverage) DO NOTHING
"""


def record_revision(
    conn: sqlite3.Connection,
    *,
    bookmark_id: int | None,
    item_ref: str,
    text: str | None,
    coverage: Coverage,
    retrieval_method: RetrievalMethod,
    extractor_name: str,
    extractor_version: str,
    retrieved_at: int,
    scrape_pass: str | None = None,
    http_status: int | None = None,
    final_locator: str | None = None,
    archive_snapshot_at: int | None = None,
    published_at: int | None = None,
    observed_at: int | None = None,
) -> str | None:
    """Append a revision unless an identical one exists.

    A ``full``/``partial`` capture whose normalized text is empty is
    recorded as ``empty``. Returns the new ``revision_ulid``, or None when
    the revision already existed. Runs inside the caller's transaction.
    """
    if coverage in NO_CONTENT:
        normalized = ""
    else:
        normalized = normalize_text(text)
        if not normalized:
            coverage = "empty"
    digest = content_digest(normalized)
    ulid = new_ulid(int(retrieved_at) * 1000)
    cur = conn.execute(
        _INSERT_SQL,
        (
            ulid,
            bookmark_id,
            item_ref,
            digest,
            TEXT_NORMALIZATION,
            len(normalized),
            normalized if coverage not in NO_CONTENT else None,
            coverage,
            retrieval_method,
            None if retrieval_method == "x_api" else scrape_pass,
            extractor_name,
            extractor_version,
            http_status,
            final_locator,
            archive_snapshot_at if retrieval_method == "archive" else None,
            published_at,
            int(retrieved_at),
            observed_at,
        ),
    )
    return ulid if cur.rowcount == 1 else None


__all__ = ["NO_CONTENT", "Coverage", "RetrievalMethod", "record_revision"]
