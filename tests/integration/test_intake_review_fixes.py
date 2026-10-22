"""Regression tests for the 2026-10-21 review of the Research Intake lane:
current revisions, prompt versions, hidden-locator masking, the assessment
context digest, source deletion, duplicate items and the packet dry run.

ALL DATA IS SYNTHETIC (invented IDs, reserved example hosts). The model
client is a MagicMock (tests/integration/intake_fakes.py)."""

from __future__ import annotations

import json
import sqlite3
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from muninn.consumers.cli.main import cli
from muninn.consumers.packets import (
    context_digest,
    export_packets,
    packet_integrity_errors,
    record_digest,
    resolve_packet,
)
from muninn.consumers.packets.schemas import schema_errors
from muninn.consumers.vault import x_pages
from muninn.enrich.interest_profile import load_interest_profile
from muninn.enrich.suitability import assess_suitability
from muninn.intake_records import assessment_record, current_revision_row, mask_title, mentions_locator
from muninn.revisions import record_revision
from tests.integration.intake_fakes import fake_client, judgement, write_profile
from tests.integration.intake_seed import add_revision, seed_link, seed_x_post

T0 = 1_790_000_000
MODEL = "claude-haiku-4-5-20251001"


# ── Finding 8: a live capture identical to an earlier archive capture ─


def test_live_capture_matching_an_earlier_archive_revision_is_current(fresh_db: sqlite3.Connection) -> None:
    conn = fresh_db
    conn.execute(
        "INSERT INTO bookmarks (bookmark_id, source, source_id, captured_at, title, url, content_visible, "
        "enrichment_source) VALUES (1, 'x_link', 'abc', ?, 't', 'https://example.com/p', 1, 'at_capture')",
        (T0,),
    )
    ref = "web:" + "a" * 64
    # Run 1: the live fetch failed (no revision), the archive returned text A.
    archive = record_revision(
        conn, bookmark_id=1, item_ref=ref, text="Hello world", coverage="full", retrieval_method="archive",
        scrape_pass="at_capture", extractor_name="e", extractor_version="1", retrieved_at=T0 + 100,
        archive_snapshot_at=T0 - 1000,
    )
    assert current_revision_row(conn, ref, 1) is None  # only a historical capture so far
    # Run 2: the live fetch returns the identical text: the key adds no row.
    conn.execute(
        "INSERT INTO scrape_results (bookmark_id, pass, fetched_at, scrape_status, content_text) "
        "VALUES (1, 'live', ?, 'ok', 'Hello world')",
        (T0 + 200,),
    )
    assert record_revision(
        conn, bookmark_id=1, item_ref=ref, text="Hello world", coverage="full", retrieval_method="live",
        scrape_pass="live", extractor_name="e", extractor_version="1", retrieved_at=T0 + 200,
    ) is None
    current = current_revision_row(conn, ref, 1)
    assert current is not None and current["revision_ulid"] == archive

    # A live row with no readable text never promotes an archive revision.
    conn.execute("UPDATE scrape_results SET content_text = NULL, scrape_status = 'failed' WHERE bookmark_id = 1")
    assert current_revision_row(conn, ref, 1) is None


# ── Finding 11: --prompt-version must belong to --task ───────────────


@pytest.mark.parametrize(
    ("args", "version"),
    [([], "suitability_v1"), (["--task", "suitability"], "per_bookmark_v1"), ([], "per_bookmark")],
)
def test_cli_prompt_version_must_match_the_task(db_path, tmp_path, args, version) -> None:
    with patch("muninn.enrich.pipeline.enrich_all") as enrich_all, \
         patch("muninn.enrich.suitability.assess_suitability") as assess:
        result = CliRunner().invoke(
            cli, ["--db", str(db_path), "enrich", *args, "--dry-run", "--prompt-version", version],
        )
    assert result.exit_code == 2 and "is not a" in result.output, result.output
    enrich_all.assert_not_called()
    assess.assert_not_called()


