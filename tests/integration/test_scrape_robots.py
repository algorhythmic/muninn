"""robots.txt (RFC 9309) on the live pass (CLAUDE.md known gap 4).

Network fully mocked with respx (unmocked requests fail); caches in tmp_path.
"""

from __future__ import annotations

import time

import httpx
import pytest
import respx

from muninn.scrape.client import IA_CDX_BASE, IA_WEB_BASE, ScrapeClient
from muninn.scrape.pipeline import scrape_all
from tests.integration.test_scrape_history import CDX_EMPTY, CDX_HIT, PAGE, Clock, NoWait

SITE = "https://blog.example.org"
PUBLIC = f"{SITE}/posts/public"
PRIVATE = f"{SITE}/private/notes"
ROBOTS = f"{SITE}/robots.txt"
RULES = (
    "User-agent: *\nDisallow: /\n\n"
    "User-agent: Muninn\nDisallow: /private/\nAllow: /\n"
)


def _insert(conn, url: str, source_id: str) -> int:
    cur = conn.execute(
        "INSERT INTO bookmarks (source, source_id, captured_at, title, url) "
        "VALUES ('bookmarks_html', ?, 1790000000, 'T', ?)",
        (source_id, url),
    )
    conn.commit()
    return int(cur.lastrowid)


def _live(conn, bookmark_id: int):
    return conn.execute(
        "SELECT scrape_status, http_status, final_url, content_text, error_detail "
        "FROM scrape_results WHERE bookmark_id = ? AND pass = 'live'",
        (bookmark_id,),
    ).fetchone()


@pytest.fixture
def make_client(tmp_path):
    clock = Clock(time.time())

    def factory(**kw) -> ScrapeClient:
        return ScrapeClient(http_cache_dir=tmp_path / "http", scrape_cache_dir=tmp_path / "scrape",
                            rate_limiter=NoWait(), clock=clock, **kw)

    factory.clock = clock
    return factory


async def _scrape(conn, client) -> dict:
    try:
        return await scrape_all(conn, client=client)
    finally:
        await client.close()


@pytest.mark.asyncio
@respx.mock
async def test_disallowed_url_is_not_fetched(fresh_db, make_client) -> None:
    """Rules are matched for the `Muninn` product token, not the `*` group."""
    respx.get(IA_CDX_BASE).mock(return_value=httpx.Response(200, text=CDX_EMPTY))
    robots = respx.get(ROBOTS).mock(return_value=httpx.Response(200, text=RULES))
    public = respx.get(PUBLIC).mock(return_value=httpx.Response(200, text=PAGE))
    private = respx.get(PRIVATE).mock(return_value=httpx.Response(200, text=PAGE))
    ok_id, blocked_id = _insert(fresh_db, PUBLIC, "p1"), _insert(fresh_db, PRIVATE, "p2")

    stats = await _scrape(fresh_db, make_client())

    assert stats["errors"] == 0
    assert public.call_count == 1 and private.call_count == 0
    assert robots.call_count == 1  # one fetch per origin per run
    assert _live(fresh_db, ok_id)["scrape_status"] == "ok"
    row = _live(fresh_db, blocked_id)
    assert row["scrape_status"] == "robots_disallowed" and row["content_text"] is None
    (rev,) = fresh_db.execute(
        "SELECT coverage, content_text FROM scrape_history WHERE bookmark_id = ?", (blocked_id,)
    ).fetchall()
    assert rev["coverage"] == "blocked" and rev["content_text"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 404, 410])
@respx.mock
async def test_robots_4xx_means_no_rules(fresh_db, make_client, status) -> None:
    respx.get(IA_CDX_BASE).mock(return_value=httpx.Response(200, text=CDX_EMPTY))
    respx.get(ROBOTS).mock(return_value=httpx.Response(status, text="Disallow: /"))
    page = respx.get(PRIVATE).mock(return_value=httpx.Response(200, text=PAGE))
    bid = _insert(fresh_db, PRIVATE, "p")

    await _scrape(fresh_db, make_client())

    assert page.call_count == 1
    assert _live(fresh_db, bid)["scrape_status"] == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [httpx.Response(503), httpx.Response(500), httpx.ConnectError("refused"), httpx.ReadTimeout("slow")],
    ids=["503", "500", "connect-error", "timeout"],
)
@respx.mock
async def test_unreachable_robots_skips_the_origin_this_run(fresh_db, make_client, answer) -> None:
    """5xx or a transport error: nothing on the origin is fetched this run,
    and nothing is cached, so the next run asks again."""
    respx.get(IA_CDX_BASE).mock(return_value=httpx.Response(200, text=CDX_EMPTY))
    robots = respx.get(ROBOTS)
    robots.side_effect = [answer, httpx.Response(404)]
    page = respx.get(PUBLIC).mock(return_value=httpx.Response(200, text=PAGE))
    first, second = _insert(fresh_db, PUBLIC, "p1"), _insert(fresh_db, f"{SITE}/other", "p2")

    await _scrape(fresh_db, make_client())

    assert robots.call_count == 1 and page.call_count == 0
    for bid in (first, second):
        row = _live(fresh_db, bid)
        assert row["scrape_status"] == "network_error"
        assert "robots.txt unreachable" in row["error_detail"]
    assert fresh_db.execute("SELECT COUNT(*) FROM scrape_history").fetchone()[0] == 0

    respx.get(f"{SITE}/other").mock(return_value=httpx.Response(200, text=PAGE))
    await _scrape(fresh_db, make_client())
    assert robots.call_count == 2 and page.call_count == 1
    assert _live(fresh_db, first)["scrape_status"] == "ok"


