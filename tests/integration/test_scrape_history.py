"""`muninn scrape` with X rows: skip API-content rows, scrape linked items,
append scrape_history revisions, live-cache TTL and --refresh (RI-01 D9).

Network fully mocked with respx; caches live in tmp_path.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx
import pytest
import respx
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from muninn.consumers.packets import content_digest, locator_source_id, normalize_text
from muninn.intake_records import content_revision_record
from muninn.scrape.client import IA_CDX_BASE, IA_WEB_BASE, ScrapeClient
from muninn.scrape.extract import EXTRACTOR_NAME, EXTRACTOR_VERSION
from muninn.scrape.http_cache import HttpCache
from muninn.scrape.pipeline import _list_visible_bookmarks, scrape_all
from muninn.scrape.rate_limiter import RateLimiter

ROOT = Path(__file__).resolve().parents[2]
PAGE = "<html><body><article><p>" + ("Synthetic linked article text. " * 20) + "</p></article></body></html>"
PAGE_V2 = PAGE.replace("Synthetic linked", "Revised synthetic linked")
AUTH = ('<html><body><form action="/login" method="post"><input type="password" name="password">'
        "<p>Please sign in to continue.</p></form></body></html>")
CDX_EMPTY = json.dumps([["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"]])
CDX_HIT = json.dumps([
    ["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"],
    ["x", "20261001120000", "x", "text/html", "200", "D", "1"],
])
SNAPSHOT_EPOCH = 1790856000  # 2026-10-01T12:00:00Z
LINK = "https://blog.example.org/posts/write-up"
HTML_URL = "https://example.com/browser-page"


def _insert(conn, source, source_id, url, *, captured_at=1790000000, meta=None) -> int:
    cur = conn.execute(
        "INSERT INTO bookmarks (source, source_id, captured_at, title, url, source_metadata) "
        "VALUES (?, ?, ?, 'T', ?, ?)",
        (source, source_id, captured_at, url, json.dumps(meta) if meta else None),
    )
    conn.commit()
    return int(cur.lastrowid)


@pytest.fixture
def seeded(fresh_db):
    ids = {
        "post": _insert(fresh_db, "x", "1111111111111111101",
                        "https://x.com/example_author/status/1111111111111111101",
                        meta={"bookmarked": True}),
        "article": _insert(fresh_db, "x_article", "1111111111111111905",
                           "https://x.com/i/article/1111111111111111905"),
        "link": _insert(fresh_db, "x_link", locator_source_id(LINK), LINK,
                        meta={"kind": "web_page", "item_id": f"web:{locator_source_id(LINK)}"}),
        "html": _insert(fresh_db, "bookmarks_html", "h1", HTML_URL),
    }
    # The post's API text, as `ingest x` leaves it.
    fresh_db.execute(
        "INSERT INTO scrape_results (bookmark_id, pass, fetched_at, scrape_status, content_text) "
        "VALUES (?, 'live', 1, 'ok', 'api text')",
        (ids["post"],),
    )
    fresh_db.execute("UPDATE bookmarks SET enrichment_source = 'live_fallback' WHERE bookmark_id = ?",
                     (ids["post"],))
    fresh_db.commit()
    return fresh_db, ids


class NoWait(RateLimiter):
    """Politeness is tested elsewhere; these tests shouldn't sleep."""

    async def wait_for_domain(self, url: str, min_interval: float) -> None:
        return None

    async def wait_for_global(self, group: str, min_interval: float) -> None:
        return None


@pytest.fixture
def make_client(tmp_path):
    def factory(**kw) -> ScrapeClient:
        return ScrapeClient(http_cache_dir=tmp_path / "http", scrape_cache_dir=tmp_path / "scrape",
                            rate_limiter=NoWait(), **kw)

    return factory


def history(conn, bookmark_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM scrape_history WHERE bookmark_id = ? ORDER BY scrape_history_id", (bookmark_id,)
    ).fetchall()


def _validator():
    schemas = {p.name.removesuffix(".schema.json"): json.loads(p.read_text())
               for p in (ROOT / "schemas" / "json" / "intake").glob("*.schema.json")}
    registry = Registry().with_resources((s["$id"], Resource.from_contents(s)) for s in schemas.values())
    return Draft202012Validator(schemas["content-revision"], registry=registry)


async def _scrape(conn, client, **kw):
    try:
        return await scrape_all(conn, client=client, **kw)
    finally:
        await client.close()


def test_selection_skips_x_posts_and_articles(seeded) -> None:
    conn, ids = seeded
    rows = _list_visible_bookmarks(conn)
    assert {r["bookmark_id"] for r in rows} == {ids["link"], ids["html"]}
    assert {r["bookmark_id"] for r in _list_visible_bookmarks(conn, sources=["x_link", "x"])} == {ids["link"]}
    assert _list_visible_bookmarks(conn, limit=1, sources=["bookmarks_html"])[0]["bookmark_id"] == ids["html"]


