"""Vault pages for the X lane: posts, X Articles and linked items.

Seeded with :mod:`tests.x_corpus` (contract section 10.1 row shapes).
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest

from muninn.consumers.packets.canonical import content_digest, new_ulid
from muninn.consumers.vault import x_pages
from muninn.consumers.vault.compiler import compile_vault
from muninn.db import connect
from tests import x_corpus as xc
from tests.integration.test_vault_golden import GOLDEN_DIR, _seed_browser_corpus

POST_SLUG = f"x-synth-author-a-{xc.POST_ID}"
QUOTED_SLUG = f"x-synth-author-b-{xc.QUOTED_POST_ID}"
ARTICLE_SLUG = f"x-article-{xc.ARTICLE_ID}"
REPO_SLUG = "github-synthetic-org-synthetic-repo"


def _seed(db_path: Path, **kwargs) -> xc.XCorpus:
    conn = connect(db_path)
    try:
        return xc.seed_x_corpus(conn, **kwargs)
    finally:
        conn.close()


def _compile(db_path: Path, tmp_path: Path) -> dict[str, str]:
    out = tmp_path / "compiled"
    count = compile_vault(db_path=db_path, output_dir=out, personal_dir=None)
    pages = {p.stem: p.read_text() for p in (out / "wiki" / "bookmarks").glob("*.md")}
    assert count == len(pages)
    return pages


def _frontmatter(page: str) -> dict[str, str]:
    block = page.split("---\n", 2)[1]
    return dict(line.split(": ", 1) for line in block.splitlines())


def _web_slug(pages: dict[str, str]) -> str:
    (slug,) = [s for s in pages if s.startswith("example-com-notes-retrieval-eval-")]
    return slug


# ── pages and privacy ─────────────────────────────────────────────


def test_one_page_per_visible_x_item_and_none_for_hidden(db_path: Path, tmp_path: Path):
    _seed(db_path)
    pages = _compile(db_path, tmp_path)
    assert set(pages) == {POST_SLUG, QUOTED_SLUG, ARTICLE_SLUG, REPO_SLUG, _web_slug(pages)}
    everything = "\n".join(pages.values())
    # The hidden link: no page, no wikilink, no title, no locator.
    assert "Private roadmap" not in everything
    assert "intranet.example" not in everything
    assert xc.HIDDEN_URL not in everything


@pytest.mark.parametrize("hidden_at_ingest", [True, False])
def test_post_text_masks_the_hidden_links_locator(db_path: Path, tmp_path: Path, hidden_at_ingest):
    # False: the stored text still quotes the locator (hidden by a policy
    # change after the last sync), so the compiler's own mask must catch it.
    _seed(db_path, hidden_at_ingest=hidden_at_ingest)
    post = _compile(db_path, tmp_path)[POST_SLUG]
    assert "> Thread on evaluating retrieval with the quillwort benchmark." in post
    assert f"> Code: {xc.REPO_URL}" in post
    assert f"> Draft plan: {x_pages.HIDDEN_LINK_TEXT}" in post
    assert xc.HIDDEN_URL not in post


def test_post_frontmatter_keeps_the_three_times_separate(db_path: Path, tmp_path: Path):
    _seed(db_path)
    fm = _frontmatter(_compile(db_path, tmp_path)[POST_SLUG])
    assert fm["page_type"] == "bookmark"
    assert fm["item_kind"] == "x_post"
    assert fm["source"] == "x"
    # Quoted: a 19-digit ID must stay a string for YAML/JS readers.
    assert fm["source_id"] == f'"{xc.POST_ID}"'
    assert fm["item_id"] == f'"x:post:{xc.POST_ID}"'
    assert fm["author_handle"] == '"synth_author_a"'
    assert fm["author_name"] == '"Synthetic Author A"'
    assert fm["author_id"] == '"1900000000000000901"'
    assert fm["language"] == '"en"'
    assert fm["published_at"] == x_pages.iso_utc(xc.PUBLISHED_AT) == "2025-09-30T14:05:00Z"
    assert fm["observed_at"] == x_pages.iso_utc(xc.OBSERVED_AT) == "2025-10-20T08:00:00Z"
    assert fm["observed_at_basis"] == "first_seen_by_sync"
    assert fm["last_observed_at"] == x_pages.iso_utc(xc.LAST_OBSERVED_AT)
    assert fm["retrieved_at"] == x_pages.iso_utc(xc.RETRIEVED_AT) == "2025-10-20T08:00:05Z"
    assert fm["retrieval_status"] == "ok"
    assert "first_seen_at" not in fm
    assert "assessment_disposition" not in fm
    assert len({fm["published_at"], fm["observed_at"], fm["retrieved_at"]}) == 3


def test_quoted_post_was_never_bookmarked(db_path: Path, tmp_path: Path):
    _seed(db_path)
    page = _compile(db_path, tmp_path)[QUOTED_SLUG]
    fm = _frontmatter(page)
    assert fm["observed_at"] == "null"
    assert fm["observed_at_basis"] == "null"
    assert fm["first_seen_at"] == x_pages.iso_utc(xc.OBSERVED_AT)
    assert fm["published_at"] == x_pages.iso_utc(xc.QUOTED_PUBLISHED_AT)
    assert "**Bookmark observed:** not bookmarked" in page


def test_linked_pages_use_first_seen_not_bookmark_observation(db_path: Path, tmp_path: Path):
    _seed(db_path)
    pages = _compile(db_path, tmp_path)
    repo = _frontmatter(pages[REPO_SLUG])
    assert repo["item_kind"] == "github_repo"
    assert repo["item_id"] == '"github:repo:synthetic-org/synthetic-repo"'
    assert repo["published_at"] == "null"  # never inferred from the bookmark
    assert repo["first_seen_at"] == x_pages.iso_utc(xc.OBSERVED_AT)
    assert repo["retrieved_at"] == x_pages.iso_utc(xc.LINK_FETCHED_AT)
    assert "observed_at" not in repo

    web = _frontmatter(pages[_web_slug(pages)])
    assert web["item_kind"] == "web_page"
    assert web["item_id"].startswith('"web:')

    article = _frontmatter(pages[ARTICLE_SLUG])
    assert article["item_kind"] == "x_article"
    assert article["title"] == '"A synthetic long-form article"'
    assert article["published_at"] == x_pages.iso_utc(xc.ARTICLE_PUBLISHED_AT)
    assert article["author_handle"] == '"synth_author_a"'


# ── links ─────────────────────────────────────────────────────────


def test_links_are_bidirectional(db_path: Path, tmp_path: Path):
    _seed(db_path)
    pages = _compile(db_path, tmp_path)
    web = _web_slug(pages)
    post = pages[POST_SLUG]
    links = post.split("## Links\n", 1)[1].split("\n## ", 1)[0]
    assert re.findall(r"^- (.+?): \[\[([^|\]]+)\|", links, re.M) == [
        ("article", ARTICLE_SLUG),
        ("quotes", QUOTED_SLUG),
        ("links to", web),
        ("links to", REPO_SLUG),
    ]
    back = f"[[{POST_SLUG}|@synth_author_a]]"
    assert f"- article of: {back}" in pages[ARTICLE_SLUG]
    assert f"- quoted by: {back}" in pages[QUOTED_SLUG]
    assert f"- linked from: {back}" in pages[web]
    assert f"- linked from: {back}" in pages[REPO_SLUG]


def test_hidden_post_leaves_no_trace_on_its_linked_pages(db_path: Path, tmp_path: Path):
    corpus = _seed(db_path)
    conn = connect(db_path)
    conn.execute("UPDATE bookmarks SET content_visible = 0 WHERE bookmark_id = ?", (corpus.post,))
    conn.commit()
    conn.close()
    pages = _compile(db_path, tmp_path)
    assert POST_SLUG not in pages
    everything = "\n".join(pages.values())
    assert "synth_author_a/status" not in everything
    assert "quillwort" not in everything
    assert "## Linked from" not in pages[REPO_SLUG]


def test_explicitly_linked_pages_are_not_repeated_as_related(db_path: Path, tmp_path: Path):
    corpus = _seed(db_path)
    conn = connect(db_path)
    for bid in (corpus.post, corpus.repo):
        conn.execute(
            "INSERT INTO enriched (bookmark_id, summary, tags, enrichment_model, "
            "  enrichment_prompt_version, content_hash, enriched_at) "
            "VALUES (?, 's', '[\"evals\"]', 'm', 'v', 'h', 1)",
            (bid,),
        )
    conn.commit()
    conn.close()
    post = _compile(db_path, tmp_path)[POST_SLUG]
    assert "## Related" not in post


# ── slugs ─────────────────────────────────────────────────────────


def test_slugs_come_from_identity_not_row_order(db_path: Path, tmp_path: Path):
    """Seeding after unrelated rows shifts every bookmark_id; X slugs stay."""
    conn = connect(db_path)
    conn.executemany(
        "INSERT INTO bookmarks (source, source_id, captured_at, title) VALUES ('bookmarks_html', ?, 1, 'filler')",
        [(str(i),) for i in range(7)],
    )
    conn.commit()
    conn.close()
    _seed(db_path)
    pages = _compile(db_path, tmp_path)
    assert {POST_SLUG, QUOTED_SLUG, ARTICLE_SLUG, REPO_SLUG} <= set(pages)


def test_colliding_x_slugs_get_a_bookmark_id_suffix(db_path: Path, tmp_path: Path):
    corpus = _seed(db_path)
    conn = connect(db_path)
    # A browser bookmark whose slug happens to equal the post page's.
    conn.execute(
        "INSERT INTO bookmarks (bookmark_id, source, source_id, captured_at, title) "
        "VALUES (?, 'bookmarks_html', 'c', 1, 'X synth author a')",
        (int(xc.POST_ID),),
    )
    # A second x_link row for the same repository (locator differs in case).
    other_repo = conn.execute(
        "INSERT INTO bookmarks (source, source_id, captured_at, title, url, domain, enrichment_source) "
        "VALUES ('x_link', 'f00', 1, 'other', 'https://github.com/Synthetic-Org/synthetic-repo', "
        "'github.com', 'none')"
    ).lastrowid
    conn.commit()
    conn.close()
    pages = _compile(db_path, tmp_path)
    # The browser slug never moves; the X page steps aside.
    assert POST_SLUG in pages and "page_type: bookmark\nbookmark_id: " in pages[POST_SLUG]
    assert "item_kind" not in pages[POST_SLUG]
    assert f"{POST_SLUG}-b{corpus.post}" in pages
    # The first repo row (lower bookmark_id) keeps the clean slug.
    assert f"bookmark_id: {corpus.repo}\n" in pages[REPO_SLUG]
    assert f"{REPO_SLUG}-b{other_repo}" in pages


@pytest.mark.parametrize(
    "title, expected",
    [
        ("@a: see https://intranet.example/p/q now", "@a: see [hidden link] now"),
        ("@a: plan at https://intranet.ex…", "@a: plan at [hidden link]"),
        ("@a: plan at https://intranet.ex...", "@a: plan at [hidden link]"),
        ("@a: plan at https://…", "@a: plan at https://…"),  # too short to say
        ("@a: plan at https://example.org/…", "@a: plan at https://example.org/…"),
        ("@a: no links here", "@a: no links here"),
    ],
)
def test_mask_hidden_title(title, expected):
    assert x_pages.mask_hidden_title(title, {"https://intranet.example/p/q"}) == expected


def test_post_slug_without_author_falls_back_to_id():
    row = {"source": "x", "source_id": "123", "source_metadata": None, "url": None,
           "title": None, "bookmark_id": 1}
    assert x_pages.x_slug(row) == "x-post-123"


# ── assessments ───────────────────────────────────────────────────


def _assess(conn: sqlite3.Connection, bookmark_id: int, disposition: str, assessed_at: int) -> None:
    rev = conn.execute(
        "SELECT scrape_history_id, item_ref, content_sha256, coverage FROM scrape_history "
        "WHERE bookmark_id = ?",
        (bookmark_id,),
    ).fetchone()
    profile = content_digest("profile")
    conn.execute(
        "INSERT OR IGNORE INTO interest_profiles (profile_sha256, profile_id, body_json, first_seen_at) "
        "VALUES (?, 'synthetic', '{}', 1)",
        (profile,),
    )
    conn.execute(
        "INSERT INTO assessments (assessment_ulid, scrape_history_id, bookmark_id, item_ref, "
        "  content_sha256, content_coverage, context_digest, context_json, profile_sha256, "
        "  policy_version, model, prompt_version, disposition, relevance, confidence, rationale, "
        "  assessed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, '[]', ?, ?, 'model', 'p1', ?, 70, 60, 'synthetic', ?)",
        (new_ulid(), rev["scrape_history_id"], bookmark_id, rev["item_ref"],
         rev["content_sha256"], rev["coverage"], "c" * 64, profile, f"policy-{assessed_at}",
         disposition, assessed_at),
    )


def test_latest_assessment_is_shown(db_path: Path, tmp_path: Path):
    corpus = _seed(db_path)
    conn = connect(db_path)
    _assess(conn, corpus.post, "deferred", 1_761_000_000)
    _assess(conn, corpus.post, "candidate", 1_761_100_000)
    conn.commit()
    conn.close()
    fm = _frontmatter(_compile(db_path, tmp_path)[POST_SLUG])
    assert fm["assessment_disposition"] == "candidate"
    assert fm["assessed_at"] == x_pages.iso_utc(1_761_100_000)


# ── metadata tolerance and failures ───────────────────────────────


@pytest.mark.parametrize(
    "value, expected",
    [
        (1_759_241_100, 1_759_241_100),
        ("2025-09-30T14:05:00Z", 1_759_241_100),
        ("2025-09-30T14:05:00.000Z", 1_759_241_100),
        ("1759241100", 1_759_241_100),
        ("not a date", None),
        (None, None),
        (True, None),
    ],
)
def test_published_at_accepts_epoch_or_iso(value, expected):
    assert x_pages._to_epoch(value) == expected


def test_author_accepts_contract_spelling():
    meta = {"author": {"platform_id": 5, "handle": "@someone", "display_name": "Some One"}}
    assert x_pages.author_from(meta) == {
        "handle": "someone", "display_name": "Some One", "platform_id": "5",
    }


def test_published_at_falls_back_to_scrape_history(db_path: Path, tmp_path: Path):
    corpus = _seed(db_path)
    conn = connect(db_path)
    meta = json.loads(conn.execute(
        "SELECT source_metadata FROM bookmarks WHERE bookmark_id = ?", (corpus.post,)
    ).fetchone()[0])
    del meta["published_at"]
    conn.execute("UPDATE bookmarks SET source_metadata = ? WHERE bookmark_id = ?",
                 (json.dumps(meta), corpus.post))
    conn.commit()
    conn.close()
    fm = _frontmatter(_compile(db_path, tmp_path)[POST_SLUG])
    assert fm["published_at"] == x_pages.iso_utc(xc.PUBLISHED_AT)


def test_deleted_post_renders_without_text(db_path: Path, tmp_path: Path):
    corpus = _seed(db_path)
    conn = connect(db_path)
    # A post the writer knows only from an errors[] entry has no title.
    conn.execute("UPDATE bookmarks SET title = NULL WHERE bookmark_id = ?", (corpus.post,))
    conn.execute(
        "UPDATE scrape_results SET scrape_status = 'failed', content_text = NULL "
        "WHERE bookmark_id = ? AND pass = 'live'",
        (corpus.post,),
    )
    conn.commit()
    conn.close()
    post = _compile(db_path, tmp_path)[POST_SLUG]
    assert "_Post text unavailable (retrieval status: failed)._" in post
    assert _frontmatter(post)["retrieval_status"] == "failed"
    assert _frontmatter(post)["title"] == '"@synth_author_a on X"'


# ── browser pages unchanged next to X pages ───────────────────────


def test_browser_pages_unchanged_when_x_pages_share_the_vault(db_path: Path, tmp_path: Path):
    _seed_browser_corpus(db_path)
    _seed(db_path)
    out = tmp_path / "compiled"
    compile_vault(db_path=db_path, output_dir=out, personal_dir=None)
    pages_dir = out / "wiki" / "bookmarks"
    for golden in GOLDEN_DIR.glob("*.md"):
        assert (pages_dir / golden.name).read_bytes() == golden.read_bytes()
    assert (pages_dir / f"{POST_SLUG}.md").exists()
