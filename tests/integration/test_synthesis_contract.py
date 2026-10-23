"""The ADR-013 task/result file contract: emit → (any harness) → ingest.

A task file is self-contained and validates against task-input.schema.json
(including the orchestrator's ``attempt``, Known gap 3). A result file is
accepted only when it names an emitted, still-running task by task_id,
attempt and task_digest; it is then validated exactly like an API run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from muninn.consumers.cli.main import cli
from muninn.db import connect
from muninn.synthesis import (
    emit_ad_hoc_analysis_task,
    emit_deep_pass_task,
    emit_era_narrative_task,
    ingest_result,
    validate_task_input,
)
from muninn.synthesis.contract import (
    CONTRACT_VERSION,
    ContractError,
    compute_task_digest,
    result_filename,
)
from tests.integration.synthesis_fakes import (
    adhoc_output,
    deep_pass_output,
    era_output,
    populate,
    runs,
)


@pytest.fixture
def db(fresh_db):
    return populate(fresh_db)


@pytest.fixture
def dirs(tmp_path):
    out, registry = tmp_path / "handoff", tmp_path / "registry"
    return out, registry


def _result(task: dict, output: dict, **overrides) -> dict:
    envelope = {
        "contract_version": CONTRACT_VERSION,
        "task_id": task["task_id"],
        "task_digest": task["task_digest"],
        "attempt": task["attempt"],
        "runner": "claude-code-local",
        "output": output,
    }
    envelope.update(overrides)
    return envelope


def _write_result(dir_: Path, task: dict, output: dict, **overrides) -> Path:
    path = dir_ / result_filename(task["task_id"], task["attempt"])
    path.write_text(json.dumps(_result(task, output, **overrides)))
    return path


def _task(result) -> dict:
    return json.loads(Path(result.task_file).read_text())


# ── the task file ─────────────────────────────────────────────────────────


def test_emitted_task_file_is_self_contained_and_valid(db, dirs):
    out, registry = dirs
    emitted = emit_era_narrative_task("early-web", out, conn=db, registry=registry)
    assert emitted.status == "emitted"
    task = _task(emitted)

    assert validate_task_input(task) == []
    assert task["attempt"] == 1 and task["contract_version"] == CONTRACT_VERSION
    assert task["task_digest"] == compute_task_digest(task)
    assert task["result_schema"]["$id"] == "era-narrative.schema.json"
    assert "verbatim substring" in task["instructions"]
    assert {b["bookmark_id"] for b in task["materials"]["bookmarks"]} == {1, 2, 3}
    # muninn's registry copy is byte-identical to the one handed out.
    assert (registry / emitted.task_file.name).read_bytes() == emitted.task_file.read_bytes()
    # The emitted task is an open run until a result comes back; no canonical write.
    assert runs(db, emitted.task_id) == [(1, "running")]
    assert db.execute("SELECT COUNT(*) FROM eras").fetchone()[0] == 0


def test_task_input_schema_accepts_attempt_and_rejects_out_of_range():
    """Known gap 3: the schema used to reject the orchestrator's own `attempt`."""
    base = {
        "contract_version": CONTRACT_VERSION, "task_id": "t-1", "task_type": "deep_pass",
        "attempt": 2, "prompt_version": "synthesis_v1", "instructions": "x",
        "result_schema": {}, "materials": {}, "correction_instructions": "fix it",
    }
    assert validate_task_input(base) == []
    assert any("attempt" in e for e in validate_task_input({**base, "attempt": 3}))
    assert any("attempt" in e for e in validate_task_input({k: v for k, v in base.items() if k != "attempt"}))


def test_emitting_the_same_task_id_twice_is_refused(db, dirs):
    out, registry = dirs
    emit_era_narrative_task("early-web", out, conn=db, registry=registry, task_id="era-fixed")
    with pytest.raises(ContractError, match="already emitted"):
        emit_era_narrative_task("early-web", out, conn=db, registry=registry, task_id="era-fixed")


# ── round trips ───────────────────────────────────────────────────────────


