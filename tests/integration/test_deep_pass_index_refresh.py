"""A deep pass's new summary/tags must reach the search indexes.

``deep_pass.write_output`` rewrites the FTS row in the same transaction
as ``enriched``; the Qdrant point goes stale and the vector reconcile
re-embeds it (no network inside the SQL transaction).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from muninn.db import connect
from muninn.enrich.pipeline import enrich_all
from muninn.synthesis import run_deep_pass
from muninn.vector.qdrant import ensure_collection, get_point_payloads
from muninn.vector.reconcile import reconcile
from tests.integration.test_deep_pass_idempotency import (
    _OneShotLauncher,
    _deep_pass_output,
    _fake_enrich,
    _seed,
)

qdrant_client = pytest.importorskip("qdrant_client")

DEEP_SUMMARY = (
    "The deep pass finds the zanzibarite argument central to the whole piece, "
    "and traces how the author builds toward it across sections."
)


def _fts_ids(conn, query: str) -> list[int]:
    return [r[0] for r in conn.execute(
        "SELECT rowid FROM fts_bookmarks WHERE fts_bookmarks MATCH ?", (query,)
    )]


def _deep_output() -> dict:
    out = _deep_pass_output()
    out["summary"] = DEEP_SUMMARY
    out["tags"] = ["quarrystone"]
    return out


@pytest.fixture
def qdrant():
    client = qdrant_client.QdrantClient(":memory:")
    ensure_collection(client)
    yield client
    client.close()


@patch("muninn.enrich.pipeline.enrich_bookmark")
def test_deep_pass_refreshes_fts_and_reconcile_refreshes_qdrant(
    mock_enrich, db_path, qdrant
):
    conn = connect(db_path)  # reconcile opens the same file by path
    _seed(conn)
    mock_enrich.side_effect = _fake_enrich
    enrich_all(conn, client=MagicMock(), qdrant=qdrant)
    assert _fts_ids(conn, "Haiku") == [1]
    assert _fts_ids(conn, "zanzibarite") == []
    assert get_point_payloads(qdrant)[1]["summary"] == "Haiku summary of Deep article"

    result = run_deep_pass(1, conn=conn, launcher=_OneShotLauncher(_deep_output()),
                           candidate_neighbor_ids=[])
    assert result.status == "completed"

    # FTS: the deep summary and tags are searchable, the old summary is gone,
    # and the content text is still indexed.
    assert _fts_ids(conn, "zanzibarite") == [1]
    assert _fts_ids(conn, "quarrystone") == [1]
    assert _fts_ids(conn, "Haiku") == []
    assert _fts_ids(conn, "verbatim") == [1]
    assert conn.execute("SELECT COUNT(*) FROM fts_bookmarks").fetchone()[0] == 1

    # Qdrant: the point is stale until reconcile re-embeds it.
    assert get_point_payloads(qdrant)[1]["summary"] == "Haiku summary of Deep article"
    stats = reconcile(db_path, client=qdrant)
    assert stats["stale_in_qdrant"] == 1 and stats["refreshed"] == 1
    payload = get_point_payloads(qdrant)[1]
    assert payload["summary"] == DEEP_SUMMARY and payload["tags"] == ["quarrystone"]
    assert reconcile(db_path, client=qdrant)["refreshed"] == 0
    conn.close()


def test_deep_pass_on_hidden_row_drops_its_fts_row(fresh_db):
    _seed(fresh_db)
    fresh_db.execute(
        "INSERT INTO fts_bookmarks (rowid, title, summary, content_text, tags) "
        "VALUES (1, 'Deep article', 'old', 'old body', '[]')"
    )
    fresh_db.execute("UPDATE bookmarks SET content_visible = 0 WHERE bookmark_id = 1")
    fresh_db.commit()
    result = run_deep_pass(1, conn=fresh_db, launcher=_OneShotLauncher(_deep_output()),
                           candidate_neighbor_ids=[])
    assert result.status == "completed"
    assert fresh_db.execute("SELECT COUNT(*) FROM fts_bookmarks").fetchone()[0] == 0


def test_failed_deep_pass_leaves_fts_untouched(fresh_db):
    _seed(fresh_db)
    fresh_db.execute(
        "INSERT INTO fts_bookmarks (rowid, title, summary, content_text, tags) "
        "VALUES (1, 'Deep article', 'old summary', 'old body', '[]')"
    )
    fresh_db.commit()
    bad = _deep_output()
    bad["key_quotes"] = ["a quote that is not in the source"]
    result = run_deep_pass(1, conn=fresh_db, launcher=_OneShotLauncher(bad),
                           candidate_neighbor_ids=[])
    assert result.status != "completed"
    assert _fts_ids(fresh_db, "old") == [1]
    assert _fts_ids(fresh_db, "zanzibarite") == []
