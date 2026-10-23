"""Evidence-packet export (contract ``muninn-research-intake`` 0.1.0-draft).

File-level pieces (the contract's digest and resolution rules):

* :func:`write_packet` writes a packet as canonical JSON bytes, so that
  ``sha256sum`` of the file equals the packet digest.
* :func:`make_tombstone` and :func:`withdraw_packet` replace a packet's
  content with its tombstone.
* :func:`resolve_packet` resolves a ``{ref, digest}`` reference to
  ``active``, ``withdrawn``, ``missing`` or ``digest_mismatch``.
* :func:`packet_integrity_errors` checks the cross-record rules the JSON
  Schema cannot express.

Database-level pieces (``muninn export packets``):

* :func:`export_packets` first withdraws every active packet whose
  material was deleted or hidden (:func:`withdrawals_due`), then builds
  packets from assessments, validates them (schema + integrity) and writes
  and records the valid ones; see the selection, evidence, revision and
  withdrawal rules above it.
* :func:`withdraw_exported_packet` withdraws one recorded revision.

Export layout under the output directory::

    <packet ULID>/<revision>.json            active packet (canonical JSON)
    <packet ULID>/<revision>.withdrawn.json  tombstone that replaced it

CLI::

    muninn export packets [--out DIR] [--item ITEM_ID]... [--since DATE]
                          [--disposition D]... [--limit N] [--dry-run]
    muninn export packets --withdraw PACKET_ID@REVISION
                          --reason {retention,operator_request,source_deleted,policy_hidden}
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Literal, Sequence

from muninn.consumers.packets.canonical import (
    CanonicalizationError,
    canonical_json,
    content_digest,
    is_ulid,
    new_ulid,
    normalize_text,
    record_digest,
    sha256_hex,
)

CONTRACT_NAME = "muninn-research-intake"
CONTRACT_VERSION = "0.1.0-draft"

WithdrawalReason = Literal["retention", "operator_request", "source_deleted", "policy_hidden"]
WITHDRAWAL_REASONS: tuple[str, ...] = (
    "retention", "operator_request", "source_deleted", "policy_hidden",
)
ResolutionStatus = Literal["active", "withdrawn", "missing", "digest_mismatch"]


@dataclass(frozen=True)
class ExportedPacket:
    """One packet revision written by the exporter."""

    packet_id: str
    revision: int
    digest: str
    path: Path
    status: Literal["active", "withdrawn"]
    revisions_withdrawn: int = 0  # content revisions cleared with a withdrawal

    @property
    def ref(self) -> str:
        return f"{self.packet_id}@{self.revision}"


@dataclass(frozen=True)
class PacketResolution:
    """Result of resolving a ``{ref, digest}`` packet reference."""

    ref: str
    status: ResolutionStatus
    path: Path | None
    record: dict[str, Any] | None = None


def parse_packet_ref(ref: str) -> tuple[str, int]:
    packet_id, sep, rev = ref.partition("@")
    if not sep or not is_ulid(packet_id) or not rev.isdigit() or int(rev) < 1:
        raise ValueError(f"not a packet reference: {ref!r}")
    return packet_id, int(rev)


def packet_path(out_dir: Path | str, packet_id: str, revision: int, *, withdrawn: bool = False) -> Path:
    suffix = ".withdrawn.json" if withdrawn else ".json"
    return Path(out_dir) / packet_id / f"{revision}{suffix}"


def write_packet(out_dir: Path | str, packet: dict[str, Any]) -> ExportedPacket:
    """Write an active packet as canonical JSON. The caller validates it first."""
    if packet.get("status") != "active":
        raise ValueError("write_packet writes active packets; use withdraw_packet for tombstones")
    data = canonical_json(packet)
    path = packet_path(out_dir, packet["packet_id"], packet["revision"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return ExportedPacket(packet["packet_id"], packet["revision"], sha256_hex(data), path, "active")


def make_tombstone(packet: dict[str, Any], *, reason: str, withdrawn_at: str) -> dict[str, Any]:
    """The tombstone that replaces ``packet``; it keeps the original digest."""
    if reason not in WITHDRAWAL_REASONS:
        raise ValueError(f"unknown withdrawal reason: {reason!r}")
    return {
        "record_type": "evidence-packet",
        "contract": CONTRACT_NAME,
        "contract_version": packet["contract_version"],
        "status": "withdrawn",
        "packet_id": packet["packet_id"],
        "revision": packet["revision"],
        "digest": record_digest(packet),
        "withdrawn_at": withdrawn_at,
        "withdrawal_reason": reason,
    }


def withdraw_packet(out_dir: Path | str, packet_id: str, revision: int, *, reason: str, withdrawn_at: str) -> ExportedPacket:
    """Replace an exported packet's content with its tombstone."""
    active = packet_path(out_dir, packet_id, revision)
    tombstone = make_tombstone(json.loads(active.read_bytes()), reason=reason, withdrawn_at=withdrawn_at)
    tomb_path = packet_path(out_dir, packet_id, revision, withdrawn=True)
    tomb_path.write_bytes(canonical_json(tombstone))
    active.unlink()
    return ExportedPacket(packet_id, revision, tombstone["digest"], tomb_path, "withdrawn")


def resolve_packet(out_dir: Path | str, ref: str, digest: str) -> PacketResolution:
    """Resolve a packet reference. A withdrawn packet resolves to ``withdrawn``
    (when the tombstone carries the referenced digest), never to a dangling path."""
    packet_id, revision = parse_packet_ref(ref)
    active = packet_path(out_dir, packet_id, revision)
    if active.exists():
        data = active.read_bytes()
        record = json.loads(data)
        if record_digest(record) != digest:
            return PacketResolution(ref, "digest_mismatch", active, None)
        return PacketResolution(ref, "active", active, record)
    tomb = packet_path(out_dir, packet_id, revision, withdrawn=True)
    if tomb.exists():
        record = json.loads(tomb.read_bytes())
        status: ResolutionStatus = "withdrawn" if record.get("digest") == digest else "digest_mismatch"
        return PacketResolution(ref, status, tomb, record)
    return PacketResolution(ref, "missing", None, None)


