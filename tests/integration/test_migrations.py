"""Schema migrator (RI-01 §4) and its archival regression checks (RI-01 §10).

Synthetic migrations live in a temp dir next to copies of the real ones, so
these tests keep exercising whatever the real head is as migrations land.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from muninn import db
from muninn.consumers.cli.main import cli
from muninn.db import (
    MIGRATIONS_DIR,
    MigrationError,
    SchemaAheadWarning,
    SchemaVersionError,
    connect,
    discover_migrations,
    init_db,
    migrate,
    schema_version,
)

REPO = Path(__file__).resolve().parents[2]
HEAD = len(discover_migrations())

# Additive, and deliberately awkward to split: a trigger body and a string
# literal both contain ";", and so does a comment.
SYNTHETIC = """
-- synthetic migration; additive only
CREATE TABLE synthetic_notes (
    note_id     INTEGER PRIMARY KEY,
    bookmark_id INTEGER REFERENCES bookmarks (bookmark_id) ON DELETE SET NULL,
    body        TEXT NOT NULL DEFAULT 'a; b'
);
ALTER TABLE enriched ADD COLUMN synthetic_flag INTEGER;
CREATE TRIGGER synthetic_notes_ai AFTER INSERT ON synthetic_notes BEGIN
    UPDATE synthetic_notes SET body = body || ';' WHERE note_id = NEW.note_id;
    UPDATE synthetic_notes SET body = trim(body) WHERE note_id = NEW.note_id;
