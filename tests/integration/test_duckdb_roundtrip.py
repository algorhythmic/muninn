"""DuckDB ATTACH SQLite — verify JSON columns round-trip cleanly.

Acceptance gate for Decision 2's "consumers/ can ATTACH" promise: every
JSON-shaped TEXT column in the canonical schema must be readable through
DuckDB without escaping/encoding loss.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import duckdb
import pytest

from muninn.ingest import ingest_html
from muninn.scrape.domain_policy import DomainPolicy

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def _seed_full_schema(conn: sqlite3.Connection) -> int:
    """Insert one row in each table that holds a JSON column. Returns the
    `bookmark_id` to use for cross-table reference."""
    # bookmarks — covers folder_path, redacted_param_names, source_metadata.
    conn.execute(
        """INSERT INTO bookmarks (
            source, source_id, captured_at, title, url, folder_path,
            era_label, domain, source_metadata,
            redacted_param_count, redacted_param_names,
            path_redacted, content_visible
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "test_source",
            "test_source_id_001",
            1700000000,
            "Test bookmark",
            "https://example.com/",
            json.dumps(["Era 1", "Sub"]),
            "Era 1",
            "example.com",
            json.dumps({"icon_uri": "https://example.com/favicon.ico", "tags": "a,b"}),
            2,
            json.dumps(["token", "api_key"]),
            0,
            1,
        ),
    )
    bookmark_id = conn.execute(
        "SELECT bookmark_id FROM bookmarks WHERE source_id = ?",
        ("test_source_id_001",),
    ).fetchone()[0]

    # enriched — covers tags, entities, key_quotes.
    conn.execute(
        """INSERT INTO enriched (
            bookmark_id, summary, tags, entities, content_type, language,
            word_count, enrichment_model, enrichment_prompt_version,
            content_hash, enriched_at, deep_pass_requested, key_quotes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            bookmark_id,
            "A test summary",
            json.dumps(["python", "testing"]),
            json.dumps(["DuckDB", "SQLite"]),
            "documentation",
            "en",
            42,
            "claude-haiku-4-5-test",
            "per_bookmark_v1",
            "deadbeef" * 8,
            1700000100,
            0,
            json.dumps(["quoted line one", "quoted line two"]),
        ),
    )

    # eras — covers dominant_topics, dominant_domains.
    conn.execute(
        """INSERT INTO eras (
            era_label, inferred_year, start_date, end_date,
            bookmark_count, dominant_topics, dominant_domains, narrative,
            enrichment_model, enrichment_prompt_version, generated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "Era 1",
            2023,
            1672531200,
            1704067199,
            1,
            json.dumps(["llms", "infra"]),
            json.dumps(["github.com", "anthropic.com"]),
            "Narrative",
            "claude-opus-4-6-test",
            "era_v1",
            1700000200,
        ),
    )

    # analyses — covers filter_query.
    conn.execute(
        """INSERT INTO analyses (
            title, prompt, filter_query, narrative, enrichment_model,
            enrichment_prompt_version
        ) VALUES (?, ?, ?, ?, ?, ?)""",
        (
            "An analysis",
            "summarize the corpus",
            json.dumps({"era": "Era 1", "min_count": 5}),
            "narrative goes here",
            "claude-opus-4-6-test",
            "analysis_v1",
        ),
    )

    # synthesis_runs — covers validation_errors.
    conn.execute(
        """INSERT INTO synthesis_runs (
            task_id, task_type, attempt, started_at, completed_at, status,
            enrichment_model, enrichment_prompt_version,
            input_token_count, output_token_count, validation_errors,
            container_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "task_001", "era_narrative", 1, 1700000300, 1700000400,
            "completed", "claude-opus-4-6-test", "era_v1",
            1000, 200,
            json.dumps([{"path": "/era_label", "msg": "ok"}]),
            "container_abc",
        ),
    )

    conn.commit()
    return bookmark_id


@pytest.fixture
def seeded_db_path(db_path):
    """`db_path` from conftest plus seed rows. Returns the path."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        _seed_full_schema(conn)
    finally:
        conn.close()
    return db_path


class TestDuckDBJSONRoundTrip:
    def test_attach_succeeds(self, seeded_db_path):
        duck = duckdb.connect()
        try:
            duck.execute(f"ATTACH '{seeded_db_path}' AS m (TYPE sqlite, READ_ONLY)")
            tables = duck.execute("SHOW TABLES FROM m").fetchall()
            names = {t[0] for t in tables}
            for expected in [
                "bookmarks", "scrape_results", "enriched", "eras",
                "cross_references", "analyses", "synthesis_runs",
            ]:
                assert expected in names
        finally:
            duck.close()

    @pytest.mark.parametrize(
        "table,column,where_sql,expected_value",
        [
            (
                "bookmarks", "folder_path",
                "source_id = 'test_source_id_001'",
                ["Era 1", "Sub"],
            ),
            (
                "bookmarks", "redacted_param_names",
                "source_id = 'test_source_id_001'",
                ["token", "api_key"],
            ),
            (
                "bookmarks", "source_metadata",
                "source_id = 'test_source_id_001'",
                {"icon_uri": "https://example.com/favicon.ico", "tags": "a,b"},
            ),
            ("enriched", "tags", "1=1", ["python", "testing"]),
            ("enriched", "entities", "1=1", ["DuckDB", "SQLite"]),
            ("enriched", "key_quotes", "1=1", ["quoted line one", "quoted line two"]),
            ("eras", "dominant_topics", "1=1", ["llms", "infra"]),
            ("eras", "dominant_domains", "1=1", ["github.com", "anthropic.com"]),
            ("analyses", "filter_query", "1=1", {"era": "Era 1", "min_count": 5}),
            (
                "synthesis_runs", "validation_errors", "1=1",
                [{"path": "/era_label", "msg": "ok"}],
            ),
        ],
    )
    def test_json_column_roundtrip(
        self, seeded_db_path, table, column, where_sql, expected_value
    ):
        duck = duckdb.connect()
        try:
            duck.execute(f"ATTACH '{seeded_db_path}' AS m (TYPE sqlite, READ_ONLY)")
            row = duck.execute(
                f"SELECT {column} FROM m.{table} WHERE {where_sql} LIMIT 1"
            ).fetchone()
            assert row is not None and row[0] is not None
            assert json.loads(row[0]) == expected_value
        finally:
            duck.close()

    def test_ingest_results_visible_via_duckdb(self, db_path):
        """End-to-end: ingest the canonical fixture, then read folder_path
        through DuckDB to confirm it's parseable JSON."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            ingest_html(FIXTURES / "bookmarks.html", conn, DomainPolicy.empty())
        finally:
            conn.close()

        duck = duckdb.connect()
        try:
            duck.execute(f"ATTACH '{db_path}' AS m (TYPE sqlite, READ_ONLY)")
            rows = duck.execute(
                "SELECT title, folder_path FROM m.bookmarks "
                "WHERE title = 'MDN Web Docs'"
            ).fetchall()
            assert len(rows) == 1
            assert json.loads(rows[0][1]) == ["Jan 1", "Development"]
        finally:
            duck.close()
