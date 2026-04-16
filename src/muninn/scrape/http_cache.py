"""On-disk SHA256-keyed HTTP response cache.

Two cache instances are used in practice:

- ``data/http-cache/`` — JSON-serialized response metadata + body for live
  and Wayback fetches; lets a re-run skip the network entirely.
- ``data/scrape-cache/`` — gzipped raw HTML written alongside the response,
  plus CDX-lookup memoization (timestamps).

The cache key is an arbitrary string (callers prefix it with the pass —
``live:``, ``ia:``, ``cdx:``, etc. — to avoid collisions). The on-disk
filename is ``sha256(key).json``; for raw HTML, it's ``sha256(key).html.gz``.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
from typing import Optional


class HttpCache:
    """File-backed HTTP cache. JSON for response payloads, gzip for raw HTML."""

    def __init__(self, cache_dir: Path) -> None:
        self._dir = cache_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    def _hash(self, key: str) -> str:
        return hashlib.sha256(key.encode()).hexdigest()

    def _key_path(self, key: str) -> Path:
        return self._dir / f"{self._hash(key)}.json"

    def _raw_path(self, key: str) -> Path:
        return self._dir / f"{self._hash(key)}.html.gz"

    def get(self, key: str) -> Optional[dict]:
        """Return the cached JSON payload for ``key``, or None if absent."""
        p = self._key_path(key)
        if p.exists():
            try:
                return json.loads(p.read_text())
            except (OSError, json.JSONDecodeError):
                return None
        return None

    def put(self, key: str, data: dict) -> None:
        """Store ``data`` (must be JSON-serializable) under ``key``."""
        p = self._key_path(key)
        p.write_text(json.dumps(data, default=str))

    def has(self, key: str) -> bool:
        return self._key_path(key).exists()

    def put_raw(self, key: str, body: bytes) -> str:
        """Write a gzipped raw HTML body to disk; return the absolute path.

        Used to spill large response bodies out of the SQLite row into
        ``scrape_results.raw_html_path`` while keeping ``content_html`` in DB
        for small/medium pages.
        """
        p = self._raw_path(key)
        with gzip.open(p, "wb") as fh:
            fh.write(body)
        return str(p)

    def get_raw(self, key: str) -> Optional[bytes]:
        """Read back the gzipped raw HTML body for ``key``."""
        p = self._raw_path(key)
        if p.exists():
            with gzip.open(p, "rb") as fh:
                return fh.read()
        return None

    def clear(self) -> None:
        for f in self._dir.glob("*.json"):
            f.unlink()
        for f in self._dir.glob("*.html.gz"):
            f.unlink()
