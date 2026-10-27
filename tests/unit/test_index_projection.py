"""Projection tests use fake character counting, never a production tokenizer."""

import json
from dataclasses import replace

import pytest

from muninn.index.authored import AuthoredSource, inventory_authored, Inventory
from muninn.index.chunking import ChunkSettings
from muninn.index.embedding_profile import EmbeddingProfile
from muninn.index.projection import (
    ATTRIBUTION_SCHEMA, PROJECTION_SCHEMA, attribution_id, attribution_requirements, project,
)


class FakeTokenizer:
    digest = "fake-character-tokenizer"
    max_seq_length = 256

    def count_tokens(self, text):
        return len(text)


PROFILE = EmbeddingProfile("fake", "immutable-test", 3, "query-prompt", "document-prompt",
                           FakeTokenizer.digest, backend="hash")
SETTINGS = ChunkSettings(target_tokens=32, max_tokens=64, overlap_tokens=4)
EMPTY = Inventory((), (), (), True)


def bookmark(conn, identifier, *, visible=1, body=None, title="Synthetic"):
    bid = conn.execute(
        "INSERT INTO bookmarks(source,source_id,captured_at,title,url,content_visible) "
        "VALUES ('netscape', ?, 100, ?, 'https://example.com/synthetic', ?)",
        (identifier, title, visible),
    ).lastrowid
    if body is not None:
        conn.execute("INSERT INTO scrape_results(bookmark_id,pass,fetched_at,scrape_status,content_text) "
                     "VALUES (?, 'live', 200, 'ok', ?)", (bid, body))
    return bid


def records(conn, inventory=EMPTY, **kwargs):
    return list(project(conn, "synthetic-corpus", inventory, PROFILE, FakeTokenizer(), settings=SETTINGS, **kwargs))


def test_deterministic_wire_records_exact_spans_and_prefix_mapping(fresh_db):
    body = "Synthetic paragraph one.\n\n" + "Body words " * 15
    bookmark(fresh_db, "one", body=body)
    first = records(fresh_db)
    assert first == records(fresh_db)
    # Every wire object and the stream envelope are JSON ready.
    json.dumps([{"kind": r.kind, "value": r.value, "embedding_input": r.embedding_input} for r in first])
    nodes = [r for r in first if r.kind == "node"]
    assert all(record.value["cost"] == 1 for record in nodes if record.value["type"] in ("document", "chunk"))
    document = next(r for r in nodes if r.value["type"] == "document")
    chunks = [r for r in nodes if r.value["type"] == "chunk"]
    assert len(chunks) > 1
    covered = set()
    normalized = body.strip()
    for chunk in chunks:
        attrs = chunk.value["attrs"]
        assert attrs["projection_schema"] == PROJECTION_SCHEMA
        assert attrs["document_id"] == document.value["id"]
        assert attrs["source_revision"] == document.value["attrs"]["source_revision"]
        assert chunk.value["text"] == normalized[attrs["start"]:attrs["end"]]
        assert chunk.embedding_input[attrs["embedding_body_start"]:attrs["embedding_body_end"]] == chunk.value["text"]
        assert attrs["token_count"] <= SETTINGS.max_tokens
        covered.update(range(attrs["start"], attrs["end"]))
        assert attrs["source_ref"].startswith("scrape_results:")
        assert attrs["bookmark_id"] == 1
    assert covered == set(range(len(normalized)))
    edges = [r.value for r in first if r.kind == "edge"]
    assert len(edges) == len(chunks) * 3
    assert {e["type"] for e in edges} == {"contains", "belongs_to", "requires"}
    assert all(e["ts"] == "1970-01-01T00:03:20Z" for e in edges)


def test_missing_body_keeps_metadata_and_explicit_omission(fresh_db):
    bookmark(fresh_db, "missing")
    result = records(fresh_db)
    node = next(r.value for r in result if r.kind == "node")
    assert node["attrs"]["coverage"] == "unavailable"
    assert node["attrs"]["source_ref"] == "bookmarks:1"
    assert [r.value["reason"] for r in result if r.kind == "omission"] == ["body_missing"]
    assert all(r.value["namespace"] == "bookmarks" for r in result if r.kind == "omission")
    assert not any(r.value.get("type") == "chunk" for r in result)


def test_partial_body_reports_scoped_incomplete_coverage(fresh_db):
    bid = bookmark(fresh_db, "partial", body="Synthetic partial capture")
    fresh_db.execute("UPDATE scrape_results SET scrape_status='partial' WHERE bookmark_id=?", (bid,))
    result = records(fresh_db)
    assert {"document_id": next(r.value["id"] for r in result if r.kind == "node"),
            "namespace": "bookmarks", "reason": "body_partial"} in [r.value for r in result if r.kind == "omission"]


