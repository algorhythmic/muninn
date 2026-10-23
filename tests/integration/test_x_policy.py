"""Domain policy on X rows: canonical post locators, hidden titles, masks.

Synthetic pages through the real parser and writer (no HTTP): reserved
example hosts, invented IDs and handles.
"""

from __future__ import annotations

import sqlite3

import pytest

from muninn.db import transaction
from muninn.ingest.policy import apply_policy, child_mask_patterns, mask_text, policy_hides
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


# ── finding 1: the re-apply pass ────────────────────────────────────────

SECRET_SITE = DomainPolicy(domain_patterns=("secret.example.com",))
OLD = {"id": P2, "author_id": "501", "text": "read https://t.co/AAAAAAAAAA",
       "entities": {"urls": [{"url": "https://t.co/AAAAAAAAAA",
                              "unwound_url": "https://secret.example.com/my/case"}]}}


def _seed_fts(conn, bookmark_id, summary="", content=""):
    title = conn.execute("SELECT title FROM bookmarks WHERE bookmark_id = ?", (bookmark_id,)).fetchone()[0]
    conn.execute("INSERT INTO fts_bookmarks (rowid, title, summary, content_text, tags) "
                 "VALUES (?, ?, ?, ?, '[]')", (bookmark_id, title, summary, content))
    conn.commit()


def test_apply_policy_hides_and_masks_rows_no_sync_touched(fresh_db) -> None:
    write(fresh_db, DomainPolicy.empty(), page([OLD]))
    parent = row(fresh_db, P2)
    assert live(fresh_db, parent["bookmark_id"]) == "read https://secret.example.com/my/case"
    _seed_fts(fresh_db, parent["bookmark_id"], summary="About https://secret.example.com/my/case",
              content="read https://secret.example.com/my/case")
    # The next run under the new policy only sees a newer post (the r3 repro).
    write(fresh_db, SECRET_SITE, page([{"id": P9, "author_id": "501", "text": "hello"}]),
          run="01JA0000000000000000000001")
    assert live(fresh_db, parent["bookmark_id"]) == "read https://secret.example.com/my/case"

    report = apply_policy(fresh_db, SECRET_SITE, now=BASE + 600)
    assert (report.newly_hidden, report.texts_masked, report.revisions_new, report.fts_refreshed) == (1, 1, 1, 1)
    link = fresh_db.execute("SELECT * FROM bookmarks WHERE source = 'x_link'").fetchone()
    assert link["content_visible"] == 0
    parent = row(fresh_db, P2)
    assert live(fresh_db, parent["bookmark_id"]) == "read [hidden link]"
    assert parent["title"] == "@pub: read [hidden link]"
    assert history(fresh_db, P2) == ["read https://secret.example.com/my/case", "read [hidden link]"]
    newest = fresh_db.execute(
        "SELECT retrieval_method, retrieved_at, observed_at, coverage FROM scrape_history "
        "WHERE item_ref = ? ORDER BY revision_ulid DESC LIMIT 1", (f"x:post:{P2}",)).fetchone()
    assert tuple(newest) == ("x_api", BASE + 600, BASE, "full")
    fts = fresh_db.execute("SELECT title, summary, content_text FROM fts_bookmarks WHERE rowid = ?",
                           (parent["bookmark_id"],)).fetchone()
    assert tuple(fts) == ("@pub: read [hidden link]", "About [hidden link]", "read [hidden link]")

    # Idempotent; and the writer derives the same text on the next sighting.
    again = apply_policy(fresh_db, SECRET_SITE, now=BASE + 700)
    assert not again.changed and again.revisions_new == 0
    write(fresh_db, SECRET_SITE, page([OLD], observed_at=BASE + 800), run="01JA0000000000000000000002")
    assert history(fresh_db, P2) == ["read https://secret.example.com/my/case", "read [hidden link]"]


def test_apply_policy_dry_run_writes_nothing(fresh_db) -> None:
    write(fresh_db, DomainPolicy.empty(), page([OLD]))
    before = [tuple(r) for r in fresh_db.execute("SELECT * FROM bookmarks")]
    revisions = len(history(fresh_db, P2))
    report = apply_policy(fresh_db, SECRET_SITE, dry_run=True)
    assert report.dry_run and report.newly_hidden == 1 and report.texts_masked == 1
    assert [tuple(r) for r in fresh_db.execute("SELECT * FROM bookmarks")] == before
    assert len(history(fresh_db, P2)) == revisions and not fresh_db.in_transaction


