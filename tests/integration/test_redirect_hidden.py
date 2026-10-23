"""A visible item whose fetch ends on a policy-hidden locator is hidden.

A linked page on a visible host (a shortener, an old domain) can redirect
to a host ``domain_policy.yml`` hides. The rule: a row is hidden when its
own locator or any locator a fetch of it ended on (``scrape_results.
final_url``, a revision's ``final_locator``, the original inside a Wayback
URL) is hidden. It holds at scrape time and in ``muninn policy apply``,
and from there every consumer (packets, vault, MCP, keyword index, model
calls) treats the row as hidden.

ALL DATA IS SYNTHETIC (invented IDs, reserved example hosts). respx
refuses unmocked requests.
"""

from __future__ import annotations

import json
import sqlite3

import httpx
import pytest
import respx

from muninn.consumers.mcp import tools
from muninn.consumers.packets import export_packets, link_item_id, locator_source_id
from muninn.consumers.vault.compiler import compile_vault
from muninn.db import connect
from muninn.enrich.interest_profile import load_interest_profile
from muninn.enrich.suitability import assess_suitability
from muninn.ingest.policy import (
    HIDDEN_LINK,
    apply_policy,
    archived_original,
    row_hidden,
)
from muninn.ingest.x_store import XPageWriter
from muninn.scrape.client import IA_CDX_BASE, IA_WEB_BASE, ScrapeClient
from muninn.scrape.domain_policy import DomainPolicy
from muninn.scrape.pipeline import scrape_all
from tests.integration.intake_fakes import fake_client, judgement, write_profile
from tests.integration.intake_seed import Seeded, add_revision, seed_x_post
from tests.integration.test_scrape_history import CDX_EMPTY, CDX_HIT, PAGE, NoWait

T0 = 1_790_000_000
MODEL = "claude-haiku-4-5-20251001"
HIDDEN_HOST = "private.example.com"
POLICY = DomainPolicy(domain_patterns=(HIDDEN_HOST,))
SHORT = "https://short.example.net/abc"
TARGET = f"https://{HIDDEN_HOST}/doc/plan?id=7"
POST_ID = "1900000000000000071"
SECRET_WORDS = "quarterly hiring plan"
HIDDEN_PAGE = f"<html><body><article><p>{('The ' + SECRET_WORDS + ' is here. ') * 20}</p></article></body></html>"


def _seed(conn: sqlite3.Connection):
    post = seed_x_post(conn, POST_ID, f"Worth a read {SHORT} on retrieval eval",
                       captured_at=T0, published_at=T0 - 100)
    conn.execute("UPDATE bookmarks SET title = ? WHERE bookmark_id = ?",
                 (f"@synth_author_a: Worth a read {SHORT} on retrieval eval", post.bookmark_id))
    cur = conn.execute(
        "INSERT INTO bookmarks (source, source_id, captured_at, title, url, domain, source_metadata) "
        "VALUES ('x_link', ?, ?, 'Shortened link', ?, 'short.example.net', ?)",
        (locator_source_id(SHORT), T0, SHORT,
         json.dumps({"kind": "web_page", "item_id": link_item_id(SHORT)})),
    )
    link = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO item_links (parent_bookmark_id, child_bookmark_id, parent_ref, child_ref, "
        "relation, position, locator_resolution, first_seen_at) "
        "VALUES (?, ?, ?, ?, 'links_to', 0, 'unwound', ?)",
        (post.bookmark_id, link, post.item_id, link_item_id(SHORT), T0),
    )
    conn.commit()
    return post, link


def _client(tmp_path) -> ScrapeClient:
    return ScrapeClient(http_cache_dir=tmp_path / "http", scrape_cache_dir=tmp_path / "scrape",
                        rate_limiter=NoWait())


async def _scrape(conn, client, **kw):
    try:
        return await scrape_all(conn, client=client, sources=["x_link"], **kw)
    finally:
        await client.close()


def _visible(conn, bid: int) -> int:
    return conn.execute("SELECT content_visible FROM bookmarks WHERE bookmark_id = ?", (bid,)).fetchone()[0]


def _everything(tmp_path, *dirs) -> str:
    return "\n".join(p.read_text() for d in dirs if d.exists() for p in sorted(d.rglob("*")) if p.is_file())


def _no_robots() -> None:
    respx.get(url__regex=r"^https?://[^/]+/robots\.txt$").mock(return_value=httpx.Response(404))


def test_the_rule() -> None:
    assert row_hidden(POLICY, "x_link", SHORT, [TARGET])
    assert not row_hidden(POLICY, "x_link", SHORT, ["https://www.example.org/landing"])
    wayback = f"{IA_WEB_BASE}/20261001120000id_/{TARGET}"
    assert archived_original(wayback) == TARGET and row_hidden(POLICY, "bookmarks_html", SHORT, [wayback])
    # An X post's or article's stored locator is its own: only fetched rows count.
    assert not row_hidden(POLICY, "x", "https://x.com/a/status/1", [TARGET])


