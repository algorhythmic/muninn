"""Dual-pass scrape pipeline (live + Wayback at_capture / recent_archive).

Public surface:
- ``scrape_all`` — orchestrate dual-pass scrape across all visible bookmarks
- ``scrape_one`` — run dual-pass for a single bookmark
- ``ScrapeClient`` — httpx wrapper with politeness + on-disk cache

Per-bookmark execution is serial (at_capture → recent_archive → live), but
``scrape_all`` may interleave many bookmarks concurrently. Politeness is
enforced by the shared rate limiter (per-domain for live, global for IA).

The public names load lazily (PEP 562) so that importing a light submodule
such as ``muninn.scrape.domain_policy`` doesn't pull in httpx/selectolax.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from muninn.scrape.client import USER_AGENT, ScrapeClient
    from muninn.scrape.pipeline import scrape_all, scrape_one

__all__ = ["scrape_all", "scrape_one", "ScrapeClient", "USER_AGENT"]

_LAZY = {
    "ScrapeClient": "muninn.scrape.client",
    "USER_AGENT": "muninn.scrape.client",
    "scrape_all": "muninn.scrape.pipeline",
    "scrape_one": "muninn.scrape.pipeline",
}


def __getattr__(name: str) -> Any:
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(module), name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