def packet_integrity_errors(packet: dict[str, Any]) -> list[str]:
    """Cross-record rules an active packet must meet beyond its JSON Schema.

    Every reference inside the packet resolves inside the packet, with the
    right digest: revisions by ``content_sha256``, assessments and profiles
    by the sha256 of their canonical JSON. Included text matches its digest
    and is already normalized. Every excerpt (packet evidence and the
    model-cited evidence of each assessment) occurs in the text of the
    revision it cites, which the packet must include, and an assessment's
    evidence cites only items in the packet. Returns a list of problems
    (empty if sound).
    """
    errors: list[str] = []
    item_ids = [i["item_id"] for i in packet["items"]]
    items = set(item_ids)
    if len(items) != len(item_ids):
        errors.append("items: duplicate item_id")
    if packet["subject_item_id"] not in items:
        errors.append("subject_item_id is not in items")

    for n, link in enumerate(packet["item_links"]):
        if link["parent_item_id"] == link["child_item_id"]:
            errors.append(f"item_links[{n}]: parent equals child")
        for end in ("parent_item_id", "child_item_id"):
            if link[end] not in items:
                errors.append(f"item_links[{n}].{end} is not in items")
    for n, obs in enumerate(packet["observations"]):
        if obs["item_id"] not in items:
            errors.append(f"observations[{n}].item_id is not in items")

    revisions: dict[str, dict[str, Any]] = {}
    for n, rev in enumerate(packet["content_revisions"]):
        if rev["revision_id"] in revisions:
            errors.append(f"content_revisions[{n}]: duplicate revision_id")
        revisions[rev["revision_id"]] = rev
        if rev["item_id"] not in items:
            errors.append(f"content_revisions[{n}].item_id is not in items")
        if "text" in rev:
            text = rev["text"]
            if normalize_text(text) != text:
                errors.append(f"content_revisions[{n}].text is not normalized")
            if content_digest(text) != rev["content_sha256"]:
                errors.append(f"content_revisions[{n}].text does not match content_sha256")
            if len(text) != rev["char_count"]:
                errors.append(f"content_revisions[{n}].char_count does not match text")

    def check_revision_ref(where: str, ref: dict[str, str]) -> dict[str, Any] | None:
        rev = revisions.get(ref["ref"])
        if rev is None:
            errors.append(f"{where}: revision {ref['ref']} is not in content_revisions")
        elif rev["content_sha256"] != ref["digest"]:
            errors.append(f"{where}: digest does not match revision {ref['ref']}")
        return rev

    def check_excerpt(where: str, rev: dict[str, Any], excerpt: str | None) -> None:
        if not excerpt:
            return
        if "text" not in rev:
            errors.append(f"{where}.excerpt cites a revision whose text is not included")
        elif excerpt not in rev["text"]:
            errors.append(f"{where}.excerpt is not in the revision text")

    profiles = {record_digest(p): p for p in packet["interest_profiles"]}
    assessments: dict[str, str] = {}
    for n, a in enumerate(packet["assessments"]):
        where = f"assessments[{n}]"
        assessments[a["assessment_id"]] = record_digest(a)
        rev = check_revision_ref(f"{where}.content_revision", a["content_revision"])
        if rev is not None:
            if rev["item_id"] != a["item_id"]:
                errors.append(f"{where}.item_id differs from its revision's item_id")
            if rev["coverage"] != a["coverage"]:
                errors.append(f"{where}.coverage differs from its revision's coverage")
        profile = profiles.get(a["interest_profile"]["digest"])
        if profile is None or profile["profile_id"] != a["interest_profile"]["ref"]:
            errors.append(f"{where}.interest_profile is not in interest_profiles")
        else:
            known = {i["interest_id"] for i in profile["interests"]}
            if not set(a["matched_interests"]) <= known:
                errors.append(f"{where}.matched_interests are not in its profile")
        for m, ev in enumerate(a.get("evidence", ())):
            ewhere = f"{where}.evidence[{m}]"
            if ev["item_id"] not in items:
                errors.append(f"{ewhere}.item_id is not in items")
            cited = check_revision_ref(f"{ewhere}.source", ev["source"])
            if cited is not None:
                if cited["item_id"] != ev["item_id"]:
                    errors.append(f"{ewhere}.item_id differs from its revision's item_id")
                check_excerpt(ewhere, cited, ev["excerpt"])
    for n, a in enumerate(packet["assessments"]):
        prior = a.get("prior_assessment")
        if not prior:
            continue
        if prior["ref"] not in assessments:
            errors.append(f"assessments[{n}].prior_assessment is not in assessments")
        elif assessments[prior["ref"]] != prior["digest"]:
            errors.append(f"assessments[{n}].prior_assessment digest mismatch")

    primary = packet["primary_assessment"]
    if assessments.get(primary["ref"]) != primary["digest"]:
        errors.append("primary_assessment does not match an included assessment")

    for kind in ("supporting", "contradictory"):
        for n, ev in enumerate(packet["evidence"][kind]):
            rev = check_revision_ref(f"evidence.{kind}[{n}].source", ev["source"])
            if rev is not None:
                check_excerpt(f"evidence.{kind}[{n}]", rev, ev.get("excerpt"))
    for n, rr in enumerate(packet["repository_revisions"]):
        if rr["item_id"] not in items:
            errors.append(f"repository_revisions[{n}].item_id is not in items")
    return errors


