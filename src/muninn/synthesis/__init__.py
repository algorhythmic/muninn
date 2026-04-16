"""Muninn Synthesis — era narratives, deep-pass analysis, ad-hoc queries.

Public API:
  - run_era_narrative(era_label, ...)
  - run_deep_pass(bookmark_id, ...)
  - run_ad_hoc_analysis(prompt, ...)

All entry points launch the synthesis container (subscription-mode Claude
Max via the saga-claude-credentials volume), validate output against the
JSON Schemas in `schemas/json/`, run a single self-correction retry on
validation failure, and write to canonical tables on success.

The container is invoked via `claude --dangerously-skip-permissions`,
NEVER `claude -p` (which silently forces API billing — see
docs/SAGA_ARCHITECTURE.MD line 141).
"""

from muninn.synthesis.orchestrator import (
    TaskResult,
    run_ad_hoc_analysis,
    run_deep_pass,
    run_era_narrative,
)
from muninn.synthesis.validation import (
    validate_cross_reference_targets,
    validate_key_quotes,
    validate_output,
    validate_task_input,
)

__all__ = [
    "TaskResult",
    "run_ad_hoc_analysis",
    "run_deep_pass",
    "run_era_narrative",
    "validate_cross_reference_targets",
    "validate_key_quotes",
    "validate_output",
    "validate_task_input",
]
