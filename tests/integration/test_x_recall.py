"""End to end for the X lane: seed → enrich → index → compile → MCP search.

Seeds the contract section 10.1 row shapes (:mod:`tests.x_corpus`), runs
the real per-bookmark enrichment against a fake Anthropic client (so the
real prompt building and response parsing run), indexes into an
in-process Qdrant with the ``hash`` embedding backend, compiles the vault
and queries through the MCP tool functions. No network, no model.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from muninn.config import QDRANT_COLLECTION
from muninn.consumers.mcp import tools
from muninn.consumers.vault.compiler import compile_vault
from muninn.db import connect
from muninn.enrich.pipeline import enrich_all, get_eligible_bookmarks
from muninn.vector.qdrant import ensure_collection, get_point_payloads
from muninn.vector.reconcile import reconcile
from tests import x_corpus as xc

qdrant_client = pytest.importorskip("qdrant_client")


class FakeAnthropic:
    """Stands in for ``anthropic.Anthropic``: ``messages.create`` returns a
    response shaped like the SDK's, with a JSON body derived from the
    user message so each item gets its own summary."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        user = kwargs["messages"][0]["content"]
        content = user.split("Content:\n", 1)[1]
        first = content.strip().splitlines()[0]
        body = {
            "summary": f"Summary: {first}",
            "tags": ["retrieval-evaluation"],
            "entities": ["Synthetic Entity"],
            "content_type": "article",
            "language": "en",
        }
        return SimpleNamespace(
            content=[SimpleNamespace(text=json.dumps(body))],
            usage=SimpleNamespace(
                input_tokens=100,
                output_tokens=50,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
            ),
        )


@pytest.fixture
def qdrant():
    client = qdrant_client.QdrantClient(":memory:")
    ensure_collection(client)
    yield client
    client.close()


@pytest.fixture
def indexed(db_path: Path, qdrant):
    """The X corpus, enriched and indexed exactly as `muninn enrich` would."""
    conn = connect(db_path)
    corpus = xc.seed_x_corpus(conn)
    anthropic = FakeAnthropic()
    stats = enrich_all(conn, client=anthropic, qdrant=qdrant)
    conn.close()
    return SimpleNamespace(corpus=corpus, stats=stats, anthropic=anthropic)


def _ids(results_json: str) -> list[int]:
    return [r["bookmark_id"] for r in json.loads(results_json)]


def test_enrich_selects_every_visible_x_row(db_path: Path, indexed):
    c = indexed.corpus
    visible = {c.post, c.quoted, c.article, c.repo, c.web}
    conn = connect(db_path)
    try:
        assert {r["bookmark_id"] for r in get_eligible_bookmarks(conn)} == visible
        assert indexed.stats.enriched == 5 and indexed.stats.errors == 0
        # The post was enriched from its live-pass text (the API text).
        post_call = next(
            call for call in indexed.anthropic.calls
            if "quillwort benchmark." in call["messages"][0]["content"]
        )
        assert "Draft plan:" in post_call["messages"][0]["content"]
        enriched = {r[0] for r in conn.execute("SELECT bookmark_id FROM enriched")}
        assert enriched == visible
        fts = {r[0]: r[1] for r in conn.execute("SELECT rowid, content_text FROM fts_bookmarks")}
        assert set(fts) == visible
        assert fts[c.post].startswith("Thread on evaluating retrieval")
    finally:
        conn.close()


def test_qdrant_holds_a_point_per_enriched_x_row(qdrant, indexed):
    c = indexed.corpus
    payloads = get_point_payloads(qdrant)
    assert set(payloads) == {c.post, c.quoted, c.article, c.repo, c.web}
    assert payloads[c.post]["summary"].startswith("Summary: Thread on evaluating")
    assert indexed.stats.qdrant_writes == 5 and indexed.stats.qdrant_counts_match


def test_reconcile_rebuilds_the_x_points(db_path: Path, qdrant, indexed):
    c = indexed.corpus
    before = get_point_payloads(qdrant)
    qdrant.delete_collection(QDRANT_COLLECTION)
    ensure_collection(qdrant)
    stats = reconcile(db_path, client=qdrant)
    assert stats["backfilled"] == 5 and stats["errors"] == 0
    assert get_point_payloads(qdrant) == before
    assert c.hidden not in before


