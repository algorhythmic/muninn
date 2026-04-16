"""Live-fetch pass: GET the original URL directly from the origin.

Produces a :class:`ScrapeResult` with ``pass='live'``. Status mapping:

- HTTP 2xx + content + not auth-walled → ``ok`` (extraction_quality from
  :func:`extract_with_quality`)
- HTTP 2xx + auth-wall heuristic → ``auth_required``
- HTTP 401/403 → ``auth_required``
- HTTP 451 → ``robots_disallowed`` (legal blocks; closest mapping)
- HTTP 4xx/5xx otherwise → ``failed``
- httpx timeout → ``timeout``
- httpx other transport errors → ``network_error``
"""

from __future__ import annotations

import time
from typing import Optional

from muninn.models import ScrapeResult
from muninn.scrape.auth_wall import detect_auth_wall
from muninn.scrape.client import ScrapeClient, LARGE_BODY_THRESHOLD
from muninn.scrape.extract import classify_extraction_quality, extract_text


async def fetch_live(
    client: ScrapeClient, bookmark_id: int, url: Optional[str]
) -> ScrapeResult:
    """Run the live pass for one bookmark and return a populated
    :class:`ScrapeResult`. Caller persists it via :func:`muninn.db.transaction`.
    """
    now = int(time.time())

    if not url:
        return ScrapeResult(
            bookmark_id=bookmark_id,
            **{"pass": "live"},
            fetched_at=now,
            scrape_status="failed",
            error_detail="no url on bookmark",
        )

    result = await client.fetch_live(url)

    if "error" in result:
        return ScrapeResult(
            bookmark_id=bookmark_id,
            **{"pass": "live"},
            fetched_at=now,
            scrape_status=result.get("error_kind", "network_error"),
            error_detail=result.get("error"),
        )

    http_status = int(result["http_status"])
    body = result.get("body") or ""
    body_bytes: bytes = result.get("body_bytes") or b""
    final_url = result.get("final_url")

    status, extraction_quality, content_text, error_detail = _classify(
        http_status, body
    )

    content_html: Optional[str] = body if body else None
    raw_html_path: Optional[str] = None
    if body_bytes and len(body_bytes) >= LARGE_BODY_THRESHOLD:
        raw_html_path = client.spill_raw_html(f"live:{url}", body_bytes)
        content_html = None

    return ScrapeResult(
        bookmark_id=bookmark_id,
        **{"pass": "live"},
        fetched_at=now,
        final_url=final_url,
        http_status=http_status,
        scrape_status=status,
        extraction_quality=extraction_quality,
        content_text=content_text,
        content_html=content_html,
        raw_html_path=raw_html_path,
        error_detail=error_detail,
    )


def _classify(
    http_status: int, body: str
) -> tuple[str, Optional[str], Optional[str], Optional[str]]:
    """Map ``(http_status, body)`` → ``(scrape_status, extraction_quality,
    content_text, error_detail)``."""
    # Hard auth signals from the wire come first.
    if http_status in (401, 403):
        return "auth_required", None, None, f"HTTP {http_status}"
    if http_status == 451:
        return "robots_disallowed", None, None, "HTTP 451"
    if http_status >= 400:
        return "failed", None, None, f"HTTP {http_status}"

    # 2xx/3xx — inspect the body.
    if detect_auth_wall(body):
        return "auth_required", None, None, "auth-wall heuristic"

    text = extract_text(body)
    quality = classify_extraction_quality(text, body)
    if quality == "failed":
        return "partial", quality, text or None, "extraction yielded no text"
    return "ok", quality, text, None
