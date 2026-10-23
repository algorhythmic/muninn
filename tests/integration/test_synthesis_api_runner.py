"""The default synthesis runner: a budgeted, streamed Messages API call.

The Anthropic client is a MagicMock (tests/integration/synthesis_fakes.py):
no network, no model. Covers each task type, the request shape (streaming,
adaptive thinking, effort), measured-usage accounting into usage_records,
the per-run and monthly caps (cap_hit with no call made), and failure modes
recorded as runs rather than raised.
"""

from __future__ import annotations

import json
import time

import pytest
from click.testing import CliRunner

from muninn.consumers.cli.main import cli
from muninn.db import connect
from muninn.consumers.packets.schemas import schema_errors
from muninn.enrich.pricing import cost_micro_usd, price_for
from muninn.intake_records import usage_record
from muninn.synthesis import run_ad_hoc_analysis, run_deep_pass, run_era_narrative
from muninn.synthesis.api_runner import ApiRunner, task_message
from muninn.synthesis.budget import (
    Budget,
    SynthesisConfigError,
    month_start,
    monthly_spent_micro_usd,
)
from muninn.synthesis.contract import build_task
from muninn.synthesis.tasks import era_narrative
from tests.integration.synthesis_fakes import (
    adhoc_output,
    deep_pass_output,
    era_output,
    fake_client,
    message,
    populate,
    runs,
)

MONTHLY_ENV = "MUNINN_SYNTHESIS_MONTHLY_BUDGET_USD"
ONE_CALL = cost_micro_usd(price_for("claude-opus-5-5"), input_tokens=1000, output_tokens=500)


@pytest.fixture
def db(fresh_db, monkeypatch):
    monkeypatch.setenv(MONTHLY_ENV, "10")
    monkeypatch.delenv("MUNINN_SYNTHESIS_MODEL", raising=False)
    monkeypatch.delenv("MUNINN_SYNTHESIS_EFFORT", raising=False)
    return populate(fresh_db)


def _usage_rows(conn) -> list:
    return conn.execute("SELECT * FROM usage_records ORDER BY usage_record_id").fetchall()


def _seed_usage(conn, *, cost: int, operation: str = "synthesize:era_narrative", occurred_at: int | None = None):
    conn.execute(
        "INSERT INTO usage_records (usage_ulid, kind, provider, operation, model, request_count, "
        "cost_micro_usd, cost_basis, occurred_at) VALUES (?, 'model', 'anthropic', ?, "
        "'claude-opus-5-5', 1, ?, 'estimated', ?)",
        (f"01J{len(_usage_rows(conn)):023d}", operation, cost, occurred_at or int(time.time())),
    )
    conn.commit()


# ── each task type through the API runner ─────────────────────────────────


def test_era_narrative_runs_streamed_with_adaptive_thinking(db):
    client = fake_client(message(era_output(), msg_id="msg_era"))
    runner = ApiRunner(client)

    result = run_era_narrative("early-web", conn=db, runner=runner)

    assert result.status == "completed"
    kwargs = client.messages.stream.call_args.kwargs
    assert kwargs["model"] == "claude-opus-5-5"
    assert kwargs["thinking"] == {"type": "adaptive"}
    assert kwargs["output_config"] == {"effort": "high"}
    assert kwargs["max_tokens"] == runner.max_output_tokens
    assert "verbatim substring" in kwargs["system"]  # the task's instructions
    sent = kwargs["messages"][0]["content"]
    assert '"era_label": "early-web"' in sent and '"result_schema"' in sent
    assert '"instructions"' not in sent  # sent as the system prompt, not twice

    row = db.execute("SELECT narrative, enrichment_model FROM eras").fetchone()
    assert row["enrichment_model"] == "claude-opus-5-5"  # the served model, not the model's claim
    run = db.execute(
        "SELECT status, enrichment_model, input_token_count, output_token_count, container_id "
        "FROM synthesis_runs WHERE task_id = ?", (result.task_id,),
    ).fetchone()
    assert tuple(run) == ("completed", "claude-opus-5-5", 1000, 500, "anthropic-api:msg_era")
    # synthesis_metadata carries the measured counts, not the model's guesses.
    assert result.output["synthesis_metadata"]["input_token_count"] == 1000

    (usage,) = _usage_rows(db)
    assert usage["operation"] == "synthesize:era_narrative"
    assert usage["model"] == "claude-opus-5-5" and usage["cost_basis"] == "estimated"
    assert (usage["input_token_count"], usage["output_token_count"]) == (1000, 500)
    assert usage["cost_micro_usd"] == ONE_CALL == 14_000
    assert schema_errors("usage-record", usage_record(usage)) == []