@pytest.mark.asyncio
@respx.mock
async def test_scrape_skips_x_rows_and_records_link_history(seeded, make_client) -> None:
    conn, ids = seeded
    respx.get(IA_CDX_BASE).mock(return_value=httpx.Response(200, text=CDX_EMPTY))
    link_route = respx.get(LINK).mock(return_value=httpx.Response(200, text=PAGE))
    html_route = respx.get(HTML_URL).mock(return_value=httpx.Response(200, text=PAGE))
    client = make_client()
    await _scrape(conn, client)

    fetched = {entry["url"] for entry in client.request_log if entry["type"] == "live"}
    assert fetched == {LINK, HTML_URL} and link_route.called and html_route.called
    post_rows = conn.execute("SELECT pass, content_text FROM scrape_results WHERE bookmark_id = ?",
                             (ids["post"],)).fetchall()
    assert [tuple(r) for r in post_rows] == [("live", "api text")]  # untouched
    assert conn.execute("SELECT COUNT(*) FROM scrape_results WHERE bookmark_id = ?",
                        (ids["article"],)).fetchone()[0] == 0

    (rev,) = history(conn, ids["link"])
    text = conn.execute("SELECT content_text FROM scrape_results WHERE bookmark_id = ? AND pass = 'live'",
                        (ids["link"],)).fetchone()[0]
    assert rev["item_ref"] == f"web:{locator_source_id(LINK)}"
    assert (rev["retrieval_method"], rev["scrape_pass"], rev["coverage"]) == ("live", "live", "full")
    assert rev["content_sha256"] == content_digest(text) and rev["content_text"] == normalize_text(text)
    assert (rev["extractor_name"], rev["extractor_version"]) == (EXTRACTOR_NAME, EXTRACTOR_VERSION)
    assert rev["http_status"] == 200 and rev["observed_at"] is None and rev["published_at"] is None
    assert rev["final_locator"] is None  # same as the item's locator
    # Browser bookmarks get revisions too, keyed by their page identity, with ADD_DATE as observation.
    (html_rev,) = history(conn, ids["html"])
    assert html_rev["item_ref"] == f"web:{locator_source_id(HTML_URL)}"
    assert html_rev["observed_at"] == 1790000000
    validator = _validator()
    for row in conn.execute("SELECT * FROM scrape_history"):
        assert not list(validator.iter_errors(content_revision_record(row, include_text=True)))

    # Re-run: served from cache, unchanged content → no new revision.
    client2 = make_client()
    await _scrape(conn, client2)
    assert client2.request_log == [] or all(e["type"] == "cdx" for e in client2.request_log)
    assert len(history(conn, ids["link"])) == 1


@pytest.mark.asyncio
@respx.mock
async def test_identical_live_and_archive_text_is_one_current_revision(seeded, make_client) -> None:
    conn, ids = seeded
    respx.get(IA_CDX_BASE).mock(return_value=httpx.Response(200, text=CDX_HIT))
    respx.get(f"{IA_WEB_BASE}/20261001120000id_/{LINK}").mock(return_value=httpx.Response(200, text=PAGE))
    respx.get(f"{IA_WEB_BASE}/20261001120000id_/{HTML_URL}").mock(
        return_value=httpx.Response(200, text=PAGE_V2))
    respx.get(LINK).mock(return_value=httpx.Response(200, text=PAGE))
    respx.get(HTML_URL).mock(return_value=httpx.Response(200, text=PAGE))
    await _scrape(conn, make_client())

    (rev,) = history(conn, ids["link"])
    assert rev["retrieval_method"] == "live"  # the current capture wins the shared digest
    # Linked items prefer the live pass; browser bookmarks keep at_capture first.
    sources = dict(conn.execute("SELECT bookmark_id, enrichment_source FROM bookmarks").fetchall())
    assert sources[ids["link"]] == "live_fallback" and sources[ids["html"]] == "at_capture"

    # Different archive text → a separate historical revision with its snapshot time.
    html_revs = history(conn, ids["html"])
    assert [r["retrieval_method"] for r in html_revs] == ["live", "archive"]
    archive = html_revs[1]
    assert archive["scrape_pass"] == "at_capture" and archive["archive_snapshot_at"] == SNAPSHOT_EPOCH
    assert archive["final_locator"].startswith("https://web.archive.org/web/20261001120000id_/")
    record = content_revision_record(archive)
    assert record["freshness"] == "historical" and record["archive_snapshot_at"] == "2026-10-01T12:00:00Z"
    assert not list(_validator().iter_errors(record))


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize(
    "response, coverage",
    [
        (httpx.Response(404, text="gone"), "deleted"),
        (httpx.Response(200, text=AUTH), "blocked"),
        (httpx.Response(403, text="no"), "blocked"),
        (httpx.Response(200, text="<html><body></body></html>"), "empty"),
        (httpx.Response(503, text="later"), None),
        (httpx.ConnectError("synthetic"), None),
    ],
)
async def test_coverage_mapping(seeded, make_client, response, coverage) -> None:
    conn, ids = seeded
    respx.get(IA_CDX_BASE).mock(return_value=httpx.Response(200, text=CDX_EMPTY))
    route = respx.get(LINK)
    if isinstance(response, Exception):
        route.mock(side_effect=response)
    else:
        route.mock(return_value=response)
    respx.get(HTML_URL).mock(return_value=httpx.Response(200, text=PAGE))
    await _scrape(conn, make_client(), sources=["x_link"])
    revs = history(conn, ids["link"])
    if coverage is None:
        assert revs == []  # operational failure, not a content state
        return
    (rev,) = revs
    assert rev["coverage"] == coverage and rev["char_count"] == 0
    assert rev["content_sha256"] == content_digest("") and rev["content_text"] is None
    assert not list(_validator().iter_errors(content_revision_record(rev, include_text=True)))