@pytest.mark.asyncio
@respx.mock
async def test_scrape_never_follows_a_redirect_to_a_hidden_host_and_hides_the_row(
    db_path, tmp_path, monkeypatch
) -> None:
    conn = connect(db_path)
    post, link = _seed(conn)
    _no_robots()
    respx.get(IA_CDX_BASE).mock(return_value=httpx.Response(200, text=CDX_EMPTY))
    respx.get(SHORT).mock(return_value=httpx.Response(302, headers={"location": TARGET}))
    target = respx.get(url__startswith=f"https://{HIDDEN_HOST}/").mock(
        return_value=httpx.Response(200, text=HIDDEN_PAGE))

    stats = await _scrape(conn, _client(tmp_path), policy=POLICY)

    # Neither the page nor the hidden host's robots.txt was requested.
    assert target.call_count == 0
    assert stats["hidden_by_policy"] == 1 and _visible(conn, link) == 0
    live = conn.execute("SELECT * FROM scrape_results WHERE bookmark_id = ? AND pass = 'live'",
                        (link,)).fetchone()
    assert (live["scrape_status"], live["http_status"], live["final_url"]) == ("failed", 302, TARGET)
    assert live["content_text"] is None and live["content_html"] is None
    assert conn.execute("SELECT COUNT(*) FROM scrape_history WHERE bookmark_id = ?", (link,)).fetchone()[0] == 0
    # The post that links it is masked, as for any hidden link (policy apply after the run).
    text = conn.execute("SELECT content_text FROM scrape_results WHERE bookmark_id = ? AND pass = 'live'",
                        (post.bookmark_id,)).fetchone()[0]
    assert text == f"Worth a read {HIDDEN_LINK} on retrieval eval"
    title = conn.execute("SELECT title FROM bookmarks WHERE bookmark_id = ?", (post.bookmark_id,)).fetchone()[0]
    assert SHORT not in title

    # A later re-sync of the post does not make the link visible again.
    XPageWriter(conn, run_ulid="01JA0000000000000000000001", policy=POLICY)._apply_policy(link, SHORT)
    assert _visible(conn, link) == 0
    conn.commit()

    # Model, packets, vault and MCP.
    profile = load_interest_profile(write_profile(tmp_path))
    client = fake_client(default=judgement())
    assert assess_suitability(conn, profile, client, model=MODEL, now=T0 + 100).assessed == 1
    sent = "\n".join(c.kwargs["messages"][0]["content"] for c in client.messages.create.call_args_list)
    out = tmp_path / "packets"
    (written,) = export_packets(conn, out, now=T0 + 200).written
    conn.close()
    vault = tmp_path / "vault"
    compile_vault(db_path=db_path, output_dir=vault, personal_dir=None)
    monkeypatch.setenv("MUNINN_VAULT_DIR", str(vault))
    pages = json.loads(tools.list_namespace("bookmarks", db_path=db_path))["pages"]
    served = [json.loads(tools.get_page(p["path"], db_path=db_path)) for p in pages]
    mcp = json.dumps([pages, served, json.loads(tools.get_bookmark(post.bookmark_id, db_path=db_path))])

    assert json.loads(tools.get_bookmark(link, db_path=db_path)) == {"error": f"Bookmark {link} not found"}
    assert [p["bookmark_id"] for p in pages] == [post.bookmark_id]
    packet = json.loads(written.path.read_bytes())
    assert [i["item_id"] for i in packet["items"]] == [post.item_id]
    for blob in (sent, _everything(tmp_path, out, vault), mcp):
        low = blob.lower()
        assert HIDDEN_HOST not in low and "short.example.net" not in low and SECRET_WORDS not in low


@pytest.mark.asyncio
@respx.mock
async def test_a_wayback_snapshot_of_a_hidden_redirect_target_keeps_no_content(fresh_db, tmp_path) -> None:
    conn = fresh_db
    _post, link = _seed(conn)
    _no_robots()
    respx.get(IA_CDX_BASE).mock(return_value=httpx.Response(200, text=CDX_HIT))
    # Wayback answers the snapshot of SHORT with a redirect to its snapshot of TARGET.
    respx.get(f"{IA_WEB_BASE}/20261001120000id_/{SHORT}").mock(return_value=httpx.Response(
        302, headers={"location": f"{IA_WEB_BASE}/20261001120000id_/{TARGET}"}))
    respx.get(f"{IA_WEB_BASE}/20261001120000id_/{TARGET}").mock(
        return_value=httpx.Response(200, text=HIDDEN_PAGE))
    respx.get(SHORT).mock(return_value=httpx.Response(200, text=PAGE))

    await _scrape(conn, _client(tmp_path), policy=POLICY)

    assert _visible(conn, link) == 0
    rows = {r["pass"]: r for r in conn.execute(
        "SELECT * FROM scrape_results WHERE bookmark_id = ?", (link,)).fetchall()}
    assert rows["at_capture"]["scrape_status"] == "failed"
    assert rows["at_capture"]["content_text"] is None and rows["at_capture"]["content_html"] is None
    dump = "\n".join(conn.iterdump())
    assert SECRET_WORDS not in dump


