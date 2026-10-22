"""Domain policy on X rows: canonical post locators, hidden titles, masks.

Synthetic pages through the real parser and writer (no HTTP): reserved
example hosts, invented IDs and handles.
"""

from __future__ import annotations

import sqlite3

import pytest

from muninn.db import transaction
from muninn.ingest.policy import child_mask_patterns, mask_text, policy_hides
from muninn.ingest.x import XCheckpoint, parse_bookmark_page
from muninn.ingest.x_store import XPageWriter
from muninn.scrape.domain_policy import DomainPolicy

BASE = 1792584000
SECRET = DomainPolicy(path_patterns=("x.com/secretuser/*",))
USERS = [{"id": "500", "username": "secretuser", "name": "S"},
         {"id": "501", "username": "pub", "name": "P"}]
P1, P2, P3, P9 = (f"100000000000000000{i}" for i in (1, 2, 3, 9))


def page(data, users=USERS, tweets=(), observed_at=BASE):
    body = {"data": data, "includes": {"users": users, "tweets": list(tweets)}, "meta": {}}
    return parse_bookmark_page(body, checkpoint=XCheckpoint(user_id="9"), observed_at=observed_at)


def write(conn, policy, *pages, run="01JA0000000000000000000000"):
    writer = XPageWriter(conn, run_ulid=run, policy=policy)
    for p in pages:
        with transaction(conn):
            writer.write_page(p)


def row(conn, source_id, source="x") -> sqlite3.Row:
    return conn.execute(
        "SELECT * FROM bookmarks WHERE source = ? AND source_id = ?", (source, source_id)
    ).fetchone()


def live(conn, bookmark_id) -> str | None:
    r = conn.execute(
        "SELECT content_text FROM scrape_results WHERE bookmark_id = ? AND pass = 'live'", (bookmark_id,)
    ).fetchone()
    return r[0] if r else None


def history(conn, source_id) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT content_text FROM scrape_history WHERE item_ref = ? ORDER BY revision_ulid",
        (f"x:post:{source_id}",),
    )]


def link_post(pid, target_url, text="see https://t.co/AAAAAAAAAA", author="501"):
    return {"id": pid, "author_id": author, "text": text,
            "entities": {"urls": [{"url": "https://t.co/AAAAAAAAAA", "expanded_url": target_url}]}}


# ── finding 6: hidden posts and articles keep no text in the title ──────

def test_hidden_posts_titles_are_the_handle_only(fresh_db) -> None:
    data = [
        {"id": P1, "author_id": "500", "text": "PRIVATE DIARY CONTENT about my results"},
        {"id": P2, "author_id": "501", "text": "quoting",
         "referenced_tweets": [{"type": "quoted", "id": P3}]},
    ]
    write(fresh_db, SECRET, page(data, tweets=[{"id": P3, "author_id": "500", "text": "QUOTED SECRET"}]))
    for pid in (P1, P3):
        r = row(fresh_db, pid)
        assert (r["title"], r["content_visible"]) == ("@secretuser", 0)
        assert live(fresh_db, r["bookmark_id"]) is None
    assert row(fresh_db, P2)["title"] == "@pub: quoting"
    titles = " ".join(r[0] or "" for r in fresh_db.execute("SELECT title FROM bookmarks"))
    assert "PRIVATE" not in titles and "QUOTED SECRET" not in titles


def test_title_follows_visibility_on_a_later_sighting(fresh_db) -> None:
    data = [{"id": P1, "author_id": "500", "text": "PRIVATE DIARY CONTENT"}]
    write(fresh_db, DomainPolicy.empty(), page(data))
    assert row(fresh_db, P1)["title"] == "@secretuser: PRIVATE DIARY CONTENT"
    write(fresh_db, SECRET, page(data, observed_at=BASE + 60), run="01JA0000000000000000000001")
    assert (row(fresh_db, P1)["title"], row(fresh_db, P1)["content_visible"]) == ("@secretuser", 0)
    write(fresh_db, DomainPolicy.empty(), page(data, observed_at=BASE + 120), run="01JA0000000000000000000002")
    assert row(fresh_db, P1)["title"] == "@secretuser: PRIVATE DIARY CONTENT"


def test_hidden_article_has_no_title(fresh_db) -> None:
    post = {"id": P2, "author_id": "501", "text": "an article",
            "article": {"id": "1000000000000000777", "title": "PRIVATE ARTICLE TITLE"}}
    write(fresh_db, DomainPolicy(path_patterns=("x.com/i/article/*",)), page([post]))
    art = row(fresh_db, "1000000000000000777", "x_article")
    assert (art["title"], art["content_visible"]) == (None, 0)
    write(fresh_db, DomainPolicy.empty(), page([post], observed_at=BASE + 60), run="01JA0000000000000000000001")
    assert row(fresh_db, "1000000000000000777", "x_article")["title"] == "PRIVATE ARTICLE TITLE"


# ── finding 3: the post locator is canonical, whatever URL form linked it ─

