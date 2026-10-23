"""Muninn Synthesis — era narratives, deep-pass analysis, ad-hoc queries,
and the model tier of vault lint (contradictions and superseded claims
among compiled bookmark pages sharing a tag).

Synthesis is a task/result file contract (ADR-013, docs/synthesis-contract.md):

  - run_era_narrative / run_deep_pass / run_ad_hoc_analysis / run_lint run a task
    in-process; the default runner is the budgeted direct API call
    (muninn.synthesis.api_runner.ApiRunner)
  - emit_*_task writes the same task file for any other harness, and
    ingest_result validates the harness's result file and writes it

Every output is validated against the JSON Schemas in `schemas/json/`,
gets a single self-correction retry on validation failure, and is written
to the canonical tables only when valid.
"""

from muninn.synthesis.orchestrator import (
    TaskResult,
    emit_ad_hoc_analysis_task,
    emit_deep_pass_task,
    emit_era_narrative_task,
    emit_lint_task,
    ingest_result,
    run_ad_hoc_analysis,
    run_deep_pass,
    run_era_narrative,
    run_lint,
)
from muninn.synthesis.validation import (
    validate_cross_reference_targets,
    validate_key_quotes,
    validate_output,
    validate_task_input,
)

__all__ = [
    "TaskResult",
    "emit_ad_hoc_analysis_task",
    "emit_deep_pass_task",
    "emit_era_narrative_task",
    "emit_lint_task",
    "ingest_result",
    "run_ad_hoc_analysis",
    "run_deep_pass",
    "run_era_narrative",
    "run_lint",
    "validate_cross_reference_targets",
    "validate_key_quotes",
    "validate_output",
    "validate_task_input",
]
