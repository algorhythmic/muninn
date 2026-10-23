"""The model-tier lint task (task_type 'lint', prompt lint_v1).

The Anthropic client is a MagicMock (tests/integration/synthesis_fakes.py):
no network, no model. Covers the materials (visible compiled bookmark pages
sharing a tag, X masking and withholding), the budget (cap_hit with no
call, the required monthly cap), idempotent re-runs that never re-pay,
evidence verification with the correction retry, the emit/ingest path,
the prompt-version check and the CLI.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from muninn.consumers.cli.main import cli
from muninn.synthesis import emit_lint_task, ingest_result, run_lint, validate_task_input
from muninn.synthesis.api_runner import ApiRunner
from muninn.synthesis.budget import Budget, SynthesisConfigError
from muninn.synthesis.contract import CONTRACT_VERSION, ContractError, result_filename
from muninn.synthesis.tasks import lint as lint_task
from tests.integration.lint_fakes import (
    CONTRADICTS,
    HIDDEN_URL,
    SUPERSEDES,
    finding,
    lint_output,
    seed,
)
from tests.integration.synthesis_fakes import fake_client, message, runs

MONTHLY_ENV = "MUNINN_SYNTHESIS_MONTHLY_BUDGET_USD"


@pytest.fixture
def db(fresh_db, monkeypatch):
    monkeypatch.setenv(MONTHLY_ENV, "10")
    monkeypatch.delenv("MUNINN_SYNTHESIS_MODEL", raising=False)
    monkeypatch.delenv("MUNINN_SYNTHESIS_EFFORT", raising=False)
    return seed(fresh_db)


def _count(conn, table: str) -> int:
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


# ── materials ─────────────────────────────────────────────────────────────


def test_materials_are_visible_tagged_pages_with_x_masking(db):
    materials = lint_task.prepare_input(db, "llm")
    pages = materials["pages"]
    assert [p["bookmark_id"] for p in pages] == [1, 2, 3]  # 4 hidden, 5 untagged; oldest first
    dump = json.dumps(materials)
    assert "intra.example" not in dump and "secret" not in dump.lower()
    post = pages[2]
    assert post["text"] == "Hot take: prompt caching never works in practice. Details [hidden link] soon."
    assert post["summary"] is None  # it quoted the hidden locator: withheld whole
    assert post["title"].endswith("[hidden link]")
    assert (post["dated_by"], post["dated_at"]) == ("published", "2023-12-24T22:13:20Z")
    assert (pages[0]["dated_by"], pages[0]["dated_at"]) == ("captured", "2023-11-14T22:13:20Z")
    for page in pages:
        assert page["content_hash"] == lint_task._canonical_hash(page)


def test_tag_match_is_case_insensitive_and_too_few_pages_is_refused(db):
    assert [p["bookmark_id"] for p in lint_task.prepare_input(db, "LLM")["pages"]] == [1, 2, 3]
    with pytest.raises(LookupError, match="has 1 visible page"):
        lint_task.prepare_input(db, "other")
    with pytest.raises(LookupError, match="has 0 visible page"):
        lint_task.prepare_input(db, "caching-free")


def test_limit_keeps_the_newest_pages(db):
    assert [p["bookmark_id"] for p in lint_task.prepare_input(db, "llm", limit=2)["pages"]] == [2, 3]


# ── run through the API runner ────────────────────────────────────────────


def test_run_records_checks_findings_and_usage(db):
    client = fake_client(message(lint_output(SUPERSEDES, CONTRADICTS), msg_id="msg_lint"))
    result = run_lint("llm", conn=db, runner=ApiRunner(client))

    assert result.status == "completed", result.errors
    kwargs = client.messages.stream.call_args.kwargs
    assert "You are the lint step of Muninn" in kwargs["system"]
    sent = kwargs["messages"][0]["content"]
    assert '"task_type": "lint"' in sent and '"tag": "llm"' in sent
    assert "intra.example" not in sent

    run = db.execute(
        "SELECT task_type, status, enrichment_prompt_version, container_id FROM synthesis_runs WHERE task_id = ?",
        (result.task_id,),
    ).fetchone()
    assert tuple(run) == ("lint", "completed", "lint_v1", "anthropic-api:msg_lint")
    checks = db.execute(
        "SELECT bookmark_id_a, bookmark_id_b, model, prompt_version, task_id, tag FROM lint_pair_checks ORDER BY 1, 2"
    ).fetchall()
    assert [tuple(c)[:2] for c in checks] == [(1, 2), (1, 3), (2, 3)]
    assert {tuple(c)[2:] for c in checks} == {("claude-opus-5-5", "lint_v1", result.task_id, "llm")}
    rows = db.execute(
        "SELECT relation, bookmark_id, other_bookmark_id, evidence_json FROM lint_findings ORDER BY relation"
    ).fetchall()
    assert [(r[0], r[1], r[2]) for r in rows] == [("contradicts", 1, 3), ("supersedes", 2, 1)]
    assert json.loads(rows[1]["evidence_json"]) == [
        {"bookmark_id": 2, "field": "text", "quote": "prompts are now cached for one hour"},
        {"bookmark_id": 1, "field": "text", "quote": "caches prompts for five minutes"},
    ]
    usage = db.execute("SELECT operation, model FROM usage_records").fetchall()
    assert [tuple(u) for u in usage] == [("synthesize:lint", "claude-opus-5-5")]


def test_rerun_with_unchanged_inputs_does_not_pay(db):
    assert run_lint("llm", conn=db, runner=ApiRunner(fake_client(message(lint_output())))).status == "completed"
    usage_before = _count(db, "usage_records")

    again = fake_client(message(lint_output()))
    result = run_lint("llm", conn=db, runner=ApiRunner(again))
    assert result.status == "up_to_date"
    assert again.messages.stream.call_count == 0
    assert _count(db, "usage_records") == usage_before
    assert _count(db, "synthesis_runs") == 1

    # A dated snapshot served for the requested alias counts as the same model.
    conn_model = db.execute("SELECT DISTINCT model FROM lint_pair_checks").fetchall()
    assert [r[0] for r in conn_model] == ["claude-opus-5-5"]
    db.execute("UPDATE lint_pair_checks SET model = 'claude-opus-5-5-20261001'")
    snap = fake_client(message(lint_output()))
    assert run_lint("llm", conn=db, runner=ApiRunner(snap)).status == "up_to_date"
    assert snap.messages.stream.call_count == 0

    # Another model has not checked these pairs: it would pay.
    other = fake_client(message(lint_output(), model="claude-sonnet-5-5"))
    assert run_lint("llm", conn=db, runner=ApiRunner(other, model="claude-sonnet-5-5")).status == "completed"
    assert other.messages.stream.call_count == 1

    # Changed content re-opens that page's pairs, for the same model too.
    db.execute("UPDATE scrape_results SET content_text = content_text || ' Updated.' WHERE bookmark_id = 1")
    db.commit()
    changed = fake_client(message(lint_output()))
    assert run_lint("llm", conn=db, runner=ApiRunner(changed)).status == "completed"
    assert changed.messages.stream.call_count == 1


def test_budget_refusal_makes_no_call_and_writes_no_findings(db, monkeypatch):
    client = fake_client(message(lint_output(SUPERSEDES)))
    budget = Budget(db, run_cap_micro_usd=1_000)  # $0.001: below any worst case
    result = run_lint("llm", conn=db, runner=ApiRunner(client), budget=budget)
    assert result.status == "cap_hit"
    assert client.messages.stream.call_count == 0
    assert runs(db, result.task_id) == [(1, "cap_hit")]
    assert _count(db, "lint_pair_checks") == _count(db, "lint_findings") == _count(db, "usage_records") == 0

    monkeypatch.setenv(MONTHLY_ENV, "0.001")
    result = run_lint("llm", conn=db, runner=ApiRunner(client))
    assert result.status == "cap_hit" and client.messages.stream.call_count == 0

    monkeypatch.delenv(MONTHLY_ENV)
    with pytest.raises(SynthesisConfigError, match=MONTHLY_ENV):
        run_lint("llm", conn=db, runner=ApiRunner(client))
    assert client.messages.stream.call_count == 0


@pytest.mark.parametrize(
    ("bad", "expected"),
    [
        (finding("contradicts", 1, 3, [(1, "caches prompts for five minutes"), (3, "caching always works")]),
         "findings[0].evidence[1]: the quote is not in bookmark 3's text or summary"),
        (finding("contradicts", 1, 3, [(1, "caches prompts"), (3, "Details [hidden link] soon")]),
         "findings[0].evidence[1]: the quote is not in bookmark 3's text"),
        (finding("contradicts", 1, 3, [(1, "caches prompts"), (1, "Version 1")]),
         "findings[0]: no verified evidence quote from bookmark 3"),
        (finding("supersedes", 1, 2, [(1, "caches prompts"), (2, "Version 2 replaces v1")]),
         "findings[0]: supersedes needs bookmark_id 1 (dated 2023-11-14T22:13:20Z) to be newer"),
        (finding("contradicts", 1, 4, [(1, "caches prompts"), (4, "secret plan")]),
         "findings[0]: bookmark id(s) [4] are not pages of this task"),
        (finding("contradicts", 2, 2, [(2, "Version 2"), (2, "one hour")]),
         "findings[0]: bookmark_id and other_bookmark_id are the same page"),
    ],
)
def test_evidence_and_pair_checks_fail_validation_then_retry(db, bad, expected):
    client = fake_client(message(lint_output(bad)), message(lint_output(SUPERSEDES)))
    result = run_lint("llm", conn=db, runner=ApiRunner(client))
    assert result.status == "completed"
    assert runs(db, result.task_id) == [(1, "validation_failed"), (2, "completed")]
    errors = db.execute(
        "SELECT validation_errors FROM synthesis_runs WHERE task_id = ? AND attempt = 1", (result.task_id,)
    ).fetchone()[0]
    assert any(e["error"].startswith(expected) for e in json.loads(errors)), errors
    retry_task = json.loads(client.messages.stream.call_args_list[1].kwargs["messages"][0]["content"].split("\n\n", 1)[1])
    assert expected.split(":")[0] in retry_task["correction_instructions"]
    assert [tuple(r) for r in db.execute("SELECT relation, bookmark_id FROM lint_findings")] == [("supersedes", 2)]


def test_whitespace_differences_in_quotes_are_tolerated_and_the_stored_span_kept(db):
    loose = finding("supersedes", 2, 1, [(2, "prompts are now\n cached   for one hour"), (1, "v1 caches prompts")])
    result = run_lint("llm", conn=db, runner=ApiRunner(fake_client(message(lint_output(loose)))))
    assert result.status == "completed"
    evidence = json.loads(db.execute("SELECT evidence_json FROM lint_findings").fetchone()[0])
    assert evidence[0]["quote"] == "prompts are now cached for one hour"
    assert evidence[1] == {"bookmark_id": 1, "field": "text", "quote": "v1 caches prompts"}


def test_both_attempts_invalid_writes_nothing(db):
    bad = finding("contradicts", 1, 3, [(1, "nope"), (3, "nope")])
    client = fake_client(message(lint_output(bad)), message(lint_output(bad)))
    result = run_lint("llm", conn=db, runner=ApiRunner(client))
    assert result.status == "validation_failed"
    assert _count(db, "lint_pair_checks") == _count(db, "lint_findings") == 0
    # Not recorded as checked, so the next run is not skipped.
    assert lint_task.pending_pairs(db, lint_task.prepare_input(db, "llm")["pages"], prompt_version="lint_v1")


def test_prompt_version_must_be_a_lint_prompt(db, tmp_path):
    with pytest.raises(ContractError, match="is not a lint prompt"):
        emit_lint_task("llm", tmp_path, conn=db, prompt_version="synthesis_v1")
    with pytest.raises(ContractError, match="unknown synthesis prompt version 'lint_v9'"):
        emit_lint_task("llm", tmp_path, conn=db, prompt_version="lint_v9", registry=tmp_path / "r")


# ── emit / ingest ─────────────────────────────────────────────────────────


def test_emit_then_ingest_writes_findings_and_later_emits_are_skipped(db, tmp_path):
    out, registry = tmp_path / "handoff", tmp_path / "registry"
    emitted = emit_lint_task("llm", out, conn=db, registry=registry)
    assert emitted.status == "emitted"
    task = json.loads(emitted.task_file.read_text())
    assert validate_task_input(task) == []
    assert task["task_type"] == "lint" and task["prompt_version"] == "lint_v1" and task["tag"] == "llm"
    assert task["instructions"].startswith("You are the lint step of Muninn")
    assert "intra.example" not in emitted.task_file.read_text()

    envelope = {
        "contract_version": CONTRACT_VERSION,
        "task_id": task["task_id"],
        "task_digest": task["task_digest"],
        "attempt": 1,
        "runner": "claude-code-local",
        "output": lint_output(CONTRADICTS),
    }
    result_file = out / result_filename(task["task_id"], 1)
    result_file.write_text(json.dumps(envelope))
    ingested = ingest_result(result_file, conn=db, registry=registry)
    assert ingested.status == "completed"
    assert [tuple(r) for r in db.execute("SELECT relation, bookmark_id, other_bookmark_id, model FROM lint_findings")] == [
        ("contradicts", 1, 3, "claude-opus-5-5")
    ]
    assert _count(db, "usage_records") == 0  # a harness's spend is not muninn's
    assert emit_lint_task("llm", out, conn=db, registry=registry).status == "up_to_date"


def test_ingested_invalid_result_emits_a_retry_with_the_lint_prompt(db, tmp_path):
    out, registry = tmp_path / "handoff", tmp_path / "registry"
    task = json.loads(emit_lint_task("llm", out, conn=db, registry=registry).task_file.read_text())
    bad = finding("contradicts", 1, 3, [(1, "nope"), (3, "nope")])
    path = out / "r.json"
    path.write_text(json.dumps({
        "contract_version": CONTRACT_VERSION, "task_id": task["task_id"],
        "task_digest": task["task_digest"], "attempt": 1, "output": lint_output(bad),
    }))
    res = ingest_result(path, conn=db, registry=registry, retry_dir=out)
    assert res.status == "validation_failed"
    retry = json.loads(res.task_file.read_text())
    assert (retry["attempt"], retry["prompt_version"], retry["tag"]) == (2, "lint_v1", "llm")
    assert retry["materials"] == task["materials"]


# ── CLI ───────────────────────────────────────────────────────────────────


def _invoke(db_path: Path, *args: str):
    return CliRunner().invoke(cli, ["--db", str(db_path), "synthesize", "lint", *args])


def test_cli_emit_run_and_errors(db, tmp_path, monkeypatch):
    seed_path = Path(db.execute("PRAGMA database_list").fetchone()[2])
    monkeypatch.setenv("MUNINN_DATA_DIR", str(tmp_path / "data"))

    result = _invoke(seed_path, "--tag", "llm", "--emit-task", str(tmp_path / "out"))
    assert result.exit_code == 0, result.output
    assert "Synthesize lint 'llm': task file" in result.output

    assert _invoke(seed_path, "--tag", "llm", "--emit-task", str(tmp_path / "o"), "--model", "m").exit_code == 2
    nope = _invoke(seed_path, "--tag", "nope")
    assert nope.exit_code == 1 and "has 0 visible page(s)" in nope.output
    bad = _invoke(seed_path, "--tag", "llm", "--prompt-version", "per_bookmark_v1")
    assert bad.exit_code == 1 and "is not a lint prompt" in bad.output

    monkeypatch.delenv(MONTHLY_ENV)
    unbudgeted = _invoke(seed_path, "--tag", "llm")
    assert unbudgeted.exit_code == 1 and MONTHLY_ENV in unbudgeted.output


def test_cli_run_reports_findings_then_up_to_date(db, monkeypatch):
    seed_path = Path(db.execute("PRAGMA database_list").fetchone()[2])
    client = fake_client(message(lint_output(SUPERSEDES)))
    monkeypatch.setattr("muninn.synthesis.api_runner.ApiRunner._get_client", lambda self: client)

    result = _invoke(seed_path, "--tag", "llm", "--max-cost-usd", "2")
    assert result.exit_code == 0, result.output
    assert "Synthesize lint 'llm': completed" in result.output
    assert "1 finding(s) recorded: run `muninn vault compile` to render them." in result.output

    again = _invoke(seed_path, "--tag", "llm")
    assert again.exit_code == 0
    assert "up to date, all 3 page pair(s) already checked" in again.output
    assert client.messages.stream.call_count == 1
