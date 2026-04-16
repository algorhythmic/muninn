"""JSON Schema validation + verbatim quote / cross-reference target checks.

Inputs come from `/workspace/output/<task-id>.json` produced by the synthesis
container. Errors are returned as a list of human-readable strings; an empty
list means valid.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

import jsonschema


# Schema directory — baked into container at /workspace/schemas/, also
# checked into the repo at schemas/json/ for host-side validation.
SCHEMA_DIR = Path(__file__).resolve().parents[3] / "schemas" / "json"


# Map canonical task_type -> schema filename. We use the canonical names
# from schema.sql (era_narrative, deep_pass, ad_hoc_analysis).
SCHEMA_MAP: dict[str, str] = {
    "era_narrative": "era-narrative.schema.json",
    "deep_pass": "deep-pass.schema.json",
    "ad_hoc_analysis": "ad-hoc-analysis.schema.json",
    "task_input": "task-input.schema.json",
}


def load_schema(task_type: str, schema_dir: Path | None = None) -> dict[str, Any]:
    """Load a JSON Schema by canonical task_type."""
    if schema_dir is None:
        schema_dir = SCHEMA_DIR
    schema_file = SCHEMA_MAP.get(task_type)
    if schema_file is None:
        raise ValueError(
            f"Unknown task_type {task_type!r}. Expected one of {sorted(SCHEMA_MAP)}"
        )
    with (schema_dir / schema_file).open() as fh:
        return json.load(fh)


def validate_output(
    output: dict[str, Any],
    task_type: str,
    schema_dir: Path | None = None,
) -> list[str]:
    """Validate synthesis output against its JSON Schema. Returns errors."""
    schema = load_schema(task_type, schema_dir)
    validator = jsonschema.Draft7Validator(schema)
    errors = sorted(validator.iter_errors(output), key=lambda e: list(e.path))
    return [_format_error(e) for e in errors]


def validate_task_input(
    task_input: dict[str, Any],
    schema_dir: Path | None = None,
) -> list[str]:
    """Validate the envelope JSON we hand to the container."""
    return validate_output(task_input, "task_input", schema_dir)


def validate_key_quotes(quotes: Iterable[str], source_text: str) -> list[str]:
    """Return any quotes that are NOT verbatim substrings of source_text.

    Whitespace is preserved exactly — synthesis MUST emit the bytes from
    the canonical scrape pass, not paraphrase.
    """
    return [q for q in quotes if q not in source_text]


def validate_cross_reference_targets(
    cross_references: list[dict[str, Any]],
    known_bookmark_ids: set[int],
) -> list[str]:
    """Verify every target_bookmark_id in cross_references appears in the
    bookmarks the container was handed. Prevents synthesis from inventing IDs
    that would FK-violate on insert."""
    errors: list[str] = []
    for i, ref in enumerate(cross_references):
        target_id = ref.get("target_bookmark_id")
        if target_id is None:
            continue
        if target_id not in known_bookmark_ids:
            errors.append(
                f"cross_references[{i}].target_bookmark_id: {target_id} "
                f"not found in input materials"
            )
    return errors


def _format_error(error: jsonschema.ValidationError) -> str:
    path = ".".join(str(p) for p in error.absolute_path) if error.absolute_path else "(root)"
    return f"{path}: {error.message}"
