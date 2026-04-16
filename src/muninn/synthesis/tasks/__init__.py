"""Per-task input preparation and DB write handlers.

Each task module exposes:
  - prepare_input(conn, **kwargs) -> dict     # builds the materials envelope
  - write_output(conn, output, run, **kwargs) -> None  # persists synth output

The orchestrator dispatches to these based on task_type.
"""

from muninn.synthesis.tasks import era_narrative, deep_pass, ad_hoc_analysis

__all__ = ["era_narrative", "deep_pass", "ad_hoc_analysis"]
