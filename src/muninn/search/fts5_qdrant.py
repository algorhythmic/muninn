"""Default search backend: SQLite FTS5 for keyword, Qdrant for semantic.

Both indexes are derived data (ADR-008). Heavy imports — the Qdrant
client and the embedding model — happen inside :meth:`semantic`, so
building this backend (or a keyword-only query) stays light.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

from muninn.config import QDRANT_COLLECTION, QDRANT_URL
from muninn.db import connect
from muninn.search.base import SearchHit, SearchUnavailable
from muninn.search.hybrid import rrf_hybrid

if TYPE_CHECKING:  # pragma: no cover
    from qdrant_client import QdrantClient

_TERM_RE = re.compile(r"\w+")


def fts5_terms_query(query: str) -> str:
    """``query`` as an FTS5 MATCH of its word tokens, each quoted.

    Quoting makes every token a literal string, so punctuation, operators
    and column filters in natural-language text (``C++``, ``what's``,
    ``a-b``, ``NOT``) can't raise a syntax error; tokens are implicitly
    ANDed. Empty when ``query`` has no word characters.
    """
    return " ".join(f'"{term}"' for term in _TERM_RE.findall(query))


class Fts5QdrantBackend:
    """Keyword via ``fts_bookmarks`` (bm25), semantic via the Qdrant collection.

    ``qdrant_client`` injects a client (tests); otherwise one is created
    per semantic query against ``qdrant_url``.
    """

    name = "fts5-qdrant"

    def __init__(
        self,
        *,
        db_path: str | Path | None = None,
        qdrant_client: "QdrantClient | None" = None,
        qdrant_url: str = QDRANT_URL,
        collection_name: str = QDRANT_COLLECTION,
    ) -> None:
        self._db_path = db_path
        self._qdrant_client = qdrant_client
        self._qdrant_url = qdrant_url
        self._collection_name = collection_name

    def keyword(self, query: str, limit: int = 10) -> list[SearchHit]:
        """FTS5 MATCH over title/summary/content_text/tags, best bm25 first.

        Hidden bookmarks are filtered before ``LIMIT``. Invalid MATCH
        syntax raises ``sqlite3.OperationalError``, as it always has.
        """
        conn = connect(self._db_path) if self._db_path else connect()
        try:
            rows = conn.execute(
                "SELECT b.bookmark_id, fts.rank "
                "FROM fts_bookmarks fts "
                "JOIN bookmarks b ON b.bookmark_id = fts.rowid "
                "WHERE fts_bookmarks MATCH ? AND b.content_visible = 1 "
                "ORDER BY rank "
                "LIMIT ?",
                (query, limit),
            ).fetchall()
        finally:
            conn.close()
        # FTS5 rank is bm25, lower = better; negate for higher-is-better.
        return [
            SearchHit(
                bookmark_id=int(r["bookmark_id"]),
                score=-float(r["rank"]) if r["rank"] is not None else None,
                index="fts5",
            )
            for r in rows
        ]

    def hybrid(self, query: str, limit: int = 10) -> list[SearchHit]:
        """Reciprocal rank fusion of :meth:`keyword` + :meth:`semantic`
        (:func:`~muninn.search.hybrid.rrf_hybrid`); keyword-only when
        Qdrant or the embedding backend is unavailable.

        Hybrid queries are usually natural language, so a query that is
        not valid FTS5 syntax is retried as its quoted word tokens
        (:func:`fts5_terms_query`) instead of raising.
        """
        return rrf_hybrid(self, query, limit, keyword=self._keyword_lenient)

    def _keyword_lenient(self, query: str, limit: int) -> list[SearchHit]:
        try:
            return self.keyword(query, limit)
        except sqlite3.OperationalError:
            terms = fts5_terms_query(query)
            return self.keyword(terms, limit) if terms else []

    def semantic(self, query: str, limit: int = 10) -> list[SearchHit]:
        """Embed ``query`` (asymmetric query prompt) and search Qdrant.

        Points without a ``bookmark_id`` payload are skipped. Visibility is
        not known to Qdrant; the caller filters at hydration.
        """
        try:
            from muninn.vector.embed import embed_query

            vector = embed_query(query)
            client = self._qdrant_client
            if client is None:
                from qdrant_client import QdrantClient

                client = QdrantClient(url=self._qdrant_url)
            results = client.query_points(
                collection_name=self._collection_name,
                query=vector,
                limit=limit,
            )
        except Exception as exc:  # noqa: BLE001 — any index failure ⇒ unavailable
            raise SearchUnavailable(f"semantic search unavailable: {exc}") from exc

        hits: list[SearchHit] = []
        for point in getattr(results, "points", []):
            payload = point.payload or {}
            bid = payload.get("bookmark_id")
            if bid is not None:
                hits.append(
                    SearchHit(bookmark_id=int(bid), score=float(point.score), index="qdrant")
                )
        return hits
