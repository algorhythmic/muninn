"""Contract tests for the retrieval interface (ADR-014) and its default backend.

The default ``fts5-qdrant`` backend runs against a real schema-applied DB
for keyword search and a fake Qdrant client (with the ``hash`` embedding
backend from conftest) for semantic search — no network, no model.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from muninn import search
from muninn.config import EMBEDDING_DIM
from muninn.search import (
    DEFAULT_BACKEND,
    SearchBackend,
    SearchBackendError,
    SearchHit,
    SearchUnavailable,
    get_search_backend,
)
from muninn.search.fts5_qdrant import Fts5QdrantBackend


def _seed(db_path: Path) -> None:
    """Three 'quokka' bookmarks (one hidden) plus an unrelated one."""
    now = int(time.time())
    conn = sqlite3.connect(str(db_path))
    try:
        rows = [
            (1, "Quokka rescue", "quokka quokka quokka sanctuary", 1),
            (2, "Island trip", "saw one quokka on the island", 1),
            (3, "Hidden quokka", "quokka quokka quokka quokka quokka", 0),
            (4, "Transformer paper", "attention is all you need", 1),
        ]
        for bid, title, text, visible in rows:
            conn.execute(
                "INSERT INTO bookmarks (bookmark_id, source, source_id, captured_at, "
                "  title, url, content_visible, enrichment_source) "
                "VALUES (?, 'netscape', ?, ?, ?, ?, ?, 'at_capture')",
                (bid, f"s{bid}", now, title, f"https://example.com/{bid}", visible),
            )
            conn.execute(
                "INSERT INTO fts_bookmarks (rowid, title, summary, content_text, tags) "
                "VALUES (?, ?, '', ?, '')",
                (bid, title, text),
            )
        conn.commit()
    finally:
        conn.close()


class _FakeQdrant:
    class _Point:
        def __init__(self, payload, score):
            self.payload = payload
            self.score = score

    class _Results:
        def __init__(self, points):
            self.points = points

    def __init__(self, points=(), *, fail: bool = False):
        self._points = list(points)
        self._fail = fail
        self.calls: list[dict] = []

    def query_points(self, collection_name, query, limit):
        self.calls.append({"collection_name": collection_name, "query": query, "limit": limit})
        if self._fail:
            raise ConnectionError("qdrant down")
        return self._Results([self._Point(p, s) for p, s in self._points])


# ── factory ─────────────────────────────────────────────────────────


def test_default_backend_satisfies_protocol(db_path: Path, monkeypatch):
    monkeypatch.delenv("MUNINN_SEARCH_BACKEND", raising=False)
    backend = get_search_backend(db_path=db_path)
    assert isinstance(backend, Fts5QdrantBackend)
    assert isinstance(backend, SearchBackend)
    assert backend.name == DEFAULT_BACKEND


def test_env_selects_registered_backend(monkeypatch):
    built = {}

    class _Stub:
        name = "stub"

        def __init__(self, *, db_path=None, **options):
            built["db_path"] = db_path
            built["options"] = options

        def keyword(self, query, limit=10):
            return []

        def semantic(self, query, limit=10):
            return []

    monkeypatch.setitem(search._REGISTRY, "stub", _Stub)
    monkeypatch.setenv("MUNINN_SEARCH_BACKEND", "stub")
    backend = get_search_backend(db_path="/x.db", flavour="mint")
    assert isinstance(backend, _Stub)
    assert built == {"db_path": "/x.db", "options": {"flavour": "mint"}}
    # An explicit name beats the environment.
    assert isinstance(get_search_backend(DEFAULT_BACKEND), Fts5QdrantBackend)


def test_register_backend_adds_to_registry(monkeypatch):
    monkeypatch.setattr(search, "_REGISTRY", dict(search._REGISTRY))
    search.register_backend("other", lambda **_: None)  # type: ignore[arg-type,return-value]
    assert "other" in search.available_backends()


def test_unknown_backend_raises(monkeypatch):
    monkeypatch.setenv("MUNINN_SEARCH_BACKEND", "nope")
    with pytest.raises(SearchBackendError, match="nope"):
        get_search_backend()


# ── default backend: keyword ────────────────────────────────────────


def test_keyword_returns_typed_hits_best_first(db_path: Path):
    _seed(db_path)
    hits = Fts5QdrantBackend(db_path=db_path).keyword("quokka")
    assert all(isinstance(h, SearchHit) for h in hits)
    assert [h.bookmark_id for h in hits] == [1, 2]  # denser match ranks first
    assert all(h.index == "fts5" for h in hits)
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True)  # higher is better


def test_keyword_filters_hidden_before_limit(db_path: Path):
    """The hidden row is the strongest match; it must not consume the slot."""
    _seed(db_path)
    hits = Fts5QdrantBackend(db_path=db_path).keyword("quokka", limit=1)
    assert [h.bookmark_id for h in hits] == [1]


def test_keyword_no_match(db_path: Path):
    _seed(db_path)
    assert Fts5QdrantBackend(db_path=db_path).keyword("xyznotpresent") == []


def test_keyword_bad_syntax_raises(db_path: Path):
    _seed(db_path)
    with pytest.raises(sqlite3.OperationalError):
        Fts5QdrantBackend(db_path=db_path).keyword('"unterminated')


# ── default backend: semantic ───────────────────────────────────────


def test_semantic_embeds_query_and_maps_points(db_path: Path):
    fake = _FakeQdrant(
        [({"bookmark_id": 4}, 0.91), ({"no_id": True}, 0.5), ({"bookmark_id": "1"}, 0.42)]
    )
    backend = Fts5QdrantBackend(db_path=db_path, qdrant_client=fake, collection_name="c")
    hits = backend.semantic("attention", limit=7)
    assert hits == [
        SearchHit(bookmark_id=4, score=0.91, index="qdrant"),
        SearchHit(bookmark_id=1, score=0.42, index="qdrant"),
    ]
    call = fake.calls[0]
    assert call["collection_name"] == "c" and call["limit"] == 7
    assert len(call["query"]) == EMBEDDING_DIM
    assert all(isinstance(x, float) for x in call["query"])


def test_semantic_index_failure_is_unavailable(db_path: Path):
    backend = Fts5QdrantBackend(db_path=db_path, qdrant_client=_FakeQdrant(fail=True))
    with pytest.raises(SearchUnavailable):
        backend.semantic("anything")


def test_semantic_embedding_failure_is_unavailable(db_path: Path, monkeypatch):
    monkeypatch.setenv("MUNINN_EMBEDDING_BACKEND", "no-such-backend")
    fake = _FakeQdrant([({"bookmark_id": 1}, 0.9)])
    backend = Fts5QdrantBackend(db_path=db_path, qdrant_client=fake)
    with pytest.raises(SearchUnavailable):
        backend.semantic("anything")
    assert fake.calls == []
