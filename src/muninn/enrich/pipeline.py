"""Bulk enrichment pipeline: eligible bookmarks → Haiku → enriched + Qdrant.

Eligibility (Decision 4 + Decision 5):

    A bookmark is enriched iff
        content_visible = 1
        AND enrichment_source IS NOT NULL
        AND enrichment_source != 'none'

The canonical scrape pass for each bookmark is the one whose
``scrape_results.pass`` matches the bookmark's ``enrichment_source``
column ('at_capture', 'recent_archive', 'live_fallback'). Bookmarks
gated to 'live_fallback' get their content from the ``live`` pass.

Idempotency: ``(enrichment_model, enrichment_prompt_version,
content_hash)`` — see :mod:`muninn.enrich.idempotency`. The triple is
computed *before* the API call so re-runs on unchanged content cost
zero tokens.

Qdrant writes are best-effort: a 5xx, a timeout, or an unreachable
server is logged and counted but does not fail the pipeline. The
``scripts/reconcile-vector-index.py`` script back-fills whatever's
missing on the next run.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from muninn.config import HAIKU_MODEL, PER_BOOKMARK_PROMPT_VERSION
from muninn.db import transaction
from muninn.enrich.haiku import (
    EnrichmentResult,
    build_embedding_text,
    enrich_bookmark,
)
from muninn.enrich.idempotency import (
    IdempotencyTriple,
    compute_content_hash,
    would_skip,
)
from muninn.vector.embed import text_to_vector
from muninn.vector.qdrant import (
    ensure_collection,
    get_client,
    get_collection_count,
    upsert_point,
)

if TYPE_CHECKING:  # pragma: no cover
    import anthropic
    from qdrant_client import QdrantClient

logger = logging.getLogger(__name__)

# The 'live_fallback' enrichment_source value points at the 'live' scrape pass.
ENRICHMENT_SOURCE_TO_PASS = {
    "at_capture": "at_capture",
    "recent_archive": "recent_archive",
    "live_fallback": "live",
}


# ── Eligibility query ────────────────────────────────────────────────
# Joins each bookmark to the scrape_results row whose ``pass`` matches
# the bookmark's chosen ``enrichment_source`` (Decision 4). 'live_fallback'
# is mapped to the 'live' pass via CASE so the join works directly.
_ELIGIBILITY_SQL = """
SELECT b.bookmark_id,
       b.title,
       b.url,
       b.enrichment_source,
       sr.content_text,
       sr.content_html,
       sr.final_url
FROM   bookmarks b
LEFT JOIN scrape_results sr
       ON sr.bookmark_id = b.bookmark_id
      AND sr.pass = CASE b.enrichment_source
                        WHEN 'live_fallback' THEN 'live'
                        ELSE b.enrichment_source
                    END
WHERE  b.content_visible   = 1
  AND  b.enrichment_source IS NOT NULL
  AND  b.enrichment_source != 'none'
ORDER  BY b.bookmark_id
"""


def get_eligible_bookmarks(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return every bookmark row + canonical scrape join eligible for enrichment."""
    return conn.execute(_ELIGIBILITY_SQL).fetchall()


# ── Stats ────────────────────────────────────────────────────────────


@dataclass
class EnrichmentStats:
    """Per-run counters + post-run verification snapshot."""

    total: int = 0
    enriched: int = 0
    skipped_idempotent: int = 0
    skipped_no_content: int = 0
    api_calls: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    qdrant_writes: int = 0
    qdrant_skipped: int = 0
    errors: int = 0
    # Verification snapshot
    enriched_row_count: int = 0
    eligible_count: int = 0
    qdrant_point_count: int = 0
    all_eligible_enriched: bool = False
    qdrant_counts_match: bool = False
    missing_bookmark_ids: list[int] = field(default_factory=list)

    @property
    def cache_hit_rate(self) -> float:
        """Cache reads / total API calls in [0.0, 1.0]."""
        if self.api_calls == 0:
            return 0.0
        return self.cache_hits / self.api_calls

    def summary(self) -> dict:
        return {
            "total_eligible": self.total,
            "enriched": self.enriched,
            "skipped_idempotent": self.skipped_idempotent,
            "skipped_no_content": self.skipped_no_content,
            "api_calls": self.api_calls,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "cache_hit_rate": round(self.cache_hit_rate, 4),
            "qdrant_writes": self.qdrant_writes,
            "qdrant_skipped": self.qdrant_skipped,
            "errors": self.errors,
            "verification": {
                "enriched_row_count": self.enriched_row_count,
                "eligible_count": self.eligible_count,
                "all_eligible_enriched": self.all_eligible_enriched,
                "qdrant_point_count": self.qdrant_point_count,
                "qdrant_counts_match": self.qdrant_counts_match,
            },
        }


