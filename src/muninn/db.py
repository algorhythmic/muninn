"""SQLite (canonical) + DuckDB (analytics) connection management.

Schema changes ship as numbered migrations `schemas/sql/NNN_*.sql`; the
applied version lives in `PRAGMA user_version` (RI-01 §4). Migrating is
explicit — `muninn db migrate`, or `init_db()` for tests and scripts — and
never happens on `connect()`, because the CLI, MCP and DuckDB readers share
the file under WAL. `connect()` refuses a DB whose schema is behind the code.
"""

from __future__ import annotations

import re
import sqlite3
import time
import warnings
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from muninn.config import load_paths


MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "schemas" / "sql"
_MIGRATION_NAME = re.compile(r"^(\d{3})_[\w-]+\.sql$")


class SchemaVersionError(RuntimeError):
    """The DB's schema version is incompatible with this build."""


class MigrationError(RuntimeError):
    """A migration failed (and was rolled back) or the result failed checks."""


class SchemaAheadWarning(UserWarning):
    """The DB was migrated by a newer build; additive changes are ignored."""


@dataclass
class MigrationPlan:
    db_path: Path
    current: int  # effective version before migrating (unstamped 001 DB = 1)
    head: int
    pending: list[Path] = field(default_factory=list)
    unstamped: bool = False  # pre-migrator DB at user_version=0: gets stamped 1
    backup_path: Path | None = None


def discover_migrations(migrations_dir: Path | str | None = None) -> list[tuple[int, Path]]:
    """(version, path) pairs sorted by number. Must be exactly 001..N."""
    root = Path(migrations_dir or MIGRATIONS_DIR)
    found = sorted(
        (int(m[1]), p) for p in root.glob("*.sql") if (m := _MIGRATION_NAME.match(p.name))
    )
    if not found or [n for n, _ in found] != list(range(1, len(found) + 1)):
        raise MigrationError(f"Migrations in {root} must be numbered 001..N without gaps or duplicates")
    return found


def schema_version(conn: sqlite3.Connection) -> int:
    """Effective schema version. A DB built from 001 before the migrator
    existed has user_version=0 but already has `bookmarks`: that counts as 1."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == 0 and _has_table(conn, "bookmarks"):
        return 1
    return version


def connect(
    db_path: Path | str | None = None, *, migrations_dir: Path | str | None = None
) -> sqlite3.Connection:
    """Open an existing muninn DB with operational PRAGMAs applied.

    Fails fast if the file is missing or its schema is behind this build
    (run `muninn db migrate`); warns and continues if it is ahead."""
    path = _resolve(db_path)
    if not path.exists():
        raise SchemaVersionError(
            f"No muninn database at {path}: run `muninn db migrate` to create it."
        )
    conn = _open(path)
    try:
        _check_schema(conn, len(discover_migrations(migrations_dir)))
    except Exception:
        conn.close()
        raise
    return conn


def require_current_schema(
    db_path: Path | str | None = None, *, migrations_dir: Path | str | None = None
) -> Path:
    """The cheap version check of `connect()`, for readers that open the file
    some other way (DuckDB ATTACH). Opens read-only and changes nothing.
    Raises SchemaVersionError if the file is missing or behind; returns the path."""
    path = _resolve(db_path)
    if not path.exists():
        raise SchemaVersionError(
            f"No muninn database at {path}: run `muninn db migrate` to create it."
        )
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        _check_schema(conn, len(discover_migrations(migrations_dir)))
    finally:
        conn.close()
    return path


def init_db(
    db_path: Path | str | None = None, *, migrations_dir: Path | str | None = None
) -> sqlite3.Connection:
    """Create the DB if empty, migrate it to head, and return a connection.

    Entry point for tests and scripts/init-db.py; operators upgrading an
    existing DB use `muninn db migrate`. Safe to call on a DB already at head."""
    path = _resolve(db_path)
    conn = _open(path)
    try:
        plan = _migrate(conn, path, migrations_dir, dry_run=False)
        _check_schema(conn, plan.head)
    except Exception:
        conn.close()
        raise
    return conn


def migrate(
    db_path: Path | str | None = None,
    *,
    migrations_dir: Path | str | None = None,
    dry_run: bool = False,
) -> MigrationPlan:
    """Apply pending migrations (backing the file up first). With dry_run,
    only report the plan: a missing file is not created, and an existing one
    is only read (no PRAGMAs, so not even its journal mode changes)."""
    path = _resolve(db_path)
    if dry_run and not path.exists():
        found = discover_migrations(migrations_dir)
        return MigrationPlan(path, 0, len(found), [p for _, p in found])
    conn = sqlite3.connect(str(path)) if dry_run else _open(path)
    try:
        return _migrate(conn, path, migrations_dir, dry_run=dry_run)
    finally:
        conn.close()


def _migrate(
    conn: sqlite3.Connection, path: Path, migrations_dir: Path | str | None, *, dry_run: bool
) -> MigrationPlan:
    migrations = discover_migrations(migrations_dir)
    current = schema_version(conn)
    plan = MigrationPlan(
        db_path=path,
        current=current,
        head=len(migrations),
        pending=[p for n, p in migrations if n > current],
        unstamped=current == 1 and conn.execute("PRAGMA user_version").fetchone()[0] == 0,
    )
    if plan.unstamped:
        # Stamping trusts that the file really is 001: check the part an
        # early build is known to have shaped differently.
        _check_001_shape(conn, migrations[0][1])
    if dry_run or not (plan.pending or plan.unstamped):
        return plan
    if plan.pending and conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone():
        plan.backup_path = _backup(conn, path, current)

    # Manual transaction control. foreign_keys must be OFF (outside any
    # transaction) so a table-rebuild migration's DROP TABLE cannot cascade;
    # violations are caught by foreign_key_check before each COMMIT instead.
    isolation = conn.isolation_level
    conn.isolation_level = None
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        if plan.unstamped:
            _apply(conn, 1, "", "bootstrap stamp")
        for version, mpath in migrations[current:]:
            _apply(conn, version, mpath.read_text(), mpath.name)
        _verify(conn, plan.backup_path)
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.isolation_level = isolation
    return plan


FTS_TABLE = "fts_bookmarks"

# Mirrors what enrich (`_upsert_enriched_row`) and the deep pass write: one
# row per enriched, *visible* bookmark, rowid = bookmark_id, the content of
# the scrape pass its enrichment_source names.
_FTS_REFILL_SQL = """
    INSERT INTO fts_bookmarks (rowid, title, summary, content_text, tags)
    SELECT e.bookmark_id, COALESCE(b.title, ''), COALESCE(e.summary, ''),
           COALESCE(sr.content_text, ''), COALESCE(e.tags, '[]')
    FROM enriched e
    JOIN bookmarks b ON b.bookmark_id = e.bookmark_id
    LEFT JOIN scrape_results sr
           ON sr.bookmark_id = b.bookmark_id
          AND sr.pass = CASE b.enrichment_source
                            WHEN 'live_fallback' THEN 'live'
                            ELSE b.enrichment_source
                        END
    WHERE b.content_visible = 1
    ORDER BY e.bookmark_id
