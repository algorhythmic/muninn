"""Pinned bounded-context requests against a synthetic fake Braid only."""
import copy
import json

import pytest

from muninn.db import connect
from muninn.index.braid_client import BraidError
from muninn.index.paths import load_index_paths
from muninn.index.publisher import activate_generation
from muninn.search.context import context_json
from muninn.search.context_braid import BraidContextBackend
from muninn.search.documents import ContextRequest, DocumentBackendUnavailable
from tests.unit.test_index_publisher import setup


def engine(setup, *, build_options=None):
    path, profile, published, build = setup
    manifest = build(**(build_options or {}))
    activate_generation(manifest.generation_id, path)
    nodes = published[0].nodes
    calls = []

    class Client:
        state = manifest.braid["state"]
        identity = manifest.braid["identity"]
        capabilities = {"query_embedding", "required_context", "context_export", "get_many", "allowed_ids", "read_revision"}
        closed = False

        def request(self, method, **kwargs):
            calls.append((method, kwargs))
            if method == "query":
                q = kwargs["query"]
                predicate = q["filters"]["predicate"]
                selected = [n for n in nodes if n["type"] == "chunk"]
                if predicate["op"] == "and":
                    document = predicate["children"][1]["value"]
                    selected = [n for n in selected if n["attrs"]["document_id"] == document]
                return {"dataset": self.state, "items": [{**copy.deepcopy(node), "score": 0.03}
                    for node in selected[:q["budget"]["max"]]]}
            assert method == "export_context"
            q = kwargs["export"]["query"]
            rows = [{k: copy.deepcopy(n[k]) for k in ("id", "type", "text", "attrs")}
                    for n in nodes if n["id"] in q["filters"]["allowed_ids"]]
            for row in rows:
                row["attrs"] = {k: v for k, v in row["attrs"].items() if k in kwargs["export"]["output"]["attrs"]}
            return {"dataset": self.state, "context": rows, "context_bytes": len(context_json(rows)), "abstained": False}

        def close(self):
            self.closed = True

    client = Client()
    def encode(query, actual, **kwargs):
        assert actual == profile
        return {"model": profile.profile_id, "vector": [0.6, 0.8]}
    return BraidContextBackend(path, client_factory=lambda **kwargs: client, query_encoder=encode), client, calls, nodes, manifest


def request(**kwargs):
    return ContextRequest("Synthetic", namespaces=("bookmarks",), **kwargs)


def test_native_export_preserves_exact_rows_and_counted_attribution(setup):
    backend, client, calls, nodes, manifest = engine(setup)
    result = backend.build(request())
    assert result.backend == "braid" and result.state == "ok"
    assert {e.type for e in result.context} == {"context_excerpt", "attribution"}
    assert result.context_bytes == len(context_json(result.context))
    excerpt = next(e for e in result.context if e.type == "context_excerpt")
    assert excerpt.text == "Synthetic body paragraph."
    assert excerpt.attrs["start"] == 0 and excerpt.attrs["end"] == len(excerpt.text)
    exported = calls[-1][1]["export"]
    query = exported["query"]
    assert "allowed_ids" not in query
    assert set(query["filters"]["allowed_ids"]) == {e.id for e in result.context}
    assert query["weights"] == {"lexical": 1, "dense": 1, "graph": 0, "temporal": 0}
    assert query["query_embedding"]["model"] == manifest.profile_id
    assert query["expected_revision"] == client.state["revision"]
    assert query["requires"][excerpt.id] == [excerpt.attrs["attribution_id"]]
    assert exported["output"]["fields"] == ["id", "type", "text", "attrs"]
    assert client.closed


def test_required_source_maps_trusted_ref_to_mandatory_excerpt(setup):
    backend, client, calls, nodes, _ = engine(setup)
    result = backend.build(request(required_sources=("bookmark:1",)))
    query = calls[-1][1]["export"]["query"]
    assert len(query["mandatory_ids"]) == 1
    assert query["mandatory_ids"][0] in [e.id for e in result.context]
    assert calls[0][1]["query"]["filters"]["predicate"]["children"][1]["path"] == "document_id"