END;
"""


def _dir(tmp_path: Path, upto: int = HEAD, extra: dict[str, str] | None = None) -> Path:
    """Copies of the real migrations 001..upto, plus synthetic extras."""
    root = Path(tempfile.mkdtemp(prefix="migrations-", dir=tmp_path))
    for version, path in discover_migrations():
        if version <= upto:
            shutil.copy(path, root / path.name)
    for name, sql in (extra or {}).items():
        (root / name).write_text(sql)
    return root


def _next(offset: int = 1) -> str:
    return f"{HEAD + offset:03d}"


def _seed(conn: sqlite3.Connection) -> None:
    now = int(time.time())
    for i in range(1, 4):
        conn.execute(
            "INSERT INTO bookmarks (source, source_id, captured_at, title, url, folder_path, domain) "
            "VALUES ('bookmarks_html', ?, ?, ?, ?, '[\"Era\"]', 'e.com')",
            (str(i), now - i * 100, f"Title {i}", f"https://e.com/{i}"),
        )
        conn.execute(
            "INSERT INTO enriched (bookmark_id, summary, tags, enrichment_model, "
            "  enrichment_prompt_version, content_hash, enriched_at) "
            "VALUES (?, ?, '[\"t\"]', 'haiku', 'per_bookmark_v1', ?, ?)",
            (i, f"summary {i}", f"h{i}", now),
        )
        conn.execute(
            "INSERT INTO scrape_results (bookmark_id, pass, fetched_at, scrape_status, content_text) "
            "VALUES (?, 'live', ?, 'ok', ?)",
            (i, now, f"body {i}"),
        )
        conn.execute(
            "INSERT INTO fts_bookmarks (rowid, title, summary, content_text, tags) VALUES (?, ?, ?, ?, 't')",
            (i, f"Title {i}", f"summary {i}", f"body {i}"),
        )
    conn.commit()


_SEEDED = ("bookmarks", "enriched", "scrape_results", "fts_bookmarks")


def _snapshot(conn: sqlite3.Connection, columns: dict[str, list[str]] | None = None):
    """Rows of the seeded tables, restricted to their pre-migration columns."""
    if columns is None:
        columns = {t: [r[1] for r in conn.execute(f"PRAGMA table_info({t})")] for t in _SEEDED}
    rows = {
        t: [tuple(r) for r in conn.execute(f"SELECT rowid, {', '.join(cols)} FROM {t} ORDER BY rowid")]
        for t, cols in columns.items()
    }
    return columns, rows


def _master(path: Path) -> list[tuple]:
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
    finally:
        conn.close()


def _user_version(path: Path) -> int:
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def _legacy_db(path: Path) -> None:
    """A pre-migrator DB: 001 applied by executescript, user_version left 0."""
    conn = sqlite3.connect(str(path))
    conn.executescript((MIGRATIONS_DIR / "001_initial.sql").read_text())
    conn.close()


# ── layout ─────────────────────────────────────────────────────────


def test_schema_sql_mirrors_001_byte_for_byte() -> None:
    assert (REPO / "schema.sql").read_bytes() == (MIGRATIONS_DIR / "001_initial.sql").read_bytes()


def test_discover_sorts_by_number(tmp_path: Path) -> None:
    root = _dir(tmp_path, extra={f"{_next(2)}_b.sql": "", f"{_next()}_a.sql": "", "notes.txt": ""})
    found = discover_migrations(root)
    assert [n for n, _ in found] == list(range(1, HEAD + 3))
    assert found[-1][1].name == f"{_next(2)}_b.sql"


@pytest.mark.parametrize("names", [["001_a.sql", "003_c.sql"], ["001_a.sql", "001_b.sql"], []])
def test_discover_rejects_gaps_duplicates_and_empty(tmp_path: Path, names: list[str]) -> None:
    for name in names:
        (tmp_path / name).write_text("")
    with pytest.raises(MigrationError, match="001..N"):
        discover_migrations(tmp_path)


def test_statement_split_respects_triggers_strings_and_comments() -> None:
    statements = [s for s in db._statements(SYNTHETIC) if s.strip(" \n;")]
    assert len(statements) == 3
    assert all(sqlite3.complete_statement(s) for s in statements)
    assert statements[2].strip().startswith("CREATE TRIGGER") and statements[2].rstrip().endswith("END;")


# ── archival regression (RI-01 §10) ────────────────────────────────


def test_migrating_seeded_001_db_preserves_rows_and_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "muninn.db"
    conn = init_db(path, migrations_dir=_dir(tmp_path, upto=1))
    _seed(conn)
    columns, before = _snapshot(conn)
    conn.close()

    full = _dir(tmp_path, extra={f"{_next()}_synthetic.sql": SYNTHETIC})
    plan = migrate(path, migrations_dir=full)
    assert (plan.current, plan.head) == (1, HEAD + 1)
    assert [p.name for p in plan.pending][-1] == f"{_next()}_synthetic.sql"
    assert plan.backup_path is not None and plan.backup_path.exists()

    conn = connect(path, migrations_dir=full)
    assert schema_version(conn) == HEAD + 1
    assert _snapshot(conn, columns)[1] == before
    # The trigger split correctly: it fires and runs both of its statements.
    conn.execute("INSERT INTO synthetic_notes (bookmark_id, body) VALUES (1, ' x ')")
    assert conn.execute("SELECT body FROM synthetic_notes").fetchone()[0] == "x ;"
    conn.close()

    master = _master(path)
    again = migrate(path, migrations_dir=full)
    assert again.pending == [] and not again.unstamped and again.backup_path is None
    assert _master(path) == master and _user_version(path) == HEAD + 1


@pytest.mark.parametrize("synthetic", [False, True], ids=["real", "real+synthetic"])
def test_fresh_to_head_matches_001_plus_migrations(tmp_path: Path, synthetic: bool) -> None:
    extra = {f"{_next()}_synthetic.sql": SYNTHETIC} if synthetic else None
    full = _dir(tmp_path, extra=extra)
    fresh, stepped = tmp_path / "fresh.db", tmp_path / "stepped.db"
    init_db(fresh, migrations_dir=full).close()
    init_db(stepped, migrations_dir=_dir(tmp_path, upto=1)).close()
    migrate(stepped, migrations_dir=full)
    assert _master(fresh) == _master(stepped)
    assert _user_version(fresh) == _user_version(stepped) == len(discover_migrations(full))


def test_table_rebuild_migration_does_not_cascade(tmp_path: Path) -> None:
    """Migrations run with foreign_keys=OFF, so the CHECK-enum rebuild
    pattern (create, copy, drop, rename) keeps child rows."""
    path = tmp_path / "muninn.db"
    conn = init_db(path)
    _seed(conn)
    ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'bookmarks'").fetchone()[0]
    conn.close()
    rebuild = (
        ddl.replace("CREATE TABLE bookmarks", "CREATE TABLE bookmarks_new", 1) + ";\n"
        "INSERT INTO bookmarks_new SELECT * FROM bookmarks;\n"
        "DROP TABLE bookmarks;\n"
        "ALTER TABLE bookmarks_new RENAME TO bookmarks;\n"
    )
    migrate(path, migrations_dir=_dir(tmp_path, extra={f"{_next()}_rebuild.sql": rebuild}))
    conn = sqlite3.connect(str(path))
    assert conn.execute("SELECT count(*) FROM enriched").fetchone()[0] == 3
    assert conn.execute("SELECT count(*) FROM scrape_results").fetchone()[0] == 3
    conn.close()


# ── atomicity ──────────────────────────────────────────────────────


def test_failing_migration_rolls_back_schema_and_version(tmp_path: Path) -> None:
    path = tmp_path / "muninn.db"
    init_db(path).close()
    master = _master(path)
    broken = (
        "CREATE TABLE half_done (x INTEGER);\n"
        "INSERT INTO half_done VALUES (1);\n"
        "ALTER TABLE bookmarks ADD COLUMN half_done TEXT;\n"
        "CREATE TABLE broken (;\n"
    )
    with pytest.raises(MigrationError, match="rolled back"):
        migrate(path, migrations_dir=_dir(tmp_path, extra={f"{_next()}_broken.sql": broken}))
    assert _master(path) == master
    assert _user_version(path) == HEAD


def test_each_migration_commits_on_its_own(tmp_path: Path) -> None:
    path = tmp_path / "muninn.db"
    init_db(path).close()
    root = _dir(
        tmp_path,
        extra={f"{_next()}_ok.sql": "CREATE TABLE ok_table (x);", f"{_next(2)}_bad.sql": "SELEC 1;"},
    )
    with pytest.raises(MigrationError, match=f"{_next(2)}_bad.sql"):
        migrate(path, migrations_dir=root)
    assert _user_version(path) == HEAD + 1
    assert any(row[1] == "ok_table" for row in _master(path))


def test_foreign_key_violation_rolls_back(tmp_path: Path) -> None:
    path = tmp_path / "muninn.db"
    init_db(path).close()
    orphan = (
        "INSERT INTO enriched (bookmark_id, enrichment_model, enrichment_prompt_version, "
        "content_hash, enriched_at) VALUES (999, 'm', 'v', 'h', 0);"
    )
    with pytest.raises(MigrationError, match="foreign-key"):
        migrate(path, migrations_dir=_dir(tmp_path, extra={f"{_next()}_orphan.sql": orphan}))
    assert _user_version(path) == HEAD
    conn = sqlite3.connect(str(path))
    assert conn.execute("SELECT count(*) FROM enriched").fetchone()[0] == 0
    conn.close()


# ── bootstrap, version checks ──────────────────────────────────────


def test_unstamped_001_db_is_version_1_and_migrate_stamps_it(tmp_path: Path) -> None:
    path = tmp_path / "muninn.db"
    _legacy_db(path)
    one = _dir(tmp_path, upto=1)
    connect(path, migrations_dir=one).close()  # effective v1: readable before stamping
    plan = migrate(path, migrations_dir=one)
    assert plan.unstamped and plan.current == 1 and plan.pending == []
    assert plan.backup_path is None  # stamping alone rewrites no schema
    assert _user_version(path) == 1


def test_unstamped_db_with_pending_migrations_backs_up_as_v1(tmp_path: Path) -> None:
    path = tmp_path / "muninn.db"
    _legacy_db(path)
    plan = migrate(path, migrations_dir=_dir(tmp_path, extra={f"{_next()}_syn.sql": SYNTHETIC}))
    assert plan.unstamped and plan.backup_path.name.startswith("muninn.db.bak-v1-")
    assert _user_version(path) == HEAD + 1


def test_connect_fails_fast_when_behind(tmp_path: Path) -> None:
    path = tmp_path / "muninn.db"
    init_db(path).close()
    ahead_code = _dir(tmp_path, extra={f"{_next()}_syn.sql": SYNTHETIC})
    with pytest.raises(SchemaVersionError, match="run `muninn db migrate`"):
        connect(path, migrations_dir=ahead_code)


def test_connect_warns_and_continues_when_ahead(tmp_path: Path) -> None:
    path = tmp_path / "muninn.db"
    init_db(path, migrations_dir=_dir(tmp_path, extra={f"{_next()}_syn.sql": SYNTHETIC})).close()
    with pytest.warns(SchemaAheadWarning, match="newer than this muninn"):
        conn = connect(path)
    assert conn.execute("SELECT count(*) FROM bookmarks").fetchone()[0] == 0
    conn.close()


def test_connect_refuses_when_min_reader_version_exceeds_code(tmp_path: Path) -> None:
    path = tmp_path / "muninn.db"
    meta = (
        "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);\n"
        f"INSERT INTO schema_meta VALUES ('min_reader_version', '{HEAD + 1}');\n"
    )
    init_db(path, migrations_dir=_dir(tmp_path, extra={f"{_next()}_meta.sql": meta})).close()
    with pytest.raises(SchemaVersionError, match="or newer"):
        connect(path)


def test_connect_refuses_missing_and_empty_dbs(tmp_path: Path) -> None:
    missing = tmp_path / "missing.db"
    with pytest.raises(SchemaVersionError, match="No muninn database"):
        connect(missing)
    assert not missing.exists()  # a typo'd path doesn't leave an empty file behind
    empty = tmp_path / "empty.db"
    sqlite3.connect(str(empty)).close()
    empty.touch()
    with pytest.raises(SchemaVersionError, match="v0 but this muninn needs"):
        connect(empty)


def test_init_db_is_safe_on_a_db_at_head(tmp_path: Path) -> None:
    path = tmp_path / "muninn.db"
    conn = init_db(path)
    _seed(conn)
    conn.close()
    conn = init_db(path)
    assert conn.execute("SELECT count(*) FROM bookmarks").fetchone()[0] == 3
    conn.close()
    assert not list(tmp_path.glob("*.bak-*"))


# ── dry run, backup ────────────────────────────────────────────────


def test_dry_run_changes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "muninn.db"
    conn = init_db(path)
    _seed(conn)
    conn.close()
    before = path.read_bytes()
    plan = migrate(path, migrations_dir=_dir(tmp_path, extra={f"{_next()}_syn.sql": SYNTHETIC}), dry_run=True)
    assert (plan.current, plan.head) == (HEAD, HEAD + 1)
    assert [p.name for p in plan.pending] == [f"{_next()}_syn.sql"]
    assert path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir() if p.is_file()) == ["muninn.db"]

    missing = tmp_path / "new.db"
    plan = migrate(missing, dry_run=True)
    assert plan.current == 0 and len(plan.pending) == HEAD and not missing.exists()


def test_backup_holds_the_pre_migration_db(tmp_path: Path) -> None:
    path = tmp_path / "muninn.db"
    conn = init_db(path)
    _seed(conn)
    conn.close()
    plan = migrate(path, migrations_dir=_dir(tmp_path, extra={f"{_next()}_syn.sql": SYNTHETIC}))
    assert plan.backup_path.parent == tmp_path
    assert plan.backup_path.name.startswith(f"muninn.db.bak-v{HEAD}-")
    backup = sqlite3.connect(str(plan.backup_path))
    assert backup.execute("PRAGMA user_version").fetchone()[0] == HEAD
    assert backup.execute("SELECT count(*) FROM bookmarks").fetchone()[0] == 3
    assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    backup.close()


def test_new_file_is_created_at_head_without_backup(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "muninn.db"
    plan = migrate(path)
    assert (plan.current, plan.head) == (0, HEAD) and plan.backup_path is None
    assert _user_version(path) == HEAD


# ── CLI ────────────────────────────────────────────────────────────


def test_cli_db_migrate_creates_then_is_noop(tmp_path: Path) -> None:
    path = tmp_path / "muninn.db"
    runner = CliRunner()
    dry = runner.invoke(cli, ["--db", str(path), "db", "migrate", "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert "pending: 001_initial.sql" in dry.output and "nothing changed" in dry.output
    assert not path.exists()

    first = runner.invoke(cli, ["--db", str(path), "db", "migrate"])
    assert first.exit_code == 0, first.output
    assert f"Migrated to v{HEAD}." in first.output
    second = runner.invoke(cli, ["--db", str(path), "db", "migrate"])
    assert second.exit_code == 0 and "Nothing to migrate." in second.output

    status = runner.invoke(cli, ["--db", str(path), "db", "status"])
    assert status.exit_code == 0 and f"Schema version: v{HEAD}, head: v{HEAD}" in status.output


def test_cli_behind_db_fails_until_migrated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "muninn.db"
    init_db(path).close()
    monkeypatch.setattr(db, "MIGRATIONS_DIR", _dir(tmp_path, extra={f"{_next()}_syn.sql": SYNTHETIC}))
    runner = CliRunner()

    blocked = runner.invoke(cli, ["--db", str(path), "status"])
    assert isinstance(blocked.exception, SchemaVersionError)

    migrated = runner.invoke(cli, ["--db", str(path), "db", "migrate"])
    assert migrated.exit_code == 0, migrated.output
    assert f"pending: {_next()}_syn.sql" in migrated.output and "Backup:" in migrated.output
    assert runner.invoke(cli, ["--db", str(path), "status"]).exit_code == 0


def test_cli_db_migrate_reports_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "muninn.db"
    init_db(path).close()
    monkeypatch.setattr(db, "MIGRATIONS_DIR", _dir(tmp_path, extra={f"{_next()}_bad.sql": "SELEC 1;"}))
    result = CliRunner().invoke(cli, ["--db", str(path), "db", "migrate"])
    assert result.exit_code == 1 and "rolled back" in result.output