def test_era_round_trip_writes_eras_and_completes_the_run(db, dirs, tmp_path):
    out, registry = dirs
    task = _task(emit_era_narrative_task("early-web", out, conn=db, registry=registry))
    path = _write_result(tmp_path, task, era_output())

    result = ingest_result(path, conn=db, registry=registry)

    assert result.status == "completed"
    row = db.execute("SELECT narrative, bookmark_count, enrichment_model FROM eras").fetchone()
    assert row["narrative"] == "A" * 60 and row["bookmark_count"] == 3
    assert row["enrichment_model"] == "claude-opus-4-6"  # the harness-reported model
    run = db.execute(
        "SELECT status, input_token_count, output_token_count, container_id, completed_at "
        "FROM synthesis_runs WHERE task_id = ?", (task["task_id"],),
    ).fetchone()
    assert tuple(run)[:4] == ("completed", 5000, 1200, "result:claude-code-local")
    assert run["completed_at"] is not None
    # A harness's spend is not muninn's API spend.
    assert db.execute("SELECT COUNT(*) FROM usage_records").fetchone()[0] == 0


def test_deep_pass_round_trip_writes_enriched_and_cross_refs(db, dirs, tmp_path):
    out, registry = dirs
    task = _task(emit_deep_pass_task(1, out, conn=db, registry=registry))
    result = ingest_result(_write_result(tmp_path, task, deep_pass_output(2)), conn=db, registry=registry)

    assert result.status == "completed"
    row = db.execute("SELECT summary, deep_pass_requested FROM enriched WHERE bookmark_id = 1").fetchone()
    assert row["summary"] == "S" * 120 and row["deep_pass_requested"] == 0
    refs = db.execute("SELECT target_bookmark_id, created_by FROM cross_references").fetchall()
    assert [tuple(r) for r in refs] == [(2, "deep_pass")]


def test_ad_hoc_round_trip_appends_analysis_with_title(db, dirs, tmp_path):
    out, registry = dirs
    task = _task(emit_ad_hoc_analysis_task(
        "what themes connect these?", out, conn=db, registry=registry,
        bookmark_ids=[1, 2], title="Themes",
    ))
    assert {b["bookmark_id"] for b in task["materials"]["bookmarks"]} == {1, 2}
    result = ingest_result(_write_result(tmp_path, task, adhoc_output()), conn=db, registry=registry)

    assert result.status == "completed"
    row = db.execute("SELECT title, prompt FROM analyses").fetchone()
    assert tuple(row) == ("Themes", "what themes connect these?")


# ── rejected results write nothing ────────────────────────────────────────


def _nothing_written(db, task_id: str) -> None:
    assert runs(db, task_id) == [(1, "running")]
    assert db.execute("SELECT COUNT(*) FROM eras").fetchone()[0] == 0


def test_result_with_mismatched_digest_is_rejected(db, dirs, tmp_path):
    out, registry = dirs
    task = _task(emit_era_narrative_task("early-web", out, conn=db, registry=registry))
    path = _write_result(tmp_path, task, era_output(), task_digest="sha256:" + "0" * 64)
    with pytest.raises(ContractError, match="task_digest"):
        ingest_result(path, conn=db, registry=registry)
    _nothing_written(db, task["task_id"])


def test_result_for_unknown_task_is_rejected(db, dirs, tmp_path):
    out, registry = dirs
    task = _task(emit_era_narrative_task("early-web", out, conn=db, registry=registry))
    path = _write_result(tmp_path, task, era_output(), task_id="era-never-emitted")
    with pytest.raises(ContractError, match="no emitted task"):
        ingest_result(path, conn=db, registry=registry)
    _nothing_written(db, task["task_id"])


def test_result_for_wrong_attempt_is_rejected(db, dirs, tmp_path):
    out, registry = dirs
    task = _task(emit_era_narrative_task("early-web", out, conn=db, registry=registry))
    path = _write_result(tmp_path, task, era_output(), attempt=2)
    with pytest.raises(ContractError, match="no emitted task"):
        ingest_result(path, conn=db, registry=registry)
    _nothing_written(db, task["task_id"])


