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

Alongside each upsert, every fetch that observed a content state appends a
``scrape_history`` revision (contract muninn-research-intake §4.4): digest
over the normalized extracted text, extractor name and version,
``retrieval_method`` ``live`` or ``archive`` (archive revisions are
historical), and coverage. Unchanged content adds nothing (002 unique key).
Transport failures and missing archives are operational states, not
content, and add no revision.

X rows (``source`` ``x`` / ``x_article``) are never scraped: their content
comes from the X API, and a live fetch would overwrite it. Linked items
(``x_link``) are scraped like any bookmark, but their ``enrichment_source``
prefers the live pass, because research selection uses current captures
(RI-01 §5); the browser lane keeps at_capture-first.

``scrape_all`` may interleave many bookmarks concurrently with a bounded
semaphore; politeness is enforced by the shared :class:`RateLimiter` (one
RPS per live origin, 0.5 RPS global to IA), not by the concurrency limit.

Domain policy also applies to where a fetch *ends*
(:func:`muninn.ingest.policy.row_hidden`). The live pass never follows a
redirect to a hidden locator; a fetch that nonetheless ended on one (a
Wayback snapshot of a redirect target, a cached response from before a
policy change) keeps no content. Either way the hidden locator is kept in
``final_url`` and the row is hidden (``content_visible = 0``) in the same
transaction, exactly as if its own URL were hidden; once the run is over,
:func:`muninn.ingest.policy.apply_policy` propagates that (masks in
parent posts, titles, keyword index).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from typing import Iterable, Optional

from muninn.db import connect, transaction
from muninn.ingest.policy import apply_policy, locator_hidden, row_visible_flag
from muninn.intake_records import API_CONTENT_SOURCES, SOURCE_X_LINK, item_ref_for, row_metadata
from muninn.models import ScrapeResult
from muninn.revisions import record_revision
from muninn.sanitize import sanitize_url
from muninn.scrape.at_capture import fetch_at_capture
from muninn.scrape.client import ScrapeClient
from muninn.scrape.extract import EXTRACTOR_NAME, EXTRACTOR_VERSION
from muninn.scrape.live import fetch_live
from muninn.scrape.domain_policy import DomainPolicy, load_domain_policy
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
    *,
    policy: Optional[DomainPolicy] = None,
) -> dict:
    """Run all passes for one bookmark, persist results, update
    ``enrichment_source``. Returns a small per-bookmark stats dict
    (``hidden``: the row is now hidden because a fetch ended on a
    locator ``policy`` hides; no policy, no such check).
    """
    bookmark_id = int(bookmark_row["bookmark_id"])
    url = bookmark_row.get("url")
    captured_at = bookmark_row.get("captured_at")
    stop_at = (lambda u: locator_hidden(policy, u)) if policy is not None else None

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
    live_result = await fetch_live(client, bookmark_id, url, stop_at=stop_at)
    results.append(live_result)

    if stop_at is not None:
        results = [_withhold_hidden_fetch(r, stop_at) for r in results]

    # Persist + route enrichment_source atomically per bookmark.
    identity = _identity(conn, bookmark_row)
    enrichment_source = _pick_enrichment_source(
        results, prefer_live=identity["source"] == SOURCE_X_LINK
    )
    with transaction(conn):
        for r in results:
            _upsert_scrape_result(conn, r)
        # Live first: when a live and an archive capture have identical text,
        # the one revision the unique key keeps is the current (live) one.
        for r in sorted(results, key=lambda r: r.pass_ != "live"):
            _record_history(conn, r, identity)
        _update_enrichment_source(conn, bookmark_id, enrichment_source)
        hidden = policy is not None and not row_visible_flag(
            conn, policy, bookmark_id, url, identity["source"]
        )
        if hidden:
            conn.execute(
                "UPDATE bookmarks SET content_visible = 0 WHERE bookmark_id = ?", (bookmark_id,)
            )
            log.info("bookmark %d hidden: a fetch ended on a policy-hidden locator", bookmark_id)

    log.info(
        "bookmark %d → enrichment_source=%s (%d passes)",
        bookmark_id, enrichment_source, len(results),
    )
    return {
        "bookmark_id": bookmark_id,
        "enrichment_source": enrichment_source,
        "passes": {r.pass_: r.scrape_status for r in results},
        "hidden": hidden,
    }


