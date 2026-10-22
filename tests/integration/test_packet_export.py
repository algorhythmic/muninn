"""``muninn export packets``: packets built from assessments validate, are
content-addressed, re-export is a no-op, revisions chain, tombstones
resolve, and nothing hidden or secret is ever exported. All data synthetic;
assessments come from the suitability task with a MagicMock client."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from muninn.consumers.cli.main import cli
from muninn.consumers.packets import (
    export_packets,
    packet_integrity_errors,
    record_digest,
    resolve_packet,
    withdraw_exported_packet,
)
from muninn.consumers.packets.schemas import schema_errors
from muninn.enrich.interest_profile import load_interest_profile
from muninn.enrich.suitability import assess_suitability
from tests.integration.intake_fakes import PROFILE_V2, fake_client, judgement, write_profile
from tests.integration.intake_seed import seed_link, seed_x_post

T0 = 1_790_000_000
MODEL = "claude-haiku-4-5-20251001"
POST_A, POST_B, POST_H = "1900000000000000021", "1900000000000000022", "1900000000000000023"
A, B, H = (f"x:post:{p}" for p in (POST_A, POST_B, POST_H))
CANARY = "MUNINN-CANARY-7c1e5f0b-not-a-real-secret"


@pytest.fixture
def world(fresh_db: sqlite3.Connection, tmp_path: Path):
    """Post A (candidate) links a page, a repo and a page hidden by policy;
    post B is reference_only; post H is hidden. Linked items are assessed
    too. The canary is planted wherever a careless exporter could pick it up."""
    conn = fresh_db
    a = seed_x_post(conn, POST_A, "Synthetic thread on evaluating retrieval.\n\nNotes and code linked.",
                    captured_at=T0 + 500, published_at=T0 - 86400,
                    extra_metadata={"debug_token": CANARY, "sync_run_id": "01K7ZZZZZZZZZZZZZZZZZZZZZZ"})
    page = seed_link(conn, a, position=0, unwound_url=f"https://example.com/notes?access_token={CANARY}&utm_source=x",
                     text="Hybrid retrieval improved recall at ten.", title="Synthetic notes", retrieved_at=T0 + 510)
    seed_link(conn, a, position=1, expanded_url="https://github.com/Synthetic-Org/synthetic-repo",
              text="synthetic-repo\n\nA toolkit.", coverage="partial", retrieved_at=T0 + 520)
    seed_link(conn, a, position=2, unwound_url="https://private.example.net/doc", text=f"secret page {CANARY}",
              retrieved_at=T0 + 530, content_visible=0)
    seed_x_post(conn, POST_B, "Synthetic note on SQLite STRICT tables.", captured_at=T0 + 400, published_at=T0 - 60)
    seed_x_post(conn, POST_H, f"Hidden synthetic post {CANARY}", captured_at=T0 + 300, published_at=T0 - 60,
                content_visible=0)
    conn.execute("UPDATE scrape_results SET content_html = ?, raw_html_path = ?, error_detail = ? WHERE bookmark_id = ?",
                 (f"<p>{CANARY}</p>", f"/raw/{CANARY}.html", CANARY, page.bookmark_id))
    conn.execute(
        """INSERT INTO sync_checkpoints (run_ulid, source, account_ref, collection, status, next_token, started_at, updated_at)
           VALUES ('01K7ZZZZZZZZZZZZZZZZZZZZZZ', 'x', '1900000000000000901', 'bookmarks', 'running', ?, ?, ?)""",
        (CANARY, T0, T0),
    )
    conn.commit()

    profile = load_interest_profile(write_profile(tmp_path))
    client = fake_client({B: judgement("reference_only", relevance=55)},
                         default=lambda item: judgement("rejected", relevance=10, matched_interests=[])
                         if item.startswith("github:") else judgement())
    assess_suitability(conn, profile, client, model=MODEL, now=T0 + 1000)
    assess_suitability(conn, profile, client, model=MODEL, sources=("x_link",), now=T0 + 1000)
    return conn, tmp_path, profile


def _packet_files(out: Path) -> list[Path]:
    return sorted(out.rglob("*.json"))


def test_packets_validate_and_are_content_addressed(world) -> None:
    conn, tmp, _ = world
    out = tmp / "packets"
    run = export_packets(conn, out, now=T0 + 2000)
    assert [p.status for p in run.written] == ["active"] and not run.invalid
    exported = run.written[0]

    data = exported.path.read_bytes()
    packet = json.loads(data)
    assert exported.path == out / packet["packet_id"] / "1.json"
    assert hashlib.sha256(data).hexdigest() == exported.digest == record_digest(packet)
    assert not data.endswith(b"\n")
    assert schema_errors("evidence-packet", packet) == [] and packet_integrity_errors(packet) == []

    row = conn.execute("SELECT * FROM evidence_packets").fetchone()
    assert row["packet_sha256"] == exported.digest and row["body_json"].encode() == data
    assert row["subject_ref"] == A and row["revision"] == 1 and row["withdrawn_at"] is None

    assert packet["subject_item_id"] == A and packet["revision"] == 1 and "previous_revision" not in packet
    kinds = {i["item_id"].split(":")[0] for i in packet["items"]}
    assert kinds == {"x", "web", "github"}
    assert len(packet["items"]) == 3 and len(packet["item_links"]) == 2  # hidden page dropped
    assert packet["observations"][0]["observed_at_basis"] == "first_seen_by_sync"
    assert {a["item_id"] for a in packet["assessments"]} == {i["item_id"] for i in packet["items"]}
    primary = next(a for a in packet["assessments"] if a["assessment_id"] == packet["primary_assessment"]["ref"])
    assert primary["item_id"] == A and primary["disposition"] == "candidate"
    assert len(packet["evidence"]["supporting"]) == 2 and len(packet["evidence"]["contradictory"]) == 1
    assert packet["repository_revisions"] == []
    assert packet["unresolved_questions"] == ["Is the benchmark corpus available?"]
    assert {u["assessment_id"] for u in packet["usage"]} <= {a["assessment_id"] for a in packet["assessments"]}
    assert resolve_packet(out, exported.ref, exported.digest).status == "active"


def test_disposition_filter(world) -> None:
    conn, tmp, _ = world
    run = export_packets(conn, tmp / "p", dispositions=("reference_only",), now=T0 + 2000, dry_run=True)
    assert run.planned == [B] and not run.written
    assert not (tmp / "p").exists() and conn.execute("SELECT COUNT(*) FROM evidence_packets").fetchone()[0] == 0
    run = export_packets(conn, tmp / "p", dispositions=("candidate", "reference_only"), item_ids=[B], now=T0 + 2000)
    assert [json.loads(p.path.read_bytes())["subject_item_id"] for p in run.written] == [B]


def test_reexport_of_unchanged_inputs_is_a_noop(world) -> None:
    conn, tmp, _ = world
    out = tmp / "packets"
    first = export_packets(conn, out, now=T0 + 2000).written[0]
    before = {p: p.read_bytes() for p in _packet_files(out)}

    again = export_packets(conn, out, now=T0 + 9999)
    assert again.written == [] and again.unchanged == [first.ref] and again.rematerialized == 0
    assert {p: p.read_bytes() for p in _packet_files(out)} == before
    assert conn.execute("SELECT COUNT(*) FROM evidence_packets").fetchone()[0] == 1

    # A fresh directory gets the stored bytes back, identical.
    other = tmp / "elsewhere"
    run = export_packets(conn, other, now=T0 + 9999)
    assert run.rematerialized == 1 and run.written == []
    assert (other / first.path.relative_to(out)).read_bytes() == before[first.path]


def test_reassessment_creates_a_new_revision_with_previous(world) -> None:
    conn, tmp, _ = world
    out = tmp / "packets"
    r1 = export_packets(conn, out, now=T0 + 2000).written[0]

    v2 = load_interest_profile(write_profile(tmp, PROFILE_V2, "v2.yml"))
    assess_suitability(conn, v2, fake_client(default=judgement(relevance=90)), model=MODEL, limit=1, now=T0 + 3000)
    run = export_packets(conn, out, now=T0 + 4000)
    r2 = run.written[0]
    p2 = json.loads(r2.path.read_bytes())

    assert (r2.packet_id, r2.revision) == (r1.packet_id, 2)
    assert p2["previous_revision"] == {"ref": r1.ref, "digest": r1.digest}
    assert len(p2["interest_profiles"]) == 2
    new_primary = next(a for a in p2["assessments"] if a["assessment_id"] == p2["primary_assessment"]["ref"])
    assert new_primary["interest_profile"]["digest"] == v2.digest and "prior_assessment" in new_primary
    assert schema_errors("evidence-packet", p2) == [] and packet_integrity_errors(p2) == []
    # The superseded revision still resolves.
    assert resolve_packet(out, r1.ref, r1.digest).status == "active"
    assert resolve_packet(out, r2.ref, r2.digest).status == "active"


def test_withdrawal_replaces_content_with_a_tombstone(world) -> None:
    conn, tmp, _ = world
    out = tmp / "packets"
    r1 = export_packets(conn, out, now=T0 + 2000).written[0]
    v2 = load_interest_profile(write_profile(tmp, PROFILE_V2, "v2.yml"))
    assess_suitability(conn, v2, fake_client(), model=MODEL, limit=1, now=T0 + 3000)
    r2 = export_packets(conn, out, now=T0 + 4000).written[0]

    tomb = withdraw_exported_packet(conn, out, r1.ref, reason="source_deleted", now=T0 + 5000)
    assert tomb.status == "withdrawn" and tomb.digest == r1.digest
    assert not r1.path.exists() and tomb.path.name == "1.withdrawn.json"
    res = resolve_packet(out, r1.ref, r1.digest)
    assert res.status == "withdrawn" and schema_errors("evidence-packet", res.record) == []
    assert set(res.record) == {"record_type", "contract", "contract_version", "status", "packet_id",
                               "revision", "digest", "withdrawn_at", "withdrawal_reason"}
    assert res.record["withdrawal_reason"] == "source_deleted"
    row = conn.execute("SELECT * FROM evidence_packets WHERE revision = 1").fetchone()
    assert row["body_json"] is None and row["withdrawn_at"] == T0 + 5000
    assert resolve_packet(out, r2.ref, r2.digest).status == "active"  # others untouched

    # Idempotent, and a later export re-materializes the tombstone elsewhere.
    again = withdraw_exported_packet(conn, out, r1.ref, reason="source_deleted", now=T0 + 6000)
    assert again.path.read_bytes() == tomb.path.read_bytes()
    other = tmp / "mirror"
    export_packets(conn, other, now=T0 + 7000)
    assert resolve_packet(other, r1.ref, r1.digest).status == "withdrawn"
    assert resolve_packet(other, r2.ref, r2.digest).status == "active"


def test_withdrawn_latest_is_not_republished_until_reassessed(world) -> None:
    conn, tmp, profile = world
    out = tmp / "packets"
    r1 = export_packets(conn, out, now=T0 + 2000).written[0]
    withdraw_exported_packet(conn, out, r1.ref, reason="operator_request", now=T0 + 3000)
    run = export_packets(conn, out, now=T0 + 4000)
    assert run.written == [] and run.skipped and "withdrawn" in run.skipped[0][1]

    v2 = load_interest_profile(write_profile(tmp, PROFILE_V2, "v2.yml"))
    assess_suitability(conn, v2, fake_client(), model=MODEL, limit=1, now=T0 + 5000)
    r2 = export_packets(conn, out, now=T0 + 6000).written[0]
    assert r2.revision == 2
    assert json.loads(r2.path.read_bytes())["previous_revision"] == {"ref": r1.ref, "digest": r1.digest}
    assert resolve_packet(out, r1.ref, r1.digest).status == "withdrawn"


def test_withdraw_rejects_unknown_refs_and_reasons(world) -> None:
    conn, tmp, _ = world
    out = tmp / "packets"
    r1 = export_packets(conn, out, now=T0 + 2000).written[0]
    with pytest.raises(ValueError):
        withdraw_exported_packet(conn, out, r1.ref, reason="because")
    with pytest.raises(LookupError):
        withdraw_exported_packet(conn, out, f"{r1.packet_id}@7", reason="retention")


def test_invalid_packet_is_refused_not_written(world) -> None:
    conn, tmp, _ = world
    # An unsanitized locator sneaks into a revision: the schema must stop the packet.
    conn.execute("UPDATE scrape_history SET final_locator = ? WHERE item_ref LIKE 'web:%'",
                 (f"https://example.com/notes?access_token={CANARY}",))
    conn.commit()
    out = tmp / "packets"
    run = export_packets(conn, out, now=T0 + 2000)
    assert run.written == [] and run.invalid and run.invalid[0][0] == A
    assert any("final_locator" in e for e in run.invalid[0][1])
    assert CANARY not in json.dumps(run.summary())  # errors never echo the value
    assert not out.exists() or _packet_files(out) == []
    assert conn.execute("SELECT COUNT(*) FROM evidence_packets").fetchone()[0] == 0


def test_canary_and_hidden_content_never_exported(world, monkeypatch) -> None:
    conn, tmp, _ = world
    monkeypatch.setenv("ANTHROPIC_API_KEY", CANARY)
    out = tmp / "packets"
    run = export_packets(conn, out, dispositions=("candidate", "reference_only", "needs_evidence", "deferred", "rejected"),
                         now=T0 + 2000)
    assert len(run.written) == 2 and not run.invalid  # A and B; H is hidden
    subjects = set()
    for path in _packet_files(out):
        data = path.read_bytes()
        assert CANARY.encode() not in data, path
        packet = json.loads(data)
        subjects.add(packet["subject_item_id"])
        assert "private.example.net" not in data.decode()
        assert H not in {i["item_id"] for i in packet["items"]}
    assert subjects == {A, B}
    for (body,) in conn.execute("SELECT body_json FROM evidence_packets"):
        assert CANARY not in body
    # The hidden post was never assessed either, so nothing about it can leak.
    assert conn.execute("SELECT COUNT(*) FROM assessments WHERE item_ref = ?", (H,)).fetchone()[0] == 0


def test_export_never_includes_withdrawn_content_revisions(world) -> None:
    conn, tmp, _ = world
    conn.execute(
        "UPDATE scrape_history SET content_text = NULL, withdrawn_at = ?, withdrawal_reason = 'retention' "
        "WHERE item_ref LIKE 'github:%'",
        (T0 + 1500,),
    )
    conn.commit()
    packet = json.loads(export_packets(conn, tmp / "p", now=T0 + 2000).written[0].path.read_bytes())
    assert not any(r["item_id"].startswith("github:") for r in packet["content_revisions"])
    assert not any(a["item_id"].startswith("github:") for a in packet["assessments"])
    assert packet_integrity_errors(packet) == []


def test_cli_export_and_withdraw(world) -> None:
    conn, tmp, _ = world
    db = conn.execute("PRAGMA database_list").fetchone()["file"]
    out = tmp / "cli-packets"
    runner = CliRunner()
    result = runner.invoke(cli, ["--db", db, "export", "packets", "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert "Packets exported:" in result.output
    ref = json.loads(result.output.split("Packets exported: ", 1)[1])["written"][0]

    result = runner.invoke(cli, ["--db", db, "export", "packets", "--out", str(out)])
    assert result.exit_code == 0 and json.loads(result.output.split("Packets exported: ", 1)[1])["unchanged"] == [ref]

    result = runner.invoke(cli, ["--db", db, "export", "packets", "--out", str(out), "--withdraw", ref])
    assert result.exit_code == 2 and "--reason" in result.output
    result = runner.invoke(cli, ["--db", db, "export", "packets", "--out", str(out),
                                 "--withdraw", ref, "--reason", "operator_request"])
    assert result.exit_code == 0, result.output
    packet_id, rev = ref.split("@")
    assert (out / packet_id / f"{rev}.withdrawn.json").exists()