def test_library_entry_points_refuse_another_tasks_prompt(fresh_db, tmp_path) -> None:
    from muninn.enrich.pipeline import enrich_all
    from muninn.enrich.suitability import SuitabilityError

    client = MagicMock()
    with pytest.raises(ValueError, match="not a per_bookmark prompt"):
        enrich_all(fresh_db, client=client, qdrant=None, prompt_version="suitability_v1")
    profile = load_interest_profile(write_profile(tmp_path))
    with pytest.raises(SuitabilityError, match="not a suitability prompt"):
        assess_suitability(fresh_db, profile, client, model=MODEL, prompt_version="per_bookmark_v1")
    client.messages.create.assert_not_called()


# ── Finding 10: masking ignores case and scheme; withheld enrichment ──

REPO_LOCATOR = "https://github.com/mycorp/secret-repo"  # stored lowercase root


@pytest.mark.parametrize(
    "text",
    [
        "The author shares https://github.com/MyCorp/Secret-Repo, an internal tool.",
        "The author shares github.com/MyCorp/Secret-Repo, an internal tool.",
        "The author shares https://www.github.com/mycorp/secret-repo, an internal tool.",
        "The author shares https://github.com/MyCorp/Secret-Repo.git, an internal tool.",
        "The author shares HTTP://GITHUB.COM/MYCORP/SECRET-REPO/, an internal tool.",
    ],
)
def test_mask_hidden_links_ignores_case_scheme_and_www(text) -> None:
    masked = x_pages.mask_hidden_links(text, {REPO_LOCATOR})
    assert masked == f"The author shares {x_pages.HIDDEN_LINK_TEXT}, an internal tool."
    assert mentions_locator(text, {REPO_LOCATOR}) and not mentions_locator(masked, {REPO_LOCATOR})


def test_mask_does_not_touch_other_links() -> None:
    for text in ("https://github.com/mycorp/secret-repo-two", "https://notgithub.com/mycorp/secret-repo",
                 "https://github.com/other/secret-repo"):
        assert x_pages.mask_hidden_links(text, {REPO_LOCATOR}) == text
    assert mask_title("@a: see github.com/MyCorp/Sec…", {REPO_LOCATOR}) == "@a: see [hidden link]"


def _x_vault_with_stale_enrichment(db_path):
    from muninn.db import connect
    from tests import x_corpus as xc

    conn = connect(db_path)
    corpus = xc.seed_x_corpus(conn, hidden_at_ingest=False)
    secret = xc.HIDDEN_URL.replace("intranet.example", "Intranet.Example")  # the summary's own case
    conn.execute(
        "INSERT INTO enriched (bookmark_id, summary, tags, entities, key_quotes, content_type, language, "
        "enrichment_model, enrichment_prompt_version, content_hash, enriched_at) "
        "VALUES (?, ?, '[\"retrieval\"]', ?, ?, 'post', 'en', 'm', 'v', 'h', 1)",
        (corpus.post, f"Shares a draft plan at {secret}: the private roadmap covers Q3 hiring.",
         json.dumps(["Private roadmap"]), json.dumps([f"Draft plan: {secret}"])),
    )
    conn.commit()
    conn.close()
    return corpus, secret


def test_vault_withholds_enrichment_that_quotes_a_hidden_link(db_path, tmp_path) -> None:
    from muninn.consumers.vault.compiler import compile_vault

    _x_vault_with_stale_enrichment(db_path)
    out = tmp_path / "compiled"
    compile_vault(db_path=db_path, output_dir=out, personal_dir=None)
    everything = "\n".join(p.read_text() for p in (out / "wiki" / "bookmarks").glob("*.md"))
    assert "intranet.example" not in everything.lower()
    assert "Q3 hiring" not in everything and "Private roadmap" not in everything
    assert x_pages.ENRICHMENT_WITHHELD in everything


def test_withhold_enrichment_keeps_clean_output() -> None:
    urls = {REPO_LOCATOR}
    assert x_pages.withhold_enrichment(urls, "A toolkit.", ["q"], '["e"]') == ("A toolkit.", ["q"], '["e"]', False)
    assert x_pages.withhold_enrichment(urls, "A toolkit.", '["see github.com/MyCorp/secret-repo"]', '["e"]') == (
        None, "[]", "[]", True)


