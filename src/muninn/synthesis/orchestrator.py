"""Public Python API for the synthesis pipeline.

Lifecycle for one task:
  1. prepare materials (per-task module)
  2. launch container (attempt 1)
  3. validate output (schema + verbatim + cross-ref targets)
  4. on validation failure: rebuild input with correction_instructions and
     launch container ONCE more (attempt 2)
  5. on success: write to canonical table(s) inside a transaction; record
     synthesis_runs row with status='completed'
  6. on cap_hit: record synthesis_runs row with status='cap_hit'
  7. on container failure: record synthesis_runs row with status='container_failed'
  8. on validation failure (both attempts): record status='validation_failed',
     no canonical-table writes

The orchestrator is the only thing that writes to `synthesis_runs`. Each
launch attempt produces one row (UNIQUE on task_id, attempt).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any

from muninn.config import OPUS_MODEL
from muninn.db import connect, transaction
from muninn.models import SynthesisRun
from muninn.synthesis import correction as _correction
from muninn.synthesis.container import (
    CapHitError,
    ContainerLauncher,
    ContainerLaunchError,
    ContainerResult,
)
from muninn.synthesis.tasks import ad_hoc_analysis as _ad_hoc
from muninn.synthesis.tasks import deep_pass as _deep_pass
from muninn.synthesis.tasks import era_narrative as _era
from muninn.synthesis.validation import (
    validate_cross_reference_targets,
    validate_key_quotes,
    validate_output,
)


log = logging.getLogger(__name__)


SYNTHESIS_PROMPT_VERSION = "synthesis_v1"


@dataclass
class TaskResult:
    """Outcome of a complete orchestrated task (one or two container attempts)."""
    task_id: str
    task_type: str
    status: str
    attempts: list[SynthesisRun]
    output: dict[str, Any] | None = None

    @property
    def final_attempt(self) -> SynthesisRun | None:
        return self.attempts[-1] if self.attempts else None


# ── Public entry points ────────────────────────────────────────────────────


def run_era_narrative(
    era_label: str,
    *,
    conn: sqlite3.Connection | None = None,
    launcher: ContainerLauncher | None = None,
    task_id: str | None = None,
) -> TaskResult:
    """Synthesize an era narrative and UPSERT it into `eras`."""
    own_conn, conn = _ensure_conn(conn)
    try:
        materials = _era.prepare_input(conn, era_label)
        return _run_task(
            conn=conn,
            launcher=launcher,
            task_id=task_id or _new_task_id("era"),
            task_type="era_narrative",
            materials=materials,
            envelope_extra={"era_label": era_label},
            on_success=lambda c, o, r: _era.write_output(
                c, o, r, era_label=era_label, materials=materials
            ),
        )
    finally:
        if own_conn:
            conn.close()


def run_deep_pass(
    bookmark_id: int,
    *,
    conn: sqlite3.Connection | None = None,
    launcher: ContainerLauncher | None = None,
    candidate_neighbor_ids: list[int] | None = None,
    task_id: str | None = None,
) -> TaskResult:
    """Run a deep_pass on one bookmark.

    Writes:
      - UPDATE enriched (summary, tags, entities, content_type, language,
        word_count, key_quotes, deep_pass_requested=1)
      - INSERT cross_references (created_by='deep_pass')
    Verbatim quote check + cross-reference target FK validation are performed
    BEFORE any canonical-table write.
    """
    own_conn, conn = _ensure_conn(conn)
    try:
        materials = _deep_pass.prepare_input(
            conn, bookmark_id, candidate_neighbor_ids=candidate_neighbor_ids
        )
        return _run_task(
            conn=conn,
            launcher=launcher,
            task_id=task_id or _new_task_id("deep"),
            task_type="deep_pass",
            materials=materials,
            envelope_extra={"bookmark_id": bookmark_id},
            on_success=lambda c, o, r: _deep_pass.write_output(
                c, o, r, bookmark_id=bookmark_id, materials=materials
            ),
        )
    finally:
        if own_conn:
            conn.close()


def run_ad_hoc_analysis(
    prompt: str,
    *,
    conn: sqlite3.Connection | None = None,
    launcher: ContainerLauncher | None = None,
    filter_query: str | None = None,
    bookmark_ids: list[int] | None = None,
    title: str | None = None,
    task_id: str | None = None,
) -> TaskResult:
    """Run an ad-hoc analysis. Appends one row to `analyses`."""
    own_conn, conn = _ensure_conn(conn)
    try:
        materials = _ad_hoc.prepare_input(
            conn, prompt, filter_query=filter_query, bookmark_ids=bookmark_ids
        )
        return _run_task(
            conn=conn,
            launcher=launcher,
            task_id=task_id or _new_task_id("adhoc"),
            task_type="ad_hoc_analysis",
            materials=materials,
            envelope_extra={"prompt": prompt, "filter_query": filter_query},
            on_success=lambda c, o, r: _ad_hoc.write_output(
                c, o, r,
                prompt=prompt,
                filter_query=filter_query,
                title=title,
                materials=materials,
            ),
        )
    finally:
        if own_conn:
            conn.close()


# ── Core run loop ──────────────────────────────────────────────────────────


def _run_task(
    *,
    conn: sqlite3.Connection,
    launcher: ContainerLauncher | None,
    task_id: str,
    task_type: str,
    materials: dict[str, Any],
    envelope_extra: dict[str, Any],
    on_success,
) -> TaskResult:
    launcher = launcher or ContainerLauncher()
    attempts: list[SynthesisRun] = []
    correction_instructions: str | None = None

    for attempt in (1, 2):
        run = SynthesisRun(
            task_id=task_id,
            task_type=task_type,  # type: ignore[arg-type]
            attempt=attempt,
            started_at=int(time.time()),
            status="running",
            enrichment_model=OPUS_MODEL,
            enrichment_prompt_version=SYNTHESIS_PROMPT_VERSION,
        )
        envelope = _build_envelope(
            task_id=task_id,
            task_type=task_type,
            attempt=attempt,
            materials=materials,
            envelope_extra=envelope_extra,
            correction_instructions=correction_instructions,
        )

        try:
            result = launcher.launch(envelope)
        except ContainerLaunchError as exc:
            log.error("container launch failed: %s", exc)
            run.completed_at = int(time.time())
            run.status = "container_failed"
            run.validation_errors = [{"error": str(exc)}]
            _record_run(conn, run)
            attempts.append(run)
            return TaskResult(
                task_id=task_id, task_type=task_type, status="container_failed",
                attempts=attempts,
            )

        run.container_id = result.container_id
        _set_token_counts(run, result.output_json)

        if result.cap_hit:
            run.completed_at = int(time.time())
            run.status = "cap_hit"
            run.validation_errors = [{"cap_hit_evidence": result.cap_hit_evidence}]
            _record_run(conn, run)
            attempts.append(run)
            return TaskResult(
                task_id=task_id, task_type=task_type, status="cap_hit",
                attempts=attempts,
            )

        # Container exited but produced no parseable output JSON.
        if result.output_json is None:
            errors = _container_failure_errors(result)
            run.completed_at = int(time.time())
            run.status = "container_failed"
            run.validation_errors = [{"error": e} for e in errors]
            _record_run(conn, run)
            attempts.append(run)
            return TaskResult(
                task_id=task_id, task_type=task_type, status="container_failed",
                attempts=attempts,
            )

        # Validate.
        errors = _validate(result.output_json, task_type, materials)

        if not errors:
            # Success path: write canonical tables + audit row in one transaction.
            with transaction(conn):
                on_success(conn, result.output_json, run)
                run.completed_at = int(time.time())
                run.status = "completed"
                _record_run(conn, run)
            attempts.append(run)
            return TaskResult(
                task_id=task_id, task_type=task_type, status="completed",
                attempts=attempts, output=result.output_json,
            )

        # Validation failed. Record this attempt; if first attempt, build
        # correction_instructions and loop. If second attempt, give up.
        run.completed_at = int(time.time())
        run.status = "validation_failed"
        run.validation_errors = [{"error": e} for e in errors]
        _record_run(conn, run)
        attempts.append(run)

        if attempt == 1:
            correction_instructions = _correction.build_correction_instructions(errors)
            log.info("validation failed on attempt 1; retrying with correction")
            continue

        # Both attempts failed.
        return TaskResult(
            task_id=task_id, task_type=task_type, status="validation_failed",
            attempts=attempts,
        )

    # Unreachable; loop always returns.
    raise RuntimeError("unreachable: synthesis run loop exited without result")


# ── Helpers ────────────────────────────────────────────────────────────────


def _build_envelope(
    *,
    task_id: str,
    task_type: str,
    attempt: int,
    materials: dict[str, Any],
    envelope_extra: dict[str, Any],
    correction_instructions: str | None,
) -> dict[str, Any]:
    envelope: dict[str, Any] = {
        "task_id": task_id,
        "task_type": task_type,
        "attempt": attempt,
        "materials": materials,
        "correction_instructions": correction_instructions,
    }
    envelope.update({k: v for k, v in envelope_extra.items() if v is not None})
    return envelope


def _validate(
    output: dict[str, Any],
    task_type: str,
    materials: dict[str, Any],
) -> list[str]:
    errors = validate_output(output, task_type)
    if errors:
        return errors

    if task_type == "deep_pass":
        # Verbatim quote check
        source_text = materials.get("source_text", "") or ""
        quotes = output.get("key_quotes") or []
        if quotes and source_text:
            missing = validate_key_quotes(quotes, source_text)
            if missing:
                errors.extend(
                    f"key_quotes[{i}]: {q!r} is not a verbatim substring of source text"
                    for i, q in enumerate(missing)
                )

        # Cross-reference target FK validation against input materials.
        cross_refs = output.get("cross_references") or []
        known_ids = set(materials.get("known_bookmark_ids", []) or [])
        if cross_refs and known_ids:
            errors.extend(validate_cross_reference_targets(cross_refs, known_ids))

    return errors


def _set_token_counts(run: SynthesisRun, output_json: dict[str, Any] | None) -> None:
    if not output_json:
        return
    meta = output_json.get("synthesis_metadata") or {}
    run.input_token_count = meta.get("input_token_count")
    run.output_token_count = meta.get("output_token_count")


def _container_failure_errors(result: ContainerResult) -> list[str]:
    errs: list[str] = [
        f"container exited with code {result.exit_code} and produced no output JSON"
    ]
    tail = (result.raw_stderr or result.raw_stdout or "").strip().splitlines()[-10:]
    if tail:
        errs.append("last lines: " + " | ".join(tail))
    return errs


def _record_run(conn: sqlite3.Connection, run: SynthesisRun) -> None:
    """INSERT a synthesis_runs row. Uses its own savepoint so the audit row
    survives even when called outside the success transaction."""
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO synthesis_runs (
            task_id, task_type, attempt, started_at, completed_at, status,
            enrichment_model, enrichment_prompt_version,
            input_token_count, output_token_count, validation_errors, container_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run.task_id,
            run.task_type,
            run.attempt,
            run.started_at,
            run.completed_at,
            run.status,
            run.enrichment_model,
            run.enrichment_prompt_version,
            run.input_token_count,
            run.output_token_count,
            json.dumps(run.validation_errors) if run.validation_errors else None,
            run.container_id,
        ),
    )
    conn.commit()


def _ensure_conn(
    conn: sqlite3.Connection | None,
) -> tuple[bool, sqlite3.Connection]:
    """Return (own_conn, conn). own_conn=True means caller didn't pass one."""
    if conn is None:
        return True, connect()
    return False, conn


def _new_task_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"
