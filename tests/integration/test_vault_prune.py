"""`muninn vault compile` prunes compiled pages it no longer writes.

A row hidden by domain policy after its page was compiled, or a row whose
slug moved, must not leave its old page behind. Only files carrying the
compiler's frontmatter marker (``page_type: bookmark`` + an integer
``bookmark_id``) are ever deleted.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from muninn.consumers.cli.main import cli
from muninn.consumers.vault.compiler import (
    compile_vault,
    compile_vault_report,
    is_compiled_page,
)
from muninn.db import connect
from tests import x_corpus as xc
from tests.integration.test_vault import _seed_vault_corpus

HAND_WRITTEN = {
    # No frontmatter at all.
    "reading-notes.md": "# Reading notes\n\nWritten by hand.\n",
    # Frontmatter, but another page type.
    "concept-like.md": "---\npage_type: concept\nbookmark_id: 1\n---\n\n# Not compiled\n",
    # page_type: bookmark without a bookmark_id.
    "bookmark-ish.md": "---\npage_type: bookmark\ntitle: \"by hand\"\n---\n\n# By hand\n",
    # The marker only in the body, not the frontmatter.
    "quoting.md": "# Quoting\n\n---\npage_type: bookmark\nbookmark_id: 3\n---\n",
}


def _pages(out: Path) -> set[str]:
    return {p.name for p in (out / "wiki" / "bookmarks").glob("*.md")}


def _hide(db_path: Path, bookmark_id: int) -> None:
    conn = connect(db_path)
    conn.execute("UPDATE bookmarks SET content_visible = 0 WHERE bookmark_id = ?", (bookmark_id,))
    conn.commit()
    conn.close()


def _hand_write(out: Path) -> None:
    pages_dir = out / "wiki" / "bookmarks"
    for name, text in HAND_WRITTEN.items():
        (pages_dir / name).write_text(text)
    (pages_dir / "sub").mkdir()
    (pages_dir / "sub" / "first-bookmark-1.md").write_text(
        (pages_dir / "first-bookmark-1.md").read_text()
    )


def test_hidden_row_page_is_removed_and_hand_written_files_survive(db_path: Path, tmp_path: Path):
    _seed_vault_corpus(db_path)
    out = tmp_path / "compiled"
    assert compile_vault(db_path=db_path, output_dir=out, personal_dir=None) == 3
    assert _pages(out) == {"first-bookmark-1.md", "second-bookmark-2.md", "ai-paper-3.md"}
    _hand_write(out)

    _hide(db_path, 2)
    report = compile_vault_report(db_path=db_path, output_dir=out, personal_dir=None)
    assert report.written == 2 and report.removed == ["second-bookmark-2.md"]
    assert _pages(out) == {"first-bookmark-1.md", "ai-paper-3.md", *HAND_WRITTEN}
    assert (out / "wiki" / "bookmarks" / "sub" / "first-bookmark-1.md").exists()
    for name, text in HAND_WRITTEN.items():
        assert (out / "wiki" / "bookmarks" / name).read_text() == text

    # Idempotent: nothing left to prune.
    again = compile_vault_report(db_path=db_path, output_dir=out, personal_dir=None)
    assert again.removed == []


def test_renamed_slug_removes_the_old_page(db_path: Path, tmp_path: Path):
    _seed_vault_corpus(db_path)
    out = tmp_path / "compiled"
    compile_vault(db_path=db_path, output_dir=out, personal_dir=None)
    conn = connect(db_path)
    conn.execute("UPDATE bookmarks SET title = 'Renamed Paper' WHERE bookmark_id = 3")
    conn.commit()
    conn.close()

    report = compile_vault_report(db_path=db_path, output_dir=out, personal_dir=None)
    assert report.removed == ["ai-paper-3.md"]
    assert _pages(out) == {"first-bookmark-1.md", "second-bookmark-2.md", "renamed-paper-3.md"}


def test_hidden_x_link_page_is_removed(db_path: Path, tmp_path: Path):
    conn = connect(db_path)
    corpus = xc.seed_x_corpus(conn)
    conn.close()
    out = tmp_path / "compiled"
    compile_vault(db_path=db_path, output_dir=out, personal_dir=None)
    (repo_page,) = [n for n in _pages(out) if n.startswith("github-")]
    assert is_compiled_page(out / "wiki" / "bookmarks" / repo_page)

    _hide(db_path, corpus.repo)
    report = compile_vault_report(db_path=db_path, output_dir=out, personal_dir=None)
    assert report.removed == [repo_page]
    everything = "\n".join(p.read_text() for p in (out / "wiki" / "bookmarks").glob("*.md"))
    assert xc.REPO_URL not in everything and repo_page.removesuffix(".md") not in everything


def test_no_prune_keeps_stale_pages(db_path: Path, tmp_path: Path):
    _seed_vault_corpus(db_path)
    out = tmp_path / "compiled"
    compile_vault(db_path=db_path, output_dir=out, personal_dir=None)
    _hide(db_path, 1)
    report = compile_vault_report(db_path=db_path, output_dir=out, personal_dir=None, prune=False)
    assert report.removed == [] and "first-bookmark-1.md" in _pages(out)


def test_cli_reports_removed_pages(db_path: Path, tmp_path: Path):
    _seed_vault_corpus(db_path)
    out = tmp_path / "compiled"
    args = ["--db", str(db_path), "vault", "compile", "--vault-dir", str(out), "--personal-dir",
            str(tmp_path / "personal")]
    first = CliRunner().invoke(cli, args)
    assert first.exit_code == 0, first.output
    assert "Compiled 3 vault page(s); removed 0 stale page(s)." in first.output

    _hide(db_path, 2)
    kept = CliRunner().invoke(cli, [*args, "--no-prune"])
    assert kept.exit_code == 0 and "removed 0 stale page(s)" in kept.output
    assert "second-bookmark-2.md" in _pages(out)

    pruned = CliRunner().invoke(cli, args)
    assert pruned.exit_code == 0, pruned.output
    assert "removed stale page wiki/bookmarks/second-bookmark-2.md" in pruned.output
    assert "Compiled 2 vault page(s); removed 1 stale page(s)." in pruned.output
    assert "second-bookmark-2.md" not in _pages(out)
