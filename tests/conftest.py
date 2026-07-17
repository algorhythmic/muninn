"""Shared pytest fixtures."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

# The suite must run offline and deterministic: use the hash embedding
# backend unless a test explicitly overrides it.
os.environ.setdefault("MUNINN_EMBEDDING_BACKEND", "hash")

from muninn.db import init_db  # noqa: E402


@pytest.fixture
def fresh_db(tmp_path: Path) -> sqlite3.Connection:
    """A fresh muninn DB with the canonical schema applied."""
    conn = init_db(tmp_path / "muninn.db")
    yield conn
    conn.close()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """A path to a fresh, schema-applied muninn DB. Closed before yield."""
    path = tmp_path / "muninn.db"
    conn = init_db(path)
    conn.close()
    return path