def test_reconcile_reindexes_a_point_that_carries_a_hidden_locator(db_path, tmp_path) -> None:
    qdrant_client = pytest.importorskip("qdrant_client")
    from muninn.vector.qdrant import ensure_collection, get_point_payloads, upsert_point
    from muninn.vector.embed import embed_document
    from muninn.vector.reconcile import point_payload, reconcile

    corpus, secret = _x_vault_with_stale_enrichment(db_path)
    from muninn.db import connect

    conn = connect(db_path)
    title = conn.execute("SELECT title FROM bookmarks WHERE bookmark_id = ?", (corpus.post,)).fetchone()[0]
    summary = conn.execute("SELECT summary FROM enriched WHERE bookmark_id = ?", (corpus.post,)).fetchone()[0]
    conn.close()
    client = qdrant_client.QdrantClient(":memory:")
    ensure_collection(client)
    # The point the enrich pipeline wrote before the link was hidden.
    upsert_point(client, corpus.post, embed_document(f"{title}\n{summary}\nretrieval"),
                 point_payload(corpus.post, title=title, summary=summary, tags=["retrieval"],
                               content_type="post", language="en"))
    stats = reconcile(db_path, client=client)
    assert stats["stale_in_qdrant"] == 1 and stats["refreshed"] == 1
    payload = get_point_payloads(client)[corpus.post]
    assert "intranet.example" not in json.dumps(payload).lower() and "Q3 hiring" not in json.dumps(payload)
    assert payload["summary"] == "" and payload["tags"] == ["retrieval"]
    assert reconcile(db_path, client=client)["stale_in_qdrant"] == 0  # converged
    client.close()


# ── Finding 9: the context digest re-keys an assessment ──────────────


def _post_with_link(conn, post_id="1900000000000000051"):
    post = seed_x_post(conn, post_id, "Thread on retrieval eval, notes linked.", captured_at=T0, published_at=T0 - 100)
    link = seed_link(conn, post, position=0, unwound_url="https://example.com/notes",
                     text="Hybrid retrieval improved recall at ten.", retrieved_at=T0 + 10)
    return post, link


def test_hiding_a_linked_item_leads_to_reassessment_and_reexport(fresh_db, tmp_path) -> None:
    conn = fresh_db
    post, link = _post_with_link(conn)
    profile = load_interest_profile(write_profile(tmp_path))
    ev = [{"item_id": link.item_id, "stance": "supports", "excerpt": "Hybrid retrieval improved recall at ten.",
           "note": "Measured result."}]
    assess_suitability(conn, profile, fake_client(default=judgement(evidence=ev)), model=MODEL, now=T0 + 100)
    out = tmp_path / "out"
    (r1,) = export_packets(conn, out, now=T0 + 150).written

    conn.execute("UPDATE bookmarks SET content_visible = 0 WHERE bookmark_id = ?", (link.bookmark_id,))
    conn.commit()
    assert [r for _, r in export_packets(conn, out, now=T0 + 200).withdrawn] == ["policy_hidden"]

    # What the model is shown changed (the link is gone), so this is a new key.
    run = assess_suitability(conn, profile, fake_client(default=judgement()), model=MODEL, now=T0 + 300)
    assert run.assessed == 1 and run.skipped_idempotent == 0
    rows = conn.execute("SELECT context_digest, context_json FROM assessments ORDER BY assessment_id").fetchall()
    assert len({r["context_digest"] for r in rows}) == 2
    for r in rows:
        assert r["context_digest"] == context_digest(json.loads(r["context_json"]))
    assert [e[0] for e in json.loads(rows[1]["context_json"])] == [post.item_id]

    (r2,) = export_packets(conn, out, now=T0 + 400).written
    assert (r2.packet_id, r2.revision) == (r1.packet_id, 2)
    packet = json.loads(r2.path.read_bytes())
    assert packet["previous_revision"] == {"ref": r1.ref, "digest": r1.digest}
    assert link.item_id not in json.dumps(packet)
    assert resolve_packet(out, r1.ref, r1.digest).status == "withdrawn"
    # An unchanged context is still idempotent.
    assert assess_suitability(conn, profile, fake_client(), model=MODEL, now=T0 + 500).skipped_idempotent == 1