def _withhold_hidden_fetch(r: ScrapeResult, hidden) -> ScrapeResult:
    """A fetch that ended on a hidden locator keeps none of its content
    (its ``final_url`` stays, so the row is hidden by it)."""
    if not r.final_url or not hidden(_sanitized_locator(r.final_url) or r.final_url):
        return r
    if r.raw_html_path:
        try:
            os.unlink(r.raw_html_path)
        except OSError:
            pass
    return r.model_copy(update={
        "scrape_status": "failed",
        "extraction_quality": None,
        "content_text": None,
        "content_html": None,
        "raw_html_path": None,
        "error_detail": r.error_detail if r.scrape_status == "failed"
        else "fetch ended on a locator hidden by domain policy",
    })


async def scrape_all(
    conn: sqlite3.Connection,
    *,
    client: Optional[ScrapeClient] = None,
    concurrency: int = 4,
    limit: Optional[int] = None,
    sources: Optional[Iterable[str]] = None,
    policy: Optional[DomainPolicy] = None,
) -> dict:
    """Run dual-pass scrape for every ``content_visible=1`` bookmark.

    ``limit``: scrape only the N most recently captured bookmarks
    (newest ``captured_at`` first) — the roadmap's ~100-bookmark
    smoke-test workflow. Default: all visible bookmarks.
    ``sources``: only rows whose ``bookmarks.source`` is listed. X post and
    article rows are always skipped (their content comes from the X API).
    ``policy``: the domain policy (default: ``domain_policy.yml``); a row
    whose fetch ends on a locator it hides is hidden, and the policy is
    then re-applied to every stored row (:func:`apply_policy`).

    Returns aggregated stats: ``{processed, errors, hidden_by_policy,
    by_source: {…}}``.
    """
    if policy is None:
        policy = load_domain_policy()
    bookmarks = _list_visible_bookmarks(conn, limit=limit, sources=sources)
    log.info("scraping %d visible bookmarks", len(bookmarks))

    own_client = client is None
    if client is None:
        client = ScrapeClient()

    sem = asyncio.Semaphore(max(1, concurrency))
    stats = {"processed": 0, "errors": 0, "hidden_by_policy": 0, "by_source": {}}

    async def _one(bm: dict) -> None:
        async with sem:
            try:
                result = await scrape_one(conn, client, bm, policy=policy)
                stats["processed"] += 1
                stats["hidden_by_policy"] += result["hidden"]
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

    if stats["hidden_by_policy"]:
        # Newly hidden rows: mask their locators in the posts that link
        # them, fix titles and keyword-index rows (as `muninn policy apply`).
        report = apply_policy(conn, policy)
        log.info("domain policy re-applied after scrape: %s", report.summary())
    return stats


# ── Routing ──────────────────────────────────────────────────────

def _pick_enrichment_source(
    results: Iterable[ScrapeResult], *, prefer_live: bool = False
) -> str:
    """Apply the priority ladder. Only ``scrape_status='ok'`` rows count.

    ``prefer_live`` (linked items of X posts) puts the live pass first."""
    by_pass = {r.pass_: r for r in results}
    order = ("live", "at_capture", "recent_archive") if prefer_live else (
        "at_capture", "recent_archive", "live"
    )
    for pass_name in order:
        r = by_pass.get(pass_name)
        if r is not None and r.scrape_status == "ok":
            return _PASS_TO_SOURCE[pass_name]
    return "none"


# ── DB helpers (kept here, not in muninn.db, since they're scrape-only) ──

_SKIPPED_SOURCES = ", ".join(f"'{s}'" for s in API_CONTENT_SOURCES)

