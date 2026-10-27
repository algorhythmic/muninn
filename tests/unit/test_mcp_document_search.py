"""Synthetic document routing through MCP, with no backend process or model."""

import json
from types import SimpleNamespace

import pytest

from muninn.consumers.mcp import tools
from muninn.search.documents import DocumentBackendUnavailable, DocumentHit, DocumentSearchResult


class Legacy:
    name = "fts5-qdrant"

    def __init__(self):
        self.calls = 0

    def hybrid(self, query, limit):
        self.calls += 1
        return []


def adapter(primary):
    return SimpleNamespace(name="braid", document_backend=primary, legacy=Legacy())


def test_mcp_braid_empty_is_success_without_second_search(db_path):
    backend = adapter(lambda *args: DocumentSearchResult(()))
    result = json.loads(tools.search("synthetic", "bookmarks", db_path=db_path, backend=backend))
    assert result["results"] == []
    assert result["backend"] == "braid"
    assert result["fallback_reason"] is None
    assert backend.legacy.calls == 0


def test_mcp_whole_request_fallback_has_structured_reason(db_path):
    def unavailable(*args):
        raise DocumentBackendUnavailable("stale_index")
    backend = adapter(unavailable)
    result = json.loads(tools.search("synthetic", "bookmarks", db_path=db_path, backend=backend))
    assert result["results"] == []
    assert result["backend"] == "fts5-qdrant"
    assert result["fallback_reason"] == "stale_index"
    assert backend.legacy.calls == 1


def test_mcp_authored_document_keeps_path_and_fields(db_path):
    hit = DocumentHit("doc", "concepts", "wiki/concepts/synthetic.md", "digest", 0.8,
                      ("lexical", "dense"), payload={"path": "wiki/concepts/synthetic.md",
                      "title": "Synthetic", "summary": "Safe current metadata"})
    backend = adapter(lambda *args: DocumentSearchResult((hit,), generation={"generation_id": "synthetic"}))
    result = json.loads(tools.search("synthetic", "concepts", db_path=db_path, backend=backend))
    assert result["results"][0]["path"] == "wiki/concepts/synthetic.md"
    assert result["results"][0]["matched"] == ["lexical", "dense"]
    assert result["generation"] == {"generation_id": "synthetic"}


@pytest.mark.parametrize("query,namespace,code", [("", "bookmarks", "invalid_query"),
                                                 ("synthetic", "denied", "unknown_namespace")])
def test_mcp_validation_before_routing(query, namespace, code):
    backend = adapter(lambda *args: pytest.fail("backend must not run"))
    assert json.loads(tools.search(query, namespace, backend=backend))["code"] == code


def test_semantic_tool_preserves_valid_empty_braid_result(db_path):
    from muninn.search.routing import BraidBookmarkBackend

    class Primary:
        def search_documents(self, *args):
            return DocumentSearchResult(())

    class Forbidden:
        def keyword(self, *args):
            pytest.fail("valid empty result must not trigger FTS")

    backend = BraidBookmarkBackend(document_backend=Primary(), legacy=Forbidden())
    assert json.loads(tools.semantic_search("synthetic", db_path=db_path, backend=backend)) == []


def test_captured_success_retains_existing_public_metadata(db_path):
    from muninn.db import connect
    from muninn.index.registry import source_epoch

    conn = connect(db_path)
    conn.execute("INSERT INTO bookmarks(bookmark_id,source,source_id,captured_at,title,url,domain) "
                 "VALUES(1,'synthetic','1',1,'Canonical title','https://example.com/synthetic','example.com')")
    conn.commit()
    epoch = source_epoch(conn)
    conn.close()
    hit = DocumentHit("doc", "bookmarks", "bookmarks:1", "digest", 0.8, ("dense",),
                      payload={"bookmark_id": 1, "summary": None})
    backend = adapter(lambda *args: DocumentSearchResult((hit,), generation={"source_epoch": epoch}))
    result = json.loads(tools.search("synthetic", "bookmarks", db_path=db_path, backend=backend))
    item = result["results"][0]
    assert item["title"] == "Canonical title"
    assert item["domain"] == "example.com"
    assert {"era_label", "item_kind", "path", "summary", "source", "url"}.issubset(item)


def test_fallback_and_get_bookmark_withhold_hidden_input_paraphrases(db_path):
    from muninn.db import connect
    from muninn.search.base import SearchHit

    conn = connect(db_path)
    conn.executemany("INSERT INTO bookmarks(bookmark_id,source,source_id,captured_at,title,content_visible) "
                     "VALUES(?,'synthetic',?,1,'Synthetic',?)", [(1, "1", 1), (2, "2", 0)])
    conn.execute("INSERT INTO enriched(bookmark_id,summary,tags,key_quotes,entities,enrichment_model,"
                 "enrichment_prompt_version,content_hash,enriched_at) "
                 "VALUES(1,'Secret paraphrase','[\"secret tag\"]','[\"secret quote\"]','{}','fake','1','hash',1)")
    digest = "sha256:" + "a" * 64
    conn.execute("INSERT INTO deep_pass_results(task_id,attempt,bookmark_id,model,prompt_version,"
                 "source_content_hash,materials_digest,material_bookmark_ids_json,output_json,output_digest,started_at,completed_at) "
                 "VALUES('synthetic',1,1,'fake','1',?,?,'[1,2]','{}',?,1,2)", (digest, digest, digest))
    conn.commit()
    conn.close()

    def unavailable(*args):
        raise DocumentBackendUnavailable("stale_index")
    backend = adapter(unavailable)
    backend.legacy.hybrid = lambda *args: [SearchHit(1, 0.5, "fts5")]
    result = json.loads(tools.search("synthetic", "bookmarks", db_path=db_path, backend=backend))
    assert result["results"][0]["summary"] is None
    full = json.loads(tools.get_bookmark(1, db_path))
    assert full["enrichment_withheld"]
    assert full["summary"] is None
    assert "secret" not in json.dumps(full).lower()
