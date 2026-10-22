"""Regression tests for the 2026-10-21 review of the Research Intake lane:
current revisions, prompt versions, hidden-locator masking, the assessment
context digest, source deletion, duplicate items and the packet dry run.

ALL DATA IS SYNTHETIC (invented IDs, reserved example hosts). The model
client is a MagicMock (tests/integration/intake_fakes.py)."""

from __future__ import annotations

import json
import sqlite3
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from muninn.consumers.cli.main import cli
from muninn.consumers.vault import x_pages
from muninn.intake_records import current_revision_row, mask_title, mentions_locator
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


# ── Finding 10: masking ignores case and scheme; withheld enrichment ──

REPO_LOCATOR = "https://github.com/mycorp/secret-repo"  # stored lowercase root


@pytest.mark.parametrize(
    "text",
    [
        "The author shares https://github.com/MyCorp/Secret-Repo, an internal tool.",
        "The author shares github.com/MyCorp/Secret-Repo, an internal tool.",
        "The author shares https://www.github.com/mycorp/secret-repo, an internal tool.",
        "The author shares https://github.com/MyCorp/Secret-Repo.git, an internal tool.",
        "The author shares HTTP://GITHUB.COM/MYCORP/SECRET-REPO/, an internal tool.",
    ],
)
def test_mask_hidden_links_ignores_case_scheme_and_www(text) -> None:
    masked = x_pages.mask_hidden_links(text, {REPO_LOCATOR})
    assert masked == f"The author shares {x_pages.HIDDEN_LINK_TEXT}, an internal tool."
    assert mentions_locator(text, {REPO_LOCATOR}) and not mentions_locator(masked, {REPO_LOCATOR})


def test_mask_does_not_touch_other_links() -> None:
    for text in ("https://github.com/mycorp/secret-repo-two", "https://notgithub.com/mycorp/secret-repo",
                 "https://github.com/other/secret-repo"):
        assert x_pages.mask_hidden_links(text, {REPO_LOCATOR}) == text
    assert mask_title("@a: see github.com/MyCorp/Sec…", {REPO_LOCATOR}) == "@a: see [hidden link]"


def _x_vault_with_stale_enrichment(db_path):
    from muninn.db import connect
    from tests import x_corpus as xc

    conn = connect(db_path)
    corpus = xc.seed_x_corpus(conn, hidden_at_ingest=False)
    secret = xc.HIDDEN_URL.replace("intranet.example", "Intranet.Example")  # the summary's own case
    conn.execute(
        "INSERT INTO enriched (bookmark_id, summary, tags, entities, key_quotes, content_type, language, "
        "enrichment_model, enrichment_prompt_version, content_hash, enriched_at) "
        "VALUES (?, ?, '[\"retrieval\"]', ?, ?, 'post', 'en', 'm', 'v', 'h', 1)",
        (corpus.post, f"Shares a draft plan at {secret}: the private roadmap covers Q3 hiring.",
         json.dumps(["Private roadmap"]), json.dumps([f"Draft plan: {secret}"])),
    )
    conn.commit()
    conn.close()
    return corpus, secret


def test_vault_withholds_enrichment_that_quotes_a_hidden_link(db_path, tmp_path) -> None:
    from muninn.consumers.vault.compiler import compile_vault

    _x_vault_with_stale_enrichment(db_path)
    out = tmp_path / "compiled"
    compile_vault(db_path=db_path, output_dir=out, personal_dir=None)
    everything = "\n".join(p.read_text() for p in (out / "wiki" / "bookmarks").glob("*.md"))
    assert "intranet.example" not in everything.lower()
    assert "Q3 hiring" not in everything and "Private roadmap" not in everything
    assert x_pages.ENRICHMENT_WITHHELD in everything


def test_withhold_enrichment_keeps_clean_output() -> None:
    urls = {REPO_LOCATOR}
    assert x_pages.withhold_enrichment(urls, "A toolkit.", ["q"], '["e"]') == ("A toolkit.", ["q"], '["e"]', False)
    assert x_pages.withhold_enrichment(urls, "A toolkit.", '["see github.com/MyCorp/secret-repo"]', '["e"]') == (
        None, "[]", "[]", True)


def test_reconcile_reindexes_a_point_that_carries_a_hidden_locator(db_path, tmp_path) -> None:
    qdrant_client = pytest.importorskip("qdrant_client")
    from muninn.vector.qdrant import ensure_collection, get_point_payloads, upsert_point
    from muninn.vector.embed import embed_document
    from muninn.vector.reconcile import point_payload, reconcile

    corpus, secret = _x_vault_with_stale_enrichment(db_path)
    from muninn.db import connect

    conn = connect(db_path)
    title = conn.execute("SELECT title FROM bookmarks WHERE bookmark_id = ?", (corpus.post,)).fetchone()[0]
    summary = conn.execute("SELECT summary FROM enriched WHERE bookmark_id = ?", (corpus.post,)).fetchone()[0]
    conn.close()
    client = qdrant_client.QdrantClient(":memory:")
    ensure_collection(client)
    # The point the enrich pipeline wrote before the link was hidden.
    upsert_point(client, corpus.post, embed_document(f"{title}\n{summary}\nretrieval"),
                 point_payload(corpus.post, title=title, summary=summary, tags=["retrieval"],
                               content_type="post", language="en"))
    stats = reconcile(db_path, client=client)
    assert stats["stale_in_qdrant"] == 1 and stats["refreshed"] == 1
    payload = get_point_payloads(client)[corpus.post]
    assert "intranet.example" not in json.dumps(payload).lower() and "Q3 hiring" not in json.dumps(payload)
    assert payload["summary"] == "" and payload["tags"] == ["retrieval"]
    assert reconcile(db_path, client=client)["stale_in_qdrant"] == 0  # converged
    client.close()