def test_missing_required_source_abstains_instead_of_partial_answer(setup):
    backend, client, calls, nodes, _ = engine(setup)
    result = backend.build(request(required_sources=("bookmark:99",)))
    assert not result.context and result.reason == "required_context_unavailable"
    assert result.state == "abstained" and not calls


def test_generation_precondition_returns_conflict_without_query(setup):
    backend, client, calls, nodes, _ = engine(setup)
    result = backend.build(request(expected_generation="other-generation"))
    assert result.state == "conflict" and result.reason == "generation_conflict"
    assert not calls


@pytest.mark.parametrize("fault", ["missing_citation", "text", "span", "revision", "citation", "cost", "duplicate", "extra_field", "citation_url", "citation_attrs"])
def test_export_integrity_failure_never_returns_partial_context(setup, fault):
    backend, client, calls, nodes, _ = engine(setup)
    original = client.request
    def broken(method, **kwargs):
        result = original(method, **kwargs)
        if method != "export_context":
            return result
        rows = result["context"]
        excerpt = next(e for e in rows if e["type"] == "context_excerpt")
        citation = next(e for e in rows if e["type"] == "attribution")
        if fault == "missing_citation":
            rows.remove(citation)
        elif fault == "text":
            excerpt["text"] = "tampered"
        elif fault == "span":
            excerpt["attrs"]["start"] = -1
        elif fault == "revision":
            excerpt["attrs"]["source_revision"] = "wrong"
        elif fault == "citation":
            citation["attrs"]["citation"]["source_ref"] = "wrong"
        elif fault == "citation_url":
            citation["attrs"]["citation"]["url"] = "https://synthetic.invalid/unverified"
        elif fault == "citation_attrs":
            citation["attrs"]["private"] = "unverified"
        elif fault == "cost":
            excerpt["attrs"]["cost_estimator"] = "wrong"
        elif fault == "duplicate":
            rows.append(copy.deepcopy(excerpt))
        else:
            excerpt["private"] = "not permitted"
        return result
    client.request = broken
    with pytest.raises(DocumentBackendUnavailable):
        backend.build(request())
    assert client.closed


def test_post_export_source_mutation_retries_once_then_refuses(setup):
    backend, client, calls, nodes, _ = engine(setup)
    original = client.request
    def change(method, **kwargs):
        result = original(method, **kwargs)
        if method == "export_context":
            conn = connect(setup[0])
            conn.execute("UPDATE bookmarks SET content_visible=0")
            conn.commit()
            conn.close()
        return result
    client.request = change
    with pytest.raises(DocumentBackendUnavailable, match="stale_index"):
        backend.build(request())
    assert len([c for c in calls if c[0] == "export_context"]) == 1


def test_old_generation_without_context_nodes_is_typed_unavailable(setup):
    backend, client, calls, nodes, _ = engine(setup)
    for n in nodes:
        n["attrs"].pop("context_excerpt_id", None)
    with pytest.raises(DocumentBackendUnavailable, match="context_generation_unavailable"):
        backend.build(request())


def test_final_byte_check_rejects_untrusted_export_bound_claim(setup):
    backend, client, calls, nodes, _ = engine(setup)
    with pytest.raises(DocumentBackendUnavailable, match="export_bound_mismatch"):
        backend.build(request(max_bytes=2))


def test_capability_and_timeout_are_typed_unavailable(setup):
    backend, client, calls, nodes, _ = engine(setup)
    client.capabilities = {"query_embedding"}
    with pytest.raises(DocumentBackendUnavailable, match="capability_mismatch"):
        backend.build(request())


def test_authored_citation_uses_canonical_path_and_digest(setup, tmp_path, monkeypatch):
    vault = tmp_path / "synthetic-context-vault"
    folder = vault / "wiki/concepts"
    folder.mkdir(parents=True)
    (folder / "context.md").write_text("# Synthetic context\n\nSynthetic authored body.")
    monkeypatch.setenv("MUNINN_VAULT_DIR", str(vault))
    backend, client, calls, nodes, manifest = engine(setup, build_options={"namespaces": ("concepts",)})
    result = backend.build(ContextRequest("Synthetic", namespaces=("concepts",), required_sources=("vault:wiki/concepts/context.md",)))
    citation = next(e.attrs["citation"] for e in result.context if e.type == "attribution")
    assert citation["path"] == "wiki/concepts/context.md"
    assert citation["source_ref"] == "vault:wiki/concepts/context.md"
    assert len(citation["body_digest"]) == 64


