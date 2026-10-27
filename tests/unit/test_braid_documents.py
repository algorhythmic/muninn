"""Document backend protocol/hydration checks using synthetic fake Braid data."""

import json
import pytest

from muninn.db import connect
from muninn.index.publisher import activate_generation
from muninn.index.paths import load_index_paths
from muninn.search.braid import BraidDocumentBackend
from muninn.search.documents import DocumentBackendUnavailable
from tests.unit.test_index_publisher import setup  # shared synthetic build fixture


def backend(setup, *, items=None, build_options=None):
    path, profile, published, build = setup
    manifest = build(**(build_options or {}))
    (load_index_paths().generation_dir(manifest.generation_id) / "braid.json").write_text(
        json.dumps({"candidate_limit": 512, "defaults": {"mmr_lambda": 1}})
    )
    activate_generation(manifest.generation_id, path)
    nodes = published[0].nodes
    requests = []

    class Client:
        state = manifest.braid["state"]
        identity = manifest.braid["identity"]
        capabilities = {"query_embedding"}
        closed = False

        def request(self, method, **kwargs):
            requests.append((method, kwargs))
            return {"dataset": self.state, "items": items if items is not None else [
                {**node, "score": index + 1.0, "why": ["dense: synthetic match"]}
                for index, node in enumerate(nodes)
            ]}

        def close(self):
            self.closed = True

    client = Client()

    def encode(query, actual_profile, **kwargs):
        assert actual_profile == profile
        assert kwargs == {"capabilities": {"query_embedding"}, "protocol_version": 1}
        return {"model": profile.profile_id, "vector": [0.6, 0.8]}

    return BraidDocumentBackend(path, client_factory=lambda **kwargs: client,
                               query_encoder=encode), client, requests


def test_document_collapse_keeps_strongest_score_and_hydrates_canonical_source(setup):
    engine, client, requests = backend(setup)
    result = engine.search_documents("synthetic", ("bookmarks",), 5)
    assert len(result.hits) == 1
    hit = result.hits[0]
    assert hit.score == 2.0
    assert hit.payload["title"] == "Synthetic title"
    assert hit.payload["bookmark_id"] == 1
    assert len(hit.chunk_refs) == 1
    assert hit.matched_stages == ("dense",)
    query = requests[0][1]["query"]
    assert query["query_embedding"]["vector"] == [0.6, 0.8]
    assert query["expected_revision"] == client.state["revision"]
    assert query["weights"] == {"lexical": 1, "dense": 1, "graph": 0, "temporal": 0}
    assert query["filters"]["predicate"]["values"] == ["bookmarks"]
    assert query["budget"] == {"max": 512, "unit": "muninn_candidate", "estimator": "node-cost-one/1"}
    assert "candidate_limit" not in query and "diversity_lambda" not in query
    assert client.closed


def test_valid_empty_result_stays_successful(setup):
    engine, client, _ = backend(setup, items=[])
    result = engine.search_documents("no-match", ("bookmarks",), 5)
    assert result.hits == ()
    assert result.fallback_reason is None
    assert client.closed


def test_stale_captured_epoch_refuses_entire_request_before_query(setup):
    engine, client, requests = backend(setup)
    path = setup[0]
    conn = connect(path)
    conn.execute("UPDATE bookmarks SET content_visible=0")
    conn.commit()
    conn.close()
    with pytest.raises(DocumentBackendUnavailable, match="stale_index"):
        engine.search_documents("synthetic", ("bookmarks",), 5)
    assert requests == []


def test_unpublished_scope_refuses_whole_request(setup):
    engine, client, requests = backend(setup)
    with pytest.raises(DocumentBackendUnavailable, match="scope_unavailable"):
        engine.search_documents("synthetic", ("bookmarks", "concepts"), 5)
    assert requests == []


@pytest.mark.parametrize("fault,reason", [("capability", "capability_mismatch"), ("dataset", "dataset_mismatch"),
                                         ("identity", "binary_identity_mismatch")])
def test_negotiation_and_identity_fail_closed(setup, fault, reason):
    engine, client, requests = backend(setup)
    if fault == "capability":
        client.capabilities = set()
    elif fault == "dataset":
        client.state = {**client.state, "revision": 99}
    else:
        client.identity = {"wrong": "binary"}
    with pytest.raises(DocumentBackendUnavailable, match=reason):
        engine.search_documents("synthetic", ("bookmarks",), 5)
    assert not requests
    assert client.closed


@pytest.mark.parametrize("fault,reason", [("timeout", "deadline_exceeded"), ("malformed", "protocol_fault"),
                                         ("profile", "embedding_unavailable"), ("model", "embedding_model_mismatch")])
