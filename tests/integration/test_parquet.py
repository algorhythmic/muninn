"""Integration tests for the DuckDB-driven parquet exporter."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import duckdb
import pyarrow.parquet as pq

from muninn.consumers.parquet.export import export_parquet


def _seed(db_path: Path) -> None:
    now = int(time.time())
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executemany(
            "INSERT INTO bookmarks (source, source_id, captured_at, title, url, "
            "  era_label, domain, content_visible, enrichment_source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("netscape", "1", now - 9000, "A", "https://e.com/a",
                 "early-web", "e.com", 1, "at_capture"),
                ("netscape", "2", now - 8000, "B", "https://e.com/b",
                 "early-web", "e.com", 1, "at_capture"),
                ("netscape", "3", now - 3000, "C", "https://x.com/c",
                 "ai-era", "x.com", 0, "none"),
            ],
        )
        conn.executemany(
            "INSERT INTO enriched (bookmark_id, summary, tags, entities, key_quotes, "
            "  enrichment_model, enrichment_prompt_version, content_hash, enriched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (1, "summary A", json.dumps(["web", "history"]),
                 json.dumps(["E1"]), json.dumps(["q"]),
                 "haiku", "v1", "h1", now),
                (2, "summary B", json.dumps(["web"]),
                 None, None, "haiku", "v1", "h2", now),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def test_export_creates_file(db_path: Path, tmp_path: Path):
    _seed(db_path)
    out = tmp_path / "export.parquet"
    count = export_parquet(out_path=out, db_path=db_path)
    assert out.exists()
    assert count == 3  # all bookmarks (left join)


def test_parquet_readable_by_duckdb(db_path: Path, tmp_path: Path):
    _seed(db_path)
    out = tmp_path / "export.parquet"
    export_parquet(out_path=out, db_path=db_path)

    con = duckdb.connect()
    rows = con.execute(f"SELECT * FROM read_parquet('{out}')").fetchall()
    con.close()
    assert len(rows) == 3


def test_parquet_readable_by_pyarrow(db_path: Path, tmp_path: Path):
    _seed(db_path)
    out = tmp_path / "export.parquet"
    export_parquet(out_path=out, db_path=db_path)

    table = pq.read_table(str(out))
    assert table.num_rows == 3
    cols = set(table.column_names)
    assert {"bookmark_id", "tags", "entities", "key_quotes", "summary"}.issubset(cols)


def test_parquet_readable_by_pandas(db_path: Path, tmp_path: Path):
    """SC8: parquet must be readable by pandas."""
    pd = __import__("pandas")
    _seed(db_path)
    out = tmp_path / "export.parquet"
    export_parquet(out_path=out, db_path=db_path)

    df = pd.read_parquet(str(out))
    assert len(df) == 3
    for val in df["tags"].dropna():
        parsed = json.loads(val)
        assert isinstance(parsed, list)


def test_parquet_json_roundtrip(db_path: Path, tmp_path: Path):
    _seed(db_path)
    out = tmp_path / "export.parquet"
    export_parquet(out_path=out, db_path=db_path)

    con = duckdb.connect()
    rows = con.execute(
        f"SELECT tags, entities, key_quotes FROM read_parquet('{out}') "
        f"WHERE tags IS NOT NULL"
    ).fetchall()
    con.close()
    for tags_str, _, _ in rows:
        parsed = json.loads(tags_str)
        assert isinstance(parsed, list)
