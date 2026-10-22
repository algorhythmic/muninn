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
    link_item_id,
    packet_integrity_errors,
    record_digest,
    resolve_packet,
    withdraw_exported_packet,
    withdrawals_due,
)
from muninn.consumers.packets.schemas import schema_errors
from muninn.enrich.interest_profile import load_interest_profile
from muninn.enrich.suitability import assess_suitability
from muninn.sanitize import sanitize_url
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
                    extra_metadata={"debug_token": CANARY, "first_sync_run_id": "01K7ZZZZZZZZZZZZZZZZZZZZZZ"})
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


# ── Model-cited evidence ─────────────────────────────────────────────

PAGE = link_item_id(sanitize_url(f"https://example.com/notes?access_token={CANARY}&utm_source=x").sanitized_url)
PRIVATE = link_item_id(sanitize_url("https://private.example.net/doc").sanitized_url)
REPO = "github:repo:synthetic-org/synthetic-repo"


def _cite(item: str, excerpt: str, stance: str = "supports", note: str = "The page reports the measured gain.") -> dict:
    return {"item_id": item, "stance": stance, "excerpt": excerpt, "note": note}


def _reassess_a(conn, tmp, judgement_value, now: int) -> None:
    v2 = load_interest_profile(write_profile(tmp, PROFILE_V2, "v2.yml"))
    run = assess_suitability(conn, v2, fake_client({A: judgement_value}), model=MODEL, limit=1, now=now)
    assert run.assessed == 1


def test_packet_uses_stored_model_cited_evidence(world) -> None:
    conn, tmp, _ = world
    _reassess_a(conn, tmp, judgement(evidence=[
        _cite(PAGE, "Hybrid retrieval improved recall at ten."),
        _cite(A, "Notes and code linked.", "contradicts", "The post itself states no result."),
        _cite(PRIVATE, f"secret page {CANARY}"),  # hidden: never shown, so dropped
    ]), now=T0 + 1500)
    packet = json.loads(export_packets(conn, tmp / "p", now=T0 + 2000).written[0].path.read_bytes())
    assert schema_errors("evidence-packet", packet) == [] and packet_integrity_errors(packet) == []

    primary = next(a for a in packet["assessments"] if a["assessment_id"] == packet["primary_assessment"]["ref"])
    assert [e["item_id"] for e in primary["evidence"]] == [PAGE, A]
    page_rev = next(r for r in packet["content_revisions"] if r["item_id"] == PAGE)
    supporting, contradictory = packet["evidence"]["supporting"], packet["evidence"]["contradictory"]
    # The subject's stored evidence replaces its derived statement; linked
    # items without stored evidence keep their derived ones.
    assert supporting[0] == {"statement": "The page reports the measured gain.",
                             "source": {"ref": page_rev["revision_id"], "digest": page_rev["content_sha256"]},
                             "excerpt": "Hybrid retrieval improved recall at ten."}
    assert contradictory[0]["excerpt"] == "Notes and code linked."
    assert not any(e["source"] == primary["content_revision"] and "excerpt" not in e for e in supporting)
    assert len(supporting) == 2 and len(contradictory) == 2  # + page (candidate), repo (rejected)
    assert CANARY not in json.dumps(packet)


def test_integrity_rejects_bad_assessment_evidence(world) -> None:
    conn, tmp, _ = world
    _reassess_a(conn, tmp, judgement(evidence=[_cite(PAGE, "Hybrid retrieval improved recall at ten.")]), now=T0 + 1500)
    packet = json.loads(export_packets(conn, tmp / "p", now=T0 + 2000).written[0].path.read_bytes())

    def tamper(change) -> list[str]:
        copy_ = json.loads(json.dumps(packet))
        change(next(a for a in copy_["assessments"] if a.get("evidence"))["evidence"][0])
        return packet_integrity_errors(copy_)

    assert any("excerpt is not in the revision text" in e
               for e in tamper(lambda ev: ev.update(excerpt="Hybrid retrieval doubled recall.")))
    assert any("evidence[0].item_id is not in items" in e for e in tamper(lambda ev: ev.update(item_id=PRIVATE)))
    assert any("evidence[0].source" in e for e in tamper(lambda ev: ev["source"].update(digest="0" * 64)))


def test_evidence_citing_a_since_hidden_item_is_never_exported(world) -> None:
    conn, tmp, _ = world
    _reassess_a(conn, tmp, judgement(evidence=[_cite(PAGE, "Hybrid retrieval improved recall at ten.")]), now=T0 + 1500)
    conn.execute("UPDATE bookmarks SET content_visible = 0 WHERE source = 'x_link' AND url LIKE 'https://example.com/%'")
    conn.commit()
    out = tmp / "p"
    run = export_packets(conn, out, now=T0 + 2000)
    assert run.written == [] and not run.invalid
    assert run.skipped == [(A, "primary assessment cites content the packet cannot include; reassess")]
    assert not out.exists() or _packet_files(out) == []


