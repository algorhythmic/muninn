"""Synthetic corpus and canned outputs for the model-tier lint tests.

Tag ``llm`` is carried by three visible pages and one hidden one:

- 1: a browser bookmark (captured T0) claiming v1 caches prompts for five
  minutes;
- 2: a browser bookmark captured 30 days later claiming v2 caches them for
  one hour (it supersedes 1);
- 3: an X post (published T0 + 40 days) that says caching never works and
  links bookmark 4; its stored summary quotes 4's locator;
- 4: the x_link post 3 links, hidden by domain policy (``content_visible =
  0``), also tagged ``llm``;
- 5: visible, tagged ``other`` only.
"""

from __future__ import annotations

import json
import sqlite3

from muninn.consumers.packets import link_item_id

T0 = 1_700_000_000
DAY = 86_400
POST_ID = "1900000000000000003"
HIDDEN_URL = "https://intra.example/secret-plan"

TEXT_1 = "The library v1 caches prompts for five minutes. Version 1 is the current release."
TEXT_2 = "Version 2 replaces v1: prompts are now cached for one hour by default."
TEXT_3 = f"Hot take: prompt caching never works in practice. Details {HIDDEN_URL} soon."


def seed(conn: sqlite3.Connection) -> sqlite3.Connection:
    meta = {"author": {"id": "1900000000000000901", "username": "synth_author"},
            "published_at": T0 + 40 * DAY}
    conn.executemany(
        "INSERT INTO bookmarks (bookmark_id, source, source_id, captured_at, title, url, domain, "
        "  content_visible, enrichment_source, source_metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (1, "bookmarks_html", "1", T0, "Caching in v1", "https://example.org/v1", "example.org", 1, "at_capture", None),
            (2, "bookmarks_html", "2", T0 + 30 * DAY, "Caching in v2", "https://example.org/v2", "example.org", 1, "at_capture", None),
            (3, "x", POST_ID, T0 + 41 * DAY, f"Hot take: prompt caching never works {HIDDEN_URL}",
             f"https://x.com/synth_author/status/{POST_ID}", "x.com", 1, "live_fallback", json.dumps(meta)),
            (4, "x_link", "a" * 64, T0 + 41 * DAY, "Secret plan", HIDDEN_URL, "intra.example", 0, None, None),
            (5, "bookmarks_html", "5", T0 + 50 * DAY, "Unrelated", "https://example.org/u", "example.org", 1, "at_capture", None),
        ],
    )
    conn.executemany(
        "INSERT INTO scrape_results (bookmark_id, pass, fetched_at, scrape_status, content_text) VALUES (?, ?, ?, 'ok', ?)",
        [
            (1, "at_capture", T0, TEXT_1),
            (2, "at_capture", T0 + 30 * DAY, TEXT_2),
            (3, "live", T0 + 41 * DAY, TEXT_3),
            (4, "live", T0 + 41 * DAY, "Hidden page text about the secret plan."),
            (5, "at_capture", T0 + 50 * DAY, "Nothing to see."),
        ],
    )
    conn.executemany(
        "INSERT INTO enriched (bookmark_id, summary, tags, enrichment_model, enrichment_prompt_version, "
        "  content_hash, enriched_at) VALUES (?, ?, ?, 'haiku', 'per_bookmark_v1', ?, ?)",
        [
            (1, "v1 caches prompts for five minutes.", json.dumps(["llm", "caching"]), "h1", T0),
            (2, "v2 caches prompts for an hour.", json.dumps(["LLM"]), "h2", T0),
            (3, f"A post doubting caching, pointing at {HIDDEN_URL}.", json.dumps(["llm"]), "h3", T0),
            (4, "The secret plan.", json.dumps(["llm"]), "h4", T0),
            (5, "Unrelated.", json.dumps(["other"]), "h5", T0),
        ],
    )
    conn.execute(
        "INSERT INTO item_links (parent_bookmark_id, child_bookmark_id, parent_ref, child_ref, relation, "
        "  position, first_seen_at) VALUES (3, 4, ?, ?, 'links_to', 0, ?)",
        (f"x:post:{POST_ID}", link_item_id(HIDDEN_URL), T0),
    )
    conn.commit()
    return conn


def finding(relation: str, a: int, b: int, quotes: list[tuple[int, str]], rationale: str = "They conflict.") -> dict:
    return {
        "relation": relation,
        "bookmark_id": a,
        "other_bookmark_id": b,
        "rationale": rationale,
        "evidence": [{"bookmark_id": i, "quote": q} for i, q in quotes],
    }


SUPERSEDES = finding(
    "supersedes", 2, 1,
    [(2, "prompts are now cached for one hour"), (1, "caches prompts for five minutes")],
    "v2 changed the cache lifetime from five minutes to one hour.",
)
CONTRADICTS = finding(
    "contradicts", 3, 1,
    [(3, "prompt caching never works"), (1, "The library v1 caches prompts")],
    "One says caching never works; the other documents how it works.",
)


def lint_output(*findings: dict) -> dict:
    return {
        "findings": list(findings),
        "synthesis_metadata": {
            "model": "claude-opus-5-5",
            "prompt_version": "lint_v1",
            "input_token_count": 0,
            "output_token_count": 0,
        },
    }
