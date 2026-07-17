"""Dual-pass scrape orchestration.

Per-bookmark control flow (serial within a bookmark):

1. ``at_capture`` — Wayback snapshot ±365d of ``captured_at``.
2. ``recent_archive`` — most-recent Wayback snapshot, only if at_capture
   didn't yield ``ok``.
3. ``live`` — always run (for completeness/freshness comparison even when
   archives succeeded). Becomes the canonical content only as a last-resort
   fallback.

After all passes, ``bookmarks.enrichment_source`` is set per priority:
``at_capture > recent_archive > live_fallback > none`` (where the chosen
pass must have ``scrape_status='ok'``).

All inserts use ``INSERT … ON CONFLICT(bookmark_id, pass) DO UPDATE`` so
re-runs are idempotent (SC7) — row count stays constant across re-runs.

``scrape_all`` may interleave many bookmarks concurrently with a bounded
semaphore; politeness is enforced by the shared :class:`RateLimiter` (one
RPS per live origin, 0.5 RPS global to IA), not by the concurrency limit.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from typing import Iterable, Optional

from muninn.db import connect, transaction
from muninn.models import ScrapeResult
from muninn.scrape.at_capture import fetch_at_capture
from muninn.scrape.client import ScrapeClient
from muninn.scrape.live import fetch_live
from muninn.scrape.recent_archive import fetch_recent_archive

log = logging.getLogger(__name__)

# Enrichment-source priority (highest first).
ENRICHMENT_PRIORITY = ("at_capture", "recent_archive", "live_fallback")

# Map a successful pass name to the enrichment_source value persisted on the
# bookmark row. Note: ``live`` pass success is recorded as ``live_fallback``
# in enrichment_source (the spec wording: "live_fallback as canonical when
# both archive passes fail").
_PASS_TO_SOURCE = {
    "at_capture": "at_capture",
    "recent_archive": "recent_archive",
    "live": "live_fallback",
}


# ── Public API ───────────────────────────────────────────────────

async def scrape_one(
    conn: sqlite3.Connection,
    client: ScrapeClient,
    bookmark_row: dict,
) -> dict:
    """Run all passes for one bookmark, persist results, update
    ``enrichment_source``. Returns a small per-bookmark stats dict.
    """
    bookmark_id = int(bookmark_row["bookmark_id"])
    url = bookmark_row.get("url")
    captured_at = bookmark_row.get("captured_at")

    results: list[ScrapeResult] = []

    # Pass 1: at_capture
    at_capture_result = await fetch_at_capture(client, bookmark_id, url, captured_at)
    results.append(at_capture_result)

    # Pass 2: recent_archive (only if at_capture didn't succeed)
    if at_capture_result.scrape_status != "ok":
        recent_result = await fetch_recent_archive(client, bookmark_id, url)
        results.append(recent_result)

    # Pass 3: live (always — even if archives succeeded, we want a fresh
    # comparison snapshot, and stream-2's tests assert the row exists).
    live_result = await fetch_live(client, bookmark_id, url)
    results.append(live_result)

    # Persist + route enrichment_source atomically per bookmark.
    enrichment_source = _pick_enrichment_source(results)
    with transaction(conn):
        for r in results:
            _upsert_scrape_result(conn, r)
        _update_enrichment_source(conn, bookmark_id, enrichment_source)

    log.info(
        "bookmark %d → enrichment_source=%s (%d passes)",
        bookmark_id, enrichment_source, len(results),
    )
    return {
        "bookmark_id": bookmark_id,
        "enrichment_source": enrichment_source,
        "passes": {r.pass_: r.scrape_status for r in results},
    }


async def scrape_all(
    conn: sqlite3.Connection,
    *,
    client: Optional[ScrapeClient] = None,
    concurrency: int = 4,
    limit: Optional[int] = None,
) -> dict:
    """Run dual-pass scrape for every ``content_visible=1`` bookmark.

    ``limit``: scrape only the N most recently captured bookmarks
    (newest ``captured_at`` first) — the roadmap's ~100-bookmark
    smoke-test workflow. Default: all visible bookmarks.

    Returns aggregated stats: ``{processed, errors, by_source: {…}}``.
    """
    bookmarks = _list_visible_bookmarks(conn, limit=limit)
    log.info("scraping %d visible bookmarks", len(bookmarks))

    own_client = client is None
    if client is None:
        client = ScrapeClient()

    sem = asyncio.Semaphore(max(1, concurrency))
    stats = {"processed": 0, "errors": 0, "by_source": {}}

    async def _one(bm: dict) -> None:
        async with sem:
            try:
                result = await scrape_one(conn, client, bm)
                stats["processed"] += 1
                src = result["enrichment_source"]
                stats["by_source"][src] = stats["by_source"].get(src, 0) + 1
            except Exception:
                log.exception("scrape failed for bookmark_id=%s", bm.get("bookmark_id"))
                stats["errors"] += 1

    try:
        await asyncio.gather(*(_one(bm) for bm in bookmarks))
    finally:
        if own_client:
            await client.close()

    return stats


# ── Routing ──────────────────────────────────────────────────────

def _pick_enrichment_source(results: Iterable[ScrapeResult]) -> str:
    """Apply the priority ladder. Only ``scrape_status='ok'`` rows count."""
    by_pass = {r.pass_: r for r in results}
    for pass_name in ("at_capture", "recent_archive", "live"):
        r = by_pass.get(pass_name)
        if r is not None and r.scrape_status == "ok":
            return _PASS_TO_SOURCE[pass_name]
    return "none"


# ── DB helpers (kept here, not in muninn.db, since they're scrape-only) ──

_VISIBLE_BOOKMARKS_SQL = """
    SELECT bookmark_id, url, captured_at, content_visible
    FROM bookmarks
    WHERE content_visible = 1
    ORDER BY bookmark_id