def test_recapturing_a_linked_item_changes_the_context(fresh_db, tmp_path) -> None:
    conn = fresh_db
    _post, link = _post_with_link(conn)
    profile = load_interest_profile(write_profile(tmp_path))
    assess_suitability(conn, profile, fake_client(), model=MODEL, now=T0 + 100)
    add_revision(conn, link, "Hybrid retrieval improved recall at ten, revised.", retrieval_method="live",
                 retrieved_at=T0 + 200)
    run = assess_suitability(conn, profile, fake_client(), model=MODEL, now=T0 + 300)
    assert run.assessed == 1
    # Same subject revision, two contexts.
    assert tuple(conn.execute("SELECT COUNT(DISTINCT scrape_history_id), COUNT(*) FROM assessments").fetchone()) == (1, 2)


# ── Finding 2: a hidden child's locator inside a visible parent ───────

SECRET = "https://secret.example.org/private-doc"
SECRET_HOST = "secret.example.org"


def _packet_texts(out) -> str:
    return "\n".join(p.read_text() for p in sorted(out.rglob("*.json"))) if out.exists() else ""


def _calls(client) -> str:
    return "\n".join(c.kwargs["messages"][0]["content"] for c in client.messages.create.call_args_list)


def _hide(conn, item) -> None:
    conn.execute("UPDATE bookmarks SET content_visible = 0 WHERE bookmark_id = ?", (item.bookmark_id,))
    conn.commit()


def test_hidden_locator_in_the_current_revision_reaches_neither_model_nor_packet(fresh_db, tmp_path) -> None:
    conn = fresh_db
    post = seed_x_post(conn, "1900000000000000031", f"Great writeup {SECRET.replace('secret', 'Secret')} on retrieval eval",
                       captured_at=T0, published_at=T0 - 100)
    conn.execute("UPDATE bookmarks SET title = ? WHERE bookmark_id = ?",
                 (f"@synth_author_a: Great writeup {SECRET}", post.bookmark_id))
    child = seed_link(conn, post, position=0, unwound_url=SECRET, text="Retrieval eval method.",
                      title="Private doc", retrieved_at=T0 + 10)
    _hide(conn, child)
    profile = load_interest_profile(write_profile(tmp_path))
    client = fake_client(default=judgement(evidence=[
        {"item_id": post.item_id, "stance": "supports", "excerpt": "Great writeup [hidden link] on", "note": "n"},
    ]))
    run = assess_suitability(conn, profile, client, model=MODEL, now=T0 + 100)
    sent = _calls(client).lower()
    assert run.assessed == 1 and SECRET_HOST not in sent and x_pages.HIDDEN_LINK_TEXT in sent
    assert run.evidence_dropped == 1  # it quotes the masked text, not the stored revision

    out = tmp_path / "out"
    export = export_packets(conn, out, now=T0 + 400)
    assert export.written == [] and not export.invalid
    assert export.skipped == [(post.item_id, "primary assessment's revision quotes a hidden item's locator; "
                                             "re-sync and reassess")]
    assert SECRET_HOST not in _packet_texts(out).lower()


def test_revisions_and_assessments_from_before_the_hide_are_left_out(fresh_db, tmp_path) -> None:
    conn = fresh_db
    post = seed_x_post(conn, "1900000000000000031", f"Great writeup {SECRET} on retrieval eval",
                       captured_at=T0, published_at=T0 - 100)
    child = seed_link(conn, post, position=0, unwound_url=SECRET, text="Retrieval eval method and results.",
                      retrieved_at=T0 + 10)
    profile = load_interest_profile(write_profile(tmp_path))
    client = fake_client(default=judgement(rationale=f"The linked doc at {SECRET} reports results."))
    assess_suitability(conn, profile, client, model=MODEL, now=T0 + 100)
    # Policy hides the domain; the next sync records the masked post text.
    _hide(conn, child)
    add_revision(conn, post, "Great writeup [hidden link] on retrieval eval", retrieval_method="x_api",
                 retrieved_at=T0 + 200, published_at=T0 - 100, observed_at=T0)
    second = fake_client(default=judgement())
    assert assess_suitability(conn, profile, second, model=MODEL, now=T0 + 300).assessed == 1
    assert SECRET_HOST not in _calls(second).lower()

    out = tmp_path / "out"
    (written,) = export_packets(conn, out, now=T0 + 400).written
    packet = json.loads(written.path.read_bytes())
    assert SECRET_HOST not in _packet_texts(out).lower()
    assert [r["text"] for r in packet["content_revisions"]] == ["Great writeup [hidden link] on retrieval eval"]
    assert len(packet["assessments"]) == 1  # the pre-hide assessment saw the hidden doc