# ── Persistence ──────────────────────────────────────────────────────


def _upsert_enriched_row(
    conn: sqlite3.Connection,
    *,
    bookmark_id: int,
    title: str,
    content_text: str,
    result: EnrichmentResult,
    content_hash: str,
    enriched_at: int,
    prompt_version: str = PER_BOOKMARK_PROMPT_VERSION,
) -> None:
    """Write the ``enriched`` row + sync the contentless ``fts_bookmarks`` index.

    The schema constraint is NOT NULL on
    (enrichment_model, enrichment_prompt_version, content_hash, enriched_at)
    — every code path here populates them. ``key_quotes`` is left NULL;
    the deep-pass enrichment writes it later.
    """
    word_count = len(content_text.split()) if content_text else 0
    tags_json = json.dumps(result.tags)
    entities_json = json.dumps(result.entities)

    with transaction(conn):
        conn.execute(
            """
            INSERT INTO enriched (
                bookmark_id, summary, tags, entities,
                content_type, language, word_count,
                enrichment_model, enrichment_prompt_version, content_hash,
                enriched_at, deep_pass_requested, key_quotes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL)
            ON CONFLICT(bookmark_id) DO UPDATE SET
                summary                   = excluded.summary,
                tags                      = excluded.tags,
                entities                  = excluded.entities,
                content_type              = excluded.content_type,
                language                  = excluded.language,
                word_count                = excluded.word_count,
                enrichment_model          = excluded.enrichment_model,
                enrichment_prompt_version = excluded.enrichment_prompt_version,
                content_hash              = excluded.content_hash,
                enriched_at               = excluded.enriched_at
            """,
            (
                bookmark_id,
                result.summary,
                tags_json,
                entities_json,
                result.content_type,
                result.language,
                word_count,
                HAIKU_MODEL,
                prompt_version,
                content_hash,
                enriched_at,
            ),
        )

        # Contentless FTS5: rowid == bookmark_id. Application-layer sync.
        conn.execute(
            "DELETE FROM fts_bookmarks WHERE rowid = ?",
            (bookmark_id,),
        )
        conn.execute(
            """
            INSERT INTO fts_bookmarks(rowid, title, summary, content_text, tags)
            VALUES (?, ?, ?, ?, ?)
            """,
            (bookmark_id, title or "", result.summary, content_text or "", tags_json),
        )


# ── Pipeline ─────────────────────────────────────────────────────────


