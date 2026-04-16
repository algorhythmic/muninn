"""Shared pytest fixtures."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from muninn.db import init_db


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