def test_zero_optional_roots_performs_empty_native_export(setup):
    backend, client, calls, nodes, _ = engine(setup)
    result = backend.build(request(optional_roots=0))
    assert result.context == () and result.context_bytes == 2
    assert [c[0] for c in calls] == ["export_context"]
    assert calls[0][1]["export"]["query"]["filters"]["allowed_ids"] == []


def test_required_disjoint_query_still_uses_visible_source_for_root_lookup(setup):
    backend, client, calls, nodes, _ = engine(setup)
    backend.build(ContextRequest("Unrelated question", namespaces=("bookmarks",), required_sources=("bookmark:1",)))
    lookup = calls[0][1]["query"]
    assert lookup["text"] == "Synthetic body paragraph."
    assert lookup["weights"]["dense"] == 0 and "query_embedding" not in lookup


def test_stale_read_reassembles_once_only(setup, monkeypatch):
    backend, client, calls, nodes, _ = engine(setup)
    attempts = []
    def stale(req):
        attempts.append(req)
        raise DocumentBackendUnavailable("stale_index")
    monkeypatch.setattr(backend, "_build", stale)
    with pytest.raises(DocumentBackendUnavailable, match="stale_index"):
        backend.build(request())
    assert len(attempts) == 2


def test_coverage_never_discloses_excluded_source_paths(setup, monkeypatch):
    from dataclasses import replace
    from muninn.search import context_braid
    backend, client, calls, nodes, manifest = engine(setup)
    synthetic = replace(manifest, omissions=({"reason": "hidden", "path": "wiki/concepts/private-fixture.md"},
        {"reason": "body_partial", "document_id": "private-fixture-id"}))
    monkeypatch.setattr(context_braid.Manifest, "read", lambda *a, **k: synthetic)
    result = backend.build(request())
    assert result.coverage["complete"] is False
    assert result.coverage["omission_counts"] == {"body_partial": 1}
    assert "private-fixture" not in json.dumps(result.to_dict())


def test_native_mandatory_abstention_is_preserved(setup):
    backend, client, calls, nodes, _ = engine(setup)
    original = client.request
    def abstain(method, **kwargs):
        result = original(method, **kwargs)
        if method == "export_context":
            result.update(context=[], context_bytes=2, abstained=True, abstention_reason="infeasible_budget")
        return result
    client.request = abstain
    result = backend.build(request(required_sources=("bookmark:1",), budget=0))
    assert result.context == () and result.state == "abstained"
    assert result.reason == "infeasible_budget"


def test_dense_only_rank_noise_abstains_without_export_or_fallback(setup):
    backend, client, calls, _, _ = engine(setup)
    original = client.request
    def low_score(method, **kwargs):
        result = original(method, **kwargs)
        if method == "query":
            for item in result["items"]:
                item["score"] = 1 / 61  # Max dense-only RRF at pinned k=60.
        return result
    client.request = low_score
    result = backend.build(request())
    assert result.backend == "braid" and result.state == "abstained"
    assert result.context == () and result.reason == "no_relevant_context"
    assert result.fallback_reason is None
    assert all(method != "export_context" for method, _ in calls)
    assert calls[0][1]["query"]["min_score"] == .02


def test_native_no_candidates_signal_is_a_valid_empty_context(setup):
    backend, client, calls, _, _ = engine(setup)
    original = client.request
    def abstain(method, **kwargs):
        result = original(method, **kwargs)
        if method == "query":
            result.update(items=[], abstained=True, abstention_reason="evidence_policy")
        return result
    client.request = abstain
    result = backend.build(request())
    assert not result.context and result.state == "abstained"
    assert result.backend == "braid" and result.fallback_reason is None