def test_apply_policy_unhide_restores_visibility_not_masked_text(fresh_db) -> None:
    data = [{"id": P1, "author_id": "500", "text": "my diary"}, OLD]
    write(fresh_db, DomainPolicy.empty(), page(data))
    apply_policy(fresh_db, DomainPolicy(domain_patterns=("secret.example.com",),
                                        path_patterns=("x.com/secretuser/*",)))
    assert row(fresh_db, P1)["title"] == "@secretuser"
    report = apply_policy(fresh_db, DomainPolicy.empty())
    assert report.newly_visible == 2
    assert (row(fresh_db, P1)["content_visible"], row(fresh_db, P1)["title"]) == (1, "@secretuser: my diary")
    # The mask stays until a re-sync re-reads the post (`ingest x --full`) ...
    assert live(fresh_db, row(fresh_db, P2)["bookmark_id"]) == "read [hidden link]"
    write(fresh_db, DomainPolicy.empty(), page([OLD], observed_at=BASE + 60), run="01JA0000000000000000000001")
    assert live(fresh_db, row(fresh_db, P2)["bookmark_id"]) == "read https://secret.example.com/my/case"


def test_apply_policy_canonicalizes_legacy_post_locators(fresh_db) -> None:
    fresh_db.execute(
        "INSERT INTO bookmarks (source, source_id, captured_at, url, domain, source_metadata, content_visible) "
        "VALUES ('x', ?, ?, ?, 'twitter.com', ?, 1)",
        (P1, BASE, f"https://twitter.com/SecretUser/status/{P1}", '{"kind": "x_post", "bookmarked": false}'),
    )
    fresh_db.commit()
    report = apply_policy(fresh_db, SECRET)
    assert report.locators_canonicalized == 1 and report.newly_hidden == 1
    r = row(fresh_db, P1)
    assert (r["url"], r["domain"], r["content_visible"]) == (
        f"https://x.com/SecretUser/status/{P1}", "x.com", 0)


def test_apply_policy_hidden_article_title_and_revival(fresh_db) -> None:
    post = {"id": P2, "author_id": "501", "text": "an article",
            "article": {"id": "1000000000000000777", "title": "PRIVATE ARTICLE TITLE"}}
    write(fresh_db, DomainPolicy.empty(), page([post]))
    apply_policy(fresh_db, DomainPolicy(path_patterns=("x.com/i/article/*",)))
    assert row(fresh_db, "1000000000000000777", "x_article")["title"] is None
    apply_policy(fresh_db, DomainPolicy.empty())
    assert row(fresh_db, "1000000000000000777", "x_article")["title"] == "PRIVATE ARTICLE TITLE"


def test_apply_policy_covers_browser_rows_like_reingest(fresh_db, tmp_path) -> None:
    from muninn.ingest import ingest_html

    html = tmp_path / "b.html"
    html.write_text(
        '<!DOCTYPE NETSCAPE-Bookmark-file-1>\n<DL><p>\n<DT><H3 ADD_DATE="1609459200">Era</H3>\n<DL><p>\n'
        '<DT><A HREF="https://secret.example.com/a" ADD_DATE="1609459201">A</A>\n'
        '<DT><A HREF="https://example.org/b" ADD_DATE="1609459202">B</A>\n'
        '<DT><A HREF="https://twitter.com/SecretUser/status/1" ADD_DATE="1609459203">C</A>\n'
        '</DL><p>\n</DL><p>\n', encoding="utf-8")
    policy = DomainPolicy(domain_patterns=("secret.example.com",), path_patterns=("x.com/secretuser/*",))
    ingest_html(html, fresh_db, DomainPolicy.empty())
    report = apply_policy(fresh_db, policy)
    applied = [tuple(r) for r in fresh_db.execute("SELECT * FROM bookmarks ORDER BY bookmark_id")]
    assert report.newly_hidden == 2
    ingest_html(html, fresh_db, policy)
    assert [tuple(r) for r in fresh_db.execute("SELECT * FROM bookmarks ORDER BY bookmark_id")] == applied
    # Only the X sources when `ingest x` runs it.
    apply_policy(fresh_db, DomainPolicy.empty(), sources=("x", "x_article", "x_link"))
    assert fresh_db.execute("SELECT COUNT(*) FROM bookmarks WHERE content_visible = 0").fetchone()[0] == 2


def test_apply_policy_refuses_an_open_transaction(fresh_db) -> None:
    fresh_db.execute("UPDATE bookmarks SET title = title")
    fresh_db.execute("INSERT INTO sync_checkpoints (run_ulid, source, account_ref, collection, status, "
                     "config_json, started_at, updated_at) VALUES "
                     "('01JA0000000000000000000009', 'x', '1', 'bookmarks', 'running', '{}', 1, 1)")
    with pytest.raises(RuntimeError, match="outside an open transaction"):
        apply_policy(fresh_db, SECRET)
    fresh_db.rollback()


