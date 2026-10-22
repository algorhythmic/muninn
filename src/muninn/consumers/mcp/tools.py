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

from muninn.consumers.vault import x_pages
from muninn.db import connect
from muninn.search import DEFAULT_BACKEND, SearchBackend, SearchHit, get_search_backend


# ── helpers ────────────────────────────────────────────────────────


def _open(db_path: str | Path | None) -> sqlite3.Connection:
    return connect(db_path) if db_path else connect()


def _row_to_dict(row: sqlite3.Row | None) -> dict:
    return dict(row) if row else {}


def _item_kind(row: sqlite3.Row) -> str | None:
    """Contract item kind (``x_post``, ``x_article``, ``github_repo``,
    ``web_page``) for X-lane rows; None for browser bookmarks."""
    if row["source"] not in x_pages.X_SOURCES:
        return None
    return x_pages.item_kind_for(row)


def _title(conn: sqlite3.Connection, row: sqlite3.Row) -> str | None:
    """``bookmarks.title``. An X post's title is the start of its text, so
    a locator of a link hidden since the last sync is masked as in the
    vault; a post without a title (known only from an ``errors[]`` entry or
    a bare reference) falls back to ``@handle``."""
    if row["source"] != "x":
        return row["title"]
    if not row["title"]:
        return x_pages.display_name(row)
    return x_pages.mask_hidden_title(row["title"], x_pages.hidden_child_urls(conn, row["bookmark_id"]))


def _bookmark_summary(
    conn: sqlite3.Connection, row: sqlite3.Row, score: float | None = None
) -> dict:
    """Compact bookmark view for search results."""
    out = {
        "bookmark_id": row["bookmark_id"],
        "title": _title(conn, row),
        "url": row["url"],
        "era_label": row["era_label"],
        "domain": row["domain"],
        "summary": row["summary"] if "summary" in row.keys() else None,
        "source": row["source"],
        "item_kind": _item_kind(row),
    }
    if score is not None:
        out["score"] = score
    return out


def _backend(
    backend: SearchBackend | None,
    db_path: str | Path | None,
    client=None,
) -> SearchBackend:
    """Resolve the search backend: explicit > legacy Qdrant client > config."""
    if backend is not None:
        return backend
    if client is not None:
        # Legacy hook: a bare Qdrant client means the default backend.
        return get_search_backend(DEFAULT_BACKEND, db_path=db_path, qdrant_client=client)
    return get_search_backend(db_path=db_path)


def _hydrate(
    db_path: str | Path | None,
    hits: list[SearchHit],
    *,
    with_score: bool,
) -> list[dict]:
    """Hydrate hits from SQLite, in hit order, dropping hidden/missing rows.

    This is the visibility gate for every backend: an index that doesn't
    know about ``content_visible`` can never leak a hidden bookmark.
    """
    conn = _open(db_path)
    try:
        output = []
        for hit in hits:
            row = conn.execute(
                "SELECT b.bookmark_id, b.title, b.url, b.era_label, b.domain, "
                "       b.source, b.source_id, b.source_metadata, e.summary "
                "FROM bookmarks b LEFT JOIN enriched e "
                "  ON e.bookmark_id = b.bookmark_id "
                "WHERE b.bookmark_id = ? AND b.content_visible = 1",
                (hit.bookmark_id,),
            ).fetchone()
            if row is not None:
                output.append(
                    _bookmark_summary(conn, row, score=hit.score if with_score else None)
                )
        return output
    finally:
        conn.close()


# ── tools ──────────────────────────────────────────────────────────


def semantic_search(
    query: str,
    limit: int = 10,
    db_path: str | Path | None = None,
    client=None,
    backend: SearchBackend | None = None,
) -> str:
    """Search bookmarks by semantic similarity via the search backend.

    The default backend embeds the query locally with the same model (and
    asymmetric query prompt) used to index documents, then queries Qdrant.
    Falls back to keyword search if the semantic index is unavailable or
    returns nothing. Result rows are hydrated from SQLite so the schema is
    canonical. ``client`` injects a Qdrant client into the default backend.
    """
    search = _backend(backend, db_path, client)
    try:
        hits = search.semantic(query, limit)
        if not hits:
            return fts_search(query, limit, db_path=db_path, backend=search)
        return json.dumps(_hydrate(db_path, hits, with_score=True), indent=2)
    except Exception:
        return fts_search(query, limit, db_path=db_path, backend=search)


def fts_search(
    query: str,
    limit: int = 10,
    db_path: str | Path | None = None,
    backend: SearchBackend | None = None,
) -> str:
    """Keyword search via the search backend (FTS5 ``fts_bookmarks`` by default)."""
    hits = _backend(backend, db_path).keyword(query, limit)
    return json.dumps(_hydrate(db_path, hits, with_score=False), indent=2)


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
        out["title"] = _title(conn, row)
        out["item_kind"] = _item_kind(row)
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