@pytest.mark.parametrize("minimum,state", [("0.02", "ok"), ("0.04", "abstained"), ("0", "ok")])
def test_context_floor_is_explicit_operator_policy(setup, monkeypatch, minimum, state):
    monkeypatch.setenv("MUNINN_BRAID_CONTEXT_MIN_SCORE", minimum)
    backend, _, _, _, _ = engine(setup)
    assert backend.build(request()).state == state


@pytest.mark.parametrize("minimum", ["nan", "inf", "-1", "invalid"])
def test_context_floor_refuses_invalid_values(monkeypatch, minimum):
    from muninn.config import load_braid_context_min_score
    monkeypatch.setenv("MUNINN_BRAID_CONTEXT_MIN_SCORE", minimum)
    with pytest.raises(ValueError):
        load_braid_context_min_score()


def test_explicit_required_source_is_not_removed_by_optional_floor(setup, monkeypatch):
    monkeypatch.setenv("MUNINN_BRAID_CONTEXT_MIN_SCORE", "1")
    backend, _, calls, _, _ = engine(setup)
    result = backend.build(request(required_sources=("bookmark:1",)))
    assert result.context and result.backend == "braid"
    assert "min_score" not in calls[0][1]["query"]
    assert calls[-1][1]["export"]["query"]["min_score"] == 1


def test_invalid_context_policy_has_safe_failure_reason(setup, monkeypatch):
    monkeypatch.setenv("MUNINN_BRAID_CONTEXT_MIN_SCORE", "nan")
    backend, _, calls, _, _ = engine(setup)
    with pytest.raises(DocumentBackendUnavailable, match="context_policy_invalid"):
        backend.build(request())
    assert calls == []


def test_final_cost_recheck_rejects_oversized_native_result(setup):
    backend, client, calls, nodes, _ = engine(setup)
    with pytest.raises(DocumentBackendUnavailable, match="export_bound_mismatch"):
        backend.build(request(budget=0))


def test_index_metadata_never_enters_exported_context(setup):
    backend, client, calls, nodes, _ = engine(setup)
    for node in nodes:
        if node["type"] in ("chunk", "context_excerpt"):
            node["attrs"].update(summary="UNVERIFIED PRIVATE SUMMARY", tags=["PRIVATE TAG"],
                                 url="https://unverified.invalid", embedding_prefix="UNVERIFIED PREFIX")
    result = backend.build(request())
    encoded = json.dumps(result.to_dict())
    assert "UNVERIFIED" not in encoded and "PRIVATE TAG" not in encoded and "unverified.invalid" not in encoded
    excerpt = next(e for e in result.context if e.type == "context_excerpt")
    assert not {"summary", "tags", "url", "embedding_prefix", "title"}.intersection(excerpt.attrs)
    assert calls[-1][1]["export"]["output"]["attrs"]


def test_excerpt_cannot_smuggle_a_second_unverified_citation(setup):
    backend, client, calls, nodes, _ = engine(setup)
    for node in nodes:
        if node["type"] in ("chunk", "context_excerpt"):
            node["attrs"]["citation"] = {"url": "https://unverified.invalid/private"}
    with pytest.raises(DocumentBackendUnavailable, match="stale_index"):
        backend.build(request())


def test_multiple_excerpts_share_one_attribution_within_root_limit(setup):
    path = setup[0]
    conn = connect(path)
    conn.execute("UPDATE scrape_results SET content_text=?", ("Synthetic body paragraph. " * 100,))
    conn.commit()
    conn.close()
    backend, client, calls, nodes, _ = engine(setup)
    result = backend.build(request(optional_roots=2, budget=10000, max_bytes=65536))
    excerpts = [entry for entry in result.context if entry.type == "context_excerpt"]
    citations = [entry for entry in result.context if entry.type == "attribution"]
    assert len(excerpts) == 2 and len(citations) == 1
    assert {entry.attrs["attribution_id"] for entry in excerpts} == {citations[0].id}
    assert result.context_bytes == len(context_json(result.context))
    from muninn.search.context import estimate_entry_cost
    assert result.accounting["delivered"] == sum(estimate_entry_cost(entry) for entry in result.context)