def test_cli_policy_apply(db_path, tmp_path) -> None:
    from click.testing import CliRunner

    from muninn.consumers.cli.main import cli
    from muninn.db import connect

    conn = connect(db_path)
    write(conn, DomainPolicy.empty(), page([OLD]))
    conn.close()
    rules = tmp_path / "policy.yml"
    rules.write_text("domains:\n  - secret.example.com\n", encoding="utf-8")
    runner = CliRunner()
    dry = runner.invoke(cli, ["--db", str(db_path), "policy", "apply", "--dry-run", "--policy-file", str(rules)])
    assert dry.exit_code == 0, dry.output
    assert "Dry run (nothing written): would change" in dry.output and "newly_hidden=1" in dry.output
    real = runner.invoke(cli, ["--db", str(db_path), "policy", "apply", "--policy-file", str(rules)])
    assert real.exit_code == 0 and "Applied domain policy:" in real.output and "texts_masked=1" in real.output
    empty = tmp_path / "empty.yml"
    empty.write_text("", encoding="utf-8")
    back = runner.invoke(cli, ["--db", str(db_path), "policy", "apply", "--policy-file", str(empty)])
    assert "newly_visible=1" in back.output and "muninn ingest x --full" in back.output
    missing = runner.invoke(cli, ["--db", str(db_path), "policy", "apply", "--policy-file", str(tmp_path / "no.yml")])
    assert missing.exit_code == 1 and "not found" in missing.output
    help_text = " ".join(runner.invoke(cli, ["policy", "apply", "--help"]).output.split())
    assert "restored only by re-reading it from X: `muninn ingest x --full`" in help_text


# ── a hidden linked item keeps no title (the page's own) ────────────────

PAGE_TITLE = "SECRET PAGE TITLE"
TITLED = {"id": P2, "author_id": "501", "text": "read https://t.co/AAAAAAAAAA",
          "entities": {"urls": [{"url": "https://t.co/AAAAAAAAAA", "title": PAGE_TITLE,
                                 "unwound_url": "https://secret.example.com/my/case"}]}}
REPO = {"id": P9, "author_id": "501", "text": "code https://t.co/BBBBBBBBBB",
        "entities": {"urls": [{"url": "https://t.co/BBBBBBBBBB",
                               "expanded_url": "https://github.com/secret-org/tool"}]}}


def _link_row(conn, host: str) -> sqlite3.Row:
    return conn.execute("SELECT * FROM bookmarks WHERE source = 'x_link' AND domain = ?", (host,)).fetchone()


def test_writer_stores_no_title_for_a_hidden_link(fresh_db) -> None:
    write(fresh_db, SECRET_SITE, page([TITLED]))
    link = _link_row(fresh_db, "secret.example.com")
    assert (link["title"], link["content_visible"]) == (None, 0)
    # Visible on a later sighting: the title comes back from the link.
    write(fresh_db, DomainPolicy.empty(), page([TITLED], observed_at=BASE + 60), run="01JA0000000000000000000001")
    assert _link_row(fresh_db, "secret.example.com")["title"] == PAGE_TITLE
    # Hidden again: dropped again.
    write(fresh_db, SECRET_SITE, page([TITLED], observed_at=BASE + 120), run="01JA0000000000000000000002")
    assert _link_row(fresh_db, "secret.example.com")["title"] is None


def test_apply_policy_drops_a_hidden_links_title_and_index_row(fresh_db, tmp_path) -> None:
    import json

    import duckdb

    from muninn.consumers.parquet.export import export_parquet

    write(fresh_db, DomainPolicy.empty(), page([TITLED, REPO]))
    link, repo = _link_row(fresh_db, "secret.example.com"), _link_row(fresh_db, "github.com")
    assert (link["title"], repo["title"]) == (PAGE_TITLE, "secret-org/tool")
    _seed_fts(fresh_db, link["bookmark_id"], summary="A page.", content="Body.")
    hide = DomainPolicy(domain_patterns=("secret.example.com",), path_patterns=("github.com/secret-org/*",))

    report = apply_policy(fresh_db, hide)

    assert report.newly_hidden == 2
    for host in ("secret.example.com", "github.com"):
        assert (_link_row(fresh_db, host)["title"], _link_row(fresh_db, host)["content_visible"]) == (None, 0)
    assert fresh_db.execute("SELECT COUNT(*) FROM fts_bookmarks WHERE rowid = ?",
                            (link["bookmark_id"],)).fetchone()[0] == 0
    titles = json.dumps([r[0] for r in fresh_db.execute("SELECT title FROM bookmarks")])
    assert PAGE_TITLE not in titles and "secret-org/tool" not in titles

    # The full-table Parquet export carries no hidden page title either.
    db_file = fresh_db.execute("PRAGMA database_list").fetchone()[2]
    out = tmp_path / "all.parquet"
    export_parquet(out, db_path=db_file)
    exported = duckdb.connect().execute(f"SELECT title FROM read_parquet('{out}')").fetchall()
    assert PAGE_TITLE not in json.dumps(exported)

    # Visible again: a repository gets `owner/name` back at once; a page's
    # title waits for the next sync that sees the link.
    apply_policy(fresh_db, DomainPolicy.empty())
    assert _link_row(fresh_db, "github.com")["title"] == "secret-org/tool"
    assert _link_row(fresh_db, "secret.example.com")["title"] is None
    write(fresh_db, DomainPolicy.empty(), page([TITLED], observed_at=BASE + 60), run="01JA0000000000000000000001")
    assert _link_row(fresh_db, "secret.example.com")["title"] == PAGE_TITLE
