"""Integration tests for the consumer CLI subcommands (triage, status, deep-pass).

The dispatcher's `--help` is also exercised so we know lazy imports don't
break basic startup even when sibling modules don't exist yet.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from click.testing import CliRunner

from muninn.consumers.cli.main import cli


def _seed(db_path: Path) -> None:
    now = int(time.time())
    conn = sqlite3.connect(str(db_path))
    try:
        # 3 visible bookmarks; one un-enriched, one with a failed scrape.
        conn.executemany(
            "INSERT INTO bookmarks (source, source_id, captured_at, title, url, "
            "  era_label, domain, content_visible, enrichment_source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("netscape", "1", now - 9000, "Done", "https://e.com/a",
                 "early-web", "e.com", 1, "at_capture"),
                ("netscape", "2", now - 8000, "FailedScrape", "https://e.com/b",
                 "early-web", "e.com", 1, "at_capture"),
                ("netscape", "3", now - 3000, "Unenriched", "https://x.com/c",
                 "ai-era", "x.com", 1, "none"),
            ],
        )
        # Bookmark 1: enriched, scrape ok.
        conn.execute(
            "INSERT INTO enriched (bookmark_id, summary, "
            "  enrichment_model, enrichment_prompt_version, content_hash, enriched_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (1, "summary 1", "haiku", "v1", "h1", now),
        )
        conn.execute(
            "INSERT INTO scrape_results (bookmark_id, pass, fetched_at, scrape_status) "
            "VALUES (?, ?, ?, ?)",
            (1, "live", now, "ok"),
        )
        # Bookmark 2: enriched, scrape failed.
        conn.execute(
            "INSERT INTO enriched (bookmark_id, summary, "
            "  enrichment_model, enrichment_prompt_version, content_hash, enriched_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (2, "summary 2", "haiku", "v1", "h2", now),
        )
        conn.execute(
            "INSERT INTO scrape_results (bookmark_id, pass, fetched_at, scrape_status) "
            "VALUES (?, ?, ?, ?)",
            (2, "live", now, "failed"),
        )
        # Bookmark 3: not enriched at all (enrichment_source=none).

        # A synthesis run for status output.
        conn.execute(
            "INSERT INTO synthesis_runs (task_id, task_type, attempt, started_at, "
            "  status, enrichment_model, enrichment_prompt_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("era:early-web", "era_narrative", 1, now, "completed", "opus", "v1"),
        )
        conn.commit()
    finally:
        conn.close()


# ── --help works without sibling modules being importable ─────────


def test_cli_help_works():
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "muninn" in result.output.lower()
    # Make sure the subcommands we own are listed.
    for name in ("triage", "status", "deep-pass", "vault", "timeline", "export", "mcp"):
        assert name in result.output


# ── triage ────────────────────────────────────────────────────────


def test_triage_human(db_path: Path):
    _seed(db_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["--db", str(db_path), "triage"])
    assert result.exit_code == 0, result.output
    # Bookmark 2 (failed scrape) and 3 (unenriched) should appear.
    assert "FailedScrape" in result.output
    assert "Unenriched" in result.output
    # Bookmark 1 should not.
    assert "Done" not in result.output


def test_triage_json(db_path: Path):
    _seed(db_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["--db", str(db_path), "triage", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert "triage" in data
    ids = {row["bookmark_id"] for row in data["triage"]}
    assert ids == {2, 3}


# ── status ────────────────────────────────────────────────────────


def test_status(db_path: Path):
    _seed(db_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["--db", str(db_path), "status"])
    assert result.exit_code == 0, result.output
    assert "Bookmarks: 3" in result.output
    assert "Enriched: 2" in result.output
    assert "Scrape results" in result.output
    assert "live" in result.output  # at least one pass appears
    assert "Recent synthesis runs" in result.output
    assert "era_narrative" in result.output


# ── deep-pass ─────────────────────────────────────────────────────


def test_deep_pass_sets_flag(db_path: Path):
    _seed(db_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["--db", str(db_path), "deep-pass", "1"])
    assert result.exit_code == 0, result.output

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT deep_pass_requested FROM enriched WHERE bookmark_id = 1"
    ).fetchone()
    conn.close()
    assert row["deep_pass_requested"] == 1


def test_deep_pass_missing_bookmark_errors(db_path: Path):
    _seed(db_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["--db", str(db_path), "deep-pass", "9999"])
    assert result.exit_code != 0
    assert "no enriched row" in result.output.lower()


# ── synthesize subcommand surface (lazy imports must defer errors) ─


def test_synthesize_group_help(db_path: Path):
    runner = CliRunner()
    result = runner.invoke(cli, ["synthesize", "--help"])
    assert result.exit_code == 0
    assert "era" in result.output
    assert "deep-pass" in result.output
    assert "analyze" in result.output


def test_synthesize_deep_pass_requires_id_or_pending():
    runner = CliRunner()
    # No id and no --pending → should error before any lazy import fires.
    result = runner.invoke(cli, ["synthesize", "deep-pass"])
    assert result.exit_code != 0
    assert "BOOKMARK_ID" in result.output or "--pending" in result.output
