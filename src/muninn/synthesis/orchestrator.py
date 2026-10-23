"""Public Python API for the synthesis pipeline (ADR-013 task/result contract).

Every task is a task file (``muninn.synthesis.contract``). Two ways to run
one, with the same validation and the same canonical-table writers:

**Run** (``run_era_narrative`` / ``run_deep_pass`` / ``run_ad_hoc_analysis``;
the default ``muninn synthesize …``): a runner executes the task in-process.
The default is the budgeted :class:`~muninn.synthesis.api_runner.ApiRunner`.

  1. prepare materials (per-task module) and build the task file (attempt 1)
  2. if the runner can estimate its worst-case cost, check it against the
     budget; when it does not fit, no call is made → ``cap_hit``
  3. run it; a measured cost is written to ``usage_records``
  4. runner failure (API error, refusal) → ``container_failed``
  5. validate the output (schema + verbatim quotes + cross-ref targets)
  6. on validation failure: rebuild the task with correction_instructions
     and run it ONCE more (attempt 2)
  7. on success: write the canonical table(s) and the audit row in one
     transaction → ``completed``; both attempts invalid → ``validation_failed``
     with no canonical-table writes

**Emit / ingest** (``emit_*_task`` + ``ingest_result``; ``--emit-task DIR``
and ``synthesize ingest-result FILE``): any harness runs the task file.

  1. emit: write the task file to DIR and to muninn's task registry, and
     record a ``synthesis_runs`` row with status ``running``
  2. ingest: the result file must name an emitted task (task_id, attempt,
     task_digest) whose row is still ``running``; otherwise it is rejected
     with no writes at all
  3. validate the output exactly as in a run; invalid → the row becomes
     ``validation_failed`` (and, with ``retry_dir``, an attempt-2 task
     file carrying the correction is emitted); valid → canonical writes
     and ``completed`` in one transaction

The orchestrator is the only thing that writes to ``synthesis_runs``: one
row per attempt (UNIQUE on task_id, attempt). ``synthesis_runs.container_id``
(a legacy name) holds the runner reference: ``anthropic-api:<message id>``
for the API runner, ``result:<runner>`` for an ingested result file.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from muninn.config import SYNTHESIS_PROMPT_VERSION, synthesis_task_registry
from muninn.db import connect, transaction
from muninn.models import SynthesisRun
from muninn.synthesis import correction as _correction
from muninn.synthesis.api_runner import ApiRunner, RunnerResult
from muninn.synthesis.budget import Budget, record_usage
from muninn.synthesis.contract import (
    ContractError,
    build_task,
    check_result_envelope,
    load_registered_task,
    read_json,
    task_filename,
    write_json,
)
from muninn.synthesis.tasks import ad_hoc_analysis as _ad_hoc
from muninn.synthesis.tasks import deep_pass as _deep_pass
from muninn.synthesis.tasks import era_narrative as _era
from muninn.synthesis.tasks import lint as _lint
from muninn.synthesis.validation import (
    validate_against_schema,
    validate_cross_reference_targets,
    validate_key_quotes,
)


log = logging.getLogger(__name__)

TASK_ID_PREFIX = {"era_narrative": "era", "deep_pass": "deep", "ad_hoc_analysis": "adhoc", "lint": "lint"}
# Prompt version per task type; the three original tasks share synthesis_vN.
DEFAULT_PROMPT_VERSIONS = {"lint": _lint.PROMPT_VERSION}


def default_prompt_version(task_type: str) -> str:
    return DEFAULT_PROMPT_VERSIONS.get(task_type, SYNTHESIS_PROMPT_VERSION)


@dataclass
class TaskResult:
    """Outcome of one orchestrated task: a run (one or two attempts), an
    emitted task file, or an ingested result file."""
    task_id: str
    task_type: str
    status: str
    attempts: list[SynthesisRun]
    output: dict[str, Any] | None = None
    task_file: Path | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def final_attempt(self) -> SynthesisRun | None:
        return self.attempts[-1] if self.attempts else None


# ── Task specs: materials + envelope fields per task type ─────────────────


def _era_spec(conn: sqlite3.Connection, era_label: str) -> tuple[dict, dict]:
    return _era.prepare_input(conn, era_label), {"era_label": era_label}


def _deep_spec(
    conn: sqlite3.Connection, bookmark_id: int, candidate_neighbor_ids: list[int] | None
) -> tuple[dict, dict]:
    materials = _deep_pass.prepare_input(conn, bookmark_id, candidate_neighbor_ids=candidate_neighbor_ids)
    return materials, {"bookmark_id": bookmark_id}


def _ad_hoc_spec(
    conn: sqlite3.Connection,
    prompt: str,
    filter_query: str | None,
    bookmark_ids: list[int] | None,
    title: str | None,
) -> tuple[dict, dict]:
    materials = _ad_hoc.prepare_input(conn, prompt, filter_query=filter_query, bookmark_ids=bookmark_ids)
    return materials, {"prompt": prompt, "filter_query": filter_query, "title": title}


def _lint_spec(
    conn: sqlite3.Connection, tag: str, limit: int, prompt_version: str, model: str | None
) -> tuple[dict, dict, int]:
    """Materials holding only the pending pairs (and their pages), the
    envelope fields, and the number of candidate pairs."""
    materials, candidates = _lint.prepare_task(
        conn, tag, limit=limit, prompt_version=prompt_version, model=model
    )
    return materials, {"tag": tag.strip()}, candidates


def _lint_prompt_version(prompt_version: str | None) -> str:
    from muninn.enrich.haiku import check_prompt_version  # the enrich rule: <task>_vN

    version = prompt_version or _lint.PROMPT_VERSION
    try:
        check_prompt_version(_lint.TASK, version)
    except ValueError as exc:
        raise ContractError(str(exc)) from None
    return version


def _writer_for(task: dict[str, Any]) -> Callable[[sqlite3.Connection, dict, SynthesisRun], None]:
    """The canonical-table writer for a task, rebuilt from the task file
    alone, so a run and an ingested result write identically."""
    materials = task["materials"]
    task_type = task["task_type"]
    if task_type == "era_narrative":
        return lambda c, o, r: _era.write_output(
            c, o, r, era_label=task["era_label"], materials=materials
        )
    if task_type == "deep_pass":
        return lambda c, o, r: _deep_pass.write_output(
            c, o, r, bookmark_id=task["bookmark_id"], materials=materials
        )
    if task_type == "ad_hoc_analysis":
        return lambda c, o, r: _ad_hoc.write_output(
            c, o, r,
            prompt=task["prompt"],
            filter_query=task.get("filter_query"),
            title=task.get("title"),
            materials=materials,
        )
    if task_type == "lint":
        return lambda c, o, r: _lint.write_output(c, o, r, tag=task["tag"], materials=materials)
    raise ContractError(f"unknown task_type {task_type!r}")


# ── Public entry points: run ───────────────────────────────────────────────


def run_era_narrative(
    era_label: str,
    *,
    conn: sqlite3.Connection | None = None,
    runner: Any = None,
    budget: Budget | None = None,
    task_id: str | None = None,
) -> TaskResult:
    """Synthesize an era narrative and UPSERT it into `eras`."""
    own_conn, conn = _ensure_conn(conn)
    try:
        materials, extra = _era_spec(conn, era_label)
        return _run_task(conn, runner, budget, task_id, "era_narrative", materials, extra)
    finally:
        if own_conn:
            conn.close()


def run_deep_pass(
    bookmark_id: int,
    *,
    conn: sqlite3.Connection | None = None,
    runner: Any = None,
    budget: Budget | None = None,
    candidate_neighbor_ids: list[int] | None = None,
    task_id: str | None = None,
) -> TaskResult:
    """Run a deep_pass on one bookmark.

    Writes:
      - UPDATE enriched (summary, tags, entities, content_type, language,
        word_count, key_quotes, deep_pass_requested=0); the enrichment
        idempotency triple is preserved (see tasks/deep_pass.py)
      - INSERT cross_references (created_by='deep_pass')
    Verbatim quote check + cross-reference target FK validation are performed
    BEFORE any canonical-table write.
    """
    own_conn, conn = _ensure_conn(conn)
    try:
        materials, extra = _deep_spec(conn, bookmark_id, candidate_neighbor_ids)
        return _run_task(conn, runner, budget, task_id, "deep_pass", materials, extra)
    finally:
        if own_conn:
            conn.close()


def run_ad_hoc_analysis(
    prompt: str,
    *,
    conn: sqlite3.Connection | None = None,
    runner: Any = None,
    budget: Budget | None = None,
    filter_query: str | None = None,
    bookmark_ids: list[int] | None = None,
    title: str | None = None,
    task_id: str | None = None,
) -> TaskResult:
    """Run an ad-hoc analysis. Appends one row to `analyses`."""
    own_conn, conn = _ensure_conn(conn)
    try:
        materials, extra = _ad_hoc_spec(conn, prompt, filter_query, bookmark_ids, title)
        return _run_task(conn, runner, budget, task_id, "ad_hoc_analysis", materials, extra)
    finally:
        if own_conn:
            conn.close()


def run_lint(
    tag: str,
    *,
    conn: sqlite3.Connection | None = None,
    runner: Any = None,
    budget: Budget | None = None,
    limit: int = _lint.DEFAULT_LIMIT,
    prompt_version: str | None = None,
    task_id: str | None = None,
) -> TaskResult:
    """Lint the compiled bookmark pages tagged ``tag`` for contradictions
    and superseded claims; writes lint_pair_checks + lint_findings.

    Only the pending pairs (not yet checked with the same content hashes,
    prompt version and model) and their pages are sent. When there are
    none, no task is built and no call is made (status ``up_to_date``)."""
    own_conn, conn = _ensure_conn(conn)
    try:
        version = _lint_prompt_version(prompt_version)
        runner = runner if runner is not None else ApiRunner()
        materials, extra, candidates = _lint_spec(conn, tag, limit, version, getattr(runner, "model", None))
        if not materials["pairs"]:
            return _up_to_date("lint", candidates)
        return _run_task(conn, runner, budget, task_id, "lint", materials, extra, prompt_version=version)
    finally:
        if own_conn:
            conn.close()


# ── Public entry points: emit / ingest ─────────────────────────────────────


def emit_era_narrative_task(
    era_label: str,
    out_dir: Path | str,
    *,
    conn: sqlite3.Connection | None = None,
    registry: Path | str | None = None,
    task_id: str | None = None,
) -> TaskResult:
    """Write an era_narrative task file to `out_dir`; run nothing."""
    own_conn, conn = _ensure_conn(conn)
    try:
        materials, extra = _era_spec(conn, era_label)
        return _emit(conn, "era_narrative", materials, extra, out_dir, registry, task_id)
    finally:
        if own_conn:
            conn.close()


def emit_deep_pass_task(
    bookmark_id: int,
    out_dir: Path | str,
    *,
    conn: sqlite3.Connection | None = None,
    registry: Path | str | None = None,
    candidate_neighbor_ids: list[int] | None = None,
    task_id: str | None = None,
) -> TaskResult:
    """Write a deep_pass task file to `out_dir`; run nothing."""
    own_conn, conn = _ensure_conn(conn)
    try:
        materials, extra = _deep_spec(conn, bookmark_id, candidate_neighbor_ids)
        return _emit(conn, "deep_pass", materials, extra, out_dir, registry, task_id)
    finally:
        if own_conn:
            conn.close()


def emit_ad_hoc_analysis_task(
    prompt: str,
    out_dir: Path | str,
    *,
    conn: sqlite3.Connection | None = None,
    registry: Path | str | None = None,
    filter_query: str | None = None,
    bookmark_ids: list[int] | None = None,
    title: str | None = None,
    task_id: str | None = None,
) -> TaskResult:
    """Write an ad_hoc_analysis task file to `out_dir`; run nothing."""
    own_conn, conn = _ensure_conn(conn)
    try:
        materials, extra = _ad_hoc_spec(conn, prompt, filter_query, bookmark_ids, title)
        return _emit(conn, "ad_hoc_analysis", materials, extra, out_dir, registry, task_id)
    finally:
        if own_conn:
            conn.close()


def emit_lint_task(
    tag: str,
    out_dir: Path | str,
    *,
    conn: sqlite3.Connection | None = None,
    registry: Path | str | None = None,
    limit: int = _lint.DEFAULT_LIMIT,
    prompt_version: str | None = None,
    task_id: str | None = None,
) -> TaskResult:
    """Write a lint task file to `out_dir`; run nothing. The task holds
    the pairs not yet checked with the same content hashes and prompt
    version, by any model. Skipped (``up_to_date``, no file) when there
    are none."""
    own_conn, conn = _ensure_conn(conn)
    try:
        version = _lint_prompt_version(prompt_version)
        materials, extra, candidates = _lint_spec(conn, tag, limit, version, None)
        if not materials["pairs"]:
            return _up_to_date("lint", candidates)
        return _emit(conn, "lint", materials, extra, out_dir, registry, task_id, prompt_version=version)
    finally:
        if own_conn:
            conn.close()


def ingest_result(
    path: Path | str,
    *,
    conn: sqlite3.Connection | None = None,
    registry: Path | str | None = None,
    retry_dir: Path | str | None = None,
) -> TaskResult:
    """Validate a result file against its emitted task and write it.

    Raises :class:`ContractError`, with nothing written, when the file is
    not a valid result envelope, names no emitted task, carries a different
    task_digest, or answers a task that is no longer ``running``.
    """
    own_conn, conn = _ensure_conn(conn)
    try:
        return _ingest(conn, Path(path), Path(registry or synthesis_task_registry()), retry_dir)
    finally:
        if own_conn:
            conn.close()


# ── Core: run loop ─────────────────────────────────────────────────────────


def _run_task(
    conn: sqlite3.Connection,
    runner: Any,
    budget: Budget | None,
    task_id: str | None,
    task_type: str,
    materials: dict[str, Any],
    extra: dict[str, Any],
    *,
    prompt_version: str | None = None,
) -> TaskResult:
    runner = runner if runner is not None else ApiRunner()
    prompt_version = prompt_version or default_prompt_version(task_type)
    task_id = task_id or _new_task_id(task_type)
    attempts: list[SynthesisRun] = []
    correction_instructions: str | None = None

    def done(status: str, output: dict | None = None, errors: list[str] | None = None) -> TaskResult:
        return TaskResult(task_id=task_id, task_type=task_type, status=status,
                          attempts=attempts, output=output, errors=errors or [])

    for attempt in (1, 2):
        task = build_task(
            task_id=task_id,
            task_type=task_type,
            attempt=attempt,
            prompt_version=prompt_version,
            materials=materials,
            extra=extra,
            correction_instructions=correction_instructions,
        )
        run = _new_run(task, model=getattr(runner, "model", None))

        estimate = getattr(runner, "estimate", None)
        if estimate is not None:
            if budget is None:
                budget = Budget(conn)
            check = budget.check(estimate(task))
            if not check.fits:
                log.warning("synthesis %s attempt %d stopped by the budget: %s",
                            task_id, attempt, check.evidence())
                _finish(conn, run, "cap_hit", [check.evidence()], insert=True)
                attempts.append(run)
                return done("cap_hit")

        result: RunnerResult = runner.run(task)
        if result.cost_micro_usd is not None:
            if budget is not None:
                budget.charge(result.cost_micro_usd)
            record_usage(
                conn,
                task_type=task_type,
                model=result.model or run.enrichment_model or "unknown",
                usage=result.usage,
                cost_micro_usd=result.cost_micro_usd,
                now=int(time.time()),
            )
        run.container_id = result.runner_ref
        run.enrichment_model = result.model or run.enrichment_model
        _set_token_counts(run, result.output_json)
        if result.input_tokens is not None:
            run.input_token_count = result.input_tokens
        if result.output_tokens is not None:
            run.output_token_count = result.output_tokens

        if result.error:
            log.error("synthesis runner failed: %s", result.error)
            _finish(conn, run, "container_failed", [{"error": result.error}], insert=True)
            attempts.append(run)
            return done("container_failed", errors=[result.error])

        output = result.output_json
        if result.parse_errors:
            errors = list(result.parse_errors)
        elif output is None:
            errors = ["(root): the runner returned no output"]
        else:
            errors = _validate(output, task)
        if not run.enrichment_model and output:
            run.enrichment_model = (output.get("synthesis_metadata") or {}).get("model")

        if not errors:
            with transaction(conn):
                _writer_for(task)(conn, output, run)
                _finish(conn, run, "completed", None, insert=True)
            attempts.append(run)
            return done("completed", output=output)

        _finish(conn, run, "validation_failed", [{"error": e} for e in errors], insert=True)
        attempts.append(run)
        if attempt == 1:
            correction_instructions = _correction.build_correction_instructions(errors)
            log.info("validation failed on attempt 1; retrying with correction")
            continue
        return done("validation_failed", errors=errors)

    raise RuntimeError("unreachable: synthesis run loop exited without result")


# ── Core: emit / ingest ────────────────────────────────────────────────────


def _emit(
    conn: sqlite3.Connection,
    task_type: str,
    materials: dict[str, Any],
    extra: dict[str, Any],
    out_dir: Path | str,
    registry: Path | str | None,
    task_id: str | None,
    *,
    attempt: int = 1,
    correction_instructions: str | None = None,
    prompt_version: str | None = None,
) -> TaskResult:
    task_id = task_id or _new_task_id(task_type)
    registry = Path(registry or synthesis_task_registry())
    name = task_filename(task_id, attempt)
    if (registry / name).exists() or _run_row(conn, task_id, attempt) is not None:
        raise ContractError(f"task {task_id!r} attempt {attempt} was already emitted")
    task = build_task(
        task_id=task_id,
        task_type=task_type,
        attempt=attempt,
        prompt_version=prompt_version or default_prompt_version(task_type),
        materials=materials,
        extra=extra,
        correction_instructions=correction_instructions,
    )
    write_json(registry / name, task)
    run = _new_run(task, model=None)
    _finish(conn, run, "running", None, insert=True, completed=False)
    out = write_json(Path(out_dir) / name, task)
    return TaskResult(task_id=task_id, task_type=task_type, status="emitted",
                      attempts=[run], task_file=out)


def _ingest(
    conn: sqlite3.Connection,
    path: Path,
    registry: Path,
    retry_dir: Path | str | None,
) -> TaskResult:
    envelope = read_json(path)
    check_result_envelope(envelope)
    task_id, attempt = envelope["task_id"], envelope["attempt"]
    task = load_registered_task(registry, task_id, attempt)
    if envelope["task_digest"] != task["task_digest"]:
        raise ContractError(
            f"result task_digest {envelope['task_digest']} does not match emitted task "
            f"{task_id!r} attempt {attempt} ({task['task_digest']})"
        )
    row = _run_row(conn, task_id, attempt)
    if row is None:
        raise ContractError(f"no synthesis_runs row for emitted task {task_id!r} attempt {attempt}")
    if row["status"] != "running":
        raise ContractError(
            f"task {task_id!r} attempt {attempt} is already {row['status']}; a result is ingested once"
        )

    output = envelope["output"]
    meta = output.get("synthesis_metadata") if isinstance(output.get("synthesis_metadata"), dict) else {}
    run = _new_run(task, model=meta.get("model") if isinstance(meta.get("model"), str) else None)
    run.started_at = row["started_at"]
    run.container_id = "result:" + envelope.get("runner", "unnamed")
    _set_token_counts(run, output)
    task_type = task["task_type"]

    errors = _validate(output, task)
    if errors:
        _finish(conn, run, "validation_failed", [{"error": e} for e in errors], insert=False)
        res = TaskResult(task_id=task_id, task_type=task_type, status="validation_failed",
                         attempts=[run], errors=errors)
        if retry_dir is not None and attempt == 1:
            retry = _emit(
                conn, task_type, task["materials"], _envelope_extra(task), retry_dir, registry,
                task_id, attempt=2,
                correction_instructions=_correction.build_correction_instructions(errors),
                prompt_version=task["prompt_version"],
            )
            res.task_file = retry.task_file
        return res

    with transaction(conn):
        _writer_for(task)(conn, output, run)
        _finish(conn, run, "completed", None, insert=False)
    return TaskResult(task_id=task_id, task_type=task_type, status="completed",
                      attempts=[run], output=output)


# ── Helpers ────────────────────────────────────────────────────────────────


def _envelope_extra(task: dict[str, Any]) -> dict[str, Any]:
    keys = ("era_label", "bookmark_id", "prompt", "filter_query", "title", "tag")
    return {k: task.get(k) for k in keys}


def _up_to_date(task_type: str, pairs: int) -> TaskResult:
    return TaskResult(
        task_id="", task_type=task_type, status="up_to_date", attempts=[],
        errors=[f"all {pairs} page pair(s) already checked; nothing to pay for"],
    )


def _new_run(task: dict[str, Any], *, model: str | None) -> SynthesisRun:
    return SynthesisRun(
        task_id=task["task_id"],
        task_type=task["task_type"],  # type: ignore[arg-type]
        attempt=task["attempt"],
        started_at=int(time.time()),
        status="running",
        enrichment_model=model,
        enrichment_prompt_version=task["prompt_version"],
    )


def _validate(output: dict[str, Any], task: dict[str, Any]) -> list[str]:
    """Schema (the task's own result_schema), then the deep-pass checks
    against the task's materials."""
    errors = validate_against_schema(output, task["result_schema"])
    if errors:
        return errors

    materials = task["materials"]
    if task["task_type"] == "lint":
        errors.extend(_lint.validate_output(output, materials))
    if task["task_type"] == "deep_pass":
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
    if not isinstance(meta, dict):
        return
    for attr, key in (("input_token_count", "input_token_count"), ("output_token_count", "output_token_count")):
        value = meta.get(key)
        setattr(run, attr, value if isinstance(value, int) and value >= 0 else None)


def _run_row(conn: sqlite3.Connection, task_id: str, attempt: int) -> sqlite3.Row | None:
    cur = conn.execute(
        "SELECT status, started_at FROM synthesis_runs WHERE task_id = ? AND attempt = ?",
        (task_id, attempt),
    )
    cur.row_factory = sqlite3.Row
    return cur.fetchone()


def _finish(
    conn: sqlite3.Connection,
    run: SynthesisRun,
    status: str,
    errors: list[dict[str, Any]] | None,
    *,
    insert: bool,
    completed: bool = True,
) -> None:
    """Set the run's final status and write its synthesis_runs row:
    INSERT for a run attempt, UPDATE for an ingested result (whose row was
    inserted as `running` at emit). Commits, so the audit row survives
    even outside the success transaction."""
    run.status = status  # type: ignore[assignment]
    run.validation_errors = errors
    if completed:
        run.completed_at = int(time.time())
    values = (
        run.completed_at,
        run.status,
        run.enrichment_model,
        run.enrichment_prompt_version,
        run.input_token_count,
        run.output_token_count,
        json.dumps(run.validation_errors) if run.validation_errors else None,
        run.container_id,
    )
    if insert:
        conn.execute(
            """
            INSERT INTO synthesis_runs (
                task_id, task_type, attempt, started_at, completed_at, status,
                enrichment_model, enrichment_prompt_version,
                input_token_count, output_token_count, validation_errors, container_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (run.task_id, run.task_type, run.attempt, run.started_at, *values),
        )
    else:
        conn.execute(
            """
            UPDATE synthesis_runs SET
                completed_at = ?, status = ?, enrichment_model = ?,
                enrichment_prompt_version = ?, input_token_count = ?,
                output_token_count = ?, validation_errors = ?, container_id = ?
            WHERE task_id = ? AND attempt = ?
            """,
            (*values, run.task_id, run.attempt),
        )
    conn.commit()


def _ensure_conn(
    conn: sqlite3.Connection | None,
) -> tuple[bool, sqlite3.Connection]:
    """Return (own_conn, conn). own_conn=True means caller didn't pass one."""
    if conn is None:
        return True, connect()
    return False, conn


def _new_task_id(task_type: str) -> str:
    return f"{TASK_ID_PREFIX[task_type]}-{uuid.uuid4().hex[:12]}"
