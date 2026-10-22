"""Default hybrid ranking: reciprocal rank fusion of keyword + semantic.

Stdlib only, like :mod:`muninn.search.base`.

Reciprocal rank fusion (Cormack, Clarke & Büttcher, SIGIR 2009) scores
each bookmark as ``Σ 1 / (RRF_K + rank)`` over the rankings it appears
in (rank is 1-based). It needs no score calibration between indexes,
which matters here: bm25 and cosine similarity are not on one scale.

``RRF_K = 60`` is the constant from that paper and the common default.
A larger value flattens the curve (deep ranks count for more); a smaller
one rewards the head of each list.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence

from muninn.search.base import SearchBackend, SearchHit, SearchUnavailable

log = logging.getLogger(__name__)

#: The RRF smoothing constant (Cormack et al. 2009).
RRF_K = 60

#: Each component ranking is fetched this many times deeper than the
#: fused ``limit``, so an item ranked just outside one list's top
#: ``limit`` can still win on the other.
DEPTH_FACTOR = 2


def rrf_fuse(
    rankings: Sequence[Sequence[SearchHit]],
    *,
    k: int = RRF_K,
    limit: int | None = None,
) -> list[SearchHit]:
    """Fuse best-first rankings into one, best first.

    A bookmark repeated within one ranking counts once, at its best rank.
    The fused hit's ``score`` is its RRF score and its ``index`` names the
    contributing indexes joined by ``+`` (e.g. ``"fts5+qdrant"``), in the
    order they first ranked it. Ties go to the better single rank, then
    to whichever was ranked first (keyword before semantic for
    :func:`rrf_hybrid`).
    """
    scores: dict[int, float] = {}
    best_rank: dict[int, int] = {}
    first_seen: dict[int, int] = {}
    indexes: dict[int, list[str]] = {}
    for ranking in rankings:
        seen: set[int] = set()
        for rank, hit in enumerate(ranking, start=1):
            bid = hit.bookmark_id
            if bid in seen:
                continue
            seen.add(bid)
            scores[bid] = scores.get(bid, 0.0) + 1.0 / (k + rank)
            best_rank[bid] = min(best_rank.get(bid, rank), rank)
            first_seen.setdefault(bid, len(first_seen))
            names = indexes.setdefault(bid, [])
            if hit.index not in names:
                names.append(hit.index)
    order = sorted(scores, key=lambda b: (-scores[b], best_rank[b], first_seen[b]))
    if limit is not None:
        order = order[:limit]
    return [SearchHit(bookmark_id=b, score=scores[b], index="+".join(indexes[b])) for b in order]


def rrf_hybrid(
    backend: SearchBackend,
    query: str,
    limit: int = 10,
    *,
    k: int = RRF_K,
    depth: int | None = None,
    keyword: Callable[[str, int], list[SearchHit]] | None = None,
) -> list[SearchHit]:
    """The default ``hybrid`` operation, in terms of ``keyword`` + ``semantic``.

    Both rankings are fetched ``depth`` deep (default ``limit *
    DEPTH_FACTOR``) and fused with :func:`rrf_fuse`. When the semantic
    index raises :class:`SearchUnavailable` the result is the keyword
    ranking alone (still RRF-scored, so scores keep one meaning). Other
    exceptions propagate. ``keyword`` overrides the keyword operation,
    for a backend that wants a more forgiving parse of natural-language
    queries.
    """
    if limit <= 0:
        return []
    depth = max(limit, depth if depth is not None else limit * DEPTH_FACTOR)
    keyword_hits = (keyword or backend.keyword)(query, depth)
    try:
        semantic_hits = backend.semantic(query, depth)
    except SearchUnavailable as exc:
        log.warning("hybrid search: semantic index unavailable, keyword only (%s)", exc)
        semantic_hits = []
    return rrf_fuse([keyword_hits, semantic_hits], k=k, limit=limit)


def hybrid_search(backend: SearchBackend, query: str, limit: int = 10) -> list[SearchHit]:
    """Hybrid ranking from any backend: its own ``hybrid`` if it has one
    (:class:`~muninn.search.base.HybridSearchBackend`), else
    :func:`rrf_hybrid`."""
    native = getattr(backend, "hybrid", None)
    if callable(native):
        return native(query, limit)
    return rrf_hybrid(backend, query, limit)


__all__ = ["DEPTH_FACTOR", "RRF_K", "hybrid_search", "rrf_fuse", "rrf_hybrid"]