def test_pre_hide_assessment_is_left_out_even_without_a_locator(fresh_db, tmp_path) -> None:
    """Its context included the since-hidden item, so its rationale may paraphrase it."""
    conn = fresh_db
    post, link = _post_with_link(conn)
    profile = load_interest_profile(write_profile(tmp_path))
    assess_suitability(conn, profile, fake_client(default=judgement(rationale="The notes say recall doubled.")),
                       model=MODEL, now=T0 + 100)
    _hide(conn, link)
    from tests.integration.intake_fakes import PROFILE_V2

    v2 = load_interest_profile(write_profile(tmp_path, PROFILE_V2, "v2.yml"))
    assess_suitability(conn, v2, fake_client(default=judgement(rationale="Post only.")), model=MODEL, now=T0 + 200)
    (written,) = export_packets(conn, tmp_path / "out", now=T0 + 300).written
    packet = json.loads(written.path.read_bytes())
    assert [a["rationale"] for a in packet["assessments"]] == ["Post only."]
    assert "recall doubled" not in json.dumps(packet)


def test_packet_titles_mask_a_hidden_locator(fresh_db, tmp_path) -> None:
    conn = fresh_db
    post = seed_x_post(conn, "1900000000000000032", "Masked text [hidden link] here.", captured_at=T0,
                       published_at=T0 - 100)
    conn.execute("UPDATE bookmarks SET title = ? WHERE bookmark_id = ?",
                 ("@synth_author_a: Masked text https://secret.example.org/priv…", post.bookmark_id))
    _hide(conn, seed_link(conn, post, position=0, unwound_url=SECRET, text="x", retrieved_at=T0 + 10))
    profile = load_interest_profile(write_profile(tmp_path))
    assess_suitability(conn, profile, fake_client(), model=MODEL, now=T0 + 100)
    (written,) = export_packets(conn, tmp_path / "out", now=T0 + 200).written
    packet = json.loads(written.path.read_bytes())
    assert packet["items"][0]["title"] == "@synth_author_a: Masked text [hidden link]"
    assert SECRET_HOST not in _packet_texts(tmp_path / "out")


# ── Finding 17: prior_assessment resolves inside the packet ──────────


def test_prior_assessment_is_omitted_when_its_target_is_left_out(fresh_db, tmp_path) -> None:
    conn = fresh_db
    _post, link = _post_with_link(conn)
    profile = load_interest_profile(write_profile(tmp_path))
    ev = [{"item_id": link.item_id, "stance": "supports", "excerpt": "Hybrid retrieval improved recall at ten.",
           "note": "Measured result."}]
    assess_suitability(conn, profile, fake_client(default=judgement(evidence=ev)), model=MODEL, now=T0 + 100)
    _hide(conn, link)  # the first assessment cites it: left out of every later packet
    assess_suitability(conn, profile, fake_client(), model=MODEL, now=T0 + 200)
    first, second = conn.execute("SELECT * FROM assessments ORDER BY assessment_id").fetchall()
    stored = assessment_record(conn, second)
    assert stored["prior_assessment"]["ref"] == first["assessment_ulid"]

    (written,) = export_packets(conn, tmp_path / "out", now=T0 + 300).written
    packet = json.loads(written.path.read_bytes())
    (only,) = packet["assessments"]
    assert only["assessment_id"] == stored["assessment_id"] and "prior_assessment" not in only
    assert packet["primary_assessment"] == {"ref": only["assessment_id"], "digest": record_digest(only)}
    assert packet_integrity_errors(packet) == [] and schema_errors("evidence-packet", packet) == []

    # The integrity check rejects a prior that does not resolve inside the packet.
    only["prior_assessment"] = stored["prior_assessment"]
    packet["primary_assessment"]["digest"] = record_digest(only)
    assert "assessments[0].prior_assessment is not in assessments" in packet_integrity_errors(packet)


# ── Finding 4: content its source deleted is withdrawn, not re-exported ─

DELETED_MARKER = "DELETEDMARKER"


