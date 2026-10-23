"""Render lint findings (``lint_findings``) into compiled bookmark pages.

Only the vault compiler calls this, and only for pages it writes in
``wiki/bookmarks/``: a finding never reaches an authored page (ADR-008),
and the model tier only ever compares compiled bookmark pages anyway.

A finding is rendered while it is **current**:

- both pages are visible (``content_visible = 1``), so a page hidden
  since takes its findings, and their model-written rationale, with it;
- both pages' lint content hashes (:func:`muninn.synthesis.tasks.lint.current_pages`)
  still equal the hashes the finding was made on, so a page whose text,
  summary or masking changed shows nothing until it is linted again;
- it belongs to the latest check of that pair with those hashes, so a
  re-check under a new prompt version or model that finds nothing clears
  it;
- every page its task showed the model is still visible
  (:func:`muninn.synthesis.tasks.lint.tainted_tasks`): the rationale may
  paraphrase a page other than the finding's two, so a task that saw a
  page hidden since renders nothing.

The rationale is model text derived from scraped pages. Ingest rejects
markup in it, and :func:`escape_rationale` escapes it again here (defence
in depth, and for rows written before that check). Evidence quotes are
stored but never rendered.

Rendering (pages with no current finding are byte-identical to before):

- ``contradicts``: on both pages, an inline
  ``<!-- contradiction: {other_page} -->`` comment followed by a visible
  callout linking the other page, with the rationale.
- ``supersedes``: on the older page only, ``superseded_at:`` (the
  earliest superseding page's date) and ``superseded_by:`` (wikilinks to
  the newer pages) in frontmatter, plus a visible callout. Both pages stay.

Output is deterministic: notes are ordered by relation, then the other
page's slug.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass

from muninn.consumers.vault.x_pages import has_table

_FRONTMATTER_END_RE = re.compile(r"\A---\n.*?\n(?=---\n)", re.S)
_H1_LINE_RE = re.compile(r"^# .*\n(?:\n)?", re.M)
_ESCAPES = {"\\": "\\\\", "&": "&amp;", "<": "&lt;", ">": "&gt;", "[": "\\[", "]": "\\]"}
_ESCAPE_RE = re.compile("|".join(re.escape(c) for c in _ESCAPES))


def escape_rationale(text: str) -> str:
    """Model-written text as inert Markdown inside a callout line: one line,
    and no HTML, comment, link or embed markup (``<!--`` can't hide the
    page, ``[[x]]`` and ``![x](url)`` are not links). Text without any of
    ``\\ & < > [ ]`` is unchanged."""
    return _ESCAPE_RE.sub(lambda m: _ESCAPES[m.group(0)], " ".join(text.split()))


@dataclass(frozen=True)
class LintNote:
    relation: str  # "contradicts" or "superseded_by"
    other_id: int
    rationale: str
    other_dated_at: str | None = None  # the superseding page's date


def load_lint_notes(conn: sqlite3.Connection, visible_ids: set[int]) -> dict[int, list[LintNote]]:
    """Current findings, as notes keyed by the page they render on."""
    if not has_table(conn, "lint_findings"):
        return {}
    from muninn.synthesis.tasks.lint import current_pages, tainted_tasks  # lazy: the task module

    findings = conn.execute(
        "SELECT lint_finding_id, relation, bookmark_id, other_bookmark_id, content_hash, "
        "       other_content_hash, rationale, task_id "
        "FROM lint_findings ORDER BY lint_finding_id"
    ).fetchall()
    findings = [f for f in findings if f["bookmark_id"] in visible_ids and f["other_bookmark_id"] in visible_ids]
    if not findings:
        return {}
    pages = current_pages(conn, {f["bookmark_id"] for f in findings} | {f["other_bookmark_id"] for f in findings})
    tainted = tainted_tasks(conn, {f["task_id"] for f in findings})

    latest: dict[tuple[int, int, str, str], str | None] = {}

    def latest_check(a: int, b: int, ha: str, hb: str) -> str | None:
        key = (a, b, ha, hb)
        if key not in latest:
            row = conn.execute(
                "SELECT task_id FROM lint_pair_checks WHERE bookmark_id_a = ? AND bookmark_id_b = ? "
                "AND content_hash_a = ? AND content_hash_b = ? ORDER BY lint_pair_check_id DESC LIMIT 1",
                key,
            ).fetchone()
            latest[key] = row["task_id"] if row else None
        return latest[key]

    current: dict[tuple[str, int, int], sqlite3.Row] = {}
    for f in findings:
        a, b = f["bookmark_id"], f["other_bookmark_id"]
        if a not in pages or b not in pages:
            continue
        if pages[a]["content_hash"] != f["content_hash"] or pages[b]["content_hash"] != f["other_content_hash"]:
            continue  # a page changed since: the finding may no longer hold
        lo, hi = (a, b) if a < b else (b, a)
        hashes = (pages[lo]["content_hash"], pages[hi]["content_hash"])
        if latest_check(lo, hi, *hashes) != f["task_id"]:
            continue  # a later check of this pair superseded this verdict
        if f["task_id"] in tainted:
            continue  # its task saw a page hidden since: the rationale may paraphrase it
        current[(f["relation"], a, b)] = f  # ordered by id: the latest wins

    notes: dict[int, list[LintNote]] = {}
    for (relation, a, b), f in current.items():
        rationale = escape_rationale(f["rationale"] or "")
        if relation == "contradicts":
            notes.setdefault(a, []).append(LintNote("contradicts", b, rationale))
            notes.setdefault(b, []).append(LintNote("contradicts", a, rationale))
        elif relation == "supersedes":
            notes.setdefault(b, []).append(
                LintNote("superseded_by", a, rationale, pages[a]["dated_at"])
            )
    return notes


def apply_lint_notes(content: str, notes: list[LintNote], slug_by_id: dict[int, str]) -> str:
    """Add frontmatter keys and callouts for ``notes`` to a rendered page.
    Returns ``content`` unchanged when no note's other page has a slug."""
    notes = [n for n in notes if n.other_id in slug_by_id]
    if not notes:
        return content
    notes = sorted(set(notes), key=lambda n: (n.relation, slug_by_id[n.other_id], n.other_id, n.rationale))

    superseded = [n for n in notes if n.relation == "superseded_by"]
    if superseded:
        fm = _FRONTMATTER_END_RE.match(content)
        if fm is not None:
            dates = sorted(n.other_dated_at for n in superseded if n.other_dated_at)
            links = sorted({f"[[{slug_by_id[n.other_id]}]]" for n in superseded})
            extra = ""
            if dates:
                extra += f"superseded_at: {dates[0]}\n"
            extra += f"superseded_by: {json.dumps(links, ensure_ascii=False)}\n"
            content = content[: fm.end()] + extra + content[fm.end():]

    blocks = []
    for n in notes:
        slug = slug_by_id[n.other_id]
        if n.relation == "contradicts":
            blocks.append(
                f"<!-- contradiction: {slug} -->\n"
                f"> [!warning] Possible contradiction\n"
                f"> This page may contradict [[{slug}]]. {n.rationale}\n"
            )
        else:
            when = f" ({n.other_dated_at})" if n.other_dated_at else ""
            blocks.append(
                f"> [!note] Superseded\n"
                f"> A newer item supersedes a claim on this page: [[{slug}]]{when}. {n.rationale}\n"
            )
    block = "\n".join(blocks) + "\n"

    fm = re.match(r"\A---\n.*?\n---\n", content, re.S)
    start = fm.end() if fm else 0
    h1 = _H1_LINE_RE.search(content, start)
    at = h1.end() if h1 else start
    return content[:at] + block + content[at:]


__all__ = ["LintNote", "apply_lint_notes", "escape_rationale", "load_lint_notes"]
