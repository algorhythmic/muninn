"""Optional actual stdio binary contract. No model, sockets or live data."""
import os
import hashlib
import json
import shutil
from pathlib import Path

import pytest

from muninn.index.braid_client import BraidClient
from tests.unit.test_index_publisher import setup


def binary_path():
    raw = os.environ.get("MUNINN_TEST_BRAID_BINARY")
    if not raw or not Path(raw).is_file():
        pytest.skip("set MUNINN_TEST_BRAID_BINARY to an approved pinned Braid executable")
    return Path(raw)


def test_pilot_real_binary_negotiates_required_contract(tmp_path):
    client = BraidClient.create(binary=binary_path(), generation_dir=tmp_path / "synthetic",
                                dataset_id="synthetic:pilot-contract", profile_id="synthetic-space")
    try:
        assert "query_embedding" in client.capabilities
        assert client.request("dataset")["id"] == "synthetic:pilot-contract"
    finally:
        client.close()


def native_fixture(setup, tmp_path, monkeypatch):
    from muninn.index.publisher import activate_generation
    binary = tmp_path / "pinned-braid"
    shutil.copyfile(binary_path(), binary)
    binary.chmod(0o700)
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    # Synthetic provenance label: this test validates the pin mechanism, not a
    # claim about the external executable's actual source commit.
    provenance = {"source_commit": "a" * 40, "binary_sha256": digest}
    Path(str(binary) + ".provenance.json").write_text(json.dumps(provenance))
    config = {"schema": "muninn-braid-pilot/1", "enabled": True,
              "binary_path": str(binary), **provenance}
    config_path = tmp_path / "pilot.json"
    config_path.write_text(json.dumps(config))
    monkeypatch.setenv("MUNINN_BRAID_PILOT_CONFIG", str(config_path))
    path, profile, _, build = setup
    # Reuse the labelled synthetic profile/tokenizer/encoder, but publish through
    # the ACTUAL stdio process and SQLite store, never FakeBraid.
    manifest = build(binary=binary, client_factory=None)
    activate_generation(manifest.generation_id, path)
    return path, profile, manifest, config_path, config


def test_native_query_kill_fallback_and_flag_rollback(setup, tmp_path, monkeypatch):
    from muninn.index.runtime import runtime
    from muninn.search.braid import BraidDocumentBackend
    from muninn.search.routing import BraidBookmarkBackend
    from muninn.search.fts5_qdrant import Fts5QdrantBackend
    from muninn.consumers.mcp import tools
    from muninn.db import connect
    from types import SimpleNamespace
    path, profile, manifest, config_path, config = native_fixture(setup, tmp_path, monkeypatch)
    conn = connect(path)
    conn.execute("INSERT INTO fts_bookmarks(rowid,title,content_text) VALUES(1,'Synthetic title','Synthetic body paragraph.')")
    conn.commit()
    conn.close()
    class FakeQdrant:
        def query_points(self, **kwargs):
            return SimpleNamespace(points=[])
    legacy = Fts5QdrantBackend(db_path=path, qdrant_client=FakeQdrant())
    encoder = lambda query, profile, **kwargs: {"model": profile.profile_id, "vector": [.6, .8]}
    primary = BraidDocumentBackend(path, query_encoder=encoder)
    adapter = BraidBookmarkBackend(db_path=path, document_backend=primary, legacy=legacy)
    def search(backend):
        return json.loads(tools.search("synthetic", namespace="bookmarks", db_path=path, backend=backend))
    try:
        baseline = search(legacy)
        result = search(adapter)
        assert result["backend"] == "braid" and result["results"]
        sessions = [s for s in runtime._sessions.values() if s.generation == manifest.generation_id]
        assert len(sessions) == 1
        process = sessions[0].client.transport.process  # Exact child started here.
        process.kill()
        process.wait(timeout=5)
        fallback = search(adapter)
        assert fallback["backend"] == "fts5-qdrant"
        assert fallback["results"] == baseline["results"]
        assert fallback["fallback_reason"] in {"transport_closed", "transport_timeout"}
        config["enabled"] = False
        config_path.write_text(json.dumps(config))
        rolled_back = search(adapter)
        assert rolled_back["results"] == baseline["results"]
        assert rolled_back["fallback_reason"] == "pilot_disabled"
    finally:
        runtime.shutdown()


def test_native_context_exports_exact_attribution_with_bounds(setup, tmp_path, monkeypatch):
    from muninn.index.runtime import runtime
    from muninn.search.context_braid import BraidContextBackend
    from muninn.consumers.mcp import tools
    path, profile, _, _, _ = native_fixture(setup, tmp_path, monkeypatch)
    encoder = lambda query, profile, **kwargs: {"model": profile.profile_id, "vector": [.6, .8]}
    backend = BraidContextBackend(path, query_encoder=encoder)
    try:
        result = json.loads(tools.search_context("synthetic", namespaces=["bookmarks"],
            required_sources=["bookmark:1"], db_path=path, context_backend=backend))
        assert result["backend"] == "braid", result
        assert result["context_bytes"] <= 16384 and result["accounting"]["delivered"] <= 2500
        assert any(row["type"] == "context_excerpt" and "Synthetic body" in row["text"] for row in result["context"])
        assert any(row["type"] == "attribution" for row in result["context"])
        absent = json.loads(tools.search_context("lunar cheese production quota", namespaces=["bookmarks"],
            db_path=path, context_backend=backend))
        assert absent["backend"] == "braid" and absent["state"] == "abstained", absent
        assert absent["context"] == [] and absent["fallback_reason"] is None
    finally:
        runtime.shutdown()
