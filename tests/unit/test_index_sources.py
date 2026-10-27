"""Synthetic captured-source projection and hydration guards."""

import json

import pytest

from muninn.consumers.packets.canonical import content_digest
from muninn.index.sources import iter_captured, resolve_captured
from muninn.revisions import record_revision


def bookmark(conn, *, source="netscape", visible=1, selected=None):
    return conn.execute(
        "INSERT INTO bookmarks(source,source_id,captured_at,title,url,content_visible,enrichment_source) "
        "VALUES (?, ?, 100, 'Synthetic title', 'https://example.com/synthetic', ?, ?)",
        (source, str(conn.execute("SELECT count(*) FROM bookmarks").fetchone()[0] + 1), visible, selected),
    ).lastrowid


def scrape(conn, bid, text, *, pass_="live", status="ok"):
    return conn.execute(
        "INSERT INTO scrape_results(bookmark_id,pass,fetched_at,scrape_status,content_text,actual_snapshot_at) "
        "VALUES (?, ?, 200, ?, ?, ?)",
        (bid, pass_, status, text, 50 if pass_ != "live" else None),
    ).lastrowid


def test_selected_capture_is_exact_and_not_concatenated(fresh_db):
    bid = bookmark(fresh_db, selected="at_capture")
    scrape(fresh_db, bid, "Current live body")
    sid = scrape(fresh_db, bid, "  Archived body\r\n", pass_="at_capture")
    source = resolve_captured(fresh_db, "synthetic-corpus", bid)
    assert source.body.text == "Archived body"
    assert source.body.source_reference == f"scrape_results:{sid}"
    assert source.body.scrape_pass == "at_capture"
    assert source.body.freshness == "historical"
    assert source.body.archive_snapshot_at == 50
    assert source.body.revision_ulid is None  # legacy local record, no invented ULID
    assert source.body.source_digest == content_digest("  Archived body\r\n")
    assert source.captured_at == 100
    assert source.body.retrieved_at == 200
    assert source.body.published_at is None


def test_capture_with_matching_revision_keeps_publication_separate(fresh_db):
    bid = bookmark(fresh_db)
    scrape(fresh_db, bid, "Synthetic current body")
    revision = record_revision(
        fresh_db, bookmark_id=bid, item_ref="web:" + "a" * 64,
        text="Synthetic current body", coverage="full", retrieval_method="live",
        scrape_pass="live", extractor_name="synthetic", extractor_version="1",
        retrieved_at=200, published_at=10,
    )
    body = resolve_captured(fresh_db, "test", bid).body
    assert body.revision_ulid == revision
    assert body.published_at == 10
    assert body.retrieved_at == 200


def test_missing_selected_pass_falls_back_to_live(fresh_db):
    bid = bookmark(fresh_db, selected="recent_archive")
    scrape(fresh_db, bid, "Live only")
    assert resolve_captured(fresh_db, "test", bid).body.scrape_pass == "live"


def test_x_post_always_uses_live_body(fresh_db):
    bid = bookmark(fresh_db, source="x", selected="at_capture")
    scrape(fresh_db, bid, "Live post")
    scrape(fresh_db, bid, "Historical post", pass_="at_capture")
    assert resolve_captured(fresh_db, "test", bid).body.text == "Live post"


@pytest.mark.parametrize("status,text,omission", [
    ("failed", "Stale retained text", "body_unavailable:failed"),
    ("ok", "", "body_empty"),
])
def test_unavailable_body_retains_metadata_with_omission(fresh_db, status, text, omission):
    bid = bookmark(fresh_db)
    scrape(fresh_db, bid, text, status=status)
    source = resolve_captured(fresh_db, "test", bid)
    assert source.title == "Synthetic title"
    assert source.body is None
    assert source.omissions == (omission,)


def test_hydration_refuses_changed_missing_or_hidden_sources(fresh_db):
    bid = bookmark(fresh_db)
    scrape(fresh_db, bid, "First body")
    original = resolve_captured(fresh_db, "test", bid)
    assert resolve_captured(fresh_db, "test", bid, expected_revision=original.projection_revision) == original
    fresh_db.execute("UPDATE scrape_results SET content_text = 'Changed body' WHERE bookmark_id = ?", (bid,))
    assert resolve_captured(fresh_db, "test", bid, expected_revision=original.projection_revision) is None
    fresh_db.execute("UPDATE bookmarks SET content_visible = 0 WHERE bookmark_id = ?", (bid,))
    assert resolve_captured(fresh_db, "test", bid) is None
    assert resolve_captured(fresh_db, "test", 9999) is None


def test_complete_inventory_is_visible_ordered_and_corpus_bound(fresh_db):
    visible = [bookmark(fresh_db), bookmark(fresh_db)]
    bookmark(fresh_db, visible=0)
    results = list(iter_captured(fresh_db, "corpus-a"))
    assert [s.bookmark_id for s in results] == visible
    assert all(s.omissions == ("body_missing",) for s in results)
    other = resolve_captured(fresh_db, "corpus-b", visible[0])
    assert results[0].document_id != other.document_id
    assert results[0].projection_revision != other.projection_revision


def test_hidden_link_masks_original_body_and_withholds_derived_enrichment(fresh_db):
    parent = bookmark(fresh_db, source="x")
    child = bookmark(fresh_db, visible=0)
    hidden_url = "https://example.com/private-child"
    fresh_db.execute("UPDATE bookmarks SET url = ? WHERE bookmark_id = ?", (hidden_url, child))
    fresh_db.execute(
        "INSERT INTO item_links(parent_bookmark_id,child_bookmark_id,parent_ref,child_ref,relation,position,locator_resolution,first_seen_at) "
        "VALUES (?, ?, 'x:post:1', 'web:synthetic', 'links_to', 0, 'unwound', 100)", (parent, child),
    )
    raw = "Original post cites " + hidden_url
    scrape(fresh_db, parent, raw)
    fresh_db.execute(
        "INSERT INTO enriched(bookmark_id,summary,tags,key_quotes,enrichment_model,enrichment_prompt_version,content_hash,enriched_at) "
        "VALUES (?, 'Paraphrase without locator', ?, ?, 'fake', '1', 'fake', 200)",
        (parent, json.dumps(["synthetic"]), json.dumps([hidden_url])),
    )
    source = resolve_captured(fresh_db, "test", parent)
    assert source.summary is None  # key quote invalidates the entire derived output
    assert source.tags == ()
    assert "enrichment_withheld" in source.omissions
    assert hidden_url not in source.body.text
    assert "[hidden link]" in source.body.text
    assert source.body.source_digest == content_digest(raw)
    assert source.body.text_digest != source.body.source_digest


def test_metadata_change_invalidates_exact_projection(fresh_db):
    bid = bookmark(fresh_db)
    before = resolve_captured(fresh_db, "test", bid)
    fresh_db.execute("UPDATE bookmarks SET title = 'Changed metadata' WHERE bookmark_id = ?", (bid,))
    assert resolve_captured(fresh_db, "test", bid, expected_revision=before.projection_revision) is None
