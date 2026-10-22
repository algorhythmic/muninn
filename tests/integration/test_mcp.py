"""Integration tests for the MCP tool callables.

The FastMCP wrapper is a thin pass-through; we test the tool functions
directly against a populated canonical-schema DB.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from muninn.consumers.mcp import tools


def _seed(db_path: Path) -> None:
    now = int(time.time())
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        conn.execute(
            "INSERT INTO eras (era_label, narrative, start_date, end_date, bookmark_count) "
            "VALUES (?, ?, ?, ?, ?)",
            ("early-web", "early days narrative", now - 10000, now - 5000, 1),
        )
        conn.execute(
            "INSERT INTO eras (era_label, narrative, start_date, end_date, bookmark_count) "
            "VALUES (?, ?, ?, ?, ?)",
            ("ai-era", "ai narrative", now - 4000, now, 1),
        )
        conn.execute(
            "INSERT INTO bookmarks (source, source_id, captured_at, title, url, "
            "  era_label, domain, content_visible, enrichment_source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("netscape", "1", now - 9000, "Quokka rescue", "https://example.com/quokka",
             "early-web", "example.com", 1, "at_capture"),
        )
        conn.execute(
            "INSERT INTO bookmarks (source, source_id, captured_at, title, url, "
            "  era_label, domain, content_visible, enrichment_source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("netscape", "2", now - 3000, "Transformer paper", "https://arxiv.org/x",
             "ai-era", "arxiv.org", 1, "at_capture"),
        )
        conn.executemany(
            "INSERT INTO enriched (bookmark_id, summary, tags, content_text, "
            "  enrichment_model, enrichment_prompt_version, content_hash, enriched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [],  # placeholder — content_text is a scrape column, not enriched
        ) if False else None
        conn.executemany(
            "INSERT INTO enriched (bookmark_id, summary, tags, "
            "  enrichment_model, enrichment_prompt_version, content_hash, enriched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (1, "A short summary about quokkas", json.dumps(["animal"]),
                 "haiku", "v1", "h1", now),
                (2, "Attention is all you need", json.dumps(["ai"]),
                 "haiku", "v1", "h2", now),
            ],
        )

        # Populate FTS5 (contentless): rowid = bookmark_id.
        conn.executemany(
            "INSERT INTO fts_bookmarks (rowid, title, summary, content_text, tags) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (1, "Quokka rescue", "A short summary about quokkas", "", "animal"),
                (2, "Transformer paper", "Attention is all you need", "", "ai"),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def test_fts_search_finds_match(db_path: Path):
    _seed(db_path)
    out = json.loads(tools.fts_search("quokka", db_path=db_path))
    assert len(out) == 1
    assert out[0]["title"] == "Quokka rescue"
    assert out[0]["summary"] == "A short summary about quokkas"


def test_fts_search_no_results(db_path: Path):
    _seed(db_path)
    out = json.loads(tools.fts_search("xyznotapresent", db_path=db_path))
    assert out == []


def test_get_bookmark_includes_scrape_results(db_path: Path):
    _seed(db_path)
    # Add a scrape result so we exercise the join.
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO scrape_results (bookmark_id, pass, fetched_at, "
            "  http_status, scrape_status, extraction_quality) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (1, "live", int(time.time()), 200, "ok", "ok"),
        )
        conn.commit()
    finally:
        conn.close()

    out = json.loads(tools.get_bookmark(1, db_path=db_path))
    assert out["bookmark_id"] == 1
    assert out["title"] == "Quokka rescue"
    assert out["summary"] == "A short summary about quokkas"
    assert isinstance(out["scrape_results"], list)
    assert out["scrape_results"][0]["pass"] == "live"


def test_get_bookmark_not_found(db_path: Path):
    _seed(db_path)
    out = json.loads(tools.get_bookmark(99999, db_path=db_path))
    assert "error" in out


class _FakeQdrant:
    """Captures the query passed to query_points and returns canned points."""

    class _Point:
        def __init__(self, bid: int, score: float):
            self.payload = {"bookmark_id": bid}
            self.score = score

    class _Results:
        def __init__(self, points):
            self.points = points

    def __init__(self, hits: list[tuple[int, float]]):
        self._hits = hits
        self.captured_query = None

    def query_points(self, collection_name, query, limit):
        self.captured_query = query
        return self._Results([self._Point(b, s) for b, s in self._hits])


def test_semantic_search_embeds_query_and_hydrates(db_path: Path):
    """The query must reach Qdrant as an embedding vector (not raw text),
    and hits must hydrate from SQLite with scores attached."""
    _seed(db_path)
    fake = _FakeQdrant(hits=[(1, 0.93)])
    out = json.loads(tools.semantic_search("quokka", db_path=db_path, client=fake))
    assert isinstance(fake.captured_query, list)
    assert all(isinstance(x, float) for x in fake.captured_query)
    assert out[0]["bookmark_id"] == 1
    assert out[0]["title"] == "Quokka rescue"
    assert out[0]["score"] == 0.93


def test_semantic_search_empty_hits_falls_back_to_fts(db_path: Path):
    _seed(db_path)
    fake = _FakeQdrant(hits=[])
    out = json.loads(tools.semantic_search("quokka", db_path=db_path, client=fake))
    # FTS fallback still finds the seeded bookmark by keyword.
    assert any(r["bookmark_id"] == 1 for r in out)


def test_get_bookmark_hidden_indistinguishable_from_missing(db_path: Path):
    """content_visible=0 rows must not leak through direct-ID fetch, and the
    error must not reveal that the ID exists."""
    _seed(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO bookmarks (source, source_id, captured_at, title, url, "
            "  era_label, domain, content_visible, enrichment_source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("netscape", "hidden-1", int(time.time()), "Hidden thing",
             "https://intra.example/x", "ai-era", "intra.example", 0, "none"),
        )
        hidden_id = conn.execute(
            "SELECT bookmark_id FROM bookmarks WHERE source_id = 'hidden-1'"
        ).fetchone()[0]
        conn.commit()
    finally:
        conn.close()

    hidden = json.loads(tools.get_bookmark(hidden_id, db_path=db_path))
    missing = json.loads(tools.get_bookmark(99999, db_path=db_path))
    assert "error" in hidden
    assert "Hidden thing" not in json.dumps(hidden)
    assert hidden["error"].replace(str(hidden_id), "{id}") == missing["error"].replace(
        "99999", "{id}"
    )


def test_get_era(db_path: Path):
    _seed(db_path)
    out = json.loads(tools.get_era("early-web", db_path=db_path))
    assert out["era_label"] == "early-web"
    assert "narrative" in out


def test_get_era_not_found(db_path: Path):
    _seed(db_path)
    out = json.loads(tools.get_era("never-existed", db_path=db_path))
    assert "error" in out


def test_list_eras_counts_are_live(db_path: Path):
    _seed(db_path)
    out = json.loads(tools.list_eras(db_path=db_path))
    labels = {e["era_label"]: e["bookmark_count"] for e in out}
    assert labels["early-web"] == 1
    assert labels["ai-era"] == 1


# ── search interface swap point (ADR-014) ──────────────────────────


class _FakeBackend:
    """A non-default SearchBackend: canned hits, records every call."""

    name = "fake"

    def __init__(self, *, semantic_hits=(), keyword_hits=(), semantic_error=None):
        from muninn.search import SearchHit

        self._semantic = [SearchHit(b, s, "fake-vec") for b, s in semantic_hits]
        self._keyword = [SearchHit(b, s, "fake-kw") for b, s in keyword_hits]
        self._semantic_error = semantic_error
        self.calls: list[tuple[str, str, int]] = []

    def keyword(self, query, limit=10):
        self.calls.append(("keyword", query, limit))
        return list(self._keyword)

    def semantic(self, query, limit=10):
        self.calls.append(("semantic", query, limit))
        if self._semantic_error is not None:
            raise self._semantic_error
        return list(self._semantic)


def _add_hidden(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            "INSERT INTO bookmarks (source, source_id, captured_at, title, url, "
            "  era_label, domain, content_visible, enrichment_source) "
            "VALUES ('netscape', 'hidden-2', ?, 'Secret', 'https://intra.example/y', "
            "  'ai-era', 'intra.example', 0, 'none')",
            (int(time.time()),),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def test_fake_backend_satisfies_protocol():
    from muninn.search import SearchBackend

    assert isinstance(_FakeBackend(), SearchBackend)


def test_fake_backend_semantic_hydrates_in_backend_order(db_path: Path):
    _seed(db_path)
    fake = _FakeBackend(semantic_hits=[(2, 0.8), (1, 0.5)])
    out = json.loads(tools.semantic_search("anything", limit=3, db_path=db_path, backend=fake))
    assert fake.calls == [("semantic", "anything", 3)]
    assert [(r["bookmark_id"], r["score"]) for r in out] == [(2, 0.8), (1, 0.5)]
    assert out[0]["summary"] == "Attention is all you need"


def test_fake_backend_hidden_and_unknown_hits_never_leak(db_path: Path):
    """Visibility is enforced at hydration, whatever the backend returns."""
    _seed(db_path)
    hidden_id = _add_hidden(db_path)
    fake = _FakeBackend(
        semantic_hits=[(hidden_id, 0.99), (424242, 0.9), (1, 0.4)],
        keyword_hits=[(hidden_id, 5.0), (2, 1.0)],
    )
    sem = json.loads(tools.semantic_search("q", db_path=db_path, backend=fake))
    kw = json.loads(tools.fts_search("q", db_path=db_path, backend=fake))
    assert [r["bookmark_id"] for r in sem] == [1]
    assert [r["bookmark_id"] for r in kw] == [2]
    assert "Secret" not in json.dumps(sem) + json.dumps(kw)


def test_fake_backend_keyword_output_has_no_score(db_path: Path):
    _seed(db_path)
    fake = _FakeBackend(keyword_hits=[(1, 3.2)])
    out = json.loads(tools.fts_search("quokka", db_path=db_path, backend=fake))
    assert out == [
        {
            "bookmark_id": 1,
            "title": "Quokka rescue",
            "url": "https://example.com/quokka",
            "era_label": "early-web",
            "domain": "example.com",
            "summary": "A short summary about quokkas",
            "source": "netscape",
            "item_kind": None,  # browser bookmarks have no contract item kind
        }
    ]


def test_search_results_carry_source_and_item_kind_for_x_rows(db_path: Path):
    """X rows: `source`, the contract `item_kind`, and `bookmarks.title`
    (an X post the writer saw only as an errors[] entry has no title, so
    the result falls back to its author handle). Hidden rows stay hidden."""
    _seed(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        rows = [
            (10, "x", "1900000000000000010", "@synth_author: A synthetic post",
             "https://x.com/synth_author/status/1900000000000000010", "x.com", 1,
             json.dumps({"kind": "x_post", "author": {"id": "9", "username": "synth_author"}})),
            (11, "x", "1900000000000000011", None,
             "https://x.com/synth_author/status/1900000000000000011", "x.com", 1,
             json.dumps({"kind": "x_post", "author": {"id": "9", "username": "synth_author"}})),
            (12, "x_link", "a" * 64, "synthetic-org/synthetic-repo",
             "https://github.com/synthetic-org/synthetic-repo", "github.com", 1,
             json.dumps({"kind": "github_repo"})),
            (13, "x_link", "b" * 64, "Hidden page", "https://intranet.example/p",
             "intranet.example", 0, json.dumps({"kind": "web_page"})),
        ]
        conn.executemany(
            "INSERT INTO bookmarks (bookmark_id, source, source_id, title, url, domain, "
            "  content_visible, source_metadata, captured_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    fake = _FakeBackend(keyword_hits=[(10, 3.0), (11, 2.0), (12, 1.0), (13, 0.5), (1, 0.1)])
    out = json.loads(tools.fts_search("synthetic", db_path=db_path, backend=fake))
    got = [(r["bookmark_id"], r["source"], r["item_kind"], r["title"]) for r in out]
    assert got == [
        (10, "x", "x_post", "@synth_author: A synthetic post"),
        (11, "x", "x_post", "@synth_author"),
        (12, "x_link", "github_repo", "synthetic-org/synthetic-repo"),
        (1, "netscape", None, "Quokka rescue"),
    ]
    assert "Hidden page" not in json.dumps(out)
    full = json.loads(tools.get_bookmark(12, db_path=db_path))
    assert full["source"] == "x_link" and full["item_kind"] == "github_repo"
    assert "error" in json.loads(tools.get_bookmark(13, db_path=db_path))


def test_fake_backend_semantic_unavailable_falls_back_to_its_keyword(db_path: Path):
    from muninn.search import SearchUnavailable

    _seed(db_path)
    fake = _FakeBackend(semantic_error=SearchUnavailable("down"), keyword_hits=[(1, 1.0)])
    out = json.loads(tools.semantic_search("quokka", db_path=db_path, backend=fake))
    assert [c[0] for c in fake.calls] == ["semantic", "keyword"]
    assert [r["bookmark_id"] for r in out] == [1]
    assert "score" not in out[0]


def test_mcp_server_uses_injected_backend(db_path: Path, monkeypatch):
    from muninn.consumers.mcp import server

    _seed(db_path)
    monkeypatch.setenv("MUNINN_DB_PATH", str(db_path))
    fake = _FakeBackend(semantic_hits=[(1, 0.7)], keyword_hits=[(2, 1.0)])
    server.set_search_backend(fake)
    try:
        sem = json.loads(server.semantic_search("anything", 4))
        kw = json.loads(server.fts_search("anything"))
    finally:
        server.set_search_backend(None)
    assert fake.calls == [("semantic", "anything", 4), ("keyword", "anything", 10)]
    assert sem[0]["bookmark_id"] == 1 and sem[0]["score"] == 0.7
    assert kw[0]["bookmark_id"] == 2


def test_mcp_server_selects_backend_from_env(db_path: Path, monkeypatch):
    """The config path: register a backend, select it by name, no code edits."""
    from muninn import search
    from muninn.consumers.mcp import server

    _seed(db_path)
    fake = _FakeBackend(keyword_hits=[(1, 1.0)])
    seen = {}

    def _factory(*, db_path=None, **_options):
        seen["db_path"] = db_path
        return fake

    monkeypatch.setitem(search._REGISTRY, "fake", _factory)
    monkeypatch.setenv("MUNINN_SEARCH_BACKEND", "fake")
    monkeypatch.setenv("MUNINN_DB_PATH", str(db_path))
    out = json.loads(server.fts_search("quokka"))
    assert fake.calls == [("keyword", "quokka", 10)]
    assert [r["bookmark_id"] for r in out] == [1]
    assert seen == {"db_path": None}  # the server passes none; the DB comes from env
