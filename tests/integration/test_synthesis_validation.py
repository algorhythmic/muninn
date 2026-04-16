"""Integration tests for the synthesis pipeline.

Covers:
  - JSON Schema validation (era_narrative, deep_pass, ad_hoc_analysis)
  - Verbatim quote substring check (deep_pass)
  - Cross-reference target validation against input materials (deep_pass)
  - Single-retry self-correction loop (success on retry; failure on retry)
  - Cap-hit detection from container output
  - synthesis_runs audit row written for every attempt
  - Canonical-table writes only happen on validation success
  - cross_references.created_by injected as 'deep_pass'
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from muninn.synthesis import (
    run_ad_hoc_analysis,
    run_deep_pass,
    run_era_narrative,
    validate_cross_reference_targets,
    validate_key_quotes,
    validate_output,
)
from muninn.synthesis.container import ContainerResult, detect_cap_hit
from muninn.synthesis.correction import build_correction_instructions


SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schemas" / "json"


# ── Mocks ─────────────────────────────────────────────────────────────────


@dataclass
class MockLauncher:
    """Stand-in ContainerLauncher: returns canned outputs in sequence."""
    outputs: list[dict[str, Any]] = field(default_factory=list)
    cap_hit_on: list[int] = field(default_factory=list)  # 1-indexed attempts
    container_failure_on: list[int] = field(default_factory=list)
    call_count: int = 0
    received_envelopes: list[dict[str, Any]] = field(default_factory=list)

    def launch(self, envelope: dict[str, Any]) -> ContainerResult:
        self.call_count += 1
        self.received_envelopes.append(envelope)
        attempt = envelope.get("attempt", 1)

        if attempt in self.cap_hit_on:
            return ContainerResult(
                container_id=f"mock-{self.call_count}",
                output_json=None,
                raw_stdout="rate limit exceeded for this account",
                raw_stderr="",
                exit_code=0,
                cap_hit=True,
                cap_hit_evidence="rate limit exceeded for this account",
            )

        if attempt in self.container_failure_on:
            return ContainerResult(
                container_id=f"mock-{self.call_count}",
                output_json=None,
                raw_stdout="",
                raw_stderr="container exploded",
                exit_code=1,
            )

        idx = self.call_count - 1
        out = self.outputs[idx] if idx < len(self.outputs) else self.outputs[-1]
        return ContainerResult(
            container_id=f"mock-{self.call_count}",
            output_json=out,
            raw_stdout="",
            raw_stderr="",
            exit_code=0,
        )


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def populated_db(fresh_db):
    """Seed bookmarks/scrape_results so synthesis tasks have inputs."""
    cur = fresh_db.cursor()
    now = int(time.time())
    # Era 'early-web' bookmarks.
    for bm_id, title in [(1, "HTML 4 spec"), (2, "Mosaic browser"), (3, "Yahoo dir")]:
        cur.execute(
            "INSERT INTO bookmarks (bookmark_id, source, source_id, captured_at, "
            "title, url, era_label, domain, enrichment_source) "
            "VALUES (?, 'netscape', ?, ?, ?, 'http://example.com/' || ?, "
            "'early-web', 'example.com', 'at_capture')",
            (bm_id, f"src{bm_id}", now - 86400 * bm_id, title, bm_id),
        )
        cur.execute(
            "INSERT INTO scrape_results (bookmark_id, pass, fetched_at, "
            "scrape_status, content_text) "
            "VALUES (?, 'at_capture', ?, 'ok', ?)",
            (bm_id, now, f"Content for bookmark {bm_id}: verbatim quote from source"),
        )
    # Bookmark 99 lives in another era so it's a candidate cross-ref target
    # only when explicitly passed in.
    cur.execute(
        "INSERT INTO bookmarks (bookmark_id, source, source_id, captured_at, "
        "title, url, era_label, domain, enrichment_source) "
        "VALUES (99, 'netscape', 'src99', ?, 'Other', 'http://x', "
        "'late-web', 'x.com', 'at_capture')",
        (now,),
    )
    fresh_db.commit()
    return fresh_db


# ── Sample valid outputs ──────────────────────────────────────────────────


def _valid_era_output() -> dict:
    return {
        "era_label": "early-web",
        "narrative": "A" * 60,
        "inferred_year": 1995,
        "dominant_topics": ["web", "html"],
        "dominant_domains": ["example.com"],
        "synthesis_metadata": {
            "model": "claude-opus-4-6",
            "prompt_version": "synthesis_v1",
            "input_token_count": 5000,
            "output_token_count": 1200,
            "neighboring_eras_used": [],
        },
    }


def _valid_deep_pass_output(target_id: int = 2) -> dict:
    return {
        "bookmark_id": 1,
        "summary": "S" * 120,
        "tags": ["web", "history"],
        "entities": [{"name": "HTML", "type": "spec"}],
        "content_type": "article",
        "language": "en",
        "word_count": 500,
        "key_quotes": ["verbatim quote from source"],
        "cross_references": [
            {
                "target_bookmark_id": target_id,
                "relationship": "cites",
                "confidence": 0.9,
            }
        ],
        "synthesis_metadata": {
            "model": "claude-opus-4-6",
            "prompt_version": "synthesis_v1",
            "input_token_count": 8000,
            "output_token_count": 2000,
        },
    }


def _valid_adhoc_output() -> dict:
    return {
        "prompt": "what themes",
        "narrative": "N" * 220,
        "key_findings": [
            {"finding": "the early web was personal", "supporting_evidence": ["1"]}
        ],
        "referenced_bookmarks": [1, 2, 3],
        "synthesis_metadata": {
            "model": "claude-opus-4-6",
            "prompt_version": "synthesis_v1",
            "input_token_count": 10000,
            "output_token_count": 3000,
        },
    }


# ── Pure-validation tests ─────────────────────────────────────────────────


class TestSchemaValidation:
    def test_era_valid(self):
        assert validate_output(_valid_era_output(), "era_narrative", SCHEMA_DIR) == []

    def test_era_narrative_too_short(self):
        bad = _valid_era_output()
        bad["narrative"] = "short"
        errs = validate_output(bad, "era_narrative", SCHEMA_DIR)
        assert any("narrative" in e for e in errs)

    def test_era_year_too_low(self):
        bad = _valid_era_output()
        bad["inferred_year"] = 1989
        errs = validate_output(bad, "era_narrative", SCHEMA_DIR)
        assert any("inferred_year" in e for e in errs)

    def test_deep_pass_valid(self):
        assert validate_output(_valid_deep_pass_output(), "deep_pass", SCHEMA_DIR) == []

    def test_deep_pass_summary_too_short(self):
        bad = _valid_deep_pass_output()
        bad["summary"] = "x"
        errs = validate_output(bad, "deep_pass", SCHEMA_DIR)
        assert any("summary" in e for e in errs)

    def test_deep_pass_too_many_quotes(self):
        bad = _valid_deep_pass_output()
        bad["key_quotes"] = [f"q{i}" for i in range(6)]
        errs = validate_output(bad, "deep_pass", SCHEMA_DIR)
        assert any("key_quotes" in e for e in errs)

    def test_adhoc_valid(self):
        assert validate_output(_valid_adhoc_output(), "ad_hoc_analysis", SCHEMA_DIR) == []

    def test_adhoc_narrative_too_short(self):
        bad = _valid_adhoc_output()
        bad["narrative"] = "short"
        errs = validate_output(bad, "ad_hoc_analysis", SCHEMA_DIR)
        assert any("narrative" in e for e in errs)


class TestVerbatimQuoteCheck:
    def test_all_present(self):
        assert validate_key_quotes(["foo", "bar"], "alpha foo beta bar gamma") == []

    def test_some_missing(self):
        missing = validate_key_quotes(["foo", "missing"], "alpha foo beta")
        assert missing == ["missing"]

    def test_whitespace_strict(self):
        # Verbatim means EXACT bytes — extra whitespace counts as different.
        missing = validate_key_quotes(["foo  bar"], "alpha foo bar beta")
        assert missing == ["foo  bar"]


class TestCrossRefTargetValidation:
    def test_all_known(self):
        refs = [
            {"target_bookmark_id": 10, "relationship": "x", "confidence": 0.9},
            {"target_bookmark_id": 20, "relationship": "y", "confidence": 0.5},
        ]
        assert validate_cross_reference_targets(refs, {10, 20, 30}) == []

    def test_unknown_target(self):
        refs = [{"target_bookmark_id": 999, "relationship": "x", "confidence": 0.5}]
        errs = validate_cross_reference_targets(refs, {10, 20})
        assert len(errs) == 1
        assert "999" in errs[0]


class TestCorrectionInstructions:
    def test_includes_all_errors(self):
        text = build_correction_instructions(["a wrong", "b also wrong"])
        assert "a wrong" in text
        assert "b also wrong" in text
        assert "JSON" in text


class TestCapHitDetection:
    def test_rate_limit_in_output(self):
        hit, evidence = detect_cap_hit("some line\nrate limit exceeded\nmore")
        assert hit is True
        assert "rate limit" in (evidence or "").lower()

    def test_5_hour_limit(self):
        hit, _ = detect_cap_hit("you have hit your 5-hour limit")
        assert hit is True

    def test_no_cap_hit(self):
        hit, evidence = detect_cap_hit("normal output here\neverything fine")
        assert hit is False
        assert evidence is None


# ── Orchestration tests (with mocked launcher; real DB) ───────────────────


class TestEraNarrativeOrchestration:
    def test_success_writes_eras_row_and_audit(self, populated_db):
        launcher = MockLauncher(outputs=[_valid_era_output()])
        result = run_era_narrative(
            "early-web", conn=populated_db, launcher=launcher,
        )
        assert result.status == "completed"
        assert launcher.call_count == 1
        assert len(result.attempts) == 1
        assert result.attempts[0].attempt == 1

        cur = populated_db.cursor()
        cur.execute("SELECT narrative, bookmark_count FROM eras WHERE era_label='early-web'")
        row = cur.fetchone()
        assert row is not None
        assert len(row["narrative"]) >= 50
        assert row["bookmark_count"] == 3

        cur.execute(
            "SELECT status, attempt FROM synthesis_runs WHERE task_id=?",
            (result.task_id,),
        )
        runs = cur.fetchall()
        assert len(runs) == 1
        assert runs[0]["status"] == "completed"

    def test_correction_loop_success_on_retry(self, populated_db):
        bad = _valid_era_output()
        bad["narrative"] = "too short"
        launcher = MockLauncher(outputs=[bad, _valid_era_output()])

        result = run_era_narrative(
            "early-web", conn=populated_db, launcher=launcher,
        )

        assert result.status == "completed"
        assert launcher.call_count == 2
        # Second envelope must carry correction_instructions.
        assert launcher.received_envelopes[1]["correction_instructions"] is not None
        assert "narrative" in launcher.received_envelopes[1]["correction_instructions"]

        cur = populated_db.cursor()
        cur.execute(
            "SELECT attempt, status FROM synthesis_runs WHERE task_id=? ORDER BY attempt",
            (result.task_id,),
        )
        runs = cur.fetchall()
        assert [(r["attempt"], r["status"]) for r in runs] == [
            (1, "validation_failed"),
            (2, "completed"),
        ]

    def test_correction_loop_both_fail(self, populated_db):
        bad = _valid_era_output()
        bad["narrative"] = "too short"
        launcher = MockLauncher(outputs=[bad, bad])

        result = run_era_narrative(
            "early-web", conn=populated_db, launcher=launcher,
        )

        assert result.status == "validation_failed"
        assert launcher.call_count == 2  # Single retry, no third attempt.

        cur = populated_db.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM eras WHERE era_label='early-web'")
        # No canonical-table write on double failure.
        assert cur.fetchone()["c"] == 0
        cur.execute(
            "SELECT attempt, status FROM synthesis_runs WHERE task_id=? ORDER BY attempt",
            (result.task_id,),
        )
        runs = cur.fetchall()
        assert [(r["attempt"], r["status"]) for r in runs] == [
            (1, "validation_failed"),
            (2, "validation_failed"),
        ]


class TestDeepPassOrchestration:
    def test_success_updates_enriched_and_writes_cross_refs(self, populated_db):
        launcher = MockLauncher(outputs=[_valid_deep_pass_output(target_id=2)])
        result = run_deep_pass(
            bookmark_id=1, conn=populated_db, launcher=launcher,
        )
        assert result.status == "completed"

        cur = populated_db.cursor()
        cur.execute("SELECT summary, key_quotes, deep_pass_requested FROM enriched WHERE bookmark_id=1")
        row = cur.fetchone()
        assert row is not None
        assert row["deep_pass_requested"] == 1
        assert "verbatim quote from source" in row["key_quotes"]

        cur.execute(
            "SELECT source_bookmark_id, target_bookmark_id, created_by "
            "FROM cross_references WHERE source_bookmark_id=1"
        )
        refs = cur.fetchall()
        assert len(refs) == 1
        assert refs[0]["target_bookmark_id"] == 2
        assert refs[0]["created_by"] == "deep_pass"

    def test_quote_not_verbatim_triggers_correction(self, populated_db):
        bad = _valid_deep_pass_output(target_id=2)
        bad["key_quotes"] = ["this string is not in the source text"]
        good = _valid_deep_pass_output(target_id=2)
        launcher = MockLauncher(outputs=[bad, good])

        result = run_deep_pass(
            bookmark_id=1, conn=populated_db, launcher=launcher,
        )
        assert result.status == "completed"
        assert launcher.call_count == 2
        assert "verbatim" in launcher.received_envelopes[1]["correction_instructions"].lower()

    def test_quote_not_verbatim_both_attempts_fails(self, populated_db):
        bad = _valid_deep_pass_output(target_id=2)
        bad["key_quotes"] = ["fabricated quote not in source"]
        launcher = MockLauncher(outputs=[bad, bad])

        result = run_deep_pass(
            bookmark_id=1, conn=populated_db, launcher=launcher,
        )
        assert result.status == "validation_failed"

        cur = populated_db.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM enriched WHERE bookmark_id=1")
        assert cur.fetchone()["c"] == 0  # No DB write on double failure.

    def test_unknown_cross_ref_target_triggers_correction(self, populated_db):
        # bookmark_id=1 lives in 'early-web'; default candidate set is {2,3}.
        # target_id=999 is in the DB but NOT in candidate set.
        bad = _valid_deep_pass_output(target_id=999)
        good = _valid_deep_pass_output(target_id=2)
        launcher = MockLauncher(outputs=[bad, good])

        result = run_deep_pass(
            bookmark_id=1, conn=populated_db, launcher=launcher,
        )
        assert result.status == "completed"
        assert launcher.call_count == 2

    def test_explicit_candidate_neighbors_allow_target(self, populated_db):
        # Pass 99 explicitly so it's a known cross-ref target.
        launcher = MockLauncher(outputs=[_valid_deep_pass_output(target_id=99)])
        result = run_deep_pass(
            bookmark_id=1,
            conn=populated_db,
            launcher=launcher,
            candidate_neighbor_ids=[2, 3, 99],
        )
        assert result.status == "completed"


class TestAdHocAnalysisOrchestration:
    def test_success_appends_analysis_row(self, populated_db):
        launcher = MockLauncher(outputs=[_valid_adhoc_output()])
        result = run_ad_hoc_analysis(
            "what themes connect these?",
            conn=populated_db,
            launcher=launcher,
            filter_query="era_label='early-web'",
        )
        assert result.status == "completed"

        cur = populated_db.cursor()
        cur.execute("SELECT title, prompt, filter_query, narrative FROM analyses")
        rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0]["prompt"] == "what themes connect these?"
        assert rows[0]["filter_query"] == "era_label='early-web'"
        assert len(rows[0]["narrative"]) >= 200


class TestCapHitOrchestration:
    def test_cap_hit_on_first_attempt(self, populated_db):
        launcher = MockLauncher(
            outputs=[_valid_era_output()],
            cap_hit_on=[1],
        )
        result = run_era_narrative(
            "early-web", conn=populated_db, launcher=launcher,
        )
        assert result.status == "cap_hit"
        assert launcher.call_count == 1  # No retry on cap_hit.

        cur = populated_db.cursor()
        cur.execute(
            "SELECT status FROM synthesis_runs WHERE task_id=?",
            (result.task_id,),
        )
        assert cur.fetchone()["status"] == "cap_hit"


class TestContainerFailureOrchestration:
    def test_container_failure_recorded(self, populated_db):
        launcher = MockLauncher(
            outputs=[_valid_era_output()],
            container_failure_on=[1],
        )
        result = run_era_narrative(
            "early-web", conn=populated_db, launcher=launcher,
        )
        assert result.status == "container_failed"

        cur = populated_db.cursor()
        cur.execute(
            "SELECT status FROM synthesis_runs WHERE task_id=?",
            (result.task_id,),
        )
        assert cur.fetchone()["status"] == "container_failed"


class TestSynthesisRunsAudit:
    def test_run_carries_canonical_task_type(self, populated_db):
        launcher = MockLauncher(outputs=[_valid_era_output()])
        result = run_era_narrative(
            "early-web", conn=populated_db, launcher=launcher,
        )
        cur = populated_db.cursor()
        cur.execute(
            "SELECT task_type FROM synthesis_runs WHERE task_id=?",
            (result.task_id,),
        )
        # MUST be canonical 'era_narrative', not stream-4's 'era'.
        assert cur.fetchone()["task_type"] == "era_narrative"

    def test_token_counts_recorded(self, populated_db):
        launcher = MockLauncher(outputs=[_valid_era_output()])
        result = run_era_narrative(
            "early-web", conn=populated_db, launcher=launcher,
        )
        cur = populated_db.cursor()
        cur.execute(
            "SELECT input_token_count, output_token_count FROM synthesis_runs "
            "WHERE task_id=?",
            (result.task_id,),
        )
        row = cur.fetchone()
        assert row["input_token_count"] == 5000
        assert row["output_token_count"] == 1200