@pytest.mark.parametrize("target", [
    f"https://twitter.com/secretuser/status/{P1}",
    f"https://mobile.twitter.com/SecretUser/status/{P1}?lang=en",
    f"https://www.x.com/SECRETUSER/status/{P1}/photo/1",
])
def test_referenced_post_locator_is_canonical_and_hidden(fresh_db, target) -> None:
    write(fresh_db, SECRET, page([link_post(P2, target)]))
    child = row(fresh_db, P1)
    assert child["url"].lower() == f"https://x.com/secretuser/status/{P1}"
    assert child["content_visible"] == 0
    parent = row(fresh_db, P2)
    assert live(fresh_db, parent["bookmark_id"]) == "see [hidden link]"
    assert parent["title"] == "@pub: see [hidden link]"

    # Later bookmarked itself: still hidden, no text stored, handle-only title.
    write(fresh_db, SECRET, page([{"id": P1, "author_id": "500", "text": "PRIVATE TEXT"}],
                                 observed_at=BASE + 60), run="01JA0000000000000000000001")
    child = row(fresh_db, P1)
    assert (child["url"], child["content_visible"], child["title"]) == (
        f"https://x.com/secretuser/status/{P1}", 0, "@secretuser")
    assert live(fresh_db, child["bookmark_id"]) is None
    assert "PRIVATE" not in " ".join(history(fresh_db, P1) + history(fresh_db, P2))


def test_unknown_handle_uses_i_web_then_the_api_handle(fresh_db) -> None:
    write(fresh_db, SECRET, page([link_post(P2, f"https://x.com/i/web/status/{P1}")]))
    assert (row(fresh_db, P1)["url"], row(fresh_db, P1)["content_visible"]) == (
        f"https://x.com/i/web/status/{P1}", 1)
    write(fresh_db, SECRET, page([{"id": P1, "author_id": "500", "text": "PRIVATE TEXT"}],
                                 observed_at=BASE + 60), run="01JA0000000000000000000001")
    child = row(fresh_db, P1)
    assert (child["url"], child["content_visible"]) == (f"https://x.com/secretuser/status/{P1}", 0)
    assert live(fresh_db, child["bookmark_id"]) is None


def test_api_handle_overrides_a_wrong_url_handle(fresh_db) -> None:
    # X serves a post under any handle in the path; the API's author is authoritative.
    write(fresh_db, SECRET, page([link_post(P2, f"https://twitter.com/someone_else/status/{P1}")]))
    assert row(fresh_db, P1)["content_visible"] == 1
    write(fresh_db, SECRET, page([{"id": P1, "author_id": "500", "text": "PRIVATE TEXT"}],
                                 observed_at=BASE + 60), run="01JA0000000000000000000001")
    assert (row(fresh_db, P1)["url"], row(fresh_db, P1)["content_visible"]) == (
        f"https://x.com/secretuser/status/{P1}", 0)


def test_handle_case_and_host_alias_do_not_escape_a_rule(fresh_db) -> None:
    users = [{"id": "500", "username": "SecretUser"}, {"id": "501", "username": "pub"}]
    write(fresh_db, DomainPolicy(path_patterns=("twitter.com/secretuser/*",)),
          page([{"id": P1, "author_id": "500", "text": "PRIVATE"}], users=users))
    r = row(fresh_db, P1)
    assert (r["url"], r["content_visible"], r["title"]) == (
        f"https://x.com/SecretUser/status/{P1}", 0, "@SecretUser")


def test_policy_hides_x_aliases_only_on_x_hosts() -> None:
    policy = DomainPolicy(path_patterns=("x.com/secretuser/*",))
    assert policy_hides(policy, "https://twitter.com/SecretUser/status/1")
    assert not policy_hides(policy, "https://m.twitter.com/secretuser")  # the rule needs a path
    assert policy_hides(policy, "https://m.twitter.com/secretuser/likes")
    assert not policy_hides(policy, "https://example.com/secretuser/status/1")
    assert not policy_hides(policy, "https://x.com/otheruser/status/1")
    assert not policy_hides(policy, None)


def test_child_masks_stop_at_the_locator() -> None:
    post = child_mask_patterns("x", P1, f"https://x.com/secretuser/status/{P1}")
    text = (f"a https://twitter.com/SecretUser/status/{P1}. b https://x.com/i/web/status/{P1}?s=1 "
            f"c https://x.com/u/status/{P1}9 d https://x.com/u/status/{P1}/photo/1)")
    assert mask_text(text, post) == (
        f"a [hidden link]. b [hidden link] c https://x.com/u/status/{P1}9 d [hidden link])")
    repo = child_mask_patterns("x_link", "d", "https://github.com/acme/tool", {"kind": "github_repo"})
    assert mask_text("x https://github.com/Acme/Tool.git, y https://github.com/acme/tool/tree/main "
                     "z https://github.com/acme/tool-2", repo) == (
        "x [hidden link], y https://github.com/acme/tool/tree/main z https://github.com/acme/tool-2")
    web = child_mask_patterns("x_link", "d", "https://a.example/x")
    assert mask_text("https://a.example/x https://a.example/x.html https://a.example/x/y", web) == (
        "[hidden link] https://a.example/x.html https://a.example/x/y")
