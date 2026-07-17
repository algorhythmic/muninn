"""Tool implementations for the Muninn MCP server.

Pure functions — no MCP decorators here so the same callables can be unit
tested without standing up a stdio transport. `server.py` wraps each one as a
FastMCP tool.

All output is JSON strings (so the MCP layer can hand them straight to the
caller). Schema is the canonical one in /schema.sql.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from muninn.config import QDRANT_COLLECTION, QDRANT_URL
from muninn.db import connect


# ── helpers ────────────────────────────────────────────────────────


def _open(db_path: str | Path | None) -> sqlite3.Connection:
    return connect(db_path) if db_path else connect()


def _row_to_dict(row: sqlite3.Row | None) -> dict:
    return dict(row) if row else {}


def _bookmark_summary(row: sqlite3.Row, score: float | None = None) -> dict:
    """Compact bookmark view for search results."""
    out = {
        "bookmark_id": row["bookmark_id"],
        "title": row["title"],
        "url": row["url"],
        "era_label": row["era_label"],
        "domain": row["domain"],
        "summary": row["summary"] if "summary" in row.keys() else None,
    }
    if score is not None:
        out["score"] = score
    return out


# ── tools ──────────────────────────────────────────────────────────


def semantic_search(
    query: str,
    limit: int = 10,
    db_path: str | Path | None = None,
    client=None,
) -> str:
    """Search bookmarks by semantic similarity using Qdrant.

    The query is embedded locally with the same model (and asymmetric
    query prompt) used to index documents. Falls back to FTS if the
    embedding backend or Qdrant is unavailable or returns nothing.
    Result rows are hydrated from SQLite so the schema is canonical.
    """
    try:
        from muninn.vector.embed import embed_query

        vector = embed_query(query)
        if client is None:
            from qdrant_client import QdrantClient

            client = QdrantClient(url=QDRANT_URL)
        results = client.query_points(
            collection_name=QDRANT_COLLECTION,
            query=vector,
            limit=limit,
        )
        ids_with_scores: list[tuple[int, float]] = []
        for point in getattr(results, "points", []):
            payload = point.payload or {}
            bid = payload.get("bookmark_id")
            if bid is not None:
                ids_with_scores.append((int(bid), float(point.score)))

        if not ids_with_scores:
            return fts_search(query, limit, db_path=db_path)

        conn = _open(db_path)
        try:
            output = []
            for bid, score in ids_with_scores:
                row = conn.execute(
                    "SELECT b.bookmark_id, b.title, b.url, b.era_label, b.domain, "
                    "       e.summary "
                    "FROM bookmarks b LEFT JOIN enriched e "
                    "  ON e.bookmark_id = b.bookmark_id "
                    "WHERE b.bookmark_id = ? AND b.content_visible = 1",
                    (bid,),
                ).fetchone()
                if row is not None:
                    output.append(_bookmark_summary(row, score=score))
            return json.dumps(output, indent=2)
        finally:
            conn.close()
    except Exception:
        return fts_search(query, limit, db_path=db_path)


def fts_search(
    query: str,
    limit: int = 10,
    db_path: str | Path | None = None,
) -> str:
    """Full-text search via the contentless `fts_bookmarks` virtual table."""
    conn = _open(db_path)
    try:
        rows = conn.execute(
            "SELECT b.bookmark_id, b.title, b.url, b.era_label, b.domain, "
            "       e.summary "
            "FROM fts_bookmarks fts "
            "JOIN bookmarks b ON b.bookmark_id = fts.rowid "
            "LEFT JOIN enriched e ON e.bookmark_id = b.bookmark_id "
            "WHERE fts_bookmarks MATCH ? AND b.content_visible = 1 "
            "ORDER BY rank "
            "LIMIT ?",
            (query, limit),
        ).fetchall()
        return json.dumps([_bookmark_summary(r) for r in rows], indent=2)
    finally:
        conn.close()


def get_bookmark(bookmark_id: int, db_path: str | Path | None = None) -> str:
    """Full bookmark view — bookmarks JOIN enriched plus per-pass scrape rows.

    Hidden bookmarks (`content_visible = 0`) are indistinguishable from
    missing IDs: same not-found error, so MCP callers can't enumerate them.
    """
    conn = _open(db_path)
    try:
        row = conn.execute(
            "SELECT b.*, e.summary, e.tags, e.entities, e.content_type, "
            "       e.language, e.word_count, e.enrichment_model, "
            "       e.enrichment_prompt_version, e.enriched_at, "
            "       e.deep_pass_requested, e.key_quotes "
            "FROM bookmarks b LEFT JOIN enriched e "
            "  ON e.bookmark_id = b.bookmark_id "
            "WHERE b.bookmark_id = ? AND b.content_visible = 1",
            (bookmark_id,),
        ).fetchone()
        if row is None:
            return json.dumps({"error": f"Bookmark {bookmark_id} not found"})

        scrape = conn.execute(
            "SELECT pass, scrape_status, fetched_at, http_status, "
            "       extraction_quality, error_detail "
            "FROM scrape_results WHERE bookmark_id = ? ORDER BY fetched_at",
            (bookmark_id,),
        ).fetchall()

        out = _row_to_dict(row)
        out["scrape_results"] = [_row_to_dict(s) for s in scrape]
        return json.dumps(out, indent=2, default=str)
    finally:
        conn.close()


def get_era(era_label: str, db_path: str | Path | None = None) -> str:
    """Era narrative + dominant topics/domains by `era_label` PK."""
    conn = _open(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM eras WHERE era_label = ?", (era_label,)
        ).fetchone()
        if row is None:
            return json.dumps({"error": f"Era '{era_label}' not found"})
        return json.dumps(_row_to_dict(row), indent=2, default=str)
    finally:
        conn.close()


def list_eras(db_path: str | Path | None = None) -> str:
    """All known eras with bookmark counts (live, not the cached
    `eras.bookmark_count`) so re-classified folders are reflected."""
    conn = _open(db_path)
    try:
        rows = conn.execute(
            "SELECT e.era_label, e.inferred_year, e.start_date, e.end_date, "
            "       e.narrative, e.dominant_topics, e.dominant_domains, "
            "       (SELECT COUNT(*) FROM bookmarks "
            "          WHERE era_label = e.era_label) AS bookmark_count "
            "FROM eras e ORDER BY e.start_date NULLS LAST, e.era_label"
        ).fetchall()
        return json.dumps(
            [_row_to_dict(r) for r in rows], indent=2, default=str
        )
    finally:
        conn.close()


__all__ = [
    "semantic_search",
    "fts_search",
    "get_bookmark",
    "get_era",
    "list_eras",
]
