"""Regression for RI-01 D6: a deep pass must not clobber the enrichment triple.

Before the fix, ``deep_pass.write_output`` overwrote ``enrichment_model``,
``enrichment_prompt_version`` and ``content_hash`` with the synthesis
run's values, so the next ``muninn enrich`` saw a mismatch and paid to
re-enrich every deep-passed row (and overwrote its deep-pass summary).
"""

from __future__ import annotations

import json
import sqlite3
import time
from unittest.mock import MagicMock, patch

from muninn.config import HAIKU_MODEL, OPUS_MODEL, PER_BOOKMARK_PROMPT_VERSION
from muninn.enrich.haiku import EnrichmentResult
from muninn.enrich.idempotency import compute_content_hash
from muninn.enrich.pipeline import enrich_all
from muninn.synthesis import run_deep_pass
from muninn.synthesis.container import ContainerResult

SOURCE_TEXT = "Long-form article body. A verbatim quote from source lives here."
DEEP_SUMMARY = "D" * 120


class _OneShotLauncher:
    """Stand-in ContainerLauncher returning one valid deep-pass output."""

    def __init__(self, output: dict):
        self._output = output
        self.call_count = 0

    def launch(self, envelope):
        self.call_count += 1
        return ContainerResult(
            container_id="mock",
            output_json=self._output,
            raw_stdout="",
            raw_stderr="",
            exit_code=0,
        )


def _deep_pass_output() -> dict:
    return {
        "bookmark_id": 1,
        "summary": DEEP_SUMMARY,
        "tags": ["deep", "tags"],
        "entities": [{"name": "Thing", "type": "concept"}],
        "content_type": "essay",
        "language": "en",
        "word_count": 11,
        "key_quotes": ["A verbatim quote from source"],
        "cross_references": [],
        "synthesis_metadata": {
            "model": "claude-opus-4-6",
            "prompt_version": "synthesis_v1",
            "input_token_count": 1000,
            "output_token_count": 200,
        },
    }


def _seed(conn: sqlite3.Connection) -> None:
    now = int(time.time())
    conn.execute(
        "INSERT INTO bookmarks (bookmark_id, source, source_id, captured_at, "
        "  title, url, era_label, content_visible, enrichment_source) "
        "VALUES (1, 'netscape', 's1', ?, 'Deep article', 'https://example.com/1', "
        "  'era', 1, 'at_capture')",
        (now - 86400,),
    )
    conn.execute(
        "INSERT INTO scrape_results (bookmark_id, pass, fetched_at, scrape_status, "
        "  extraction_quality, content_text) "
        "VALUES (1, 'at_capture', ?, 'ok', 'ok', ?)",
        (now, SOURCE_TEXT),
    )
    conn.commit()


def _fake_enrich(title, content, client=None, **_kwargs):
    return EnrichmentResult(
        summary=f"Haiku summary of {title}",
        tags=["haiku"],
        entities=["E"],
        content_type="article",
        language="en",
        cache_hit=False,
        input_tokens=10,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        output_tokens=10,
    )


def _triple(conn: sqlite3.Connection) -> tuple:
    return tuple(
        conn.execute(
            "SELECT enrichment_model, enrichment_prompt_version, content_hash, "
            "       enriched_at FROM enriched WHERE bookmark_id = 1"
        ).fetchone()
    )


@patch("muninn.enrich.pipeline.enrich_bookmark")
def test_enrich_after_deep_pass_makes_zero_api_calls(mock_enrich, fresh_db):
    _seed(fresh_db)
    mock_enrich.side_effect = _fake_enrich

    first = enrich_all(fresh_db, client=MagicMock(), qdrant=None, now=1_700_000_000)
    assert first.api_calls == 1
    triple_before = _triple(fresh_db)
    assert triple_before == (
        HAIKU_MODEL,
        PER_BOOKMARK_PROMPT_VERSION,
        compute_content_hash(SOURCE_TEXT),
        1_700_000_000,
    )

    fresh_db.execute("UPDATE enriched SET deep_pass_requested = 1 WHERE bookmark_id = 1")
    fresh_db.commit()
    launcher = _OneShotLauncher(_deep_pass_output())
    result = run_deep_pass(1, conn=fresh_db, launcher=launcher, candidate_neighbor_ids=[])
    assert result.status == "completed"

    # Deep-pass columns written; the enrichment triple and timestamp kept.
    row = fresh_db.execute(
        "SELECT summary, tags, key_quotes, deep_pass_requested FROM enriched "
        "WHERE bookmark_id = 1"
    ).fetchone()
    assert row["summary"] == DEEP_SUMMARY
    assert json.loads(row["tags"]) == ["deep", "tags"]
    assert json.loads(row["key_quotes"]) == ["A verbatim quote from source"]
    assert row["deep_pass_requested"] == 0
    assert _triple(fresh_db) == triple_before

    mock_enrich.reset_mock()
    second = enrich_all(fresh_db, client=MagicMock(), qdrant=None)
    assert second.api_calls == 0
    assert second.skipped_idempotent == 1
    mock_enrich.assert_not_called()
    # The skipped re-run left the deep-pass summary in place.
    assert fresh_db.execute(
        "SELECT summary FROM enriched WHERE bookmark_id = 1"
    ).fetchone()["summary"] == DEEP_SUMMARY


def test_deep_pass_without_prior_enrichment_records_its_own_provenance(fresh_db):
    """No enriched row yet: the INSERT still needs the NOT NULL triple, so it
    records the synthesis run — there is no bulk enrichment to preserve."""
    _seed(fresh_db)
    launcher = _OneShotLauncher(_deep_pass_output())
    result = run_deep_pass(1, conn=fresh_db, launcher=launcher, candidate_neighbor_ids=[])
    assert result.status == "completed"
    model, prompt_version, content_hash, _ = _triple(fresh_db)
    assert model == OPUS_MODEL
    assert prompt_version == "synthesis_v1"
    assert content_hash == compute_content_hash(SOURCE_TEXT)
