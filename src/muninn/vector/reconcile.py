"""Reconcile the Qdrant index with the canonical ``enriched`` rows.

SQLite is canonical and Qdrant is derived (ADR-008), so this can always
bring the index back in line. One pass:

1. Read every ``enriched`` row with its bookmark's title and visibility.
2. Scroll every point (with payload) out of the collection.
3. Upsert a fresh point for each visible row that is
   - **missing** from Qdrant (a failed or skipped write at enrich time), or
   - **stale**: its payload no longer equals what the row would produce
     now. A deep pass rewrites ``enriched.summary``/``tags`` without
     touching Qdrant, and a re-enrichment whose Qdrant write failed
     leaves the old point; both are caught here, with no marker column.
4. Delete points whose bookmark is hidden (``content_visible = 0``), so a
   row hidden after it was indexed stops carrying its title and summary
   in the payload. Search hydration already filters such hits; this
   removes the data itself.

A visible row that links to a hidden item (``item_links``) is indexed
**masked**, the same way the vault and MCP show it: a locator of the hidden
item in its title becomes ``[hidden link]``, and when its enrichment
(summary, key quotes or entities) quotes such a locator the summary is
withheld from the payload and the embedding text, because it was written
before the link was hidden and can paraphrase the hidden page. Such a row's
expected payload differs from an unmasked point's, so a point that still
carries the locator is stale and is re-upserted with the masked payload.
Re-upserting (rather than deleting the point until re-enrichment) keeps the
row findable by its title and tags while carrying nothing hidden.

Points whose bookmark has no ``enriched`` row are reported as
``extra_in_qdrant`` and left alone.

The embedding text is ``(title, summary, tags)``, the same as
:func:`muninn.enrich.pipeline.enrich_all` writes, and the payload has the
same keys, so a point written at enrich time is never seen as stale.
Idempotent: a second run is a no-op.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any

from muninn.consumers.vault.x_pages import enrichment_quotes_hidden
from muninn.db import connect
from muninn.enrich.haiku import build_embedding_text
from muninn.intake_records import hidden_child_locators, mask_title
from muninn.vector.embed import embed_document
from muninn.vector.qdrant import (
    delete_points,
    ensure_collection,
    get_client,
    get_point_payloads,
    upsert_points_batch,
)

if TYPE_CHECKING:  # pragma: no cover
    from qdrant_client import QdrantClient

logger = logging.getLogger(__name__)


def _tags(raw: str | None) -> list:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def point_payload(
    bookmark_id: int,
    *,
    title: str,
    summary: str,
    tags: list,
    content_type: str | None,
    language: str | None,
) -> dict[str, Any]:
    """The payload stored with each point (same keys as the enrich pipeline)."""
    return {
        "bookmark_id": bookmark_id,
        "title": title,
        "summary": summary,
        "tags": tags,
        "content_type": content_type,
        "language": language,
    }


def get_all_enriched_rows(conn: sqlite3.Connection) -> dict[int, dict]:
    """``{bookmark_id: {title, summary, tags, content_type, language, visible}}``.

    ``tags`` stays the raw JSON text. ``title`` and ``summary`` are ``""``
    when NULL (an X post has no title), matching the enrich pipeline. For a
    row that links to hidden items, ``title`` is masked and ``summary`` is
    ``""`` when its enrichment quotes a hidden locator (see the module
    docstring).
    """
    rows = conn.execute(
        """
        SELECT e.bookmark_id,
               b.title,
               b.content_visible,
               e.summary,
               e.tags,
               e.entities,
               e.key_quotes,
               e.content_type,
               e.language
        FROM   enriched e
        JOIN   bookmarks b ON b.bookmark_id = e.bookmark_id
        """
    ).fetchall()
    hidden = hidden_child_locators(conn, [r["bookmark_id"] for r in rows if r["content_visible"]])
    out: dict[int, dict] = {}
    for r in rows:
        title, summary = r["title"] or "", r["summary"] or ""
        urls = hidden.get(r["bookmark_id"], set())
        if urls:
            title = mask_title(title, urls)
            if enrichment_quotes_hidden(urls, summary, r["key_quotes"], r["entities"]):
                summary = ""
        out[r["bookmark_id"]] = {
            "title": title,
            "summary": summary,
            "tags": r["tags"],
            "content_type": r["content_type"],
            "language": r["language"],
            "visible": bool(r["content_visible"]),
        }
    return out


def _expected_payload(bookmark_id: int, row: dict) -> dict[str, Any]:
    return point_payload(
        bookmark_id,
        title=row["title"],
        summary=row["summary"],
        tags=_tags(row["tags"]),
        content_type=row["content_type"],
        language=row["language"],
    )


def reconcile(
    db_path: str | Path | None = None,
    *,
    client: "QdrantClient | None | str" = "auto",
    dry_run: bool = False,
    batch_size: int = 100,
) -> dict:
    """Backfill missing points, refresh stale ones, drop hidden ones.

    ``client``: inject a Qdrant client (tests), or leave ``"auto"`` to probe
    ``QDRANT_URL``. Returns a stats dict; ``error`` is set when Qdrant is
    unreachable.
    """
    conn = connect(Path(db_path)) if db_path is not None else connect()
    try:
        enriched = get_all_enriched_rows(conn)
    finally:
        conn.close()  # Qdrant calls don't touch SQL
    enriched_ids = set(enriched)
    visible_ids = {bid for bid, row in enriched.items() if row["visible"]}

    if client == "auto":
        client = get_client()
    if client is None:
        logger.error("Qdrant unavailable — cannot reconcile")
        return {"error": "qdrant_unavailable", "enriched_count": len(enriched_ids)}

    ensure_collection(client)
    payloads = get_point_payloads(client)
    qdrant_ids = set(payloads)

    missing_ids = visible_ids - qdrant_ids
    stale_ids = {
        bid
        for bid in visible_ids & qdrant_ids
        if payloads[bid] != _expected_payload(bid, enriched[bid])
    }
    hidden_ids = (enriched_ids - visible_ids) & qdrant_ids
    extra_ids = qdrant_ids - enriched_ids

    stats = {
        "enriched_count": len(enriched_ids),
        "qdrant_count": len(qdrant_ids),
        "missing_from_qdrant": len(missing_ids),
        "stale_in_qdrant": len(stale_ids),
        "hidden_in_qdrant": len(hidden_ids),
        "extra_in_qdrant": len(extra_ids),
        "backfilled": 0,
        "refreshed": 0,
        "removed_hidden": 0,
        "errors": 0,
    }
    logger.info(
        "Reconciliation: %d enriched, %d in Qdrant, %d missing, %d stale, "
        "%d hidden, %d extra",
        len(enriched_ids),
        len(qdrant_ids),
        len(missing_ids),
        len(stale_ids),
        len(hidden_ids),
        len(extra_ids),
    )
    if dry_run:
        logger.info(
            "DRY RUN — would upsert %d points and delete %d",
            len(missing_ids) + len(stale_ids),
            len(hidden_ids),
        )
        return stats

    batch: list[tuple[int, list[float], dict]] = []

    def _flush() -> None:
        written = upsert_points_batch(client, batch)
        refreshed = sum(1 for bid, _, _ in batch if bid in stale_ids) if written else 0
        stats["refreshed"] += refreshed
        stats["backfilled"] += written - refreshed
        stats["errors"] += len(batch) - written
        batch.clear()

    for bid in sorted(missing_ids | stale_ids):
        row = enriched[bid]
        payload = _expected_payload(bid, row)
        text = build_embedding_text(row["title"], row["summary"], payload["tags"])
        batch.append((bid, embed_document(text), payload))
        if len(batch) >= batch_size:
            _flush()
    if batch:
        _flush()

    if hidden_ids:
        if delete_points(client, sorted(hidden_ids)):
            stats["removed_hidden"] = len(hidden_ids)
        else:
            stats["errors"] += len(hidden_ids)

    logger.info("Reconciliation complete: %s", stats)
    return stats


__all__ = ["get_all_enriched_rows", "point_payload", "reconcile"]
