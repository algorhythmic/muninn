"""After `muninn policy apply` hides a link, the derived indexes show what
the vault and MCP show.

A visible X post enriched before the hide can carry a summary that quotes
(and paraphrases) the now-hidden page. The keyword index withholds that
summary and masks the locator, as `x_pages.withhold_enrichment` does for
the vault and MCP; the Qdrant point becomes stale and the reconciler
re-upserts it masked.

ALL DATA IS SYNTHETIC (invented IDs, reserved example hosts).
"""

from __future__ import annotations

import json

import pytest

from muninn.consumers.mcp import tools
from muninn.db import connect
from muninn.ingest.policy import apply_policy
from muninn.scrape.domain_policy import DomainPolicy
from tests.integration.test_x_policy import OLD, P2, SECRET_SITE, page, row, write

SUMMARY = "Shares https://secret.example.com/my/case: the paychecks of the team."


def _link_id(conn) -> int:
    return conn.execute(
        "SELECT bookmark_id FROM bookmarks WHERE source = 'x_link' AND domain = 'secret.example.com'"
    ).fetchone()[0]


def _enrich(conn, bookmark_id: int, summary: str, tags: str = '["payroll"]') -> None:
    """An enriched row and its keyword-index entry, as `muninn enrich` writes them."""
    title, text = conn.execute(
        "SELECT b.title, sr.content_text FROM bookmarks b LEFT JOIN scrape_results sr "
        "ON sr.bookmark_id = b.bookmark_id AND sr.pass = 'live' WHERE b.bookmark_id = ?",
        (bookmark_id,)).fetchone()
    conn.execute("UPDATE bookmarks SET enrichment_source = 'live_fallback' WHERE bookmark_id = ?",
                 (bookmark_id,))
    conn.execute(
        "INSERT INTO enriched (bookmark_id, summary, tags, entities, key_quotes, enrichment_model, "
        "enrichment_prompt_version, content_hash, enriched_at) VALUES (?, ?, ?, '[]', NULL, 'm', 'v', 'h', 1)",
        (bookmark_id, summary, tags))
    conn.execute("INSERT INTO fts_bookmarks (rowid, title, summary, content_text, tags) VALUES (?, ?, ?, ?, ?)",
                 (bookmark_id, title or "", summary, text or "", tags))
    conn.commit()


def test_policy_apply_withholds_a_pre_hide_summary_from_the_keyword_index(db_path) -> None:
    conn = connect(db_path)
    write(conn, DomainPolicy.empty(), page([OLD]))
    post, link = row(conn, P2)["bookmark_id"], _link_id(conn)
    conn.execute("INSERT INTO scrape_results (bookmark_id, pass, fetched_at, scrape_status, content_text) "
                 "VALUES (?, 'live', 1, 'ok', 'Case notes on salaries.')", (link,))
    _enrich(conn, post, SUMMARY)
    _enrich(conn, link, "Case notes listing paychecks.", tags='["casework"]')

    def hits(query: str) -> list[int]:
        return sorted(r["bookmark_id"] for r in json.loads(tools.fts_search(query, db_path=db_path)))

    assert hits("paychecks") == sorted([post, link])
    apply_policy(conn, SECRET_SITE)

    fts = conn.execute("SELECT title, summary, content_text, tags FROM fts_bookmarks WHERE rowid = ?",
                       (post,)).fetchone()
    assert tuple(fts) == ("@pub: read [hidden link]", "", "read [hidden link]", '["payroll"]')
    assert conn.execute("SELECT COUNT(*) FROM fts_bookmarks WHERE rowid = ?", (link,)).fetchone()[0] == 0
    # Found by what it still shows (its tags), not by the withheld summary
    # or the masked locator; MCP withholds the same summary.
    assert hits("paychecks") == [] and hits("secret") == [] and hits("payroll") == [post]
    shown = json.loads(tools.get_bookmark(post, db_path=db_path))
    assert shown["summary"] is None and shown["enrichment_withheld"] is True
    assert not apply_policy(conn, SECRET_SITE).fts_refreshed  # converged

    # Visible again: the link's entry comes back, and so does the post's
    # summary (it quotes no hidden locator now); the masked text stays
    # until a re-sync re-reads the post.
    apply_policy(conn, DomainPolicy.empty())
    assert hits("paychecks") == sorted([post, link])
    assert conn.execute("SELECT content_text FROM fts_bookmarks WHERE rowid = ?",
                        (post,)).fetchone()[0] == "read [hidden link]"
    conn.close()


def test_policy_apply_makes_the_vector_point_stale_and_reconcile_masks_it(db_path) -> None:
    qdrant_client = pytest.importorskip("qdrant_client")
    from muninn.vector.embed import embed_document
    from muninn.vector.qdrant import ensure_collection, get_point_payloads, upsert_point
    from muninn.vector.reconcile import point_payload, reconcile

    conn = connect(db_path)
    write(conn, DomainPolicy.empty(), page([OLD]))
    post = row(conn, P2)["bookmark_id"]
    _enrich(conn, post, SUMMARY)
    title = row(conn, P2)["title"]
    client = qdrant_client.QdrantClient(":memory:")
    ensure_collection(client)
    upsert_point(client, post, embed_document(f"{title}\n{SUMMARY}\npayroll"),
                 point_payload(post, title=title, summary=SUMMARY, tags=["payroll"],
                               content_type=None, language=None))
    assert reconcile(db_path, client=client)["stale_in_qdrant"] == 0

    apply_policy(conn, SECRET_SITE)
    conn.close()

    stats = reconcile(db_path, client=client)
    assert stats["stale_in_qdrant"] == 1 and stats["refreshed"] == 1
    payload = get_point_payloads(client)[post]
    assert "secret.example.com" not in json.dumps(payload) and "paychecks" not in json.dumps(payload)
    assert payload["title"] == "@pub: read [hidden link]" and payload["summary"] == ""
    assert reconcile(db_path, client=client)["stale_in_qdrant"] == 0
    client.close()
