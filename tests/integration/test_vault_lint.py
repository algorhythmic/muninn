"""`muninn lint`: the deterministic, model-free vault lint.

Synthetic vaults under tmp_path only. Covers orphan detection with explicit
hub exclusions, Obsidian-style link resolution (aliases, |display, #heading,
paths, embeds, Markdown links), missing-link suggestions and their
false-positive guards, the personal-vault guard, and the guarantee that
lint never changes a vault file.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from muninn.consumers.cli.main import cli
from muninn.consumers.mcp.vault_fs import VaultAccessError
from muninn.consumers.vault.lint import (
    build_page,
    lint_pages,
    lint_vault,
    parse_frontmatter,
    render_text,
)

CONCEPT = """---
title: Prompt Caching
aliases: [cache prefix, "KV reuse"]
---
# Prompt Caching

Pairs with [[ada-lovelace|Ada]] and [[missing-page#Setup]].

```
retrieval augmented generation inside a code block
```
"""

RAG = """---
aliases:
  - RAG
  - Retrieval augmented generation
tags: [ml]
---
# Retrieval-augmented generation

It uses prompt caching heavily. Prompt caching again, and `cache prefix` in code.
<!-- KV reuse inside a comment -->
See https://example.com/kv-reuse for nothing.
"""

PERSON = """# Ada Lovelace

Wrote about [the cache](../concepts/prompt-caching.md#top) and [[retrieval-augmented-generation#History]].
"""

BOOKMARK = """---
page_type: bookmark
bookmark_id: 1
title: "A post about KV reuse"
tags: ["ml"]
---

# A post about KV reuse

Mentions kv reuse twice: KV  reuse. Also links ![[RAG]].
"""


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "compiled-vault"
    _write(root, "wiki/concepts/prompt-caching.md", CONCEPT)
    _write(root, "wiki/concepts/retrieval-augmented-generation.md", RAG)
    _write(root, "wiki/people/ada-lovelace.md", PERSON)
    _write(root, "wiki/index.md", "# Index\n\nStart at [[prompt-caching]].\n")
    _write(root, "wiki/log.md", "# Log\n\n- 2026-10-01 nothing\n")
    _write(root, "wiki/bookmarks/x-synthetic-1.md", BOOKMARK)
    _write(root, "wiki/infra/lonely.md", "# Lonely box\n\nNobody links here.\n")
    _write(root, "wiki/infra/quiet.md", "---\nlint_ignore: orphan\n---\n# Quiet\n")
    return root


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MUNINN_VAULT_DIR", raising=False)
    monkeypatch.delenv("MUNINN_PERSONAL_VAULT_DIR", raising=False)


def _tree_digest(root: Path) -> dict[str, str]:
    """Every file and directory under root, with content hashes and modes."""
    out = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_file():
            out[rel] = hashlib.sha256(path.read_bytes()).hexdigest() + f":{path.stat().st_mtime_ns}"
        else:
            out[rel] = "dir"
    return out


# ── frontmatter ───────────────────────────────────────────────────


def test_frontmatter_scalars_inline_and_block_lists() -> None:
    meta, offset = parse_frontmatter(RAG)
    assert meta["aliases"] == ["RAG", "Retrieval augmented generation"]
    assert meta["tags"] == ["ml"]
    assert RAG[offset:].startswith("# Retrieval")
    meta, _ = parse_frontmatter(CONCEPT)
    assert meta["title"] == "Prompt Caching" and meta["aliases"] == ["cache prefix", "KV reuse"]
    assert parse_frontmatter("# no frontmatter\n") == ({}, 0)


# ── orphans and resolution ────────────────────────────────────────


def test_orphans_exclude_hubs_and_opt_outs(vault: Path) -> None:
    report = lint_vault(vault)
    orphans = {o["page"] for o in report.orphans}
    # index.md and log.md are hubs; quiet.md opted out; prompt-caching is
    # linked from index, the person page from prompt-caching, RAG by its
    # heading link and an alias embed, the bookmark by no one.
    assert orphans == {"wiki/infra/lonely.md", "wiki/bookmarks/x-synthetic-1.md"}
    lanes = {o["page"]: o["lane"] for o in report.orphans}
    assert lanes["wiki/bookmarks/x-synthetic-1.md"] == "captured"
    assert lanes["wiki/infra/lonely.md"] == "authored"


@pytest.mark.parametrize(
    "link",
    [
        "[[Target Page]]",                  # file name, case-insensitive
        "[[target page|shown text]]",       # display suffix
        "[[target page#Some heading]]",     # heading suffix
        "[[target page#^block-id]]",        # block reference
        "[[target page#Heading|shown]]",    # both
        "![[target page]]",                 # embed
        "[[concepts/target page]]",         # path from wiki/
        "[[wiki/concepts/target page.md]]", # path from the vault root, with .md
        "[[The Alias]]",                    # alias from frontmatter
        "| [[target page\\|in a table]] |", # escaped pipe inside a table
        "[text](../concepts/target%20page.md#x)",  # relative Markdown link
    ],
)
def test_link_forms_resolve(link: str) -> None:
    target = build_page("wiki/concepts/target page.md", "---\naliases: [The Alias]\n---\n# T\n")
    source = build_page("wiki/people/source.md", f"# Source\n\n{link}\n")
    report = lint_pages([target, source])
    assert report.unresolved == []
    assert "wiki/concepts/target page.md" not in {o["page"] for o in report.orphans}


def test_links_in_code_and_comments_do_not_count() -> None:
    target = build_page("wiki/concepts/target.md", "# Target\n")
    source = build_page(
        "wiki/people/source.md",
        "# S\n\n`[[target]]`\n\n```\n[[target]]\n```\n\n<!-- [[target]] -->\n",
    )
    report = lint_pages([target, source])
    assert "wiki/concepts/target.md" in {o["page"] for o in report.orphans}


def test_self_links_and_heading_only_links_are_not_inbound() -> None:
    page = build_page("wiki/concepts/solo.md", "# Solo\n\n[[solo]] and [[#Heading]]\n")
    report = lint_pages([page])
    assert [o["page"] for o in report.orphans] == ["wiki/concepts/solo.md"]
    assert report.unresolved == []


def test_ambiguous_names_prefer_the_same_directory() -> None:
    a = build_page("wiki/concepts/shared.md", "# A\n")
    b = build_page("wiki/people/shared.md", "# B\n")
    source = build_page("wiki/people/source.md", "# S\n\n[[shared]]\n")
    report = lint_pages([a, b, source])
    orphans = {o["page"] for o in report.orphans}
    assert "wiki/people/shared.md" not in orphans and "wiki/concepts/shared.md" in orphans


def test_unresolved_links_are_reported_with_lines(vault: Path) -> None:
    report = lint_vault(vault)
    assert report.unresolved == [
        {"page": "wiki/concepts/prompt-caching.md", "lane": "authored", "line": 7, "target": "missing-page"}
    ]


# ── suggestions ───────────────────────────────────────────────────


def test_suggestions_and_false_positive_guards(vault: Path) -> None:
    report = lint_vault(vault)
    got = [(s["page"], s["line"], s["mention"], s["target"], s["link"]) for s in report.suggestions]
    assert got == [
        # Alias, case-insensitive: first in the heading, and once per
        # target per page although the body says it twice more.
        ("wiki/bookmarks/x-synthetic-1.md", 8, "KV reuse", "wiki/concepts/prompt-caching.md",
         "[[prompt-caching|KV reuse]]"),
        # Title match; the code span and the comment are not scanned.
        ("wiki/concepts/retrieval-augmented-generation.md", 9, "prompt caching",
         "wiki/concepts/prompt-caching.md", "[[prompt-caching|prompt caching]]"),
    ]
    # Not suggested: RAG inside prompt-caching's code block; the person
    # page already links both concepts; the bookmark's frontmatter title
    # and its URL; the bookmark already embeds RAG.


def test_suggestion_guards_short_common_and_shared_terms() -> None:
    pages = [
        build_page("wiki/concepts/ai.md", "# AI\n"),                    # too short
        build_page("wiki/concepts/notes.md", "# Notes\n"),              # common word
        build_page("wiki/concepts/graph-a.md", "---\naliases: [Graph]\n---\n# Graph A\n"),
        build_page("wiki/people/graph-b.md", "---\naliases: [Graph]\n---\n# Graph B\n"),  # shared alias
        build_page("wiki/concepts/rust.md", "# Rust\n"),
        build_page(
            "wiki/people/source.md",
            "# Source\n\nAI notes about a graph. Rusty trust in rust-lang, rusted. Rust!\n",
        ),
    ]
    report = lint_pages(pages)
    got = [(s["page"], s["mention"]) for s in report.suggestions]
    # "Rusty", "trust" and "rusted" are not whole-word matches; "rust-lang"
    # is (a hyphen ends a word), so the first suggestion is on that line.
    assert got == [("wiki/people/source.md", "rust")]


def test_suggestions_target_authored_namespaces_only() -> None:
    bookmark = build_page("wiki/bookmarks/vector-search-9.md", "---\ntitle: \"Vector search\"\n---\n# Vector search\n")
    source = build_page("wiki/concepts/source.md", "# Source\n\nAll about vector search.\n")
    assert lint_pages([bookmark, source]).suggestions == []


def test_lint_ignore_suggest_turns_off_suggestions_on_a_page() -> None:
    target = build_page("wiki/concepts/kubernetes.md", "# Kubernetes\n")
    source = build_page("wiki/infra/box.md", "---\nlint_ignore: [suggest]\n---\n# Box\n\nRuns kubernetes.\n")
    assert lint_pages([target, source]).suggestions == []


# ── never writes; guard ───────────────────────────────────────────


def test_lint_never_writes_the_vault(vault: Path, tmp_path: Path) -> None:
    before = _tree_digest(vault)
    report_file = tmp_path / "reports" / "lint.json"
    runner = CliRunner()
    for args in (["--vault", str(vault)], ["--vault", str(vault), "--json"],
                 ["--vault", str(vault), "--json", "--report", str(report_file)]):
        result = runner.invoke(cli, ["lint", *args])
        assert result.exit_code == 0, result.output
    assert _tree_digest(vault) == before
    assert json.loads(report_file.read_text())["counts"]["orphans"] == 2


def test_report_file_inside_the_vault_is_refused(vault: Path) -> None:
    before = _tree_digest(vault)
    result = CliRunner().invoke(cli, ["lint", "--vault", str(vault), "--report", str(vault / "lint.txt")])
    assert result.exit_code != 0 and "inside the vault" in result.output
    assert _tree_digest(vault) == before


def test_personal_vault_overlap_is_refused(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(VaultAccessError) as exc:
        lint_vault(vault, personal_dir=vault)
    assert exc.value.code == "vault_conflict" and "refusing to lint" in str(exc.value)
    monkeypatch.setenv("MUNINN_PERSONAL_VAULT_DIR", str(vault / "wiki"))
    result = CliRunner().invoke(cli, ["lint", "--vault", str(vault)])
    assert result.exit_code == 1 and "refusing to lint" in result.output


def test_unconfigured_vault_is_a_clean_error(tmp_path: Path) -> None:
    result = CliRunner().invoke(cli, ["lint"])
    assert result.exit_code == 1 and "MUNINN_VAULT_DIR is not set" in result.output
    result = CliRunner().invoke(cli, ["lint", "--vault", str(tmp_path / "nowhere")])
    assert result.exit_code == 1 and "no wiki/ directory" in result.output


# ── output ────────────────────────────────────────────────────────


def test_text_and_json_output(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MUNINN_VAULT_DIR", str(vault))
    runner = CliRunner()
    text = runner.invoke(cli, ["lint"])
    assert text.exit_code == 0
    assert "Report only: no vault file was changed." in text.output
    assert "Orphans (2)" in text.output and "wiki/infra/lonely.md [authored]" in text.output
    assert '"KV reuse" -> [[prompt-caching|KV reuse]]' in text.output

    data = json.loads(runner.invoke(cli, ["lint", "--json"]).output)
    assert data["report_version"] == "muninn-vault-lint/1"
    assert data["counts"] == {"orphans": 2, "suggestions": 2, "unresolved_links": 1, "skipped": 0}
    assert data["pages_by_lane"] == {"captured": 1, "authored": 7}
    assert data["exclusions"]["orphan_hub_names"] == ["index", "_index", "readme", "home", "log"]

    # Deterministic: the same vault gives the same bytes.
    assert runner.invoke(cli, ["lint", "--json"]).output == runner.invoke(cli, ["lint", "--json"]).output
    assert render_text(lint_vault(vault)) == text.output


def test_strict_exits_nonzero_on_findings(vault: Path, tmp_path: Path) -> None:
    runner = CliRunner()
    assert runner.invoke(cli, ["lint", "--vault", str(vault), "--strict"]).exit_code == 1
    clean = tmp_path / "clean"
    _write(clean, "wiki/index.md", "# Index\n\n[[a-page]]\n")
    _write(clean, "wiki/concepts/a-page.md", "# A page\n")
    assert runner.invoke(cli, ["lint", "--vault", str(clean), "--strict"]).exit_code == 0


def test_report_flag_writes_file_and_prints_summary(vault: Path, tmp_path: Path) -> None:
    out = tmp_path / "weekly" / "lint.txt"
    result = CliRunner().invoke(cli, ["lint", "--vault", str(vault), "--report", str(out)])
    assert result.exit_code == 0
    assert result.output.strip() == (
        f"Vault lint: 2 orphan(s), 2 suggested link(s), 1 unresolved link(s); report: {out}"
    )
    assert out.read_text().startswith("Vault lint (muninn-vault-lint/1)")


def test_oversized_pages_are_skipped_not_read(vault: Path) -> None:
    from muninn.consumers.mcp.vault_fs import MAX_PAGE_BYTES

    _write(vault, "wiki/concepts/huge.md", "# Huge\n" + "x" * (MAX_PAGE_BYTES + 1))
    report = lint_vault(vault)
    assert report.skipped == [{"page": "wiki/concepts/huge.md", "reason": f"larger than {MAX_PAGE_BYTES} bytes"}]