def test_deep_pass_through_api_runner_keeps_d6_and_writes_cross_refs(db):
    db.execute(
        "INSERT INTO enriched (bookmark_id, summary, tags, entities, content_type, language, "
        "deep_pass_requested, enrichment_model, enrichment_prompt_version, content_hash, enriched_at) "
        "VALUES (1, 'bulk', '[]', '[]', 'article', 'en', 1, 'claude-haiku-4-5-20251001', "
        "'per_bookmark_v1', 'hash-1', 1700000000)"
    )
    db.commit()
    client = fake_client(message(deep_pass_output(2)))

    result = run_deep_pass(1, conn=db, runner=ApiRunner(client))

    assert result.status == "completed"
    row = db.execute(
        "SELECT summary, deep_pass_requested, enrichment_model, enrichment_prompt_version, "
        "content_hash, enriched_at FROM enriched WHERE bookmark_id = 1"
    ).fetchone()
    assert row["summary"] == "S" * 120 and row["deep_pass_requested"] == 0
    # D6: the bulk enrichment triple and timestamp survive the deep pass.
    assert tuple(row)[2:] == ("claude-haiku-4-5-20251001", "per_bookmark_v1", "hash-1", 1700000000)
    assert db.execute("SELECT created_by FROM cross_references").fetchone()[0] == "deep_pass"
    assert _usage_rows(db)[0]["operation"] == "synthesize:deep_pass"


def test_ad_hoc_analysis_through_api_runner(db):
    client = fake_client(message(adhoc_output()))
    result = run_ad_hoc_analysis("what themes?", conn=db, runner=ApiRunner(client), title="T")
    assert result.status == "completed"
    assert tuple(db.execute("SELECT title, prompt FROM analyses").fetchone()) == ("T", "what themes?")
    assert _usage_rows(db)[0]["operation"] == "synthesize:ad_hoc_analysis"


def test_model_and_effort_come_from_env(db, monkeypatch):
    monkeypatch.setenv("MUNINN_SYNTHESIS_MODEL", "claude-sonnet-5-5")
    monkeypatch.setenv("MUNINN_SYNTHESIS_EFFORT", "medium")
    client = fake_client(message(era_output(), model="claude-sonnet-5-5"))
    run_era_narrative("early-web", conn=db, runner=ApiRunner(client))
    kwargs = client.messages.stream.call_args.kwargs
    assert (kwargs["model"], kwargs["output_config"]) == ("claude-sonnet-5-5", {"effort": "medium"})
    assert _usage_rows(db)[0]["cost_micro_usd"] == cost_micro_usd(
        price_for("claude-sonnet-5-5"), input_tokens=1000, output_tokens=500
    )


def test_unpriced_model_or_bad_effort_refuses_to_start():
    with pytest.raises(SynthesisConfigError, match="no price"):
        ApiRunner(model="claude-unknown-9")
    with pytest.raises(SynthesisConfigError, match="effort"):
        ApiRunner(effort="extreme")


# ── budget: worst case before, measured cost after ────────────────────────


def test_per_run_cap_stops_before_any_call(db):
    client = fake_client(message(era_output()))
    runner = ApiRunner(client)
    budget = Budget(db, run_cap_micro_usd=100_000)  # $0.10 < the worst case

    result = run_era_narrative("early-web", conn=db, runner=runner, budget=budget)

    assert result.status == "cap_hit"
    client.messages.stream.assert_not_called()
    assert runs(db, result.task_id) == [(1, "cap_hit")]
    evidence = json.loads(db.execute(
        "SELECT validation_errors FROM synthesis_runs WHERE task_id = ?", (result.task_id,)
    ).fetchone()[0])[0]
    assert evidence["budget"] == "per-run cap"
    assert _usage_rows(db) == [] and db.execute("SELECT COUNT(*) FROM eras").fetchone()[0] == 0


