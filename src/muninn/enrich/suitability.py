"""Suitability assessment: ``muninn enrich --task suitability`` (ADR-009, RI-05).

A second enrichment task beside the per-bookmark one. For each selected
item it shows the model the item's **current content revision** (a
``scrape_history`` row) and the current revisions of the items it links
to (``item_links``: the post's article, repositories, quoted posts), with
each one's coverage. The model's judgement is validated against the
contract's ``assessment`` schema and appended to ``assessments``.

Rules (contract ``muninn-research-intake`` §4.6, RI-01 §3):

* **Idempotency key**: content revision + interest-profile revision +
  policy version + model + prompt version. An identical key is skipped
  without a call. A changed profile, policy, model or prompt appends a new
  row (``prior_assessment`` points at the item's previous assessment);
  earlier rows are never updated.
* **Bounded selection**: only rows the command selects (``sources``,
  bookmark observation window, ``limit`` on calls) are assessed. Ingest
  never triggers assessment. Hidden rows (``content_visible = 0``) are
  never selected and never shown as context.
* **Budget**: before each call the worst-case cost of that call is checked
  against what is left of ``max_cost_micro_usd``; the run stops cleanly
  when it would not fit. Spend is measured from each response's
  ``usage`` and written as a ``usage_records`` row (``cost_basis =
  'estimated'``, from :mod:`muninn.enrich.pricing`).
* **No candidate without content**: an assessment of a ``blocked``,
  ``deleted`` or ``empty`` revision cannot be ``candidate`` (schema and
  DDL). When neither the item nor any linked item has readable content,
  no call is made at all. Retrieval failures are never dispositions.
* **Model-cited evidence**: the model may cite up to ``MAX_EVIDENCE``
  verbatim excerpts, each naming the item it quotes and whether it
  supports or contradicts the item's claim or relevance. Each citation is
  checked before it is stored (``assessments.evidence_json``): the item
  must be the subject or a linked item shown in this call (so never a
  hidden one), it must have readable text, and the excerpt (at most
  ``EVIDENCE_EXCERPT_MAX`` characters) must occur in that revision's
  stored text, whitespace aside. A citation that fails is stripped and
  counted (``evidence_dropped``); the judgement itself still stands. The
  stored entry carries the cited revision's ``{ref, digest}``, filled in
  by muninn, never by the model.
* **Failure isolation**: output that fails validation is recorded as a
  failed outcome (its paid call still gets a usage row, with no
  assessment) and the run continues. API errors are counted; three in a
  row stop the run. The ``anthropic`` package is imported only when a call
  is about to be made, and capture commands never import this module.

The policy version (``muninn.config.SUITABILITY_POLICY_VERSION``) names
these deterministic rules: context selection, truncation limits, the
coverage rule and output validation. Bump it when they change.

Calls run sequentially, like ``enrich_all``: the budget check needs each
call's measured cost before the next one starts.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Sequence

from muninn.config import (
    HAIKU_MODEL,
    SUITABILITY_MODEL_ENV,
    SUITABILITY_POLICY_VERSION,
    SUITABILITY_PROMPT_VERSION,
)
from muninn.consumers.packets.canonical import new_ulid
from muninn.consumers.packets.schemas import schema_errors
from muninn.db import transaction
from muninn.enrich.haiku import _load_system_prompt, _parse_json_response
from muninn.enrich.interest_profile import InterestProfile, record_profile
from muninn.enrich.pricing import (
    cost_micro_usd,
    format_usd,
    price_for,
    worst_case_micro_usd,
)
from muninn.intake_records import (
    current_revision_row,
    export_item_id,
    iso,
    revision_text,
)

if TYPE_CHECKING:  # pragma: no cover
    import anthropic

logger = logging.getLogger(__name__)

TASK_OPERATION = "enrich:suitability"
DEFAULT_LIMIT = 20
DEFAULT_MAX_COST_MICRO_USD = 1_000_000  # $1.00 per run
MAX_OUTPUT_TOKENS = 1024
MAX_CONSECUTIVE_API_ERRORS = 3

# Context limits (part of the policy version).
SUBJECT_MAX_CHARS = 6_000
CHILD_MAX_CHARS = 3_000
CHILDREN_MAX_CHARS = 9_000
MAX_CHILDREN = 8
# Conservative chars-per-token for the pre-call worst-case estimate.
CHARS_PER_TOKEN_BOUND = 3

# Model-cited evidence limits (part of the policy version; the assessment
# schema caps excerpts at 300 and notes at 500 characters, 8 entries).
MAX_EVIDENCE = 6
EVIDENCE_EXCERPT_MAX = 300
EVIDENCE_NOTE_MAX = 500
EVIDENCE_STANCES = ("supports", "contradicts")

NO_CONTENT = frozenset({"blocked", "deleted", "empty"})
DEFAULT_SOURCES = ("x",)
_JUDGEMENT_KEYS = (
    "disposition", "relevance", "confidence", "rationale",
    "matched_interests", "unresolved_questions",
)
_RELATION_ORDER = "CASE l.relation WHEN 'links_to' THEN 0 WHEN 'article' THEN 1 WHEN 'quotes' THEN 2 ELSE 3 END"


class SuitabilityError(RuntimeError):
    """The run cannot start (bad configuration, missing dependency)."""


def default_model() -> str:
    return os.environ.get(SUITABILITY_MODEL_ENV) or HAIKU_MODEL


# ── Plan and outcome records ─────────────────────────────────────────


@dataclass
class ContextItem:
    """One item as shown to the model."""

    bookmark: sqlite3.Row
    item_id: str
    revision: sqlite3.Row | None
    relation: str | None = None
    position: int | None = None

    @property
    def coverage(self) -> str | None:
        return self.revision["coverage"] if self.revision is not None else None

    @property
    def has_content(self) -> bool:
        return self.revision is not None and self.revision["coverage"] not in NO_CONTENT


@dataclass
class PlannedCall:
    subject: ContextItem
    children: list[ContextItem]
    user_message: str
    estimated_input_tokens: int
    worst_case_micro_usd: int


@dataclass
class ItemOutcome:
    item_id: str
    status: str  # assessed | skipped_* | invalid_output | api_error | not_attempted
    assessment_id: str | None = None
    disposition: str | None = None
    cost_micro_usd: int = 0
    detail: str | None = None


@dataclass
class SuitabilityRun:
    model: str
    prompt_version: str
    policy_version: str
    profile_digest: str
    budget_micro_usd: int
    dry_run: bool = False
    selected: int = 0
    planned: int = 0
    api_calls: int = 0
    assessed: int = 0
    skipped_idempotent: int = 0
    skipped_no_revision: int = 0
    skipped_no_content: int = 0
    invalid_output: int = 0
    api_errors: int = 0
    cache_hits: int = 0
    evidence_cited: int = 0
    evidence_dropped: int = 0
    spent_micro_usd: int = 0
    estimated_micro_usd: int = 0
    stopped: str | None = None  # budget | api_errors
    outcomes: list[ItemOutcome] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "task": "suitability",
            "dry_run": self.dry_run,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "policy_version": self.policy_version,
            "profile_digest": self.profile_digest,
            "selected": self.selected,
            "planned": self.planned,
            "api_calls": self.api_calls,
            "assessed": self.assessed,
            "skipped_idempotent": self.skipped_idempotent,
            "skipped_no_revision": self.skipped_no_revision,
            "skipped_no_content": self.skipped_no_content,
            "invalid_output": self.invalid_output,
            "api_errors": self.api_errors,
            "cache_hits": self.cache_hits,
            "evidence_cited": self.evidence_cited,
            "evidence_dropped": self.evidence_dropped,
            "spent": format_usd(self.spent_micro_usd),
            "budget": format_usd(self.budget_micro_usd),
            "estimated_upper_bound": format_usd(self.estimated_micro_usd),
            "stopped": self.stopped,
        }


# ── Selection ────────────────────────────────────────────────────────


def _select_rows(
    conn: sqlite3.Connection,
    sources: Sequence[str],
    since: int | None,
    until: int | None,
) -> list[sqlite3.Row]:
    clauses = ["content_visible = 1", f"source IN ({','.join('?' * len(sources))})"]
    params: list[Any] = list(sources)
    if since is not None:
        clauses.append("captured_at >= ?")
        params.append(since)
    if until is not None:
        clauses.append("captured_at < ?")
        params.append(until)
    sql = (
        f"SELECT * FROM bookmarks WHERE {' AND '.join(clauses)} "
        "ORDER BY captured_at DESC, bookmark_id DESC"
    )
    return conn.execute(sql, params).fetchall()


def _children(conn: sqlite3.Connection, parent: sqlite3.Row) -> list[ContextItem]:
    rows = conn.execute(
        f"""
        SELECT l.child_ref, l.relation, l.position, b.*
        FROM   item_links l
        JOIN   bookmarks b ON b.bookmark_id = l.child_bookmark_id
        WHERE  l.parent_bookmark_id = ? AND b.content_visible = 1
        ORDER  BY {_RELATION_ORDER}, l.position
        """,
        (parent["bookmark_id"],),
    ).fetchall()
    out = []
    for row in rows[:MAX_CHILDREN]:
        rev = current_revision_row(conn, row["child_ref"], row["bookmark_id"])
        out.append(ContextItem(row, row["child_ref"], rev, row["relation"], row["position"]))
    return out


def _assessment_exists(
    conn: sqlite3.Connection, revision_id: int, profile: str, policy: str, model: str, prompt: str
) -> bool:
    return conn.execute(
        """
        SELECT 1 FROM assessments
        WHERE scrape_history_id = ? AND profile_sha256 = ? AND policy_version = ?
          AND model = ? AND prompt_version = ?
        """,
        (revision_id, profile, policy, model, prompt),
    ).fetchone() is not None


# ── Prompt assembly ──────────────────────────────────────────────────


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[... truncated: showing {limit} of {len(text)} characters]"


def _describe(item: ContextItem, text_limit: int) -> list[str]:
    bm = item.bookmark
    lines = [f"item_id: {item.item_id}"]
    if bm["url"]:
        lines.append(f"locator: {bm['url']}")
    if bm["title"]:
        lines.append(f"title: {bm['title'][:300]}")
    rev = item.revision
    published = rev["published_at"] if rev is not None and rev["published_at"] is not None else None
    lines.append(f"published_at: {iso(published) if published is not None else 'unknown'}")
    if rev is None:
        lines.append("coverage: not captured (no content revision)")
        return lines
    method = rev["retrieval_method"]
    if rev["coverage"] in NO_CONTENT:
        lines.append(f"coverage: {rev['coverage']} (no readable content captured; via {method})")
        return lines
    text = revision_text(rev) or ""
    lines.append(f"coverage: {rev['coverage']} (via {method}; {len(text)} characters)")
    lines.append("text:\n<<<\n" + _clip(text, text_limit) + "\n>>>")
    return lines


def build_user_message(subject: ContextItem, children: Sequence[ContextItem]) -> str:
    parts = ["SUBJECT ITEM"]
    parts += _describe(subject, SUBJECT_MAX_CHARS)
    observed = subject.bookmark["captured_at"]
    basis = "first seen by sync" if subject.bookmark["source"] == "x" else "bookmark time"
    parts.insert(2, f"bookmark_observed_at: {iso(observed)} ({basis})")
    if children:
        parts.append("")
        parts.append(f"LINKED ITEMS ({len(children)})")
        budget = CHILDREN_MAX_CHARS
        for n, child in enumerate(children, 1):
            parts.append("")
            parts.append(f"[{n}] relation: {child.relation} (position {child.position})")
            limit = max(0, min(CHILD_MAX_CHARS, budget))
            parts += _describe(child, limit)
            if child.has_content:
                budget -= min(limit, len(revision_text(child.revision) or ""))
    else:
        parts.append("")
        parts.append("LINKED ITEMS (0)")
    return "\n".join(parts)


def build_system_blocks(prompt_text: str, profile: InterestProfile) -> list[dict[str, Any]]:
    """System prompt + profile, cached as one prefix (same pattern as
    ``enrich_bookmark``). Deterministic JSON keeps the prefix byte-stable."""
    profile_json = json.dumps(profile.record, indent=2, sort_keys=True, ensure_ascii=False)
    return [
        {"type": "text", "text": prompt_text},
        {
            "type": "text",
            "text": "INTEREST PROFILE (JSON):\n" + profile_json,
            "cache_control": {"type": "ephemeral"},
        },
    ]


# ── Output validation ────────────────────────────────────────────────


def _int_or_zero(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _locate_excerpt(excerpt: str, text: str) -> str | None:
    """The span of ``text`` the excerpt quotes: an exact substring, or the
    same words with different whitespace (the stored span is returned)."""
    if excerpt in text:
        return excerpt
    words = excerpt.split()
    if not words:
        return None
    match = re.search(r"\s+".join(map(re.escape, words)), text)
    return match.group(0) if match else None


def validate_evidence(
    raw: Any, context: Sequence[ContextItem]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Model-cited evidence → the entries to store, plus why others were dropped.

    ``context`` is every item shown to the model in this call. An entry is
    kept only if it names one of those items with readable text, its stance
    is known, its note is short, and its excerpt occurs in that item's
    revision text (see :func:`_locate_excerpt`). The revision reference is
    taken from the context, never from the model. Reasons never quote the
    rejected excerpt.
    """
    if raw is None:
        return [], []
    if not isinstance(raw, list):
        return [], ["evidence is not an array"]
    by_id = {c.item_id: c for c in context}
    kept: list[dict[str, Any]] = []
    dropped: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    for n, entry in enumerate(raw):
        where = f"evidence[{n}]"
        if len(kept) >= MAX_EVIDENCE:
            dropped.append(f"{where}: more than {MAX_EVIDENCE} entries")
            continue
        if not isinstance(entry, dict):
            dropped.append(f"{where}: not an object")
            continue
        cited = entry.get("item_id")
        item = by_id.get(cited) if isinstance(cited, str) else None
        if item is None:
            dropped.append(f"{where}: cites an item that was not shown")
            continue
        text = revision_text(item.revision) if item.has_content else None
        if not text:
            dropped.append(f"{where}: cites an item with no readable text")
            continue
        stance = entry.get("stance")
        if stance not in EVIDENCE_STANCES:
            dropped.append(f"{where}: stance is not supports or contradicts")
            continue
        note = entry.get("note")
        note = note.strip() if isinstance(note, str) else ""
        if not note or len(note) > EVIDENCE_NOTE_MAX:
            dropped.append(f"{where}: note is empty or longer than {EVIDENCE_NOTE_MAX} characters")
            continue
        excerpt = entry.get("excerpt")
        excerpt = excerpt.strip() if isinstance(excerpt, str) else ""
        span = _locate_excerpt(excerpt, text) if excerpt else None
        if span is None:
            dropped.append(f"{where}: excerpt does not occur in the cited text")
            continue
        if len(span) > EVIDENCE_EXCERPT_MAX:
            dropped.append(f"{where}: excerpt is longer than {EVIDENCE_EXCERPT_MAX} characters")
            continue
        key = (item.item_id, stance, span)
        if key in seen:
            dropped.append(f"{where}: duplicate")
            continue
        seen.add(key)
        rev = item.revision
        kept.append({
            "stance": stance,
            "item_id": item.item_id,
            "source": {"ref": rev["revision_ulid"], "digest": rev["content_sha256"]},
            "excerpt": span,
            "note": note,
        })
    return kept, dropped


