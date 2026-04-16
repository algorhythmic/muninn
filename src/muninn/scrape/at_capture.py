"""at_capture pass: fetch the IA snapshot closest to the bookmark's
``captured_at`` (within ±365 days, per :data:`muninn.config.AT_CAPTURE_WINDOW_DAYS`).

The point of at_capture is to recover the page **as the user originally saw
it**, even if the live site has since rotted, redirected, or been redesigned.
We accept the closest snapshot in the window even if it's a month off — the
±365d slop matches the spec.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from muninn.config import AT_CAPTURE_WINDOW_DAYS
from muninn.models import ScrapeResult
from muninn.scrape.auth_wall import detect_auth_wall
from muninn.scrape.client import ScrapeClient, LARGE_BODY_THRESHOLD
from muninn.scrape.extract import classify_extraction_quality, extract_text


async def fetch_at_capture(
    client: ScrapeClient,
    bookmark_id: int,
    url: Optional[str],
    captured_at: Optional[int],
) -> ScrapeResult:
    """Run the at_capture pass for one bookmark.

    Returns a :class:`ScrapeResult` with ``pass='at_capture'``. If we can't
    even attempt (no URL, no captured_at), the row is recorded with
    ``scrape_status='no_archive'`` so re-runs don't spam IA needlessly.
    """
    now = int(time.time())

    if not url or captured_at is None:
        return ScrapeResult(
            bookmark_id=bookmark_id,
            **{"pass": "at_capture"},
            fetched_at=now,
            scrape_status="no_archive",
            error_detail="missing url or captured_at",
        )

    captured_dt = datetime.fromtimestamp(captured_at, tz=timezone.utc)
    from_dt = captured_dt - timedelta(days=AT_CAPTURE_WINDOW_DAYS)
    to_dt = captured_dt + timedelta(days=AT_CAPTURE_WINDOW_DAYS)

    target_timestamp = captured_at  # epoch — what we asked of IA

    timestamp = await client.cdx_lookup(
        url,
        closest=captured_dt.strftime("%Y%m%d%H%M%S"),
        from_ts=from_dt.strftime("%Y%m%d%H%M%S"),
        to_ts=to_dt.strftime("%Y%m%d%H%M%S"),
        limit="1",
        cache_key=f"cdx:at_capture:{url}:{captured_at}",
    )

    if not timestamp:
        return ScrapeResult(
            bookmark_id=bookmark_id,
            **{"pass": "at_capture"},
            fetched_at=now,
            target_timestamp=target_timestamp,
            scrape_status="no_archive",
        )

    result = await client.fetch_archive(url, timestamp)
    archive_url = result.get("archive_url")
    actual_snapshot_at = _iso_to_epoch(timestamp)

    if "error" in result:
        return ScrapeResult(
            bookmark_id=bookmark_id,
            **{"pass": "at_capture"},
            fetched_at=now,
            target_timestamp=target_timestamp,
            actual_snapshot_at=actual_snapshot_at,
            archive_url=archive_url,
            scrape_status=result.get("error_kind", "network_error"),
            error_detail=result.get("error"),
        )

    return _build_archive_result(
        client=client,
        bookmark_id=bookmark_id,
        pass_name="at_capture",
        result=result,
        now=now,
        target_timestamp=target_timestamp,
        actual_snapshot_at=actual_snapshot_at,
        archive_url=archive_url,
        original_url=url,
    )


def _iso_to_epoch(ts: str) -> Optional[int]:
    """``20240115120000`` → epoch seconds, or None if unparseable."""
    try:
        dt = datetime.strptime(ts, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except (ValueError, TypeError):
        return None


def _build_archive_result(
    *,
    client: ScrapeClient,
    bookmark_id: int,
    pass_name: str,
    result: dict,
    now: int,
    target_timestamp: Optional[int],
    actual_snapshot_at: Optional[int],
    archive_url: Optional[str],
    original_url: str,
) -> ScrapeResult:
    """Shared result construction for at_capture and recent_archive."""
    http_status = int(result["http_status"])
    body = result.get("body") or ""
    body_bytes: bytes = result.get("body_bytes") or b""
    final_url = result.get("final_url")

    if http_status in (401, 403):
        status, quality, content_text, err = "auth_required", None, None, f"HTTP {http_status}"
    elif http_status == 404:
        status, quality, content_text, err = "no_archive", None, None, "HTTP 404 from IA"
    elif http_status >= 400:
        status, quality, content_text, err = "failed", None, None, f"HTTP {http_status}"
    elif detect_auth_wall(body):
        status, quality, content_text, err = "auth_required", None, None, "auth-wall heuristic"
    else:
        text = extract_text(body)
        quality = classify_extraction_quality(text, body)
        if quality == "failed":
            status, content_text, err = "partial", text or None, "extraction yielded no text"
        else:
            status, content_text, err = "ok", text, None

    content_html: Optional[str] = body if body else None
    raw_html_path: Optional[str] = None
    if body_bytes and len(body_bytes) >= LARGE_BODY_THRESHOLD:
        raw_html_path = client.spill_raw_html(
            f"{pass_name}:{original_url}:{actual_snapshot_at}", body_bytes
        )
        content_html = None

    return ScrapeResult(
        bookmark_id=bookmark_id,
        **{"pass": pass_name},
        fetched_at=now,
        target_timestamp=target_timestamp,
        actual_snapshot_at=actual_snapshot_at,
        archive_url=archive_url,
        final_url=final_url,
        http_status=http_status,
        scrape_status=status,
        extraction_quality=quality,
        content_text=content_text,
        content_html=content_html,
        raw_html_path=raw_html_path,
        error_detail=err,
    )