def test_monthly_cap_counts_this_months_synthesis_usage_only(db, monkeypatch):
    monkeypatch.setenv(MONTHLY_ENV, "1.00")
    now = int(time.time())
    _seed_usage(db, cost=950_000)  # this month, synthesis: counts
    _seed_usage(db, cost=5_000_000, operation="enrich:suitability")  # not synthesis
    _seed_usage(db, cost=5_000_000, occurred_at=month_start(now) - 1)  # last month
    assert monthly_spent_micro_usd(db, now) == 950_000

    client = fake_client(message(era_output()))
    result = run_era_narrative("early-web", conn=db, runner=ApiRunner(client))

    assert result.status == "cap_hit"
    client.messages.stream.assert_not_called()
    evidence = json.loads(db.execute(
        "SELECT validation_errors FROM synthesis_runs WHERE task_id = ?", (result.task_id,)
    ).fetchone()[0])[0]
    assert evidence["budget"] == "monthly cap" and evidence["month_remaining"] == "$0.0500"


def test_missing_monthly_cap_refuses_to_spend(db, monkeypatch):
    monkeypatch.delenv(MONTHLY_ENV)
    client = fake_client(message(era_output()))
    with pytest.raises(SynthesisConfigError, match=MONTHLY_ENV):
        run_era_narrative("early-web", conn=db, runner=ApiRunner(client))
    client.messages.stream.assert_not_called()
    assert db.execute("SELECT COUNT(*) FROM synthesis_runs").fetchone()[0] == 0


def test_shared_run_budget_is_charged_measured_cost(db):
    client = fake_client(message(deep_pass_output(2)), message(deep_pass_output(3)))
    runner = ApiRunner(client)
    budget = Budget(db, run_cap_micro_usd=10_000_000)
    run_deep_pass(1, conn=db, runner=runner, budget=budget)
    run_deep_pass(1, conn=db, runner=runner, budget=budget)
    # The run is charged what was measured, not the (much larger) worst case.
    assert budget.spent_micro_usd == 2 * ONE_CALL
    assert sum(r["cost_micro_usd"] for r in _usage_rows(db)) == 2 * ONE_CALL