"""


def _normalized_sql(sql: str | None) -> str:
    text = re.sub(r"\s+", " ", (sql or "").strip().rstrip(";")).lower()
    return re.sub(r"\s*([(),=])\s*", r"\1", text)


def _initial_create_sql(initial: Path, name: str) -> str:
    """The `sqlite_master.sql` a DB built from 001 holds for ``name``."""
    pattern = re.compile(rf"\bCREATE\s+(VIRTUAL\s+)?TABLE\s+{re.escape(name)}\b", re.IGNORECASE)
    statement = next((s for s in _statements(initial.read_text()) if pattern.search(s)), None)
    if statement is None:
        raise MigrationError(f"{initial.name} defines no {name} table")
    mem = sqlite3.connect(":memory:")
    try:
        mem.execute(statement)
        return mem.execute("SELECT sql FROM sqlite_master WHERE name = ?", (name,)).fetchone()[0]
    finally:
        mem.close()


def _check_001_shape(conn: sqlite3.Connection, initial: Path) -> None:
    """Refuse to stamp an unstamped DB whose ``fts_bookmarks`` is not 001's.

    Early builds created it contentless (``content=''``); stamped as v1 it
    would then fail every FTS DELETE (re-enrichment, deep pass)."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (FTS_TABLE,)
    ).fetchone()
    expected = _initial_create_sql(initial, FTS_TABLE)
    if row is None or _normalized_sql(row[0]) != _normalized_sql(expected):
        found = "is missing" if row is None else f"is `{' '.join(str(row[0]).split())}`"
        raise MigrationError(
            f"This unstamped database predates the migrator and its {FTS_TABLE} {found}, "
            f"not 001's `{' '.join(expected.split())}` (an early contentless FTS index "
            "rejects the DELETEs re-enrichment needs). Nothing was changed. Run "
            "`muninn db rebuild-fts` (backs the file up, recreates the index as 001 "
            "defines it and refills it from enriched rows), then `muninn db migrate`."
        )


@dataclass
class FtsRebuild:
    db_path: Path
    rows: int
    backup_path: Path | None


