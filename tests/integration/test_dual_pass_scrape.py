"""Integration tests for the dual-pass scrape pipeline.

Covers all 8 success criteria from the work-stream brief:

- SC1: ``scrape_all`` runs dual-pass for every ``content_visible=1`` bookmark
- SC2: per-domain ≤1 rps to live origins; ≤0.5 rps global to IA
- SC3: at_capture asks IA for the ±365d window around ``captured_at``
- SC4: recent_archive fallback fires when at_capture → ``no_archive``
- SC5: enrichment_source priority (at_capture > recent_archive > live_fallback > none)
- SC6: auth-wall heuristic produces ``scrape_status='auth_required'``
- SC7: idempotent re-runs (row count constant, UPSERT)
- SC8: HTTP cache prevents network re-fetch on the second run

Network is fully mocked via ``respx``. ``ScrapeClient`` writes its caches
into ``tmp_path`` to keep tests hermetic.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import respx

from muninn.scrape.auth_wall import detect_auth_wall
from muninn.scrape.client import (
    IA_CDX_BASE,
    IA_WEB_BASE,
    ScrapeClient,
)
from muninn.scrape.http_cache import HttpCache
from muninn.scrape.pipeline import scrape_all
from muninn.scrape.rate_limiter import RateLimiter


# ── Fixtures ─────────────────────────────────────────────────────

SAMPLE_HTML = (
    "<html><head><title>Test</title></head><body>"
    "<article><p>" + ("Hello world. " * 60) + "</p></article>"
    "</body></html>"
)
AUTH_HTML = (
    '<html><body>'
    '<form action="/login" method="post">'
    '<input type="password" name="password">'
    '<input type="submit" value="Log in">'
    "<p>Please sign in to continue.</p>"
    "</form></body></html>"
)

CDX_RESPONSE_HIT = json.dumps([
    ["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"],
    ["com,example)/page", "20240115120000", "https://example.com/page",
     "text/html", "200", "ABC123", "1234"],
])
CDX_RESPONSE_EMPTY = json.dumps([
    ["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"],
])


def _epoch(iso: str) -> int:
    """ISO-8601 (UTC) → epoch seconds."""
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def _insert_bookmark(
    conn: sqlite3.Connection,
    url: str,
    captured_at_iso: str | None = "2024-01-15T12:00:00+00:00",
    *,
    source: str = "netscape",
    source_id: str | None = None,
    content_visible: int = 1,
) -> int:
    """Insert a bookmark and return its ``bookmark_id``."""
    captured_at = _epoch(captured_at_iso) if captured_at_iso else 0
    sid = source_id or url
    cur = conn.execute(
        """INSERT INTO bookmarks
           (source, source_id, captured_at, title, url, content_visible)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (source, sid, captured_at, f"Title for {url}", url, content_visible),
    )
    conn.commit()
    return int(cur.lastrowid)


@pytest.fixture
def cache_dirs(tmp_path: Path) -> tuple[Path, Path]:
    http_dir = tmp_path / "http-cache"
    scrape_dir = tmp_path / "scrape-cache"
    http_dir.mkdir()
    scrape_dir.mkdir()
    return http_dir, scrape_dir


@pytest.fixture
def make_client(cache_dirs):
    """Factory returning fresh ScrapeClients pointing at hermetic cache dirs."""
    http_dir, scrape_dir = cache_dirs

    def _factory() -> ScrapeClient:
        return ScrapeClient(http_cache_dir=http_dir, scrape_cache_dir=scrape_dir)

    return _factory


# ── SC1: dual-pass for all content_visible=1 ─────────────────────