def _scraped_before_the_policy(conn) -> tuple:
    """A link scraped (and its post enriched) while the redirect target's
    host was still visible."""
    post, link = _seed(conn)
    conn.execute(
        "INSERT INTO scrape_results (bookmark_id, pass, fetched_at, final_url, http_status, scrape_status, "
        "extraction_quality, content_text) VALUES (?, 'live', ?, ?, 200, 'ok', 'ok', ?)",
        (link, T0 + 10, TARGET, f"The {SECRET_WORDS} is here."),
    )
    conn.execute("UPDATE bookmarks SET enrichment_source = 'live_fallback' WHERE bookmark_id = ?", (link,))
    conn.execute(
        "INSERT INTO enriched (bookmark_id, summary, tags, enrichment_model, enrichment_prompt_version, "
        "content_hash, enriched_at) VALUES (?, ?, '[]', 'm', 'v', 'h', 1)",
        (link, f"Describes the {SECRET_WORDS}."),
    )
    conn.execute(
        "INSERT INTO fts_bookmarks (rowid, title, summary, content_text, tags) VALUES (?, ?, ?, ?, '[]')",
        (link, "Shortened link", f"Describes the {SECRET_WORDS}.", f"The {SECRET_WORDS} is here."),
    )
    conn.commit()
    return post, link


def test_policy_apply_hides_a_row_whose_stored_fetch_ended_on_a_hidden_host(db_path, tmp_path) -> None:
    conn = connect(db_path)
    post, link = _scraped_before_the_policy(conn)
    child = Seeded(link, link_item_id(SHORT))
    add_revision(conn, child, f"The {SECRET_WORDS} is here.", retrieval_method="live",
                 retrieved_at=T0 + 10, final_locator=TARGET)
    conn.commit()
    profile = load_interest_profile(write_profile(tmp_path))
    ev = [{"item_id": child.item_id, "stance": "supports", "excerpt": f"The {SECRET_WORDS} is here.",
           "note": "n"}]
    assess_suitability(conn, profile, fake_client(default=judgement(evidence=ev)), model=MODEL, now=T0 + 100)
    out = tmp_path / "packets"
    (first,) = export_packets(conn, out, now=T0 + 150).written
    assert SECRET_WORDS in first.path.read_text()  # before the policy: visible

    report = apply_policy(conn, POLICY, now=T0 + 200)

    assert report.newly_hidden == 1 and _visible(conn, link) == 0
    assert HIDDEN_LINK in conn.execute(
        "SELECT content_text FROM scrape_results WHERE bookmark_id = ? AND pass = 'live'",
        (post.bookmark_id,)).fetchone()[0]
    run = export_packets(conn, out, now=T0 + 300)
    assert [r for _, r in run.withdrawn] == ["policy_hidden"]
    conn.close()
    vault = tmp_path / "vault"
    compile_vault(db_path=db_path, output_dir=vault, personal_dir=None)
    hits = json.loads(tools.fts_search("quarterly", db_path=db_path))
    assert hits == []
    assert json.loads(tools.get_bookmark(link, db_path=db_path))["error"]
    assert not first.path.exists()  # withdrawn: replaced by its tombstone
    everything = _everything(tmp_path, out, vault)
    assert SECRET_WORDS not in everything and HIDDEN_HOST not in everything.lower()


def test_browser_reingest_keeps_a_redirect_hidden_row_hidden(fresh_db, tmp_path) -> None:
    from muninn.ingest.pipeline import ingest_html

    html = tmp_path / "bookmarks.html"
    html.write_text(
        "<!DOCTYPE NETSCAPE-Bookmark-file-1>\n<DL><p>\n"
        f'<DT><A HREF="{SHORT}" ADD_DATE="{T0}">Short</A>\n</DL><p>\n', encoding="utf-8")
    (bid,) = ingest_html(html, fresh_db, policy=POLICY).bookmark_ids
    fresh_db.execute(
        "INSERT INTO scrape_results (bookmark_id, pass, fetched_at, final_url, scrape_status) "
        "VALUES (?, 'live', ?, ?, 'failed')", (bid, T0, TARGET))
    fresh_db.commit()
    assert apply_policy(fresh_db, POLICY).newly_hidden == 1
    stats = ingest_html(html, fresh_db, policy=POLICY)
    assert stats.hidden_by_policy == 1 and _visible(fresh_db, bid) == 0
    assert ingest_html(html, fresh_db, policy=DomainPolicy()).hidden_by_policy == 0
    assert _visible(fresh_db, bid) == 1  # the policy no longer hides the target
