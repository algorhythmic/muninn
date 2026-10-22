"""Single-retry self-correction loop.

If the first synthesis attempt fails validation (schema, verbatim quote, or
cross-reference target check), the task is run ONCE more as attempt 2 with
`correction_instructions` populated: by the API runner directly, or as a
retry task file from `synthesize ingest-result --retry-dir`. If the retry
also fails, the run is marked `validation_failed` and no rows are written
to the canonical tables — only the audit row in `synthesis_runs`.
"""

from __future__ import annotations

from typing import Iterable


def build_correction_instructions(errors: Iterable[str]) -> str:
    """Render validation errors into a prompt-ready correction blurb.

    The synthesis instructions tell the model to fix every error listed in
    the task file's `correction_instructions`.
    """
    lines = ["Your previous output had validation errors. Please fix them:"]
    for i, error in enumerate(errors, 1):
        lines.append(f"  {i}. {error}")
    lines.append("")
    lines.append(
        "Re-emit the corrected JSON only. No prose, no markdown fences, no "
        "explanations outside the JSON object."
    )
    return "\n".join(lines)
