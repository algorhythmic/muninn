"""The search interface every retrieval backend implements (ADR-014).

Kept dependency-free on purpose: importing this module (or
:mod:`muninn.search`) must never load Qdrant, sentence-transformers or
any other backend's client library.

Contract:

- Backends return **bookmark IDs with scores**, not hydrated rows. SQLite
  stays canonical; callers hydrate hits from it.
- Callers re-apply ``content_visible = 1`` at hydration, so a backend
  whose index cannot see visibility (e.g. a vector store) is still safe.
  A backend that *can* filter (FTS5 shares the DB) should filter before
  applying ``limit`` so hidden rows don't eat result slots.
- Hits are ordered best-first. ``score`` is higher-is-better and only
  comparable within one result list; ``None`` means the index produced
  no score.
- :meth:`SearchBackend.semantic` may raise when its index can't serve
  (unreachable, missing embedding backend). It should raise
  :class:`SearchUnavailable` for that; callers fall back to
  :meth:`SearchBackend.keyword`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class SearchBackendError(RuntimeError):
    """The configured search backend is unknown or cannot be built."""


class SearchUnavailable(RuntimeError):
    """A backend's index cannot serve this query right now."""


@dataclass(frozen=True)
class SearchHit:
    """One ranked result: which bookmark, how well it matched, and where from."""

    bookmark_id: int
    score: float | None
    #: Name of the index that produced the hit, e.g. ``"fts5"`` or ``"qdrant"``.
    index: str


@runtime_checkable
class SearchBackend(Protocol):
    """What the MCP server needs from a retrieval engine."""

    #: Registry name, e.g. ``"fts5-qdrant"``.
    name: str

    def keyword(self, query: str, limit: int = 10) -> list[SearchHit]:
        """Lexical search. ``query`` uses the backend's match syntax
        (FTS5 MATCH for the default backend)."""
        ...

    def semantic(self, query: str, limit: int = 10) -> list[SearchHit]:
        """Embedding-similarity search. May raise :class:`SearchUnavailable`."""
        ...
