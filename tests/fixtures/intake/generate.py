"""Regenerate the muninn-research-intake 0.1 fixtures.

ALL DATA HERE IS SYNTHETIC: invented IDs, handles, hosts and text, written
for the contract tests. Nothing is recorded from X, GitHub or the
operator's corpus. Hosts use reserved example domains; X IDs are made up.

Run from the repository root::

    PYTHONPATH=src python tests/fixtures/intake/generate.py

then refresh the release manifest (see the contract's Releases section).
The fixtures are interlinked by digest, so edit this script, not the JSON.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from muninn.consumers.packets import (
    canonical_json,
    content_digest,
    link_item_id,
    make_tombstone,
    new_ulid,
    normalize_text,
    record_digest,
    x_post_item_id,
)
from muninn.sanitize import sanitize_url

HERE = Path(__file__).resolve().parent
PROVENANCE = "synthetic: invented for muninn-research-intake 0.1.0-draft tests; not recorded data"
CANARY = "MUNINN-CANARY-7c1e5f0b-not-a-real-secret"
CONTRACT_VERSION = "0.1.0-draft"


def iso(s: str) -> str:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ulid(label: str, when: str) -> str:
    ms = int(datetime.strptime(when, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp() * 1000)
    return new_ulid(ms, hashlib.sha256(label.encode()).digest()[:10])


def write(record_type: str, name: str, expect: str, record: dict, covers: list[str], why: str,
          fails_at: str | None = None, fails_with: str | None = None) -> None:
    path = HERE / record_type / f"{name}.{expect}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {
        "synthetic": True,
        "provenance": PROVENANCE,
        "schema": record_type,
        "expect": expect,
        "covers": covers,
        "why": why,
    }
    assert (expect == "invalid") == (fails_at is not None), name
    if fails_at is not None:
        meta["fails_at"] = fails_at  # JSON path of the error that must be raised ("" = record root)
    if fails_with is not None:
        meta["fails_with"] = fails_with  # substring of that error's message
    doc = {"fixture": meta, "record": record}
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# ── Raw synthetic inputs (the canary lives only here and in invalid fixtures) ──

RAW_ENTITIES = {
    "web": {
        "url": "https://t.co/SynthW0001",
        "expanded_url": f"https://example.com/articles/synthetic-retrieval-notes?utm_source=twitter&access_token={CANARY}",
        "unwound_url": f"https://example.com/articles/synthetic-retrieval-notes?access_token={CANARY}&utm_source=twitter",
        "status": 200,
    },
    "repo": {
        "url": "https://t.co/SynthR0002",
        "expanded_url": "https://github.com/Synthetic-Org/synthetic-repo",
        "unwound_url": None,
        "status": None,
    },
    "blocked": {
        "url": "https://t.co/SynthB0003",
        "expanded_url": None,
        "unwound_url": None,
        "status": None,
    },
}
BLOCKED_RESOLVED = "https://paywalled.example.net/story/42?session=" + CANARY


def locator(raw: str) -> tuple[str, dict]:
    res = sanitize_url(raw)
    assert res.sanitized_url, raw
    return res.sanitized_url, {
        "param_names": res.redacted_param_names,
        "path_redacted": res.path_redacted,
        "userinfo_redacted": res.userinfo_redacted,
    }


def main() -> None:
    (HERE / "canary").mkdir(exist_ok=True)
    (HERE / "canary" / "raw-inputs.json").write_text(json.dumps({
        "fixture": {
            "synthetic": True,
            "provenance": PROVENANCE,
            "why": "Raw connector-side inputs carrying a canary secret. The canary must never appear in any valid export fixture: sanitize_url removes it before any locator is stored.",
        },
        "canary": CANARY,
        "token_response": {"token_type": "bearer", "access_token": CANARY, "refresh_token": CANARY},
        "url_entities": RAW_ENTITIES,
        "blocked_resolved_url": BLOCKED_RESOLVED,
    }, indent=2) + "\n", encoding="utf-8")

    # ── Items ────────────────────────────────────────────────────────
    post_a_id, post_q_id = "1900000000000000001", "1900000000000000002"
    A, Q = x_post_item_id(post_a_id), x_post_item_id(post_q_id)
    w_loc, w_red = locator(RAW_ENTITIES["web"]["unwound_url"])
    r_loc, r_red = locator(RAW_ENTITIES["repo"]["expanded_url"])
    b_loc, b_red = locator(RAW_ENTITIES["blocked"]["url"])  # only the t.co form survived
    b_final, _ = locator(BLOCKED_RESOLVED)
    W, R, B = link_item_id(w_loc), link_item_id(r_loc), link_item_id(b_loc)
    no_red = {"param_names": [], "path_redacted": False, "userinfo_redacted": False}

    item_a = {
        "record_type": "source-item", "item_id": A, "kind": "x_post",
        "locator": f"https://x.com/synth_author_a/status/{post_a_id}",
        "title": None, "published_at": iso("2026-09-30 14:05:00"), "language": "en",
        "author": {"platform_id": "1900000000000000901", "handle": "synth_author_a", "display_name": "Synthetic Author A"},
        "x_post": {"conversation_id": post_a_id, "is_long_post": True, "has_media": False},
        "redaction": no_red,
    }
    item_q = {
        "record_type": "source-item", "item_id": Q, "kind": "x_post",
        "locator": f"https://x.com/synth_author_b/status/{post_q_id}",
        "title": None, "published_at": iso("2026-09-12 09:30:00"), "language": "en",
        "author": {"platform_id": "1900000000000000902", "handle": "synth_author_b"},
        "x_post": {"conversation_id": post_q_id, "is_long_post": False},
        "redaction": no_red,
    }
    item_w = {
        "record_type": "source-item", "item_id": W, "kind": "web_page", "locator": w_loc,
        "title": "Synthetic notes on retrieval evaluation", "published_at": iso("2026-09-28 00:00:00"),
        "redaction": w_red,
    }
    item_r = {
        "record_type": "source-item", "item_id": R, "kind": "github_repo", "locator": r_loc,
        "title": "synthetic-org/synthetic-repo", "published_at": None,
        "repository": {"owner": "synthetic-org", "name": "synthetic-repo"},
        "redaction": r_red,
    }
    item_b = {
        "record_type": "source-item", "item_id": B, "kind": "web_page", "locator": b_loc,
        "title": None, "published_at": None, "redaction": b_red,
    }
    item_art = {
        "record_type": "source-item", "item_id": "x:article:1900000000000000501", "kind": "x_article",
        "locator": "https://x.com/i/article/1900000000000000501",
        "title": "A synthetic long-form article", "published_at": iso("2026-09-30 14:00:00"),
        "author": {"platform_id": "1900000000000000901", "handle": "synth_author_a"},
        "redaction": no_red,
    }
    for name, rec, cov in [
        ("x-post", item_a, ["x post with author and long-post flag"]),
        ("web-page-sanitized", item_w, ["web page whose raw locator carried the canary; sanitized locator and redaction names only", "canary"]),
        ("github-repo", item_r, ["github:repo identity lowercased from a mixed-case locator"]),
        ("blocked-web-page", item_b, ["linked page later captured as blocked"]),
        ("x-article", item_art, ["proposed x:article identity (OPEN: RI-02a article body)"]),
    ]:
        write("source-item", name, "valid", rec, cov, "valid source item")

    bad = copy.deepcopy(item_a); bad["item_id"] = W
    write("source-item", "kind-id-mismatch", "invalid", bad, ["identity rules"], "kind x_post with a web: item_id", fails_at='item_id')
    bad = copy.deepcopy(item_w); bad["locator"] = RAW_ENTITIES["web"]["unwound_url"]
    write("source-item", "unsanitized-locator", "invalid", bad, ["canary"], "locator still carries an access_token parameter: it was never passed through sanitize_url", fails_at='locator')
    bad = copy.deepcopy(item_r); bad["internal_bookmark_id"] = 17
    write("source-item", "extra-property", "invalid", bad, ["additionalProperties false"], "internal database ids are not part of the contract", fails_at='', fails_with='internal_bookmark_id')

    # ── Item links ───────────────────────────────────────────────────
    seen = iso("2026-10-20 08:00:00")
    links = [
        {"record_type": "item-link", "parent_item_id": A, "child_item_id": W, "relation": "links_to", "position": 0, "locator_resolution": "unwound", "entity_status": 200, "first_seen_at": seen},
        {"record_type": "item-link", "parent_item_id": A, "child_item_id": R, "relation": "links_to", "position": 1, "locator_resolution": "expanded", "entity_status": None, "first_seen_at": seen},
        {"record_type": "item-link", "parent_item_id": A, "child_item_id": B, "relation": "links_to", "position": 2, "locator_resolution": "short", "first_seen_at": seen},
        {"record_type": "item-link", "parent_item_id": A, "child_item_id": Q, "relation": "quotes", "position": 0, "locator_resolution": "api_reference", "first_seen_at": seen},
    ]
    write("item-link", "links-to-unwound", "valid", links[0], ["URL entity resolved from unwound_url"], "valid link")
    write("item-link", "quotes", "valid", links[3], ["quoted post referenced by id"], "valid link")
    bad = copy.deepcopy(links[3]); bad["locator_resolution"] = "unwound"
    write("item-link", "quote-with-url-resolution", "invalid", bad, ["relation rules"], "a quoted post is referenced by id, so its resolution must be api_reference", fails_at='locator_resolution')
    bad = copy.deepcopy(links[0]); bad["position"] = -1
    write("item-link", "negative-position", "invalid", bad, ["position"], "position is 0-based and non-negative", fails_at='position')

    # ── Observations ─────────────────────────────────────────────────
    run1 = ulid("sync-run-1", "2026-10-20T08:00:00Z")
    obs_a = {"record_type": "bookmark-observation", "item_id": A, "source": "x", "observed_at": seen,
             "observed_at_basis": "first_seen_by_sync", "last_observed_at": iso("2026-10-21 06:00:00"), "sync_run_id": run1}
    obs_browser = {"record_type": "bookmark-observation", "item_id": W, "source": "bookmarks_html",
                   "observed_at": iso("2019-03-04 21:15:00"), "observed_at_basis": "source_reported"}
    write("bookmark-observation", "x-first-seen", "valid", obs_a, ["X has no bookmark time; observation is the first sync"], "valid observation")
    write("bookmark-observation", "browser-add-date", "valid", obs_browser, ["browser ADD_DATE is source-reported"], "valid observation")
    bad = copy.deepcopy(obs_a); bad["observed_at"] = "2026-10-20T10:00:00+02:00"
    write("bookmark-observation", "non-utc-time", "invalid", bad, ["ISO-8601 UTC"], "export times are UTC with a Z suffix", fails_at='observed_at')
    bad = copy.deepcopy(obs_a); del bad["observed_at_basis"]
    write("bookmark-observation", "missing-basis", "invalid", bad, ["observation basis"], "the basis of the observation time is required", fails_at='', fails_with='observed_at_basis')

    # ── Content revisions ────────────────────────────────────────────
    def rev(label, item, text, coverage, method, retrieved, *, extractor, pass_=None, http=200,
            published=None, observed=None, snapshot=None, include_text=True, final=None):
        t = normalize_text(text)
        r = {
            "record_type": "content-revision", "revision_id": ulid(label, retrieved), "item_id": item,
            "content_sha256": content_digest(t), "normalization": "muninn-text/1", "char_count": len(t),
            "coverage": coverage, "retrieval_method": method,
            "freshness": "historical" if method == "archive" else "current",
            "extractor": extractor, "http_status": http, "published_at": published,
            "retrieved_at": retrieved, "observed_at": observed,
        }
        if pass_:
            r["scrape_pass"] = pass_
        if snapshot:
            r["archive_snapshot_at"] = snapshot
        if final:
            r["final_locator"] = final
        if include_text:
            r["text"] = t
        return r

    x_ext = {"name": "muninn.ingest.x", "version": "0.1.0"}
    web_ext = {"name": "muninn.scrape.extract", "version": "selectolax-0.4.13"}
    rev_a = rev("rev-a", A, "Synthetic post: a short thread on evaluating retrieval over small personal corpora.\n\nNotes https://example.com/articles/synthetic-retrieval-notes and code https://github.com/synthetic-org/synthetic-repo",
                "full", "x_api", iso("2026-10-20 08:00:05"), extractor=x_ext, published=item_a["published_at"], observed=seen)
    rev_q = rev("rev-q", Q, "Synthetic quoted post: hybrid search beat dense-only on our toy benchmark.",
                "full", "x_api", iso("2026-10-20 08:00:05"), extractor=x_ext, published=item_q["published_at"], observed=None)
    w1_text = ("Synthetic notes on retrieval evaluation.\n\nWe compare BM25, dense and hybrid retrieval on a "
               "ten-thousand-document synthetic corpus. Hybrid retrieval improved recall at ten by a wide margin.\n\n"
               "Method: fixed query set, judged by two annotators.")
    w2_text = w1_text.replace("by a wide margin", "by a modest margin") + "\n\nUpdate: the margin shrank after a tokenizer fix."
    rev_w1 = rev("rev-w1", W, w1_text, "full", "live", iso("2026-10-20 08:03:10"), extractor=web_ext, pass_="live",
                 published=item_w["published_at"], observed=seen)
    rev_w2 = rev("rev-w2", W, w2_text, "full", "live", iso("2026-10-21 06:02:00"), extractor=web_ext, pass_="live",
                 published=item_w["published_at"], observed=seen)
    rev_w_arch = rev("rev-w-archive", W, "Synthetic notes on retrieval evaluation (draft).\n\nResults pending.",
                     "full", "archive", iso("2026-10-20 08:04:00"), extractor=web_ext, pass_="recent_archive",
                     published=item_w["published_at"], observed=seen, snapshot=iso("2026-09-29 00:00:00"))
    rev_r = rev("rev-r", R, "synthetic-repo\n\nA synthetic hybrid retrieval toolkit. Installation: see docs.",
                "partial", "live", iso("2026-10-20 08:03:30"), extractor=web_ext, pass_="live", published=None, observed=seen)
    rev_b = rev("rev-b", B, "", "blocked", "live", iso("2026-10-20 08:03:50"), extractor=web_ext, pass_="live",
                http=403, observed=seen, include_text=False, final=b_final)
    assert rev_w1["content_sha256"] != rev_w2["content_sha256"]

    write("content-revision", "x-post-api", "valid", rev_a, ["post text delivered by the X API (retrieval_method x_api)"], "valid revision")
    write("content-revision", "web-live-r1", "valid", rev_w1, ["first live revision of a linked page"], "valid revision")
    write("content-revision", "web-live-r2-changed", "valid", rev_w2, ["changed-content revision: same item, new content_sha256, new revision_id"], "valid revision")
    write("content-revision", "web-archive-historical", "valid", rev_w_arch, ["archive capture labelled historical"], "valid revision")
    write("content-revision", "repo-partial", "valid", rev_r, ["partial capture"], "valid revision")
    write("content-revision", "blocked", "valid", rev_b, ["blocked capture: empty-content digest, char_count 0", "final_locator sanitized after a t.co redirect (canary dropped)"], "valid revision")
    bad = copy.deepcopy(rev_w_arch); bad["freshness"] = "current"
    write("content-revision", "archive-marked-current", "invalid", bad, ["freshness"], "archive captures are historical, never current evidence", fails_at='freshness')
    bad = copy.deepcopy(rev_b); bad["char_count"] = 12; bad["content_sha256"] = content_digest("Please log in")
    write("content-revision", "blocked-with-content", "invalid", bad, ["coverage rules"], "a blocked capture carries no content (an auth-wall page is not the item's content)", fails_at='char_count')
    bad = copy.deepcopy(rev_w1); bad["retrieval_method"] = "playwright"
    write("content-revision", "unknown-retrieval-method", "invalid", bad, ["retrieval_method enum"], "retrieval method is x_api, live or archive", fails_at='retrieval_method')

    # ── Interest profiles ────────────────────────────────────────────
    prof_v1 = {
        "record_type": "interest-profile", "profile_id": "operator-research", "label": "Synthetic research interests",
        "version_label": "v1",
        "interests": [
            {"interest_id": "retrieval-eval", "label": "Retrieval evaluation", "description": "Benchmarks and methods for evaluating search over small corpora.", "weight": 80},
            {"interest_id": "sqlite-tooling", "label": "SQLite tooling", "description": "Migrations, FTS5 and embedded analytics.", "weight": 60},
        ],
        "exclusions": [{"exclusion_id": "hype", "description": "Announcements without method or data."}],
    }
    prof_v2 = copy.deepcopy(prof_v1)
    prof_v2["version_label"] = "v2"
    prof_v2["interests"][0]["weight"] = 90
    prof_v2["interests"].append({"interest_id": "agent-harness", "label": "Agent harnesses", "description": "Tooling that runs coding agents under supervision.", "weight": 70})
    pv1, pv2 = record_digest(prof_v1), record_digest(prof_v2)
    assert pv1 != pv2
    write("interest-profile", "profile-v1", "valid", prof_v1, ["profile revision = sha256 of canonical JSON"], "valid profile")
    write("interest-profile", "profile-v2-changed", "valid", prof_v2, ["changed profile: new digest"], "valid profile")
    bad = copy.deepcopy(prof_v1); bad["interests"][0]["weight"] = 0.8
    write("interest-profile", "float-weight", "invalid", bad, ["integers only"], "floats are outside the canonical-JSON subset; weights are integers 0-100", fails_at='interests/0/weight', fails_with='integer')
    bad = copy.deepcopy(prof_v1); bad["interests"] = []
    write("interest-profile", "no-interests", "invalid", bad, ["interests"], "a profile needs at least one interest", fails_at='interests')

    # ── Assessments ──────────────────────────────────────────────────
    def assess(label, revision, profile, profile_digest, when, disposition, relevance, confidence, rationale,
               matched, questions, prior=None):
        a = {
            "record_type": "assessment", "assessment_id": ulid(label, when), "item_id": revision["item_id"],
            "content_revision": {"ref": revision["revision_id"], "digest": revision["content_sha256"]},
            "coverage": revision["coverage"],
            "interest_profile": {"ref": profile["profile_id"], "digest": profile_digest},
            "policy_version": "intake-policy/1", "model": "synthetic-model-small", "prompt_version": "suitability_v1",
            "disposition": disposition, "relevance": relevance, "confidence": confidence, "rationale": rationale,
            "matched_interests": matched, "unresolved_questions": questions, "assessed_at": when,
        }
        if prior is not None:
            a["prior_assessment"] = {"ref": prior["assessment_id"], "digest": record_digest(prior)}
        return a

    a1 = assess("a1", rev_w1, prof_v1, pv1, iso("2026-10-20 09:00:00"), "candidate", 82, 70,
                "States a method and a measured result relevant to retrieval evaluation.", ["retrieval-eval"],
                ["Is the benchmark corpus available?"])
    a2 = assess("a2", rev_w1, prof_v2, pv2, iso("2026-10-20 12:00:00"), "candidate", 88, 70,
                "Re-assessed under profile v2 (higher retrieval weight). Same content revision.", ["retrieval-eval"],
                ["Is the benchmark corpus available?"], prior=a1)
    a3 = assess("a3", rev_w2, prof_v2, pv2, iso("2026-10-21 06:30:00"), "needs_evidence", 64, 60,
                "The update weakens the headline result. The claim needs the corrected numbers.", ["retrieval-eval"],
                ["What did the tokenizer fix change?"], prior=a2)
    a4 = assess("a4", rev_b, prof_v1, pv1, iso("2026-10-20 09:00:00"), "needs_evidence", 40, 15,
                "Capture was blocked; only the post context was available.", [],
                ["Can the page be read through another route?"])
    a5 = assess("a5", rev_r, prof_v1, pv1, iso("2026-10-20 09:00:00"), "reference_only", 50, 45,
                "Partial README only. Useful as a pointer to the toolkit.", ["retrieval-eval", "sqlite-tooling"], [])

    write("assessment", "web-r1-profile-v1", "valid", a1, ["first assessment"], "valid assessment")
    write("assessment", "web-r1-profile-v2-reassessed", "valid", a2, ["changed-profile reassessment: same content revision, new profile digest, prior_assessment set"], "valid assessment")
    write("assessment", "web-r2-changed-content", "valid", a3, ["assessment of the changed-content revision"], "valid assessment")
    write("assessment", "blocked-capture", "valid", a4, ["blocked capture assessed with coverage shown; disposition is a judgement, not the retrieval state"], "valid assessment")
    write("assessment", "partial-capture", "valid", a5, ["partial capture assessed with coverage shown"], "valid assessment")
    bad = copy.deepcopy(a4); bad["disposition"] = "candidate"
    write("assessment", "blocked-candidate", "invalid", bad, ["coverage rules"], "an item whose capture had no readable content cannot be a candidate", fails_at='disposition')
    bad = copy.deepcopy(a4); bad["disposition"] = "blocked"
    write("assessment", "retrieval-state-as-disposition", "invalid", bad, ["disposition enum"], "retrieval failures are operational states, never dispositions", fails_at='disposition')
    bad = copy.deepcopy(a1); bad["relevance"] = 0.82
    write("assessment", "float-relevance", "invalid", bad, ["integers only"], "relevance is an integer 0-100", fails_at='relevance', fails_with='integer')

    # ── Usage records ────────────────────────────────────────────────
    u_x = {"record_type": "usage-record", "usage_id": ulid("u-x", "2026-10-20T08:00:05Z"), "kind": "x_api", "provider": "x",
           "operation": "GET /2/users/:id/bookmarks", "request_count": 1, "resource_count": 2,
           "cost_micro_usd": 2000, "cost_basis": "estimated", "occurred_at": iso("2026-10-20 08:00:05"), "sync_run_id": run1}
    u_m = {"record_type": "usage-record", "usage_id": ulid("u-m", "2026-10-20T09:00:00Z"), "kind": "model", "provider": "anthropic",
           "operation": "enrich:suitability", "model": "synthetic-model-small", "request_count": 1,
           "input_tokens": 1800, "output_tokens": 220, "cache_read_tokens": 0,
           "cost_micro_usd": 2900, "cost_basis": "estimated", "occurred_at": iso("2026-10-20 09:00:00"), "assessment_id": a1["assessment_id"]}
    write("usage-record", "x-api-page", "valid", u_x, ["X API page read"], "valid usage record")
    write("usage-record", "model-call", "valid", u_m, ["model call for an assessment"], "valid usage record")
    bad = copy.deepcopy(u_m); del bad["model"]
    write("usage-record", "model-without-model", "invalid", bad, ["kind rules"], "model usage must name the model", fails_at='', fails_with="'model' is a required property")
    bad = copy.deepcopy(u_x); bad["cost_micro_usd"] = 0.002
    write("usage-record", "float-cost", "invalid", bad, ["integers only"], "cost is integer micro-USD", fails_at='cost_micro_usd', fails_with='integer')
    bad = copy.deepcopy(u_x); bad["operation"] = "GET /2/users/1900000000000000901/bookmarks?pagination_token=abc"
    write("usage-record", "operation-with-query", "invalid", bad, ["no URLs or tokens in operation"], "operation is an endpoint template, never a full request", fails_at='operation')

    # ── Evidence packets ─────────────────────────────────────────────
    packet_id = ulid("packet-1", "2026-10-20T10:00:00Z")
    p1 = {
        "record_type": "evidence-packet", "contract": "muninn-research-intake", "contract_version": CONTRACT_VERSION,
        "status": "active", "packet_id": packet_id, "revision": 1, "created_at": iso("2026-10-20 10:00:00"),
        "subject_item_id": A,
        "items": [item_a, item_q, item_w, item_r, item_b],
        "item_links": links,
        "observations": [obs_a],
        "content_revisions": [rev_a, rev_q, rev_w1, rev_w_arch, rev_r, rev_b],
        "interest_profiles": [prof_v1],
        "assessments": [a1, a4, a5],
        "primary_assessment": {"ref": a1["assessment_id"], "digest": record_digest(a1)},
        "evidence": {
            "supporting": [{"statement": "Hybrid retrieval improved recall at ten on a synthetic corpus.",
                            "source": {"ref": rev_w1["revision_id"], "digest": rev_w1["content_sha256"]},
                            "excerpt": "Hybrid retrieval improved recall at ten by a wide margin."}],
            "contradictory": [{"statement": "The archived draft reported no results yet (historical comparison).",
                               "source": {"ref": rev_w_arch["revision_id"], "digest": rev_w_arch["content_sha256"]},
                               "excerpt": "Results pending."}],
        },
        "repository_revisions": [{"item_id": R, "commit_sha": hashlib.sha1(b"synthetic-commit-1").hexdigest(),
                                  "ref_name": "main", "inspected_at": iso("2026-10-20 09:30:00")}],
        "unresolved_questions": ["Is the benchmark corpus available?", "Can the blocked page be read another way?"],
        "usage": [u_x, u_m],
    }
    p2 = copy.deepcopy(p1)
    p2.update({
        "revision": 2, "created_at": iso("2026-10-21 07:00:00"),
        "previous_revision": {"ref": f"{packet_id}@1", "digest": record_digest(p1)},
        "content_revisions": [rev_a, rev_q, rev_w1, rev_w2, rev_w_arch, rev_r, rev_b],
        "interest_profiles": [prof_v1, prof_v2],
        "assessments": [a1, a2, a3, a4, a5],
        "primary_assessment": {"ref": a3["assessment_id"], "digest": record_digest(a3)},
        "unresolved_questions": ["What did the tokenizer fix change?", "Can the blocked page be read another way?"],
    })
    p2["evidence"]["contradictory"].insert(0, {
        "statement": "The page's update says the margin shrank after a tokenizer fix.",
        "source": {"ref": rev_w2["revision_id"], "digest": rev_w2["content_sha256"]},
        "excerpt": "the margin shrank after a tokenizer fix."})
    del p2["usage"]

    write("evidence-packet", "packet-r1", "valid", p1,
          ["old packet that still resolves (p2.previous_revision carries its digest)", "blocked and partial captures with coverage shown"], "valid packet")
    write("evidence-packet", "packet-r2", "valid", p2,
          ["revision 2 superseding r1", "changed-content revision", "changed-profile reassessment"], "valid packet")

    # A different packet, withdrawn: only its tombstone is kept.
    other = copy.deepcopy(p1)
    other["packet_id"] = ulid("packet-2", "2026-10-20T11:00:00Z")
    other["created_at"] = iso("2026-10-20 11:00:00")
    tomb = make_tombstone(other, reason="operator_request", withdrawn_at=iso("2026-10-21 08:00:00"))
    write("evidence-packet", "withdrawn-tombstone", "valid", tomb,
          ["withdrawn packet tombstone: id, revision, original digest, reason, time"], "valid tombstone")

    bad = copy.deepcopy(p2); del bad["previous_revision"]
    write("evidence-packet", "r2-without-previous", "invalid", bad, ["revision rules"], "a revision above 1 must name the revision it supersedes", fails_at='', fails_with="'previous_revision' is a required property")
    bad = copy.deepcopy(p1); bad["previous_revision"] = {"ref": f"{packet_id}@1", "digest": record_digest(p1)}
    write("evidence-packet", "r1-with-previous", "invalid", bad, ["revision rules"], "revision 1 supersedes nothing", fails_at='', fails_with='previous_revision')
    bad = copy.deepcopy(tomb); bad["items"] = [item_a]
    write("evidence-packet", "tombstone-with-content", "invalid", bad, ["tombstone shape"], "a tombstone carries no content", fails_at='', fails_with="'items' was unexpected")
    bad = copy.deepcopy(p1); bad["status"] = "draft"
    write("evidence-packet", "unknown-status", "invalid", bad, ["status"], "status is active or withdrawn", fails_at='status')
    bad = copy.deepcopy(p1); bad["assessments"][0]["confidence"] = 0.7
    write("evidence-packet", "float-inside", "invalid", bad, ["integers only"], "a nested float makes the packet non-canonical", fails_at='assessments/0/confidence', fails_with='integer')

    # Sanity: digests are reproducible and canonical bytes are stable.
    assert canonical_json(p1) == canonical_json(json.loads(canonical_json(p1)))


if __name__ == "__main__":
    main()
