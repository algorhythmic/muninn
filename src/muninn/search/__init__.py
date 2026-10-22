"""Retrieval interface (ADR-014): one seam between consumers and search engines.

Consumers (the MCP server today) talk to a :class:`SearchBackend` obtained
from :func:`get_search_backend`; they never import Qdrant, FTS SQL or an
embedding model directly. Swapping engines means adding one backend
module and one registry entry here.

Backends, selected by ``$MUNINN_SEARCH_BACKEND`` (read at call time):

- ``fts5-qdrant`` (default) — SQLite FTS5 keyword + Qdrant semantic
  (see :mod:`muninn.search.fts5_qdrant`).

Backend modules are imported lazily, so ``import muninn.search`` is light.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from muninn.search.base import (
    SearchBackend,
    SearchBackendError,
    SearchHit,
    SearchUnavailable,
)

DEFAULT_BACKEND = "fts5-qdrant"

# name → "module:attr" (imported on first use) or a callable factory.
_REGISTRY: dict[str, str | Callable[..., SearchBackend]] = {
    DEFAULT_BACKEND: "muninn.search.fts5_qdrant:Fts5QdrantBackend",
}


def register_backend(name: str, factory: Callable[..., SearchBackend]) -> None:
    """Register (or replace) a backend factory under ``name``.

    The factory is called as ``factory(db_path=..., **options)``.
    """
    _REGISTRY[name] = factory


def available_backends() -> list[str]:
    return sorted(_REGISTRY)


def _resolve(entry: str | Callable[..., SearchBackend]) -> Callable[..., SearchBackend]:
    if callable(entry):
        return entry
    module_name, _, attr = entry.partition(":")
    return getattr(importlib.import_module(module_name), attr)


def get_search_backend(
    name: str | None = None,
    *,
    db_path: str | Path | None = None,
    **options: Any,
) -> SearchBackend:
    """Build the configured backend.

    ``name`` defaults to ``$MUNINN_SEARCH_BACKEND``, then ``fts5-qdrant``.
    ``db_path`` is the canonical SQLite store used for hydration-side
    concerns; ``options`` are passed through to the backend's constructor.
    """
    chosen = name or os.environ.get("MUNINN_SEARCH_BACKEND") or DEFAULT_BACKEND
    entry = _REGISTRY.get(chosen)
    if entry is None:
        raise SearchBackendError(
            f"Unknown MUNINN_SEARCH_BACKEND {chosen!r} — expected one of "
            f"{available_backends()}"
        )
    return _resolve(entry)(db_path=db_path, **options)


__all__ = [
    "DEFAULT_BACKEND",
    "SearchBackend",
    "SearchBackendError",
    "SearchHit",
    "SearchUnavailable",
    "available_backends",
    "get_search_backend",
    "register_backend",
]
