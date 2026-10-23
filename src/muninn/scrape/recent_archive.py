"""recent_archive pass: fetch the most-recent IA snapshot regardless of date.

Fired only when at_capture produced ``no_archive`` (or otherwise didn't
yield ``ok``). The most-recent snapshot is found via CDX with ``limit=-1``
and no time bounds. ``target_timestamp`` is left NULL because we asked for
"latest", not a specific date. Unlike the at_capture lookup, that answer
changes over time, so it is cached with a TTL (``recent_cdx_ttl``) and
``--refresh`` bypasses it; the snapshot body it names is immutable and
cached forever.
"""

from __future__ import annotations

import time
from typing import Optional

from muninn.models import ScrapeResult
from muninn.scrape.at_capture import _build_archive_result, _iso_to_epoch
from muninn.scrape.client import ScrapeClient


async def fetch_recent_archive(
    client: ScrapeClient,
    bookmark_id: int,
    url: Optional[str],
) -> ScrapeResult:
    """Run the recent_archive pass for one bookmark."""
    now = int(time.time())

    if not url:
        return ScrapeResult(
            bookmark_id=bookmark_id,
            **{"pass": "recent_archive"},
            fetched_at=now,
            scrape_status="no_archive",
            error_detail="missing url",
        )

    # "Latest snapshot" changes as IA re-archives the page: the memo
    # expires (MUNINN_RECENT_CDX_TTL_SECONDS) and `--refresh` skips it.
    timestamp = await client.cdx_lookup(
        url,
        limit="-1",
        cache_key=f"cdx:recent:{url}",
        max_age=client.recent_cdx_ttl,
        bypass_cache=client.refresh,
    )

    if not timestamp:
        return ScrapeResult(
            bookmark_id=bookmark_id,
            **{"pass": "recent_archive"},
            fetched_at=now,
            scrape_status="no_archive",
        )

    result = await client.fetch_archive(url, timestamp)
    archive_url = result.get("archive_url")
    actual_snapshot_at = _iso_to_epoch(timestamp)

    if "error" in result:
        return ScrapeResult(
            bookmark_id=bookmark_id,
            **{"pass": "recent_archive"},
            fetched_at=now,
            actual_snapshot_at=actual_snapshot_at,
            archive_url=archive_url,
            scrape_status=result.get("error_kind", "network_error"),
            error_detail=result.get("error"),
        )

    return _build_archive_result(
        client=client,
        bookmark_id=bookmark_id,
        pass_name="recent_archive",
        result=result,
        now=now,
        target_timestamp=None,
        actual_snapshot_at=actual_snapshot_at,
        archive_url=archive_url,
        original_url=url,
    )
