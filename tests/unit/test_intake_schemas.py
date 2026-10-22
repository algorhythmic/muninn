"""muninn-research-intake 0.1.0-draft: schemas, fixtures, digests, identity.

Every record schema has valid and invalid fixtures (all synthetic, under
tests/fixtures/intake/), and this module proves both behave. It also checks
the cross-record rules, the canonical-JSON digest rules, the identity
helpers, the canary, and the release manifest named in the contract.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterator

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from muninn.consumers.packets import (
    CanonicalizationError,
    canonical_json,
    content_digest,
    is_ulid,
    link_item_id,
    locator_source_id,
    make_tombstone,
    new_ulid,
    normalize_text,
    packet_integrity_errors,
    packet_ref,
    record_digest,
    resolve_packet,
    withdraw_packet,
    write_packet,
    x_post_item_id,
)
from muninn.sanitize import sanitize_url

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = ROOT / "schemas" / "json" / "intake"
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "intake"
CONTRACT = ROOT / "docs" / "contracts" / "MUNINN-RESEARCH-INTAKE.md"
MANIFEST = ROOT / "docs" / "contracts" / "MUNINN-RESEARCH-INTAKE-0.1.0-draft.sha256"

RECORDS = (
    "source-item",
    "item-link",
    "bookmark-observation",
    "content-revision",
    "interest-profile",
    "assessment",
    "evidence-packet",
    "usage-record",
)


# ── Loading ──────────────────────────────────────────────────────────

def _schemas() -> dict[str, dict[str, Any]]:
    return {p.name.removesuffix(".schema.json"): json.loads(p.read_text()) for p in SCHEMA_DIR.glob("*.schema.json")}


SCHEMAS = _schemas()
REGISTRY = Registry().with_resources(
    (s["$id"], Resource.from_contents(s)) for s in SCHEMAS.values()
)


def validator(record_type: str) -> Draft202012Validator:
    return Draft202012Validator(SCHEMAS[record_type], registry=REGISTRY)


def _fixture_files() -> list[Path]:
    return sorted(p for p in FIXTURE_DIR.glob("*/*.json") if p.parent.name in RECORDS)


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def fixture(record_type: str, name: str) -> dict[str, Any]:
    matches = list((FIXTURE_DIR / record_type).glob(f"{name}.*.json"))
    assert len(matches) == 1, (record_type, name)
    return load(matches[0])["record"]


def _walk(node: Any) -> Iterator[Any]:
    yield node
    if isinstance(node, dict):
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


# ── Schema shape ─────────────────────────────────────────────────────

def test_every_record_has_a_schema_and_nothing_else_is_loose() -> None:
    assert set(SCHEMAS) == set(RECORDS) | {"defs"}


@pytest.mark.parametrize("name", sorted(SCHEMAS))
def test_schema_is_valid_draft_2020_12_with_contract_id(name: str) -> None:
    schema = SCHEMAS[name]
    Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"] == f"urn:muninn:research-intake:{name}:0.1"


@pytest.mark.parametrize("name", sorted(SCHEMAS))
def test_objects_are_closed_and_numbers_are_integers(name: str) -> None:
    for node in _walk(SCHEMAS[name]):
        if not isinstance(node, dict):
            continue
        if "properties" in node and node.get("type") == "object":
            assert node.get("additionalProperties") is False, node.get("title", node)
        types = node.get("type")
        assert types != "number" and not (isinstance(types, list) and "number" in types)


# ── Fixtures ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("record_type", RECORDS)
def test_every_schema_has_valid_and_invalid_fixtures(record_type: str) -> None:
    names = [p.name for p in (FIXTURE_DIR / record_type).glob("*.json")]
    assert any(n.endswith(".valid.json") for n in names)
    assert any(n.endswith(".invalid.json") for n in names)


@pytest.mark.parametrize("path", _fixture_files(), ids=lambda p: f"{p.parent.name}/{p.name}")
def test_fixture_behaves(path: Path) -> None:
    doc = load(path)
    meta, record = doc["fixture"], doc["record"]
    expect = path.name.rsplit(".", 2)[-2]
    assert meta["synthetic"] is True and "synthetic" in meta["provenance"]
    assert meta["schema"] == path.parent.name and meta["expect"] == expect
    errors = list(validator(path.parent.name).iter_errors(record))
    if expect == "valid":
        assert errors == [], [e.message for e in errors]
        return
    assert expect == "invalid"
    assert errors, f"{path.name} should be rejected: {meta['why']}"
    # It must fail for the stated reason, not incidentally.
    flat = list(_flatten(errors))
    hits = [
        e for e in flat
        if "/".join(map(str, e.absolute_path)) == meta["fails_at"]
        and meta.get("fails_with", "") in e.message
    ]
    assert hits, (meta["fails_at"], meta.get("fails_with"), [(list(e.absolute_path), e.message[:80]) for e in flat])


def _flatten(errors: Any) -> Iterator[Any]:
    for e in errors:
        yield e
        yield from _flatten(e.context or [])


def _valid_records() -> list[tuple[Path, dict[str, Any]]]:
    return [(p, load(p)["record"]) for p in _fixture_files() if p.name.endswith(".valid.json")]


def test_valid_fixtures_are_in_the_canonical_subset() -> None:
    for path, record in _valid_records():
        assert not any(isinstance(v, float) for v in _walk(record)), path
        assert json.loads(canonical_json(record)) == record


def test_valid_active_packets_are_internally_consistent() -> None:
    for name in ("packet-r1", "packet-r2"):
        assert packet_integrity_errors(fixture("evidence-packet", name)) == []


def test_assessment_evidence_quotes_its_cited_revision() -> None:
    for name, revision, stance in (("web-r1-profile-v1", "web-live-r1", "supports"),
                                   ("web-r2-changed-content", "web-live-r2-changed", "contradicts")):
        (ev,) = fixture("assessment", name)["evidence"]
        rev = fixture("content-revision", revision)
        assert ev["stance"] == stance and ev["item_id"] == rev["item_id"]
        assert ev["source"] == {"ref": rev["revision_id"], "digest": rev["content_sha256"]}
        assert ev["excerpt"] in rev["text"]
    # The packet carries the stored evidence as statement + excerpt.
    packet = fixture("evidence-packet", "packet-r1")
    a1 = fixture("assessment", "web-r1-profile-v1")
    assert {"statement": a1["evidence"][0]["note"], "source": a1["evidence"][0]["source"],
            "excerpt": a1["evidence"][0]["excerpt"]} in packet["evidence"]["supporting"]


def test_integrity_check_catches_bad_assessment_evidence() -> None:
    packet = copy.deepcopy(fixture("evidence-packet", "packet-r1"))
    packet["assessments"][0]["evidence"][0]["excerpt"] = "Not in the page."
    assert any("assessments[0].evidence[0].excerpt is not in the revision text" in e
               for e in packet_integrity_errors(packet))
    packet = copy.deepcopy(fixture("evidence-packet", "packet-r1"))
    packet["assessments"][0]["evidence"][0]["item_id"] = "x:post:1900000000000000999"
    assert any("assessments[0].evidence[0].item_id is not in items" in e for e in packet_integrity_errors(packet))


def test_integrity_check_catches_a_wrong_digest() -> None:
    packet = copy.deepcopy(fixture("evidence-packet", "packet-r1"))
    packet["evidence"]["supporting"][0]["source"]["digest"] = "0" * 64
    packet["primary_assessment"]["digest"] = "0" * 64
    errors = packet_integrity_errors(packet)
    assert any("evidence.supporting[0]" in e for e in errors)
    assert any("primary_assessment" in e for e in errors)


# ── Acceptance scenarios ─────────────────────────────────────────────

def test_changed_content_creates_a_new_revision() -> None:
    r1 = fixture("content-revision", "web-live-r1")
    r2 = fixture("content-revision", "web-live-r2-changed")
    assert r1["item_id"] == r2["item_id"]
    assert r1["revision_id"] != r2["revision_id"]
    assert r1["content_sha256"] != r2["content_sha256"]
    for r in (r1, r2):
        assert content_digest(r["text"]) == r["content_sha256"]


def test_changed_profile_reassessment_appends() -> None:
    a1 = fixture("assessment", "web-r1-profile-v1")
    a2 = fixture("assessment", "web-r1-profile-v2-reassessed")
    v1 = fixture("interest-profile", "profile-v1")
    v2 = fixture("interest-profile", "profile-v2-changed")
    assert a1["content_revision"] == a2["content_revision"]
    assert a1["interest_profile"]["digest"] == record_digest(v1)
    assert a2["interest_profile"]["digest"] == record_digest(v2)
    assert a1["interest_profile"]["digest"] != a2["interest_profile"]["digest"]
    assert a2["prior_assessment"] == {"ref": a1["assessment_id"], "digest": record_digest(a1)}


def test_blocked_and_partial_captures_are_assessed_with_coverage_shown() -> None:
    blocked = fixture("assessment", "blocked-capture")
    partial = fixture("assessment", "partial-capture")
    assert blocked["coverage"] == "blocked" and blocked["disposition"] == "needs_evidence"
    assert partial["coverage"] == "partial"
    rev = fixture("content-revision", "blocked")
    assert blocked["content_revision"] == {"ref": rev["revision_id"], "digest": rev["content_sha256"]}
    assert rev["content_sha256"] == hashlib.sha256(b"").hexdigest() and rev["char_count"] == 0


def test_old_packet_still_resolves_after_a_new_revision(tmp_path: Path) -> None:
    p1 = fixture("evidence-packet", "packet-r1")
    p2 = fixture("evidence-packet", "packet-r2")
    assert p2["packet_id"] == p1["packet_id"] and p2["revision"] == 2
    assert p2["previous_revision"] == {"ref": packet_ref(p1["packet_id"], 1), "digest": record_digest(p1)}

    e1, e2 = write_packet(tmp_path, p1), write_packet(tmp_path, p2)
    assert hashlib.sha256(e1.path.read_bytes()).hexdigest() == e1.digest == record_digest(p1)
    old = resolve_packet(tmp_path, p2["previous_revision"]["ref"], p2["previous_revision"]["digest"])
    assert old.status == "active" and old.record == p1
    assert resolve_packet(tmp_path, e2.ref, e2.digest).status == "active"
    assert resolve_packet(tmp_path, e1.ref, "f" * 64).status == "digest_mismatch"


def test_withdrawn_packet_resolves_to_its_tombstone(tmp_path: Path) -> None:
    p1 = fixture("evidence-packet", "packet-r1")
    exported = write_packet(tmp_path, p1)
    tomb = withdraw_packet(tmp_path, p1["packet_id"], 1, reason="source_deleted", withdrawn_at="2026-10-22T00:00:00Z")
    assert not exported.path.exists() and tomb.digest == exported.digest
    res = resolve_packet(tmp_path, exported.ref, exported.digest)
    assert res.status == "withdrawn"
    assert list(validator("evidence-packet").iter_errors(res.record)) == []
    assert set(res.record) == {
        "record_type", "contract", "contract_version", "status", "packet_id",
        "revision", "digest", "withdrawn_at", "withdrawal_reason",
    }
    assert resolve_packet(tmp_path, packet_ref(new_ulid(), 1), "0" * 64).status == "missing"


def test_tombstone_fixture_shape() -> None:
    tomb = fixture("evidence-packet", "withdrawn-tombstone")
    assert tomb["status"] == "withdrawn" and tomb["withdrawal_reason"] == "operator_request"
    with pytest.raises(ValueError):
        make_tombstone(fixture("evidence-packet", "packet-r1"), reason="because", withdrawn_at="2026-10-22T00:00:00Z")


# ── Canary ───────────────────────────────────────────────────────────

def test_canary_never_reaches_a_valid_export_fixture() -> None:
    raw = load(FIXTURE_DIR / "canary" / "raw-inputs.json")
    canary = raw["canary"]
    assert canary in json.dumps(raw)
    for path, _record in _valid_records():
        assert canary not in path.read_text(encoding="utf-8"), path
    for path in SCHEMA_DIR.glob("*.json"):
        assert canary not in path.read_text(encoding="utf-8")
    if CONTRACT.exists():
        assert canary not in CONTRACT.read_text(encoding="utf-8")


def test_canary_is_removed_by_sanitize_before_identity() -> None:
    raw = load(FIXTURE_DIR / "canary" / "raw-inputs.json")
    entity = raw["url_entities"]["web"]
    chosen = entity["unwound_url"] or entity["expanded_url"] or entity["url"]
    result = sanitize_url(chosen)
    assert raw["canary"] not in result.sanitized_url
    item = fixture("source-item", "web-page-sanitized")
    assert item["locator"] == result.sanitized_url
    assert item["item_id"] == link_item_id(result.sanitized_url)
    assert item["redaction"]["param_names"] == result.redacted_param_names
    bad = load(FIXTURE_DIR / "source-item" / "unsanitized-locator.invalid.json")["record"]
    assert raw["canary"] in bad["locator"]
    assert list(validator("source-item").iter_errors(bad))


# ── Canonical JSON, digests, identities ──────────────────────────────

def test_canonical_json_matches_the_jcs_form_on_the_allowed_subset() -> None:
    value = {"b": 1, "a": [True, None, "é\n "], "é": {"z": -3, "y": ""}}
    assert canonical_json(value) == '{"a":[true,null,"é\\n "],"b":1,"é":{"y":"","z":-3}}'.encode()
    assert canonical_json({"c": "\x01\x1f\"\\"}) == b'{"c":"\\u0001\\u001f\\"\\\\"}'


@pytest.mark.parametrize("bad", [{"x": 1.5}, {"x": 2**53}, {"\U0001f600": 1}, {1: "a"}, {"x": {1, 2}}])
def test_canonical_json_rejects_values_outside_the_subset(bad: Any) -> None:
    with pytest.raises(CanonicalizationError):
        canonical_json(bad)


def test_text_normalization() -> None:
    assert normalize_text("  a \r\nb\t\r\n\r\n\r\n\nc  ") == "a\nb\n\nc"
    assert normalize_text("é") == "é"
    assert normalize_text(None) == "" and content_digest(None) == hashlib.sha256(b"").hexdigest()


def test_identity_rules() -> None:
    assert x_post_item_id("1900000000000000001") == "x:post:1900000000000000001"
    for bad in ("", "0123", "12a"):
        with pytest.raises(ValueError):
            x_post_item_id(bad)
    assert link_item_id("https://github.com/Synthetic-Org/Synthetic-Repo.git") == "github:repo:synthetic-org/synthetic-repo"
    assert link_item_id("https://github.com/synthetic-org/synthetic-repo/") == "github:repo:synthetic-org/synthetic-repo"
    deep = "https://github.com/synthetic-org/synthetic-repo/blob/main/README.md"
    assert link_item_id(deep) == f"web:{locator_source_id(deep)}"
    assert link_item_id("https://github.com/topics/search").startswith("web:")
    web = "https://example.com/a"
    assert link_item_id(web) == "web:" + hashlib.sha256(web.encode()).hexdigest()


def test_ulids() -> None:
    a = new_ulid(1_700_000_000_000, bytes(10))
    b = new_ulid(1_700_000_000_001, bytes(10))
    assert is_ulid(a) and is_ulid(b) and a < b and len(a) == 26
    assert new_ulid(0, bytes(10)) == "0" * 26
    assert new_ulid(2**48 - 1, b"\xff" * 10) == "7" + "Z" * 25


def test_packets_package_stays_light() -> None:
    heavy = ("qdrant_client", "duckdb", "pyarrow", "mcp", "jinja2", "sentence_transformers", "torch", "anthropic", "httpx")
    code = (
        "import json, sys\n"
        "import muninn.consumers.packets\n"
        f"print(json.dumps(sorted(m for m in {heavy!r} if m in sys.modules)))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
    )
    assert json.loads(out.stdout) == []


# ── Release manifest ─────────────────────────────────────────────────

def _release_files() -> list[str]:
    files = [CONTRACT] + sorted(SCHEMA_DIR.glob("*.schema.json")) + sorted(
        p for p in FIXTURE_DIR.rglob("*") if p.is_file() and "__pycache__" not in p.parts
    )
    return [p.relative_to(ROOT).as_posix() for p in files]


def test_release_manifest_matches_the_files() -> None:
    """The contract's Releases table names this manifest. It must list the
    sha256 of the contract and of every schema and fixture, exactly.
    Regenerate with: python scripts/intake-release-manifest.py"""
    lines = MANIFEST.read_text(encoding="utf-8").splitlines()
    listed = dict(reversed(line.split("  ", 1)) for line in lines)
    assert sorted(listed) == sorted(_release_files()), "manifest file set is stale"
    for rel, digest in listed.items():
        actual = hashlib.sha256((ROOT / rel).read_bytes()).hexdigest()
        assert actual == digest, f"{rel} changed; regenerate the manifest"


def test_contract_header_and_no_relative_links() -> None:
    text = CONTRACT.read_text(encoding="utf-8")
    assert "Contract: muninn-research-intake, version 0.1.0-draft" in text.splitlines()[:5]

    for target in re.findall(r"\]\(([^)]+)\)", text):
        assert re.match(r"^(https?://|#)", target), f"relative link {target!r}"
