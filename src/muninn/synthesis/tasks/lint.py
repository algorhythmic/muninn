"""lint task: contradictions and superseded claims among pages sharing a tag.

The model tier of vault lint (roadmap Phase 6, docs/vault-lint.md). It reads
only compiled bookmark pages, never authored ones, and only their stored
content from SQLite.

Materials in the task file:
  - tag             the tag the pages share
  - pairs: [[a, b], ...]   the pairs to judge, a < b, sorted: only the
                           pending ones (see below)
  - pages: [{bookmark_id, title, source, published_at, captured_at,
             dated_at, dated_by, summary, text, text_truncated,
             content_hash}, ...]   oldest first; exactly the pages that
                                   appear in ``pairs``

  The candidates are visible bookmarks (``content_visible = 1``: the
  compiler writes a page for each) whose enrichment tags include ``tag``,
  newest ``limit``. A pair of candidates is *pending* unless it was already
  checked with the same two content hashes and prompt version (and, for an
  API run, model), under any tag: the check is keyed by content, not by
  tag. Only pending pairs, and only their pages, go into a task, so one
  new page among ``limit`` costs ``limit - 1`` pairs, not all of them, and
  a pair is never re-judged as a side effect of an unrelated page.
  ``dated_at`` is the publication time when known, else the
  bookmark/capture time, and orders a supersession. ``text`` is the
  captured text of the canonical pass, clipped to :data:`TEXT_LIMIT`. An
  X post that links a policy-hidden item gets the vault's treatment
  (``consumers/vault/x_pages.py``): the hidden locators are masked out of
  its text and title, and enrichment that quotes one is withheld whole.
  ``content_hash`` is ``sha256:`` over the page entry's canonical JSON
  without itself; the vault compiler recomputes it to tell whether a
  finding is still current.

Validation (after the result schema): both ids of a finding are pages of
the task and differ, and the pair is one of ``pairs``; a ``supersedes`` finding's ``bookmark_id`` is dated
strictly after ``other_bookmark_id``; every evidence quote names one of
the finding's two pages and occurs in that page's ``text`` or ``summary``
(exactly, or with different whitespace), never across a masked locator;
each of the two pages is quoted at least once; the rationale is plain text
(:data:`RATIONALE_FORBIDDEN`: no HTML or comment markup, no wikilinks, no
URLs), because the compiler renders it into vault pages.

Third pages: a task shows the model every page in its pairs, so a
rationale may paraphrase a page other than its finding's two. The task's
page set is recorded implicitly: it is the union of the task's
``lint_pair_checks`` pairs (exactly its ``materials.pages``). A task is
*tainted* (:func:`tainted_tasks`) once any page of that set is no longer
visible: its findings stop rendering, and a pair on which it found
something is pending again, so a re-check without the hidden page can
restore the finding. (No code path deletes a bookmark row; a manually
deleted page's checks cascade away, so it can't taint.)

DB writes (single transaction, through the orchestrator):
  1. INSERT one ``lint_pair_checks`` row per pair of ``pairs``, finding or
     not: the idempotency record. A later run leaves a pair out when it
     was already checked with the same two content hashes and prompt
     version (and, for an API run, model), and skips the model call when
     no pair is left. Pairs not in ``pairs`` are never recorded, so a
     task cannot overwrite the verdict on a pair it was not asked about.
  2. INSERT one ``lint_findings`` row per finding (contradictions stored
     with the lower bookmark id first), with both content hashes, the
     model, prompt version, run id and the located evidence spans.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import re
import sqlite3
import time
from typing import Any, Iterable

from muninn.consumers.vault import x_pages
from muninn.models import SynthesisRun
from muninn.synthesis.tasks.deep_pass import _canonical_content_text

TASK = "lint"
PROMPT_VERSION = "lint_v2"
DEFAULT_LIMIT = 20
MAX_LIMIT = 40
MIN_PAGES = 2
#: Characters of captured text per page (part of the content hash).
TEXT_LIMIT = 4000
SUMMARY_LIMIT = 2000
RELATIONS = ("contradicts", "supersedes")
#: A rationale containing any of these is rejected: it is rendered into a
#: compiled page, where ``<!--`` would hide the rest of the page, ``[[…]]``
#: would become a link and a URL could name a since-hidden item.
RATIONALE_FORBIDDEN = ("<", ">", "[[", "]]", "://")

_MASKS = (x_pages.HIDDEN_LINK_TEXT, x_pages.ENRICHMENT_WITHHELD)


class LintInputError(LookupError):
    """The tag has too few visible pages to compare."""


# ── page materials ─────────────────────────────────────────────────


def _canonical_hash(entry: dict[str, Any]) -> str:
    body = {k: v for k, v in entry.items() if k != "content_hash"}
    data = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _clip(text: str | None, limit: int) -> tuple[str | None, bool]:
    if not text:
        return None, False
    text = text.strip()
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _published_at(conn: sqlite3.Connection, row: sqlite3.Row) -> int | None:
    meta = x_pages.parse_metadata(row["source_metadata"])
    published = x_pages._to_epoch(meta.get("published_at"))
    if published is None:
        published = x_pages._history_published_at(conn, row["bookmark_id"])
    return published


def page_entry(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any] | None:
    """The lint material for one visible bookmark row, or None when it has
    nothing to compare (no summary and no text)."""
    if not row["content_visible"]:
        return None  # a hidden row never reaches a model
    bid = row["bookmark_id"]
    enr = conn.execute("SELECT summary, key_quotes, entities FROM enriched WHERE bookmark_id = ?", (bid,)).fetchone()
    summary = enr["summary"] if enr else None
    title = row["title"]
    text, _ = _canonical_content_text(conn, bid, row["enrichment_source"])
    if row["source"] in x_pages.X_SOURCES:
        hidden = x_pages.hidden_child_urls(conn, bid)
        if hidden:
            summary, _, _, withheld = x_pages.withhold_enrichment(
                hidden, summary, enr["key_quotes"] if enr else None, enr["entities"] if enr else None
            )
            if withheld:
                summary = None
            if text:
                text = x_pages.mask_hidden_links(text, hidden)
            if title:
                title = x_pages.mask_hidden_title(title, hidden)
    summary, _ = _clip(summary, SUMMARY_LIMIT)
    text, truncated = _clip(text, TEXT_LIMIT)
    if not summary and not text:
        return None
    published = _published_at(conn, row)
    dated = published if published is not None else row["captured_at"]
    entry: dict[str, Any] = {
        "bookmark_id": bid,
        "title": title or row["url"] or "(untitled)",
        "source": row["source"],
        "published_at": x_pages.iso_utc(published),
        "captured_at": x_pages.iso_utc(row["captured_at"]),
        "dated_at": x_pages.iso_utc(dated),
        "dated_by": "published" if published is not None else "captured",
        "summary": summary,
        "text": text,
        "text_truncated": truncated,
    }
    entry["content_hash"] = _canonical_hash(entry)
    return entry


_ROW_SQL = (
    "SELECT b.bookmark_id, b.source, b.source_id, b.captured_at, b.title, b.url, "
    "       b.content_visible, b.enrichment_source, b.source_metadata "
    "FROM bookmarks b "
)


def current_pages(conn: sqlite3.Connection, bookmark_ids: Iterable[int]) -> dict[int, dict[str, Any]]:
    """Lint material, as a task would build it now, for the visible ids
    among ``bookmark_ids`` (the vault compiler checks findings with it)."""
    ids = sorted(set(bookmark_ids))
    out: dict[int, dict[str, Any]] = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        rows = conn.execute(
            _ROW_SQL + f"WHERE b.content_visible = 1 AND b.bookmark_id IN ({','.join('?' * len(chunk))})",
            chunk,
        ).fetchall()
        for row in rows:
            entry = page_entry(conn, row)
            if entry is not None:
                out[row["bookmark_id"]] = entry
    return out


def select_pages(conn: sqlite3.Connection, tag: str, limit: int = DEFAULT_LIMIT) -> list[dict[str, Any]]:
    """The newest ``limit`` comparable visible pages tagged ``tag``
    (case-insensitive), returned oldest first."""
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
    rows = conn.execute(
        _ROW_SQL
        + "JOIN enriched e ON e.bookmark_id = b.bookmark_id "
        "WHERE b.content_visible = 1 AND json_valid(e.tags) "
        "  AND EXISTS (SELECT 1 FROM json_each(e.tags) t WHERE lower(t.value) = lower(?)) "
        "ORDER BY b.bookmark_id",
        (tag,),
    ).fetchall()
    entries = [e for e in (page_entry(conn, r) for r in rows) if e is not None]
    entries.sort(key=lambda e: (e["dated_at"], e["bookmark_id"]), reverse=True)
    chosen = entries[:limit]
    chosen.sort(key=lambda e: (e["dated_at"], e["bookmark_id"]))
    return chosen


def prepare_task(
    conn: sqlite3.Connection,
    tag: str,
    *,
    limit: int = DEFAULT_LIMIT,
    prompt_version: str = PROMPT_VERSION,
    model: str | None = None,
) -> tuple[dict[str, Any], int]:
    """The task materials for ``tag``, and the number of candidate pairs.

    ``materials["pairs"]`` holds only the pending pairs and
    ``materials["pages"]`` only their pages; both are empty when every
    candidate pair was already checked (nothing to pay for)."""
    tag = (tag or "").strip()
    if not tag:
        raise LintInputError("a lint task needs a tag")
    pages = select_pages(conn, tag, limit)
    if len(pages) < MIN_PAGES:
        raise LintInputError(
            f"tag {tag!r} has {len(pages)} visible page(s) with content; lint compares at least {MIN_PAGES}"
        )
    pending = pending_pairs(conn, pages, prompt_version=prompt_version, model=model)
    involved = {bid for pair in pending for bid in pair}
    materials = {
        "tag": tag,
        "pairs": [[a, b] for a, b in pending],
        "pages": [p for p in pages if p["bookmark_id"] in involved],
    }
    return materials, len(pages) * (len(pages) - 1) // 2


def prepare_input(
    conn: sqlite3.Connection,
    tag: str,
    *,
    limit: int = DEFAULT_LIMIT,
    prompt_version: str = PROMPT_VERSION,
    model: str | None = None,
) -> dict[str, Any]:
    return prepare_task(conn, tag, limit=limit, prompt_version=prompt_version, model=model)[0]


# ── idempotency ────────────────────────────────────────────────────


def page_pairs(pages: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Every unordered pair, lower bookmark id first."""
    ordered = sorted(pages, key=lambda p: p["bookmark_id"])
    return list(itertools.combinations(ordered, 2))