def rebuild_fts(
    db_path: Path | str | None = None, *, migrations_dir: Path | str | None = None
) -> FtsRebuild:
    """Drop ``fts_bookmarks`` and recreate it exactly as 001 defines it, then
    refill it from ``enriched`` (visible rows only, as enrich indexes them).

    Derived data only, so nothing is lost; the file is still backed up
    first. Works on a DB of any version (it is the fix for an unstamped DB
    the migrator refuses) and never changes ``user_version``."""
    path = _resolve(db_path)
    if not path.exists():
        raise SchemaVersionError(
            f"No muninn database at {path}: run `muninn db migrate` to create it."
        )
    initial = discover_migrations(migrations_dir)[0][1]
    create_sql = _initial_create_sql(initial, FTS_TABLE)
    conn = _open(path)
    try:
        for table in ("bookmarks", "enriched", "scrape_results"):
            if not _has_table(conn, table):
                raise MigrationError(f"{path} has no {table} table; not a muninn database")
        backup = _backup(conn, path, schema_version(conn))
        isolation = conn.isolation_level
        conn.isolation_level = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(f"DROP TABLE IF EXISTS {FTS_TABLE}")
                conn.execute(create_sql)
                rows = conn.execute(_FTS_REFILL_SQL).rowcount
                conn.execute("COMMIT")
            except Exception as exc:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise MigrationError(f"Rebuilding {FTS_TABLE} failed and was rolled back: {exc}") from exc
        finally:
            conn.isolation_level = isolation
    finally:
        conn.close()
    return FtsRebuild(db_path=path, rows=rows, backup_path=backup)


def _apply(conn: sqlite3.Connection, version: int, sql: str, name: str) -> None:
    """One migration plus its user_version bump, atomically. Not
    executescript: it commits on entry and can't be rolled back as a unit."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        for statement in _statements(sql):
            conn.execute(statement)
        if conn.execute("PRAGMA foreign_key_check").fetchone():
            raise MigrationError("leaves foreign-key violations")
        conn.execute(f"PRAGMA user_version = {int(version)}")
        conn.execute("COMMIT")
    except Exception as exc:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise MigrationError(f"Migration {name} failed and was rolled back: {exc}") from exc


def _statements(sql: str) -> Iterator[str]:
    """Split a script into single statements. sqlite3.complete_statement
    knows about string literals, comments and CREATE TRIGGER … END bodies."""
    buf = ""
    for piece in sql.split(";"):
        buf += piece + ";"
        if sqlite3.complete_statement(buf):
            yield buf
            buf = ""
    if buf:  # trailing text: drop the ";" added above; SQLite rejects it if truncated
        yield buf[:-1]


def _verify(conn: sqlite3.Connection, backup_path: Path | None) -> None:
    problems = [r[0] for r in conn.execute("PRAGMA integrity_check")]
    if problems != ["ok"]:
        raise MigrationError(
            f"integrity_check failed after migrating: {problems[:5]}; restore {backup_path}"
        )
    if conn.execute("PRAGMA foreign_key_check").fetchone():
        raise MigrationError(f"foreign_key_check failed after migrating; restore {backup_path}")


def _backup(conn: sqlite3.Connection, path: Path, version: int) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    dest = path.with_name(f"{path.name}.bak-v{version}-{stamp}")
    target = sqlite3.connect(str(dest))
    try:
        conn.backup(target)
    finally:
        target.close()
    return dest


def _check_schema(conn: sqlite3.Connection, head: int) -> None:
    current = schema_version(conn)
    minimum = _min_reader_version(conn)
    if head < minimum:
        raise SchemaVersionError(
            f"Database schema v{current} requires muninn with schema v{minimum} or newer "
            f"(this build: v{head}). Upgrade muninn or restore a backup."
        )
    if current < head:
        raise SchemaVersionError(
            f"Database schema is v{current} but this muninn needs v{head}: run `muninn db migrate`."
        )
    if current > head:
        warnings.warn(
            f"Database schema v{current} is newer than this muninn (v{head}); continuing.",
            SchemaAheadWarning,
            stacklevel=3,
        )


def _min_reader_version(conn: sqlite3.Connection) -> int:
    """Oldest schema version a reader may have. Only a non-additive migration
    sets it, in schema_meta (no migration creates that table yet)."""
    if not _has_table(conn, "schema_meta"):
        return 1
    row = conn.execute("SELECT value FROM schema_meta WHERE key = 'min_reader_version'").fetchone()
    return int(row[0]) if row else 1


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    query = "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?"
    return conn.execute(query, (name,)).fetchone() is not None


def _resolve(db_path: Path | str | None) -> Path:
    return Path(db_path) if db_path is not None else load_paths().db_path


def _open(path: Path) -> sqlite3.Connection:
    """Open (creating if needed) with operational PRAGMAs; no schema check."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Explicit transaction wrapper. Commits on success, rolls back on error."""
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def duckdb_attached(db_path: Path | str | None = None):
    """DuckDB connection with the SQLite store attached read-only as
    `sqlite_db`. Used by analytics consumers (timeline, parquet)."""
    import duckdb

    db_path = require_current_schema(db_path)
    con = duckdb.connect()
    con.execute(f"ATTACH '{db_path}' AS sqlite_db (TYPE sqlite, READ_ONLY)")
    return con
