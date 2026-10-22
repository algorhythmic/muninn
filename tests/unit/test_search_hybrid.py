"""Hybrid ranking (ADR-014): reciprocal rank fusion of keyword + semantic.

Fake backends only, except the last tests, which run the default
``fts5-qdrant`` backend against a real schema-applied DB and a fake
Qdrant client (hash embeddings from conftest).
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from muninn.search import (
    RRF_K,
    HybridSearchBackend,
    SearchBackend,
    SearchHit,
    SearchUnavailable,
    hybrid_search,
    rrf_fuse,
    rrf_hybrid,
)
from muninn.search.fts5_qdrant import Fts5QdrantBackend, fts5_terms_query


def _hits(ids, index):
    return [SearchHit(bookmark_id=b, score=None, index=index) for b in ids]


class _Fake:
    """keyword + semantic only (no native hybrid)."""

    name = "fake"

    def __init__(self, keyword=(), semantic=(), semantic_error=None):
        self._keyword = _hits(keyword, "kw")
        self._semantic = _hits(semantic, "vec")
        self._semantic_error = semantic_error
        self.calls = []

    def keyword(self, query, limit=10):
        self.calls.append(("keyword", query, limit))
        return self._keyword[:limit]

    def semantic(self, query, limit=10):
        self.calls.append(("semantic", query, limit))
        if self._semantic_error is not None:
            raise self._semantic_error
        return self._semantic[:limit]


# ── fusion ──────────────────────────────────────────────────────────


def test_rrf_scores_and_ordering():
    # 3 is 2nd in both lists, so it beats 1 (1st in keyword only).
    fused = rrf_fuse([_hits([1, 3, 2], "kw"), _hits([4, 3, 5], "vec")])
    assert [h.bookmark_id for h in fused] == [3, 1, 4, 2, 5]
    by_id = {h.bookmark_id: h for h in fused}
    assert by_id[3].score == pytest.approx(2 / (RRF_K + 2))
    assert by_id[1].score == pytest.approx(1 / (RRF_K + 1))
    assert by_id[3].index == "kw+vec"
    assert by_id[1].index == "kw" and by_id[4].index == "vec"


def test_rrf_ties_prefer_better_rank_then_keyword_list():
    # 1 and 4 tie (rank 1 each); the keyword list's item wins.
    fused = rrf_fuse([_hits([1, 2], "kw"), _hits([4, 5], "vec")])
    assert [h.bookmark_id for h in fused] == [1, 4, 2, 5]


def test_rrf_duplicates_within_a_list_count_once():
    fused = rrf_fuse([_hits([7, 7, 8], "kw")])
    assert [h.bookmark_id for h in fused] == [7, 8]
    assert fused[0].score == pytest.approx(1 / (RRF_K + 1))
    assert fused[1].score == pytest.approx(1 / (RRF_K + 3))  # rank is positional


def test_rrf_constant_and_limit():
    fused = rrf_fuse([_hits([1, 2, 3], "kw")], k=0, limit=2)
    assert [(h.bookmark_id, h.score) for h in fused] == [(1, 1.0), (2, 0.5)]


# ── default hybrid over a fake backend ──────────────────────────────


def test_rrf_hybrid_fetches_both_lists_deeper_than_limit():
    fake = _Fake(keyword=[1, 2, 3, 4, 5], semantic=[5, 4, 9])
    fused = rrf_hybrid(fake, "q", limit=2)
    assert fake.calls == [("keyword", "q", 4), ("semantic", "q", 4)]
    assert [h.bookmark_id for h in fused] == [4, 1]  # 4: ranks 4 + 2


def test_rrf_hybrid_semantic_unavailable_is_keyword_only():
    fake = _Fake(keyword=[2, 1], semantic_error=SearchUnavailable("qdrant down"))
    fused = rrf_hybrid(fake, "q", limit=5)
    assert [h.bookmark_id for h in fused] == [2, 1]
    assert {h.index for h in fused} == {"kw"}
    assert fused[0].score == pytest.approx(1 / (RRF_K + 1))


def test_rrf_hybrid_other_semantic_errors_propagate():
    fake = _Fake(keyword=[1], semantic_error=KeyError("bug"))
    with pytest.raises(KeyError):
        rrf_hybrid(fake, "q")


def test_rrf_hybrid_zero_limit_queries_nothing():
    fake = _Fake(keyword=[1])
    assert rrf_hybrid(fake, "q", limit=0) == []
    assert fake.calls == []


def test_keyword_semantic_backend_still_satisfies_protocol():
    """``hybrid`` is optional: the protocol didn't grow a required member."""
    fake = _Fake()
    assert isinstance(fake, SearchBackend)
    assert not isinstance(fake, HybridSearchBackend)