def test_source_deleted_post_withdraws_its_packet_and_its_text(fresh_db, tmp_path) -> None:
    conn = fresh_db
    post, link = _post_with_link(conn)
    add_revision(conn, post, f"Original text {DELETED_MARKER} about retrieval eval, older.", retrieval_method="x_api",
                 retrieved_at=T0 + 1, published_at=T0 - 100, observed_at=T0)  # an older text, never exported
    add_revision(conn, post, f"Original text {DELETED_MARKER} about retrieval eval.", retrieval_method="x_api",
                 retrieved_at=T0 + 2, published_at=T0 - 100, observed_at=T0)
    profile = load_interest_profile(write_profile(tmp_path))
    assess_suitability(conn, profile, fake_client(), model=MODEL, now=T0 + 100)
    out = tmp_path / "out"
    (r1,) = export_packets(conn, out, now=T0 + 150).written

    # The author deletes the post: the next sync records a 'deleted' revision.
    add_revision(conn, post, "", coverage="deleted", retrieval_method="x_api", retrieved_at=T0 + 200,
                 published_at=T0 - 100, observed_at=T0)
    run = export_packets(conn, out, now=T0 + 250)
    assert [(p.ref, reason) for p, reason in run.withdrawn] == [(r1.ref, "source_deleted")]
    assert run.written == [] and run.unchanged == []
    texts = conn.execute(
        "SELECT coverage, content_text, withdrawal_reason FROM scrape_history WHERE item_ref = ? ORDER BY scrape_history_id",
        (post.item_id,),
    ).fetchall()
    # Every readable revision of the post is withdrawn, not only the exported one.
    assert [(r["coverage"], r["content_text"], r["withdrawal_reason"]) for r in texts] == [
        ("full", None, "source_deleted"), ("full", None, "source_deleted"),
        ("full", None, "source_deleted"), ("deleted", None, None)]
    assert run.revisions_withdrawn == 3
    # The linked page still exists, so its content stays.
    assert conn.execute("SELECT content_text FROM scrape_history WHERE item_ref = ?", (link.item_id,)).fetchone()[0]

    # Reassessed on the deletion (the linked page is still readable): exported
    # again without the deleted text, and not withdrawn again by the next sweep.
    reassess = assess_suitability(conn, profile, fake_client(default=judgement("needs_evidence")), model=MODEL,
                                  now=T0 + 300)
    assert reassess.assessed == 1
    (r2,) = export_packets(conn, out, dispositions=("needs_evidence",), now=T0 + 350).written
    packet = json.loads(r2.path.read_bytes())
    assert (r2.revision, packet["previous_revision"]["ref"]) == (2, r1.ref)
    post_revs = [r for r in packet["content_revisions"] if r["item_id"] == post.item_id]
    assert [r["coverage"] for r in post_revs] == ["deleted"]
    assert [a["coverage"] for a in packet["assessments"] if a["item_id"] == post.item_id] == ["deleted"]
    assert DELETED_MARKER not in _packet_texts(out)
    assert export_packets(conn, out, dispositions=("needs_evidence",), now=T0 + 400).withdrawn == []


def test_assessments_of_a_deleted_items_old_content_are_never_exported(fresh_db, tmp_path) -> None:
    conn = fresh_db
    post, link = _post_with_link(conn)
    profile = load_interest_profile(write_profile(tmp_path))
    assess_suitability(conn, profile, fake_client(default=judgement(rationale="The notes show recall doubled.")),
                       model=MODEL, now=T0 + 100)
    assess_suitability(conn, profile, fake_client(), model=MODEL, sources=("x_link",), now=T0 + 100)
    # The linked page now answers 404 (never exported, so nothing withdraws its old text).
    add_revision(conn, link, "", coverage="deleted", retrieval_method="live", retrieved_at=T0 + 200, http_status=404)
    run = export_packets(conn, tmp_path / "out", now=T0 + 250)
    assert run.written == [] and run.skipped == [
        (post.item_id, "primary assessment saw an item that is now hidden or deleted; reassess")]

    assess_suitability(conn, profile, fake_client(default=judgement(rationale="Post only.")), model=MODEL, now=T0 + 300)
    (written,) = export_packets(conn, tmp_path / "out", now=T0 + 350).written
    packet = json.loads(written.path.read_bytes())
    assert [r["coverage"] for r in packet["content_revisions"] if r["item_id"] == link.item_id] == ["deleted"]
    assert not any(a["item_id"] == link.item_id for a in packet["assessments"])  # made on the old text
    assert [a["rationale"] for a in packet["assessments"]] == ["Post only."]
    assert "Hybrid retrieval" not in json.dumps(packet)


