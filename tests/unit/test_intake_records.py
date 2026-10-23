"""The one row-to-record mapping (``muninn.intake_records``) and its
optional-key convention: required keys are always present (null where the
schema allows), optional keys are omitted when there is no value. All rows
are synthetic plain dicts."""

from __future__ import annotations

import json

import pytest

from muninn.consumers.packets import content_digest, link_item_id, new_ulid
from muninn.consumers.packets.schemas import schema_errors
from muninn.intake_records import (
    bookmark_observation_record,
    content_revision_record,
    export_item_id,
    item_link_record,
    source_item_record,
    usage_record,
)

POST = "1900000000000000301"
RUN = new_ulid(1_790_000_000_000, bytes(10))


def _post_row(**meta) -> dict:
    metadata = {"author": {"id": "1900000000000000901", "username": "synth_a", "name": " Synthetic A "},
                "published_at": "2026-09-30T14:05:00Z", "lang": "en", "media_keys": [],
                "is_long_post": False, "bookmarked": True, "first_sync_run_id": RUN,
                "last_observed_at": 1_790_000_100, **meta}
    return {"bookmark_id": 1, "source": "x", "source_id": POST, "url": f"https://x.com/synth_a/status/{POST}",
            "title": "@synth_a: Synthetic post", "captured_at": 1_790_000_000,
            "source_metadata": json.dumps(metadata), "redacted_param_names": None, "path_redacted": 0}


def test_x_post_item_and_observation() -> None:
    item = source_item_record(_post_row())
    assert schema_errors("source-item", item) == []
    assert item["published_at"] == "2026-09-30T14:05:00Z"  # ISO accepted as well as epoch
    assert item["author"] == {"platform_id": "1900000000000000901", "handle": "synth_a", "display_name": "Synthetic A"}
    assert item["x_post"] == {"is_long_post": False, "has_media": False}
    obs = bookmark_observation_record(_post_row())
    assert schema_errors("bookmark-observation", obs) == []
    assert obs["sync_run_id"] == RUN and obs["last_observed_at"] == "2026-09-21T14:15:00Z"


def test_author_null_is_a_stated_fact_and_unbookmarked_posts_have_no_observation() -> None:
    row = _post_row(author=None, bookmarked=False)
    item = source_item_record(row)
    assert item["author"] is None and schema_errors("source-item", item) == []
    assert bookmark_observation_record(row) is None


def test_optional_keys_are_omitted_not_null() -> None:
    link = item_link_record({"parent_ref": f"x:post:{POST}", "child_ref": "web:" + "a" * 64, "relation": "links_to",
                             "position": 0, "locator_resolution": "short", "entity_status": None,
                             "first_seen_at": 1_790_000_000})
    assert "entity_status" not in link and schema_errors("item-link", link) == []
    quote = item_link_record({"parent_ref": f"x:post:{POST}", "child_ref": "x:post:1900000000000000302",
                              "relation": "quotes", "position": 0, "locator_resolution": None,
                              "entity_status": None, "first_seen_at": 1_790_000_000})
    assert quote["locator_resolution"] == "api_reference" and schema_errors("item-link", quote) == []

    text = "Synthetic post text."
    rev = content_revision_record({
        "revision_ulid": RUN, "item_ref": f"x:post:{POST}", "content_sha256": content_digest(text),
        "normalization": "muninn-text/1", "char_count": len(text), "content_text": text, "coverage": "full",
        "retrieval_method": "x_api", "scrape_pass": None, "extractor_name": "muninn.ingest.x",
        "extractor_version": "0.1.0", "http_status": None, "final_locator": None, "archive_snapshot_at": None,
        "published_at": None, "retrieved_at": 1_790_000_000, "observed_at": None, "withdrawn_at": None,
    }, include_text=True)
    assert not {"http_status", "final_locator", "scrape_pass", "archive_snapshot_at"} & set(rev)
    assert rev["published_at"] is None and rev["text"] == text  # required: null; text verified
    assert schema_errors("content-revision", rev) == []


def test_text_is_never_exported_when_it_does_not_match_its_digest() -> None:
    rev = content_revision_record({
        "revision_ulid": RUN, "item_ref": "web:" + "b" * 64, "content_sha256": content_digest("original"),
        "normalization": "muninn-text/1", "char_count": 8, "content_text": "tampered", "coverage": "full",
        "retrieval_method": "live", "scrape_pass": "live", "extractor_name": "e", "extractor_version": "1",
        "retrieved_at": 1_790_000_000,
    }, include_text=True)
    assert "text" not in rev


def test_browser_bookmark_identity_and_observation() -> None:
    url = "https://github.com/Synthetic-Org/Synthetic-Repo"
    row = {"bookmark_id": 9, "source": "bookmarks_html", "source_id": "h9", "url": url, "title": " Repo ",
           "captured_at": 1_551_734_100, "source_metadata": None, "redacted_param_names": "[]", "path_redacted": 0}
    item = source_item_record(row)
    assert item["item_id"] == link_item_id(url) == "github:repo:synthetic-org/synthetic-repo"
    assert item["title"] == "Repo" and item["repository"] == {"owner": "synthetic-org", "name": "synthetic-repo"}
    obs = bookmark_observation_record(row)
    assert obs["observed_at_basis"] == "source_reported" and schema_errors("bookmark-observation", obs) == []
    with pytest.raises(ValueError):
        export_item_id({**row, "url": None})


def test_usage_record_reads_joined_ulids() -> None:
    row = {"usage_ulid": RUN, "kind": "model", "provider": "anthropic", "operation": "enrich:suitability",
           "model": "synthetic-model", "request_count": 1, "resource_count": None, "input_token_count": 10,
           "output_token_count": 2, "cache_read_token_count": None, "cost_micro_usd": 5, "cost_basis": "estimated",
           "occurred_at": 1_790_000_000, "assessment_ulid": RUN, "run_ulid": None}
    rec = usage_record(row)
    assert rec["assessment_id"] == RUN and "sync_run_id" not in rec and "cache_read_tokens" not in rec
    assert schema_errors("usage-record", rec) == []
