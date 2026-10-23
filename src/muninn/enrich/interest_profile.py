"""Load the operator's interest profile (contract §4.5).

The profile is a versioned YAML file, handled like ``domain_policy.yml``: a
missing or malformed file aborts instead of silently defaulting. It is
personal, so its path comes from ``--profile`` or ``MUNINN_INTEREST_PROFILE``;
the repository ships only the synthetic ``interest_profile.example.yml``.

The YAML holds the ``interest-profile`` record without ``record_type``. The
profile's **revision** is the sha256 of the parsed record's canonical JSON,
not of the file bytes, so comments and formatting do not trigger
reassessment. Every revision an assessment uses is recorded in
``interest_profiles`` so old assessments and packets stay resolvable.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from muninn.config import INTEREST_PROFILE_ENV
from muninn.consumers.packets.canonical import CanonicalizationError, canonical_json, record_digest
from muninn.consumers.packets.schemas import schema_errors

EXAMPLE_PROFILE = Path(__file__).resolve().parents[3] / "interest_profile.example.yml"


class InterestProfileError(RuntimeError):
    """The interest profile is missing, unreadable or not a valid profile."""


@dataclass(frozen=True)
class InterestProfile:
    record: dict[str, Any]  # the contract's interest-profile record
    digest: str  # the revision: sha256 of the record's canonical JSON
    path: Path

    @property
    def profile_id(self) -> str:
        return self.record["profile_id"]

    @property
    def interest_ids(self) -> frozenset[str]:
        return frozenset(i["interest_id"] for i in self.record["interests"])


def resolve_profile_path(path: Path | str | None = None) -> Path:
    raw = str(path) if path else os.environ.get(INTEREST_PROFILE_ENV, "")
    if not raw:
        raise InterestProfileError(
            f"No interest profile: pass --profile or set {INTEREST_PROFILE_ENV}. "
            f"The format is shown in {EXAMPLE_PROFILE.name} (synthetic; copy it "
            f"outside the repository, since the real profile is personal)."
        )
    return Path(raw).expanduser()


def load_interest_profile(path: Path | str | None = None) -> InterestProfile:
    p = resolve_profile_path(path)
    if not p.is_file():
        raise InterestProfileError(f"Interest profile not found at {p}")
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise InterestProfileError(f"{p} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise InterestProfileError(f"{p} must be a mapping (profile_id, label, interests, ...)")
    if "record_type" in data and data["record_type"] != "interest-profile":
        raise InterestProfileError(f"{p}: record_type must be 'interest-profile' if given")
    record = {"record_type": "interest-profile", **{k: v for k, v in data.items() if k != "record_type"}}
    errors = schema_errors("interest-profile", record)
    if errors:
        raise InterestProfileError(f"{p} is not a valid interest profile: " + "; ".join(errors[:5]))
    ids = [i["interest_id"] for i in record["interests"]]
    if len(set(ids)) != len(ids):
        raise InterestProfileError(f"{p}: interest_id values must be unique")
    try:
        digest = record_digest(record)
    except CanonicalizationError as exc:  # pragma: no cover - the schema rejects floats first
        raise InterestProfileError(f"{p}: {exc}") from exc
    return InterestProfile(record=record, digest=digest, path=p)


def record_profile(conn: sqlite3.Connection, profile: InterestProfile, *, now: int) -> bool:
    """Store this profile revision if it is new. Returns True when inserted.

    Append-only: an existing revision (same digest) is never rewritten.
    The caller owns the transaction.
    """
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO interest_profiles (profile_sha256, profile_id, body_json, first_seen_at)
        VALUES (?, ?, ?, ?)
        """,
        (profile.digest, profile.profile_id, canonical_json(profile.record).decode("utf-8"), now),
    )
    return cur.rowcount == 1
