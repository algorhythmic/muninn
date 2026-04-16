"""SQLite (canonical) + DuckDB (analytics) connection management."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from muninn.config import load_paths


SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schema.sql"


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open a SQLite connection with operational PRAGMAs applied."""
    if db_path is None:
        db_path = load_paths().db_path
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Apply schema.sql against a fresh DB. Idempotent: schema uses CREATE TABLE
    (no IF NOT EXISTS) so calling this twice on a populated DB will error —
    callers should use a fresh path."""
    conn = connect(db_path)
    conn.executescript(SCHEMA_PATH.read_text())
    conn.commit()
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

    if db_path is None:
        db_path = load_paths().db_path
    con = duckdb.connect()
    con.execute(f"ATTACH '{db_path}' AS sqlite_db (TYPE sqlite, READ_ONLY)")
    return con