def test_hybrid_search_prefers_native_hybrid():
    class _Native(_Fake):
        def hybrid(self, query, limit=10):
            self.calls.append(("hybrid", query, limit))
            return _hits([42], "native")

    native = _Native(keyword=[1], semantic=[2])
    assert isinstance(native, HybridSearchBackend)
    assert [h.bookmark_id for h in hybrid_search(native, "q", 3)] == [42]
    assert native.calls == [("hybrid", "q", 3)]

    plain = _Fake(keyword=[1], semantic=[2])
    assert [h.bookmark_id for h in hybrid_search(plain, "q", 3)] == [1, 2]


# ── default backend ─────────────────────────────────────────────────


class _FakeQdrant:
    class _Point:
        def __init__(self, bid, score):
            self.payload = {"bookmark_id": bid}
            self.score = score

    class _Results:
        def __init__(self, points):
            self.points = points

    def __init__(self, points=(), fail=False):
        self._points = points
        self._fail = fail

    def query_points(self, collection_name, query, limit):
        if self._fail:
            raise ConnectionError("down")
        return self._Results([self._Point(b, s) for b, s in self._points][:limit])


def _seed(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        for bid, title, text in [
            (1, "Quokka rescue", "quokka sanctuary on an island"),
            (2, "C++ templates", "metaprogramming in c++"),
            (3, "Ferry hopping", "ferries between islands"),
        ]:
            conn.execute(
                "INSERT INTO bookmarks (bookmark_id, source, source_id, captured_at, title, "
                "  url, content_visible, enrichment_source) "
                "VALUES (?, 'netscape', ?, ?, ?, ?, 1, 'at_capture')",
                (bid, f"s{bid}", int(time.time()), title, f"https://example.com/{bid}"),
            )
            conn.execute(
                "INSERT INTO fts_bookmarks (rowid, title, summary, content_text, tags) "
                "VALUES (?, ?, '', ?, '')",
                (bid, title, text),
            )
        conn.commit()
    finally:
        conn.close()


def test_default_backend_is_hybrid_and_fuses(db_path: Path):
    _seed(db_path)
    backend = Fts5QdrantBackend(db_path=db_path, qdrant_client=_FakeQdrant([(3, 0.9), (1, 0.5)]))
    assert isinstance(backend, HybridSearchBackend)
    fused = backend.hybrid("island", limit=5)
    # keyword matches only 1 ("islands" is another token without stemming);
    # semantic ranks 3 then 1, so 1 (in both lists) comes first.
    assert [(h.bookmark_id, h.index) for h in fused] == [(1, "fts5+qdrant"), (3, "qdrant")]
    assert fused[0].score == pytest.approx(1 / (RRF_K + 1) + 1 / (RRF_K + 2))


def test_default_backend_hybrid_falls_back_when_qdrant_down(db_path: Path):
    _seed(db_path)
    backend = Fts5QdrantBackend(db_path=db_path, qdrant_client=_FakeQdrant(fail=True))
    fused = backend.hybrid("quokka")
    assert [(h.bookmark_id, h.index) for h in fused] == [(1, "fts5")]


def test_default_backend_hybrid_tolerates_natural_language_syntax(db_path: Path):
    """keyword() keeps raising on bad MATCH syntax; hybrid() retries the
    query as quoted terms instead."""
    _seed(db_path)
    backend = Fts5QdrantBackend(db_path=db_path, qdrant_client=_FakeQdrant())
    with pytest.raises(sqlite3.OperationalError):
        backend.keyword('what about "c++')
    assert [h.bookmark_id for h in backend.hybrid('templates in "c++')] == [2]
    assert backend.hybrid('"(*)"') == []


def test_fts5_terms_query_quotes_every_token():
    assert fts5_terms_query("C++ isn't NOT a-b") == '"C" "isn" "t" "NOT" "a" "b"'
    assert fts5_terms_query("+++") == ""
