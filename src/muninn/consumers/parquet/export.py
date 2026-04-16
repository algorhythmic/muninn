"""Parquet export — DuckDB COPY ... TO over the canonical SQLite store.

Joins `bookmarks` with `enriched`, leaving JSON columns (folder_path, tags,
entities, key_quotes) as VARCHAR for clean round-tripping. Downstream readers
(pandas / pyarrow / DuckDB) all handle JSON-text gracefully.
"""

from __future__ import annotations

from pathlib import Path

from muninn.config import load_paths


def export_parquet(
    out_path: str | Path,
    db_path: str | Path | None = None,
) -> int:
    """Write the join to `out_path` and return the row count."""
    import duckdb

    sqlite_path = str(db_path or load_paths().db_path)
    out = str(out_path)

    Path(out).parent.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect()
    try:
        con.execute(f"ATTACH '{sqlite_path}' AS muninn (TYPE sqlite, READ_ONLY)")
        con.execute(
            f"""
            COPY (
                SELECT
                    b.bookmark_id,
                    b.source,
                    b.source_id,
                    b.captured_at,
                    b.title,
                    b.url,
                    b.folder_path,
                    b.era_label,
                    b.domain,
                    b.content_visible,
                    b.enrichment_source,
                    b.ingested_at,
                    e.summary,
                    e.tags,
                    e.entities,
                    e.content_type,
                    e.language,
                    e.word_count,
                    e.enrichment_model,
                    e.enrichment_prompt_version,
                    e.content_hash,
                    e.enriched_at,
                    e.deep_pass_requested,
                    e.key_quotes
                FROM muninn.bookmarks b
                LEFT JOIN muninn.enriched e
                       ON e.bookmark_id = b.bookmark_id
                ORDER BY b.bookmark_id
            ) TO '{out}' (FORMAT PARQUET)
            """
        )
        row_count = con.execute(
            f"SELECT COUNT(*) FROM read_parquet('{out}')"
        ).fetchone()[0]
        return int(row_count)
    finally:
        con.close()


__all__ = ["export_parquet"]