def task_pairs(materials: dict[str, Any]) -> set[tuple[int, int]]:
    """The pairs a task asks about, lower id first. A task file emitted
    before ``materials.pairs`` existed (lint_v1) asked about every pair of
    its pages."""
    if "pairs" in materials:
        return {(min(p), max(p)) for p in materials["pairs"] if len(p) == 2}
    return {(a["bookmark_id"], b["bookmark_id"]) for a, b in page_pairs(materials.get("pages") or [])}


def pending_pairs(
    conn: sqlite3.Connection,
    pages: list[dict[str, Any]],
    *,
    prompt_version: str,
    model: str | None = None,
) -> list[tuple[int, int]]:
    """Pairs not yet checked with these two content hashes under this
    prompt version (and model, when given: the id itself or a dated
    snapshot of it, ``<model>-<digits…>``). Empty means re-running would
    pay for nothing new."""
    ids = sorted({p["bookmark_id"] for p in pages})
    if not ids:
        return []
    sql = (
        "SELECT bookmark_id_a, bookmark_id_b, content_hash_a, content_hash_b, task_id FROM lint_pair_checks "
        f"WHERE prompt_version = ? AND bookmark_id_a IN ({','.join('?' * len(ids))})"
    )
    params: list[Any] = [prompt_version, *ids]
    if model is not None:
        # The row records the served model: the requested id, or a dated
        # snapshot of it when the API resolves an alias.
        sql += " AND (model = ? OR model GLOB ?)"
        params += [model, f"{model}-[0-9]*"]
    rows = [tuple(r) for r in conn.execute(sql, params)]
    # A check from a tainted task (it showed the model a page hidden since)
    # no longer counts for a pair it found something on: that finding
    # stops rendering, so the pair is judged again without the hidden page.
    tainted = tainted_tasks(conn, {r[4] for r in rows})
    voided = _finding_pairs(conn, tainted)
    done = {r[:4] for r in rows if (r[4], r[0], r[1]) not in voided}
    return [
        (a["bookmark_id"], b["bookmark_id"])
        for a, b in page_pairs(pages)
        if (a["bookmark_id"], b["bookmark_id"], a["content_hash"], b["content_hash"]) not in done
    ]


