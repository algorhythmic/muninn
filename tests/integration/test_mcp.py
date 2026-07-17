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
