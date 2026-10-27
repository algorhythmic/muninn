"""Synthetic freshness/status tests; no Braid, model downloads or live corpus."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from muninn.config import load_braid_settings, load_paths
from muninn.index.freshness import assess
from muninn.index.paths import load_index_paths
from muninn.index.status import index_status


def manifest(conn, **kwargs):
    epoch = conn.execute("SELECT source_epoch FROM index_corpus").fetchone()[0]
    return SimpleNamespace(source_epoch=epoch, namespaces=("bookmarks",),
                           authored_inventory_digest=None, omissions=(), **kwargs)


def test_current_empty_captured_scope_is_distinct_from_failed_authored_scan(fresh_db, monkeypatch):
    m = manifest(fresh_db)
    assert assess(fresh_db, m).current
    m.namespaces = ("concepts",)
    monkeypatch.delenv("MUNINN_VAULT_DIR", raising=False)
    assert assess(fresh_db, m).reason == "authored_inventory_unavailable"


def test_epoch_and_partial_scope_fail_closed(fresh_db):
    m = manifest(fresh_db)
    assert assess(fresh_db, m, ("concepts",)).reason == "scope_unavailable"
    m.omissions = ({"namespace": "bookmarks", "reason": "content_unavailable"},)
    assert assess(fresh_db, m).reason == "incomplete_coverage"
    m.omissions = ({"reason": "enrichment_withheld"},)
    assert assess(fresh_db, m).current
    fresh_db.execute("INSERT INTO bookmarks(source,source_id,captured_at) VALUES('fake','1',1)")
    assert assess(fresh_db, m).reason == "stale_index"
    fresh_db.rollback()
    assert assess(fresh_db, m).current


def test_index_defaults_and_binary_pinning(monkeypatch):
    monkeypatch.delenv("MUNINN_DATA_DIR", raising=False)
    monkeypatch.delenv("MUNINN_BRAID_BINARY", raising=False)
    assert load_index_paths().root.is_relative_to(load_paths().data_dir)
    assert load_braid_settings().binary is None
    monkeypatch.setenv("MUNINN_BRAID_BINARY", "braid")
    with pytest.raises(ValueError, match="absolute"):
        load_braid_settings()
    monkeypatch.setenv("MUNINN_BRAID_BINARY", "/synthetic/bin/braid")
    monkeypatch.setenv("MUNINN_BRAID_SHA256", "invalid")
    with pytest.raises(ValueError, match="SHA256"):
        load_braid_settings()


def test_status_separates_intent_build_pointer_and_observation(tmp_path, monkeypatch):
    from muninn.db import init_db
    path = tmp_path / "synthetic.db"
    init_db(path).close()
    monkeypatch.delenv("MUNINN_SEARCH_BACKEND", raising=False)
    value = index_status(path)
    assert value["config_intent"] == "fts5-qdrant"
    assert value["ready_build"] is value["active_generation"] is value["backend_actually_used"] is None
    assert value["fallback_reason"] == "generation_unavailable"
    assert value["authored_observation"] == {"scope": None, "digest": None,
                                            "observed_at": None, "failure_category": None}
