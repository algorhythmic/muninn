"""Vector reconcile: backfill missing, refresh stale, drop hidden points.

Runs against qdrant-client's in-process ``:memory:`` mode with the
``hash`` embedding backend: no server, no network, no model.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from muninn.config import QDRANT_COLLECTION
from muninn.db import connect
from muninn.enrich.haiku import build_embedding_text
from muninn.vector.embed import embed_document
from muninn.vector.qdrant import ensure_collection, get_point_payloads, upsert_point
from muninn.vector.reconcile import point_payload, reconcile

qdrant_client = pytest.importorskip("qdrant_client")


@pytest.fixture
def qdrant():
    client = qdrant_client.QdrantClient(":memory:")
    ensure_collection(client)
    yield client
    client.close()


def _seed(db_path: Path) -> None:
    conn = connect(db_path)
    conn.executemany(
        "INSERT INTO bookmarks (bookmark_id, source, source_id, captured_at, title, "
        "  content_visible) VALUES (?, ?, ?, 1, ?, ?)",
        [
            (1, "bookmarks_html", "1", "Browser page", 1),
            (2, "x", "1900000000000000001", None, 1),  # X post: no title
            (3, "x_link", "a" * 64, "Linked page", 1),
            (4, "x_link", "b" * 64, "Hidden page", 0),
        ],
    )
    conn.executemany(
        "INSERT INTO enriched (bookmark_id, summary, tags, content_type, language, "
        "  enrichment_model, enrichment_prompt_version, content_hash, enriched_at) "
        "VALUES (?, ?, ?, 'article', 'en', 'm', 'v', 'h', 1)",
        [(i, f"Summary {i}", json.dumps([f"tag{i}"])) for i in (1, 2, 3, 4)],
    )
    conn.commit()
    conn.close()


def _write_point_like_enrich(client, bid: int, title: str, summary: str, tags: list) -> None:
    payload = point_payload(bid, title=title, summary=summary, tags=tags,
                            content_type="article", language="en")
    upsert_point(client, bid, embed_document(build_embedding_text(title, summary, tags)), payload)


def test_backfills_every_visible_source_and_skips_hidden(db_path: Path, qdrant):
    _seed(db_path)
    stats = reconcile(db_path, client=qdrant)
    assert stats["missing_from_qdrant"] == 3
    assert stats["backfilled"] == 3
    assert stats["errors"] == 0
    payloads = get_point_payloads(qdrant)
    assert set(payloads) == {1, 2, 3}
    assert payloads[2] == {
        "bookmark_id": 2, "title": "", "summary": "Summary 2", "tags": ["tag2"],
        "content_type": "article", "language": "en",
    }


def test_second_run_is_a_noop(db_path: Path, qdrant):
    _seed(db_path)
    reconcile(db_path, client=qdrant)
    stats = reconcile(db_path, client=qdrant)
    assert stats["missing_from_qdrant"] == stats["stale_in_qdrant"] == 0
    assert stats["backfilled"] == stats["refreshed"] == stats["removed_hidden"] == 0


def test_points_written_by_enrich_are_not_stale(db_path: Path, qdrant):
    """Same payload keys and values as enrich_all writes ⇒ nothing to redo."""
    _seed(db_path)
    _write_point_like_enrich(qdrant, 1, "Browser page", "Summary 1", ["tag1"])
    _write_point_like_enrich(qdrant, 2, "", "Summary 2", ["tag2"])
    stats = reconcile(db_path, client=qdrant)
    assert stats["stale_in_qdrant"] == 0
    assert stats["backfilled"] == 1  # only bookmark 3 was missing


def test_stale_point_is_reembedded(db_path: Path, qdrant):
    _seed(db_path)
    reconcile(db_path, client=qdrant)
    old_vector = qdrant.retrieve(QDRANT_COLLECTION, [2], with_vectors=True)[0].vector
    conn = connect(db_path)
    conn.execute("UPDATE enriched SET summary = 'Rewritten', tags = '[\"new\"]' WHERE bookmark_id = 2")
    conn.commit()
    conn.close()

    stats = reconcile(db_path, client=qdrant, dry_run=True)
    assert stats["stale_in_qdrant"] == 1 and stats["refreshed"] == 0

    stats = reconcile(db_path, client=qdrant)
    assert stats["stale_in_qdrant"] == 1
    assert stats["refreshed"] == 1 and stats["backfilled"] == 0
    point = qdrant.retrieve(QDRANT_COLLECTION, [2], with_payload=True, with_vectors=True)[0]
    assert point.payload["summary"] == "Rewritten" and point.payload["tags"] == ["new"]
    assert point.vector != old_vector


def test_point_of_a_row_hidden_after_indexing_is_removed(db_path: Path, qdrant):
    _seed(db_path)
    _write_point_like_enrich(qdrant, 4, "Hidden page", "Summary 4", ["tag4"])
    stats = reconcile(db_path, client=qdrant)
    assert stats["hidden_in_qdrant"] == 1 and stats["removed_hidden"] == 1
    assert 4 not in get_point_payloads(qdrant)


def test_orphan_points_are_reported_not_deleted(db_path: Path, qdrant):
    _seed(db_path)
    _write_point_like_enrich(qdrant, 99, "gone", "gone", [])
    stats = reconcile(db_path, client=qdrant)
    assert stats["extra_in_qdrant"] == 1
    assert 99 in get_point_payloads(qdrant)


def test_unreachable_qdrant_reports_error(db_path: Path):
    _seed(db_path)
    assert reconcile(db_path, client=None) == {"error": "qdrant_unavailable", "enriched_count": 4}


def test_script_wraps_the_library(db_path: Path, qdrant, monkeypatch, capsys):
    path = Path(__file__).resolve().parents[2] / "scripts" / "reconcile-vector-index.py"
    spec = importlib.util.spec_from_file_location("reconcile_script", path)
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    _seed(db_path)
    monkeypatch.setattr("muninn.vector.reconcile.get_client", lambda: qdrant)
    monkeypatch.setattr("sys.argv", ["reconcile", "--db", str(db_path)])
    assert script.main() == 0
    assert json.loads(capsys.readouterr().out)["backfilled"] == 3
