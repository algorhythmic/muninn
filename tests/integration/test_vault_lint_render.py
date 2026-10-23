"""The vault compiler renders current lint findings into compiled pages only.

Golden-style: a page with no current finding compiles byte-for-byte as it
does without any finding; a page with one gains exactly the frontmatter
keys and callouts pinned here. A finding stops rendering when a page is
hidden, when a page's content changes, or when a later check of the pair
finds nothing. Authored pages are never touched.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from muninn.consumers.vault.compiler import compile_vault
from muninn.db import connect
from muninn.synthesis import run_lint
from muninn.synthesis.api_runner import ApiRunner
from tests.integration.lint_fakes import CONTRADICTS, SUPERSEDES, lint_output, seed
from tests.integration.synthesis_fakes import fake_client, message

POST = "x-synth-author-1900000000000000003.md"
V1, V2, OTHER = "caching-in-v1-1.md", "caching-in-v2-2.md", "unrelated-5.md"

V1_FRONTMATTER = (
    "superseded_at: 2023-12-14T22:13:20Z\n"
    'superseded_by: ["[[caching-in-v2-2]]"]\n'
)
V1_BLOCK = (
    "<!-- contradiction: x-synth-author-1900000000000000003 -->\n"
    "> [!warning] Possible contradiction\n"
    "> This page may contradict [[x-synth-author-1900000000000000003]]. "
    "One says caching never works; the other documents how it works.\n"
    "\n"
    "> [!note] Superseded\n"
    "> A newer item supersedes a claim on this page: [[caching-in-v2-2]] (2023-12-14T22:13:20Z). "
    "v2 changed the cache lifetime from five minutes to one hour.\n"
    "\n"
)
POST_BLOCK = (
    "<!-- contradiction: caching-in-v1-1 -->\n"
    "> [!warning] Possible contradiction\n"
    "> This page may contradict [[caching-in-v1-1]]. "
    "One says caching never works; the other documents how it works.\n"
    "\n"
)


@pytest.fixture
def db_file(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("MUNINN_SYNTHESIS_MONTHLY_BUDGET_USD", "10")
    conn = connect(db_path)
    try:
        seed(conn)
    finally:
        conn.close()
    return db_path


def _lint(db_path: Path, *findings: dict, model: str = "claude-opus-5-5") -> str:
    conn = connect(db_path)
    try:
        client = fake_client(message(lint_output(*findings), model=model))
        result = run_lint("llm", conn=conn, runner=ApiRunner(client, model=model))
        assert result.status == "completed", result.errors
        return result.task_id
    finally:
        conn.close()


def _compile(db_path: Path, out: Path) -> dict[str, str]:
    compile_vault(db_path=db_path, output_dir=out, personal_dir=None)
    return {p.name: p.read_text() for p in sorted((out / "wiki" / "bookmarks").glob("*.md"))}


def _with(page: str, *, frontmatter: str = "", block: str = "") -> str:
    """The page as compiled without findings, plus the given insertions."""
    head, sep, rest = page.partition("\n---\n")  # closing frontmatter fence
    page = head + "\n" + frontmatter + "---\n" + rest if frontmatter else page
    h1_start = page.index("\n# ") + 1
    h1_end = page.index("\n", h1_start) + 1
    if page[h1_end:h1_end + 1] == "\n":
        h1_end += 1
    return page[:h1_end] + block + page[h1_end:]


def test_findings_render_into_compiled_pages_only(db_file: Path, tmp_path: Path) -> None:
    baseline = _compile(db_file, tmp_path / "before")
    _lint(db_file, SUPERSEDES, CONTRADICTS)
    vault = tmp_path / "vault"
    authored = vault / "wiki" / "concepts" / "prompt-caching.md"
    authored.parent.mkdir(parents=True)
    authored.write_text("# Prompt caching\n\nCaching in v1 never works, says a post.\n")
    authored_hash = hashlib.sha256(authored.read_bytes()).hexdigest()

    pages = _compile(db_file, vault)

    assert pages[V1] == _with(baseline[V1], frontmatter=V1_FRONTMATTER, block=V1_BLOCK)
    assert pages[POST] == _with(baseline[POST], block=POST_BLOCK)
    # The newer page of a supersession and pages without findings are unchanged.
    assert pages[V2] == baseline[V2]
    assert pages[OTHER] == baseline[OTHER]
    assert hashlib.sha256(authored.read_bytes()).hexdigest() == authored_hash
    # Deterministic.
    assert _compile(db_file, vault) == pages


def _copy(src: Path, dest: Path) -> Path:
    source, target = sqlite3.connect(src), sqlite3.connect(dest)
    try:
        source.backup(target)
    finally:
        source.close()
        target.close()
    return dest


def _hide(db_path: Path, bookmark_id: int) -> None:
    conn = connect(db_path)
    conn.execute("UPDATE bookmarks SET content_visible = 0 WHERE bookmark_id = ?", (bookmark_id,))
    conn.commit()
    conn.close()


def test_a_hidden_page_takes_every_finding_of_its_task_with_it(db_file: Path, tmp_path: Path) -> None:
    unlinted = _copy(db_file, tmp_path / "unlinted.db")
    _lint(db_file, SUPERSEDES, CONTRADICTS)
    for path in (db_file, unlinted):
        _hide(path, 3)
    baseline = _compile(unlinted, tmp_path / "before")
    pages = _compile(db_file, tmp_path / "vault")
    assert POST not in pages
    # The task showed the model the post, so even the 1-2 supersession's
    # rationale could paraphrase it: nothing of that task renders.
    assert pages[V1] == baseline[V1]
    assert pages[V2] == baseline[V2]

    # The pair it found something on is pending again, alone: page 3 is
    # no longer a candidate, and (1, 2) is re-judged without it.
    conn = connect(db_file)
    try:
        from muninn.synthesis.tasks import lint as lint_task

        assert lint_task.prepare_input(conn, "llm")["pairs"] == [[1, 2]]
    finally:
        conn.close()
    _lint(db_file, SUPERSEDES)
    pages = _compile(db_file, tmp_path / "relinted")
    supersession_only = V1_BLOCK.split("\n\n", 1)[1]
    assert pages[V1] == _with(baseline[V1], frontmatter=V1_FRONTMATTER, block=supersession_only)
    assert "x-synth-author" not in pages[V1]


def test_a_third_page_hidden_since_the_task_drops_the_finding(db_file: Path, tmp_path: Path) -> None:
    # Only 1 and 2 are in the finding, but the task also showed page 3.
    _lint(db_file, SUPERSEDES)
    assert V1_FRONTMATTER in _compile(db_file, tmp_path / "a")[V1]
    _hide(db_file, 3)
    pages = _compile(db_file, tmp_path / "b")
    assert "superseded_by" not in pages[V1] and "[!note]" not in pages[V1]


def test_a_rationale_is_rendered_as_inert_text(db_file: Path, tmp_path: Path) -> None:
    # A row written before ingest rejected markup (or by any other path):
    # the compiler escapes it again.
    _lint(db_file, SUPERSEDES)
    hostile = "Fine. <!-- hides the rest --> [[secret-page|x]] ![img](https://e.example/p.png) a\\[b] & -->"
    conn = connect(db_file)
    conn.execute("UPDATE lint_findings SET rationale = ?", (hostile,))
    conn.commit()
    conn.close()
    page = _compile(db_file, tmp_path / "vault")[V1]
    callout = next(line for line in page.splitlines() if line.startswith("> A newer item supersedes"))
    assert callout.endswith(
        "Fine. &lt;!-- hides the rest --&gt; \\[\\[secret-page|x\\]\\] "
        "!\\[img\\](https://e.example/p.png) a\\\\\\[b\\] &amp; --&gt;"
    )
    assert page.count("<!--") == page.count("<!-- contradiction:") + page.count("<!-- muninn")
    assert "[[secret-page" not in page and "-->" not in callout


def test_changed_content_expires_a_finding(db_file: Path, tmp_path: Path) -> None:
    _lint(db_file, SUPERSEDES)
    assert "superseded_by" in _compile(db_file, tmp_path / "a")[V1]
    conn = connect(db_file)
    conn.execute("UPDATE scrape_results SET content_text = 'v1 now caches for a day.' WHERE bookmark_id = 1")
    conn.commit()
    conn.close()
    pages = _compile(db_file, tmp_path / "b")
    assert "superseded_by" not in pages[V1] and "[!note]" not in pages[V1]


def test_a_later_check_without_findings_clears_the_old_verdict(db_file: Path, tmp_path: Path) -> None:
    _lint(db_file, SUPERSEDES, CONTRADICTS)
    assert "contradiction:" in _compile(db_file, tmp_path / "a")[POST]
    # Another model re-checks the same pairs and finds only the supersession.
    _lint(db_file, SUPERSEDES, model="claude-sonnet-5-5")
    pages = _compile(db_file, tmp_path / "b")
    assert "contradiction:" not in pages[POST] and "contradiction:" not in pages[V1]
    assert V1_FRONTMATTER in pages[V1]


def test_compiled_pages_keep_the_compiler_marker(db_file: Path, tmp_path: Path) -> None:
    from muninn.consumers.vault.compiler import is_compiled_page

    _lint(db_file, SUPERSEDES, CONTRADICTS)
    out = tmp_path / "vault"
    _compile(db_file, out)
    for name in (V1, POST):
        assert is_compiled_page(out / "wiki" / "bookmarks" / name)


def test_deterministic_lint_reads_a_compiled_vault_without_changing_it(db_file: Path, tmp_path: Path) -> None:
    from muninn.consumers.vault.lint import lint_vault

    _lint(db_file, SUPERSEDES, CONTRADICTS)
    out = tmp_path / "vault"
    _compile(db_file, out)
    before = {p: p.read_bytes() for p in sorted(out.rglob("*.md"))}
    report = lint_vault(out, db_path=db_file)
    assert {p: p.read_bytes() for p in sorted(out.rglob("*.md"))} == before
    assert report.lanes == {"captured": 4, "authored": 0}
    assert report.unresolved == []
    # v2 is linked from v1's superseded_by property; only the unrelated page is an orphan.
    assert [o["page"] for o in report.orphans] == ["wiki/bookmarks/unrelated-5.md"]

    # A policy apply hides v2 before the next compile: its title-derived
    # page is still on disk, but the report neither lists it nor names it
    # as the target of v1's link.
    from muninn.consumers.vault.lint import render_json, render_text

    conn = connect(db_file)
    conn.execute("UPDATE bookmarks SET content_visible = 0 WHERE bookmark_id = 2")
    conn.commit()
    conn.close()
    report = lint_vault(out, db_path=db_file)
    assert report.withheld == 1 and report.lanes == {"captured": 3, "authored": 0}
    for text in (render_text(report), render_json(report)):
        assert "caching-in-v2" not in text and "Caching in v2" not in text
    assert {(u["page"], u["target"]) for u in report.unresolved} == {
        ("wiki/bookmarks/caching-in-v1-1.md", "[hidden link]")
    }
