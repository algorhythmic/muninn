"""The synthesis task/result file contract (ADR-013, docs/synthesis-contract.md).

Muninn builds a **task file**: one self-contained JSON object carrying the
instructions, the materials and the result schema, validated against
``schemas/json/task-input.schema.json``. Any harness runs it and returns a
**result file** (``schemas/json/synthesis-result.schema.json``) naming the
task by ``task_id``, ``attempt`` and ``task_digest``. The default API runner
consumes the same task object in-process.

``task_digest`` is ``sha256:`` + sha256 over the task object's canonical
JSON (sorted keys, no whitespace, UTF-8) without the ``task_digest`` key.
Emitting writes the task twice: the copy handed to the harness, and
muninn's own copy in the task registry. A result is checked against the
registry copy, so a harness that edits its copy changes nothing muninn
writes.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from muninn.synthesis.validation import load_schema, validate_output, validate_task_input

CONTRACT_VERSION = "muninn-synthesis/1"
PROMPT_DIR = Path(__file__).parent / "prompts"


class ContractError(ValueError):
    """A task or result file breaks the contract; nothing is written."""


def load_instructions(prompt_version: str) -> str:
    path = PROMPT_DIR / f"{prompt_version}.md"
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ContractError(f"unknown synthesis prompt version {prompt_version!r}") from None


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def compute_task_digest(task: dict[str, Any]) -> str:
    body = {k: v for k, v in task.items() if k != "task_digest"}
    return "sha256:" + hashlib.sha256(canonical_bytes(body)).hexdigest()


def build_task(
    *,
    task_id: str,
    task_type: str,
    attempt: int,
    prompt_version: str,
    materials: dict[str, Any],
    extra: dict[str, Any],
    correction_instructions: str | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    """Build and validate one task object, ``task_digest`` included."""
    created = datetime.fromtimestamp(now, timezone.utc) if now is not None else datetime.now(timezone.utc)
    task: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "task_id": task_id,
        "task_type": task_type,
        "attempt": attempt,
        "prompt_version": prompt_version,
        "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "instructions": load_instructions(prompt_version),
        "result_schema": load_schema(task_type),
        "correction_instructions": correction_instructions,
        "materials": materials,
    }
    task.update({k: v for k, v in extra.items() if v is not None})
    errors = validate_task_input(task)
    if errors:
        raise ContractError("task file fails task-input.schema.json: " + "; ".join(errors))
    task["task_digest"] = compute_task_digest(task)
    return task


def task_filename(task_id: str, attempt: int) -> str:
    return f"{task_id}-a{attempt}.task.json"


def result_filename(task_id: str, attempt: int) -> str:
    return f"{task_id}-a{attempt}.result.json"


def write_json(path: Path, value: dict[str, Any]) -> Path:
    """Write pretty JSON atomically (temp file + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise ContractError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ContractError(f"{path} is not JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractError(f"{path} does not hold a JSON object")
    return value


def load_registered_task(registry: Path, task_id: str, attempt: int) -> dict[str, Any]:
    """Muninn's own copy of an emitted task, re-validated and digest-checked."""
    path = Path(registry) / task_filename(task_id, attempt)
    if not path.is_file():
        raise ContractError(f"no emitted task {task_id!r} attempt {attempt} in {registry}")
    task = read_json(path)
    errors = validate_task_input(task)
    if errors:
        raise ContractError(f"registered task {path} is invalid: " + "; ".join(errors))
    if task.get("task_digest") != compute_task_digest(task):
        raise ContractError(f"registered task {path} does not match its own task_digest")
    return task


def check_result_envelope(result: dict[str, Any]) -> None:
    errors = validate_output(result, "synthesis_result")
    if errors:
        raise ContractError("result file fails synthesis-result.schema.json: " + "; ".join(errors))
