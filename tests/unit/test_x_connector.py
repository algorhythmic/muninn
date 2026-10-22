"""X API v2 connector stub (RI-02b) against synthetic fixtures.

All responses come from ``tests/fixtures/x/`` (synthetic, documentation-
derived, not recorded) through respx; there is no network. Time is a fake
clock whose ``sleep`` advances it, so rate-limit waits cost nothing.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx

from muninn.ingest.x import (
    JsonFileTokenStore,
    MemoryTokenStore,
    OAuthClientConfig,
    XApiError,
    XAuthError,
    XCheckpoint,
    XClient,
    XConnectorConfig,
    XForbidden,
    XTokens,
    XTransportError,
    XUsageCapped,
    build_authorization_url,
    exchange_code,
    new_pkce_pair,
    parse_bookmark_page,
    parse_url_entity,
    pkce_challenge,
    refresh_access_token,
    start_authorization,
)

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "x"
BASE = 1792584000  # 2026-10-21T12:00:00Z; fixture reset headers are relative to it
API = "https://api.x.com/2"
USER = "9000000001"
BOOKMARKS = f"{API}/users/{USER}/bookmarks"
TOKEN_URL = "https://api.x.com/2/oauth2/token"
T2 = "synthetic_next_token_page2"
T3 = "synthetic_next_token_page3"
PAGES = {None: "bookmarks_page1", T2: "bookmarks_page2", T3: "bookmarks_page3"}
ALL_POST_IDS = [f"111111111111111110{i}" for i in range(1, 9)]


def body(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))


def fx(name: str, *, override: dict | None = None) -> httpx.Response:
    meta = json.loads((FIX / f"{name}.headers.json").read_text(encoding="utf-8"))
    payload = body(name)
    if override:
        payload.update(override)
    return httpx.Response(meta["status"], headers=meta["headers"], json=payload)


class FakeClock:
    def __init__(self, now: float = BASE) -> None:
        self.now = now
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def pages_handler(overrides: dict | None = None):
    """Serve the three fixture pages by pagination_token; ``overrides`` maps a
    token to a queue of responses (or exceptions) served before the fixture."""
    overrides = overrides or {}

    def handler(request: httpx.Request) -> httpx.Response:
        token = request.url.params.get("pagination_token")
        queue = overrides.get(token)
        if queue:
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return fx(PAGES[token])

    return handler


def tokens(**kw) -> XTokens:
    base = dict(
        access_token="synthetic-access-token-0001",
        refresh_token="synthetic-refresh-token-0001",
        expires_at=BASE + 7200,
        scope="tweet.read users.read bookmark.read offline.access",
    )
    base.update(kw)
    return XTokens(**base)


OAUTH = OAuthClientConfig(client_id="synthetic-client-id", redirect_uri="http://127.0.0.1:8765/callback")


def make_client(clock: FakeClock, *, store=None, config=None, oauth=OAUTH) -> XClient:
    return XClient(
        oauth,
        store if store is not None else MemoryTokenStore(tokens()),
        config=config,
        http=httpx.Client(),
        clock=clock,
        sleep=clock.sleep,
    )


@pytest.fixture
def mock():
    with respx.mock(assert_all_called=False) as m:
        yield m


def page_tokens(route) -> list[str | None]:
    return [c.request.url.params.get("pagination_token") for c in route.calls]


# ── Pagination ──────────────────────────────────────────────────────────

def test_full_run_follows_tokens_and_short_page_continues(mock) -> None:
    clock = FakeClock()
    route = mock.get(BOOKMARKS).mock(side_effect=pages_handler())
    sync = make_client(clock).iter_bookmark_pages(XCheckpoint(user_id=USER))
    pages = list(sync)

    assert [p.request_token for p in pages] == [None, T2, T3]
    # Page 2 is short (2 < max_results) yet carries a token, so page 3 is fetched.
    assert pages[1].result_count == 2 < XConnectorConfig().max_results
    assert pages[1].next_token == T3 and not pages[1].is_last
    assert pages[2].is_last
    assert sync.outcome == "complete"
    assert sync.checkpoint.status == "complete" and sync.checkpoint.next_token is None
    assert sync.checkpoint.pages_fetched == 3 and sync.checkpoint.posts_seen == 8
    assert [post.post_id for p in pages for post in p.posts] == ALL_POST_IDS
    assert [post.list_position for p in pages for post in p.posts] == list(range(8))
    assert route.call_count == 3
    assert clock.sleeps == []


def test_empty_page_with_token_is_not_the_end(mock) -> None:
    empty = httpx.Response(200, json={"meta": {"result_count": 0, "next_token": T2}})
    mock.get(BOOKMARKS).mock(side_effect=pages_handler({None: [empty]}))
    sync = make_client(FakeClock()).iter_bookmark_pages(XCheckpoint(user_id=USER))
    pages = list(sync)
    assert [p.result_count for p in pages] == [0, 2, 2]
    assert sync.outcome == "complete"


def test_complete_checkpoint_fetches_nothing(mock) -> None:
    route = mock.get(BOOKMARKS).mock(side_effect=pages_handler())
    sync = make_client(FakeClock()).iter_bookmark_pages(XCheckpoint(user_id=USER, status="complete"))
    assert list(sync) == [] and sync.outcome == "complete"
    assert route.call_count == 0


def test_token_that_does_not_advance_is_an_error(mock) -> None:
    loop = fx("bookmarks_page2", override={"meta": {"result_count": 2, "next_token": T2}})
    mock.get(BOOKMARKS).mock(side_effect=pages_handler({T2: [loop]}))
    with pytest.raises(XApiError, match="did not advance"):
        list(make_client(FakeClock()).iter_bookmark_pages(XCheckpoint(user_id=USER)))


def test_max_pages_budget_stops_resumably(mock) -> None:
    mock.get(BOOKMARKS).mock(side_effect=pages_handler())
    client = make_client(FakeClock())
    sync = client.iter_bookmark_pages(XCheckpoint(user_id=USER), max_pages=1)
    assert len(list(sync)) == 1 and sync.outcome == "page_budget"
    assert sync.checkpoint.status == "in_progress" and sync.checkpoint.next_token == T2
    rest = client.iter_bookmark_pages(sync.checkpoint)
    assert [p.request_token for p in rest] == [T2, T3]


@pytest.mark.parametrize(
    "failure, exc_type",
    [
        (httpx.Response(503, json={"title": "Service Unavailable"}), XApiError),
        (httpx.ConnectError("synthetic connect failure"), XTransportError),
    ],
)
def test_resume_from_persisted_checkpoint_after_mid_run_failure(mock, tmp_path, failure, exc_type) -> None:
    route = mock.get(BOOKMARKS).mock(side_effect=pages_handler({T3: [failure]}))
    saved = tmp_path / "checkpoint.json"
    handled: list[str] = []

    sync = make_client(FakeClock()).iter_bookmark_pages(XCheckpoint(user_id=USER))
    with pytest.raises(exc_type):
        for page in sync:
            handled.extend(p.post_id for p in page.posts)
            saved.write_text(page.checkpoint.to_json())
    assert sync.checkpoint.next_token == T3  # still points at the failed page

    resumed_cp = XCheckpoint.from_json(saved.read_text())
    assert resumed_cp.next_token == T3 and resumed_cp.pages_fetched == 2
    resumed = make_client(FakeClock(BASE + 60)).iter_bookmark_pages(resumed_cp)
    for page in resumed:
        handled.extend(p.post_id for p in page.posts)

    assert handled == ALL_POST_IDS  # every post once, in order, no duplicates
    assert resumed.outcome == "complete"
    assert resumed.checkpoint.posts_seen == 8 and resumed.checkpoint.pages_fetched == 3
    assert page_tokens(route) == [None, T2, T3, T3]


# ── Usage cap and rate limits ───────────────────────────────────────────

def test_usage_capped_is_an_exhaustion_status_not_a_retry_loop(mock) -> None:
    clock = FakeClock()
    capped = [fx("error_429_usage_capped")]
    route = mock.get(BOOKMARKS).mock(side_effect=pages_handler({T2: capped}))
    sync = make_client(clock).iter_bookmark_pages(XCheckpoint(user_id=USER))
    pages = list(sync)

    assert len(pages) == 1
    assert sync.outcome == "usage_capped"
    assert isinstance(sync.error, XUsageCapped) and sync.error.period == "Monthly"
    assert sync.checkpoint.status == "usage_capped" and sync.checkpoint.next_token == T2
    assert route.call_count == 2  # page 1, then a single capped request
    assert clock.sleeps == []

    # After a top-up, the persisted checkpoint resumes at page 2.
    resumed = make_client(clock).iter_bookmark_pages(XCheckpoint.from_json(sync.checkpoint.to_json()))
    assert [p.request_token for p in resumed] == [T2, T3]
    assert resumed.outcome == "complete"


def test_fetch_page_raises_usage_capped_once(mock) -> None:
    route = mock.get(BOOKMARKS).mock(return_value=fx("error_429_usage_capped"))
    with pytest.raises(XUsageCapped) as info:
        make_client(FakeClock()).fetch_bookmark_page(XCheckpoint(user_id=USER))
    assert info.value.status == 429 and info.value.problem == "usage-capped"
    assert route.call_count == 1


def test_rate_limit_waits_until_reset_header(mock) -> None:
    clock = FakeClock()
    route = mock.get(BOOKMARKS).mock(
        side_effect=pages_handler({T2: [fx("error_429_rate_limit")]})
    )
    sync = make_client(clock).iter_bookmark_pages(XCheckpoint(user_id=USER))
    assert len(list(sync)) == 3 and sync.outcome == "complete"
    # x-rate-limit-reset is BASE + 300; one second of margin.
    assert clock.sleeps == [301.0]
    assert page_tokens(route) == [None, T2, T2, T3]


def test_rate_limit_beyond_wait_budget_stops_with_resume_after(mock) -> None:
    clock = FakeClock()
    mock.get(BOOKMARKS).mock(side_effect=pages_handler({T2: [fx("error_429_rate_limit")]}))
    config = XConnectorConfig(max_rate_limit_wait_seconds=60)
    sync = make_client(clock, config=config).iter_bookmark_pages(XCheckpoint(user_id=USER))
    assert len(list(sync)) == 1
    assert sync.outcome == "rate_limited" and clock.sleeps == []
    assert sync.checkpoint.status == "rate_limited"
    assert sync.checkpoint.resume_after == BASE + 300
    assert sync.checkpoint.next_token == T2


def test_rate_limit_wait_count_is_bounded(mock) -> None:
    clock = FakeClock()
    route = mock.get(BOOKMARKS).mock(return_value=fx("error_429_rate_limit"))
    sync = make_client(clock).iter_bookmark_pages(XCheckpoint(user_id=USER))
    assert list(sync) == [] and sync.outcome == "rate_limited"
    # First wait to the reset, then the reset is in the past: margin only.
    assert clock.sleeps == [301.0, 1.0, 1.0]
    assert route.call_count == XConnectorConfig().max_rate_limit_waits + 1


# ── Auth ────────────────────────────────────────────────────────────────

def test_401_refreshes_once_and_stores_rotated_token(mock) -> None:
    store = MemoryTokenStore(tokens())
    route = mock.get(BOOKMARKS).mock(
        side_effect=pages_handler({None: [fx("error_401_unauthorized")]})
    )
    token_route = mock.post(TOKEN_URL).mock(return_value=fx("token_refresh_rotated"))
    clock = FakeClock()
    pages = list(make_client(clock, store=store).iter_bookmark_pages(XCheckpoint(user_id=USER)))

    assert len(pages) == 3
    assert token_route.call_count == 1
    sent = parse_qs(token_route.calls[0].request.content.decode())
    assert sent["grant_type"] == ["refresh_token"]
    assert sent["refresh_token"] == ["synthetic-refresh-token-0001"]
    assert sent["client_id"] == ["synthetic-client-id"]  # public client
    assert store.saves == 1
    assert store.tokens.access_token == "synthetic-access-token-0002"
    assert store.tokens.refresh_token == "synthetic-refresh-token-0002"
    assert store.tokens.expires_at == BASE + 7200
    auth = [c.request.headers["Authorization"] for c in route.calls]
    assert auth == ["Bearer synthetic-access-token-0001"] + ["Bearer synthetic-access-token-0002"] * 3


def test_401_after_refresh_is_auth_error(mock) -> None:
    mock.get(BOOKMARKS).mock(return_value=fx("error_401_unauthorized"))
    token_route = mock.post(TOKEN_URL).mock(return_value=fx("token_refresh_rotated"))
    with pytest.raises(XAuthError) as info:
        list(make_client(FakeClock()).iter_bookmark_pages(XCheckpoint(user_id=USER)))
    assert info.value.status == 401
    assert token_route.call_count == 1


def test_refresh_failure_is_auth_error(mock) -> None:
    mock.get(BOOKMARKS).mock(return_value=fx("error_401_unauthorized"))
    mock.post(TOKEN_URL).mock(return_value=fx("token_error_invalid_grant"))
    with pytest.raises(XAuthError, match="invalid_request"):
        make_client(FakeClock()).fetch_bookmark_page(XCheckpoint(user_id=USER))


def test_expiring_token_is_refreshed_before_the_request(mock) -> None:
    store = MemoryTokenStore(tokens(expires_at=BASE + 30))
    route = mock.get(BOOKMARKS).mock(side_effect=pages_handler())
    mock.post(TOKEN_URL).mock(return_value=fx("token_refresh_rotated"))
    make_client(FakeClock(), store=store).fetch_bookmark_page(XCheckpoint(user_id=USER))
    assert route.call_count == 1
    assert route.calls[0].request.headers["Authorization"] == "Bearer synthetic-access-token-0002"


def test_no_stored_tokens_is_auth_error(mock) -> None:
    route = mock.get(BOOKMARKS).mock(side_effect=pages_handler())
    with pytest.raises(XAuthError, match="no stored X tokens"):
        make_client(FakeClock(), store=MemoryTokenStore()).fetch_bookmark_page(XCheckpoint(user_id=USER))
    assert route.call_count == 0


def test_403_client_forbidden(mock) -> None:
    route = mock.get(BOOKMARKS).mock(return_value=fx("error_403_client_forbidden"))
    with pytest.raises(XForbidden) as info:
        list(make_client(FakeClock()).iter_bookmark_pages(XCheckpoint(user_id=USER)))
    assert info.value.status == 403
    assert info.value.problem == "client-forbidden"
    assert info.value.reason == "client-not-enrolled"
    assert route.call_count == 1


# ── Parsing ─────────────────────────────────────────────────────────────

def parsed(name: str, *, config: XConnectorConfig | None = None, posts_seen: int = 0):
    cp = XCheckpoint(user_id=USER, posts_seen=posts_seen)
    page = parse_bookmark_page(body(name), checkpoint=cp, observed_at=BASE, config=config)
    return page, {p.post_id: p for p in page.posts}


def test_partial_errors_are_item_problems_not_exceptions() -> None:
    page, posts = parsed("bookmarks_page2")
    assert len(posts) == 2
    assert [(p.kind, p.resource_type, p.resource_id) for p in page.problems] == [
        ("resource-not-found", "tweet", "1111111111111111192"),
        ("not-authorized-for-resource", "tweet", "1111111111111111193"),
        ("resource-unavailable", "user", "9000000099"),
    ]
    assert page.checkpoint.problems_seen == 3


def test_plain_post_with_fully_unwound_link() -> None:
    _, posts = parsed("bookmarks_page1")
    post = posts["1111111111111111101"]
    expected = int(datetime(2026, 9, 1, 8, 15, tzinfo=timezone.utc).timestamp())
    assert post.published_at == expected and post.observed_at == BASE
    assert post.author.username == "example_author"
    assert not post.has_note_tweet and post.article is None
    (link,) = post.links
    assert link.locator_source == "unwound_url"
    # sanitize_url dropped the utm_* tracking params.
    assert link.locator == "https://www.example.com/articles/synthetic-guide?id=42"
    assert link.kind == "web" and link.status == 200


def test_long_post_uses_note_tweet_text_and_entities() -> None:
    raw = body("bookmarks_page1")["data"][1]
    _, posts = parsed("bookmarks_page1")
    post = posts["1111111111111111102"]
    assert post.has_note_tweet and post.text == raw["note_tweet"]["text"]
    assert len(post.text) > len(raw["text"])
    assert [(link.kind, link.github_repo) for link in post.links] == [
        ("github_repo", "example-org/example-repo"),
        ("web", None),
    ]
    assert post.links[1].locator == "https://blog.example.org/posts/write-up"


def test_quote_post_resolves_quoted_post_from_includes() -> None:
    _, posts = parsed("bookmarks_page1")
    post = posts["1111111111111111103"]
    assert post.quoted_post_ids == ("1111111111111111191",)
    assert post.links[0].kind == "x_post" and post.links[0].x_id == "1111111111111111191"
    (quoted,) = post.quoted_posts
    assert quoted.author.username == "quoted_author"
    assert quoted.observed_at is None and quoted.list_position is None
    (link,) = quoted.links  # t.co only
    assert link.locator_source == "url" and link.locator == "https://t.co/EXAMPLE0191"


def test_media_post() -> None:
    _, posts = parsed("bookmarks_page1")
    post = posts["1111111111111111104"]
    assert post.media_keys == ("3_1111111111111111104",)
    assert post.media[0].type == "photo"
    assert post.media[0].url == "https://media.example.com/EXAMPLE0104.jpg"
    assert post.links[0].kind == "x_media" and post.links[0].media_key == "3_1111111111111111104"


def test_article_with_and_without_body_and_link_statuses() -> None:
    _, posts = parsed("bookmarks_page2")
    with_body = posts["1111111111111111105"]
    assert with_body.article.title == "A synthetic long-form article"
    assert with_body.article.body.startswith("The first lines")
    assert with_body.links[0].kind == "x_article"
    assert with_body.links[0].x_id == "1111111111111111905"
    assert with_body.referenced == (("replied_to", "1111111111111111193"),)

    no_body = posts["1111111111111111106"]
    assert no_body.article.body is None and no_body.article.preview_text
    assert no_body.quoted_posts == ()  # quoted post was not found (errors[])
    null_unwound, dead = no_body.links
    assert null_unwound.locator_source == "expanded_url"
    assert null_unwound.locator == "https://example.org/papers/synthetic-paper.pdf"
    assert null_unwound.status is None
    assert dead.status == 404 and dead.locator_source == "unwound_url"
    assert dead.locator == "https://www.example.org/a-page-that-is-gone"


def test_credential_param_in_destination_is_redacted() -> None:
    _, posts = parsed("bookmarks_page3")
    (link,) = posts["1111111111111111108"].links
    assert link.redacted_param_names == ("access_token",)
    assert "synthetic-not-a-real-token" not in link.locator
    assert posts["1111111111111111107"].links == ()


@pytest.mark.parametrize(
    "entity, source, locator",
    [
        (
            {"url": "https://t.co/A", "expanded_url": "https://e.example.net/x",
             "unwound_url": "https://WWW.Example.com/final?utm_source=x&b=2&a=1"},
            "unwound_url",
            "https://www.example.com/final?a=1&b=2",
        ),
        (
            {"url": "https://t.co/A", "expanded_url": "https://example.org/p?ref=x", "unwound_url": None},
            "expanded_url",
            "https://example.org/p",
        ),
        ({"url": "https://t.co/A"}, "url", "https://t.co/A"),
        (
            {"url": "https://t.co/A", "expanded_url": "https://example.org/ok", "unwound_url": "javascript:alert(1)"},
            "expanded_url",
            "https://example.org/ok",
        ),
    ],
)
def test_locator_order_then_sanitize(entity, source, locator) -> None:
    link = parse_url_entity(entity, 0)
    assert link.locator_source == source and link.locator == locator


def test_x_share_params_never_reach_a_locator() -> None:
    link = parse_url_entity(
        {"url": "https://t.co/A",
         "expanded_url": "https://twitter.com/someone/status/1234567890123456789?s=20&t=SyntheticShareTok_0001"},
        0,
    )
    assert link.locator == "https://twitter.com/someone/status/1234567890123456789"
    assert link.kind == "x_post" and link.redacted_param_names == ()
    profile = parse_url_entity({"url": "https://t.co/B", "expanded_url": "https://x.com/someone?s=21&t=Tok"}, 1)
    assert profile.locator == "https://x.com/someone"


def test_entity_without_any_locator() -> None:
    link = parse_url_entity({"url": None, "expanded_url": ""}, 3)
    assert link.locator is None and link.parse_error == "no_locator" and link.position == 3


# ── Configuration, not constants ────────────────────────────────────────

@pytest.mark.parametrize("cap, flagged_after", [(800, []), (5, [1, 2]), (4, [0, 1, 2]), (None, [])])
def test_bookmark_cap_is_configuration_and_never_stops_the_run(mock, cap, flagged_after) -> None:
    assert XConnectorConfig().bookmark_cap == 800
    mock.get(BOOKMARKS).mock(side_effect=pages_handler())
    sync = make_client(FakeClock(), config=XConnectorConfig(bookmark_cap=cap)).iter_bookmark_pages(
        XCheckpoint(user_id=USER)
    )
    pages = list(sync)
    assert len(pages) == 3 and sync.outcome == "complete"
    assert [i for i, p in enumerate(pages) if p.checkpoint.cap_reached] == flagged_after


@pytest.mark.parametrize(
    "param, includes_key", [("tweet.fields", "tweets"), ("post.fields", "posts")]
)
def test_field_name_spelling_is_configuration(mock, param, includes_key) -> None:
    other = "post.fields" if param == "tweet.fields" else "tweet.fields"
    page1 = body("bookmarks_page1")
    page1["includes"] = {**page1["includes"]}
    page1["includes"][includes_key] = page1["includes"].pop("tweets")
    route = mock.get(BOOKMARKS).mock(return_value=httpx.Response(200, json={**page1, "meta": {"result_count": 4}}))
    config = XConnectorConfig(post_fields_param=param, includes_posts_key=includes_key)

    page = make_client(FakeClock(), config=config).fetch_bookmark_page(XCheckpoint(user_id=USER))
    params = route.calls[0].request.url.params
    assert "note_tweet" in params[param] and other not in params
    quote = next(p for p in page.posts if p.post_id == "1111111111111111103")
    assert quote.quoted_posts[0].post_id == "1111111111111111191"


def test_default_request_parameters(mock) -> None:
    route = mock.get(BOOKMARKS).mock(side_effect=pages_handler())
    make_client(FakeClock()).fetch_bookmark_page(XCheckpoint(user_id=USER))
    params = route.calls[0].request.url.params
    assert params["max_results"] == "100"
    assert "pagination_token" not in params
    assert set(params["expansions"].split(",")) >= {
        "author_id", "attachments.media_keys", "referenced_tweets.id"
    }


# ── Other endpoints ─────────────────────────────────────────────────────

def test_get_me_and_folders(mock) -> None:
    mock.get(f"{API}/users/me").mock(return_value=fx("users_me"))
    mock.get(f"{BOOKMARKS}/folders").mock(return_value=fx("folders_list"))
    mock.get(f"{BOOKMARKS}/folders/1500000000000000001").mock(return_value=fx("folder_posts"))
    client = make_client(FakeClock())

    me = client.get_me()
    assert me.id == USER and me.username == "fixture_owner"
    folders = client.list_bookmark_folders(me.id)
    assert [(f.id, f.name) for f in folders] == [
        ("1500000000000000001", "Synthetic reading list"),
        ("1500000000000000002", "Synthetic tools"),
    ]
    assert client.folder_post_ids(me.id, folders[0].id) == [
        "1111111111111111101", "1111111111111111105"
    ]


# ── OAuth 2.0 PKCE helpers ──────────────────────────────────────────────

def test_pkce_s256_matches_rfc7636_vector() -> None:
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert pkce_challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    pair = new_pkce_pair()
    assert 43 <= len(pair.verifier) <= 128
    assert pair.challenge == pkce_challenge(pair.verifier) and pair.method == "S256"


def test_authorization_url() -> None:
    url = build_authorization_url(OAUTH, state="synthetic-state", code_challenge="synthetic-challenge")
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "https://x.com/i/oauth2/authorize"
    q = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert q == {
        "response_type": "code",
        "client_id": "synthetic-client-id",
        "redirect_uri": "http://127.0.0.1:8765/callback",
        "scope": "tweet.read users.read bookmark.read offline.access",
        "state": "synthetic-state",
        "code_challenge": "synthetic-challenge",
        "code_challenge_method": "S256",
    }
    a, b = start_authorization(OAUTH), start_authorization(OAUTH)
    assert a.state != b.state and a.pkce.challenge in a.url


def test_exchange_code_confidential_client_uses_basic_auth(mock) -> None:
    route = mock.post(TOKEN_URL).mock(return_value=fx("token_exchange"))
    oauth = OAuthClientConfig(
        client_id="synthetic-client-id", client_secret="synthetic-client-secret",
        redirect_uri="http://127.0.0.1:8765/callback",
    )
    got = exchange_code(oauth, code="synthetic-code", code_verifier="v" * 43, http=httpx.Client(), clock=FakeClock())
    assert got.access_token == "synthetic-access-token-0001"
    assert got.refresh_token == "synthetic-refresh-token-0001"
    assert got.expires_at == BASE + 7200
    req = route.calls[0].request
    sent = parse_qs(req.content.decode())
    assert sent["grant_type"] == ["authorization_code"] and sent["code_verifier"] == ["v" * 43]
    assert "client_id" not in sent
    assert req.headers["Authorization"] == "Basic " + base64.b64encode(
        b"synthetic-client-id:synthetic-client-secret"
    ).decode()


def test_refresh_without_rotation_keeps_the_old_refresh_token(mock) -> None:
    no_rotation = {k: v for k, v in body("token_refresh_rotated").items() if k != "refresh_token"}
    mock.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=no_rotation))
    got = refresh_access_token(OAUTH, "synthetic-refresh-token-0001", http=httpx.Client(), clock=FakeClock())
    assert got.access_token == "synthetic-access-token-0002"
    assert got.refresh_token == "synthetic-refresh-token-0001"


def test_json_file_token_store_round_trip(tmp_path) -> None:
    store = JsonFileTokenStore(tmp_path / "x" / "tokens.json")
    assert store.load() is None
    store.save(tokens())
    assert store.load() == tokens()
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    assert list(store.path.parent.iterdir()) == [store.path]


def test_checkpoint_json_round_trip_and_version_check() -> None:
    cp = XCheckpoint(user_id=USER, status="in_progress", next_token=T2, pages_fetched=1, posts_seen=4)
    assert XCheckpoint.from_json(cp.to_json()) == cp
    with pytest.raises(ValueError, match="version"):
        XCheckpoint.from_json(json.dumps({**json.loads(cp.to_json()), "version": 99}))
    with pytest.raises(ValueError, match="unknown"):
        XCheckpoint.from_json(json.dumps({**json.loads(cp.to_json()), "access_token": "x"}))


# ── Secret canary ───────────────────────────────────────────────────────

CANARIES = {
    "access": "CANARY-access-7f3c9d",
    "refresh": "CANARY-refresh-51b2e8",
    "access2": "CANARY-rotated-access-a0c4f1",
    "refresh2": "CANARY-rotated-refresh-e93d07",
    "secret": "CANARY-client-secret-2b7e66",
    "code": "CANARY-auth-code-8d1f3a",
    "verifier": "CANARY-pkce-verifier-0000000000000000000000000000c9",
}


def test_secrets_never_reach_logs_errors_reprs_or_checkpoints(mock, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    c = CANARIES
    oauth = OAuthClientConfig(
        client_id="synthetic-client-id", client_secret=c["secret"],
        redirect_uri="http://127.0.0.1:8765/callback",
    )
    surfaces: list[str] = [repr(oauth), str(oauth)]
    errors: list[BaseException] = []

    # Code exchange → the first canary token pair.
    token_route = mock.post(TOKEN_URL).mock(
        side_effect=[
            httpx.Response(200, json={"token_type": "bearer", "expires_in": 7200,
                                      "access_token": c["access"], "refresh_token": c["refresh"]}),
            httpx.Response(200, json={"token_type": "bearer", "expires_in": 7200,
                                      "access_token": c["access2"], "refresh_token": c["refresh2"]}),
            # A hostile token endpoint that echoes the refresh token back.
            httpx.Response(400, json={"error": "invalid_grant",
                                      "error_description": f"bad token {c['refresh2']} / {c['secret']}"}),
        ]
    )
    clock = FakeClock()
    first = exchange_code(oauth, code=c["code"], code_verifier=c["verifier"], http=httpx.Client(), clock=clock)
    store = MemoryTokenStore(first)
    surfaces += [repr(first), str(first), repr(store), repr(new_pkce_pair()), repr(start_authorization(oauth))]

    # 401 → rotated refresh → full run.
    forbidden_echo = fx("error_403_client_forbidden", override={"detail": f"token {c['access2']} rejected"})
    route = mock.get(BOOKMARKS).mock(
        side_effect=pages_handler({None: [fx("error_401_unauthorized")], T3: [forbidden_echo]})
    )
    client = make_client(clock, store=store, oauth=oauth)
    surfaces.append(repr(client))
    sync = client.iter_bookmark_pages(XCheckpoint(user_id=USER))
    with pytest.raises(XForbidden) as forbidden:  # the 403 detail echoes the token
        for page in sync:
            surfaces += [page.checkpoint.to_json(), repr(page.checkpoint)]
    errors.append(forbidden.value)
    surfaces += [sync.checkpoint.to_json(), repr(store.tokens), repr(store)]
    assert route.calls[0].request.headers["Authorization"] == f"Bearer {c['access']}"
    assert route.calls[1].request.headers["Authorization"] == f"Bearer {c['access2']}"

    # Usage cap and transport failure.
    mock.get(BOOKMARKS).mock(return_value=fx("error_429_usage_capped"))
    capped = client.iter_bookmark_pages(sync.checkpoint)
    list(capped)
    errors.append(capped.error)
    surfaces.append(capped.checkpoint.to_json())
    mock.get(BOOKMARKS).mock(side_effect=httpx.ConnectError(f"boom {c['access2']}"))
    with pytest.raises(XTransportError) as transport:
        client.fetch_bookmark_page(sync.checkpoint)
    errors.append(transport.value)

    # 401 again → refresh fails at a hostile token endpoint.
    mock.get(BOOKMARKS).mock(return_value=fx("error_401_unauthorized"))
    with pytest.raises(XAuthError) as auth:
        client.fetch_bookmark_page(sync.checkpoint)
    errors.append(auth.value)
    assert token_route.call_count == 3

    for exc in errors:
        surfaces += [str(exc), repr(exc), *map(str, exc.args)]
        assert exc.__cause__ is None
    surfaces.append(caplog.text)
    blob = "\n".join(surfaces)
    leaked = [name for name, value in c.items() if value in blob]
    assert leaked == []


# ── Import isolation ────────────────────────────────────────────────────

def test_ingest_package_does_not_load_the_x_connector() -> None:
    src = str(Path(__file__).resolve().parents[2] / "src")
    code = "import json, sys, muninn.ingest; print(json.dumps('muninn.ingest.x' in sys.modules))"
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True,
        env={**os.environ, "PYTHONPATH": src},
    )
    assert json.loads(out.stdout) is False
