"""Unit tests for the scrape pipeline's bookmark selection (--limit)."""

from __future__ import annotations

import sqlite3

from muninn.scrape.pipeline import _list_visible_bookmarks


def _seed(conn: sqlite3.Connection, suffix: str, captured_at: int, visible: int = 1) -> None:
    conn.execute(
        "INSERT INTO bookmarks (source, source_id, captured_at, title, url, content_visible) "
        "VALUES ('netscape', ?, ?, 'T', 'https://example.com/x', ?)",
        (f"s-{suffix}", captured_at, visible),
    )


def test_limit_selects_newest_visible_first(fresh_db):
    for i, ts in enumerate([100, 500, 300, 900, 700]):
        _seed(fresh_db, str(i), ts)
    _seed(fresh_db, "hidden", 9999, visible=0)
    fresh_db.commit()

    rows = _list_visible_bookmarks(fresh_db, limit=3)
    assert [r["captured_at"] for r in rows] == [900, 700, 500]


def test_no_limit_returns_all_visible(fresh_db):
    for i, ts in enumerate([100, 500, 300]):
        _seed(fresh_db, str(i), ts)
    _seed(fresh_db, "hidden", 9999, visible=0)
    fresh_db.commit()

    rows = _list_visible_bookmarks(fresh_db)
    assert len(rows) == 3


def test_limit_larger_than_corpus_is_fine(fresh_db):
    _seed(fresh_db, "only", 42)
    fresh_db.commit()
    assert len(_list_visible_bookmarks(fresh_db, limit=100)) == 1
