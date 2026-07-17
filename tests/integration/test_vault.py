"""Integration tests for the vault compiler.

Crucially exercises the never-same-vault guard from
`muninn.consumers.vault.compiler._validate_vault_paths`.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from muninn.consumers.vault.compiler import (
    VaultPathConflictError,
    _validate_vault_paths,
    compile_vault,
)


# ── never-same-vault guard ─────────────────────────────────────────


def test_guard_same_path_raises(tmp_path: Path) -> None:
    same = tmp_path / "vault"
    same.mkdir()
    with pytest.raises(VaultPathConflictError) as exc:
        _validate_vault_paths(same, same)
    assert "same as" in str(exc.value)


def test_guard_output_inside_personal_raises(tmp_path: Path) -> None:
    personal = tmp_path / "personal"
    output = personal / "compiled"
    output.mkdir(parents=True)
    with pytest.raises(VaultPathConflictError) as exc:
        _validate_vault_paths(output, personal)
    assert "INSIDE" in str(exc.value)


def test_guard_personal_inside_output_raises(tmp_path: Path) -> None:
    output = tmp_path / "compiled"
    personal = output / "personal"
    personal.mkdir(parents=True)
    with pytest.raises(VaultPathConflictError) as exc:
        _validate_vault_paths(output, personal)
    assert "INSIDE" in str(exc.value)


def test_guard_distinct_siblings_succeed(tmp_path: Path) -> None:
    output = tmp_path / "compiled"
    personal = tmp_path / "personal"
    output.mkdir()
    personal.mkdir()
    # Should not raise.
    _validate_vault_paths(output, personal)


def test_guard_personal_none_succeeds(tmp_path: Path) -> None:
    output = tmp_path / "compiled"
    output.mkdir()
    # Opt-out: no personal vault configured.
    _validate_vault_paths(output, None)


# ── compiler output (canonical schema) ─────────────────────────────


def _seed_vault_corpus(db_path: Path) -> None:
    """Populate the canonical schema with a handful of bookmarks/enriched
    rows for vault tests."""
    now = int(time.time())
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        conn.execute(
            "INSERT INTO eras (era_label, narrative, start_date, end_date, bookmark_count) "
            "VALUES (?, ?, ?, ?, ?)",
            ("early-web", "early days", now - 10000, now - 5000, 2),
        )
        conn.execute(
            "INSERT INTO eras (era_label, narrative, start_date, end_date, bookmark_count) "
            "VALUES (?, ?, ?, ?, ?)",
            ("ai-era", "ai stuff", now - 4000, now, 1),
        )

        # Visible
        conn.execute(
            "INSERT INTO bookmarks (source, source_id, captured_at, title, url, "
            "  era_label, domain, content_visible, enrichment_source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("netscape", "1", now - 9000, "First Bookmark", "https://example.com/a",
             "early-web", "example.com", 1, "at_capture"),
        )
        conn.execute(
            "INSERT INTO bookmarks (source, source_id, captured_at, title, url, "
            "  era_label, domain, content_visible, enrichment_source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("netscape", "2", now - 8000, "Second Bookmark", "https://example.com/b",
             "early-web", "example.com", 1, "at_capture"),
        )
        conn.execute(
            "INSERT INTO bookmarks (source, source_id, captured_at, title, url, "
            "  era_label, domain, content_visible, enrichment_source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("netscape", "3", now - 3000, "AI Paper", "https://arxiv.org/x",
             "ai-era", "arxiv.org", 1, "at_capture"),
        )
        # Hidden — must NOT produce a vault page.
        conn.execute(
            "INSERT INTO bookmarks (source, source_id, captured_at, title, url, "
            "  era_label, domain, content_visible, enrichment_source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("netscape", "4", now - 2000, "Secret Bookmark", "https://intra.example/x",
             "ai-era", "intra.example", 0, "none"),
        )

        conn.executemany(
            "INSERT INTO enriched (bookmark_id, summary, tags, entities, key_quotes, "
            "  enrichment_model, enrichment_prompt_version, content_hash, enriched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (1, "Summary 1", json.dumps(["web", "history"]),
                 json.dumps(["Tim Berners-Lee"]), json.dumps(["q1"]),
                 "haiku", "v1", "h1", now),
                (2, "Summary 2", json.dumps(["web", "social"]),
                 None, None, "haiku", "v1", "h2", now),
                (3, "Summary 3", json.dumps(["ai"]),
                 None, None, "haiku", "v1", "h3", now),
                (4, "Hidden", None, None, None, "haiku", "v1", "h4", now),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def test_compile_writes_visible_only(db_path: Path, tmp_path: Path) -> None:
    _seed_vault_corpus(db_path)
    output = tmp_path / "compiled"
    personal = tmp_path / "personal"
    personal.mkdir()

    count = compile_vault(
        db_path=db_path, output_dir=output, personal_dir=personal
    )
    assert count == 3  # only visible bookmarks
    pages_dir = output / "wiki" / "bookmarks"
    files = sorted(p.name for p in pages_dir.glob("*.md"))
    assert len(files) == 3
    # Nothing is written outside the compiler-owned namespace.
    assert sorted(p.name for p in output.iterdir()) == ["wiki"]
    # Hidden bookmark id=4: no file should mention "Secret".
    for f in files:
        assert "Secret" not in (pages_dir / f).read_text()


def test_compile_frontmatter_and_wikilinks(db_path: Path, tmp_path: Path) -> None:
    _seed_vault_corpus(db_path)
    output = tmp_path / "compiled"
    compile_vault(db_path=db_path, output_dir=output, personal_dir=None)

    # Find the page for bookmark_id=1 ("First Bookmark") — slug ends in "-1".
    pages = list((output / "wiki" / "bookmarks").glob("*-1.md"))
    assert len(pages) == 1
    text = pages[0].read_text()
    assert "page_type: bookmark" in text
    assert "bookmark_id: 1" in text
    assert 'title: "First Bookmark"' in text
    assert 'url: "https://example.com/a"' in text
    assert "era: early-web" in text
    assert "contributors: []" in text
    # Cross-reference: bookmark 2 shares era + 'web' tag.
    assert "[[" in text and "-2]]" in text


def test_compile_refuses_same_vault(db_path: Path, tmp_path: Path) -> None:
    _seed_vault_corpus(db_path)
    same = tmp_path / "vault"
    same.mkdir()
    with pytest.raises(VaultPathConflictError):
        compile_vault(db_path=db_path, output_dir=same, personal_dir=same)
    # And nothing should have been written.
    assert list(same.iterdir()) == []
