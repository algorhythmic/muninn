"""Per-domain (live origins) and global (Internet Archive) rate limiting.

Politeness constants in :mod:`muninn.config`:
- ``LIVE_DOMAIN_RPS = 1.0`` — at most 1 request/sec to any single live origin
- ``IA_GLOBAL_RPS = 0.5`` — at most 1 request every 2s across all IA endpoints

The limiter is async-safe: a single ``asyncio.Lock`` guards both maps so
concurrent ``wait_for_*`` callers never race on the timestamp update.
"""

from __future__ import annotations

import asyncio
import time
from urllib.parse import urlparse


class RateLimiter:
    """Async rate limiter that enforces minimum intervals between requests.

    Two independent buckets:
    - per-domain (keyed by netloc) for live origin politeness
    - global named groups (e.g. ``"internet_archive"``) for shared services
    """

    def __init__(self) -> None:
        self._domain_last: dict[str, float] = {}
        self._global_last: dict[str, float] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _domain(url: str) -> str:
        return urlparse(url).netloc

    async def wait_for_domain(self, url: str, min_interval: float) -> None:
        """Block until ``min_interval`` seconds have elapsed since last fetch
        from this URL's domain. Updates the last-fetch timestamp on return."""
        domain = self._domain(url)
        async with self._lock:
            last = self._domain_last.get(domain, 0.0)
            now = time.monotonic()
            wait = max(0.0, min_interval - (now - last))
            if wait > 0:
                await asyncio.sleep(wait)
            self._domain_last[domain] = time.monotonic()

    async def wait_for_global(self, group: str, min_interval: float) -> None:
        """Block until ``min_interval`` seconds have elapsed since last call
        in the named global group (e.g. shared IA endpoints)."""
        async with self._lock:
            last = self._global_last.get(group, 0.0)
            now = time.monotonic()
            wait = max(0.0, min_interval - (now - last))
            if wait > 0:
                await asyncio.sleep(wait)
            self._global_last[group] = time.monotonic()

    def record_domain_request(self, url: str) -> None:
        """Force-update the per-domain last-request timestamp (test helper)."""
        domain = self._domain(url)
        self._domain_last[domain] = time.monotonic()

    def record_global_request(self, group: str) -> None:
        """Force-update the global-group last-request timestamp (test helper)."""
        self._global_last[group] = time.monotonic()

    def last_request_time(self, url: str) -> float:
        """Return the monotonic timestamp of the last request to this domain."""
        domain = self._domain(url)
        return self._domain_last.get(domain, 0.0)
