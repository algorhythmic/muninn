"""RFC 9309 robots.txt matching (muninn.scrape.robots).

Every case here is one ``urllib.robotparser`` got wrong (independent
review, 2026-10-23), plus the RFC's other rules. Synthetic hosts only.
"""

from __future__ import annotations

import pytest

from muninn.scrape.robots import normalize, parse

SITE = "https://site.example.org"


def allowed(text: str, path: str, token: str = "Muninn") -> bool:
    return parse(text).can_fetch(token, SITE + path)


# ── Precedence: longest match, not file order ─────────────────────

def test_allow_root_before_a_longer_disallow_does_not_win() -> None:
    rules = "User-agent: *\nAllow: /\nDisallow: /private\n"
    assert not allowed(rules, "/private/x")
    assert allowed(rules, "/public/x")


def test_disallow_root_before_a_longer_allow_does_not_win() -> None:
    rules = "User-agent: *\nDisallow: /\nAllow: /public\n"
    assert allowed(rules, "/public/x")
    assert not allowed(rules, "/other")


def test_allow_wins_a_tie() -> None:
    assert allowed("User-agent: *\nDisallow: /page\nAllow: /page\n", "/page")
    assert allowed("User-agent: *\nAllow: /page\nDisallow: /page\n", "/page")


def test_length_counts_the_pattern_octets() -> None:
    # "/*.pdf" (6) is longer than "/docs" (5).
    rules = "User-agent: *\nAllow: /docs\nDisallow: /*.pdf\n"
    assert not allowed(rules, "/docs/a.pdf")
    assert allowed(rules, "/docs/a.html")


# ── Wildcards ─────────────────────────────────────────────────────

def test_star_matches_any_run() -> None:
    rules = "User-agent: *\nDisallow: /*.pdf\n"
    assert not allowed(rules, "/doc.pdf")
    assert not allowed(rules, "/a/b/doc.pdf?x=1")  # no $: a prefix match
    assert allowed(rules, "/doc.html")


def test_star_before_a_query() -> None:
    rules = "User-agent: *\nDisallow: /*?\n"
    assert not allowed(rules, "/p?q=1")
    assert allowed(rules, "/p")


def test_trailing_dollar_anchors_the_end() -> None:
    rules = "User-agent: *\nDisallow: /*.pdf$\n"
    assert not allowed(rules, "/doc.pdf")
    assert allowed(rules, "/doc.pdf?download=1")
    assert allowed(rules, "/doc.pdfx")


def test_dollar_elsewhere_and_other_regex_characters_are_literal() -> None:
    rules = "User-agent: *\nDisallow: /a$b\nDisallow: /c.d\nDisallow: /e+(f)\n"
    assert not allowed(rules, "/a$b")
    assert allowed(rules, "/a")
    assert not allowed(rules, "/c.d")
    assert allowed(rules, "/cxd")
    assert not allowed(rules, "/e+(f)/g")


# ── Agent selection and group merging ─────────────────────────────

def test_every_group_naming_the_agent_is_merged() -> None:
    rules = "User-agent: muninn\nDisallow: /a\n\nUser-agent: Muninn\nDisallow: /b\n"
    assert not allowed(rules, "/a")
    assert not allowed(rules, "/b")
    assert allowed(rules, "/c")


def test_a_named_group_replaces_the_star_groups() -> None:
    rules = (
        "User-agent: *\nDisallow: /\n\n"
        "User-agent: Muninn\nDisallow: /private/\nAllow: /\n"
    )
    assert allowed(rules, "/posts/x")
    assert not allowed(rules, "/private/notes")
    assert not allowed(rules, "/posts/x", token="OtherBot")


def test_star_groups_are_merged_too() -> None:
    rules = "User-agent: *\nDisallow: /a\n\nUser-agent: *\nDisallow: /b\n"
    assert not allowed(rules, "/a") and not allowed(rules, "/b")


def test_the_product_token_is_matched_exactly_not_as_a_substring() -> None:
    assert allowed("User-agent: mun\nDisallow: /\n", "/x")
    assert allowed("User-agent: muninnbot\nDisallow: /\n", "/x")
    assert allowed("User-agent: xmuninn\nDisallow: /\n", "/x")


def test_the_product_token_is_case_insensitive_and_ends_at_a_non_token_char() -> None:
    assert not allowed("User-agent: MUNINN\nDisallow: /\n", "/x")
    assert not allowed("User-agent: Muninn/1.0\nDisallow: /\n", "/x")


def test_consecutive_user_agent_lines_share_one_group() -> None:
    rules = "User-agent: OtherBot\nUser-agent: Muninn\nDisallow: /shared\n"
    assert not allowed(rules, "/shared")
    assert not allowed(rules, "/shared", token="OtherBot")


def test_a_user_agent_line_after_rules_starts_a_new_group() -> None:
    rules = "User-agent: Muninn\nDisallow: /a\nUser-agent: OtherBot\nDisallow: /b\n"
    assert not allowed(rules, "/a")
    assert allowed(rules, "/b")


def test_no_matching_group_means_no_rules() -> None:
    assert allowed("User-agent: OtherBot\nDisallow: /\n", "/x")
    assert allowed("", "/x")


# ── Lines that are not rules ──────────────────────────────────────

def test_empty_disallow_allows_everything() -> None:
    assert allowed("User-agent: *\nDisallow:\n", "/anything")


def test_unknown_lines_comments_and_ungrouped_rules_are_ignored() -> None:
    rules = (
        "﻿Disallow: /before-any-group\n"
        "Sitemap: https://site.example.org/sitemap.xml\n"
        "# a comment\n"
        "this line has no colon\n"
        "User-agent: *   # trailing comment\n"
        "Crawl-delay: 10\n"
        "Disallow: /x # why\n"
        "Noindex: /y\n"
    )
    assert allowed(rules, "/before-any-group")
    assert not allowed(rules, "/x")
    assert allowed(rules, "/y")


def test_keys_are_case_insensitive() -> None:
    assert not allowed("USER-AGENT: *\nDISALLOW: /x\n", "/x")


def test_robots_txt_itself_is_always_allowed() -> None:
    assert allowed("User-agent: *\nDisallow: /\n", "/robots.txt")


# ── Percent-encoding normalization ────────────────────────────────

@pytest.mark.parametrize(
    "rule, path",
    [
        ("/%7Euser", "/~user"),      # escaped unreserved in the rule
        ("/~user", "/%7euser"),      # escaped unreserved in the URL
        ("/a%2fb", "/a%2Fb"),        # reserved escapes compare case-insensitively
        ("/café", "/caf%C3%A9"),     # non-ASCII rule octets are encoded
        ("/caf%c3%a9", "/café"),     # ... and non-ASCII URL octets
    ],
)
def test_percent_encoding_is_normalized(rule: str, path: str) -> None:
    assert not allowed(f"User-agent: *\nDisallow: {rule}\n", path)


def test_an_escaped_reserved_character_is_not_decoded() -> None:
    # "/a%2Fb" is one segment; "/a/b" is two: different resources.
    assert allowed("User-agent: *\nDisallow: /a/b\n", "/a%2Fb")


def test_normalize() -> None:
    assert normalize("/%7e%2f%41é ") == "/~%2FA%C3%A9%20"
    assert normalize("/50%") == "/50%"  # a stray % is left alone