def build_assessment_record(
    data: Any,
    *,
    assessment_ulid: str,
    subject: ContextItem,
    profile: InterestProfile,
    policy_version: str,
    model: str,
    prompt_version: str,
    assessed_at: int,
    children: Sequence[ContextItem] = (),
    dropped: list[str] | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """The contract ``assessment`` for a model judgement, or the reasons it
    is invalid. ``prior_assessment`` is not part of the judgement and is
    added when the row is written. Evidence citations that fail
    :func:`validate_evidence` are stripped (their reasons are appended to
    ``dropped``) and do not invalidate the judgement."""
    if not isinstance(data, dict):
        return None, ["output is not a JSON object"]
    missing = [k for k in _JUDGEMENT_KEYS if k not in data]
    if missing:
        return None, [f"output is missing {', '.join(missing)}"]
    rev = subject.revision
    record = {
        "record_type": "assessment",
        "assessment_id": assessment_ulid,
        "item_id": subject.item_id,
        "content_revision": {"ref": rev["revision_ulid"], "digest": rev["content_sha256"]},
        "coverage": rev["coverage"],
        "interest_profile": {"ref": profile.profile_id, "digest": profile.digest},
        "policy_version": policy_version,
        "model": model,
        "prompt_version": prompt_version,
        "disposition": data["disposition"],
        "relevance": data["relevance"],
        "confidence": data["confidence"],
        "rationale": data["rationale"].strip() if isinstance(data["rationale"], str) else data["rationale"],
        "matched_interests": data["matched_interests"],
        "unresolved_questions": data["unresolved_questions"],
        "assessed_at": iso(assessed_at),
    }
    evidence, why = validate_evidence(data.get("evidence"), [subject, *children])
    if dropped is not None:
        dropped.extend(why)
    if evidence:
        record["evidence"] = evidence
    errors = schema_errors("assessment", record)
    if not errors:
        unknown = sorted(set(record["matched_interests"]) - profile.interest_ids)
        if unknown:
            errors.append(f"matched_interests not in the profile: {unknown}")
    return (None, errors) if errors else (record, [])


# ── Persistence ──────────────────────────────────────────────────────


def _insert_assessment(
    conn: sqlite3.Connection,
    record: dict[str, Any],
    subject: ContextItem,
    usage: dict[str, int],
    *,
    assessed_at: int,
) -> int:
    prior = conn.execute(
        "SELECT assessment_id FROM assessments WHERE item_ref = ? "
        "ORDER BY assessed_at DESC, assessment_id DESC LIMIT 1",
        (subject.item_id,),
    ).fetchone()
    cur = conn.execute(
        """
        INSERT INTO assessments (
            assessment_ulid, scrape_history_id, bookmark_id, item_ref, content_sha256,
            content_coverage, profile_sha256, policy_version, model, prompt_version,
            disposition, relevance, confidence, rationale, matched_interests,
            unresolved_questions, evidence_json, prior_assessment_id, input_token_count,
            output_token_count, assessed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record["assessment_id"],
            subject.revision["scrape_history_id"],
            subject.bookmark["bookmark_id"],
            subject.item_id,
            record["content_revision"]["digest"],
            record["coverage"],
            record["interest_profile"]["digest"],
            record["policy_version"],
            record["model"],
            record["prompt_version"],
            record["disposition"],
            record["relevance"],
            record["confidence"],
            record["rationale"],
            json.dumps(record["matched_interests"]),
            json.dumps(record["unresolved_questions"], ensure_ascii=False),
            json.dumps(record["evidence"], ensure_ascii=False) if record.get("evidence") else None,
            prior["assessment_id"] if prior else None,
            usage.get("input_tokens"),
            usage.get("output_tokens"),
            assessed_at,
        ),
    )
    return cur.lastrowid


def _insert_usage(
    conn: sqlite3.Connection,
    *,
    model: str,
    usage: dict[str, int] | None,
    cost: int,
    assessment_row_id: int | None,
    now: int,
) -> None:
    conn.execute(
        """
        INSERT INTO usage_records (
            usage_ulid, kind, provider, operation, model, assessment_id, request_count,
            input_token_count, output_token_count, cache_read_token_count,
            cost_micro_usd, cost_basis, occurred_at
        ) VALUES (?, 'model', 'anthropic', ?, ?, ?, 1, ?, ?, ?, ?, 'estimated', ?)
        """,
        (
            new_ulid(now * 1000),
            TASK_OPERATION,
            model,
            assessment_row_id,
            usage.get("input_tokens") if usage else None,
            usage.get("output_tokens") if usage else None,
            usage.get("cache_read_tokens") if usage else None,
            cost,
            now,
        ),
    )


# ── The model call ───────────────────────────────────────────────────


def _make_client() -> "anthropic.Anthropic":
    try:
        import anthropic  # lazy: capture and dry runs never need the SDK
    except ImportError as exc:
        raise SuitabilityError(
            "The anthropic package is not installed, so suitability assessment cannot run. "
            "Capture (ingest, scrape) is unaffected."
        ) from exc
    return anthropic.Anthropic()


def _measured_usage(response: Any) -> dict[str, int] | None:
    usage = getattr(response, "usage", None)
    if usage is None or not isinstance(getattr(usage, "input_tokens", None), int):
        return None
    return {
        "uncached_input_tokens": _int_or_zero(usage.input_tokens),
        "cache_write_tokens": _int_or_zero(getattr(usage, "cache_creation_input_tokens", 0)),
        "cache_read_tokens": _int_or_zero(getattr(usage, "cache_read_input_tokens", 0)),
        "output_tokens": _int_or_zero(getattr(usage, "output_tokens", 0)),
    }


def _response_text(response: Any) -> str:
    return "".join(
        getattr(block, "text", "") or ""
        for block in (getattr(response, "content", None) or [])
        if getattr(block, "type", None) == "text"
    )


# ── Orchestration ────────────────────────────────────────────────────


def assess_suitability(
    conn: sqlite3.Connection,
    profile: InterestProfile,
    client: "anthropic.Anthropic | None" = None,
    *,
    sources: Sequence[str] = DEFAULT_SOURCES,
    since: int | None = None,
    until: int | None = None,
    limit: int = DEFAULT_LIMIT,
    max_cost_micro_usd: int = DEFAULT_MAX_COST_MICRO_USD,
    model: str | None = None,
    prompt_version: str = SUITABILITY_PROMPT_VERSION,
    policy_version: str = SUITABILITY_POLICY_VERSION,
    dry_run: bool = False,
    now: int | None = None,
) -> SuitabilityRun:
    """Assess up to ``limit`` selected items; see the module docstring."""
    if limit < 1:
        raise SuitabilityError("--limit must be at least 1")
    if not sources:
        raise SuitabilityError("select at least one --source")
    model = model or default_model()
    try:
        price = price_for(model)
    except KeyError as exc:
        raise SuitabilityError(str(exc.args[0])) from exc
    try:
        prompt_text = _load_system_prompt(prompt_version)
    except FileNotFoundError as exc:
        raise SuitabilityError(f"Unknown prompt version {prompt_version!r}") from exc

    clock = (lambda: now) if now is not None else (lambda: int(time.time()))
    run = SuitabilityRun(
        model=model,
        prompt_version=prompt_version,
        policy_version=policy_version,
        profile_digest=profile.digest,
        budget_micro_usd=max_cost_micro_usd,
        dry_run=dry_run,
    )
    system = build_system_blocks(prompt_text, profile)
    system_chars = sum(len(b["text"]) for b in system)

    # ── plan (read-only) ──
    plan: list[PlannedCall] = []
    for row in _select_rows(conn, sources, since, until):
        if len(plan) >= limit:
            break
        run.selected += 1
        try:
            item_id = export_item_id(row)
        except ValueError:
            run.skipped_no_revision += 1
            run.outcomes.append(ItemOutcome(f"bookmark:{row['bookmark_id']}", "skipped_no_identity"))
            continue
        revision = current_revision_row(conn, item_id, row["bookmark_id"])
        if revision is None:
            run.skipped_no_revision += 1
            run.outcomes.append(ItemOutcome(item_id, "skipped_no_revision"))
            continue
        if _assessment_exists(
            conn, revision["scrape_history_id"], profile.digest, policy_version, model, prompt_version
        ):
            run.skipped_idempotent += 1
            run.outcomes.append(ItemOutcome(item_id, "skipped_idempotent"))
            continue
        subject = ContextItem(row, item_id, revision)
        children = _children(conn, row)
        if not subject.has_content and not any(c.has_content for c in children):
            run.skipped_no_content += 1
            run.outcomes.append(ItemOutcome(item_id, "skipped_no_content", detail=revision["coverage"]))
            continue
        message = build_user_message(subject, children)
        tokens = math.ceil((system_chars + len(message)) / CHARS_PER_TOKEN_BOUND)
        worst = worst_case_micro_usd(price, input_tokens=tokens, max_output_tokens=MAX_OUTPUT_TOKENS)
        plan.append(PlannedCall(subject, children, message, tokens, worst))
    run.planned = len(plan)
    run.estimated_micro_usd = sum(p.worst_case_micro_usd for p in plan)

    if dry_run:
        for p in plan:
            run.outcomes.append(
                ItemOutcome(p.subject.item_id, "planned", cost_micro_usd=p.worst_case_micro_usd,
                            detail=f"coverage={p.subject.coverage}; linked={len(p.children)}")
            )
        return run
    if not plan:
        return run

    with transaction(conn):
        record_profile(conn, profile, now=clock())

    consecutive_errors = 0
    for index, call in enumerate(plan):
        item_id = call.subject.item_id
        if run.spent_micro_usd + call.worst_case_micro_usd > max_cost_micro_usd:
            run.stopped = "budget"
            for rest in plan[index:]:
                run.outcomes.append(ItemOutcome(rest.subject.item_id, "not_attempted", detail="budget"))
            break
        if client is None:
            client = _make_client()
        try:
            response = client.messages.create(
                model=model,
                max_tokens=MAX_OUTPUT_TOKENS,
                system=system,
                messages=[{"role": "user", "content": call.user_message}],
            )
        except Exception as exc:  # noqa: BLE001 - isolate API failures from the run
            consecutive_errors += 1
            run.api_errors += 1
            logger.error("Suitability call failed for %s: %s", item_id, type(exc).__name__)
            run.outcomes.append(ItemOutcome(item_id, "api_error", detail=type(exc).__name__))
            if consecutive_errors >= MAX_CONSECUTIVE_API_ERRORS:
                run.stopped = "api_errors"
                for rest in plan[index + 1:]:
                    run.outcomes.append(ItemOutcome(rest.subject.item_id, "not_attempted", detail="api_errors"))
                break
            continue
        consecutive_errors = 0
        run.api_calls += 1
        ts = clock()

        measured = _measured_usage(response)
        if measured is None:
            cost = call.worst_case_micro_usd  # unmeasurable: charge the bound
            stored_usage: dict[str, int] | None = None
        else:
            cost = cost_micro_usd(
                price,
                input_tokens=measured["uncached_input_tokens"],
                cache_write_tokens=measured["cache_write_tokens"],
                cache_read_tokens=measured["cache_read_tokens"],
                output_tokens=measured["output_tokens"],
            )
            if measured["cache_read_tokens"] > 0:
                run.cache_hits += 1
            # usage_records has no cache-write column: input_token_count is
            # every non-cache-read input token; the cost carries the split.
            stored_usage = {
                "input_tokens": measured["uncached_input_tokens"] + measured["cache_write_tokens"],
                "output_tokens": measured["output_tokens"],
                "cache_read_tokens": measured["cache_read_tokens"],
            }
        run.spent_micro_usd += cost

        errors: list[str]
        record: dict[str, Any] | None = None
        dropped: list[str] = []
        if getattr(response, "stop_reason", None) == "max_tokens":
            errors = ["output truncated at max_tokens"]
        else:
            try:
                data = _parse_json_response(_response_text(response))
            except ValueError:
                data, errors = None, ["output is not valid JSON"]
            else:
                record, errors = build_assessment_record(
                    data,
                    assessment_ulid=new_ulid(ts * 1000),
                    subject=call.subject,
                    profile=profile,
                    policy_version=policy_version,
                    model=model,
                    prompt_version=prompt_version,
                    assessed_at=ts,
                    children=call.children,
                    dropped=dropped,
                )

        with transaction(conn):
            row_id = None
            if record is not None:
                row_id = _insert_assessment(
                    conn, record, call.subject, stored_usage or {}, assessed_at=ts
                )
            _insert_usage(conn, model=model, usage=stored_usage, cost=cost, assessment_row_id=row_id, now=ts)

        if record is None:
            run.invalid_output += 1
            logger.warning("Suitability output for %s rejected: %s", item_id, "; ".join(errors[:3]))
            run.outcomes.append(
                ItemOutcome(item_id, "invalid_output", cost_micro_usd=cost, detail="; ".join(errors[:3]))
            )
        else:
            run.assessed += 1
            run.evidence_cited += len(record.get("evidence", ()))
            run.evidence_dropped += len(dropped)
            if dropped:
                logger.warning("Suitability evidence for %s stripped: %s", item_id, "; ".join(dropped[:3]))
            run.outcomes.append(
                ItemOutcome(item_id, "assessed", record["assessment_id"], record["disposition"], cost)
            )
        if run.spent_micro_usd >= max_cost_micro_usd and index + 1 < len(plan):
            run.stopped = "budget"
            for rest in plan[index + 1:]:
                run.outcomes.append(ItemOutcome(rest.subject.item_id, "not_attempted", detail="budget"))
            break

    logger.info("Suitability run: %s", run.summary())
    return run
