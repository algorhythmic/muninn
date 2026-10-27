from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from muninn.search import DEFAULT_BACKEND, SearchHit, available_backends, get_search_backend
from muninn.search.routing import BraidBookmarkBackend, DocumentRouter


@dataclass
class Result:
    hits: tuple = ()
    fallback_reason: object = None


def test_router_empty_primary_does_not_fallback():
    calls = []
    primary = lambda *args: calls.append(("primary", args)) or Result()
    legacy = lambda *args: calls.append(("legacy", args)) or Result()
    assert DocumentRouter(primary, legacy).search_documents("synthetic", ("concepts",)).hits == ()
    assert [name for name, _ in calls] == ["primary"]


def test_router_only_typed_unavailability_falls_back_whole_request():
    from muninn.search.documents import DocumentBackendUnavailable
    calls = []
    reason = {"code": "synthetic_missing_generation"}
    def primary(*args):
        calls.append(("primary", args))
        raise DocumentBackendUnavailable(reason)
    def legacy(*args):
        calls.append(("legacy", args))
        return Result(("legacy-all-scopes",))
    result = DocumentRouter(primary, legacy).search_documents("query", ("bookmarks", "people"), 5)
    assert result.hits == ("legacy-all-scopes",)
    assert result.fallback_reason == reason
    assert calls[0][1] == calls[1][1]


@pytest.mark.parametrize("kwargs", [{"query": " "}, {"query": "q", "namespaces": ("bad",)},
    {"query": "q", "namespaces": ()}, {"query": "q", "limit": 0}, {"query": "q", "limit": True}])
def test_invalid_request_never_calls_either_backend(kwargs):
    def forbidden(*args):
        raise AssertionError("backend must not be called")
    with pytest.raises(ValueError):
        DocumentRouter(forbidden, forbidden).search_documents(**kwargs)


def test_programming_failure_propagates_without_fallback():
    def broken(*args):
        raise RuntimeError("synthetic implementation error")
    def forbidden(*args):
        raise AssertionError("must not fallback")
    with pytest.raises(RuntimeError, match="implementation error"):
        DocumentRouter(broken, forbidden).search_documents("q")


def test_bookmark_adapter_returns_only_deduplicated_bookmark_hits():
    calls = []
    def document(bid, namespace="bookmarks"):
        return SimpleNamespace(namespace=namespace, payload={"bookmark_id": bid},
            matched_stages=("dense",), score=.5)
    class Primary:
        def search_documents(self, *args):
            calls.append(args)
            return Result((document(1), document(1), document(2, "concepts"), document(3)))
    adapter = BraidBookmarkBackend(document_backend=Primary(), legacy=object())
    assert [hit.bookmark_id for hit in adapter.hybrid("q", 10)] == [1, 3]
    assert all(isinstance(hit, SearchHit) for hit in adapter.hybrid("q", 10))
    assert calls[0][1] == ("bookmarks",)
    assert calls[0][3] == {"mode": "hybrid"}
    assert adapter.semantic("q", 0) == []


def test_bookmark_adapter_fallback_directly_calls_injected_legacy():
    from muninn.search.documents import DocumentBackendUnavailable
    class Primary:
        def search_documents(self, *args):
            raise DocumentBackendUnavailable({"code": "unavailable"})
    class Legacy:
        def keyword(self, query, limit):
            return [SearchHit(7, .1, "fts5")]
        def semantic(self, query, limit):
            return [SearchHit(8, .2, "qdrant")]
    adapter = BraidBookmarkBackend(document_backend=Primary(), legacy=Legacy())
    assert adapter.keyword("q")[0].bookmark_id == 7
    assert adapter.semantic("q")[0].bookmark_id == 8
    assert {hit.bookmark_id for hit in adapter.hybrid("q")} == {7, 8}


def test_registry_optin_preserves_default():
    assert DEFAULT_BACKEND == "fts5-qdrant"
    assert "braid" in available_backends()
    adapter = get_search_backend("braid", document_backend=object(), legacy=object())
    assert isinstance(adapter, BraidBookmarkBackend)


def test_keyword_preserves_raw_fts_syntax_and_never_calls_braid():
    calls = []
    class Primary:
        def search_documents(self, *args):
            raise AssertionError("keyword must bypass Braid")
    class Legacy:
        def keyword(self, query, limit):
            calls.append((query, limit))
            return [SearchHit(42, .2, "fts5")]
    adapter = BraidBookmarkBackend(document_backend=Primary(), legacy=Legacy())
    query = 'title:"synthetic phrase" OR quokka NOT wallaby'
    assert adapter.keyword(query, 100) == [SearchHit(42, .2, "fts5")]
    assert calls == [(query, 100)]


def test_semantic_empty_success_does_not_call_legacy():
    class Primary:
        def search_documents(self, *args):
            return Result()
    class Legacy:
        def semantic(self, *args):
            raise AssertionError("empty Braid success must not fallback")
        def keyword(self, *args):
            raise AssertionError("empty Braid success must not fallback")
    adapter = BraidBookmarkBackend(document_backend=Primary(), legacy=Legacy())
    assert adapter.semantic("synthetic") == []


def test_unavailable_braid_and_empty_legacy_semantic_uses_fts():
    from muninn.search.documents import DocumentBackendUnavailable
    calls = []
    class Primary:
        def search_documents(self, *args):
            raise DocumentBackendUnavailable("generation_unavailable")
    class Legacy:
        def semantic(self, query, limit):
            calls.append("semantic")
            return []
        def keyword(self, query, limit):
            calls.append("keyword")
            return [SearchHit(42, .2, "fts5")]
    adapter = BraidBookmarkBackend(document_backend=Primary(), legacy=Legacy())
    assert adapter.semantic("synthetic") == [SearchHit(42, .2, "fts5")]
    assert calls == ["semantic", "keyword"]