# ── Finding 16: a dry run plans what the real run would do ───────────


def test_dry_run_applies_due_withdrawals_to_its_plan(fresh_db, tmp_path) -> None:
    conn = fresh_db
    post = seed_x_post(conn, "1900000000000000061", "Thread on retrieval eval.", captured_at=T0, published_at=T0 - 100)
    profile = load_interest_profile(write_profile(tmp_path))
    assess_suitability(conn, profile, fake_client(), model=MODEL, now=T0 + 100)
    # A link recorded after the assessment: in the packet, not in the model's context.
    link = seed_link(conn, post, position=0, unwound_url="https://example.com/later",
                     text="A later page.", retrieved_at=T0 + 120)
    out = tmp_path / "out"
    (r1,) = export_packets(conn, out, now=T0 + 150).written
    _hide(conn, link)

    dry = export_packets(conn, out, dry_run=True, now=T0 + 200)
    real_reason = f"latest revision {r1.ref} is withdrawn"
    assert [d.ref for d in dry.would_withdraw] == [r1.ref]
    assert dry.planned == [] and dry.skipped == [(post.item_id, real_reason)]
    assert conn.execute("SELECT withdrawn_at FROM evidence_packets").fetchone()[0] is None  # nothing applied

    real = export_packets(conn, out, now=T0 + 200)
    assert real.written == [] and real.skipped == dry.skipped


# ── Finding 5: rows sharing one item are assessed once ───────────────


def _two_browser_rows_one_page(conn) -> None:
    from muninn.consumers.packets import link_item_id
    from tests.integration.intake_seed import Seeded

    url = "https://example.com/article"
    ids = [
        conn.execute(
            "INSERT INTO bookmarks (source, source_id, captured_at, title, url, domain, content_visible) "
            "VALUES ('bookmarks_html', ?, ?, 'Synthetic', ?, 'example.com', 1)",
            (f"sid{n}", T0 + n, url),
        ).lastrowid
        for n in range(2)
    ]
    conn.commit()
    add_revision(conn, Seeded(ids[0], link_item_id(url)), "Synthetic retrieval article text.",
                 retrieval_method="live", retrieved_at=T0 + 10)


def _count(conn, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_rows_sharing_one_revision_make_one_paid_call(fresh_db, tmp_path) -> None:
    conn = fresh_db
    _two_browser_rows_one_page(conn)
    profile = load_interest_profile(write_profile(tmp_path))
    client = fake_client()
    run = assess_suitability(conn, profile, client, model=MODEL, sources=("bookmarks_html",), now=T0 + 100)
    assert client.messages.create.call_count == 1
    assert [o.status for o in run.outcomes] == ["skipped_idempotent", "assessed"]
    assert run.assessed == 1 and run.skipped_idempotent == 1 and run.planned == 1
    assert (_count(conn, "usage_records"), _count(conn, "assessments")) == (1, 1)


def test_a_write_conflict_still_records_the_paid_call(fresh_db, tmp_path, monkeypatch) -> None:
    from muninn.enrich import suitability

    conn = fresh_db
    _post_with_link(conn)
    seed_x_post(conn, "1900000000000000071", "Second synthetic post.", captured_at=T0 - 50, published_at=T0 - 100)
    profile = load_interest_profile(write_profile(tmp_path))
    assess_suitability(conn, profile, fake_client(), model=MODEL, now=T0 + 100)
    assert (_count(conn, "usage_records"), _count(conn, "assessments")) == (2, 2)
    # As if another run wrote the same keys between our check and our insert.
    monkeypatch.setattr(suitability, "_assessment_exists", lambda *a, **k: False)
    client = fake_client()
    run = assess_suitability(conn, profile, client, model=MODEL, now=T0 + 200)
    assert client.messages.create.call_count == 2  # the conflict did not abandon the rest of the plan
    assert [o.status for o in run.outcomes] == ["skipped_idempotent", "skipped_idempotent"]
    assert all(o.cost_micro_usd > 0 and "usage recorded" in o.detail for o in run.outcomes)
    assert (_count(conn, "usage_records"), _count(conn, "assessments")) == (4, 2)


# ── Finding 15: retries, failed calls and the token bound ─────────────


def test_the_client_made_for_suitability_never_retries(monkeypatch) -> None:
    import sys
    from types import SimpleNamespace

    from muninn.enrich import suitability

    made = {}
    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(Anthropic=lambda **kw: made.update(kw) or "client"))
    assert suitability._make_client() == "client" and made == {"max_retries": 0}


