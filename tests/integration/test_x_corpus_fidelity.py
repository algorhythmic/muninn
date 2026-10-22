"""The X seed helper (:mod:`tests.x_corpus`) matches what ingest really writes.

Runs the real writer (`muninn ingest x`'s :func:`sync_bookmarks`) on the
connector fixtures and the real scraper on its linked pages, then checks,
per kind of row, that the seed's rows have the same shape: the same
non-NULL columns and the same ``source_metadata`` keys. A writer row set
gives a *required* shape (columns/keys every such writer row has) and an
*allowed* one (any writer row has); each seed row must lie between them.

The vault and recall tests run on the seed, so a writer change that the
seed doesn't follow fails here rather than leaving those tests green on
rows ingest no longer produces.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx
import pytest
import respx

from muninn.db import init_db
from muninn.ingest.x import XConnectorConfig
from muninn.ingest.x_store import SyncOptions, sync_bookmarks
from muninn.scrape.client import IA_CDX_BASE, ScrapeClient
from muninn.scrape.domain_policy import DomainPolicy
from muninn.scrape.pipeline import scrape_all
from tests import x_corpus as xc
from tests.integration.test_scrape_history import CDX_EMPTY, PAGE, NoWait
from tests.integration.test_x_sync import API, BOOKMARKS, FakeClock, client, fx, serve

HIDDEN_HOST = "www.example.com"  # the one titled web link in the fixtures
OPTIONAL_POST_KEYS = frozenset({"conversation_id", "possibly_sensitive"})


@pytest.fixture(scope="module")
def writer_db(tmp_path_factory) -> sqlite3.Connection:
    """Fixture pages → sync_bookmarks → scrape of the visible x_link rows."""
    tmp = tmp_path_factory.mktemp("writer")
    conn = init_db(tmp / "muninn.db")
    clock = FakeClock()
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{API}/users/me").mock(side_effect=lambda req: fx("users_me"))
        mock.get(BOOKMARKS).mock(side_effect=serve())
        report = sync_bookmarks(
            conn, client(clock), options=SyncOptions(max_pages=None),
            policy=DomainPolicy(domain_patterns=(HIDDEN_HOST,)), clock=clock,
        )
    assert report.status == "completed"

    async def _scrape() -> None:
        scraper = ScrapeClient(http_cache_dir=tmp / "http", scrape_cache_dir=tmp / "scrape",
                               rate_limiter=NoWait())
        try:
            await scrape_all(conn, client=scraper, sources=["x_link"])
        finally:
            await scraper.close()

    import asyncio

    with respx.mock(assert_all_called=False) as mock:
        mock.get(IA_CDX_BASE).mock(return_value=httpx.Response(200, text=CDX_EMPTY))
        mock.route(method="GET").mock(return_value=httpx.Response(200, text=PAGE))
        asyncio.run(_scrape())
    yield conn
    conn.close()


@pytest.fixture(scope="module")
def seed_db(tmp_path_factory) -> sqlite3.Connection:
    conn = init_db(tmp_path_factory.mktemp("seed") / "muninn.db")
    xc.seed_x_corpus(conn)
    yield conn
    conn.close()


# ── row classification ──────────────────────────────────────────────


def _meta(row: sqlite3.Row) -> dict:
    return json.loads(row["source_metadata"] or "{}")


def _category(row: sqlite3.Row) -> str | None:
    """The kinds of row the seed models. Posts are only those the API
    returned data for (a bare reference or an errors[] entry is not
    seeded); linked items group by kind, hidden or not."""
    meta = _meta(row)
    if row["source"] == "x":
        if "published_at" not in meta:
            return None
        return "bookmarked_post" if meta.get("bookmarked") else "referenced_post"
    if row["source"] == "x_article":
        return "article"
    if row["source"] == "x_link":
        return f"link_{meta.get('kind')}"
    return None


def _rows(conn: sqlite3.Connection) -> dict[str, list[sqlite3.Row]]:
    out: dict[str, list[sqlite3.Row]] = {}
    for row in conn.execute("SELECT * FROM bookmarks ORDER BY bookmark_id"):
        cat = _category(row)
        if cat is not None:
            out.setdefault(cat, []).append(row)
    return out


CATEGORIES = ["bookmarked_post", "referenced_post", "article", "link_github_repo", "link_web_page"]


def _nonnull(row: sqlite3.Row, skip: tuple[str, ...] = ()) -> frozenset[str]:
    return frozenset(k for k in row.keys() if row[k] is not None and k not in skip)


def _assert_between(name: str, seed: list[frozenset], writer: list[frozenset]) -> None:
    assert writer and seed, name
    required = frozenset.intersection(*writer)
    allowed = frozenset.union(*writer)
    for shape in seed:
        assert required <= shape, (name, "seed lacks", sorted(required - shape))
        assert shape <= allowed, (name, "seed adds", sorted(shape - allowed))


def _children(conn: sqlite3.Connection, table: str, rows: list[sqlite3.Row], extra: str = ""):
    ids = [r["bookmark_id"] for r in rows]
    marks = ",".join("?" * len(ids))
    return conn.execute(
        f"SELECT * FROM {table} WHERE bookmark_id IN ({marks}) {extra}", ids
    ).fetchall()


# ── shapes ──────────────────────────────────────────────────────────


def test_every_seed_row_has_a_writer_counterpart(writer_db, seed_db) -> None:
    assert set(_rows(seed_db)) == set(CATEGORIES)
    assert set(CATEGORIES) <= set(_rows(writer_db))


@pytest.mark.parametrize("category", CATEGORIES)
def test_bookmarks_columns_and_metadata_keys_match(writer_db, seed_db, category) -> None:
    writer, seed = _rows(writer_db)[category], _rows(seed_db)[category]
    _assert_between(f"{category} columns", [_nonnull(r, ("bookmark_id",)) for r in seed],
                    [_nonnull(r, ("bookmark_id",)) for r in writer])
    writer_keys = [frozenset(_meta(r)) for r in writer]
    if category.endswith("_post"):
        # Requested by the connector and kept by the writer when present,
        # but absent from the documentation-derived fixtures.
        assert OPTIONAL_POST_KEYS <= set(XConnectorConfig().post_fields)
        writer_keys.append(frozenset.union(*writer_keys) | OPTIONAL_POST_KEYS)
    _assert_between(f"{category} metadata", [frozenset(_meta(r)) for r in seed], writer_keys)
    authors = [frozenset(_meta(r)["author"]) for r in writer if isinstance(_meta(r).get("author"), dict)]
    if authors:
        _assert_between(f"{category} author", [frozenset(_meta(r)["author"]) for r in seed], authors)
    kinds = {_meta(r)["kind"] for r in writer}
    assert {_meta(r)["kind"] for r in seed} == kinds


@pytest.mark.parametrize("category", ["bookmarked_post", "referenced_post"])
def test_post_titles_follow_the_writer_rule(writer_db, seed_db, category) -> None:
    for conn in (writer_db, seed_db):
        for row in _rows(conn)[category]:
            handle = _meta(row)["author"]["username"]
            assert row["title"].startswith(f"@{handle}: ")
            assert len(row["title"]) <= len(f"@{handle}: ") + 80


@pytest.mark.parametrize("category", CATEGORIES)
def test_content_rows_match(writer_db, seed_db, category) -> None:
    writer_rows = [r for r in _rows(writer_db)[category] if r["content_visible"]]
    seed_rows = [r for r in _rows(seed_db)[category] if r["content_visible"]]
    w_live = _children(writer_db, "scrape_results", writer_rows, "AND pass = 'live'")
    s_live = _children(seed_db, "scrape_results", seed_rows, "AND pass = 'live'")
    skip = ("scrape_result_id", "id", "bookmark_id")
    _assert_between(f"{category} scrape_results", [_nonnull(r, skip) for r in s_live],
                    [_nonnull(r, skip) for r in w_live])

    w_hist = _children(writer_db, "scrape_history", writer_rows)
    s_hist = _children(seed_db, "scrape_history", seed_rows)
    skip = ("scrape_history_id", "bookmark_id")
    _assert_between(f"{category} scrape_history", [_nonnull(r, skip) for r in s_hist],
                    [_nonnull(r, skip) for r in w_hist])

    def methods(rows):
        return {(r["retrieval_method"], r["scrape_pass"], r["extractor_name"], r["extractor_version"])
                for r in rows if r["coverage"] in ("full", "partial")}

    assert methods(s_hist) <= methods(w_hist)


def test_hidden_links_store_no_content(writer_db, seed_db) -> None:
    for conn in (writer_db, seed_db):
        hidden = conn.execute(
            "SELECT * FROM bookmarks WHERE source = 'x_link' AND content_visible = 0"
        ).fetchall()
        assert hidden
        for row in hidden:
            assert row["enrichment_source"] is None and row["title"]
            for table in ("scrape_results", "scrape_history"):
                assert not conn.execute(
                    f"SELECT 1 FROM {table} WHERE bookmark_id = ?", (row["bookmark_id"],)
                ).fetchone()
        # The parent post's stored text never quotes the hidden locator.
        texts = "\n".join(r[0] for r in conn.execute(
            "SELECT content_text FROM scrape_results sr JOIN bookmarks b USING (bookmark_id) "
            "WHERE b.source = 'x' AND content_text IS NOT NULL"))
        assert "[hidden link]" in texts
        assert not any(row["url"] in texts for row in hidden)


def test_item_links_match(writer_db, seed_db) -> None:
    def shapes(conn):
        out: dict[str, list[frozenset]] = {}
        resolutions: dict[str, set] = {}
        for r in conn.execute("SELECT * FROM item_links"):
            out.setdefault(r["relation"], []).append(_nonnull(r, ("item_link_id",)))
            resolutions.setdefault(r["relation"], set()).add(r["locator_resolution"])
        return out, resolutions

    writer, w_res = shapes(writer_db)
    seed, s_res = shapes(seed_db)
    for relation, seed_shapes in seed.items():
        _assert_between(f"item_links {relation}", seed_shapes, writer[relation])
        assert s_res[relation] <= w_res[relation], relation