def test_transport_profile_and_response_failures_dispose_client(setup, fault, reason):
    from muninn.index.braid_client import BraidError
    from muninn.index.embedding_profile import ProfileUnavailable
    engine, client, _ = backend(setup)
    def timeout(*args, **kwargs):
        raise BraidError("deadline_exceeded")
    def profile(*args, **kwargs):
        raise ProfileUnavailable("synthetic unavailable profile")
    if fault == "timeout":
        client.request = timeout
    elif fault == "malformed":
        client.request = lambda *a, **k: {"dataset": client.state, "items": [None]}
    elif fault == "profile":
        engine.query_encoder = profile
    else:
        engine.query_encoder = lambda *a, **k: {"model": "wrong", "vector": [0.6, 0.8]}
    with pytest.raises(DocumentBackendUnavailable, match=reason):
        engine.search_documents("synthetic", ("bookmarks",), 5)
    assert client.closed


@pytest.mark.parametrize("field,value", [("text_digest", "bad"), ("source_revision", "bad"),
                                       ("start", -1), ("end", 9999), ("source_ref", "wrong")])
def test_altered_chunk_integrity_aborts_entire_result(setup, field, value):
    engine, client, _ = backend(setup)
    original = client.request
    def altered(*args, **kwargs):
        response = original(*args, **kwargs)
        chunk = next(i for i in response["items"] if i["type"] == "chunk")
        chunk["attrs"] = {**chunk["attrs"], field: value}
        return response
    client.request = altered
    with pytest.raises(DocumentBackendUnavailable, match="stale_index"):
        engine.search_documents("synthetic", ("bookmarks",), 5)
    assert client.closed


def test_captured_source_mutation_during_query_aborts_all_results(setup):
    engine, client, _ = backend(setup)
    original = client.request
    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        conn = connect(setup[0])
        conn.execute("UPDATE bookmarks SET title='Changed during query'")
        conn.commit()
        conn.close()
        return result
    client.request = changed
    with pytest.raises(DocumentBackendUnavailable, match="stale_index"):
        engine.search_documents("synthetic", ("bookmarks",), 5)
    assert client.closed


def test_candidate_cap_is_reported_without_claiming_complete_top_k(setup):
    engine, client, _ = backend(setup)
    generation = next(load_index_paths().generations_dir.iterdir())
    (generation / "braid.json").write_text(json.dumps({"candidate_limit": 2, "defaults": {"mmr_lambda": 1}}))
    result = engine.search_documents("synthetic", ("bookmarks",), 5)
    assert result.warnings == ("candidate_cap_reached",)
    assert result.coverage["top_k_complete"] is False
    assert len(result.hits) == 1


def test_authored_exact_hydration_and_edit_during_query(setup, monkeypatch, tmp_path):
    vault = tmp_path / "synthetic-vault"
    (vault / "wiki/concepts").mkdir(parents=True)
    page = vault / "wiki/concepts/note.md"
    page.write_text("# Synthetic authored\n\nSynthetic body")
    monkeypatch.setenv("MUNINN_VAULT_DIR", str(vault))
    engine, client, _ = backend(setup, build_options={"namespaces": ("concepts",)})
    result = engine.search_documents("synthetic", ("concepts",), 5)
    assert result.hits[0].payload["path"] == "wiki/concepts/note.md"
    assert result.hits[0].payload["title"] == "Synthetic authored"
    original = client.request
    def changed(*args, **kwargs):
        response = original(*args, **kwargs)
        page.write_text("# Changed authored\n\nChanged body")
        return response
    client.request = changed
    with pytest.raises(DocumentBackendUnavailable, match="stale_index"):
        engine.search_documents("synthetic", ("concepts",), 5)


def test_old_projection_cost_policy_requires_rebuild(setup):
    from dataclasses import replace
    from muninn.index.manifest import Manifest
    engine, client, requests = backend(setup)
    directory = next(load_index_paths().generations_dir.iterdir())
    path = directory / "manifest.json"
    manifest = replace(Manifest.read(path), projection_version="muninn-braid-projection/1")
    manifest.write(path)
    conn = connect(setup[0])
    conn.execute("UPDATE index_generations SET manifest_digest=? WHERE generation_id=?", (manifest.digest, manifest.generation_id))
    conn.commit()
    conn.close()
    with pytest.raises(DocumentBackendUnavailable, match="projection_version_mismatch"):
        engine.search_documents("synthetic", ("bookmarks",), 5)
    assert requests == []


def test_unavailable_binary_is_typed_infrastructure_failure(setup):
    engine, _, _ = backend(setup)
    def unavailable(**kwargs):
        raise FileNotFoundError("synthetic missing binary")
    engine.client_factory = unavailable
    with pytest.raises(DocumentBackendUnavailable, match="generation_unavailable"):
        engine.search_documents("synthetic", ("bookmarks",), 5)
