"""Unit tests for domain_policy loading (SPEC Decision 5: fail closed).

A missing or malformed policy file must abort — never silently default to
scrape-everything. An existing empty file is an explicit "no rules".
"""

from __future__ import annotations

import pytest

from muninn.scrape.domain_policy import (
    DomainPolicyError,
    load_domain_policy,
)


def test_missing_file_fails_closed(tmp_path):
    with pytest.raises(DomainPolicyError) as exc:
        load_domain_policy(tmp_path / "domain_policy.yml")
    assert "scrape-everything" in str(exc.value)


def test_empty_file_means_no_rules(tmp_path):
    p = tmp_path / "domain_policy.yml"
    p.write_text("")
    policy = load_domain_policy(p)
    assert policy.matches("https://example.com/x") is False


def test_non_mapping_rejected(tmp_path):
    p = tmp_path / "domain_policy.yml"
    p.write_text("- just\n- a list\n")
    with pytest.raises(DomainPolicyError):
        load_domain_policy(p)


def test_non_list_domains_rejected(tmp_path):
    p = tmp_path / "domain_policy.yml"
    p.write_text("domains: chase.example\n")
    with pytest.raises(DomainPolicyError):
        load_domain_policy(p)


def test_invalid_yaml_rejected(tmp_path):
    p = tmp_path / "domain_policy.yml"
    p.write_text("domains: [unclosed\n")
    with pytest.raises(DomainPolicyError):
        load_domain_policy(p)


def test_valid_policy_loads_and_matches(tmp_path):
    p = tmp_path / "domain_policy.yml"
    p.write_text(
        "domains:\n"
        "  - chase.example\n"
        "  - '*.evil-tracker.example'\n"
        "paths:\n"
        "  - 'github.com/private-org/*'\n"
    )
    policy = load_domain_policy(p)
    assert policy.matches("https://chase.example/login") is True
    assert policy.matches("https://a.evil-tracker.example/") is True
    assert policy.matches("https://github.com/private-org/repo") is True
    assert policy.matches("https://github.com/public-org/repo") is False
    assert policy.matches("https://example.com/") is False


def test_repo_root_default_policy_loads():
    # The canonical checked-in domain_policy.yml must always load cleanly.
    policy = load_domain_policy()
    assert policy is not None