def _chunks(values: Iterable[Any], size: int = 500) -> Iterable[list[Any]]:
    items = sorted(set(values))
    for start in range(0, len(items), size):
        yield items[start:start + size]


def tainted_tasks(conn: sqlite3.Connection, task_ids: Iterable[str]) -> set[str]:
    """The lint tasks among ``task_ids`` that showed the model a page that
    is no longer visible (a page of any of the task's checked pairs)."""
    out: set[str] = set()
    for chunk in _chunks(task_ids):
        marks = ",".join("?" * len(chunk))
        out.update(
            r[0] for r in conn.execute(
                "SELECT c.task_id FROM lint_pair_checks c JOIN bookmarks b ON b.bookmark_id = c.bookmark_id_a "
                f"WHERE b.content_visible = 0 AND c.task_id IN ({marks}) "
                "UNION SELECT c.task_id FROM lint_pair_checks c JOIN bookmarks b ON b.bookmark_id = c.bookmark_id_b "
                f"WHERE b.content_visible = 0 AND c.task_id IN ({marks})",
                [*chunk, *chunk],
            )
        )
    return out


def _finding_pairs(conn: sqlite3.Connection, task_ids: set[str]) -> set[tuple[str, int, int]]:
    """(task_id, lower id, higher id) of every finding of ``task_ids``."""
    out: set[tuple[str, int, int]] = set()
    for chunk in _chunks(task_ids):
        out.update(
            (r[0], r[1], r[2]) for r in conn.execute(
                "SELECT task_id, min(bookmark_id, other_bookmark_id), max(bookmark_id, other_bookmark_id) "
                f"FROM lint_findings WHERE task_id IN ({','.join('?' * len(chunk))})",
                chunk,
            )
        )
    return out