# ── Withdrawal automation ────────────────────────────────────────────

def _bookmark_id(conn, where: str, *args) -> int:
    return conn.execute(f"SELECT bookmark_id FROM bookmarks WHERE {where}", args).fetchone()[0]


def _revisions(conn, item_ref: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM scrape_history WHERE item_ref = ? ORDER BY scrape_history_id", (item_ref,)
    ).fetchall()


def test_hidden_subject_withdraws_its_packets_as_policy_hidden(world) -> None:
    conn, tmp, _ = world
    out = tmp / "packets"
    r1 = export_packets(conn, out, now=T0 + 2000).written[0]
    conn.execute("UPDATE bookmarks SET content_visible = 0 WHERE source = 'x' AND source_id = ?", (POST_A,))
    conn.commit()

    run = export_packets(conn, out, now=T0 + 3000)
    assert [(p.ref, reason) for p, reason in run.withdrawn] == [(r1.ref, "policy_hidden")]
    assert run.summary()["withdrawn_counts"] == {"policy_hidden": 1} and run.revisions_withdrawn == 0
    assert resolve_packet(out, r1.ref, r1.digest).status == "withdrawn" and not r1.path.exists()
    row = conn.execute("SELECT * FROM evidence_packets").fetchone()
    assert (row["withdrawal_reason"], row["withdrawn_at"], row["body_json"]) == ("policy_hidden", T0 + 3000, None)
    # Hidden material is kept by muninn, just never exported.
    assert all(r["withdrawn_at"] is None and r["content_text"] for r in _revisions(conn, A))
    assert export_packets(conn, out, now=T0 + 4000).withdrawn == []  # idempotent


def test_hidden_linked_item_withdraws_every_active_revision(world) -> None:
    conn, tmp, _ = world
    out = tmp / "packets"
    r1 = export_packets(conn, out, now=T0 + 2000).written[0]
    _reassess_a(conn, tmp, judgement(relevance=90), now=T0 + 2500)
    r2 = export_packets(conn, out, now=T0 + 3000).written[0]
    assert r2.revision == 2
    conn.execute("UPDATE bookmarks SET content_visible = 0 WHERE source = 'x_link' AND url LIKE 'https://github.com/%'")
    conn.commit()

    run = export_packets(conn, out, now=T0 + 4000)
    assert sorted(p.ref for p, _ in run.withdrawn) == sorted([r1.ref, r2.ref])
    assert run.withdrawn_counts() == {"policy_hidden": 2}
    # Not re-exported until reassessed: the withdrawal is newer than the assessment.
    assert run.written == [] and run.skipped and "withdrawn" in run.skipped[0][1]
    for exported in (r1, r2):
        assert resolve_packet(out, exported.ref, exported.digest).status == "withdrawn"


def test_deleted_linked_item_withdraws_the_packet_and_its_revisions(world) -> None:
    conn, tmp, _ = world
    out = tmp / "packets"
    r1 = export_packets(conn, out, now=T0 + 2000).written[0]
    conn.execute("DELETE FROM bookmarks WHERE bookmark_id = ?",
                 (_bookmark_id(conn, "source = 'x_link' AND url LIKE 'https://example.com/%'"),))
    conn.commit()

    run = export_packets(conn, out, now=T0 + 3000)
    assert [(p.ref, reason) for p, reason in run.withdrawn] == [(r1.ref, "source_deleted")]
    assert run.revisions_withdrawn == 1 and run.summary()["revisions_withdrawn"] == 1
    (page_rev,) = _revisions(conn, PAGE)
    assert (page_rev["content_text"], page_rev["withdrawn_at"], page_rev["withdrawal_reason"]) == (
        None, T0 + 3000, "source_deleted")
    # The subject and the other linked item still exist, so their content stays.
    assert all(r["withdrawn_at"] is None for r in _revisions(conn, A) + _revisions(conn, REPO))
    tomb = json.loads((out / r1.packet_id / "1.withdrawn.json").read_bytes())
    assert tomb["withdrawal_reason"] == "source_deleted" and schema_errors("evidence-packet", tomb) == []


def test_deleted_subject_withdraws_as_source_deleted(world) -> None:
    conn, tmp, _ = world
    out = tmp / "packets"
    r1 = export_packets(conn, out, now=T0 + 2000).written[0]
    conn.execute("DELETE FROM bookmarks WHERE source = 'x' AND source_id = ?", (POST_A,))
    conn.commit()
    run = export_packets(conn, out, now=T0 + 3000)
    assert [(p.ref, reason) for p, reason in run.withdrawn] == [(r1.ref, "source_deleted")]
    assert all(r["withdrawn_at"] == T0 + 3000 and r["content_text"] is None for r in _revisions(conn, A))
    assert all(r["withdrawn_at"] is None for r in _revisions(conn, PAGE))
    assert resolve_packet(out, r1.ref, r1.digest).status == "withdrawn"


