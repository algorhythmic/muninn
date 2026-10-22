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

* :func:`export_packets` builds packets from assessments, validates them
  (schema + integrity) and writes and records the valid ones; see the
  selection and revision rules above it.
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
    and is already normalized. Returns a list of problems (empty if sound).
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
    for n, a in enumerate(packet["assessments"]):
        prior = a.get("prior_assessment")
        if prior and prior["ref"] in assessments and assessments[prior["ref"]] != prior["digest"]:
            errors.append(f"assessments[{n}].prior_assessment digest mismatch")

    primary = packet["primary_assessment"]
    if assessments.get(primary["ref"]) != primary["digest"]:
        errors.append("primary_assessment does not match an included assessment")

    for kind in ("supporting", "contradictory"):
        for n, ev in enumerate(packet["evidence"][kind]):
            rev = check_revision_ref(f"evidence.{kind}[{n}].source", ev["source"])
            excerpt = ev.get("excerpt")
            if rev is not None and excerpt and "text" in rev and excerpt not in rev["text"]:
                errors.append(f"evidence.{kind}[{n}].excerpt is not in the revision text")
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
#   assessments reference. Withdrawn revisions are never exported.
# * Evidence is derived from the stored assessments (the 002 storage keeps
#   no separate evidence rows): each included item's latest assessment
#   that is ``candidate`` or ``reference_only`` is a supporting statement
#   about its revision, and one that is ``rejected`` is contradictory.
# * ``repository_revisions`` is empty: nothing records inspected commits yet.
#
# Revision rules: the packet's content (everything except packet_id,
# revision, previous_revision and created_at) is compared with the
# subject's latest exported revision. Unchanged content is a no-op;
# changed content is revision n + 1 with ``previous_revision``. A subject
# whose latest revision was withdrawn is re-exported only after a newer
# assessment than the withdrawal.

