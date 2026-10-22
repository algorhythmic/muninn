"""Regression tests for the 2026-10-21 review of the Research Intake lane:
current revisions, prompt versions, hidden-locator masking, the assessment
context digest, source deletion, duplicate items and the packet dry run.

ALL DATA IS SYNTHETIC (invented IDs, reserved example hosts). The model
client is a MagicMock (tests/integration/intake_fakes.py)."""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from muninn.consumers.cli.main import cli
from muninn.intake_records import current_revision_row
from muninn.revisions import record_revision

T0 = 1_790_000_000
MODEL = "claude-haiku-4-5-20251001"


# ── Finding 8: a live capture identical to an earlier archive capture ─


def test_live_capture_matching_an_earlier_archive_revision_is_current(fresh_db: sqlite3.Connection) -> None:
    conn = fresh_db
    conn.execute(
        "INSERT INTO bookmarks (bookmark_id, source, source_id, captured_at, title, url, content_visible, "
        "enrichment_source) VALUES (1, 'x_link', 'abc', ?, 't', 'https://example.com/p', 1, 'at_capture')",
        (T0,),
    )
    ref = "web:" + "a" * 64
    # Run 1: the live fetch failed (no revision), the archive returned text A.
    archive = record_revision(
        conn, bookmark_id=1, item_ref=ref, text="Hello world", coverage="full", retrieval_method="archive",
        scrape_pass="at_capture", extractor_name="e", extractor_version="1", retrieved_at=T0 + 100,
        archive_snapshot_at=T0 - 1000,
    )
    assert current_revision_row(conn, ref, 1) is None  # only a historical capture so far
    # Run 2: the live fetch returns the identical text: the key adds no row.
    conn.execute(
        "INSERT INTO scrape_results (bookmark_id, pass, fetched_at, scrape_status, content_text) "
        "VALUES (1, 'live', ?, 'ok', 'Hello world')",
        (T0 + 200,),
    )
    assert record_revision(
        conn, bookmark_id=1, item_ref=ref, text="Hello world", coverage="full", retrieval_method="live",
        scrape_pass="live", extractor_name="e", extractor_version="1", retrieved_at=T0 + 200,
    ) is None
    current = current_revision_row(conn, ref, 1)
    assert current is not None and current["revision_ulid"] == archive

    # A live row with no readable text never promotes an archive revision.
    conn.execute("UPDATE scrape_results SET content_text = NULL, scrape_status = 'failed' WHERE bookmark_id = 1")
    assert current_revision_row(conn, ref, 1) is None


# ── Finding 11: --prompt-version must belong to --task ───────────────


@pytest.mark.parametrize(
    ("args", "version"),
    [([], "suitability_v1"), (["--task", "suitability"], "per_bookmark_v1"), ([], "per_bookmark")],
)
def test_cli_prompt_version_must_match_the_task(db_path, tmp_path, args, version) -> None:
    with patch("muninn.enrich.pipeline.enrich_all") as enrich_all, \
         patch("muninn.enrich.suitability.assess_suitability") as assess:
        result = CliRunner().invoke(
            cli, ["--db", str(db_path), "enrich", *args, "--dry-run", "--prompt-version", version],
        )
    assert result.exit_code == 2 and "is not a" in result.output, result.output
    enrich_all.assert_not_called()
    assess.assert_not_called()


def test_library_entry_points_refuse_another_tasks_prompt(fresh_db, tmp_path) -> None:
    from muninn.enrich.interest_profile import load_interest_profile
    from muninn.enrich.pipeline import enrich_all
    from muninn.enrich.suitability import SuitabilityError, assess_suitability
    from tests.integration.intake_fakes import write_profile

    client = MagicMock()
    with pytest.raises(ValueError, match="not a per_bookmark prompt"):
        enrich_all(fresh_db, client=client, qdrant=None, prompt_version="suitability_v1")
    profile = load_interest_profile(write_profile(tmp_path))
    with pytest.raises(SuitabilityError, match="not a suitability prompt"):
        assess_suitability(fresh_db, profile, client, model=MODEL, prompt_version="per_bookmark_v1")
    client.messages.create.assert_not_called()
