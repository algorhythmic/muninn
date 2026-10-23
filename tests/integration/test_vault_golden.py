"""Golden check: browser-bookmark vault pages compile byte-for-byte.

The X-lane pages (source ``x``, ``x_article``, ``x_link``) share
``wiki/bookmarks/`` with browser bookmarks. This pins the browser pages'
exact bytes so that work on the X lane can't silently change them. The
golden files under ``tests/fixtures/vault/golden/`` were produced by the
compiler before the X lane existed; regenerate them only for an
intended change to browser pages:

    MUNINN_UPDATE_GOLDEN=1 pytest tests/integration/test_vault_golden.py
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from muninn.consumers.vault.compiler import compile_vault

GOLDEN_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "vault" / "golden"

T0 = 1_600_000_000  # fixed epoch so captured_at is stable


def _seed_browser_corpus(db_path: Path) -> None:
    """Synthetic browser bookmarks covering every template branch."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        rows = [
            # id, source_id, captured_at, title, url, folder_path, era, domain, visible, enrichment_source
            (1, "1", T0, "First Bookmark", "https://example.com/a",
             json.dumps(["Bookmarks bar", "Early web"]), "early-web", "example.com", 1, "at_capture"),
            (2, "2", T0 + 100, 'Quoted "title" & more', "https://example.com/b",
             None, "early-web", "example.com", 1, "recent_archive"),
            (3, "3", T0 + 200, None, None, None, None, None, 1, None),
            (4, "4", T0 + 300, "AI Paper", "https://example.org/paper",
             json.dumps(["Research"]), "ai-era", "example.org", 1, "live_fallback"),
            (5, "5", T0 + 400, "Secret Bookmark", "https://intra.example/x",
             None, "ai-era", "intra.example", 0, "none"),
        ]
        conn.executemany(
            "INSERT INTO bookmarks (bookmark_id, source, source_id, captured_at, title, url, "
            "  folder_path, era_label, domain, content_visible, enrichment_source) "
            "VALUES (?, 'bookmarks_html', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.executemany(
            "INSERT INTO enriched (bookmark_id, summary, tags, entities, key_quotes, "
            "  enrichment_model, enrichment_prompt_version, content_hash, enriched_at) "
            "VALUES (?, ?, ?, ?, ?, 'haiku', 'v1', ?, ?)",
            [
                (1, "Summary one.", json.dumps(["web", "history"]),
                 json.dumps(["Example Person"]), json.dumps(["first quote", "second quote"]),
                 "h1", T0),
                (2, "Summary two.", json.dumps(["web"]), None, None, "h2", T0),
                (4, "Summary four.", json.dumps(["ai"]), json.dumps(["Model"]), None, "h4", T0),
                (5, "Hidden summary.", json.dumps(["web"]), None, None, "h5", T0),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def test_browser_pages_match_golden(db_path: Path, tmp_path: Path) -> None:
    _seed_browser_corpus(db_path)
    out = tmp_path / "compiled"
    count = compile_vault(db_path=db_path, output_dir=out, personal_dir=None)
    pages_dir = out / "wiki" / "bookmarks"
    produced = {p.name: p.read_bytes() for p in sorted(pages_dir.glob("*.md"))}
    assert count == len(produced) == 4

    if os.environ.get("MUNINN_UPDATE_GOLDEN"):
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        for old in GOLDEN_DIR.glob("*.md"):
            old.unlink()
        for name, data in produced.items():
            (GOLDEN_DIR / name).write_bytes(data)

    golden = {p.name: p.read_bytes() for p in sorted(GOLDEN_DIR.glob("*.md"))}
    assert sorted(produced) == sorted(golden)
    for name, data in produced.items():
        assert data == golden[name], f"{name} differs from its golden file"
