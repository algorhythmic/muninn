"""`muninn ingest x` writer: X bookmark pages → stage-native rows (RI-04).

Synthetic fixtures from ``tests/fixtures/x/`` through respx; no network, no
credentials. Rows are checked against the contract mapping (§10.1) and,
mapped to records, validated against ``schemas/json/intake/``.
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

from muninn.consumers.packets import content_digest, link_item_id, locator_source_id
from muninn.ingest.x import MemoryTokenStore, OAuthClientConfig, XClient, XConnectorConfig, XTokens
from muninn.ingest.x_store import (
    NothingToResume,
    SyncOptions,
    XPageWriter,
    sync_bookmarks,
)
from muninn.intake_records import (
    bookmark_observation_record,
    content_revision_record,
    item_link_record,
    source_item_record,
    usage_record,
)
from muninn.scrape.domain_policy import DomainPolicy

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "tests" / "fixtures" / "x"
SCHEMA_DIR = ROOT / "schemas" / "json" / "intake"
BASE = 1792584000  # 2026-10-21T12:00:00Z
API = "https://api.x.com/2"
USER = "9000000001"
BOOKMARKS = f"{API}/users/{USER}/bookmarks"
T2 = "synthetic_next_token_page2"
T3 = "synthetic_next_token_page3"
PAGES = {None: "bookmarks_page1", T2: "bookmarks_page2", T3: "bookmarks_page3"}
BOOKMARKED = [f"111111111111111110{i}" for i in range(1, 9)]
CANARY = "synthetic-not-a-real-token"
ACCESS = "synthetic-access-token-0001"


def fx(name: str) -> httpx.Response:
    meta = json.loads((FIX / f"{name}.headers.json").read_text(encoding="utf-8"))
    payload = json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))
    return httpx.Response(meta["status"], headers=meta["headers"], json=payload)


class FakeClock:
    def __init__(self, now: float = BASE) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def serve(overrides: dict | None = None):
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


@pytest.fixture
def x_api():
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{API}/users/me").mock(side_effect=lambda req: fx("users_me"))
        yield mock


def client(clock: FakeClock, config: XConnectorConfig | None = None) -> XClient:
    tokens = XTokens(access_token=ACCESS, refresh_token="synthetic-refresh-token-0001",
                     expires_at=BASE + 10**6)
    return XClient(
        OAuthClientConfig(client_id="synthetic-client-id", redirect_uri="http://127.0.0.1/cb"),
        MemoryTokenStore(tokens), config=config, http=httpx.Client(), clock=clock,
        sleep=clock.sleep,
    )


def run(conn, clock, *, policy=None, config=None, **options):
    return sync_bookmarks(
        conn, client(clock, config), options=SyncOptions(**options),
        policy=policy or DomainPolicy.empty(), clock=clock,
    )


def count(conn, table: str, where: str = "1") -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]


def bookmark(conn, source: str, source_id: str) -> sqlite3.Row:
    return conn.execute(
        "SELECT * FROM bookmarks WHERE source = ? AND source_id = ?", (source, source_id)
    ).fetchone()


def meta(row) -> dict:
    return json.loads(row["source_metadata"])


def snapshot(conn) -> dict[str, int]:
    return {t: count(conn, t) for t in (
        "bookmarks", "item_links", "scrape_results", "scrape_history",
        "sync_checkpoints", "usage_records",
    )}


EXPECTED = {
    "bookmarks": 19,  # 11 x (8 bookmarked + 3 referenced), 1 x_article, 7 x_link
    "item_links": 13,
    "scrape_results": 12,
    "scrape_history": 12,
    "sync_checkpoints": 1,
    "usage_records": 4,  # GET /2/users/me + three pages
}


# ── End to end ──────────────────────────────────────────────────────────

def test_three_page_sync_writes_the_contract_rows(fresh_db, x_api) -> None:
    route = x_api.get(BOOKMARKS).mock(side_effect=serve())
    report = run(fresh_db, FakeClock())

    assert len(route.calls) == 3
    assert report.status == "completed" and report.stop_reason == "end_of_list"
    assert (report.pages, report.posts_seen, report.posts_new, report.problems) == (3, 8, 8, 3)
    assert snapshot(fresh_db) == EXPECTED
    assert count(fresh_db, "bookmarks", "source = 'x'") == 11
    assert count(fresh_db, "bookmarks", "source = 'x_article'") == 1
    assert count(fresh_db, "bookmarks", "source = 'x_link'") == 7

    # A bookmarked post: id, locator, first observation, metadata without text.
    post = bookmark(fresh_db, "x", BOOKMARKED[0])
    m = meta(post)
    assert post["url"] == f"https://x.com/example_author/status/{BOOKMARKED[0]}"
    assert post["captured_at"] == BASE and post["enrichment_source"] == "live_fallback"
    assert post["domain"] == "x.com" and post["content_visible"] == 1
    assert m["bookmarked"] is True and m["list_position"] == 0
    assert m["author"] == {"id": "9000000011", "username": "example_author", "name": "Example Author"}
    assert m["published_at"] == 1788250500 and m["lang"] == "en"
    assert m["media_keys"] == [] and m["is_long_post"] is False
    assert post["title"].startswith("@example_author: A plain synthetic post with a fully unwound link")
    assert post["title"].endswith("…") and len(post["title"]) == len("@example_author: ") + 80
    assert m["first_sync_run_id"] == report.run_ulid == m["last_sync_run_id"]
    assert m["last_observed_at"] == BASE
    assert "text" not in m and "plain synthetic post" not in post["source_metadata"]

    # Its text: live scrape_results row + one x_api revision, t.co replaced.
    live = fresh_db.execute(
        "SELECT * FROM scrape_results WHERE bookmark_id = ?", (post["bookmark_id"],)
    ).fetchall()
    assert [r["pass"] for r in live] == ["live"] and live[0]["scrape_status"] == "ok"
    text = live[0]["content_text"]
    assert "https://www.example.com/articles/synthetic-guide?id=42" in text and "t.co" not in text
    (rev,) = fresh_db.execute(
        "SELECT * FROM scrape_history WHERE bookmark_id = ?", (post["bookmark_id"],)
    ).fetchall()
    assert (rev["retrieval_method"], rev["coverage"], rev["scrape_pass"]) == ("x_api", "full", None)
    assert rev["item_ref"] == f"x:post:{BOOKMARKED[0]}" and rev["content_text"] == text
    assert rev["content_sha256"] == content_digest(text) and rev["char_count"] == len(text)
    assert (rev["published_at"], rev["retrieved_at"], rev["observed_at"]) == (1788250500, BASE, BASE)

    # Long post: note_tweet full text, both t.co links replaced.
    long_text = fresh_db.execute(
        "SELECT content_text FROM scrape_results sr JOIN bookmarks b USING (bookmark_id) "
        "WHERE b.source = 'x' AND b.source_id = ?", (BOOKMARKED[1],)
    ).fetchone()[0]
    assert "The full text only arrives in note_tweet." in long_text
    assert "https://github.com/example-org/example-repo" in long_text
    assert "https://blog.example.org/posts/write-up" in long_text and "t.co" not in long_text
    assert meta(bookmark(fresh_db, "x", BOOKMARKED[1]))["is_long_post"] is True

    # GitHub repository root → x_link with kind github_repo.
    repo = bookmark(fresh_db, "x_link", locator_source_id("https://github.com/example-org/example-repo"))
    assert meta(repo)["kind"] == "github_repo"
    assert meta(repo)["item_id"] == "github:repo:example-org/example-repo"
    assert meta(repo)["repository"] == {"owner": "example-org", "name": "example-repo"}
    assert repo["enrichment_source"] is None  # not scraped yet

    # Web page: source_id is the web: digest; tracking params gone.
    web_url = "https://www.example.com/articles/synthetic-guide?id=42"
    web = bookmark(fresh_db, "x_link", locator_source_id(web_url))
    assert web["url"] == web_url and meta(web)["item_id"] == f"web:{web['source_id']}"
    assert web["title"] == "A synthetic guide" and web["captured_at"] == BASE

    # Credential-shaped destination param: redacted, name recorded, value nowhere.
    docs = bookmark(fresh_db, "x_link", locator_source_id("https://docs.example.com/share?page=2"))
    assert json.loads(docs["redacted_param_names"]) == ["access_token"]
    assert docs["redacted_param_count"] == 1
    dump = "\n".join(fresh_db.iterdump())
    assert CANARY not in dump and ACCESS not in dump and "synthetic_next_token" not in dump


def test_item_links_follow_the_contract_mapping(fresh_db, x_api) -> None:
    x_api.get(BOOKMARKS).mock(side_effect=serve())
    run(fresh_db, FakeClock())
    links = {
        (r["parent_ref"], r["relation"], r["position"]): (r["child_ref"], r["locator_resolution"], r["entity_status"])
        for r in fresh_db.execute("SELECT * FROM item_links")
    }
    p = {i: f"x:post:{BOOKMARKED[i - 1]}" for i in range(1, 9)}
    web = lambda url: f"web:{locator_source_id(url)}"  # noqa: E731
    assert links == {
        (p[1], "links_to", 0): (web("https://www.example.com/articles/synthetic-guide?id=42"), "unwound", 200),
        (p[2], "links_to", 0): ("github:repo:example-org/example-repo", "unwound", 200),
        (p[2], "links_to", 1): (web("https://blog.example.org/posts/write-up"), "unwound", 200),
        (p[3], "links_to", 0): ("x:post:1111111111111111191", "expanded", None),
        (p[3], "quotes", 0): ("x:post:1111111111111111191", "api_reference", None),
        ("x:post:1111111111111111191", "links_to", 0): (web("https://t.co/EXAMPLE0191"), "short", None),
        (p[5], "links_to", 0): ("x:article:1111111111111111905", "expanded", None),
        (p[5], "replies_to", 0): ("x:post:1111111111111111193", "api_reference", None),
        (p[5], "article", 0): ("x:article:1111111111111111905", "api_reference", None),
        (p[6], "links_to", 0): (web("https://example.org/papers/synthetic-paper.pdf"), "expanded", None),
        (p[6], "links_to", 1): (web("https://www.example.org/a-page-that-is-gone"), "unwound", 404),
        (p[6], "quotes", 0): ("x:post:1111111111111111192", "api_reference", None),
        (p[8], "links_to", 0): (web("https://docs.example.com/share?page=2"), "unwound", 200),
    }
    # The media post (104) links only its own photo: no item, no link.
    assert count(fresh_db, "item_links", f"parent_ref = '{p[4]}'") == 0
    # Every link row points at real bookmark rows on both ends.
    assert count(fresh_db, "item_links", "parent_bookmark_id IS NULL OR child_bookmark_id IS NULL") == 0


def test_referenced_posts_articles_and_errors(fresh_db, x_api) -> None:
    x_api.get(BOOKMARKS).mock(side_effect=serve())
    run(fresh_db, FakeClock())

    quoted = bookmark(fresh_db, "x", "1111111111111111191")
    assert meta(quoted)["bookmarked"] is False and "first_sync_run_id" not in meta(quoted)
    assert meta(quoted)["author"]["username"] == "quoted_author"
    assert quoted["title"].startswith("@quoted_author: The quoted synthetic post")
    assert quoted["url"] == "https://x.com/quoted_author/status/1111111111111111191"
    (q_rev,) = fresh_db.execute(
        "SELECT * FROM scrape_history WHERE item_ref = 'x:post:1111111111111111191'"
    ).fetchall()
    assert q_rev["coverage"] == "full" and q_rev["observed_at"] is None  # not a bookmark

    # errors[]: resource-not-found → deleted; not-authorized → blocked.
    for pid, coverage, status in (
        ("1111111111111111192", "deleted", "failed"),
        ("1111111111111111193", "blocked", "auth_required"),
    ):
        row = bookmark(fresh_db, "x", pid)
        assert meta(row)["bookmarked"] is False and row["enrichment_source"] == "none"
        sr = fresh_db.execute(
            "SELECT * FROM scrape_results WHERE bookmark_id = ?", (row["bookmark_id"],)
        ).fetchone()
        assert sr["scrape_status"] == status and sr["content_text"] is None
        (rev,) = fresh_db.execute(
            "SELECT * FROM scrape_history WHERE bookmark_id = ?", (row["bookmark_id"],)
        ).fetchall()
        assert rev["coverage"] == coverage and rev["char_count"] == 0
        assert rev["content_sha256"] == content_digest("") and rev["content_text"] is None
    # The suspended user (resource_type user) creates nothing.
    assert bookmark(fresh_db, "x", "9000000099") is None

    article = bookmark(fresh_db, "x_article", "1111111111111111905")
    assert article["url"] == "https://x.com/i/article/1111111111111111905"
    assert article["title"] == "A synthetic long-form article"
    assert meta(article)["author"]["username"] == "example_writer"
    art_text = fresh_db.execute(
        "SELECT content_text FROM scrape_results WHERE bookmark_id = ?", (article["bookmark_id"],)
    ).fetchone()[0]
    assert art_text.startswith("The first lines of a synthetic article.")
    # An article object without an ID (post 106) stays post metadata only.
    assert meta(bookmark(fresh_db, "x", BOOKMARKED[5]))["article"] == {
        "id": None, "title": "A synthetic article whose body is not returned", "has_body": False,
    }


def test_checkpoint_and_usage_rows(fresh_db, x_api) -> None:
    x_api.get(BOOKMARKS).mock(side_effect=serve())
    report = run(fresh_db, FakeClock())
    (cp,) = fresh_db.execute("SELECT * FROM sync_checkpoints").fetchall()
    assert cp["run_ulid"] == report.run_ulid and cp["source"] == "x"
    assert (cp["account_ref"], cp["collection"], cp["status"]) == (USER, "bookmarks", "completed")
    assert (cp["pages_fetched"], cp["items_seen"], cp["items_new"]) == (3, 8, 8)
    assert cp["next_token"] is None and cp["completed_at"] == BASE and cp["error_code"] is None
    config = json.loads(cp["config_json"])
    assert config["max_results"] == 100 and config["post_fields_param"] == "tweet.fields"
    assert config["bookmark_cap"] == 800 and config["mode"] == "incremental"
    assert config["max_pages"] == 10 and config["stop_reason"] == "end_of_list"

    usage = fresh_db.execute("SELECT * FROM usage_records ORDER BY usage_record_id").fetchall()
    assert [u["operation"] for u in usage] == [
        "GET /2/users/me"] + ["GET /2/users/:id/bookmarks"] * 3
    # posts + included posts + included users, $0.001 each.
    assert [u["resource_count"] for u in usage] == [1, 8, 3, 3]
    assert [u["cost_micro_usd"] for u in usage] == [1000, 8000, 3000, 3000]
    assert {(u["kind"], u["provider"], u["cost_basis"], u["request_count"]) for u in usage} == {
        ("x_api", "x", "estimated", 1)}
    assert {u["sync_checkpoint_id"] for u in usage} == {cp["sync_checkpoint_id"]}
    assert report.cost_micro_usd == 15000


def test_second_identical_run_is_incremental_and_adds_no_revisions(fresh_db, x_api) -> None:
    route = x_api.get(BOOKMARKS).mock(side_effect=serve())
    first = run(fresh_db, FakeClock())
    before = snapshot(fresh_db)
    rows_before = {r["bookmark_id"]: dict(r) for r in fresh_db.execute("SELECT * FROM bookmarks")}
    results_before = [dict(r) for r in fresh_db.execute("SELECT * FROM scrape_results")]

    later = BASE + 86400
    second = run(fresh_db, FakeClock(later))
    assert len(route.calls) == 4  # three, then one page: every post on it was known
    assert second.status == "completed" and second.stop_reason == "caught_up"
    assert (second.pages, second.posts_seen, second.posts_new, second.revisions_new) == (1, 4, 0, 0)
    after = snapshot(fresh_db)
    assert after["scrape_history"] == before["scrape_history"]
    assert after["bookmarks"] == before["bookmarks"] and after["item_links"] == before["item_links"]
    assert after["sync_checkpoints"] == 2 and after["usage_records"] == before["usage_records"] + 2
    assert [dict(r) for r in fresh_db.execute("SELECT * FROM scrape_results")] == results_before

    # Re-seen posts change last_observed_at / last_sync_run_id only.
    for bid, old in rows_before.items():
        new = dict(fresh_db.execute("SELECT * FROM bookmarks WHERE bookmark_id = ?", (bid,)).fetchone())
        old_meta, new_meta = json.loads(old.pop("source_metadata")), json.loads(new.pop("source_metadata"))
        assert new == old
        if old["source"] == "x" and old["source_id"] in BOOKMARKED[:4]:
            assert new_meta.pop("last_observed_at") == later
            assert new_meta.pop("last_sync_run_id") == second.run_ulid
            old_meta.pop("last_observed_at"), old_meta.pop("last_sync_run_id")
        assert new_meta == old_meta
    assert meta(bookmark(fresh_db, "x", BOOKMARKED[0]))["first_sync_run_id"] == first.run_ulid
    cp = fresh_db.execute("SELECT * FROM sync_checkpoints WHERE run_ulid = ?", (second.run_ulid,)).fetchone()
    assert (cp["status"], cp["pages_fetched"], cp["items_new"], cp["next_token"]) == ("completed", 1, 0, None)


def test_full_walks_everything_without_new_revisions(fresh_db, x_api) -> None:
    route = x_api.get(BOOKMARKS).mock(side_effect=serve())
    run(fresh_db, FakeClock())
    before = snapshot(fresh_db)
    report = run(fresh_db, FakeClock(BASE + 3600), mode="full")
    assert len(route.calls) == 6 and report.pages == 3 and report.stop_reason == "end_of_list"
    assert (report.posts_new, report.revisions_new) == (0, 0)
    assert snapshot(fresh_db)["scrape_history"] == before["scrape_history"]


def test_new_bookmark_on_top_is_picked_up_incrementally(fresh_db, x_api) -> None:
    x_api.get(BOOKMARKS).mock(side_effect=serve())
    run(fresh_db, FakeClock(), max_pages=1)  # only page 1 known; run paused
    fresh_db.execute("DELETE FROM sync_checkpoints")  # forget the paused run
    fresh_db.commit()
    report = run(fresh_db, FakeClock(BASE + 60))
    # Page 1 is all known → stop; pages 2-3 were never seen, but incremental
    # mode only promises new bookmarks at the top. --full reaches the rest.
    assert report.stop_reason == "caught_up" and report.pages == 1
    full = run(fresh_db, FakeClock(BASE + 120), mode="full")
    assert full.posts_new == 4 and count(fresh_db, "bookmarks", "source = 'x'") == 11


def test_changed_post_text_adds_a_revision(fresh_db, x_api) -> None:
    x_api.get(BOOKMARKS).mock(side_effect=serve())
    run(fresh_db, FakeClock())
    edited = json.loads((FIX / "bookmarks_page1.json").read_text())
    edited["data"][0]["text"] = "An edited synthetic post https://t.co/EXAMPLE0101"
    x_api.get(BOOKMARKS).mock(side_effect=serve({None: [httpx.Response(200, json=edited)]}))
    report = run(fresh_db, FakeClock(BASE + 60))
    assert report.revisions_new == 1
    revs = fresh_db.execute(
        "SELECT content_text FROM scrape_history WHERE item_ref = ? ORDER BY scrape_history_id",
        (f"x:post:{BOOKMARKED[0]}",),
    ).fetchall()
    assert len(revs) == 2 and revs[1][0].startswith("An edited synthetic post https://www.example.com/")


# ── Crash, resume, caps ─────────────────────────────────────────────────

def test_api_failure_on_page_3_then_resume_without_duplicates(fresh_db, x_api) -> None:
    route = x_api.get(BOOKMARKS).mock(
        side_effect=serve({T3: [httpx.Response(503, json={"title": "Service Unavailable"})]})
    )
    failed = run(fresh_db, FakeClock())
    assert failed.status == "failed" and failed.pages == 2
    cp = fresh_db.execute("SELECT * FROM sync_checkpoints").fetchone()
    assert (cp["status"], cp["next_token"], cp["pages_fetched"], cp["items_seen"]) == ("failed", T3, 2, 6)
    assert cp["error_code"] == "XApiError" and cp["completed_at"] is None

    resumed = run(fresh_db, FakeClock(BASE + 60), resume=True)
    assert resumed.resumed and resumed.run_ulid == failed.run_ulid
    assert resumed.status == "completed" and resumed.pages == 1
    assert [c.request.url.params.get("pagination_token") for c in route.calls] == [None, T2, T3, T3]
    totals = snapshot(fresh_db)
    assert {k: totals[k] for k in EXPECTED if k != "usage_records"} == {
        k: v for k, v in EXPECTED.items() if k != "usage_records"}
    assert totals["usage_records"] == 5  # me, p1, p2, me (resume), p3
    cp = fresh_db.execute("SELECT * FROM sync_checkpoints").fetchone()
    assert (cp["status"], cp["pages_fetched"], cp["items_seen"], cp["items_new"]) == ("completed", 3, 8, 8)
    assert cp["error_code"] is None and cp["next_token"] is None


class CrashOnPage3(XPageWriter):
    pages = 0

    def write_page(self, page):
        result = super().write_page(page)  # rows written, then the "crash"
        CrashOnPage3.pages += 1
        if CrashOnPage3.pages == 3:
            raise RuntimeError("injected crash after writing page 3")
        return result


def test_writer_crash_rolls_back_the_page_and_resume_completes(fresh_db, x_api) -> None:
    x_api.get(BOOKMARKS).mock(side_effect=serve())
    CrashOnPage3.pages = 0
    with pytest.raises(RuntimeError, match="injected crash"):
        sync_bookmarks(fresh_db, client(FakeClock()), policy=DomainPolicy.empty(),
                       clock=FakeClock(), writer_factory=CrashOnPage3)
    # Page 3's rows were rolled back with its checkpoint update.
    assert bookmark(fresh_db, "x", BOOKMARKED[6]) is None
    cp = fresh_db.execute("SELECT * FROM sync_checkpoints").fetchone()
    assert (cp["status"], cp["next_token"], cp["pages_fetched"]) == ("failed", T3, 2)
    assert cp["error_code"] == "RuntimeError"
    # me + pages 1-3: page 3 was fetched (billed) even though its write was rolled back.
    usage = fresh_db.execute("SELECT * FROM usage_records ORDER BY usage_record_id").fetchall()
    assert len(usage) == 4
    assert usage[-1]["operation"] == "GET /2/users/:id/bookmarks"
    assert usage[-1]["sync_checkpoint_id"] == cp["sync_checkpoint_id"]
    assert usage[-1]["resource_count"] > 0

    # A killed process leaves "running": resume picks that up as well.
    fresh_db.execute("UPDATE sync_checkpoints SET status = 'running', error_code = NULL")
    fresh_db.commit()
    resumed = run(fresh_db, FakeClock(BASE + 60), resume=True)
    assert resumed.status == "completed"
    totals = snapshot(fresh_db)
    assert totals["bookmarks"] == EXPECTED["bookmarks"]
    assert totals["scrape_history"] == EXPECTED["scrape_history"]
    assert totals["item_links"] == EXPECTED["item_links"]


def test_usage_capped_is_exhausted_and_resumable_later(fresh_db, x_api) -> None:
    route = x_api.get(BOOKMARKS).mock(side_effect=serve({T2: [fx("error_429_usage_capped")]}))
    capped = run(fresh_db, FakeClock())
    assert capped.status == "exhausted" and capped.stop_reason == "usage_capped"
    cp = fresh_db.execute("SELECT * FROM sync_checkpoints").fetchone()
    assert (cp["status"], cp["next_token"], cp["error_code"]) == ("exhausted", T2, "usage-capped")
    assert len(route.calls) == 2 and count(fresh_db, "usage_records") == 2  # me + page 1

    # Without --resume a new run starts from the top; --resume continues the cap.
    resumed = run(fresh_db, FakeClock(BASE + 86400), resume=True)
    assert resumed.run_ulid == capped.run_ulid and resumed.status == "completed"
    assert snapshot(fresh_db)["bookmarks"] == EXPECTED["bookmarks"]
    assert count(fresh_db, "sync_checkpoints", "status = 'completed'") == 1


def test_rate_limit_past_budget_pauses_until_reset(fresh_db, x_api) -> None:
    x_api.get(BOOKMARKS).mock(side_effect=serve({T2: [fx("error_429_rate_limit")]}))
    config = XConnectorConfig(max_rate_limit_waits=0)
    paused = run(fresh_db, FakeClock(), config=config)
    cp = fresh_db.execute("SELECT * FROM sync_checkpoints").fetchone()
    assert paused.status == "paused" and cp["status"] == "paused"
    assert cp["resume_after"] == paused.resume_after and cp["resume_after"] > BASE
    assert cp["error_code"] == "rate-limit-exceeded" and cp["next_token"] == T2

    with pytest.raises(NothingToResume, match="rate-limited"):
        run(fresh_db, FakeClock(BASE + 1), resume=True)
    resumed = run(fresh_db, FakeClock(cp["resume_after"] + 1), resume=True)
    assert resumed.status == "completed"


def test_page_budget_pauses_and_resume_continues(fresh_db, x_api) -> None:
    x_api.get(BOOKMARKS).mock(side_effect=serve())
    paused = run(fresh_db, FakeClock(), max_pages=1)
    assert paused.status == "paused" and paused.stop_reason == "page_budget"
    cp = fresh_db.execute("SELECT * FROM sync_checkpoints").fetchone()
    assert (cp["next_token"], cp["pages_fetched"], cp["resume_after"]) == (T2, 1, None)
    resumed = run(fresh_db, FakeClock(BASE + 60), resume=True)
    assert resumed.status == "completed" and resumed.pages == 2
    assert snapshot(fresh_db)["scrape_history"] == EXPECTED["scrape_history"]


def test_resume_with_nothing_to_resume(fresh_db, x_api) -> None:
    x_api.get(BOOKMARKS).mock(side_effect=serve())
    run(fresh_db, FakeClock())
    me_calls = len(_me_calls(x_api))
    usage = count(fresh_db, "usage_records")
    with pytest.raises(NothingToResume):
        run(fresh_db, FakeClock(), resume=True)
    # Refused from the DB alone: no billed /users/me call, nothing to record.
    assert len(_me_calls(x_api)) == me_calls and count(fresh_db, "usage_records") == usage


def _me_calls(mock) -> list:
    return [c for c in mock.calls if c.request.url.path == "/2/users/me"]


def test_rate_limited_resume_is_refused_before_users_me(fresh_db, x_api) -> None:
    x_api.get(BOOKMARKS).mock(side_effect=serve({T2: [fx("error_429_rate_limit")]}))
    run(fresh_db, FakeClock(), config=XConnectorConfig(max_rate_limit_waits=0))
    me_calls, usage = len(_me_calls(x_api)), count(fresh_db, "usage_records")
    with pytest.raises(NothingToResume, match="rate-limited"):
        run(fresh_db, FakeClock(BASE + 1), resume=True)
    assert len(_me_calls(x_api)) == me_calls and count(fresh_db, "usage_records") == usage


def test_refusal_after_users_me_still_records_its_usage(fresh_db, x_api) -> None:
    # An unfinished run of another account: only /users/me can tell it isn't ours.
    fresh_db.execute(
        "INSERT INTO sync_checkpoints (run_ulid, source, account_ref, collection, status, "
        "config_json, started_at, updated_at) VALUES "
        "('01JA0000000000000000000009', 'x', '9000000999', 'bookmarks', 'paused', '{}', ?, ?)",
        (BASE, BASE),
    )
    fresh_db.commit()
    with pytest.raises(NothingToResume):
        run(fresh_db, FakeClock(), resume=True)
    assert len(_me_calls(x_api)) == 1
    usage = fresh_db.execute("SELECT * FROM usage_records").fetchall()
    assert [(u["operation"], u["sync_checkpoint_id"], u["resource_count"]) for u in usage] == [
        ("GET /2/users/me", None, 1)
    ]


def test_pagination_cycle_fails_the_run_and_records_the_billed_page(fresh_db, x_api) -> None:
    page3 = json.loads((FIX / "bookmarks_page3.json").read_text(encoding="utf-8"))
    page3["meta"]["next_token"] = T2  # T2 -> T3 -> T2: a loop
    x_api.get(BOOKMARKS).mock(side_effect=serve({T3: [httpx.Response(200, json=page3)]}))
    report = run(fresh_db, FakeClock())
    assert report.status == "failed" and report.error_code == "pagination-cycle"
    assert report.pages == 2
    cp = fresh_db.execute("SELECT * FROM sync_checkpoints").fetchone()
    assert (cp["status"], cp["next_token"], cp["error_code"]) == ("failed", T3, "pagination-cycle")
    usage = [r["operation"] for r in fresh_db.execute("SELECT operation FROM usage_records")]
    assert usage == ["GET /2/users/me"] + ["GET /2/users/:id/bookmarks"] * 3
    total = fresh_db.execute("SELECT SUM(cost_micro_usd) FROM usage_records").fetchone()[0]
    assert report.cost_micro_usd == total


# ── Domain policy ───────────────────────────────────────────────────────

def test_domain_policy_hides_link_rows_like_browser_bookmarks(fresh_db, x_api) -> None:
    x_api.get(BOOKMARKS).mock(side_effect=serve())
    run(fresh_db, FakeClock(), policy=DomainPolicy(domain_patterns=("docs.example.com",),
                                                   path_patterns=("github.com/example-org/*",)))
    hidden = {r["url"] for r in fresh_db.execute("SELECT url FROM bookmarks WHERE content_visible = 0")}
    assert hidden == {"https://docs.example.com/share?page=2", "https://github.com/example-org/example-repo"}

    # A hidden child's locator never reaches the parent post's stored text.
    def texts(post_id: str) -> list[str]:
        return [r[0] for r in fresh_db.execute(
            "SELECT content_text FROM scrape_results sr JOIN bookmarks b USING (bookmark_id) "
            "WHERE b.source = 'x' AND b.source_id = ? UNION ALL "
            "SELECT content_text FROM scrape_history WHERE item_ref = ?",
            (post_id, f"x:post:{post_id}"),
        )]
    for pid, gone in ((BOOKMARKED[7], "docs.example.com"), (BOOKMARKED[1], "github.com/example-org")):
        assert all("[hidden link]" in t and gone not in t and "t.co" not in t for t in texts(pid))
    assert "https://blog.example.org/posts/write-up" in texts(BOOKMARKED[1])[0]
    titles = "\n".join(r[0] or "" for r in fresh_db.execute("SELECT title FROM bookmarks WHERE source = 'x'"))
    assert "docs.example.com" not in titles

    # Re-applied on the next sync, as `ingest html` does; the now-visible
    # locator becomes a new revision of the post text.
    x_api.get(BOOKMARKS).mock(side_effect=serve())
    report = run(fresh_db, FakeClock(BASE + 60), mode="full")
    assert count(fresh_db, "bookmarks", "content_visible = 0") == 0
    assert report.revisions_new == 2


def test_hidden_post_stores_no_text(fresh_db, x_api) -> None:
    x_api.get(BOOKMARKS).mock(side_effect=serve())
    run(fresh_db, FakeClock(), policy=DomainPolicy(path_patterns=("x.com/example_writer/*",)))
    for pid in BOOKMARKED[4:6]:  # example_writer's posts
        row = bookmark(fresh_db, "x", pid)
        assert row["content_visible"] == 0
        assert count(fresh_db, "scrape_results", f"bookmark_id = {row['bookmark_id']}") == 0
        assert count(fresh_db, "scrape_history", f"bookmark_id = {row['bookmark_id']}") == 0


# ── Contract records ────────────────────────────────────────────────────

def _registry() -> tuple[dict, Registry]:
    schemas = {
        p.name.removesuffix(".schema.json"): json.loads(p.read_text())
        for p in SCHEMA_DIR.glob("*.schema.json")
    }
    registry = Registry().with_resources(
        (s["$id"], Resource.from_contents(s)) for s in schemas.values()
    )
    return schemas, registry


def _assert_valid(schemas, registry, name: str, record: dict) -> None:
    errors = list(Draft202012Validator(schemas[name], registry=registry).iter_errors(record))
    assert not errors, (name, record, [e.message for e in errors])


def test_rows_map_to_schema_valid_contract_records(fresh_db, x_api) -> None:
    x_api.get(BOOKMARKS).mock(side_effect=serve())
    report = run(fresh_db, FakeClock())
    schemas, registry = _registry()

    items = {}
    for row in fresh_db.execute("SELECT * FROM bookmarks"):
        record = source_item_record(row)
        _assert_valid(schemas, registry, "source-item", record)
        items[record["item_id"]] = record
    assert items["github:repo:example-org/example-repo"]["kind"] == "github_repo"
    assert items[f"x:post:{BOOKMARKED[1]}"]["x_post"]["is_long_post"] is True
    assert items["x:article:1111111111111111905"]["kind"] == "x_article"
    docs = items[link_item_id("https://docs.example.com/share?page=2")]
    assert docs["redaction"]["param_names"] == ["access_token"]

    for row in fresh_db.execute("SELECT * FROM item_links"):
        record = item_link_record(row)
        _assert_valid(schemas, registry, "item-link", record)
        assert record["parent_item_id"] in items and record["child_item_id"] in items

    observations = [
        rec for row in fresh_db.execute("SELECT * FROM bookmarks")
        if (rec := bookmark_observation_record(row)) is not None
    ]
    assert sorted(o["item_id"] for o in observations) == sorted(f"x:post:{i}" for i in BOOKMARKED)
    for record in observations:
        _assert_valid(schemas, registry, "bookmark-observation", record)
        assert record["observed_at_basis"] == "first_seen_by_sync"
        assert record["sync_run_id"] == report.run_ulid

    for row in fresh_db.execute("SELECT * FROM scrape_history"):
        record = content_revision_record(row, include_text=True)
        _assert_valid(schemas, registry, "content-revision", record)
        assert record["item_id"] in items and record["freshness"] == "current"

    for row in fresh_db.execute(
        "SELECT u.*, s.run_ulid FROM usage_records u JOIN sync_checkpoints s USING (sync_checkpoint_id)"
    ):
        _assert_valid(schemas, registry, "usage-record", usage_record(row, sync_run_id=row["run_ulid"]))