# ── Database to packets ──────────────────────────────────────────────
#
# Selection rules (``export_packets``):
#
# * A packet's subject is a *bookmarked* item (``bookmarks.source`` 'x' or
#   'bookmarks_html'), visible (``content_visible = 1``), whose latest
#   assessment on a non-withdrawn revision has one of the requested
#   dispositions (default: candidate). That latest assessment is the
#   packet's ``primary_assessment``.
# * Its linked items are the visible children in ``item_links``. Hidden
#   children and their links are left out entirely.
# * Each included item contributes its current revision, every
#   non-withdrawn assessment of it, and the revisions and profiles those
#   assessments reference (including the revisions their evidence cites).
#   Withdrawn revisions are never exported.
# * An assessment whose stored evidence cites an item the packet does not
#   include (hidden or no longer linked) or a revision that is withdrawn or
#   no longer carries the excerpt is left out of the packet. If that is the
#   primary assessment the subject is skipped until it is reassessed: an
#   excerpt of hidden content is never exported.
# * Hidden locators (contract §6.1, §9): the locators of hidden items
#   linked from any included item. A revision whose text or final locator
#   quotes one is left out (masking would break its digest); an item title
#   is masked; an assessment made on or citing such a revision, whose free
#   text quotes one, or whose model call was shown an item since hidden or
#   deleted (``context_json``) is left out, and if it is the primary the
#   subject is skipped until reassessed. A packet whose JSON still quotes
#   one is refused.
# * Evidence: each included item's latest included assessment contributes
#   its stored, model-cited evidence (``assessments.evidence_json``:
#   ``supports`` entries are supporting, ``contradicts`` entries are
#   contradictory, the note is the statement and the verified excerpt is
#   kept). An assessment without stored evidence contributes one statement
#   derived from its judgement instead: supporting for ``candidate`` and
#   ``reference_only``, contradictory for ``rejected``, nothing otherwise.
# * ``repository_revisions`` is empty: nothing records inspected commits yet.
#
# Revision rules: the packet's content (everything except packet_id,
# revision, previous_revision and created_at) is compared with the
# subject's latest exported revision. Unchanged content is a no-op;
# changed content is revision n + 1 with ``previous_revision``. A subject
# whose latest revision was withdrawn is re-exported only after a newer
# assessment than the withdrawal.
#
# Withdrawal rules (contract §7; ``withdrawals_due`` / the sweep that
# starts every non-dry-run ``export_packets``): every active packet
# revision is checked against the current ``bookmarks`` rows. If any of
# its items is deleted at its source (no row any more, or a current
# ``deleted`` revision while the packet carries readable content of it) it
# is withdrawn with ``source_deleted``; otherwise, if any of its items is
# hidden (``content_visible = 0``) it is withdrawn with ``policy_hidden``.
# A ``source_deleted`` withdrawal also withdraws every readable revision of
# the deleted items, and a ``retention`` withdrawal (operator-initiated)
# every content revision the packet carries, in both cases unless another
# active packet still references the revision. A source-deleted item's
# earlier revisions, and assessments made on, citing or shown them, are
# never exported again. ``policy_hidden`` and ``operator_request`` keep
# the content revisions: the material is still muninn's, just not exported.

_VOLATILE = ("packet_id", "revision", "previous_revision", "created_at")
_SUBJECT_SOURCES = ("x", "bookmarks_html")
_RELATION_ORDER = {"links_to": 0, "article": 1, "quotes": 2, "replies_to": 3}
# Withdrawal reasons that also withdraw content revisions (contract §7).
_CONTENT_WITHDRAWING = ("source_deleted", "retention")


class PacketNotExportable(ValueError):
    """The subject cannot be exported now (not a defect; reported as skipped)."""


@dataclass(frozen=True)
class DueWithdrawal:
    """An active packet revision the withdrawal sweep must withdraw."""

    ref: str
    reason: str
    deleted_items: tuple[str, ...] = ()
    hidden_items: tuple[str, ...] = ()


@dataclass
class ExportRun:
    """Outcome of one :func:`export_packets` call."""

    dry_run: bool = False
    written: list[ExportedPacket] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)  # packet refs already current
    rematerialized: int = 0  # missing files rewritten from evidence_packets
    planned: list[str] = field(default_factory=list)  # dry run: subject item ids
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (subject, reason)
    invalid: list[tuple[str, list[str]]] = field(default_factory=list)  # (subject, errors)
    withdrawn: list[tuple[ExportedPacket, str]] = field(default_factory=list)  # (tombstone, reason)
    would_withdraw: list[DueWithdrawal] = field(default_factory=list)  # dry run
    revisions_withdrawn: int = 0  # content revisions cleared by the sweep

    def withdrawn_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        reasons = [r for _, r in self.withdrawn] or [d.reason for d in self.would_withdraw]
        for reason in reasons:
            counts[reason] = counts.get(reason, 0) + 1
        return dict(sorted(counts.items()))

    def summary(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "written": [p.ref for p in self.written],
            "unchanged": self.unchanged,
            "rematerialized": self.rematerialized,
            "planned": self.planned,
            "skipped": [f"{s}: {r}" for s, r in self.skipped],
            "invalid": [f"{s}: {'; '.join(e[:3])}" for s, e in self.invalid],
            "withdrawn": (
                [f"{p.ref}: {r}" for p, r in self.withdrawn]
                or [f"{d.ref}: {d.reason}" for d in self.would_withdraw]
            ),
            "withdrawn_counts": self.withdrawn_counts(),
            "revisions_withdrawn": self.revisions_withdrawn,
        }


def _core_digest(packet: dict[str, Any]) -> str:
    return record_digest({k: v for k, v in packet.items() if k not in _VOLATILE})