def test_relationship_endpoints_visibility_and_provenance(fresh_db):
    a = bookmark(fresh_db, "a")
    b = bookmark(fresh_db, "b")
    hidden = bookmark(fresh_db, "hidden", visible=0, title="Must not leak")
    fresh_db.executemany(
        "INSERT INTO item_links(parent_bookmark_id,child_bookmark_id,parent_ref,child_ref,relation,position,first_seen_at) "
        "VALUES (?, ?, 'a', ?, 'links_to', ?, 300)", [(a, b, "b", 0), (a, hidden, "hidden", 1)],
    )
    fresh_db.executemany(
        "INSERT INTO cross_references(source_bookmark_id,target_bookmark_id,relationship,rationale,created_by,created_at) "
        "VALUES (?, ?, 'private derived prose', 'private rationale', 'synthetic', 400)", [(a, b), (a, hidden)],
    )
    result = records(fresh_db)
    relations = [r.value for r in result if r.kind == "node" and r.value["type"] == "relationship"]
    edges = [r.value for r in result if r.kind == "edge"]
    assert len(relations) == 2
    assert len(edges) == 6
    assert {e["type"] for e in edges} == {"links_to", "cross_reference", "source_relation", "target_relation"}
    assert {r["attrs"]["source_ref"] for r in relations} == {"item_links:1", "cross_references:1"}
    node_ids = {r.value["id"] for r in result if r.kind == "node"}
    assert all(e["src"] in node_ids and e["dst"] in node_ids for e in edges)
    assert "private derived prose" not in json.dumps([r.value for r in result])
    assert "Must not leak" not in json.dumps([r.value for r in result])


def test_authored_exact_revision_and_namespace_scope(fresh_db, tmp_path):
    vault = tmp_path / "vault"
    (vault / "wiki/concepts").mkdir(parents=True)
    file = vault / "wiki/concepts/note.md"
    file.write_text("---\ntitle: Synthetic note\n---\n\n# Body\n\nAuthored content")
    inventory = inventory_authored(vault)
    result = records(fresh_db, inventory, namespaces=("concepts",))
    document = next(r.value for r in result if r.kind == "node" and r.value["type"] == "document")
    attrs = document["attrs"]
    assert attrs["path"] == "wiki/concepts/note.md"
    assert attrs["source_ref"] == "vault:wiki/concepts/note.md"
    assert attrs["source_revision"] == inventory.sources[0].file_digest
    assert attrs["source_body_start"] > 0
    assert attrs["namespace"] == "concepts"
    assert records(fresh_db, inventory, namespaces=("bookmarks",)) == []
    with pytest.raises(ValueError, match="authored_inventory_incomplete"):
        records(fresh_db, replace(inventory, complete=False))


def test_profile_tokenizer_scope_and_model_bounds_refuse_mismatch(fresh_db):
    with pytest.raises(ValueError, match="profile_tokenizer_mismatch"):
        list(project(fresh_db, "corpus", EMPTY, replace(PROFILE, tokenizer_digest="other"), FakeTokenizer()))
    with pytest.raises(ValueError, match="invalid corpus or namespace scope"):
        records(fresh_db, namespaces=("sessions",))
    with pytest.raises(ValueError, match="chunk_budget_exceeds_model_limit"):
        list(project(fresh_db, "corpus", EMPTY, PROFILE, FakeTokenizer()))


def test_metadata_vector_over_budget_is_explicit(fresh_db):
    bookmark(fresh_db, "long-title", title="x" * 65)
    result = records(fresh_db)
    document = next(r for r in result if r.kind == "node")
    assert document.embedding_input is None
    assert "metadata_embedding_exceeds_token_budget" in [r.value["reason"] for r in result if r.kind == "omission"]


def test_hidden_inputs_never_reach_projection_embedding_text(fresh_db):
    hidden_url = "https://example.com/private"
    parent = bookmark(fresh_db, "post", body="Original cites " + hidden_url)
    child = bookmark(fresh_db, "child", visible=0)
    fresh_db.execute("UPDATE bookmarks SET source='x', title='Post' WHERE bookmark_id=?", (parent,))
    fresh_db.execute("UPDATE bookmarks SET url=? WHERE bookmark_id=?", (hidden_url, child))
    fresh_db.execute(
        "INSERT INTO item_links(parent_bookmark_id,child_bookmark_id,parent_ref,child_ref,relation,position,first_seen_at) "
        "VALUES (?, ?, 'post', 'child', 'links_to', 0, 300)", (parent, child))
    fresh_db.execute(
        "INSERT INTO enriched(bookmark_id,summary,tags,key_quotes,enrichment_model,enrichment_prompt_version,content_hash,enriched_at) "
        "VALUES (?, 'Withheld paraphrase', '[\"Paraphrased tag\"]', ?, 'fake', '1', 'fake', 200)",
        (parent, json.dumps([hidden_url])))
    result = records(fresh_db)
    inputs = json.dumps([r.embedding_input for r in result])
    assert hidden_url not in inputs
    assert "Withheld paraphrase" not in inputs
    assert "Paraphrased tag" not in inputs
    assert "[hidden link]" in inputs
    assert any(r.kind == "omission" and r.value["reason"] == "enrichment_withheld" for r in result)


