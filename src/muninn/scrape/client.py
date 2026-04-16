"""HTTP client wrapper: shared httpx.AsyncClient + politeness + caching.

A single :class:`ScrapeClient` instance is shared across all passes for a
run. It owns:

- the underlying ``httpx.AsyncClient`` (with our User-Agent + 30s timeout)
- a :class:`RateLimiter` (per-domain for live, global for IA)
- two on-disk :class:`HttpCache` instances (response-payload cache and
  raw-HTML / CDX-memo cache)
- a request log for tests (so SC8 can assert "second run hits zero network")

Pass modules (``live``, ``at_capture``, ``recent_archive``) consume the
client via :meth:`fetch_live`, :meth:`fetch_archive`, and :meth:`cdx_lookup`.
The client itself does not know about scrape-result rows or the DB — it's a
pure transport/cache layer.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

import httpx

from muninn.config import (
    HTTP_TIMEOUT_SECONDS,
    IA_GLOBAL_RPS,
    LIVE_DOMAIN_RPS,
    load_paths,
)
from muninn.scrape.http_cache import HttpCache
from muninn.scrape.rate_limiter import RateLimiter

log = logging.getLogger(__name__)

USER_AGENT = (
    f"Muninn/v1 (+{os.environ.get('MUNINN_CONTACT_URL', 'https://example.com/muninn')})"
)

IA_CDX_BASE = "https://web.archive.org/cdx/search/cdx"
IA_WEB_BASE = "https://web.archive.org/web"
IA_GROUP = "internet_archive"

# Inverse of RPS — minimum interval between consecutive requests.
LIVE_DOMAIN_INTERVAL = 1.0 / LIVE_DOMAIN_RPS  # 1.0 s
IA_GLOBAL_INTERVAL = 1.0 / IA_GLOBAL_RPS      # 2.0 s

# Spill threshold: bodies larger than this go to ``raw_html_path`` on disk
# instead of the ``content_html`` SQLite column. Keeps the DB size sane.
LARGE_BODY_THRESHOLD = 256 * 1024  # 256 KiB


class ScrapeClient:
    """Shared httpx client + caches + rate limiter for a scrape run."""

    def __init__(
        self,
        *,
        http_cache_dir: Optional[Path] = None,
        scrape_cache_dir: Optional[Path] = None,
        client: Optional[httpx.AsyncClient] = None,
        rate_limiter: Optional[RateLimiter] = None,
    ) -> None:
        paths = load_paths()
        self.http_cache = HttpCache(http_cache_dir or paths.http_cache_dir)
        self.scrape_cache = HttpCache(scrape_cache_dir or paths.scrape_cache_dir)
        self.rate = rate_limiter or RateLimiter()
        self._client = client
        self._owns_client = client is None
        self._request_log: list[dict[str, Any]] = []

    # ── httpx lifecycle ──────────────────────────────────────────

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
                timeout=HTTP_TIMEOUT_SECONDS,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> "ScrapeClient":
        await self._get_client()
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.close()

    # ── Test hook ────────────────────────────────────────────────

    @property
    def request_log(self) -> list[dict[str, Any]]:
        """All network attempts since construction. Cache hits are not logged
        (tests rely on this to assert "second run made zero requests")."""
        return self._request_log

    # ── Live fetch ───────────────────────────────────────────────

    async def fetch_live(self, url: str) -> dict[str, Any]:
        """Fetch ``url`` directly from the live origin.

        Returns a dict with one of two shapes:

        - success: ``{"http_status", "content_type", "body", "body_bytes",
          "final_url", "elapsed_ms", "from_cache"}``
        - failure: ``{"error", "error_kind", "elapsed_ms"}`` where
          ``error_kind`` is one of ``"timeout" | "network_error"``.
        """
        cache_key = f"live:{url}"
        cached = self.http_cache.get(cache_key)
        if cached is not None:
            cached["from_cache"] = True
            cached["body_bytes"] = (cached.get("body") or "").encode(
                "utf-8", errors="replace"
            )
            return cached

        await self.rate.wait_for_domain(url, LIVE_DOMAIN_INTERVAL)
        return await self._do_fetch(url, cache_key, kind="live")

    # ── Wayback archive fetch ────────────────────────────────────

    async def fetch_archive(self, original_url: str, timestamp: str) -> dict[str, Any]:
        """Fetch the Wayback ``id_`` (raw, unrewritten) snapshot for
        ``original_url`` at ``timestamp`` (YYYYMMDDHHMMSS).
        """
        archive_url = f"{IA_WEB_BASE}/{timestamp}id_/{original_url}"
        cache_key = f"ia:{archive_url}"
        cached = self.http_cache.get(cache_key)
        if cached is not None:
            cached["from_cache"] = True
            cached["body_bytes"] = (cached.get("body") or "").encode(
                "utf-8", errors="replace"
            )
            cached["archive_url"] = archive_url
            return cached

        await self.rate.wait_for_global(IA_GROUP, IA_GLOBAL_INTERVAL)
        result = await self._do_fetch(archive_url, cache_key, kind="ia_fetch")
        result["archive_url"] = archive_url
        return result

    # ── CDX lookups ──────────────────────────────────────────────

    async def cdx_lookup(
        self,
        url: str,
        *,
        closest: Optional[str] = None,
        from_ts: Optional[str] = None,
        to_ts: Optional[str] = None,
        limit: str = "1",
        cache_key: Optional[str] = None,
    ) -> Optional[str]:
        """Query the IA CDX API for a snapshot timestamp.

        - ``closest`` + ``from_ts`` + ``to_ts`` selects the at_capture window
          mode (one snapshot closest to the given epoch within the window).
        - ``limit="-1"`` with no time bounds selects the most-recent snapshot.

        Returns the timestamp string (YYYYMMDDHHMMSS) on hit, ``None`` on
        miss or after retries are exhausted.
        """
        params: dict[str, str] = {
            "url": url,
            "output": "json",
            "limit": limit,
            "filter": "statuscode:200",
        }
        if closest is not None:
            params["closest"] = closest
        if from_ts is not None:
            params["from"] = from_ts
        if to_ts is not None:
            params["to"] = to_ts

        # Cache CDX results in the scrape_cache to memoize across runs.
        ck = cache_key or f"cdx:{url}:{closest}:{from_ts}:{to_ts}:{limit}"
        cached = self.scrape_cache.get(ck)
        if cached is not None:
            return cached.get("timestamp")

        await self.rate.wait_for_global(IA_GROUP, IA_GLOBAL_INTERVAL)
        client = await self._get_client()
        self._request_log.append(
            {"type": "cdx", "url": url, "time": time.monotonic()}
        )

        retries = 3
        for attempt in range(retries):
            try:
                resp = await client.get(IA_CDX_BASE, params=params)
                if resp.status_code == 429:
                    backoff = 2 ** (attempt + 1)
                    log.warning("CDX 429, backing off %ds", backoff)
                    await asyncio.sleep(backoff)
                    await self.rate.wait_for_global(IA_GROUP, IA_GLOBAL_INTERVAL)
                    self._request_log.append(
                        {"type": "cdx_retry", "url": url, "time": time.monotonic()}
                    )
                    continue
                resp.raise_for_status()
                data = resp.json()
                # CDX JSON: row 0 is the header, rows 1..N are matches.
                if len(data) >= 2:
                    if limit == "-1":
                        timestamp = data[-1][1]
                    else:
                        timestamp = data[1][1]
                    self.scrape_cache.put(ck, {"timestamp": timestamp})
                    return timestamp
                self.scrape_cache.put(ck, {"timestamp": None})
                return None
            except (httpx.HTTPError, ValueError):
                if attempt == retries - 1:
                    return None
        return None

    # ── Internal: do a single HTTP GET with logging + caching ────

    async def _do_fetch(
        self, url: str, cache_key: str, *, kind: str
    ) -> dict[str, Any]:
        client = await self._get_client()
        start = time.monotonic()
        self._request_log.append({"type": kind, "url": url, "time": start})
        try:
            resp = await client.get(url)
            elapsed_ms = int((time.monotonic() - start) * 1000)
            content = resp.content
            body_text = content.decode("utf-8", errors="replace")
            result: dict[str, Any] = {
                "http_status": resp.status_code,
                "content_type": resp.headers.get("content-type", ""),
                "body": body_text,
                "body_bytes": content,
                "body_length": len(content),
                "elapsed_ms": elapsed_ms,
                "final_url": str(resp.url),
                "from_cache": False,
            }
            # Cache the JSON-serializable subset (drop raw bytes).
            cache_data = {k: v for k, v in result.items() if k != "body_bytes"}
            self.http_cache.put(cache_key, cache_data)
            return result
        except httpx.TimeoutException:
            return {
                "error": "timeout",
                "error_kind": "timeout",
                "elapsed_ms": int((time.monotonic() - start) * 1000),
            }
        except httpx.HTTPError as exc:
            return {
                "error": str(exc),
                "error_kind": "network_error",
                "elapsed_ms": int((time.monotonic() - start) * 1000),
            }

    # ── Raw-HTML spill ───────────────────────────────────────────

    def spill_raw_html(self, key: str, body: bytes) -> str:
        """Write ``body`` gzipped to scrape-cache, return the absolute path."""
        return self.scrape_cache.put_raw(key, body)

    @staticmethod
    def is_large(body_length: int) -> bool:
        return body_length >= LARGE_BODY_THRESHOLD
