"""A deep pass never sends a policy-hidden bookmark's content to a model."""

from __future__ import annotations

import time

import pytest
from click.testing import CliRunner

from muninn.consumers.cli.main import cli
from muninn.db import connect
from muninn.synthesis.tasks import deep_pass


def _seed(db_path, *, visible: int) -> None:
    conn = connect(db_path)
    now = int(time.time())
    conn.execute(
        "INSERT INTO bookmarks (bookmark_id, source, source_id, captured_at, "
        "  title, url, era_label, content_visible, enrichment_source) "
        "VALUES (1, 'netscape', 's1', ?, 'Private page', 'https://intranet.example/1', "
        "  'era', ?, 'at_capture')",
        (now - 86400, visible),
    )
    conn.execute(
        "INSERT INTO enriched (bookmark_id, summary, enrichment_model, "
        "  enrichment_prompt_version, content_hash, enriched_at, deep_pass_requested) "
        "VALUES (1, 'Private summary', 'm', 'v', 'h', ?, 1)",
        (now,),
    )
    conn.commit()
    conn.close()


def test_prepare_input_refuses_a_hidden_bookmark(db_path) -> None:
    _seed(db_path, visible=0)
    conn = connect(db_path)
    try:
        with pytest.raises(LookupError, match="hidden by domain policy"):
            deep_pass.prepare_input(conn, 1)
    finally:
        conn.close()


def test_pending_queue_skips_hidden_bookmarks(db_path, monkeypatch) -> None:
    _seed(db_path, visible=0)
    calls: list[int] = []
    monkeypatch.setattr(
        "muninn.synthesis.orchestrator.run_deep_pass",
        lambda bid, **_: calls.append(bid),
    )
    result = CliRunner().invoke(
        cli, ["--db", str(db_path), "synthesize", "deep-pass", "--pending"]
    )
    assert result.exit_code == 0, result.output
    assert "No pending deep-pass rows." in result.output
    assert calls == []


def test_single_hidden_deep_pass_is_a_one_line_error(db_path, monkeypatch) -> None:
    _seed(db_path, visible=0)
    monkeypatch.setenv("MUNINN_SYNTHESIS_MONTHLY_BUDGET_USD", "1")
    result = CliRunner().invoke(cli, ["--db", str(db_path), "synthesize", "deep-pass", "1"])
    assert result.exit_code == 1
    assert "hidden by domain policy" in result.output
    assert "Traceback" not in result.output