@pytest.mark.asyncio
@respx.mock
async def test_robots_is_cached_per_origin_with_a_ttl(fresh_db, make_client) -> None:
    robots = respx.get(ROBOTS).mock(return_value=httpx.Response(200, text=RULES))

    async def check(client: ScrapeClient) -> None:
        try:
            assert await client.robots_verdict(PUBLIC) is None
            assert await client.robots_verdict(PRIVATE) == "disallowed"
        finally:
            await client.close()

    await check(make_client(robots_ttl=3600))
    assert robots.call_count == 1
    make_client.clock.now += 3599
    await check(make_client(robots_ttl=3600))
    assert robots.call_count == 1  # from the on-disk cache
    make_client.clock.now += 2
    await check(make_client(robots_ttl=3600))
    assert robots.call_count == 2
    await check(make_client(robots_ttl=3600, refresh=True))
    assert robots.call_count == 3


@pytest.mark.asyncio
@respx.mock
async def test_a_cached_page_is_not_served_once_robots_disallows_it(fresh_db, make_client) -> None:
    robots = respx.get(ROBOTS)
    robots.side_effect = [httpx.Response(404), httpx.Response(200, text="User-agent: Muninn\nDisallow: /\n")]
    page = respx.get(PUBLIC).mock(return_value=httpx.Response(200, text=PAGE))
    client = make_client()
    try:
        assert (await client.fetch_live(PUBLIC))["http_status"] == 200
    finally:
        await client.close()
    client = make_client(robots_ttl=0)
    make_client.clock.now += 1
    try:
        result = await client.fetch_live(PUBLIC)
    finally:
        await client.close()
    assert result["error_kind"] == "robots_disallowed" and page.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_redirect_into_a_disallowed_path_is_not_followed(fresh_db, make_client) -> None:
    """Each redirect hop is checked against its own origin's robots.txt."""
    target = "https://docs.example.net/members/page"
    respx.get(IA_CDX_BASE).mock(return_value=httpx.Response(200, text=CDX_EMPTY))
    respx.get(ROBOTS).mock(return_value=httpx.Response(404))
    respx.get("https://docs.example.net/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: muninn\nDisallow: /members/\n"))
    respx.get(PUBLIC).mock(return_value=httpx.Response(301, headers={"location": target}))
    landing = respx.get(target).mock(return_value=httpx.Response(200, text=PAGE))
    bid = _insert(fresh_db, PUBLIC, "p")

    await _scrape(fresh_db, make_client())

    assert landing.call_count == 0
    row = _live(fresh_db, bid)
    assert (row["scrape_status"], row["http_status"], row["final_url"]) == (
        "robots_disallowed", 301, target)


@pytest.mark.asyncio
@respx.mock
async def test_archive_passes_ignore_the_sites_robots(fresh_db, make_client) -> None:
    """Wayback serves its own copy: the target site's robots.txt does not
    gate it (only the live fetch is skipped)."""
    respx.get(IA_CDX_BASE).mock(return_value=httpx.Response(200, text=CDX_HIT))
    archive = respx.get(url__startswith=IA_WEB_BASE).mock(return_value=httpx.Response(200, text=PAGE))
    respx.get(ROBOTS).mock(return_value=httpx.Response(200, text="User-agent: *\nDisallow: /\n"))
    page = respx.get(PUBLIC).mock(return_value=httpx.Response(200, text=PAGE))
    bid = _insert(fresh_db, PUBLIC, "p")

    await _scrape(fresh_db, make_client())

    assert archive.call_count == 1 and page.call_count == 0
    statuses = dict(fresh_db.execute(
        "SELECT pass, scrape_status FROM scrape_results WHERE bookmark_id = ?", (bid,)
    ).fetchall())
    assert statuses == {"at_capture": "ok", "live": "robots_disallowed"}
    assert fresh_db.execute(
        "SELECT enrichment_source FROM bookmarks WHERE bookmark_id = ?", (bid,)
    ).fetchone()[0] == "at_capture"
    # The robots request is the only one made to the site itself.
    assert [str(c.request.url) for c in respx.calls if c.request.url.host == "blog.example.org"] == [ROBOTS]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rules, verdicts",
    [
        # Longest match wins, not file order.
        ("User-agent: *\nAllow: /\nDisallow: /private\n", ("ok", "disallowed")),
        # Wildcards.
        ("User-agent: *\nDisallow: /*/notes$\n", ("ok", "disallowed")),
        # Every group naming the agent is merged.
        ("User-agent: muninn\nDisallow: /posts/\n\nUser-agent: Muninn\nDisallow: /private/\n",
         ("disallowed", "disallowed")),
        # A group for "mun" is not Muninn's.
        ("User-agent: mun\nDisallow: /\n", ("ok", "ok")),
    ],
    ids=["longest-match", "wildcard", "merged-groups", "token-not-substring"],
)
@respx.mock
async def test_the_client_matches_robots_per_rfc_9309(make_client, rules, verdicts) -> None:
    respx.get(ROBOTS).mock(return_value=httpx.Response(200, text=rules))
    client = make_client()
    try:
        got = tuple([
            "disallowed" if await client.robots_verdict(url) == "disallowed" else "ok"
            for url in (PUBLIC, PRIVATE)
        ])
    finally:
        await client.close()
    assert got == verdicts