# ── validation ─────────────────────────────────────────────────────


def locate_quote(quote: str, text: str | None) -> str | None:
    """The span of ``text`` the quote cites: an exact substring, or the
    same words with different whitespace (the stored span is returned)."""
    if not text or not quote:
        return None
    if quote in text:
        return quote
    words = quote.split()
    if not words:
        return None
    match = re.search(r"\s+".join(map(re.escape, words)), text)
    return match.group(0) if match else None


def _locate_in_page(quote: str, page: dict[str, Any]) -> tuple[str, str] | None:
    if any(mask in quote for mask in _MASKS):
        return None  # quotes muninn's placeholder, not the stored text
    for field in ("text", "summary"):
        span = locate_quote(quote.strip(), page.get(field))
        if span is not None:
            return field, span
    return None


def validate_output(output: dict[str, Any], materials: dict[str, Any]) -> list[str]:
    """Checks beyond the schema; an empty list means the findings hold."""
    pages = {p["bookmark_id"]: p for p in materials.get("pages") or []}
    pairs = task_pairs(materials)
    errors: list[str] = []
    for i, finding in enumerate(output.get("findings") or []):
        where = f"findings[{i}]"
        a, b = finding.get("bookmark_id"), finding.get("other_bookmark_id")
        missing = [x for x in (a, b) if x not in pages]
        if missing:
            errors.append(f"{where}: bookmark id(s) {missing} are not pages of this task")
            continue
        if a == b:
            errors.append(f"{where}: bookmark_id and other_bookmark_id are the same page")
            continue
        if (min(a, b), max(a, b)) not in pairs:
            errors.append(
                f"{where}: the pair [{min(a, b)}, {max(a, b)}] is not in materials.pairs "
                "(it was already checked; judge only the listed pairs)"
            )
            continue
        bad = [s for s in RATIONALE_FORBIDDEN if s in (finding.get("rationale") or "")]
        if bad:
            errors.append(
                f"{where}: the rationale must be plain text; it contains {', '.join(repr(s) for s in bad)} "
                "(no HTML or comment markup, no [[links]], no URLs)"
            )
        if finding.get("relation") == "supersedes" and not pages[a]["dated_at"] > pages[b]["dated_at"]:
            errors.append(
                f"{where}: supersedes needs bookmark_id {a} (dated {pages[a]['dated_at']}) to be newer "
                f"than other_bookmark_id {b} (dated {pages[b]['dated_at']})"
            )
        quoted: set[int] = set()
        for j, ev in enumerate(finding.get("evidence") or []):
            cited = ev.get("bookmark_id")
            if cited not in (a, b):
                errors.append(f"{where}.evidence[{j}]: cites bookmark {cited}, not one of the pair")
                continue
            if _locate_in_page(ev.get("quote") or "", pages[cited]) is None:
                errors.append(
                    f"{where}.evidence[{j}]: the quote is not in bookmark {cited}'s text or summary "
                    "(copy the characters exactly)"
                )
                continue
            quoted.add(cited)
        for side in (a, b):
            if side not in quoted:
                errors.append(f"{where}: no verified evidence quote from bookmark {side}")
    return errors


