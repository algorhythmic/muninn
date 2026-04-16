"""Single-retry self-correction loop.

If the first synthesis attempt fails validation (schema, verbatim quote, or
cross-reference target check), we re-run the container ONCE with
`correction_instructions` populated. If the retry also fails, the run is
marked `validation_failed` and no rows are written to the canonical tables —
only the audit row in `synthesis_runs` is recorded.
"""

from __future__ import annotations

from typing import Iterable


def build_correction_instructions(errors: Iterable[str]) -> str:
    """Render validation errors into a prompt-ready correction blurb.

    The container's persona looks for this field in the task-input JSON and
    injects it into its next message.
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