def test_edited_handout_copy_does_not_matter_but_edited_registry_copy_is_refused(db, dirs, tmp_path):
    out, registry = dirs
    emitted = emit_era_narrative_task("early-web", out, conn=db, registry=registry)
    task = _task(emitted)
    # A harness edits its own copy (materials); muninn writes from the registry copy.
    edited = dict(task, materials={**task["materials"], "bookmark_count": 999})
    emitted.task_file.write_text(json.dumps(edited))
    path = _write_result(tmp_path, task, era_output())
    # But a registry copy that no longer matches its digest is refused.
    reg = registry / emitted.task_file.name
    original = reg.read_text()
    reg.write_text(json.dumps(edited))
    with pytest.raises(ContractError, match="does not match its own task_digest"):
        ingest_result(path, conn=db, registry=registry)
    reg.write_text(original)
    assert ingest_result(path, conn=db, registry=registry).status == "completed"
    assert db.execute("SELECT bookmark_count FROM eras").fetchone()[0] == 3


def test_result_is_ingested_once(db, dirs, tmp_path):
    out, registry = dirs
    task = _task(emit_era_narrative_task("early-web", out, conn=db, registry=registry))
    path = _write_result(tmp_path, task, era_output())
    assert ingest_result(path, conn=db, registry=registry).status == "completed"
    with pytest.raises(ContractError, match="already completed"):
        ingest_result(path, conn=db, registry=registry)
    assert runs(db, task["task_id"]) == [(1, "completed")]


@pytest.mark.parametrize(
    "mutate, match",
    [
        (lambda r: r.pop("task_digest"), "task_digest"),
        (lambda r: r.update(contract_version="muninn-synthesis/0"), "contract_version"),
        (lambda r: r.update(extra="field"), "extra"),
        (lambda r: r.update(output="not an object"), "output"),
    ],
)
def test_malformed_result_envelope_is_rejected(db, dirs, tmp_path, mutate, match):
    out, registry = dirs
    task = _task(emit_era_narrative_task("early-web", out, conn=db, registry=registry))
    envelope = _result(task, era_output())
    mutate(envelope)
    path = tmp_path / "bad.result.json"
    path.write_text(json.dumps(envelope))
    with pytest.raises(ContractError, match=match):
        ingest_result(path, conn=db, registry=registry)
    _nothing_written(db, task["task_id"])


def test_non_json_result_is_rejected(db, dirs, tmp_path):
    out, registry = dirs
    emit_era_narrative_task("early-web", out, conn=db, registry=registry)
    path = tmp_path / "bad.result.json"
    path.write_text("Here is your narrative: ...")
    with pytest.raises(ContractError, match="not JSON"):
        ingest_result(path, conn=db, registry=registry)


# ── invalid output: recorded, retryable ───────────────────────────────────


def test_invalid_output_is_recorded_and_retry_task_emitted(db, dirs, tmp_path):
    out, registry = dirs
    task = _task(emit_deep_pass_task(1, out, conn=db, registry=registry))
    bad = deep_pass_output(2)
    bad["key_quotes"] = ["a quote that is not in the source"]
    retry_dir = tmp_path / "retry"

    result = ingest_result(
        _write_result(tmp_path, task, bad), conn=db, registry=registry, retry_dir=retry_dir,
    )

    assert result.status == "validation_failed"
    assert any("verbatim" in e for e in result.errors)
    assert db.execute("SELECT COUNT(*) FROM enriched").fetchone()[0] == 0
    errors = json.loads(db.execute(
        "SELECT validation_errors FROM synthesis_runs WHERE task_id = ? AND attempt = 1",
        (task["task_id"],),
    ).fetchone()[0])
    assert "verbatim" in errors[0]["error"]

    retry = json.loads(result.task_file.read_text())
    assert retry["attempt"] == 2 and retry["task_id"] == task["task_id"]
    assert "verbatim" in retry["correction_instructions"]
    assert retry["materials"] == task["materials"]
    assert runs(db, task["task_id"]) == [(1, "validation_failed"), (2, "running")]

    done = ingest_result(_write_result(tmp_path, retry, deep_pass_output(2)), conn=db, registry=registry)
    assert done.status == "completed"
    assert runs(db, task["task_id"]) == [(1, "validation_failed"), (2, "completed")]