_VISIBLE_BOOKMARKS_SQL = f"""
    SELECT bookmark_id, url, captured_at, content_visible, source, source_id, source_metadata
    FROM bookmarks
    WHERE content_visible = 1
      AND source NOT IN ({_SKIPPED_SOURCES})
      {{source_filter}}
    ORDER BY {{order}}
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
    conn: sqlite3.Connection,
    limit: Optional[int] = None,
    sources: Optional[Iterable[str]] = None,
) -> list[dict]:
    params: list = []
    source_filter = ""
    if sources is not None:
        wanted = list(dict.fromkeys(sources))
        source_filter = f"AND source IN ({', '.join('?' for _ in wanted)})" if wanted else "AND 0"
        params.extend(wanted)
    if limit is not None:
        sql = _VISIBLE_BOOKMARKS_SQL.format(
            source_filter=source_filter, order="captured_at DESC, bookmark_id DESC"
        ) + " LIMIT ?"
        params.append(limit)
    else:
        sql = _VISIBLE_BOOKMARKS_SQL.format(source_filter=source_filter, order="bookmark_id")
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


# ── scrape_history (append-only revisions) ───────────────────────

_BLOCKED_STATUSES = frozenset({"auth_required", "robots_disallowed", "paywall", "js_required"})
_DELETED_HTTP = frozenset({404, 410})


def _identity(conn: sqlite3.Connection, bookmark_row: dict) -> dict:
    """source / item_ref / observation time for a bookmark being scraped."""
    row = bookmark_row
    if "source" not in row or "source_id" not in row:
        found = conn.execute(
            "SELECT bookmark_id, url, captured_at, source, source_id, source_metadata "
            "FROM bookmarks WHERE bookmark_id = ?",
            (int(bookmark_row["bookmark_id"]),),
        ).fetchone()
        row = dict(found) if found is not None else {**bookmark_row, "source": "", "source_id": ""}
    source = row.get("source") or ""
    url = row.get("url")
    captured_at = row.get("captured_at")
    return {
        "source": source,
        "url": url,
        "item_ref": item_ref_for(source, str(row.get("source_id") or ""), url, row_metadata(row)),
        # Linked items are not bookmarks; a browser bookmark's ADD_DATE is.
        "observed_at": None if source == SOURCE_X_LINK or not captured_at else int(captured_at),
    }


def _coverage(r: ScrapeResult) -> Optional[str]:
    """Contract coverage of one fetch, or None when it observed no content
    state (no URL, no archive, timeout, network error, other HTTP errors)."""
    status = r.scrape_status
    if status in ("ok", "partial"):
        if not (r.content_text or "").strip():
            return "empty"
        return "full" if status == "ok" and r.extraction_quality == "ok" else "partial"
    if status in _BLOCKED_STATUSES:
        return "blocked"
    if status == "failed" and r.http_status in _DELETED_HTTP:
        return "deleted"
    return None


def _record_history(conn: sqlite3.Connection, r: ScrapeResult, identity: dict) -> None:
    coverage = _coverage(r)
    if coverage is None:
        return
    is_archive = r.pass_ in ("at_capture", "recent_archive")
    if is_archive and r.actual_snapshot_at is None:
        return  # an archive revision must name its snapshot time
    final_locator = _sanitized_locator(r.final_url or (r.archive_url if is_archive else None))
    if final_locator == identity["url"]:
        final_locator = None
    record_revision(
        conn,
        bookmark_id=r.bookmark_id,
        item_ref=identity["item_ref"],
        text=r.content_text,
        coverage=coverage,  # type: ignore[arg-type]
        retrieval_method="archive" if is_archive else "live",
        scrape_pass=r.pass_,
        extractor_name=EXTRACTOR_NAME,
        extractor_version=EXTRACTOR_VERSION,
        http_status=r.http_status,
        final_locator=final_locator,
        archive_snapshot_at=r.actual_snapshot_at if is_archive else None,
        published_at=None,
        retrieved_at=r.fetched_at,
        observed_at=identity["observed_at"],
    )


def _sanitized_locator(url: Optional[str]) -> Optional[str]:
    """``sanitize_url`` for a fetched locator; None when it is rejected."""
    return sanitize_url(url).sanitized_url if url else None


def _upsert_scrape_result(conn: sqlite3.Connection, r: ScrapeResult) -> None:
    # ``final_url`` is wherever the redirects ended. It can carry a
    # credential-shaped param the bookmark's own (sanitized) URL never had,
    # so it is sanitized like every other stored locator.
    conn.execute(_UPSERT_SCRAPE_SQL, {
        "bookmark_id": r.bookmark_id,
        "pass": r.pass_,
        "fetched_at": r.fetched_at,
        "target_timestamp": r.target_timestamp,
        "actual_snapshot_at": r.actual_snapshot_at,
        "archive_url": r.archive_url,
        "final_url": _sanitized_locator(r.final_url),
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
    refresh: bool = False,
    sources: Optional[Iterable[str]] = None,
) -> dict:
    """Synchronous wrapper for ``scrape_all``: opens a connection, runs,
    closes. For use from ``__main__`` or click commands. ``refresh``
    bypasses the live HTTP cache and the recent_archive "latest snapshot"
    lookup, so a changed page is re-captured."""
    conn = connect(db_path)
    try:
        client = ScrapeClient(refresh=refresh)

        async def _run() -> dict:
            try:
                return await scrape_all(
                    conn, client=client, concurrency=concurrency, limit=limit, sources=sources
                )
            finally:
                await client.close()

        return asyncio.run(_run())
    finally:
        conn.close()
