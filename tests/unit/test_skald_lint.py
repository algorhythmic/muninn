"""Tests for scripts/skald_lint.py (skald-protocol.md §9)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("skald_lint", REPO_ROOT / "scripts" / "skald_lint.py")
skald_lint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(skald_lint)

PROJECT_PAGE = """---
schema_version: 1
page_type: project
project: dbriefly
status: active
phase: M1
repo: ["gitea:david/dbriefly"]
updated_at: 2026-07-16
updated_by: note-skill
---

# dbriefly

<!-- skald:begin current-state -->
One-paragraph state.
<!-- skald:end current-state -->

<!-- skald:begin open-questions -->
- open question
<!-- skald:end open-questions -->

<!-- skald:begin recent-sessions -->
- [[sessions/2026-07-15-dbriefly-cascaded-ptt]] — shipped — cascaded PTT
<!-- skald:end recent-sessions -->
"""

SESSION_PAGE = """---
schema_version: 1
page_type: session
title: Cascaded PTT + Bluetooth route restoration
date: 2026-07-15
projects: [dbriefly]
emitter: note-skill
outcome: shipped
duration_min: 140
decisions:
  - "Manual PTT records one bounded WAV"
files_touched:
  - "dbriefly: src/audio/ptt.ts"
sources:
  - raw/sessions/2026-07-15-dbriefly-cascaded-ptt/
tags: [audio, bluetooth]
---

# Cascaded PTT + Bluetooth route restoration

**What happened.** Things happened.
"""

STEM = "2026-07-15-dbriefly-cascaded-ptt"


def make_vault(tmp_path, session_name=f"{STEM}.md", session_text=SESSION_PAGE, log_ref=None):
    vault = tmp_path / "vault"
    (vault / "wiki" / "sessions").mkdir(parents=True)
    (vault / "wiki" / "projects").mkdir(parents=True)
    (vault / "raw" / "sessions" / STEM).mkdir(parents=True)
    (vault / "wiki" / "projects" / "dbriefly.md").write_text(PROJECT_PAGE, encoding="utf-8")
    (vault / "wiki" / "sessions" / session_name).write_text(session_text, encoding="utf-8")
    ref = log_ref or session_name.removesuffix(".md")
    (vault / "wiki" / "log.md").write_text(
        f"- 2026-07-15 · [dbriefly] · shipped · [[sessions/{ref}]] — gist\n", encoding="utf-8"
    )
    return vault


def run(vault, *args):
    return skald_lint.main(["--vault", str(vault), *args])


def test_valid_vault_passes(tmp_path, capsys):
    vault = make_vault(tmp_path)
    assert run(vault) == 0
    out = capsys.readouterr().out
    assert "ERROR" not in out
    assert "2 page(s) checked" in out


def test_missing_required_field(tmp_path, capsys):
    text = SESSION_PAGE.replace("outcome: shipped\n", "")
    vault = make_vault(tmp_path, session_text=text)
    assert run(vault) == 1
    assert "outcome" in capsys.readouterr().out


def test_invalid_outcome_enum(tmp_path, capsys):
    text = SESSION_PAGE.replace("outcome: shipped", "outcome: triumphant")
    vault = make_vault(tmp_path, session_text=text)
    assert run(vault) == 1
    assert "'triumphant'" in capsys.readouterr().out


def test_stop_hook_may_not_author_sessions(tmp_path, capsys):
    text = SESSION_PAGE.replace("emitter: note-skill", "emitter: stop-hook")
    vault = make_vault(tmp_path, session_text=text)
    assert run(vault) == 1
    assert "stop-hook" in capsys.readouterr().out


def test_unregistered_emitter(tmp_path, capsys):
    text = SESSION_PAGE.replace("emitter: note-skill", "emitter: mystery-bot")
    vault = make_vault(tmp_path, session_text=text)
    assert run(vault) == 1
    assert "registry" in capsys.readouterr().out


def test_filename_must_match_date_and_primary(tmp_path, capsys):
    text = SESSION_PAGE.replace("date: 2026-07-15", "date: 2026-07-14")
    vault = make_vault(tmp_path, session_text=text)
    assert run(vault) == 1
    assert "2026-07-14-dbriefly-" in capsys.readouterr().out


def test_collision_suffix_accepted(tmp_path):
    vault = make_vault(tmp_path, session_name=f"{STEM}-2.md")
    assert run(vault) == 0


def test_unresolved_project_slug(tmp_path, capsys):
    text = SESSION_PAGE.replace("projects: [dbriefly]", "projects: [dbriefly, ghost-project]")
    vault = make_vault(tmp_path, session_text=text)
    assert run(vault) == 1
    assert "ghost-project" in capsys.readouterr().out


def test_unbalanced_markers_on_touched_project_page(tmp_path, capsys):
    vault = make_vault(tmp_path)
    page = vault / "wiki" / "projects" / "dbriefly.md"
    page.write_text(page.read_text().replace("<!-- skald:end open-questions -->\n", ""), encoding="utf-8")
    assert run(vault) == 1
    assert "open-questions" in capsys.readouterr().out


def test_missing_raw_source(tmp_path, capsys):
    text = SESSION_PAGE.replace(
        f"raw/sessions/{STEM}/", "raw/sessions/never-created/"
    )
    vault = make_vault(tmp_path, session_text=text)
    assert run(vault) == 1
    assert "never-created" in capsys.readouterr().out


def test_external_url_sources_not_checked(tmp_path):
    text = SESSION_PAGE.replace(f"raw/sessions/{STEM}/", "https://example.com/post")
    vault = make_vault(tmp_path, session_text=text)
    assert run(vault) == 0


def test_log_line_required_in_bootstrap_mode(tmp_path, capsys):
    vault = make_vault(tmp_path, log_ref="some-other-session")
    assert run(vault) == 1
    assert "log.md" in capsys.readouterr().out
    assert run(vault, "--skip-log-check") == 0


def test_recent_sessions_cap_warns_not_errors(tmp_path, capsys):
    vault = make_vault(tmp_path)
    page = vault / "wiki" / "projects" / "dbriefly.md"
    lines = "\n".join(f"- [[sessions/x-{i}]] — shipped — gist" for i in range(11))
    page.write_text(
        page.read_text().replace(
            "- [[sessions/2026-07-15-dbriefly-cascaded-ptt]] — shipped — cascaded PTT", lines
        ),
        encoding="utf-8",
    )
    assert run(vault) == 0
    out = capsys.readouterr().out
    assert "WARN" in out and "recent-sessions has 11" in out


def test_fallback_parser_matches_yaml(tmp_path, monkeypatch, capsys):
    vault = make_vault(tmp_path)
    monkeypatch.setattr(skald_lint, "yaml", None)
    assert run(vault) == 0
    assert "ERROR" not in capsys.readouterr().out


def test_fallback_parser_rejects_garbage(tmp_path, monkeypatch, capsys):
    text = SESSION_PAGE.replace("outcome: shipped", ":::: not yaml at all")
    vault = make_vault(tmp_path, session_text=text)
    monkeypatch.setattr(skald_lint, "yaml", None)
    assert run(vault) == 1
    assert "parse" in capsys.readouterr().out


def test_no_vault_found(tmp_path, capsys):
    assert skald_lint.main(["--vault", str(tmp_path / "nowhere")]) == 2


def test_readme_stubs_ignored(tmp_path):
    vault = make_vault(tmp_path)
    (vault / "wiki" / "sessions" / "README.md").write_text("# sessions/\n", encoding="utf-8")
    (vault / "wiki" / "projects" / "README.md").write_text("# projects/\n", encoding="utf-8")
    assert run(vault) == 0