def test_keyword_search_returns_the_post_and_never_the_hidden_link(db_path: Path, indexed):
    c = indexed.corpus
    assert _ids(tools.fts_search("quillwort", db_path=db_path))[0] == c.post
    assert c.hidden not in _ids(tools.fts_search("roadmap OR private OR intranet", db_path=db_path))
    hidden = json.loads(tools.get_bookmark(c.hidden, db_path=db_path))
    assert "error" in hidden and "Private roadmap" not in json.dumps(hidden)


def test_semantic_search_returns_the_post_and_never_the_hidden_link(
    db_path: Path, qdrant, indexed
):
    c = indexed.corpus
    results = json.loads(
        tools.semantic_search("quillwort benchmark", limit=10, db_path=db_path, client=qdrant)
    )
    ids = [r["bookmark_id"] for r in results]
    # Hash embeddings rank arbitrarily: with every point in range, the
    # post must be there, scored (so it came from Qdrant, not the fallback).
    assert c.post in ids and all("score" in r for r in results)
    assert c.hidden not in ids
    assert "Private roadmap" not in json.dumps(results)


def test_vault_compiles_the_indexed_corpus(db_path: Path, tmp_path: Path, indexed):
    c = indexed.corpus
    out = tmp_path / "compiled"
    assert compile_vault(db_path=db_path, output_dir=out, personal_dir=None) == 5
    post = (out / "wiki" / "bookmarks" / f"x-synth-author-a-{xc.POST_ID}.md").read_text()
    assert f"bookmark_id: {c.post}\n" in post
    assert "## Summary\n\nSummary: Thread on evaluating retrieval" in post
    assert "tags: [\"retrieval-evaluation\"]" in post
    everything = "\n".join(p.read_text() for p in (out / "wiki" / "bookmarks").glob("*.md"))
    assert xc.HIDDEN_URL not in everything and "Private roadmap" not in everything


def test_link_hidden_after_indexing_disappears_everywhere(
    db_path: Path, tmp_path: Path, qdrant, indexed
):
    """Domain policy hides the web page after it was enriched and indexed."""
    c = indexed.corpus
    conn = connect(db_path)
    conn.execute("UPDATE bookmarks SET content_visible = 0 WHERE bookmark_id = ?", (c.web,))
    conn.commit()
    conn.close()

    # Search hydration filters it at once, before any reconcile.
    sem = _ids(tools.semantic_search("retrieval", limit=10, db_path=db_path, client=qdrant))
    kw = _ids(tools.fts_search("nDCG", db_path=db_path))
    assert c.web not in sem and kw == []
    assert c.post in sem

    # Reconcile then removes the point and its payload.
    stats = reconcile(db_path, client=qdrant)
    assert stats["removed_hidden"] == 1
    assert c.web not in get_point_payloads(qdrant)

    # The vault drops its page, its link and its locator in the post text.
    out = tmp_path / "compiled"
    assert compile_vault(db_path=db_path, output_dir=out, personal_dir=None) == 4
    pages = {p.stem: p.read_text() for p in (out / "wiki" / "bookmarks").glob("*.md")}
    assert not any(s.startswith("example-com-notes-") for s in pages)
    post = pages[f"x-synth-author-a-{xc.POST_ID}"]
    assert xc.WEB_URL not in post
    assert re.search(r"^> Notes: \[hidden link\]$", post, re.M)
    # The title (the text's first 80 characters) ended in the now-hidden
    # locator, truncated: masked in the page and in MCP results alike.
    assert "https://exa" not in post
    assert re.search(r'^title: ".*Notes: \[hidden link\]"$', post, re.M)
    mcp_titles = [r["title"] for r in json.loads(tools.fts_search("quillwort", db_path=db_path))
                  if r["bookmark_id"] == c.post]
    mcp_titles.append(json.loads(tools.get_bookmark(c.post, db_path=db_path))["title"])
    assert all(t.endswith("Notes: [hidden link]") for t in mcp_titles), mcp_titles
    assert "Synthetic notes on retrieval evaluation" not in "\n".join(pages.values())
