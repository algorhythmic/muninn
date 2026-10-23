"""Migration 003 (vault lint): a synthesis_runs table rebuild plus two new tables.

Upgrades a real 002 database with existing rows through the migrator
(`muninn.db.migrate`), and checks that every row survives with its id,
the AUTOINCREMENT high-water mark carries over, the CHECK enum now admits
'lint' and nothing else new, FTS and every other table are untouched, and
the lint tables enforce their keys.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
import tempfile
from pathlib import Path

import pytest

from muninn.db import connect, discover_migrations, init_db, migrate

ROOT = Path(__file__).resolve().parents[2]
SQL_003 = ROOT / "schemas" / "sql" / "003_vault_lint.sql"
H1, H2 = "sha256:" + "a" * 64, "sha256:" + "b" * 64


def _migrations_upto(tmp_path: Path, upto: int) -> Path:
    root = Path(tempfile.mkdtemp(prefix="mig-", dir=tmp_path))
    for version, path in discover_migrations():
        if version <= upto:
            shutil.copy(path, root / path.name)
    return root


def _master(conn: sqlite3.Connection) -> dict[tuple[str, str], str | None]:
    return {(r[0], r[1]): r[2] for r in conn.execute("SELECT type, name, sql FROM sqlite_master")}


@pytest.fixture
def db_002(tmp_path: Path) -> Path:
    """A version-2 database with rows in the tables 003 touches or must not."""
    path = tmp_path / "muninn.db"
    conn = init_db(path, migrations_dir=_migrations_upto(tmp_path, 2))
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    conn.execute(
        "INSERT INTO bookmarks (bookmark_id, source, source_id, captured_at, title, url, domain, enrichment_source) "
        "VALUES (1, 'bookmarks_html', '1', 1600000000, 'Synthetic one', 'https://example.org/1', 'example.org', 'at_capture'), "
        "       (2, 'bookmarks_html', '2', 1600000100, 'Synthetic two', 'https://example.org/2', 'example.org', 'at_capture')"
    )
    conn.execute(
        "INSERT INTO enriched (bookmark_id, summary, tags, enrichment_model, enrichment_prompt_version, content_hash, enriched_at) "
        "VALUES (1, 'synthetic summary', '[\"web\"]', 'haiku', 'per_bookmark_v1', 'h1', 1600000200)"
    )
    conn.execute(
        "INSERT INTO fts_bookmarks (rowid, title, summary, content_text, tags) "
        "VALUES (1, 'Synthetic one', 'synthetic summary', 'synthetic body', '[\"web\"]')"
    )
    rows = [
        ("era-1", "era_narrative", 1, "completed", '[{"error": "x"}]', "anthropic-api:msg_1"),
        ("deep-1", "deep_pass", 1, "validation_failed", None, None),
        ("deep-1", "deep_pass", 2, "cap_hit", None, None),
        ("adhoc-1", "ad_hoc_analysis", 1, "running", None, "result:claude-code-local"),
        ("gone-1", "era_narrative", 1, "container_failed", None, None),
    ]
    for task_id, task_type, attempt, status, errors, ref in rows:
        conn.execute(
            "INSERT INTO synthesis_runs (task_id, task_type, attempt, started_at, completed_at, status, "
            "enrichment_model, enrichment_prompt_version, input_token_count, output_token_count, "
            "validation_errors, container_id) VALUES (?, ?, ?, 1600000300, 1600000400, ?, "
            "'claude-opus-5-5', 'synthesis_v1', 10, 20, ?, ?)",
            (task_id, task_type, attempt, status, errors, ref),
        )
    conn.execute("DELETE FROM synthesis_runs WHERE task_id = 'gone-1'")  # high-water mark 5, max id 4
    conn.execute(
        "INSERT INTO usage_records (usage_ulid, kind, provider, operation, model, cost_micro_usd, cost_basis, occurred_at) "
        "VALUES ('01J00000000000000000000001', 'model', 'anthropic', 'synthesize:era_narrative', "
        "'claude-opus-5-5', 14000, 'estimated', 1600000500)"
    )
    conn.commit()
    conn.close()
    return path


def _rows(conn: sqlite3.Connection, table: str) -> list[tuple]:
    return [tuple(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY 1")]


def test_003_is_a_bare_migration_body() -> None:
    text = SQL_003.read_text()
    code = "\n".join(line.split("--", 1)[0] for line in text.splitlines()).upper()
    for word in ("BEGIN", "COMMIT", "ROLLBACK", "PRAGMA", "VIRTUAL", "TRIGGER"):
        assert not re.search(rf"\b{word}\b", code), word
    assert "FTS_BOOKMARKS" not in code
    assert all(line.rstrip().endswith(";") for line in text.splitlines() if ";" in line)


def test_upgrade_from_002_preserves_rows_ids_and_everything_else(db_002: Path) -> None:
    before_conn = sqlite3.connect(db_002)
    before_master = _master(before_conn)
    before = {t: _rows(before_conn, t) for t in
              ("synthesis_runs", "bookmarks", "enriched", "fts_bookmarks", "usage_records")}
    before_conn.close()

    plan = migrate(db_002)
    assert (plan.current, plan.head) == (2, len(discover_migrations()))
    assert [p.name for p in plan.pending][0] == "003_vault_lint.sql"
    assert plan.backup_path is not None and plan.backup_path.exists()

    conn = connect(db_002)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] >= 3
        for table, rows in before.items():
            assert _rows(conn, table) == rows, table
        after_master = _master(conn)
        # Only synthesis_runs (and its index) changed among existing objects.
        for key, sql in before_master.items():
            if key in {("table", "synthesis_runs"), ("index", "idx_synthesis_runs_status")}:
                continue
            assert after_master[key] == sql, key
        assert after_master[("index", "idx_synthesis_runs_status")] == before_master[("index", "idx_synthesis_runs_status")]
        assert "'lint'" in after_master[("table", "synthesis_runs")]
        assert {"lint_pair_checks", "lint_findings"} <= {n for (k, n) in after_master if k == "table"}
        assert "synthesis_runs_new" not in {n for (_, n) in after_master}
        # FTS untouched and still searchable.
        assert conn.execute("SELECT rowid FROM fts_bookmarks WHERE fts_bookmarks MATCH 'synthetic'").fetchone()[0] == 1
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

        # The high-water mark survived the rebuild: no id is ever reused.
        seq = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'synthesis_runs'").fetchone()[0]
        assert seq == 5
        assert conn.execute("SELECT count(*) FROM sqlite_sequence WHERE name LIKE 'synthesis_runs%'").fetchone()[0] == 1
        cur = conn.execute(
            "INSERT INTO synthesis_runs (task_id, task_type, attempt, started_at, status) "
            "VALUES ('lint-1', 'lint', 1, 1, 'running')"
        )
        assert cur.lastrowid == 6
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO synthesis_runs (task_id, task_type, attempt, started_at, status) "
                "VALUES ('x-1', 'other', 1, 1, 'running')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO synthesis_runs (task_id, task_type, attempt, started_at, status) "
                "VALUES ('lint-1', 'lint', 1, 1, 'running')"
            )  # UNIQUE (task_id, attempt) kept
    finally:
        conn.close()
    # Re-running migrate is a no-op.
    assert migrate(db_002).pending == []


def test_upgrade_of_an_empty_synthesis_runs_and_fresh_build_agree(tmp_path: Path) -> None:
    stepped = tmp_path / "stepped.db"
    init_db(stepped, migrations_dir=_migrations_upto(tmp_path, 2)).close()
    migrate(stepped)
    fresh = tmp_path / "fresh.db"
    init_db(fresh).close()
    a, b = sqlite3.connect(stepped), sqlite3.connect(fresh)
    try:
        assert _master(a) == _master(b)
    finally:
        a.close()
        b.close()


def test_lint_tables_enforce_keys_and_cascade(fresh_db: sqlite3.Connection) -> None:
    conn = fresh_db
    conn.execute(
        "INSERT INTO bookmarks (bookmark_id, source, source_id, captured_at) "
        "VALUES (1, 'bookmarks_html', '1', 1), (2, 'bookmarks_html', '2', 2)"
    )
    check = ("INSERT INTO lint_pair_checks (bookmark_id_a, bookmark_id_b, content_hash_a, content_hash_b, "
             "model, prompt_version, task_id, checked_at) VALUES (?, ?, ?, ?, 'm', 'lint_v1', ?, 1)")
    conn.execute(check, (1, 2, H1, H2, "lint-a"))
    for bad in ((2, 1, H1, H2, "lint-b"),          # unordered pair
                (1, 2, "h", H2, "lint-c"),          # not a sha256: hash
                (1, 2, H1, H2, "lint-a"),           # same pair twice in one run
                (1, 3, H1, H2, "lint-d")):          # unknown bookmark
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(check, bad)
    finding = ("INSERT INTO lint_findings (relation, bookmark_id, other_bookmark_id, content_hash, "
               "other_content_hash, rationale, evidence_json, model, prompt_version, task_id, created_at) "
               "VALUES (?, ?, ?, ?, ?, 'r', ?, 'm', 'lint_v1', 'lint-a', 1)")
    conn.execute(finding, ("supersedes", 2, 1, H2, H1, "[]"))
    for bad in (("refutes", 2, 1, H2, H1, "[]"), ("contradicts", 1, 1, H1, H1, "[]"),
                ("contradicts", 1, 2, H1, H2, "not json"), ("supersedes", 2, 1, H2, H1, "[]")):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(finding, bad)
    conn.execute("DELETE FROM bookmarks WHERE bookmark_id = 1")
    assert conn.execute("SELECT count(*) FROM lint_pair_checks").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM lint_findings").fetchone()[0] == 0