def _latest_assessments(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """The latest assessment of each visible bookmarked item, newest first."""
    rows = conn.execute(
        f"""
        SELECT a.*, b.source AS subject_source
        FROM   assessments a
        JOIN   scrape_history h ON h.scrape_history_id = a.scrape_history_id
        JOIN   bookmarks b      ON b.bookmark_id = a.bookmark_id
        WHERE  h.withdrawn_at IS NULL
          AND  b.content_visible = 1
          AND  b.source IN ({','.join('?' * len(_SUBJECT_SOURCES))})
        ORDER  BY a.assessed_at DESC, a.assessment_id DESC
        """,
        _SUBJECT_SOURCES,
    ).fetchall()
    seen: set[str] = set()
    latest = []
    for row in rows:
        if row["item_ref"] not in seen:
            seen.add(row["item_ref"])
            latest.append(row)
    return latest


def _item_assessments(conn: sqlite3.Connection, item_ref: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT a.* FROM assessments a
        JOIN   scrape_history h ON h.scrape_history_id = a.scrape_history_id
        WHERE  a.item_ref = ? AND h.withdrawn_at IS NULL
        ORDER  BY a.assessed_at, a.assessment_id
        """,
        (item_ref,),
    ).fetchall()


def _cited_revisions(
    conn: sqlite3.Connection, record: dict[str, Any], included: set[str]
) -> list[sqlite3.Row] | None:
    """The revisions an assessment's evidence cites, or None when any citation
    cannot be exported (item not included, revision withdrawn or changed,
    excerpt no longer in its text)."""
    from muninn.intake_records import revision_text

    rows = []
    for ev in record.get("evidence", ()):
        if ev["item_id"] not in included:
            return None
        row = conn.execute(
            "SELECT * FROM scrape_history WHERE revision_ulid = ?", (ev["source"]["ref"],)
        ).fetchone()
        if row is None or row["item_ref"] != ev["item_id"] or row["content_sha256"] != ev["source"]["digest"]:
            return None
        text = revision_text(row)
        if text is None or ev["excerpt"] not in text:
            return None
        rows.append(row)
    return rows


def _evidence_statement(rec: dict[str, Any]) -> str:
    text = (
        f"{rec['disposition']} (relevance {rec['relevance']}, confidence {rec['confidence']}, "
        f"coverage {rec['coverage']}): {rec['rationale']}"
    )
    return text[:2000]


def _packet_evidence(
    item_ids: Sequence[str], latest: dict[str, dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    supporting: list[dict[str, Any]] = []
    contradictory: list[dict[str, Any]] = []
    for item_id in item_ids:
        rec = latest.get(item_id)
        if rec is None:
            continue
        if rec.get("evidence"):
            for ev in rec["evidence"]:
                entry = {"statement": ev["note"], "source": ev["source"], "excerpt": ev["excerpt"]}
                (supporting if ev["stance"] == "supports" else contradictory).append(entry)
            continue
        entry = {"statement": _evidence_statement(rec), "source": rec["content_revision"]}
        if rec["disposition"] in ("candidate", "reference_only"):
            supporting.append(entry)
        elif rec["disposition"] == "rejected":
            contradictory.append(entry)
    return {"supporting": supporting, "contradictory": contradictory}


def build_packet(
    conn: sqlite3.Connection, primary: sqlite3.Row, *, packet_id: str, revision: int,
    previous: dict[str, str] | None, created_at: str,
) -> dict[str, Any]:
    """Assemble the active packet for ``primary``'s subject (not validated).

    Raises :class:`PacketNotExportable` when the primary assessment cites
    content the packet cannot include.
    """
    from muninn import intake_records as R

    subject = conn.execute(
        "SELECT * FROM bookmarks WHERE bookmark_id = ?", (primary["bookmark_id"],)
    ).fetchone()
    subject_id = R.export_item_id(subject)

    item_rows: dict[str, sqlite3.Row] = {subject_id: subject}
    link_rows = conn.execute(
        """
        SELECT l.*, b.bookmark_id AS child_id FROM item_links l
        JOIN   bookmarks b ON b.bookmark_id = l.child_bookmark_id
        WHERE  l.parent_bookmark_id = ? AND b.content_visible = 1
        """,
        (subject["bookmark_id"],),
    ).fetchall()
    link_rows = sorted(link_rows, key=lambda r: (_RELATION_ORDER.get(r["relation"], 9), r["position"]))
    for link in link_rows:
        child = conn.execute("SELECT * FROM bookmarks WHERE bookmark_id = ?", (link["child_id"],)).fetchone()
        item_rows.setdefault(R.export_item_id(child), child)
    included = set(item_rows)

    # Locators of hidden items linked from any included item. A visible
    # item can still quote one (text, title or enrichment stored before a
    # policy change); such content is never exported (contract §9).
    hidden_by_parent = R.hidden_child_locators(conn, [r["bookmark_id"] for r in item_rows.values()])
    hidden: set[str] = set().union(*hidden_by_parent.values()) if hidden_by_parent else set()

    def exposes(value: Any) -> bool:
        if not hidden or value is None:
            return False
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return R.mentions_locator(text, hidden)

    def revision_exportable(row: sqlite3.Row) -> bool:
        return not (exposes(R.revision_text(row)) or exposes(row["final_locator"]))

    statuses = _ItemStatus(conn)
    cache: dict[int, dict[str, Any]] = {}
    revision_rows: dict[int, sqlite3.Row] = {}
    assessments: list[tuple[sqlite3.Row, dict[str, Any]]] = []
    latest_by_item: dict[str, dict[str, Any]] = {}
    for item_id, row in item_rows.items():
        current = R.current_revision_row(conn, item_id, row["bookmark_id"])
        if current is not None and revision_exportable(current) and not statuses.superseded(current):
            revision_rows.setdefault(current["scrape_history_id"], current)
        for a in _item_assessments(conn, item_id):
            rec = R.assessment_record(conn, a, cache)
            own = conn.execute(
                "SELECT * FROM scrape_history WHERE scrape_history_id = ?", (a["scrape_history_id"],)
            ).fetchone()
            cited = _cited_revisions(conn, rec, included)
            if cited is None:
                why = "primary assessment cites content the packet cannot include; reassess"
            elif any(statuses.superseded(r) for r in (own, *cited)):
                why = "primary assessment was made on content its source has since deleted; reassess"
            elif not all(revision_exportable(r) for r in (own, *cited)):
                why = "primary assessment's revision quotes a hidden item's locator; re-sync and reassess"
            elif exposes(rec["rationale"]) or exposes(rec["unresolved_questions"]) or exposes(rec.get("evidence")):
                why = "primary assessment's text quotes a hidden item's locator; reassess"
            elif not statuses.context_exportable(a):
                why = "primary assessment saw an item that is now hidden or deleted; reassess"
            else:
                why = None
            if why is not None:
                if a["assessment_id"] == primary["assessment_id"]:
                    raise PacketNotExportable(why)
                continue
            assessments.append((a, rec))
            latest_by_item[item_id] = rec
            for rev_row in cited:
                revision_rows.setdefault(rev_row["scrape_history_id"], rev_row)
            revision_rows.setdefault(own["scrape_history_id"], own)

    final = _resolve_priors(assessments)
    assessments = [(a, final[rec["assessment_id"]]) for a, rec in assessments]
    latest_by_item = {item: final[rec["assessment_id"]] for item, rec in latest_by_item.items()}

    order = {item_id: n for n, item_id in enumerate(item_rows)}
    revisions = sorted(
        revision_rows.values(),
        key=lambda r: (order.get(r["item_ref"], len(order)), r["retrieved_at"], r["scrape_history_id"]),
    )
    profiles: list[dict[str, Any]] = []
    seen_profiles: set[str] = set()
    for a, _rec in assessments:
        if a["profile_sha256"] not in seen_profiles:
            seen_profiles.add(a["profile_sha256"])
            profiles.append(R.profile_record(conn, a["profile_sha256"]))

    primary_rec = final[primary["assessment_ulid"]]
    latest_by_item[subject_id] = primary_rec

    questions: list[str] = []
    for rec in [primary_rec] + [latest_by_item[i] for i in item_rows if i in latest_by_item]:
        for q in rec["unresolved_questions"]:
            if q not in questions:
                questions.append(q)

    assessment_ids = [a["assessment_id"] for a, _ in assessments]
    usage_rows = conn.execute(
        f"""
        SELECT u.*, a.assessment_ulid, s.run_ulid
        FROM   usage_records u
        LEFT   JOIN assessments a      ON a.assessment_id = u.assessment_id
        LEFT   JOIN sync_checkpoints s ON s.sync_checkpoint_id = u.sync_checkpoint_id
        WHERE  u.assessment_id IN ({','.join('?' * len(assessment_ids))})
        ORDER  BY u.occurred_at, u.usage_record_id
        """,
        assessment_ids,
    ).fetchall() if assessment_ids else []

    observation = R.bookmark_observation_record(subject)
    packet: dict[str, Any] = {
        "record_type": "evidence-packet",
        "contract": CONTRACT_NAME,
        "contract_version": CONTRACT_VERSION,
        "status": "active",
        "packet_id": packet_id,
        "revision": revision,
        "created_at": created_at,
        "subject_item_id": subject_id,
        "items": [_masked_item(R.source_item_record(row), hidden) for row in item_rows.values()],
        "item_links": [
            R.item_link_record(link) for link in link_rows
            if link["parent_ref"] in included and link["child_ref"] in included
        ],
        "observations": [observation] if observation else [],
        "content_revisions": [R.content_revision_record(r, include_text=True) for r in revisions],
        "interest_profiles": profiles,
        "assessments": [rec for _, rec in assessments],
        "primary_assessment": {"ref": primary_rec["assessment_id"], "digest": record_digest(primary_rec)},
        "evidence": _packet_evidence(list(item_rows), latest_by_item),
        "repository_revisions": [],
        "unresolved_questions": questions,
    }
    if usage_rows:
        packet["usage"] = [R.usage_record(u) for u in usage_rows]
    if previous is not None:
        packet["previous_revision"] = previous
    if exposes(canonical_json(packet).decode("utf-8")):  # defence in depth
        raise PacketNotExportable("packet would quote a hidden item's locator; re-sync and reassess")
    return packet


def _resolve_priors(
    assessments: Sequence[tuple[sqlite3.Row, dict[str, Any]]]
) -> dict[str, dict[str, Any]]:
    """The included assessment records with ``prior_assessment`` resolved
    inside the packet (contract §6.1): kept, with its digest recomputed,
    when the prior assessment is included too, and omitted otherwise (the
    prior was left out for a reason: hidden, withdrawn or cited content).

    Records are processed in insertion order, so a prior (always an
    earlier row) is final before anything references it. Returns
    ``{assessment ULID: record}``; the shared record cache is not mutated.
    """
    final: dict[str, dict[str, Any]] = {}
    for _row, rec in sorted(assessments, key=lambda pair: pair[0]["assessment_id"]):
        out = dict(rec)
        prior = out.get("prior_assessment")
        if prior is not None:
            target = final.get(prior["ref"])
            if target is None:
                del out["prior_assessment"]
            else:
                out["prior_assessment"] = {"ref": prior["ref"], "digest": record_digest(target)}
        final[out["assessment_id"]] = out
    return final


def _masked_item(record: dict[str, Any], hidden: set[str]) -> dict[str, Any]:
    """A source-item whose title quoted a hidden item's locator, masked
    (titles carry no digest, so masking is safe)."""
    from muninn.intake_records import mask_title

    if hidden and record.get("title"):
        record["title"] = mask_title(record["title"], hidden)
    return record


class _ItemStatus:
    """Per-item state the exporter checks assessments against (cached).

    * **Visible**: the item has a ``bookmarks`` row, and none of its rows
      is hidden.
    * **Deleted at its source**: its current revision has coverage
      ``deleted`` (an X post the API reports as not found, a page that now
      answers 404/410). Its earlier revisions are superseded: they are not
      exported, nor is any assessment made on or citing them (contract §7).
    * **Context**: ``assessments.context_json`` lists every item a model
      call was shown. An assessment whose context included an item that
      has since been hidden or deleted (its row is gone, or its source
      deleted it) may paraphrase that item in its free text (rationale,
      unresolved questions), so it is never exported (contract §9). A
      withdrawn revision in the context does not exclude it: withdrawal of
      a revision the assessment cites (evidence) or was made on already does.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self._rows: dict[str, list[tuple[int, int]] | None] = {}
        self._deleted: dict[str, sqlite3.Row | None] = {}

    def _bookmarks(self, item_id: str) -> list[tuple[int, int]] | None:
        if item_id not in self._rows:
            self._rows[item_id] = _item_bookmarks(self.conn, {item_id}).get(item_id)
        return self._rows[item_id]

    def visible(self, item_id: str) -> bool:
        rows = self._bookmarks(item_id)
        return rows is not None and all(visible for _, visible in rows)

    def deleted_revision(self, item_id: str) -> sqlite3.Row | None:
        """The item's current ``deleted`` revision, if its source deleted it."""
        if item_id not in self._deleted:
            rows = self._bookmarks(item_id) or []
            self._deleted[item_id] = _source_deleted_revision(self.conn, item_id, [b for b, _ in rows])
        return self._deleted[item_id]

    def superseded(self, revision: sqlite3.Row) -> bool:
        """True for a revision of a source-deleted item other than its deletion."""
        deleted = self.deleted_revision(revision["item_ref"])
        return deleted is not None and revision["scrape_history_id"] != deleted["scrape_history_id"]

    def context_exportable(self, assessment: sqlite3.Row) -> bool:
        try:
            entries = json.loads(assessment["context_json"] or "[]")
        except (TypeError, ValueError):
            return False
        for entry in entries:
            if not (isinstance(entry, list) and len(entry) == 3 and isinstance(entry[0], str)):
                return False
            if not self.visible(entry[0]):
                return False
            if entry[2] != "deleted" and self.deleted_revision(entry[0]) is not None:
                return False
        return True


def packet_errors(packet: dict[str, Any]) -> list[str]:
    """Schema and cross-record problems; a packet is written only if empty."""
    from muninn.consumers.packets.schemas import schema_errors

    errors = schema_errors("evidence-packet", packet)
    if errors or packet.get("status") != "active":
        return errors
    return packet_integrity_errors(packet)


def _tombstone_from_row(row: sqlite3.Row) -> dict[str, Any]:
    from muninn.intake_records import iso

    return {
        "record_type": "evidence-packet",
        "contract": CONTRACT_NAME,
        "contract_version": row["contract_version"],
        "status": "withdrawn",
        "packet_id": row["packet_ulid"],
        "revision": row["revision"],
        "digest": row["packet_sha256"],
        "withdrawn_at": iso(row["withdrawn_at"]),
        "withdrawal_reason": row["withdrawal_reason"],
    }


def _materialize(out_dir: Path, row: sqlite3.Row) -> bool:
    """Rewrite a stored packet revision's file if it is missing. True if written."""
    if row["withdrawn_at"] is None:
        path = packet_path(out_dir, row["packet_ulid"], row["revision"])
        if path.exists():
            return False
        data = row["body_json"].encode("utf-8")
        if sha256_hex(data) != row["packet_sha256"]:
            raise ValueError(f"stored body of {row['packet_ulid']}@{row['revision']} does not match its digest")
    else:
        path = packet_path(out_dir, row["packet_ulid"], row["revision"], withdrawn=True)
        if path.exists():
            return False
        data = canonical_json(_tombstone_from_row(row))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return True


# ── Withdrawal ───────────────────────────────────────────────────────

def _item_bookmarks(conn: sqlite3.Connection, item_ids: set[str]) -> dict[str, list[tuple[int, int]]]:
    """``(bookmark_id, content_visible)`` of every ``bookmarks`` row that has
    one of these item IDs. An ID with no row is missing from the result."""
    from muninn.intake_records import item_ref_for_row

    found: dict[str, list[tuple[int, int]]] = {}
    by_source = {"x:post:": "x", "x:article:": "x_article"}
    rest = set()
    for item_id in item_ids:
        prefix = next((p for p in by_source if item_id.startswith(p)), None)
        if prefix is None:
            rest.add(item_id)
            continue
        for bookmark_id, visible in conn.execute(
            "SELECT bookmark_id, content_visible FROM bookmarks WHERE source = ? AND source_id = ?",
            (by_source[prefix], item_id.removeprefix(prefix)),
        ):
            found.setdefault(item_id, []).append((int(bookmark_id), int(visible)))
    if rest:
        for row in conn.execute(
            "SELECT bookmark_id, source, source_id, url, source_metadata, content_visible "
            "FROM bookmarks WHERE source NOT IN ('x', 'x_article')"
        ):
            try:
                item_id = item_ref_for_row(row)
            except ValueError:
                continue
            if item_id in rest:
                found.setdefault(item_id, []).append((int(row["bookmark_id"]), int(row["content_visible"])))
    return found


def _item_states(conn: sqlite3.Connection, item_ids: set[str]) -> dict[str, list[int]]:
    """``content_visible`` of every ``bookmarks`` row that has one of these
    item IDs. An ID with no row is missing from the result (deleted)."""
    return {i: [v for _, v in rows] for i, rows in _item_bookmarks(conn, item_ids).items()}


def _source_deleted_revision(
    conn: sqlite3.Connection, item_id: str, bookmark_ids: Sequence[int]
) -> sqlite3.Row | None:
    """The item's current revision when its coverage is ``deleted`` (the
    source deleted it: X ``resource-not-found``, HTTP 404/410), else None."""
    from muninn.intake_records import current_revision_row

    for bookmark_id in bookmark_ids:
        current = current_revision_row(conn, item_id, bookmark_id)
        if current is not None and current["coverage"] == "deleted":
            return current
    return None


def _deleted_items(
    conn: sqlite3.Connection,
    body: dict[str, Any],
    rows: dict[str, list[tuple[int, int]]],
    cache: dict[str, bool] | None = None,
) -> tuple[str, ...]:
    """The packet's items deleted at their source: no ``bookmarks`` row any
    more, or a current ``deleted`` revision while the packet still carries
    readable content of the item (a packet carrying only the deletion
    itself is not withdrawn again)."""
    cache = cache if cache is not None else {}
    readable = {r["item_id"] for r in body["content_revisions"] if r["coverage"] in ("full", "partial")}
    out = []
    for item in (i["item_id"] for i in body["items"]):
        if item not in rows:
            out.append(item)
        elif item in readable:
            if item not in cache:
                cache[item] = _source_deleted_revision(conn, item, [b for b, _ in rows[item]]) is not None
            if cache[item]:
                out.append(item)
    return tuple(out)


def withdrawals_due(conn: sqlite3.Connection) -> list[DueWithdrawal]:
    """Active packet revisions whose material is gone or hidden (contract §7).

    ``source_deleted`` when any of the packet's items no longer has a
    ``bookmarks`` row, or its current revision is ``deleted`` while the
    packet carries readable content of it; else ``policy_hidden`` when any
    of them is hidden (``content_visible = 0``) in any row.
    """
    active = conn.execute(
        "SELECT packet_ulid, revision, body_json FROM evidence_packets "
        "WHERE withdrawn_at IS NULL ORDER BY evidence_packet_id"
    ).fetchall()
    if not active:
        return []
    bodies = [(f"{r['packet_ulid']}@{r['revision']}", json.loads(r["body_json"])) for r in active]
    wanted = {i["item_id"] for _, body in bodies for i in body["items"]}
    rows = _item_bookmarks(conn, wanted)
    states = {i: [v for _, v in r] for i, r in rows.items()}
    deleted_cache: dict[str, bool] = {}
    due = []
    for ref, body in bodies:
        items = [i["item_id"] for i in body["items"]]
        deleted = _deleted_items(conn, body, rows, deleted_cache)
        hidden = tuple(i for i in items if i in states and 0 in states[i])
        if deleted:
            due.append(DueWithdrawal(ref, "source_deleted", deleted, hidden))
        elif hidden:
            due.append(DueWithdrawal(ref, "policy_hidden", (), hidden))
    return due


def _revisions_held_by_other_packets(conn: sqlite3.Connection, exclude_id: int) -> set[str]:
    held: set[str] = set()
    for (body,) in conn.execute(
        "SELECT body_json FROM evidence_packets WHERE withdrawn_at IS NULL AND evidence_packet_id <> ?",
        (exclude_id,),
    ):
        held.update(r["revision_id"] for r in json.loads(body)["content_revisions"])
    return held


def _withdraw_row(
    conn: sqlite3.Connection, out: Path, row: sqlite3.Row, *, reason: str, ts: int,
    deleted_items: Sequence[str] | None = None,
) -> ExportedPacket:
    """Withdraw one active ``evidence_packets`` row: tombstone file, row
    update and (for ``source_deleted`` / ``retention``) its content revisions."""
    from muninn.intake_records import iso

    packet_id, revision = row["packet_ulid"], row["revision"]
    ref = f"{packet_id}@{revision}"
    body = json.loads(row["body_json"])
    tombstone = make_tombstone(body, reason=reason, withdrawn_at=iso(ts))
    if tombstone["digest"] != row["packet_sha256"]:
        raise ValueError(f"stored body of {ref} does not match its digest")
    errors = packet_errors(tombstone)
    if errors:  # pragma: no cover - make_tombstone builds the schema's shape
        raise ValueError(f"tombstone for {ref} is invalid: {errors}")

    targets: list[str] = []
    if reason in _CONTENT_WITHDRAWING:
        if reason == "source_deleted":
            if deleted_items is None:
                rows = _item_bookmarks(conn, {i["item_id"] for i in body["items"]})
                deleted_items = _deleted_items(conn, body, rows)
            scope = set(deleted_items)
            targets = [r["revision_id"] for r in body["content_revisions"] if r["item_id"] in scope]
            # The source deleted the material: every readable revision of
            # those items goes, not only the ones this packet carries.
            for item in sorted(scope):
                for (revision_id,) in conn.execute(
                    "SELECT revision_ulid FROM scrape_history WHERE item_ref = ? "
                    "AND withdrawn_at IS NULL AND coverage IN ('full', 'partial') "
                    "ORDER BY scrape_history_id",
                    (item,),
                ):
                    if revision_id not in targets:
                        targets.append(revision_id)
        else:
            targets = [r["revision_id"] for r in body["content_revisions"]]
        if targets:
            held = _revisions_held_by_other_packets(conn, row["evidence_packet_id"])
            targets = [t for t in targets if t not in held]

    tomb_path = packet_path(out, packet_id, revision, withdrawn=True)
    cleared = 0
    with _transaction(conn):
        conn.execute(
            """
            UPDATE evidence_packets
            SET    body_json = NULL, withdrawn_at = ?, withdrawal_reason = ?
            WHERE  evidence_packet_id = ?
            """,
            (ts, reason, row["evidence_packet_id"]),
        )
        for revision_id in targets:
            cleared += conn.execute(
                """
                UPDATE scrape_history
                SET    content_text = NULL, withdrawn_at = ?, withdrawal_reason = ?
                WHERE  revision_ulid = ? AND withdrawn_at IS NULL
                """,
                (ts, reason, revision_id),
            ).rowcount
        tomb_path.parent.mkdir(parents=True, exist_ok=True)
        tomb_path.write_bytes(canonical_json(tombstone))
    packet_path(out, packet_id, revision).unlink(missing_ok=True)
    return ExportedPacket(packet_id, revision, row["packet_sha256"], tomb_path, "withdrawn", cleared)


def _sweep_withdrawals(conn: sqlite3.Connection, out: Path, run: ExportRun, ts: int) -> None:
    due = withdrawals_due(conn)
    if run.dry_run:
        run.would_withdraw.extend(due)
        return
    for item in due:
        packet_id, revision = parse_packet_ref(item.ref)
        row = conn.execute(
            "SELECT * FROM evidence_packets WHERE packet_ulid = ? AND revision = ?", (packet_id, revision)
        ).fetchone()
        tomb = _withdraw_row(conn, out, row, reason=item.reason, ts=ts, deleted_items=item.deleted_items)
        run.withdrawn.append((tomb, item.reason))
        run.revisions_withdrawn += tomb.revisions_withdrawn


def export_packets(
    conn: sqlite3.Connection,
    out_dir: Path | str,
    *,
    item_ids: Sequence[str] | None = None,
    since: int | None = None,
    dispositions: Sequence[str] = ("candidate",),
    limit: int | None = None,
    dry_run: bool = False,
    now: int | None = None,
) -> ExportRun:
    """Withdraw packets whose material is gone or hidden, then build,
    validate and write evidence packets for selected assessments.

    The withdrawal sweep (see the rules above) always covers every active
    packet, whatever the selection; a dry run only reports it
    (``would_withdraw``). Selection is bounded (``item_ids``, ``since`` as
    epoch seconds on the primary assessment's ``assessed_at``,
    ``dispositions``, ``limit``). Items with ``bookmarks.content_visible = 0``
    are never exported. Every packet is validated against
    ``schemas/json/intake/evidence-packet.schema.json`` and
    :func:`packet_integrity_errors` before it is written; an invalid packet
    is reported and not written. Written packets are recorded in
    ``evidence_packets``. Imports nothing heavy (ADR-009).
    """
    import time

    from muninn.intake_records import iso

    out = Path(out_dir)
    ts = int(time.time()) if now is None else now
    run = ExportRun(dry_run=dry_run)
    wanted = set(item_ids or ())

    _sweep_withdrawals(conn, out, run, ts)
    # A dry run withdraws nothing, but plans as if the due withdrawals had
    # been applied at ``ts``, so it reports what the real run would do.
    pending = {d.ref: ts for d in run.would_withdraw}

    selected = []
    for primary in _latest_assessments(conn):
        if wanted and primary["item_ref"] not in wanted:
            continue
        if primary["disposition"] not in dispositions:
            continue
        if since is not None and primary["assessed_at"] < since:
            continue
        selected.append(primary)
        if limit is not None and len(selected) >= limit:
            break

    for primary in selected:
        subject_ref = primary["item_ref"]
        history = conn.execute(
            "SELECT * FROM evidence_packets WHERE subject_ref = ? ORDER BY revision DESC",
            (subject_ref,),
        ).fetchall()
        latest = history[0] if history else None
        latest_withdrawn_at = (
            None if latest is None
            else latest["withdrawn_at"] if latest["withdrawn_at"] is not None
            else pending.get(f"{latest['packet_ulid']}@{latest['revision']}")
        )
        if latest_withdrawn_at is not None and primary["assessed_at"] <= latest_withdrawn_at:
            run.skipped.append((subject_ref, f"latest revision {latest['packet_ulid']}@{latest['revision']} is withdrawn"))
            continue

        packet_id = latest["packet_ulid"] if latest is not None else new_ulid(ts * 1000)
        revision = latest["revision"] + 1 if latest is not None else 1
        previous = (
            {"ref": f"{latest['packet_ulid']}@{latest['revision']}", "digest": latest["packet_sha256"]}
            if latest is not None else None
        )
        try:
            packet = build_packet(
                conn, primary, packet_id=packet_id, revision=revision, previous=previous, created_at=iso(ts)
            )
        except PacketNotExportable as exc:
            run.skipped.append((subject_ref, str(exc)))
            continue
        except (ValueError, LookupError, CanonicalizationError) as exc:
            run.invalid.append((subject_ref, [f"cannot build packet: {exc}"]))
            continue

        if latest is not None and latest_withdrawn_at is None:
            if _core_digest(json.loads(latest["body_json"])) == _core_digest(packet):
                run.unchanged.append(f"{latest['packet_ulid']}@{latest['revision']}")
                if not dry_run:
                    run.rematerialized += sum(_materialize(out, row) for row in history)
                continue

        errors = packet_errors(packet)
        if errors:
            run.invalid.append((subject_ref, errors))
            continue
        if dry_run:
            run.planned.append(subject_ref)
            continue

        data = canonical_json(packet)
        digest = sha256_hex(data)
        path = packet_path(out, packet_id, revision)
        with _transaction(conn):
            conn.execute(
                """
                INSERT INTO evidence_packets (
                    packet_ulid, revision, packet_sha256, subject_ref, assessment_id,
                    contract_version, body_json, export_path, exported_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (packet_id, revision, digest, subject_ref, primary["assessment_id"],
                 CONTRACT_VERSION, data.decode("utf-8"), str(path), ts),
            )
            run.rematerialized += sum(_materialize(out, row) for row in history)
            exported = write_packet(out, packet)
        assert exported.digest == digest
        run.written.append(exported)
    return run


def withdraw_exported_packet(
    conn: sqlite3.Connection,
    out_dir: Path | str,
    ref: str,
    *,
    reason: str,
    now: int | None = None,
) -> ExportedPacket:
    """Withdraw one exported packet revision (contract §7).

    The stored body is cleared, ``withdrawn_at``/``withdrawal_reason`` are
    set, the active file is replaced by its tombstone, and the reference
    then resolves to ``withdrawn``. Other packet revisions are untouched.
    ``source_deleted`` also withdraws the packet's content revisions of the
    items whose ``bookmarks`` row is gone, and ``retention`` all of its
    content revisions, unless another active packet references them
    (``revisions_withdrawn`` counts them). Withdrawing an already-withdrawn
    revision only re-materializes its tombstone.
    """
    import time

    if reason not in WITHDRAWAL_REASONS:
        raise ValueError(f"unknown withdrawal reason: {reason!r}")
    packet_id, revision = parse_packet_ref(ref)
    out = Path(out_dir)
    row = conn.execute(
        "SELECT * FROM evidence_packets WHERE packet_ulid = ? AND revision = ?", (packet_id, revision)
    ).fetchone()
    if row is None:
        raise LookupError(f"no exported packet {ref}")
    if row["withdrawn_at"] is not None:
        _materialize(out, row)
        tomb_path = packet_path(out, packet_id, revision, withdrawn=True)
        return ExportedPacket(packet_id, revision, row["packet_sha256"], tomb_path, "withdrawn")
    ts = int(time.time()) if now is None else now
    return _withdraw_row(conn, out, row, reason=reason, ts=ts)


@contextmanager
def _transaction(conn: sqlite3.Connection) -> Iterator[None]:
    try:
        yield
        conn.commit()
    except Exception:
        conn.rollback()
        raise
