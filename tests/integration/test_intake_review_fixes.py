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
from muninn.consumers.packets import context_digest, export_packets, resolve_packet
from muninn.consumers.vault import x_pages
from muninn.enrich.interest_profile import load_interest_profile
from muninn.enrich.suitability import assess_suitability
from muninn.intake_records import current_revision_row, mask_title, mentions_locator
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
