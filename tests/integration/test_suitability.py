"""``muninn enrich --task suitability``: selection, idempotency, coverage
rule, budget, failure isolation and the CLI surface. All data synthetic;
the Anthropic client is a MagicMock (tests/integration/intake_fakes.py)."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from muninn.consumers.cli.main import cli
from muninn.consumers.packets import record_digest
from muninn.consumers.packets.records import assessment_record, usage_record
from muninn.consumers.packets.schemas import schema_errors
from muninn.enrich.interest_profile import (
    EXAMPLE_PROFILE,
    InterestProfileError,
    load_interest_profile,
)
from muninn.enrich.pricing import PRICES, cost_micro_usd, usd_to_micro
from muninn.enrich.suitability import SuitabilityError, assess_suitability
from tests.integration.intake_fakes import (
    PROFILE_V1,
    PROFILE_V2,
    fake_client,
    judgement,
    response,
    write_profile,
)
from tests.integration.intake_seed import add_revision, seed_link, seed_x_post

T0 = 1_790_000_000  # 2026-09-21, synthetic
POST_A, POST_B, POST_C, POST_D, POST_H = (f"19000000000000000{n:02d}" for n in (11, 12, 13, 14, 15))
A, B, C, D, H = (f"x:post:{p}" for p in (POST_A, POST_B, POST_C, POST_D, POST_H))
MODEL = "claude-haiku-4-5-20251001"


@pytest.fixture
def corpus(fresh_db: sqlite3.Connection) -> sqlite3.Connection:
    """Five bookmarked posts, newest first: A (full, three links), B (full),
    C (blocked post with a readable link), D (deleted, nothing readable),
    H (hidden by domain policy)."""
    a = seed_x_post(fresh_db, POST_A, "Synthetic thread on evaluating retrieval over small corpora.",
                    captured_at=T0 + 500, published_at=T0 - 86400)
    seed_link(fresh_db, a, position=0,
              unwound_url="https://example.com/articles/notes?utm_source=twitter",
              text="Hybrid retrieval improved recall at ten.", title="Synthetic notes",
              retrieved_at=T0 + 510)
    seed_link(fresh_db, a, position=1, expanded_url="https://github.com/Synthetic-Org/synthetic-repo",
              text="synthetic-repo\n\nA toolkit.", coverage="partial", retrieved_at=T0 + 520)
    seed_link(fresh_db, a, position=2, short_url="https://t.co/SynthB0003", coverage="blocked",
              http_status=403, retrieved_at=T0 + 530)
    seed_x_post(fresh_db, POST_B, "Synthetic note: SQLite STRICT tables and migrations.",
                captured_at=T0 + 400, published_at=T0 - 3600)
    c = seed_x_post(fresh_db, POST_C, "", coverage="blocked", captured_at=T0 + 300, published_at=T0 - 7200)
    seed_link(fresh_db, c, position=0, unwound_url="https://example.org/benchmarks",
              text="A synthetic benchmark write-up.", retrieved_at=T0 + 310)
    seed_x_post(fresh_db, POST_D, "", coverage="deleted", captured_at=T0 + 200, published_at=T0 - 9000)
    seed_x_post(fresh_db, POST_H, "Hidden synthetic post.", captured_at=T0 + 100,
                published_at=T0 - 9000, content_visible=0)
    return fresh_db


@pytest.fixture
def profile(tmp_path: Path):
    return load_interest_profile(write_profile(tmp_path))


def _count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _safe_for_c(item: str):
    return judgement("needs_evidence", relevance=40, confidence=20) if item == C else judgement()


# ── Happy path, rows, records ────────────────────────────────────────


def test_assesses_selected_items_and_writes_contract_rows(corpus, profile) -> None:
    client = fake_client(default=_safe_for_c)
    run = assess_suitability(corpus, profile, client, model=MODEL, now=T0 + 1000)

    assert run.api_calls == 3 and run.assessed == 3
    assert {o.item_id for o in run.outcomes if o.status == "assessed"} == {A, B, C}
    assert run.skipped_no_content == 1  # D: deleted, nothing readable anywhere
    assert H not in {o.item_id for o in run.outcomes}  # hidden: never selected
    assert client.messages.create.call_count == 3

    # One usage row per call, priced from measured usage (1000 in, 200 out).
    expected = cost_micro_usd(PRICES[MODEL], input_tokens=1000, output_tokens=200)
    assert expected == 2000
    rows = corpus.execute("SELECT * FROM usage_records ORDER BY usage_record_id").fetchall()
    assert len(rows) == 3
    for row in rows:
        assert (row["kind"], row["provider"], row["operation"], row["model"]) == ("model", "anthropic", "enrich:suitability", MODEL)
        assert row["cost_micro_usd"] == expected and row["cost_basis"] == "estimated"
        assert row["assessment_id"] is not None
        assert schema_errors("usage-record", usage_record(corpus, row)) == []
    assert run.spent_micro_usd == 3 * expected

    # The profile revision is recorded, and every assessment is a valid record.
    assert corpus.execute("SELECT profile_sha256 FROM interest_profiles").fetchall()[0][0] == profile.digest
    for row in corpus.execute("SELECT * FROM assessments"):
        rec = assessment_record(corpus, row)
        assert schema_errors("assessment", rec) == []
        assert (rec["policy_version"], rec["prompt_version"], rec["model"]) == ("intake-policy/1", "suitability_v1", MODEL)


def test_the_model_sees_coverage_and_linked_items(corpus, profile) -> None:
    client = fake_client(default=_safe_for_c)
    assess_suitability(corpus, profile, client, model=MODEL, now=T0 + 1000, limit=1)
    kwargs = client.messages.create.call_args.kwargs
    message = kwargs["messages"][0]["content"]
    assert message.startswith(f"SUBJECT ITEM\nitem_id: {A}")
    assert "coverage: full (via x_api" in message
    assert "LINKED ITEMS (3)" in message
    assert "coverage: partial (via live" in message
    assert "coverage: blocked (no readable content captured" in message
    assert "github:repo:synthetic-org/synthetic-repo" in message
    # System prompt + profile, with the cache breakpoint on the last block.
    system = kwargs["system"]
    assert system[-1]["cache_control"] == {"type": "ephemeral"}
    assert '"profile_id": "test-research"' in system[-1]["text"]
    assert kwargs["model"] == MODEL


# ── Idempotency and reassessment ─────────────────────────────────────


def test_identical_key_is_skipped(corpus, profile) -> None:
    assess_suitability(corpus, profile, fake_client(default=_safe_for_c), model=MODEL, now=T0 + 1000)
    client = fake_client(default=_safe_for_c)
    run = assess_suitability(corpus, profile, client, model=MODEL, now=T0 + 2000)
    assert client.messages.create.call_count == 0
    assert run.skipped_idempotent == 3 and run.assessed == 0
    assert _count(corpus, "assessments") == 3 and _count(corpus, "usage_records") == 3


def test_changed_profile_appends_a_second_assessment(corpus, profile, tmp_path) -> None:
    assess_suitability(corpus, profile, fake_client(default=_safe_for_c), model=MODEL, now=T0 + 1000, limit=1)
    first = corpus.execute("SELECT * FROM assessments").fetchone()
    first_rec = assessment_record(corpus, first)

    v2 = load_interest_profile(write_profile(tmp_path, PROFILE_V2, "v2.yml"))
    assert v2.digest != profile.digest
    run = assess_suitability(corpus, v2, fake_client(default=_safe_for_c), model=MODEL, now=T0 + 2000, limit=1)
    assert run.assessed == 1

    rows = corpus.execute("SELECT * FROM assessments WHERE item_ref = ? ORDER BY assessment_id", (A,)).fetchall()
    assert len(rows) == 2
    assert rows[0]["profile_sha256"] == profile.digest and rows[1]["profile_sha256"] == v2.digest
    assert rows[0]["scrape_history_id"] == rows[1]["scrape_history_id"]
    assert assessment_record(corpus, rows[0]) == first_rec  # the old row is untouched
    second = assessment_record(corpus, rows[1])
    assert second["prior_assessment"] == {"ref": first_rec["assessment_id"], "digest": record_digest(first_rec)}
    assert _count(corpus, "interest_profiles") == 2


def test_changed_model_or_content_reassesses(corpus, profile) -> None:
    assess_suitability(corpus, profile, fake_client(), model=MODEL, now=T0 + 1000, limit=1)
    run = assess_suitability(corpus, profile, fake_client(), model="claude-sonnet-4-6", now=T0 + 1100, limit=1)
    assert run.assessed == 1 and run.outcomes[0].item_id == A

    post_a = corpus.execute("SELECT bookmark_id FROM bookmarks WHERE source_id = ?", (POST_A,)).fetchone()[0]
    from tests.integration.intake_seed import Seeded

    add_revision(corpus, Seeded(post_a, A), "Edited synthetic thread with corrected numbers.",
                 retrieval_method="x_api", retrieved_at=T0 + 1200, observed_at=T0 + 500)
    run = assess_suitability(corpus, profile, fake_client(), model=MODEL, now=T0 + 1300, limit=1)
    assert run.assessed == 1
    assert corpus.execute("SELECT COUNT(DISTINCT scrape_history_id) FROM assessments WHERE item_ref = ?", (A,)).fetchone()[0] == 2


# ── No candidate without content ─────────────────────────────────────


def test_blocked_capture_cannot_be_a_candidate(corpus, profile) -> None:
    client = fake_client({C: judgement("candidate")})
    run = assess_suitability(corpus, profile, client, model=MODEL, now=T0 + 1000)
    outcome = next(o for o in run.outcomes if o.item_id == C)
    assert outcome.status == "invalid_output"
    assert "disposition" in outcome.detail
    assert run.invalid_output == 1 and run.assessed == 2  # the run went on
    assert corpus.execute("SELECT COUNT(*) FROM assessments WHERE item_ref = ?", (C,)).fetchone()[0] == 0
    # The paid call still has its usage row, linked to no assessment.
    assert corpus.execute("SELECT COUNT(*) FROM usage_records WHERE assessment_id IS NULL").fetchone()[0] == 1

    # A non-candidate judgement of the same blocked post is accepted, coverage shown.
    run = assess_suitability(corpus, profile, fake_client(default=_safe_for_c), model=MODEL, now=T0 + 2000)
    row = corpus.execute("SELECT * FROM assessments WHERE item_ref = ?", (C,)).fetchone()
    assert (row["content_coverage"], row["disposition"]) == ("blocked", "needs_evidence")


def test_nothing_readable_means_no_call(corpus, profile) -> None:
    client = fake_client()
    run = assess_suitability(corpus, profile, client, model=MODEL, sources=("x",), now=T0, since=T0 + 150, until=T0 + 250)
    assert run.skipped_no_content == 1 and run.outcomes[0].item_id == D
    assert client.messages.create.call_count == 0


@pytest.mark.parametrize(
    "bad, reason",
    [
        ("not json at all", "not valid JSON"),
        (judgement(relevance=0.8), "relevance"),
        (judgement(disposition="blocked"), "disposition"),
        (judgement(matched_interests=["not-in-profile"]), "matched_interests"),
        ({"disposition": "candidate"}, "missing"),
    ],
)
def test_schema_invalid_output_is_a_recorded_failure(corpus, profile, bad, reason) -> None:
    run = assess_suitability(corpus, profile, fake_client({A: bad}, default=_safe_for_c), model=MODEL, now=T0 + 1000)
    outcome = next(o for o in run.outcomes if o.item_id == A)
    assert outcome.status == "invalid_output" and reason in outcome.detail
    assert run.assessed == 2 and run.api_calls == 3
    assert corpus.execute("SELECT COUNT(*) FROM assessments WHERE item_ref = ?", (A,)).fetchone()[0] == 0


def test_truncated_output_is_a_failure(corpus, profile) -> None:
    trunc = response(judgement(), stop_reason="max_tokens")
    run = assess_suitability(corpus, profile, fake_client({A: trunc}, default=_safe_for_c), model=MODEL, now=T0 + 1000, limit=1)
    assert run.outcomes[0].status == "invalid_output" and "max_tokens" in run.outcomes[0].detail


# ── Budget, limits, dry run ──────────────────────────────────────────


def test_dry_run_makes_no_calls_and_writes_nothing(corpus, profile) -> None:
    client = fake_client()
    run = assess_suitability(corpus, profile, client, model=MODEL, dry_run=True)
    assert client.messages.create.call_count == 0
    assert run.planned == 3 and [o.status for o in run.outcomes].count("planned") == 3
    assert run.estimated_micro_usd == sum(o.cost_micro_usd for o in run.outcomes if o.status == "planned") > 0
    for table in ("assessments", "usage_records", "interest_profiles"):
        assert _count(corpus, table) == 0


def test_dry_run_needs_no_sdk(corpus, profile, monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "anthropic", None)  # import anthropic -> ImportError
    run = assess_suitability(corpus, profile, None, model=MODEL, dry_run=True)
    assert run.planned == 3
    with pytest.raises(SuitabilityError, match="anthropic package is not installed"):
        assess_suitability(corpus, profile, None, model=MODEL)


def test_budget_stops_before_a_call_that_could_exceed_it(corpus, profile) -> None:
    plan = assess_suitability(corpus, profile, fake_client(), model=MODEL, dry_run=True)
    worst = [o.cost_micro_usd for o in plan.outcomes if o.status == "planned"]
    per_call = cost_micro_usd(PRICES[MODEL], input_tokens=0, output_tokens=1000)  # 5000 µ$
    budget = per_call + worst[1]  # room for two calls, not three

    client = fake_client(default=_safe_for_c, input_tokens=0, output_tokens=1000)
    run = assess_suitability(corpus, profile, client, model=MODEL, max_cost_micro_usd=budget, now=T0 + 1000)
    assert client.messages.create.call_count == 2
    assert run.stopped == "budget" and run.assessed == 2
    assert run.outcomes[-1].status == "not_attempted"
    assert _count(corpus, "usage_records") == 2
    assert corpus.execute("SELECT SUM(cost_micro_usd) FROM usage_records").fetchone()[0] == 2 * per_call <= budget


def test_budget_too_small_for_one_call(corpus, profile) -> None:
    client = fake_client()
    run = assess_suitability(corpus, profile, client, model=MODEL, max_cost_micro_usd=10, now=T0)
    assert client.messages.create.call_count == 0 and run.stopped == "budget"
    assert _count(corpus, "usage_records") == 0


def test_limit_counts_calls_not_skips(corpus, profile) -> None:
    assess_suitability(corpus, profile, fake_client(), model=MODEL, now=T0 + 1000, limit=1)  # A
    client = fake_client(default=_safe_for_c)
    run = assess_suitability(corpus, profile, client, model=MODEL, now=T0 + 2000, limit=1)
    assert run.skipped_idempotent == 1 and run.assessed == 1 and run.outcomes[-1].item_id == B


def test_selection_by_source_and_observation_window(corpus, profile) -> None:
    client = fake_client(default=_safe_for_c)
    run = assess_suitability(corpus, profile, client, model=MODEL, since=T0 + 350, until=T0 + 450, now=T0)
    assert [o.item_id for o in run.outcomes] == [B]
    run = assess_suitability(corpus, profile, fake_client(), model=MODEL, sources=("x_link",), limit=10, now=T0)
    assert run.assessed == 3 and all(not o.item_id.startswith("x:post:") for o in run.outcomes)
    assert run.skipped_no_content == 1  # the blocked t.co page


def test_unknown_model_price_refuses_to_run(corpus, profile) -> None:
    with pytest.raises(SuitabilityError, match="no price"):
        assess_suitability(corpus, profile, fake_client(), model="claude-unknown-1")


def test_an_api_error_is_isolated(corpus, profile) -> None:
    client = fake_client({A: RuntimeError("synthetic outage")}, default=_safe_for_c)
    run = assess_suitability(corpus, profile, client, model=MODEL, now=T0, limit=3)
    assert run.api_errors == 1 and run.assessed == 2 and run.stopped is None
    assert next(o for o in run.outcomes if o.item_id == A).status == "api_error"
    assert _count(corpus, "usage_records") == 2  # no usage is known for the failed call


def test_an_outage_stops_the_run_after_three_errors(corpus, profile) -> None:
    outage = fake_client(default=RuntimeError("synthetic outage"))
    run = assess_suitability(corpus, profile, outage, model=MODEL, sources=("x", "x_link"), limit=10, now=T0)
    assert run.planned > 3
    assert outage.messages.create.call_count == 3
    assert run.stopped == "api_errors" and run.api_errors == 3
    assert [o.status for o in run.outcomes].count("not_attempted") == run.planned - 3
    assert _count(corpus, "assessments") == 0


@pytest.mark.parametrize(
    "module",
    ["muninn.enrich.suitability", "muninn.consumers.packets.export", "muninn.consumers.cli.main", "muninn.ingest"],
)
def test_no_sdk_import_until_a_call_is_made(module) -> None:
    """Failure isolation: importing the task, the exporter, the CLI or
    capture never loads the model SDK or the heavy consumers."""
    import os
    import subprocess

    heavy = ("anthropic", "qdrant_client", "duckdb", "mcp", "jinja2", "torch", "sentence_transformers")
    code = (
        "import importlib, json, sys\n"
        f"importlib.import_module({module!r})\n"
        f"print(json.dumps(sorted(m for m in {heavy!r} if m in sys.modules)))\n"
    )
    src = str(Path(__file__).resolve().parents[2] / "src")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                         env={**os.environ, "PYTHONPATH": src})
    assert json.loads(out.stdout) == []


def test_usd_parsing() -> None:
    assert usd_to_micro("1") == 1_000_000 and usd_to_micro("0.25") == 250_000
    with pytest.raises(ValueError):
        usd_to_micro("-1")


# ── Interest profile ─────────────────────────────────────────────────


def test_profile_revision_ignores_formatting(tmp_path) -> None:
    a = load_interest_profile(write_profile(tmp_path, PROFILE_V1, "a.yml"))
    reformatted = "# another comment\n\n" + PROFILE_V1.replace(
        "label: Retrieval evaluation", 'label: "Retrieval evaluation"    # quoted now'
    )
    assert reformatted != PROFILE_V1
    b = load_interest_profile(write_profile(tmp_path, reformatted, "b.yml"))
    assert a.digest == b.digest == record_digest(a.record)
    assert load_interest_profile(write_profile(tmp_path, PROFILE_V2, "c.yml")).digest != a.digest


@pytest.mark.parametrize("text", ["weight: 0.8", "profile_id: Not A Slug", "[]"])
def test_invalid_profiles_are_rejected(tmp_path, text) -> None:
    body = text if text == "[]" else PROFILE_V1.replace("weight: 80" if "weight" in text else "profile_id: test-research", text)
    with pytest.raises(InterestProfileError):
        load_interest_profile(write_profile(tmp_path, body))


def test_profile_path_comes_from_env(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("MUNINN_INTEREST_PROFILE", raising=False)
    with pytest.raises(InterestProfileError, match="MUNINN_INTEREST_PROFILE"):
        load_interest_profile()
    monkeypatch.setenv("MUNINN_INTEREST_PROFILE", str(write_profile(tmp_path)))
    assert load_interest_profile().profile_id == "test-research"


def test_shipped_example_is_a_valid_synthetic_profile() -> None:
    example = load_interest_profile(EXAMPLE_PROFILE)
    assert example.profile_id == "example-research"
    assert "EXAMPLE ONLY (synthetic)" in EXAMPLE_PROFILE.read_text()


# ── CLI ──────────────────────────────────────────────────────────────


def _db_file(corpus: sqlite3.Connection) -> str:
    return corpus.execute("PRAGMA database_list").fetchone()["file"]


def test_cli_suitability_dry_run(corpus, tmp_path) -> None:
    profile_path = write_profile(tmp_path)
    result = CliRunner().invoke(
        cli, ["--db", _db_file(corpus), "enrich", "--task", "suitability", "--dry-run",
              "--profile", str(profile_path), "--limit", "2", "--max-cost-usd", "0.50"],
    )
    assert result.exit_code == 0, result.output
    assert "Suitability plan:" in result.output
    summary = json.loads(result.output.split("Suitability plan: ", 1)[1])
    assert summary["planned"] == 2 and summary["budget"] == "$0.5000" and summary["api_calls"] == 0
    assert _count(corpus, "assessments") == 0


def test_cli_suitability_needs_a_profile(corpus, monkeypatch) -> None:
    monkeypatch.delenv("MUNINN_INTEREST_PROFILE", raising=False)
    result = CliRunner().invoke(cli, ["--db", _db_file(corpus), "enrich", "--task", "suitability", "--dry-run"])
    assert result.exit_code != 0 and "MUNINN_INTEREST_PROFILE" in result.output


def test_cli_default_enrich_is_unchanged(corpus) -> None:
    with patch("muninn.enrich.pipeline.enrich_all") as enrich_all:
        enrich_all.return_value = "stats"
        result = CliRunner().invoke(cli, ["--db", _db_file(corpus), "enrich", "--dry-run"])
    assert result.exit_code == 0, result.output
    _conn, = enrich_all.call_args.args
    assert enrich_all.call_args.kwargs == {"dry_run": True, "force": False, "prompt_version": None}
    assert "Enrich complete: stats" in result.output


@pytest.mark.parametrize("flags", [["--limit", "2"], ["--source", "x"], ["--max-cost-usd", "1"], ["--model", MODEL]])
def test_cli_suitability_flags_need_the_task(corpus, flags) -> None:
    with patch("muninn.enrich.pipeline.enrich_all") as enrich_all:
        result = CliRunner().invoke(cli, ["--db", _db_file(corpus), "enrich", *flags])
    assert result.exit_code == 2 and "only apply to --task suitability" in result.output
    enrich_all.assert_not_called()


def test_cli_force_is_rejected_for_suitability(corpus) -> None:
    result = CliRunner().invoke(cli, ["--db", _db_file(corpus), "enrich", "--task", "suitability", "--force"])
    assert result.exit_code == 2 and "append-only" in result.output
