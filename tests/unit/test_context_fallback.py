"""Synthetic lexical fallback fixtures; no model or backend service required."""
import hashlib
import json
import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest

from muninn.search import context_fallback as fallback
from muninn.search.context import context_json, estimate_entry_cost
from muninn.search.documents import ContextRequest


def candidate(number=1, text="Synthetic keyword body 日本語"):
    return fallback._Candidate(f"captured:synthetic-{number}", "bookmarks",
        f"scrape_results:{number}", "revision", f"Title {number}", 0, len(text),
        hashlib.sha256(text.encode()).hexdigest(), text, ("fts5",), f"bookmark:{number}")


def install(monkeypatch, candidates):
    monkeypatch.setattr(fallback, "_candidates", lambda request, db_path:
        (list(candidates), {"corpus_id": "synthetic", "source_epoch": 5}, []))


def test_fallback_counts_exact_unicode_json_and_labels_coverage(monkeypatch):
    install(monkeypatch, [candidate()])
    request = ContextRequest("keyword")
    result = fallback.legacy_context(request, reason="missing_generation")
    assert result.backend == "fts5-qdrant"
    assert result.fallback_reason == "missing_generation"
    assert [entry.type for entry in result.context] == ["attribution", "context_excerpt"]
    assert result.context_bytes == len(context_json(result.context))
    assert result.accounting["delivered"] == sum(estimate_entry_cost(e) for e in result.context)
    assert result.coverage["authored_recall"] == "keyword_only"
    assert result.coverage["graph"] is False
    assert result.context[1].attrs["text_digest"] == hashlib.sha256(result.context[1].text.encode()).hexdigest()


@pytest.mark.parametrize("limits", [{"budget": 0}, {"max_bytes": 2}])
def test_optional_bundle_omitted_whole(monkeypatch, limits):
    install(monkeypatch, [candidate()])
    result = fallback.legacy_context(ContextRequest("keyword", **limits))
    assert result.context == ()
    assert result.state == "ok"
    assert result.omissions == ({"reason": "optional_bundle_infeasible"},)


def test_required_source_is_never_partial_when_budget_infeasible(monkeypatch):
    install(monkeypatch, [candidate()])
    result = fallback.legacy_context(ContextRequest("keyword", budget=0, required_sources=("bookmark:1",)))
    assert result.context == ()
    assert result.state == "abstained"
    assert result.reason == "required_source_infeasible"


def test_missing_required_source_abstains_without_optional_answer(monkeypatch):
    install(monkeypatch, [candidate()])
    result = fallback.legacy_context(ContextRequest("keyword", required_sources=("bookmark:2",)))
    assert result.context == ()
    assert result.reason == "required_source_unavailable"


def test_expected_generation_never_searches_legacy(monkeypatch):
    def forbidden(*args):
        pytest.fail("legacy source scan must not run for generation precondition")
    monkeypatch.setattr(fallback, "_candidates", forbidden)
    result = fallback.legacy_context(ContextRequest("keyword", expected_generation="gen-1"))
    assert result.state == "unavailable"
    assert result.reason == "expected_generation_unavailable"


def test_shared_attribution_enters_once(monkeypatch):
    first = candidate()
    second = replace(first, start=20, end=24, excerpt="body", text_digest=hashlib.sha256(b"body").hexdigest())
    install(monkeypatch, [first, second])
    result = fallback.legacy_context(ContextRequest("keyword"))
    assert [entry.type for entry in result.context].count("attribution") == 1
    assert [entry.type for entry in result.context].count("context_excerpt") == 2


def test_optional_root_limit_does_not_remove_required_source(monkeypatch):
    install(monkeypatch, [candidate(1), candidate(2)])
    result = fallback.legacy_context(ContextRequest("keyword", optional_roots=0,
                                                  required_sources=("bookmark:2",)))
    assert len(result.context) == 2
    assert all(entry.attrs["document_id"] == "captured:synthetic-2" for entry in result.context)


@pytest.mark.parametrize("failures, expected", [(1, "ok"), (2, "unavailable")])
def test_source_change_retries_at_most_once(monkeypatch, failures, expected):
    calls = []
    def changing(request, db_path):
        calls.append(None)
        if len(calls) <= failures:
            raise RuntimeError("legacy_source_changed")
        return [candidate()], {"source_epoch": 6}, []
    monkeypatch.setattr(fallback, "_candidates", changing)
    result = fallback.legacy_context(ContextRequest("keyword"))
    assert len(calls) == 2
    assert result.state == expected


def test_lexical_span_uses_original_unicode_coordinates():
    text = "İ" * 400 + "needle" + " tail" * 200
    start, end = fallback._lexical_span(text, "needle")
    assert end - start <= fallback.EXCERPT_CHAR_LIMIT
    assert "needle" in text[start:end]