@pytest.mark.asyncio
@respx.mock
async def test_sc1_dual_pass_all_visible(fresh_db, make_client):
    db = fresh_db
    bm1 = _insert_bookmark(db, "https://example.com/page1")
    bm2 = _insert_bookmark(db, "https://example.com/page2",
                           "2024-06-01T12:00:00+00:00")
    _insert_bookmark(db, "https://example.com/hidden", None,
                     content_visible=0)

    respx.get(IA_CDX_BASE).mock(
        return_value=httpx.Response(200, text=CDX_RESPONSE_EMPTY)
    )
    respx.get("https://example.com/page1").mock(
        return_value=httpx.Response(
            200, text=SAMPLE_HTML, headers={"content-type": "text/html"}
        )
    )
    respx.get("https://example.com/page2").mock(
        return_value=httpx.Response(
            200, text=SAMPLE_HTML, headers={"content-type": "text/html"}
        )
    )

    client = make_client()
    stats = await scrape_all(db, client=client, concurrency=2)
    await client.close()

    assert stats["processed"] == 2
    assert stats["errors"] == 0

    for bm_id in (bm1, bm2):
        rows = db.execute(
            "SELECT pass, scrape_status FROM scrape_results WHERE bookmark_id = ?",
            (bm_id,),
        ).fetchall()
        passes = {r["pass"]: r["scrape_status"] for r in rows}
        assert "at_capture" in passes
        assert "recent_archive" in passes
        assert "live" in passes

    # Hidden bookmark must be untouched.
    n = db.execute(
        """SELECT COUNT(*) AS c FROM scrape_results sr
           JOIN bookmarks b ON sr.bookmark_id = b.bookmark_id
           WHERE b.content_visible = 0"""
    ).fetchone()["c"]
    assert n == 0


# ── SC2: politeness ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sc2_per_domain_rps():
    rl = RateLimiter()
    url = "https://example.com/page"
    timestamps = []
    for _ in range(3):
        await rl.wait_for_domain(url, 1.0)
        timestamps.append(time.monotonic())
    for i in range(1, len(timestamps)):
        gap = timestamps[i] - timestamps[i - 1]
        assert gap >= 0.95, f"per-domain gap too short: {gap:.3f}s"


@pytest.mark.asyncio
async def test_sc2_ia_global_rps():
    rl = RateLimiter()
    timestamps = []
    for _ in range(3):
        await rl.wait_for_global("internet_archive", 2.0)
        timestamps.append(time.monotonic())
    for i in range(1, len(timestamps)):
        gap = timestamps[i] - timestamps[i - 1]
        assert gap >= 1.95, f"IA gap too short: {gap:.3f}s"


# ── SC3: at_capture window is ±365 days ──────────────────────────

@pytest.mark.asyncio
@respx.mock
async def test_sc3_at_capture_window(fresh_db, make_client):
    db = fresh_db
    captured_iso = "2024-01-15T12:00:00+00:00"
    bm_id = _insert_bookmark(db, "https://example.com/page", captured_iso)

    captured_dt = datetime.fromisoformat(captured_iso)
    from_dt = captured_dt - timedelta(days=365)
    to_dt = captured_dt + timedelta(days=365)

    cdx_requests: list[dict] = []

    def cdx_handler(request):
        cdx_requests.append(dict(request.url.params))
        return httpx.Response(200, text=CDX_RESPONSE_EMPTY)

    respx.get(IA_CDX_BASE).mock(side_effect=cdx_handler)
    respx.get("https://example.com/page").mock(
        return_value=httpx.Response(
            200, text=SAMPLE_HTML, headers={"content-type": "text/html"}
        )
    )

    client = make_client()
    await scrape_all(db, client=client)
    await client.close()

    # First CDX call is the at_capture lookup with window params.
    assert len(cdx_requests) >= 1
    first = cdx_requests[0]
    assert first["from"] == from_dt.strftime("%Y%m%d%H%M%S")
    assert first["to"] == to_dt.strftime("%Y%m%d%H%M%S")
    assert first["closest"] == captured_dt.strftime("%Y%m%d%H%M%S")

    row = db.execute(
        "SELECT scrape_status, target_timestamp FROM scrape_results "
        "WHERE bookmark_id = ? AND pass = 'at_capture'",
        (bm_id,),
    ).fetchone()
    assert row["scrape_status"] == "no_archive"
    assert row["target_timestamp"] == _epoch(captured_iso)


# ── SC4: recent_archive fallback fires when at_capture → no_archive ──

