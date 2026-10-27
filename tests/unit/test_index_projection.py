"""Projection tests use fake character counting, never a production tokenizer."""

import json
from dataclasses import replace

import pytest

from muninn.index.authored import inventory_authored, Inventory
from muninn.index.chunking import ChunkSettings
from muninn.index.embedding_profile import EmbeddingProfile
from muninn.index.projection import PROJECTION_SCHEMA, project


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
    assert all(record.value["cost"] == 1 for record in nodes)
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
    assert len(edges) == len(chunks) * 2
    assert {e["type"] for e in edges} == {"contains", "belongs_to"}
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
