"""Whole-request routing and the bookmark-only compatibility adapter.

Only a typed backend-unavailable outcome authorizes fallback. An empty result
is a successful request, and caller errors never select a different backend.
"""
from __future__ import annotations

from dataclasses import replace

from muninn.search.base import SearchHit
from muninn.search.hybrid import hybrid_search

NAMESPACES = ("bookmarks", "concepts", "infra", "people", "projects")


def _validate(query, namespaces, limit):
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a nonempty string")
    if namespaces is None:
        namespaces = NAMESPACES
    if isinstance(namespaces, str) or not isinstance(namespaces, (tuple, list)):
        raise ValueError("namespaces must be a sequence")
    if not namespaces or any(scope not in NAMESPACES for scope in namespaces):
        raise ValueError("unknown or empty namespace scope")
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("limit must be an integer between 1 and 50")
    return tuple(dict.fromkeys(namespaces))


def route_request(primary, legacy):
    """Share whole-request fallback policy across document and context APIs."""
    from muninn.search.documents import DocumentBackendUnavailable

    try:
        return primary()
    except DocumentBackendUnavailable as exc:
        return replace(legacy(exc.reason), fallback_reason=exc.reason)


class DocumentRouter:
    def __init__(self, primary, legacy):
        self.primary = primary
        self.legacy = legacy

    def search_documents(self, query, namespaces=None, limit=10, options=None):
        scopes = _validate(query, namespaces, limit)
        operation = getattr(self.primary, "search_documents", self.primary)
        return route_request(lambda: operation(query, scopes, limit, options),
                             lambda reason: self.legacy(query, scopes, limit, options))


class BraidBookmarkBackend:
    """Opt-in Braid adapter preserving the existing SearchHit-only seam."""
    name = "braid"

    def __init__(self, *, db_path=None, document_backend=None, legacy=None, **options):
        if document_backend is None:
            from muninn.search.braid import BraidDocumentBackend
            document_backend = BraidDocumentBackend(db_path=db_path, **options)
        if legacy is None:
            from muninn.search.fts5_qdrant import Fts5QdrantBackend
            legacy = Fts5QdrantBackend(db_path=db_path)
        self.document_backend = document_backend
        self.legacy = legacy

    def _search(self, method, query, limit):
        from muninn.search.documents import DocumentBackendUnavailable

        if type(limit) is not int or limit < 0:
            raise ValueError("limit must be a nonnegative integer")
        if limit == 0:
            return []
        _validate(query, ("bookmarks",), min(limit, 50))
        try:
            result = self.document_backend.search_documents(
                query, ("bookmarks",), min(limit, 50), {"mode": method})
        except DocumentBackendUnavailable:
            if method == "hybrid":
                return hybrid_search(self.legacy, query, limit)
            hits = getattr(self.legacy, method)(query, limit)
            # Once the whole request has moved to the legacy semantic path,
            # retain that path's historical empty-semantic -> FTS behavior.
            return hits or self.legacy.keyword(query, limit)
        hits = []
        seen = set()
        for document in result.hits:
            bookmark_id = document.payload.get("bookmark_id")
            if document.namespace != "bookmarks" or type(bookmark_id) is not int or bookmark_id in seen:
                continue
            seen.add(bookmark_id)
            stages = "+".join(document.matched_stages)
            hits.append(SearchHit(bookmark_id, document.score, "braid" + (":" + stages if stages else "")))
            if len(hits) >= limit:
                break
        return hits

    def keyword(self, query, limit=10):
        # This public operation accepts raw FTS MATCH syntax. Braid's lexical
        # parser is a different contract; preserve phrases/operators verbatim.
        return self.legacy.keyword(query, limit)

    def semantic(self, query, limit=10):
        return self._search("semantic", query, limit)

    def hybrid(self, query, limit=10):
        return self._search("hybrid", query, limit)