def test_worst_case_bounds_the_request(db):
    runner = ApiRunner(fake_client())
    task = build_task(task_id="era-x", task_type="era_narrative", attempt=1,
                      prompt_version="synthesis_v1",
                      materials=era_narrative.prepare_input(db, "early-web"),
                      extra={"era_label": "early-web"})
    chars = len(task["instructions"]) + len(task_message(task))
    price = price_for("claude-opus-5-5")
    # Every input char billed as half a token at the cache-write rate, plus full max_tokens.
    assert runner.estimate(task) >= (chars // 2) * price.cache_write // 1_000_000 + 32_000 * 20
    assert runner.estimate(task) > ONE_CALL


# ── failures are recorded runs, not crashes ───────────────────────────────


def test_invalid_output_twice_is_validation_failed_with_both_calls_paid(db):
    bad = dict(era_output(), narrative="too short")
    client = fake_client(message(bad), message(bad))

    result = run_era_narrative("early-web", conn=db, runner=ApiRunner(client))

    assert result.status == "validation_failed"
    assert client.messages.stream.call_count == 2
    retry_prompt = client.messages.stream.call_args_list[1].kwargs["messages"][0]["content"]
    assert "Your previous output had validation errors" in retry_prompt
    assert runs(db, result.task_id) == [(1, "validation_failed"), (2, "validation_failed")]
    assert len(_usage_rows(db)) == 2
    assert db.execute("SELECT COUNT(*) FROM eras").fetchone()[0] == 0


def test_non_json_reply_then_valid_retry_completes(db):
    client = fake_client(message("I could not do it."), message(era_output()))
    result = run_era_narrative("early-web", conn=db, runner=ApiRunner(client))
    assert result.status == "completed"
    assert runs(db, result.task_id) == [(1, "validation_failed"), (2, "completed")]
    first = json.loads(db.execute(
        "SELECT validation_errors FROM synthesis_runs WHERE task_id = ? AND attempt = 1",
        (result.task_id,),
    ).fetchone()[0])
    assert "not a JSON object" in first[0]["error"]


def test_truncated_reply_is_a_validation_failure(db):
    client = fake_client(message(era_output(), stop_reason="max_tokens"), message(era_output()))
    result = run_era_narrative("early-web", conn=db, runner=ApiRunner(client))
    assert result.status == "completed" and client.messages.stream.call_count == 2
    assert "truncated" in client.messages.stream.call_args_list[1].kwargs["messages"][0]["content"]


def test_refusal_is_a_failed_run_without_retry(db):
    client = fake_client(message("", stop_reason="refusal"))
    result = run_era_narrative("early-web", conn=db, runner=ApiRunner(client))
    assert result.status == "container_failed"
    assert client.messages.stream.call_count == 1
    assert len(_usage_rows(db)) == 1  # a refused call is still billed


def test_api_error_is_a_failed_run_with_no_usage(db):
    client = fake_client(RuntimeError("connection reset"))
    result = run_era_narrative("early-web", conn=db, runner=ApiRunner(client))
    assert result.status == "container_failed"
    assert "connection reset" in result.errors[0]
    assert runs(db, result.task_id) == [(1, "container_failed")]
    assert _usage_rows(db) == []


def test_unmeasurable_usage_is_charged_the_worst_case(db):
    msg = message(era_output())
    msg.usage = None
    runner = ApiRunner(fake_client(msg))
    result = run_era_narrative("early-web", conn=db, runner=runner)
    assert result.status == "completed"
    (usage,) = _usage_rows(db)
    assert usage["cost_micro_usd"] > ONE_CALL and usage["input_token_count"] is None


# ── CLI default path ──────────────────────────────────────────────────────


def _cli_db(db_path):
    conn = connect(db_path)
    populate(conn)
    conn.close()


def test_cli_default_run_uses_the_api_runner(db_path, monkeypatch):
    _cli_db(db_path)
    monkeypatch.setenv(MONTHLY_ENV, "10")
    client = fake_client(message(era_output()))
    monkeypatch.setattr(ApiRunner, "_get_client", lambda self: client)

    res = CliRunner().invoke(cli, ["--db", str(db_path), "synthesize", "era", "early-web"])

    assert res.exit_code == 0, res.output
    assert "completed" in res.output
    client.messages.stream.assert_called_once()


def test_cli_max_cost_usd_stops_the_pending_queue(db_path, monkeypatch):
    _cli_db(db_path)
    conn = connect(db_path)
    for bid in (1, 2):
        conn.execute(
            "INSERT INTO enriched (bookmark_id, summary, tags, entities, deep_pass_requested, "
            "enrichment_model, enrichment_prompt_version, content_hash, enriched_at) "
            "VALUES (?, 's', '[]', '[]', 1, 'm', 'p', 'h', 0)", (bid,),
        )
    conn.commit()
    conn.close()
    monkeypatch.setenv(MONTHLY_ENV, "10")
    client = fake_client()
    monkeypatch.setattr(ApiRunner, "_get_client", lambda self: client)

    res = CliRunner().invoke(cli, ["--db", str(db_path), "synthesize", "deep-pass", "--pending",
                                   "--max-cost-usd", "0.01"])

    assert res.exit_code == 0, res.output
    assert "cap_hit" in res.output and "1 pending row(s) not attempted" in res.output
    client.messages.stream.assert_not_called()


def test_cli_without_monthly_cap_is_a_one_line_error(db_path, monkeypatch):
    _cli_db(db_path)
    monkeypatch.delenv(MONTHLY_ENV, raising=False)
    res = CliRunner().invoke(cli, ["--db", str(db_path), "synthesize", "era", "early-web"])
    assert res.exit_code != 0 and MONTHLY_ENV in res.output and "Traceback" not in res.output


def test_cli_unknown_model_is_refused(db_path):
    res = CliRunner().invoke(cli, ["--db", str(db_path), "synthesize", "era", "x", "--model", "nope"])
    assert res.exit_code != 0 and "no price" in res.output