def _link_page_from_b(conn) -> None:
    """Post B also links the page A links: one x_link row, two links."""
    page = _bookmark_id(conn, "source = 'x_link' AND url LIKE 'https://example.com/%'")
    conn.execute(
        """INSERT INTO item_links (parent_bookmark_id, child_bookmark_id, parent_ref, child_ref, relation,
                                   position, locator_resolution, first_seen_at)
           VALUES (?, ?, ?, ?, 'links_to', 0, 'unwound', ?)""",
        (_bookmark_id(conn, "source = 'x' AND source_id = ?", POST_B), page, B, PAGE, T0 + 600),
    )
    conn.commit()


def _export_a_and_b(conn, out: Path) -> dict[str, object]:
    run = export_packets(conn, out, dispositions=("candidate", "reference_only"), now=T0 + 2000)
    written = {json.loads(p.path.read_bytes())["subject_item_id"]: p for p in run.written}
    assert set(written) == {A, B}
    return written


def test_retention_spares_revisions_another_active_packet_holds(world) -> None:
    conn, tmp, _ = world
    _link_page_from_b(conn)
    out = tmp / "packets"
    written = _export_a_and_b(conn, out)

    tomb = withdraw_exported_packet(conn, out, written[A].ref, reason="retention", now=T0 + 3000)
    assert tomb.revisions_withdrawn == 2  # A's post and the repo; the shared page is held by B
    (page_rev,) = _revisions(conn, PAGE)
    assert page_rev["withdrawn_at"] is None and page_rev["content_text"]
    for r in _revisions(conn, A) + _revisions(conn, REPO):
        assert (r["withdrawal_reason"], r["content_text"]) == ("retention", None)

    # Once no active packet holds it, a retention withdrawal clears it too.
    assert withdraw_exported_packet(conn, out, written[B].ref, reason="retention", now=T0 + 4000).revisions_withdrawn == 2
    (page_rev,) = _revisions(conn, PAGE)
    assert (page_rev["withdrawal_reason"], page_rev["content_text"]) == ("retention", None)


def test_shared_revision_is_withdrawn_with_its_last_packet(world) -> None:
    conn, tmp, _ = world
    _link_page_from_b(conn)
    out = tmp / "packets"
    _export_a_and_b(conn, out)
    conn.execute("DELETE FROM bookmarks WHERE source = 'x_link' AND url LIKE 'https://example.com/%'")
    conn.commit()
    run = export_packets(conn, out, now=T0 + 3000)
    assert run.withdrawn_counts() == {"source_deleted": 2} and run.revisions_withdrawn == 1
    (page_rev,) = _revisions(conn, PAGE)
    assert page_rev["withdrawal_reason"] == "source_deleted" and page_rev["content_text"] is None


def test_operator_request_keeps_content_revisions(world) -> None:
    conn, tmp, _ = world
    out = tmp / "packets"
    r1 = export_packets(conn, out, now=T0 + 2000).written[0]
    assert withdraw_exported_packet(conn, out, r1.ref, reason="operator_request", now=T0 + 3000).revisions_withdrawn == 0
    assert conn.execute("SELECT COUNT(*) FROM scrape_history WHERE withdrawn_at IS NOT NULL").fetchone()[0] == 0


def test_dry_run_reports_due_withdrawals_and_changes_nothing(world) -> None:
    conn, tmp, _ = world
    out = tmp / "packets"
    r1 = export_packets(conn, out, now=T0 + 2000).written[0]
    conn.execute("UPDATE bookmarks SET content_visible = 0 WHERE source = 'x' AND source_id = ?", (POST_A,))
    conn.commit()
    assert [(d.ref, d.reason, d.hidden_items) for d in withdrawals_due(conn)] == [(r1.ref, "policy_hidden", (A,))]
    run = export_packets(conn, out, dry_run=True, now=T0 + 3000)
    assert run.withdrawn == [] and run.summary()["withdrawn"] == [f"{r1.ref}: policy_hidden"]
    assert run.withdrawn_counts() == {"policy_hidden": 1}
    assert r1.path.exists() and conn.execute("SELECT withdrawn_at FROM evidence_packets").fetchone()[0] is None


def test_cli_reports_withdrawals(world) -> None:
    conn, tmp, _ = world
    db = conn.execute("PRAGMA database_list").fetchone()["file"]
    out = tmp / "cli-packets"
    runner = CliRunner()
    assert runner.invoke(cli, ["--db", db, "export", "packets", "--out", str(out)]).exit_code == 0
    conn.execute("UPDATE bookmarks SET content_visible = 0 WHERE source = 'x' AND source_id = ?", (POST_A,))
    conn.commit()
    result = runner.invoke(cli, ["--db", db, "export", "packets", "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert "withdrew" in result.output and "policy_hidden" in result.output
    summary = json.loads(result.output.split("Packets exported: ", 1)[1])
    assert summary["withdrawn_counts"] == {"policy_hidden": 1} and summary["revisions_withdrawn"] == 0