def test_required_capture_resolves_without_query_recall_and_withholds_hidden(db_path, monkeypatch):
    from muninn.consumers.mcp import tools
    from muninn.db import connect
    from muninn.search.fts5_qdrant import Fts5QdrantBackend

    conn = connect(db_path)
    for number, visible in ((1, 1), (2, 0)):
        conn.execute("INSERT INTO bookmarks(bookmark_id,source,source_id,captured_at,title,content_visible) "
                     "VALUES (?, 'netscape', ?, 100, 'Synthetic capture', ?)", (number, str(number), visible))
        conn.execute("INSERT INTO scrape_results(bookmark_id,pass,fetched_at,scrape_status,content_text) "
                     "VALUES (?, 'live', 200, 'ok', ?)", (number, "  Synthetic canonical body 日本語  "))
    conn.commit()
    conn.close()
    calls = []
    def no_recall(query, **kwargs):
        assert isinstance(kwargs["backend"], Fts5QdrantBackend)
        calls.append(kwargs["namespace"])
        return json.dumps({"results": []})
    monkeypatch.setattr(tools, "search", no_recall)
    request = ContextRequest("absent query", namespaces=("bookmarks",), required_sources=("bookmark:1",))
    result = fallback.legacy_context(request, db_path=db_path)
    assert result.context[-1].text == "Synthetic canonical body 日本語"
    assert result.context[-1].attrs["source_ref"].startswith("scrape_results:")
    assert calls == ["bookmarks"]
    hidden = fallback.legacy_context(replace(request, required_sources=("bookmark:2",)), db_path=db_path)
    assert hidden.state == "abstained"
    assert hidden.reason == "required_source_unavailable"


def test_authored_required_source_uses_exact_normalized_body_and_revision(db_path, tmp_path, monkeypatch):
    from muninn import config
    from muninn.consumers.mcp import tools

    vault = tmp_path / "synthetic-vault"
    directory = vault / "wiki" / "concepts"
    directory.mkdir(parents=True)
    raw = b"---\ntitle: Synthetic heading\n---\n\n# Body heading\r\n\nCanonical body\n"
    (directory / "a.md").write_bytes(raw)
    monkeypatch.setattr(config, "load_paths", lambda: SimpleNamespace(vault_dir=vault, personal_vault_dir=None))
    monkeypatch.setattr(tools, "search", lambda *args, **kwargs: json.dumps({"results": []}))
    request = ContextRequest("keyword", namespaces=("concepts",), required_sources=("vault:wiki/concepts/a.md",))
    result = fallback.legacy_context(request, db_path=db_path)
    assert result.state == "ok"
    excerpt = result.context[-1]
    assert excerpt.text == "# Body heading\n\nCanonical body"
    assert excerpt.attrs["source_revision"] == hashlib.sha256(raw).hexdigest()
    assert excerpt.attrs["source_ref"] == "vault:wiki/concepts/a.md"


@pytest.mark.parametrize("namespaces, scopes", [
    (ContextRequest.__dataclass_fields__["namespaces"].default, [None]),
    (("bookmarks",), ["bookmarks"]),
    (("bookmarks", "concepts"), ["bookmarks", "concepts"]),
])
def test_complete_legacy_scope_and_candidate_bound(db_path, monkeypatch, namespaces, scopes):
    from muninn.consumers.mcp import tools
    calls = []
    def search(query, **kwargs):
        calls.append((kwargs["namespace"], kwargs["top_k"]))
        return json.dumps({"results": []})
    monkeypatch.setattr(tools, "search", search)
    result = fallback.legacy_context(ContextRequest("synthetic", namespaces=namespaces), db_path=db_path)
    assert [scope for scope, limit in calls] == scopes
    assert all(limit == 24 for scope, limit in calls)
    assert result.coverage["candidate_limit_per_search"] == 24


def test_missing_authored_source_abstains(db_path, tmp_path, monkeypatch):
    from muninn import config
    from muninn.consumers.mcp import tools
    vault = tmp_path / "synthetic-vault"
    (vault / "wiki" / "concepts").mkdir(parents=True)
    monkeypatch.setattr(config, "load_paths", lambda: SimpleNamespace(vault_dir=vault, personal_vault_dir=None))
    monkeypatch.setattr(tools, "search", lambda *args, **kwargs: json.dumps({"results": []}))
    request = ContextRequest("keyword", namespaces=("concepts",), required_sources=("vault:wiki/concepts/missing.md",))
    result = fallback.legacy_context(request, db_path=db_path)
    assert result.state == "abstained"
    assert result.reason == "required_source_unavailable"


def test_unavailable_canonical_database_is_explicit(monkeypatch):
    def missing(*args):
        raise sqlite3.OperationalError("synthetic schema absent")
    monkeypatch.setattr(fallback, "_candidates", missing)
    result = fallback.legacy_context(ContextRequest("keyword"))
    assert result.state == "unavailable"
    assert result.reason == "legacy_source_unavailable"
