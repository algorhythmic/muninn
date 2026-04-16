"""Dual-pass scrape pipeline (live + Wayback at_capture / recent_archive).

Public surface:
- ``scrape_all`` — orchestrate dual-pass scrape across all visible bookmarks
- ``scrape_one`` — run dual-pass for a single bookmark
- ``ScrapeClient`` — httpx wrapper with politeness + on-disk cache

Per-bookmark execution is serial (at_capture → recent_archive → live), but
``scrape_all`` may interleave many bookmarks concurrently. Politeness is
enforced by the shared rate limiter (per-domain for live, global for IA).
"""

from __future__ import annotations

from muninn.scrape.client import ScrapeClient, USER_AGENT
from muninn.scrape.pipeline import scrape_all, scrape_one

__all__ = ["scrape_all", "scrape_one", "ScrapeClient", "USER_AGENT"]
