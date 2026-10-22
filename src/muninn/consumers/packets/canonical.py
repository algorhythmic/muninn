"""Canonical JSON, digests, ULIDs and export identities for Research Intake.

This module is the reference implementation of the digest and identity
rules in ``docs/contracts/MUNINN-RESEARCH-INTAKE.md`` (contract
``muninn-research-intake`` 0.1.0-draft). It is stdlib-only, so capture,
assessment and export can all use it without heavy dependencies.

Canonical JSON is ``json.dumps(sort_keys=True, separators=(",", ":"),
ensure_ascii=False)`` encoded as UTF-8. On the value subset the contract
allows (strings, integers within ±(2**53 - 1), booleans, null, arrays and
objects whose keys contain no character above U+FFFF) that output is
byte-identical to RFC 8785 (JCS):

* JCS orders keys by UTF-16 code units, Python by code points. The two
  orders agree unless a key has a supplementary-plane character, which
  :func:`canonical_json` rejects.
* JCS serializes numbers the ECMAScript way. For safe integers that is the
  plain decimal form Python also emits. Floats are rejected, so the
  schemas use ``"type": "integer"`` and never ``"number"``.
* Both escape only ``"``, ``\\`` and control characters, with the same
  short escapes and lowercase ``\\u00xx`` for the rest.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import unicodedata
from typing import Any

MAX_SAFE_INTEGER = 2**53 - 1

# Name of the text normalization rule; recorded on every content revision.
TEXT_NORMALIZATION = "muninn-text/1"

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
ULID_RE = re.compile(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

_GITHUB_REPO_RE = re.compile(
    r"^https://github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/([A-Za-z0-9._-]{1,100}?)(?:\.git)?/?$"
)
# Path segments under github.com that are site sections, not owners.
_GITHUB_RESERVED_OWNERS = frozenset({
    "about", "apps", "collections", "contact", "customer-stories", "enterprise",
    "events", "explore", "features", "issues", "login", "marketplace",
    "notifications", "orgs", "pricing", "pulls", "search", "settings",
    "sponsors", "team", "topics", "trending", "users",
})


class CanonicalizationError(ValueError):
    """The value is outside the canonical-JSON subset the contract allows."""


# ── Canonical JSON and digests ───────────────────────────────────────

def _check(value: Any, path: str) -> None:
    if value is None or isinstance(value, (bool, str)):
        return
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_INTEGER:
            raise CanonicalizationError(f"{path}: integer outside the safe range")
        return
    if isinstance(value, float):
        raise CanonicalizationError(f"{path}: floats are not allowed in contract records")
    if isinstance(value, (list, tuple)):
        for i, item in enumerate(value):
            _check(item, f"{path}[{i}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalizationError(f"{path}: object key {key!r} is not a string")
            if any(ord(ch) > 0xFFFF for ch in key):
                raise CanonicalizationError(f"{path}: key {key!r} has a supplementary-plane character")
            _check(item, f"{path}.{key}")
        return
    raise CanonicalizationError(f"{path}: unsupported type {type(value).__name__}")


def canonical_json(value: Any) -> bytes:
    """Canonical UTF-8 JSON bytes for ``value`` (JCS-equivalent; see module doc)."""
    _check(value, "$")
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def record_digest(record: dict[str, Any]) -> str:
    """Digest of a contract record: sha256 over its canonical JSON.

    Used for evidence packets (where it is also the sha256 of the exported
    file, because the exporter writes canonical bytes), assessments,
    interest profiles and tombstoned references.
    """
    return sha256_hex(canonical_json(record))


def context_digest(entries: Any) -> str:
    """``assessment.context_digest``: what one suitability call showed the model.

    ``entries`` is the list of ``[item_id, content_sha256, coverage]``
    triples of every item shown, the assessed item first and then its
    linked items in the order shown; an item shown without a content
    revision is ``[item_id, None, None]``. The digest is sha256 over the
    canonical JSON of that list, so it changes when a linked item is
    hidden, unlinked, added or recaptured, and an identical context hashes
    identically.
    """
    triples = [[e[0], e[1], e[2]] for e in entries]
    if not triples:
        raise ValueError("a context includes at least the assessed item")
    return sha256_hex(canonical_json(triples))


def normalize_text(text: str | None) -> str:
    """Normalization rule ``muninn-text/1`` for extracted content.

    NFC; CRLF and CR to LF; trailing whitespace stripped from each line;
    runs of three or more newlines collapsed to two; leading and trailing
    whitespace stripped. ``None`` normalizes to the empty string.
    """
    if not text:
        return ""
    out = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    out = "\n".join(line.rstrip() for line in out.split("\n"))
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def content_digest(text: str | None) -> str:
    """``content_sha256`` of a content revision: sha256 of the normalized text."""
    return sha256_hex(normalize_text(text).encode("utf-8"))


# ── ULIDs ────────────────────────────────────────────────────────────

def new_ulid(timestamp_ms: int | None = None, randomness: bytes | None = None) -> str:
    """A 26-character Crockford-base32 ULID (48-bit ms time + 80 random bits)."""
    if timestamp_ms is None:
        timestamp_ms = time.time_ns() // 1_000_000
    if not 0 <= timestamp_ms < 2**48:
        raise ValueError("ULID timestamp out of range")
    if randomness is None:
        randomness = os.urandom(10)
    if len(randomness) != 10:
        raise ValueError("ULID randomness must be 10 bytes")
    value = (timestamp_ms << 80) | int.from_bytes(randomness, "big")
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def is_ulid(value: object) -> bool:
    return isinstance(value, str) and bool(ULID_RE.match(value))


# ── Export identities ────────────────────────────────────────────────

def x_post_item_id(post_id: str) -> str:
    """Export ID for an X post (internal: ``source='x'``, ``source_id=post_id``)."""
    if not post_id.isdigit() or post_id.startswith("0"):
        raise ValueError("X post id must be a positive decimal string")
    return f"x:post:{post_id}"


def x_article_item_id(article_id: str) -> str:
    """Export ID for an X Article (proposed; internal ``source='x_article'``)."""
    if not article_id.isdigit() or article_id.startswith("0"):
        raise ValueError("X article id must be a positive decimal string")
    return f"x:article:{article_id}"


def locator_source_id(sanitized_locator: str) -> str:
    """Internal ``source_id`` of an ``x_link`` row: sha256 of the sanitized locator."""
    return sha256_hex(sanitized_locator.encode("utf-8"))


def link_item_id(sanitized_locator: str) -> str:
    """Export ID for a linked item, from its *sanitized* canonical locator.

    A GitHub repository root (``https://github.com/<owner>/<name>``, with an
    optional ``.git`` or trailing slash) becomes ``github:repo:<owner>/<name>``,
    lowercased. Everything else becomes ``web:<sha256 of the locator>``.
    Callers must pass ``sanitize_url(...).sanitized_url``, never a raw URL.
    """
    m = _GITHUB_REPO_RE.match(sanitized_locator)
    if m and m.group(1).lower() not in _GITHUB_RESERVED_OWNERS:
        return f"github:repo:{m.group(1).lower()}/{m.group(2).lower()}"
    return f"web:{locator_source_id(sanitized_locator)}"


def packet_ref(packet_id: str, revision: int) -> str:
    """The ``ref`` string of a packet revision: ``<packet ULID>@<revision>``."""
    if not is_ulid(packet_id) or revision < 1:
        raise ValueError("invalid packet reference")
    return f"{packet_id}@{revision}"