@pytest.mark.asyncio
@respx.mock
async def test_refresh_bypasses_the_live_cache_and_captures_a_new_revision(seeded, make_client) -> None:
    conn, ids = seeded
    respx.get(IA_CDX_BASE).mock(return_value=httpx.Response(200, text=CDX_EMPTY))
    route = respx.get(LINK).mock(side_effect=[httpx.Response(200, text=PAGE), httpx.Response(200, text=PAGE_V2)])
    await _scrape(conn, make_client(), sources=["x_link"])

    cached = make_client()
    await _scrape(conn, cached, sources=["x_link"])
    assert route.call_count == 1 and len(history(conn, ids["link"])) == 1

    fresh = make_client(refresh=True)
    await _scrape(conn, fresh, sources=["x_link"])
    assert route.call_count == 2
    revs = history(conn, ids["link"])
    assert len(revs) == 2 and "Revised synthetic" in revs[1]["content_text"]
    current = conn.execute("SELECT content_text FROM scrape_results WHERE bookmark_id = ? AND pass = 'live'",
                           (ids["link"],)).fetchone()[0]
    assert "Revised synthetic" in current


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.mark.asyncio
@respx.mock
async def test_live_cache_ttl_expires_with_an_injected_clock(tmp_path) -> None:
    route = respx.get(LINK).mock(return_value=httpx.Response(200, text=PAGE))
    import time

    clock = Clock(time.time())
    client = ScrapeClient(http_cache_dir=tmp_path / "h", scrape_cache_dir=tmp_path / "s",
                          live_cache_ttl=3600, clock=clock, rate_limiter=NoWait())
    try:
        assert (await client.fetch_live(LINK))["from_cache"] is False
        clock.now += 3599
        assert (await client.fetch_live(LINK))["from_cache"] is True
        clock.now += 2
        assert (await client.fetch_live(LINK))["from_cache"] is False
        assert route.call_count == 2
    finally:
        await client.close()


@pytest.mark.asyncio
@respx.mock
async def test_archive_cache_never_expires(tmp_path) -> None:
    import time

    archive = f"{IA_WEB_BASE}/20261001120000id_/{LINK}"
    route = respx.get(archive).mock(return_value=httpx.Response(200, text=PAGE))
    clock = Clock(time.time())
    client = ScrapeClient(http_cache_dir=tmp_path / "h", scrape_cache_dir=tmp_path / "s",
                          live_cache_ttl=1, refresh=True, clock=clock,
                          rate_limiter=NoWait())
    try:
        await client.fetch_archive(LINK, "20261001120000")
        clock.now += 10**7
        assert (await client.fetch_archive(LINK, "20261001120000"))["from_cache"] is True
        assert route.call_count == 1
    finally:
        await client.close()


def test_http_cache_max_age(tmp_path) -> None:
    clock = Clock(0.0)
    cache = HttpCache(tmp_path / "c", clock=clock)
    cache.put("k", {"a": 1})
    mtime = (tmp_path / "c").glob("*.json").__next__().stat().st_mtime
    clock.now = mtime + 10
    assert cache.get("k", max_age=11) == {"a": 1}
    assert cache.get("k", max_age=9) is None
    assert cache.get("k") == {"a": 1}  # no max_age: never expires


@pytest.mark.asyncio
@respx.mock
async def test_redirect_final_url_is_sanitized(seeded, make_client) -> None:
    """A redirect target carrying a token-shaped param is stored sanitized,
    in `scrape_results.final_url` as in the revision's `final_locator`."""
    conn, ids = seeded
    secret = "synthetic-redirect-token-CANARY-0002"
    landing = "https://blog.example.org/posts/landing"
    respx.get(IA_CDX_BASE).mock(return_value=httpx.Response(200, text=CDX_EMPTY))
    respx.get(LINK).mock(return_value=httpx.Response(
        302, headers={"location": f"{landing}?access_token={secret}&page=2&utm_source=x"}))
    respx.get(landing).mock(return_value=httpx.Response(200, text=PAGE))
    respx.get(HTML_URL).mock(return_value=httpx.Response(200, text=PAGE))
    await _scrape(conn, make_client())

    final_url = conn.execute(
        "SELECT final_url FROM scrape_results WHERE bookmark_id = ? AND pass = 'live'", (ids["link"],)
    ).fetchone()[0]
    assert final_url == f"{landing}?page=2"
    (rev,) = history(conn, ids["link"])
    assert rev["final_locator"] == final_url
    dump = "\n".join(conn.iterdump())
    assert secret not in dump and "access_token" not in dump