# ── writes ─────────────────────────────────────────────────────────


def write_output(
    conn: sqlite3.Connection,
    output: dict[str, Any],
    run: SynthesisRun,
    *,
    tag: str,
    materials: dict[str, Any],
) -> None:
    """Record every pair the task asked about as checked, and every
    finding on one of those pairs, from the registry copy's materials (a
    harness that edits its own copy changes nothing). Pages whose bookmark
    was deleted since the task was built are skipped."""
    meta = output.get("synthesis_metadata") or {}
    model = run.enrichment_model or meta.get("model") or "unknown"
    prompt_version = run.enrichment_prompt_version or meta.get("prompt_version") or PROMPT_VERSION
    now = int(time.time())
    pages = {p["bookmark_id"]: p for p in materials["pages"]}
    pairs = task_pairs(materials)
    ids = sorted(pages)
    existing = {
        r[0] for r in conn.execute(
            f"SELECT bookmark_id FROM bookmarks WHERE bookmark_id IN ({','.join('?' * len(ids))})", ids
        )
    }
    for pa, pb in page_pairs(list(pages.values())):
        if (pa["bookmark_id"], pb["bookmark_id"]) not in pairs:
            continue
        if pa["bookmark_id"] not in existing or pb["bookmark_id"] not in existing:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO lint_pair_checks (bookmark_id_a, bookmark_id_b, content_hash_a, "
            "content_hash_b, model, prompt_version, task_id, tag, checked_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (pa["bookmark_id"], pb["bookmark_id"], pa["content_hash"], pb["content_hash"],
             model, prompt_version, run.task_id, tag, now),
        )
    for finding in output.get("findings") or []:
        a, b = finding["bookmark_id"], finding["other_bookmark_id"]
        if (min(a, b), max(a, b)) not in pairs:
            continue  # validation rejects it; never record a verdict nobody asked for
        if a not in existing or b not in existing:
            continue
        if finding["relation"] == "contradicts" and a > b:
            a, b = b, a  # symmetric: one orientation
        evidence = []
        for ev in finding["evidence"]:
            located = _locate_in_page(ev["quote"], pages[ev["bookmark_id"]])
            if located is not None:
                evidence.append({"bookmark_id": ev["bookmark_id"], "field": located[0], "quote": located[1]})
        conn.execute(
            "INSERT OR IGNORE INTO lint_findings (relation, bookmark_id, other_bookmark_id, content_hash, "
            "other_content_hash, rationale, evidence_json, model, prompt_version, task_id, tag, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (finding["relation"], a, b, pages[a]["content_hash"], pages[b]["content_hash"],
             " ".join(finding["rationale"].split()), json.dumps(evidence, ensure_ascii=False),
             model, prompt_version, run.task_id, tag, now),
        )
