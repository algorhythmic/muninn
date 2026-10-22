"""Integration tests for the synthesis pipeline.

Covers:
  - JSON Schema validation (era_narrative, deep_pass, ad_hoc_analysis)
  - Verbatim quote substring check (deep_pass)
  - Cross-reference target validation against input materials (deep_pass)
  - Single-retry self-correction loop (success on retry; failure on retry)
  - Runner failure recorded as container_failed
  - synthesis_runs audit row written for every attempt
  - Canonical-table writes only happen on validation success
  - cross_references.created_by injected as 'deep_pass'
"""

from __future__ import annotations

from pathlib import Path

import pytest

from muninn.synthesis import (
    run_ad_hoc_analysis,
    run_deep_pass,
    run_era_narrative,
    validate_cross_reference_targets,
    validate_key_quotes,
    validate_output,
)
from muninn.synthesis.correction import build_correction_instructions
from tests.integration.synthesis_fakes import (
    MockRunner,
    adhoc_output as _valid_adhoc_output,
    deep_pass_output as _valid_deep_pass_output,
    era_output as _valid_era_output,
    populate,
)


SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schemas" / "json"


@pytest.fixture
def populated_db(fresh_db):
    """Seed bookmarks/scrape_results so synthesis tasks have inputs."""
    return populate(fresh_db)


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


class TestEraNarrativeOrchestration:
    def test_success_writes_eras_row_and_audit(self, populated_db):
        launcher = MockRunner(outputs=[_valid_era_output()])
        result = run_era_narrative(
            "early-web", conn=populated_db, runner=launcher,
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
        launcher = MockRunner(outputs=[bad, _valid_era_output()])

        result = run_era_narrative(
            "early-web", conn=populated_db, runner=launcher,
        )

        assert result.status == "completed"
        assert launcher.call_count == 2
        # Second envelope must carry correction_instructions.
        assert launcher.received_tasks[1]["correction_instructions"] is not None
        assert "narrative" in launcher.received_tasks[1]["correction_instructions"]

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
        launcher = MockRunner(outputs=[bad, bad])

        result = run_era_narrative(
            "early-web", conn=populated_db, runner=launcher,
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
        launcher = MockRunner(outputs=[_valid_deep_pass_output(target_id=2)])
        result = run_deep_pass(
            bookmark_id=1, conn=populated_db, runner=launcher,
        )
        assert result.status == "completed"

        cur = populated_db.cursor()
        cur.execute("SELECT summary, key_quotes, deep_pass_requested FROM enriched WHERE bookmark_id=1")
        row = cur.fetchone()
        assert row is not None
        # A served request is cleared so `synthesize deep-pass --pending` drains.
        assert row["deep_pass_requested"] == 0
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
        launcher = MockRunner(outputs=[bad, good])

        result = run_deep_pass(
            bookmark_id=1, conn=populated_db, runner=launcher,
        )
        assert result.status == "completed"
        assert launcher.call_count == 2
        assert "verbatim" in launcher.received_tasks[1]["correction_instructions"].lower()

    def test_quote_not_verbatim_both_attempts_fails(self, populated_db):
        bad = _valid_deep_pass_output(target_id=2)
        bad["key_quotes"] = ["fabricated quote not in source"]
        launcher = MockRunner(outputs=[bad, bad])

        result = run_deep_pass(
            bookmark_id=1, conn=populated_db, runner=launcher,
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
        launcher = MockRunner(outputs=[bad, good])

        result = run_deep_pass(
            bookmark_id=1, conn=populated_db, runner=launcher,
        )
        assert result.status == "completed"
        assert launcher.call_count == 2

    def test_explicit_candidate_neighbors_allow_target(self, populated_db):
        # Pass 99 explicitly so it's a known cross-ref target.
        launcher = MockRunner(outputs=[_valid_deep_pass_output(target_id=99)])
        result = run_deep_pass(
            bookmark_id=1,
            conn=populated_db,
            runner=launcher,
            candidate_neighbor_ids=[2, 3, 99],
        )
        assert result.status == "completed"


class TestAdHocAnalysisOrchestration:
    def test_success_appends_analysis_row(self, populated_db):
        launcher = MockRunner(outputs=[_valid_adhoc_output()])
        result = run_ad_hoc_analysis(
            "what themes connect these?",
            conn=populated_db,
            runner=launcher,
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


class TestRunnerFailureOrchestration:
    def test_runner_failure_recorded(self, populated_db):
        # A runner that produced no output is recorded under the legacy
        # status name 'container_failed'; no retry, no canonical write.
        launcher = MockRunner(
            outputs=[_valid_era_output()],
            failure_on=[1],
        )
        result = run_era_narrative(
            "early-web", conn=populated_db, runner=launcher,
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
        launcher = MockRunner(outputs=[_valid_era_output()])
        result = run_era_narrative(
            "early-web", conn=populated_db, runner=launcher,
        )
        cur = populated_db.cursor()
        cur.execute(
            "SELECT task_type FROM synthesis_runs WHERE task_id=?",
            (result.task_id,),
        )
        # MUST be canonical 'era_narrative', not stream-4's 'era'.
        assert cur.fetchone()["task_type"] == "era_narrative"

    def test_token_counts_recorded(self, populated_db):
        launcher = MockRunner(outputs=[_valid_era_output()])
        result = run_era_narrative(
            "early-web", conn=populated_db, runner=launcher,
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
