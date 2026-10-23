"""robots.txt (RFC 9309) on the live pass (CLAUDE.md known gap 4).

Network fully mocked with respx (unmocked requests fail); caches in tmp_path.
"""

from __future__ import annotations

import time

import httpx
import pytest
import respx

from muninn.intake_records import current_revision_row
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
    # Muninn chose not to fetch: no content state was observed, so there is
    # no revision (contract `blocked` means the site refused).
    assert fresh_db.execute(
        "SELECT COUNT(*) FROM scrape_history WHERE bookmark_id = ?", (blocked_id,)
    ).fetchone()[0] == 0


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
    nothing is persisted for the live pass, and nothing is cached, so the
    next run asks again."""
    respx.get(IA_CDX_BASE).mock(return_value=httpx.Response(200, text=CDX_EMPTY))
    robots = respx.get(ROBOTS)
    robots.side_effect = [answer, httpx.Response(404)]
    page = respx.get(PUBLIC).mock(return_value=httpx.Response(200, text=PAGE))
    first, second = _insert(fresh_db, PUBLIC, "p1"), _insert(fresh_db, f"{SITE}/other", "p2")

    await _scrape(fresh_db, make_client())

    assert robots.call_count == 1 and page.call_count == 0
    for bid in (first, second):
        assert _live(fresh_db, bid) is None  # skipped: no live row written
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


# ── A robots outcome never replaces a stored capture ──────────────

def _state(conn, bid: int):
    live = _live(conn, bid)
    source, item_ref = conn.execute(
        "SELECT b.enrichment_source, h.item_ref FROM bookmarks b "
        "JOIN scrape_history h USING (bookmark_id) WHERE b.bookmark_id = ? LIMIT 1", (bid,)
    ).fetchone()
    revisions = conn.execute(
        "SELECT coverage, retrieval_method FROM scrape_history WHERE bookmark_id = ? "
        "ORDER BY scrape_history_id", (bid,)
    ).fetchall()
    current = current_revision_row(conn, item_ref, bid)
    return live, source, [tuple(r) for r in revisions], current["coverage"]


async def _scrape_with_robots(conn, make_client, answer) -> None:
    with respx.mock:
        respx.get(IA_CDX_BASE).mock(return_value=httpx.Response(200, text=CDX_EMPTY))
        respx.get(ROBOTS).mock(side_effect=[answer])
        respx.get(PRIVATE).mock(return_value=httpx.Response(200, text=PAGE))
        await _scrape(conn, make_client(refresh=True))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [httpx.Response(503), httpx.ConnectError("refused")],
    ids=["503", "connect-error"],
)
async def test_unreachable_robots_keeps_the_stored_capture(fresh_db, make_client, answer) -> None:
    bid = _insert(fresh_db, PRIVATE, "p")
    await _scrape_with_robots(fresh_db, make_client, httpx.Response(404))
    before = _state(fresh_db, bid)
    assert before[0]["scrape_status"] == "ok" and before[0]["content_text"]
    assert before[1] == "live_fallback" and before[3] == "full"

    await _scrape_with_robots(fresh_db, make_client, answer)

    after = _state(fresh_db, bid)
    assert tuple(after[0]) == tuple(before[0])  # the live row is untouched
    assert after[1:] == before[1:]


@pytest.mark.asyncio
async def test_a_robots_disallow_keeps_the_stored_capture_and_adds_no_revision(
    fresh_db, make_client
) -> None:
    bid = _insert(fresh_db, PRIVATE, "p")
    await _scrape_with_robots(fresh_db, make_client, httpx.Response(404))
    before = _state(fresh_db, bid)
    fetched_at = fresh_db.execute(
        "SELECT fetched_at FROM scrape_results WHERE bookmark_id = ? AND pass = 'live'", (bid,)
    ).fetchone()[0]

    # Longest match: /private/ disallows despite the earlier Allow: /.
    rules = "User-agent: *\nAllow: /\nDisallow: /private/\n"
    await _scrape_with_robots(fresh_db, make_client, httpx.Response(200, text=rules))

    after = _state(fresh_db, bid)
    live = after[0]
    assert (live["scrape_status"], live["content_text"]) == ("ok", before[0]["content_text"])
    assert "disallowed by robots.txt" in live["error_detail"]
    assert fresh_db.execute(
        "SELECT fetched_at FROM scrape_results WHERE bookmark_id = ? AND pass = 'live'", (bid,)
    ).fetchone()[0] == fetched_at
    # Same enrichment source, no `blocked` revision, still current and full.
    assert after[1:] == before[1:] == ("live_fallback", [("full", "live")], "full")


@pytest.mark.asyncio
@respx.mock
async def test_http_451_is_still_a_blocked_revision(fresh_db, make_client) -> None:
    """451 is the site refusing, unlike a robots.txt disallow."""
    respx.get(IA_CDX_BASE).mock(return_value=httpx.Response(200, text=CDX_EMPTY))
    respx.get(ROBOTS).mock(return_value=httpx.Response(404))
    respx.get(PRIVATE).mock(return_value=httpx.Response(451))
    bid = _insert(fresh_db, PRIVATE, "p")

    await _scrape(fresh_db, make_client())

    assert _live(fresh_db, bid)["scrape_status"] == "robots_disallowed"
    assert [r[0] for r in fresh_db.execute(
        "SELECT coverage FROM scrape_history WHERE bookmark_id = ?", (bid,)
    )] == ["blocked"]


# ── robots.txt redirects ──────────────────────────────────────────

HIDDEN_HOST = "private.example.com"


@pytest.mark.asyncio
@respx.mock
async def test_a_robots_redirect_to_a_hidden_host_is_not_followed(fresh_db, make_client) -> None:
    """The hidden host is never contacted. Its rules cannot be read, so the
    origin counts as unreachable: nothing on it is fetched this run, and the
    stop is not cached, so the next run asks again."""
    from muninn.scrape.domain_policy import DomainPolicy

    respx.get(IA_CDX_BASE).mock(return_value=httpx.Response(200, text=CDX_EMPTY))
    robots = respx.get(ROBOTS).mock(return_value=httpx.Response(
        301, headers={"location": f"https://{HIDDEN_HOST}/robots.txt"}))
    hidden = respx.route(host=HIDDEN_HOST).mock(return_value=httpx.Response(200, text=""))
    page = respx.get(PUBLIC).mock(return_value=httpx.Response(200, text=PAGE))
    bid = _insert(fresh_db, PUBLIC, "p")

    client = make_client()
    try:
        await scrape_all(fresh_db, client=client, policy=DomainPolicy(domain_patterns=(HIDDEN_HOST,)))
    finally:
        await client.close()

    assert hidden.call_count == 0 and page.call_count == 0 and robots.call_count == 1
    assert _live(fresh_db, bid) is None  # skipped this run
    client = make_client()
    try:
        assert await client.robots_verdict(PUBLIC) is None  # not cached: asked again
    finally:
        await client.close()
    assert robots.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_a_robots_redirect_is_followed_hop_by_hop(fresh_db, make_client) -> None:
    moved = "https://cdn.example.net/site/robots.txt"
    respx.get(ROBOTS).mock(return_value=httpx.Response(302, headers={"location": moved}))
    target = respx.get(moved).mock(return_value=httpx.Response(200, text=RULES))
    client = make_client()
    try:
        assert await client.robots_verdict(PRIVATE) == "disallowed"
        assert await client.robots_verdict(PUBLIC) is None
    finally:
        await client.close()
    assert target.call_count == 1
    assert [r["url"] for r in client.request_log] == [ROBOTS, moved]


@pytest.mark.asyncio
@respx.mock
async def test_more_than_five_robots_redirects_mean_no_rules(fresh_db, make_client) -> None:
    hops = [f"{SITE}/r{i}" for i in range(7)]
    respx.get(ROBOTS).mock(return_value=httpx.Response(302, headers={"location": hops[0]}))
    for here, there in zip(hops, hops[1:]):
        respx.get(here).mock(return_value=httpx.Response(302, headers={"location": there}))
    respx.get(hops[-1]).mock(return_value=httpx.Response(200, text="User-agent: *\nDisallow: /\n"))
    client = make_client()
    try:
        assert await client.robots_verdict(PRIVATE) is None
    finally:
        await client.close()
    assert len(client.request_log) == 6  # robots.txt plus five redirects, no more