@pytest.mark.asyncio
@respx.mock
async def test_sc4_recent_archive_fallback(fresh_db, make_client):
    db = fresh_db
    bm_id = _insert_bookmark(db, "https://example.com/page")

    def cdx_handler(request):
        params = dict(request.url.params)
        # at_capture has 'closest'; recent_archive uses limit=-1.
        if "closest" in params:
            return httpx.Response(200, text=CDX_RESPONSE_EMPTY)
        return httpx.Response(200, text=CDX_RESPONSE_HIT)

    respx.get(IA_CDX_BASE).mock(side_effect=cdx_handler)
    respx.get(url__startswith=IA_WEB_BASE).mock(
        return_value=httpx.Response(
            200, text=SAMPLE_HTML, headers={"content-type": "text/html"}
        )
    )
    respx.get("https://example.com/page").mock(
        return_value=httpx.Response(
            200, text=SAMPLE_HTML, headers={"content-type": "text/html"}
        )
    )

    client = make_client()
    await scrape_all(db, client=client)
    await client.close()

    rows = db.execute(
        "SELECT pass, scrape_status, archive_url, actual_snapshot_at "
        "FROM scrape_results WHERE bookmark_id = ?",
        (bm_id,),
    ).fetchall()
    by_pass = {r["pass"]: r for r in rows}
    assert by_pass["at_capture"]["scrape_status"] == "no_archive"
    assert by_pass["recent_archive"]["scrape_status"] == "ok"
    assert by_pass["recent_archive"]["archive_url"] is not None
    assert by_pass["recent_archive"]["actual_snapshot_at"] is not None
    assert "live" in by_pass

    src = db.execute(
        "SELECT enrichment_source FROM bookmarks WHERE bookmark_id = ?",
        (bm_id,),
    ).fetchone()["enrichment_source"]
    assert src == "recent_archive"


# ── SC5: enrichment_source priority ──────────────────────────────

@pytest.mark.asyncio
@respx.mock
async def test_sc5_priority_at_capture_wins(fresh_db, make_client):
    db = fresh_db
    bm_id = _insert_bookmark(db, "https://example.com/page")

    respx.get(IA_CDX_BASE).mock(
        return_value=httpx.Response(200, text=CDX_RESPONSE_HIT)
    )
    respx.get(url__startswith=IA_WEB_BASE).mock(
        return_value=httpx.Response(
            200, text=SAMPLE_HTML, headers={"content-type": "text/html"}
        )
    )
    respx.get("https://example.com/page").mock(
        return_value=httpx.Response(
            200, text=SAMPLE_HTML, headers={"content-type": "text/html"}
        )
    )

    client = make_client()
    await scrape_all(db, client=client)
    await client.close()

    src = db.execute(
        "SELECT enrichment_source FROM bookmarks WHERE bookmark_id = ?",
        (bm_id,),
    ).fetchone()["enrichment_source"]
    assert src == "at_capture"


@pytest.mark.asyncio
@respx.mock
async def test_sc5_priority_live_fallback(fresh_db, make_client):
    db = fresh_db
    bm_id = _insert_bookmark(db, "https://example.com/page")

    respx.get(IA_CDX_BASE).mock(
        return_value=httpx.Response(200, text=CDX_RESPONSE_EMPTY)
    )
    respx.get("https://example.com/page").mock(
        return_value=httpx.Response(
            200, text=SAMPLE_HTML, headers={"content-type": "text/html"}
        )
    )

    client = make_client()
    await scrape_all(db, client=client)
    await client.close()

    src = db.execute(
        "SELECT enrichment_source FROM bookmarks WHERE bookmark_id = ?",
        (bm_id,),
    ).fetchone()["enrichment_source"]
    assert src == "live_fallback"


@pytest.mark.asyncio
@respx.mock
async def test_sc5_priority_none(fresh_db, make_client):
    db = fresh_db
    bm_id = _insert_bookmark(db, "https://example.com/page")

    respx.get(IA_CDX_BASE).mock(
        return_value=httpx.Response(200, text=CDX_RESPONSE_EMPTY)
    )
    respx.get("https://example.com/page").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    client = make_client()
    await scrape_all(db, client=client)
    await client.close()

    src = db.execute(
        "SELECT enrichment_source FROM bookmarks WHERE bookmark_id = ?",
        (bm_id,),
    ).fetchone()["enrichment_source"]
    assert src == "none"

    live = db.execute(
        "SELECT scrape_status FROM scrape_results "
        "WHERE bookmark_id = ? AND pass = 'live'",
        (bm_id,),
    ).fetchone()
    assert live["scrape_status"] == "network_error"