def enrich_all(
    conn: sqlite3.Connection,
    client: "anthropic.Anthropic | None" = None,
    *,
    qdrant: "QdrantClient | None | str" = "auto",
    dry_run: bool = False,
    now: int | None = None,
    force: bool = False,
    prompt_version: str | None = None,
) -> EnrichmentStats:
    """Enrich every eligible bookmark; write ``enriched`` rows + Qdrant points.

    ``qdrant``: pass a client to inject one (tests), ``None`` to skip Qdrant
    writes entirely, or leave the sentinel ``"auto"`` to call
    ``get_client()`` and probe ``QDRANT_URL``.

    ``force``: re-enrich even when the idempotency triple matches (burns
    tokens; for prompt/model debugging).
    ``prompt_version``: override ``PER_BOOKMARK_PROMPT_VERSION`` — becomes
    part of the idempotency triple, so a new version re-enriches everything.
    """
    import time

    stats = EnrichmentStats()
    now_ts = now if now is not None else int(time.time())
    pv = prompt_version or PER_BOOKMARK_PROMPT_VERSION

    if client is None and not dry_run:
        import anthropic

        client = anthropic.Anthropic()

    # Resolve Qdrant client.
    if qdrant == "auto":
        qdrant = get_client()
    if qdrant is not None:
        try:
            ensure_collection(qdrant)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not ensure Qdrant collection: %s", exc)
            qdrant = None

    bookmarks = get_eligible_bookmarks(conn)
    stats.total = len(bookmarks)
    logger.info("Found %d eligible bookmarks for enrichment", stats.total)

    for idx, bm in enumerate(bookmarks, 1):
        bookmark_id = bm["bookmark_id"]
        title = bm["title"] or ""
        content_text = bm["content_text"] or ""

        # Hash the canonical scrape text — even if empty, hash is well-defined.
        content_hash = compute_content_hash(content_text)
        candidate = IdempotencyTriple(
            enrichment_model=HAIKU_MODEL,
            enrichment_prompt_version=pv,
            content_hash=content_hash,
        )

        if not force and would_skip(conn, bookmark_id, candidate):
            stats.skipped_idempotent += 1
            logger.debug("Skipping bookmark %d (idempotent)", bookmark_id)
            continue

        if not content_text.strip():
            # No canonical scrape content to enrich. Don't burn tokens on
            # a title-only call; reconcile/scrape can recover later.
            stats.skipped_no_content += 1
            logger.debug("Skipping bookmark %d (no content)", bookmark_id)
            continue

        if dry_run:
            logger.info("DRY RUN: would enrich bookmark %d", bookmark_id)
            continue

        # Haiku call.
        try:
            result = enrich_bookmark(title, content_text, client)
            stats.api_calls += 1
            if result.cache_hit:
                stats.cache_hits += 1
            else:
                stats.cache_misses += 1
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to enrich bookmark %d: %s", bookmark_id, exc)
            stats.errors += 1
            continue

        # Persist the enriched row + FTS sync.
        try:
            _upsert_enriched_row(
                conn,
                bookmark_id=bookmark_id,
                title=title,
                content_text=content_text,
                result=result,
                content_hash=content_hash,
                enriched_at=now_ts,
                prompt_version=pv,
            )
            stats.enriched += 1
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Failed to write enrichment for bookmark %d: %s", bookmark_id, exc
            )
            stats.errors += 1
            continue

        # Qdrant write — best-effort; reconcile catches up on failures.
        if qdrant is not None:
            embedding_text = build_embedding_text(title, result.summary, result.tags)
            vector = text_to_vector(embedding_text)
            payload = {
                "bookmark_id": bookmark_id,
                "title": title,
                "summary": result.summary,
                "tags": result.tags,
                "content_type": result.content_type,
                "language": result.language,
            }
            if upsert_point(qdrant, bookmark_id, vector, payload):
                stats.qdrant_writes += 1
            else:
                stats.qdrant_skipped += 1
        else:
            stats.qdrant_skipped += 1

        if stats.total >= 50 and idx % 50 == 0:
            logger.info(
                "Progress: %d/%d (enriched=%d, skipped=%d, errors=%d)",
                idx,
                stats.total,
                stats.enriched,
                stats.skipped_idempotent,
                stats.errors,
            )

    _verify(conn, qdrant, stats)

    logger.info("Enrichment complete: %s", stats.summary())
    if stats.api_calls > 0:
        rate_pct = stats.cache_hit_rate * 100
        logger.info(
            "Cache hit rate: %.1f%% (%d hits / %d calls)",
            rate_pct,
            stats.cache_hits,
            stats.api_calls,
        )
        # PRD: ≥80% expected on bulk passes after the first ~100 calls.
        if stats.api_calls >= 100 and rate_pct < 80:
            logger.warning(
                "Cache hit rate %.1f%% is below the 80%% target on bulk pass",
                rate_pct,
            )
    return stats


def _verify(
    conn: sqlite3.Connection,
    qdrant: "QdrantClient | None",
    stats: EnrichmentStats,
) -> None:
    """Post-run check: every eligible bookmark has a row, Qdrant counts match."""
    eligible = get_eligible_bookmarks(conn)
    stats.eligible_count = len(eligible)
    enriched_count = conn.execute("SELECT COUNT(*) FROM enriched").fetchone()[0]
    stats.enriched_row_count = enriched_count

    eligible_ids = {bm["bookmark_id"] for bm in eligible}
    enriched_ids = {
        row["bookmark_id"]
        for row in conn.execute("SELECT bookmark_id FROM enriched").fetchall()
    }
    missing = sorted(eligible_ids - enriched_ids)
    stats.missing_bookmark_ids = missing
    stats.all_eligible_enriched = not missing
    if missing:
        logger.warning(
            "Verification: %d eligible bookmarks missing enrichment: %s",
            len(missing),
            missing[:10],
        )
    else:
        logger.info(
            "Verification: all %d eligible bookmarks enriched", stats.eligible_count
        )

    if qdrant is not None:
        try:
            qcount = get_collection_count(qdrant)
            stats.qdrant_point_count = qcount
            stats.qdrant_counts_match = qcount == enriched_count
            if stats.qdrant_counts_match:
                logger.info(
                    "Verification: Qdrant points (%d) == enriched rows (%d)",
                    qcount,
                    enriched_count,
                )
            else:
                logger.warning(
                    "Verification: Qdrant points (%d) != enriched rows (%d)",
                    qcount,
                    enriched_count,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Verification: could not query Qdrant count: %s", exc)
    else:
        logger.info(
            "Verification: Qdrant unavailable — run scripts/reconcile-vector-index.py "
            "to backfill once it returns."
        )
