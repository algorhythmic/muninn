"""Evidence-packet export interface (contract ``muninn-research-intake`` 0.1.0-draft).

Status: **interface stub.** :func:`export_packets`, which reads the
database and builds packets, lands with the exporter (roadmap: suitability
enrichment and packet export). The file-level pieces are implemented here
because the contract's digest and resolution rules depend on them:

* :func:`write_packet` writes a packet as canonical JSON bytes, so that
  ``sha256sum`` of the file equals the packet digest.
* :func:`make_tombstone` and :func:`withdraw_packet` replace a packet's
  content with its tombstone.
* :func:`resolve_packet` resolves a ``{ref, digest}`` reference to
  ``active``, ``withdrawn``, ``missing`` or ``digest_mismatch``.
* :func:`packet_integrity_errors` checks the cross-record rules the JSON
  Schema cannot express.

Export layout under the output directory::

    <packet ULID>/<revision>.json            active packet (canonical JSON)
    <packet ULID>/<revision>.withdrawn.json  tombstone that replaced it

Planned CLI (registered later under the existing ``export`` group)::

    muninn export packets [--out DIR] [--item ITEM_ID]... [--since DATE]
                          [--limit N] [--dry-run]
    muninn export packets --withdraw PACKET_ID@REVISION
                          --reason {retention,operator_request,source_deleted,policy_hidden}
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Sequence

from muninn.consumers.packets.canonical import (
    canonical_json,
    content_digest,
    is_ulid,
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


def export_packets(
    conn: sqlite3.Connection,
    out_dir: Path | str,
    *,
    item_ids: Sequence[str] | None = None,
    since: int | None = None,
    limit: int | None = None,
    dry_run: bool = False,
) -> list[ExportedPacket]:
    """Build, validate and write evidence packets for selected assessments.

    Selection is bounded (``item_ids``, ``since`` as epoch seconds,
    ``limit``). Items with ``bookmarks.content_visible = 0`` are never
    exported. Every packet is validated against
    ``schemas/json/intake/evidence-packet.schema.json`` before it is
    written, recorded in ``evidence_packets`` and returned. Must not import
    Qdrant, embeddings, DuckDB, Parquet, MCP or the vault compiler (ADR-009).
    """
    raise NotImplementedError(
        "export_packets lands with the packet exporter; this is the RI-03 interface stub"
    )