class _StatusError(Exception):
    status_code = 529


class _ErrorWithUsage(Exception):
    status_code = 500
    body = {"usage": {"input_tokens": 1000, "output_tokens": 0}}


def test_failed_calls_are_recorded_or_reserved(fresh_db, tmp_path) -> None:
    from muninn.enrich.pricing import PRICES, cost_micro_usd

    conn = fresh_db
    for n, post_id in enumerate(("1900000000000000081", "1900000000000000082", "1900000000000000083")):
        seed_x_post(conn, post_id, f"Synthetic post {n}.", captured_at=T0 - n, published_at=T0 - 100)
    a, b, c = (f"x:post:{p}" for p in ("1900000000000000081", "1900000000000000082", "1900000000000000083"))
    profile = load_interest_profile(write_profile(tmp_path))
    client = fake_client({a: _ErrorWithUsage("billed"), b: _StatusError("overloaded"), c: TimeoutError("no answer")})
    run = assess_suitability(conn, profile, client, model=MODEL, now=T0 + 100)
    assert run.api_errors == 3 and run.assessed == 0
    billed = cost_micro_usd(PRICES[MODEL], input_tokens=1000, output_tokens=0)
    rows = conn.execute("SELECT cost_micro_usd, input_token_count, assessment_id FROM usage_records").fetchall()
    assert [tuple(r) for r in rows] == [(billed, 1000, None)]  # only the error that reports usage
    assert run.spent_micro_usd == billed
    timeout = next(o for o in run.outcomes if o.item_id == c)
    planned = assess_suitability(conn, profile, client, model=MODEL, dry_run=True)
    worst_c = next(o.cost_micro_usd for o in planned.outcomes if o.item_id == c)
    assert run.reserved_micro_usd == worst_c and timeout.status == "api_error"
    assert run.summary()["reserved"] != "$0.0000"


def test_reserved_worst_cases_count_against_the_budget(fresh_db, tmp_path) -> None:
    conn = fresh_db
    for n in range(3):
        seed_x_post(conn, f"190000000000000009{n}", f"Synthetic post {n}.", captured_at=T0 - n, published_at=T0 - 100)
    profile = load_interest_profile(write_profile(tmp_path))
    worst = [o.cost_micro_usd for o in assess_suitability(conn, profile, fake_client(), model=MODEL,
                                                          dry_run=True).outcomes]
    client = fake_client(default=TimeoutError("no answer"))
    run = assess_suitability(conn, profile, client, model=MODEL, max_cost_micro_usd=worst[0] + worst[1] + 1,
                             now=T0)
    assert client.messages.create.call_count == 2 and run.stopped == "budget"


def test_worst_case_bounds_tokens_by_utf8_bytes(fresh_db, tmp_path) -> None:
    from muninn.enrich.pricing import PRICES, worst_case_micro_usd
    from muninn.enrich.suitability import MAX_OUTPUT_TOKENS

    conn = fresh_db
    text = "检索评估的合成说明。" * 200 + "🔍" * 300  # 3 and 4 bytes per character
    seed_x_post(conn, "1900000000000000099", text, captured_at=T0, published_at=T0 - 100)
    profile = load_interest_profile(write_profile(tmp_path))
    run = assess_suitability(conn, profile, fake_client(), model=MODEL, dry_run=True)
    (planned,) = run.outcomes
    floor = worst_case_micro_usd(PRICES[MODEL], input_tokens=len(text.encode("utf-8")),
                                 max_output_tokens=MAX_OUTPUT_TOKENS)
    assert planned.cost_micro_usd > floor