def test_attribution_is_shared_exact_and_never_embedded(fresh_db):
    bookmark(fresh_db, "one", body="Original body " * 100)
    result = records(fresh_db)
    nodes = [r for r in result if r.kind == "node"]
    documents = [r.value for r in nodes if r.value["type"] == "document"]
    chunks = [r.value for r in nodes if r.value["type"] == "chunk"]
    attributions = [r for r in nodes if r.value["type"] == "attribution"]
    assert len(documents) == len(attributions) == 1 and len(chunks) > 1
    attribution = attributions[0]
    document = documents[0]
    citation = attribution.value["attrs"]["citation"]
    assert attribution.embedding_input is None
    assert attribution.value["id"] == attribution_id(document["id"], document["attrs"]["source_revision"])
    assert citation["schema"] == ATTRIBUTION_SCHEMA
    assert citation["namespace"] == "bookmarks"
    assert citation["source_ref"] == document["attrs"]["source_ref"]
    assert citation["source_revision"] == document["attrs"]["source_revision"]
    assert citation["normalization"] == "muninn-text/1"
    from muninn.search.context import estimate_entry_cost
    assert attribution.value["text"] == ""
    assert attribution.value["cost"] == estimate_entry_cost({
        k: attribution.value[k] for k in ("id", "type", "text", "attrs")})
    assert attribution.value["attrs"]["cost_estimator"] == "muninn-json-utf8-quarter/1"
    requirements = attribution_requirements(iter(result))
    excerpts = [r for r in nodes if r.value["type"] == "context_excerpt"]
    assert len(excerpts) == len(chunks)
    assert requirements == {excerpt.value["id"]: [attribution.value["id"]] for excerpt in excerpts}
    for chunk, excerpt in zip(chunks, excerpts):
        assert chunk["attrs"]["context_excerpt_id"] == excerpt.value["id"]
        assert excerpt.value["text"] == chunk["text"]
        assert excerpt.value["attrs"]["requires"] == [attribution.value["id"]]
        assert not {"summary", "tags", "url", "embedding_prefix", "title", "citation"}.intersection(excerpt.value["attrs"])
        assert excerpt.embedding_input == next(
            r.embedding_input for r in nodes if r.value["id"] == chunk["id"])
        assert excerpt.value["cost"] == estimate_entry_cost({
            k: excerpt.value[k] for k in ("id", "type", "text", "attrs")})
    for chunk in chunks:
        assert chunk["attrs"]["attribution_id"] == attribution.value["id"]
        assert chunk["attrs"]["embedding_prefix_role"] == "embedding_only_not_cited_body"
    assert "embedding_prefix" not in citation
    assert "summary" not in attribution.value["attrs"]
    assert "Original body" not in attribution.value["text"]


def test_attribution_identity_changes_with_source_revision(fresh_db):
    bid = bookmark(fresh_db, "one", body="Synthetic original")
    first = records(fresh_db)
    fresh_db.execute("UPDATE scrape_results SET content_text='Synthetic changed' WHERE bookmark_id=?", (bid,))
    second = records(fresh_db)
    get_id = lambda values: next(r.value["id"] for r in values if r.kind == "node" and r.value["type"] == "attribution")
    assert get_id(first) != get_id(second)


def test_authored_attribution_preserves_file_identity_and_exact_body_mapping(fresh_db):
    from muninn.consumers.packets.canonical import content_digest
    body = "# Synthetic heading\n\n" + "Authored prose. " * 20
    source = AuthoredSource(
        path="wiki/concepts/synthetic.md", namespace="concepts", file_digest="a" * 64,
        body_digest=content_digest(body), body=body, source_body_start=42,
        normalization="muninn-text/1", title="Synthetic title", bookmark_id=None,
        validation_stamp=(1, 2, len(body), 400000000000, 400000000000),
    )
    inventory = Inventory((source,), (("concepts", "present"),), (), True)
    result = records(fresh_db, inventory, namespaces=("concepts",))
    attribution = next(r.value for r in result if r.kind == "node" and r.value["type"] == "attribution")
    citation = attribution["attrs"]["citation"]
    assert citation["namespace"] == "concepts"
    assert citation["path"] == source.path
    assert citation["source_ref"] == "vault:" + source.path
    assert citation["source_revision"] == source.file_digest
    assert citation["body_digest"] == source.body_digest
    assert citation["normalization"] == "muninn-text/1"
    assert "url" not in citation
    for record in result:
        if record.kind == "node" and record.value["type"] == "chunk":
            attrs = record.value["attrs"]
            assert record.value["text"] == body[attrs["start"]:attrs["end"]]
            assert attrs["attribution_id"] == attribution["id"]