_VOLATILE = ("packet_id", "revision", "previous_revision", "created_at")
_SUBJECT_SOURCES = ("x", "bookmarks_html")
_RELATION_ORDER = {"links_to": 0, "article": 1, "quotes": 2, "replies_to": 3}


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

    def summary(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "written": [p.ref for p in self.written],
            "unchanged": self.unchanged,
            "rematerialized": self.rematerialized,
            "planned": self.planned,
            "skipped": [f"{s}: {r}" for s, r in self.skipped],
            "invalid": [f"{s}: {'; '.join(e[:3])}" for s, e in self.invalid],
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


def _evidence_statement(rec: dict[str, Any]) -> str:
    text = (
        f"{rec['disposition']} (relevance {rec['relevance']}, confidence {rec['confidence']}, "
        f"coverage {rec['coverage']}): {rec['rationale']}"
    )
    return text[:2000]


def build_packet(
    conn: sqlite3.Connection, primary: sqlite3.Row, *, packet_id: str, revision: int,
    previous: dict[str, str] | None, created_at: str,
) -> dict[str, Any]:
    """Assemble the active packet for ``primary``'s subject (not validated)."""
    from muninn.consumers.packets import records as R

    subject = conn.execute(
        "SELECT * FROM bookmarks WHERE bookmark_id = ?", (primary["bookmark_id"],)
    ).fetchone()
    subject_id = R.item_id_for(subject)

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
        item_rows.setdefault(R.item_id_for(child), child)

    cache: dict[int, dict[str, Any]] = {}
    revision_rows: dict[int, sqlite3.Row] = {}
    assessments: list[tuple[sqlite3.Row, dict[str, Any]]] = []
    latest_by_item: dict[str, dict[str, Any]] = {}
    for item_id, row in item_rows.items():
        current = R.current_revision_row(conn, item_id, row["bookmark_id"])
        if current is not None:
            revision_rows.setdefault(current["scrape_history_id"], current)
        for a in _item_assessments(conn, item_id):
            rec = R.assessment_record(conn, a, cache)
            assessments.append((a, rec))
            latest_by_item[item_id] = rec
            if a["scrape_history_id"] not in revision_rows:
                revision_rows[a["scrape_history_id"]] = conn.execute(
                    "SELECT * FROM scrape_history WHERE scrape_history_id = ?", (a["scrape_history_id"],)
                ).fetchone()

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

    primary_rec = R.assessment_record(conn, primary, cache)
    supporting, contradictory = [], []
    for item_id in item_rows:
        rec = latest_by_item.get(item_id)
        if rec is None:
            continue
        entry = {"statement": _evidence_statement(rec), "source": rec["content_revision"]}
        if rec["disposition"] in ("candidate", "reference_only"):
            supporting.append(entry)
        elif rec["disposition"] == "rejected":
            contradictory.append(entry)

    questions: list[str] = []
    for rec in [primary_rec] + [latest_by_item[i] for i in item_rows if i in latest_by_item]:
        for q in rec["unresolved_questions"]:
            if q not in questions:
                questions.append(q)

    assessment_ids = [a["assessment_id"] for a, _ in assessments]
    usage_rows = conn.execute(
        f"""
        SELECT * FROM usage_records WHERE assessment_id IN ({','.join('?' * len(assessment_ids))})
        ORDER  BY occurred_at, usage_record_id
        """,
        assessment_ids,
    ).fetchall() if assessment_ids else []

    included = set(item_rows)
    observation = R.observation_record(subject)
    packet: dict[str, Any] = {
        "record_type": "evidence-packet",
        "contract": CONTRACT_NAME,
        "contract_version": CONTRACT_VERSION,
        "status": "active",
        "packet_id": packet_id,
        "revision": revision,
        "created_at": created_at,
        "subject_item_id": subject_id,
        "items": [R.source_item_record(row) for row in item_rows.values()],
        "item_links": [
            R.item_link_record(link) for link in link_rows
            if link["parent_ref"] in included and link["child_ref"] in included
        ],
        "observations": [observation] if observation else [],
        "content_revisions": [R.revision_record(r) for r in revisions],
        "interest_profiles": profiles,
        "assessments": [rec for _, rec in assessments],
        "primary_assessment": {"ref": primary_rec["assessment_id"], "digest": record_digest(primary_rec)},
        "evidence": {"supporting": supporting, "contradictory": contradictory},
        "repository_revisions": [],
        "unresolved_questions": questions,
    }
    if usage_rows:
        packet["usage"] = [R.usage_record(conn, u) for u in usage_rows]
    if previous is not None:
        packet["previous_revision"] = previous
    return packet


def packet_errors(packet: dict[str, Any]) -> list[str]:
    """Schema and cross-record problems; a packet is written only if empty."""
    from muninn.consumers.packets.schemas import schema_errors

    errors = schema_errors("evidence-packet", packet)
    if errors or packet.get("status") != "active":
        return errors
    return packet_integrity_errors(packet)


def _tombstone_from_row(row: sqlite3.Row) -> dict[str, Any]:
    from muninn.consumers.packets.records import iso

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
    """Build, validate and write evidence packets for selected assessments.

    Selection is bounded (``item_ids``, ``since`` as epoch seconds on the
    primary assessment's ``assessed_at``, ``dispositions``, ``limit``).
    Items with ``bookmarks.content_visible = 0`` are never exported. Every
    packet is validated against ``schemas/json/intake/evidence-packet.schema.json``
    and :func:`packet_integrity_errors` before it is written; an invalid
    packet is reported and not written. Written packets are recorded in
    ``evidence_packets``. Imports nothing heavy (ADR-009).
    """
    import time

    from muninn.consumers.packets.records import iso

    out = Path(out_dir)
    ts = int(time.time()) if now is None else now
    run = ExportRun(dry_run=dry_run)
    wanted = set(item_ids or ())

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
        if latest is not None and latest["withdrawn_at"] is not None and primary["assessed_at"] <= latest["withdrawn_at"]:
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
        except (ValueError, LookupError, CanonicalizationError) as exc:
            run.invalid.append((subject_ref, [f"cannot build packet: {exc}"]))
            continue

        if latest is not None and latest["withdrawn_at"] is None:
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
    then resolves to ``withdrawn``. Other revisions are untouched.
    Withdrawing an already-withdrawn revision only re-materializes its
    tombstone. Content revisions are not withdrawn here.
    """
    import time

    from muninn.consumers.packets.records import iso

    if reason not in WITHDRAWAL_REASONS:
        raise ValueError(f"unknown withdrawal reason: {reason!r}")
    packet_id, revision = parse_packet_ref(ref)
    out = Path(out_dir)
    row = conn.execute(
        "SELECT * FROM evidence_packets WHERE packet_ulid = ? AND revision = ?", (packet_id, revision)
    ).fetchone()
    if row is None:
        raise LookupError(f"no exported packet {ref}")
    tomb_path = packet_path(out, packet_id, revision, withdrawn=True)
    if row["withdrawn_at"] is not None:
        _materialize(out, row)
        return ExportedPacket(packet_id, revision, row["packet_sha256"], tomb_path, "withdrawn")

    ts = int(time.time()) if now is None else now
    tombstone = make_tombstone(json.loads(row["body_json"]), reason=reason, withdrawn_at=iso(ts))
    if tombstone["digest"] != row["packet_sha256"]:
        raise ValueError(f"stored body of {ref} does not match its digest")
    errors = packet_errors(tombstone)
    if errors:  # pragma: no cover - make_tombstone builds the schema's shape
        raise ValueError(f"tombstone for {ref} is invalid: {errors}")
    with _transaction(conn):
        conn.execute(
            """
            UPDATE evidence_packets
            SET    body_json = NULL, withdrawn_at = ?, withdrawal_reason = ?
            WHERE  evidence_packet_id = ?
            """,
            (ts, reason, row["evidence_packet_id"]),
        )
        tomb_path.parent.mkdir(parents=True, exist_ok=True)
        tomb_path.write_bytes(canonical_json(tombstone))
    packet_path(out, packet_id, revision).unlink(missing_ok=True)
    return ExportedPacket(packet_id, revision, row["packet_sha256"], tomb_path, "withdrawn")


@contextmanager
def _transaction(conn: sqlite3.Connection) -> Iterator[None]:
    try:
        yield
        conn.commit()
    except Exception:
        conn.rollback()
        raise