def test_invalid_second_attempt_emits_no_third(db, dirs, tmp_path):
    out, registry = dirs
    task = _task(emit_era_narrative_task("early-web", out, conn=db, registry=registry))
    bad = dict(era_output(), narrative="short")
    first = ingest_result(_write_result(tmp_path, task, bad), conn=db, registry=registry,
                          retry_dir=tmp_path / "retry")
    retry = json.loads(first.task_file.read_text())
    second = ingest_result(_write_result(tmp_path, retry, bad), conn=db, registry=registry,
                           retry_dir=tmp_path / "retry")
    assert second.status == "validation_failed" and second.task_file is None
    assert runs(db, task["task_id"]) == [(1, "validation_failed"), (2, "validation_failed")]
    assert db.execute("SELECT COUNT(*) FROM eras").fetchone()[0] == 0


# ── CLI surface ───────────────────────────────────────────────────────────


def test_cli_emit_then_ingest(db_path, tmp_path, monkeypatch):
    monkeypatch.setenv("MUNINN_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("MUNINN_SYNTHESIS_MONTHLY_BUDGET_USD", raising=False)
    conn = connect(db_path)
    populate(conn)
    conn.close()
    handoff = tmp_path / "handoff"
    runner = CliRunner()

    emitted = runner.invoke(cli, ["--db", str(db_path), "synthesize", "era", "early-web",
                                  "--emit-task", str(handoff)])
    assert emitted.exit_code == 0, emitted.output
    (task_path,) = handoff.glob("*.task.json")
    assert str(task_path) in emitted.output
    assert (tmp_path / "data" / "synthesis" / "tasks" / task_path.name).is_file()
    task = json.loads(task_path.read_text())

    bad = _write_result(tmp_path, task, era_output(), task_digest="sha256:" + "1" * 64)
    rejected = runner.invoke(cli, ["--db", str(db_path), "synthesize", "ingest-result", str(bad)])
    assert rejected.exit_code != 0 and "Result rejected, nothing written" in rejected.output

    good = _write_result(tmp_path, task, era_output())
    ingested = runner.invoke(cli, ["--db", str(db_path), "synthesize", "ingest-result", str(good)])
    assert ingested.exit_code == 0, ingested.output
    assert "completed" in ingested.output


def test_cli_ingest_invalid_output_exits_nonzero_with_retry_file(db_path, tmp_path, monkeypatch):
    monkeypatch.setenv("MUNINN_DATA_DIR", str(tmp_path / "data"))
    conn = connect(db_path)
    populate(conn)
    conn.close()
    runner = CliRunner()
    runner.invoke(cli, ["--db", str(db_path), "synthesize", "deep-pass", "1",
                        "--emit-task", str(tmp_path / "handoff")])
    (task_path,) = (tmp_path / "handoff").glob("*.task.json")
    task = json.loads(task_path.read_text())
    bad = _write_result(tmp_path, task, dict(deep_pass_output(2), summary="too short"))

    res = runner.invoke(cli, ["--db", str(db_path), "synthesize", "ingest-result", str(bad),
                              "--retry-dir", str(tmp_path / "retry")])
    assert res.exit_code == 1
    assert "validation_failed" in res.output and "summary" in res.output
    assert "Retry task file:" in res.output
    assert list((tmp_path / "retry").glob("*-a2.task.json"))


def test_cli_emit_refuses_api_only_options(db_path, tmp_path):
    res = CliRunner().invoke(cli, ["--db", str(db_path), "synthesize", "era", "x",
                                   "--emit-task", str(tmp_path), "--max-cost-usd", "1"])
    assert res.exit_code != 0 and "apply to API runs" in res.output