"""

_VISIBLE_BOOKMARKS_NEWEST_SQL = """
    SELECT bookmark_id, url, captured_at, content_visible
    FROM bookmarks
    WHERE content_visible = 1
    ORDER BY captured_at DESC, bookmark_id DESC
    LIMIT ?
"""

_UPSERT_SCRAPE_SQL = """
    INSERT INTO scrape_results (
        bookmark_id, pass, fetched_at, target_timestamp, actual_snapshot_at,
        archive_url, final_url, http_status, scrape_status, extraction_quality,
        content_text, content_html, raw_html_path, error_detail
    ) VALUES (
        :bookmark_id, :pass, :fetched_at, :target_timestamp, :actual_snapshot_at,
        :archive_url, :final_url, :http_status, :scrape_status, :extraction_quality,
        :content_text, :content_html, :raw_html_path, :error_detail
    )
    ON CONFLICT(bookmark_id, pass) DO UPDATE SET
        fetched_at         = excluded.fetched_at,
        target_timestamp   = excluded.target_timestamp,
        actual_snapshot_at = excluded.actual_snapshot_at,
        archive_url        = excluded.archive_url,
        final_url          = excluded.final_url,
        http_status        = excluded.http_status,
        scrape_status      = excluded.scrape_status,
        extraction_quality = excluded.extraction_quality,
        content_text       = excluded.content_text,
        content_html       = excluded.content_html,
        raw_html_path      = excluded.raw_html_path,
        error_detail       = excluded.error_detail
"""


def _list_visible_bookmarks(
    conn: sqlite3.Connection, limit: Optional[int] = None
) -> list[dict]:
    if limit is not None:
        cur = conn.execute(_VISIBLE_BOOKMARKS_NEWEST_SQL, (limit,))
    else:
        cur = conn.execute(_VISIBLE_BOOKMARKS_SQL)
    return [dict(row) for row in cur.fetchall()]


def _upsert_scrape_result(conn: sqlite3.Connection, r: ScrapeResult) -> None:
    conn.execute(_UPSERT_SCRAPE_SQL, {
        "bookmark_id": r.bookmark_id,
        "pass": r.pass_,
        "fetched_at": r.fetched_at,
        "target_timestamp": r.target_timestamp,
        "actual_snapshot_at": r.actual_snapshot_at,
        "archive_url": r.archive_url,
        "final_url": r.final_url,
        "http_status": r.http_status,
        "scrape_status": r.scrape_status,
        "extraction_quality": r.extraction_quality,
        "content_text": r.content_text,
        "content_html": r.content_html,
        "raw_html_path": r.raw_html_path,
        "error_detail": r.error_detail,
    })


def _update_enrichment_source(
    conn: sqlite3.Connection, bookmark_id: int, source: str
) -> None:
    conn.execute(
        "UPDATE bookmarks SET enrichment_source = ? WHERE bookmark_id = ?",
        (source, bookmark_id),
    )


# ── Sync entry point (CLI / scripts) ─────────────────────────────

def run_scrape(
    db_path: Optional[str] = None,
    *,
    concurrency: int = 4,
    limit: Optional[int] = None,
) -> dict:
    """Synchronous wrapper for ``scrape_all``: opens a connection, runs,
    closes. For use from ``__main__`` or click commands."""
    conn = connect(db_path)
    try:
        return asyncio.run(scrape_all(conn, concurrency=concurrency, limit=limit))
    finally:
        conn.close()