# ── SC6: auth-wall detection ─────────────────────────────────────

def test_sc6_auth_wall_positive():
    assert detect_auth_wall(AUTH_HTML) is True


def test_sc6_auth_wall_negative():
    assert detect_auth_wall(SAMPLE_HTML) is False


@pytest.mark.asyncio
@respx.mock
async def test_sc6_auth_wall_in_db(fresh_db, make_client):
    db = fresh_db
    bm_id = _insert_bookmark(db, "https://example.com/page")

    respx.get(IA_CDX_BASE).mock(
        return_value=httpx.Response(200, text=CDX_RESPONSE_EMPTY)
    )
    respx.get("https://example.com/page").mock(
        return_value=httpx.Response(
            200, text=AUTH_HTML, headers={"content-type": "text/html"}
        )
    )

    client = make_client()
    await scrape_all(db, client=client)
    await client.close()

    row = db.execute(
        "SELECT scrape_status FROM scrape_results "
        "WHERE bookmark_id = ? AND pass = 'live'",
        (bm_id,),
    ).fetchone()
    assert row["scrape_status"] == "auth_required"


# ── SC7: idempotency — UPSERT keeps row count constant ──────────

@pytest.mark.asyncio
@respx.mock
async def test_sc7_idempotent_reruns(fresh_db, make_client):
    db = fresh_db
    _insert_bookmark(db, "https://example.com/page")

    respx.get(IA_CDX_BASE).mock(
        return_value=httpx.Response(200, text=CDX_RESPONSE_EMPTY)
    )
    respx.get("https://example.com/page").mock(
        return_value=httpx.Response(
            200, text=SAMPLE_HTML, headers={"content-type": "text/html"}
        )
    )

    c1 = make_client()
    await scrape_all(db, client=c1)
    await c1.close()
    n1 = db.execute("SELECT COUNT(*) AS c FROM scrape_results").fetchone()["c"]

    c2 = make_client()
    await scrape_all(db, client=c2)
    await c2.close()
    n2 = db.execute("SELECT COUNT(*) AS c FROM scrape_results").fetchone()["c"]

    assert n1 == n2 > 0, f"row count changed across re-runs: {n1} → {n2}"


# ── SC8: HTTP cache prevents re-fetch on second run ──────────────

@pytest.mark.asyncio
@respx.mock
async def test_sc8_cache_skips_network_on_rerun(fresh_db, make_client):
    db = fresh_db
    _insert_bookmark(db, "https://example.com/page")

    respx.get(IA_CDX_BASE).mock(
        return_value=httpx.Response(200, text=CDX_RESPONSE_EMPTY)
    )
    respx.get("https://example.com/page").mock(
        return_value=httpx.Response(
            200, text=SAMPLE_HTML, headers={"content-type": "text/html"}
        )
    )

    c1 = make_client()
    await scrape_all(db, client=c1)
    n1 = len(c1.request_log)
    await c1.close()
    assert n1 > 0, "first run should make network requests"

    c2 = make_client()
    await scrape_all(db, client=c2)
    n2 = len(c2.request_log)
    await c2.close()
    assert n2 == 0, f"second run should be served from cache, made {n2} requests"


# ── Bonus utility tests ──────────────────────────────────────────

def test_http_cache_round_trip(tmp_path):
    cache = HttpCache(tmp_path / "cache")
    assert cache.get("k") is None
    cache.put("k", {"status": 200})
    assert cache.get("k") == {"status": 200}
    assert cache.has("k")


def test_http_cache_raw_html_round_trip(tmp_path):
    cache = HttpCache(tmp_path / "cache")
    body = b"<html>raw bytes</html>" * 100
    p = cache.put_raw("k", body)
    assert Path(p).exists()
    assert cache.get_raw("k") == body
