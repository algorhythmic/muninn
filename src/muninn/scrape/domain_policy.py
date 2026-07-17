"""Load and apply `domain_policy.yml`.

Per SPEC.md Decision 5 (sanitize, don't exclude): the policy file is a
single toggle per matching entry. When a bookmark URL matches, that
bookmark stays in the `bookmarks` table but with `content_visible = 0` —
gating scrape, enrich, vault, and MCP exposure. The event survives.

Lives under `scrape/` per SPEC layout but is also called by the ingest
pipeline at insert time to set `content_visible`.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import yaml


class DomainPolicyError(RuntimeError):
    """The policy file is missing or malformed.

    Per SPEC Decision 5 the pipeline must never silently default to
    scrape-everything: an absent or unreadable policy aborts the run. An
    EXISTING empty file is a deliberate "no rules" statement and is fine.
    """


@dataclass(frozen=True)
class DomainPolicy:
    """Compiled policy: glob patterns matched case-insensitively against
    `(hostname, path)` of a sanitized URL."""

    domain_patterns: tuple[str, ...] = ()
    path_patterns: tuple[str, ...] = ()

    @classmethod
    def empty(cls) -> "DomainPolicy":
        return cls()

    def matches(self, url: str | None) -> bool:
        """True if the URL matches any pattern → caller should set
        `content_visible = 0`. None URLs (failed sanitization) never match."""
        if not url:
            return False
        try:
            parts = urlsplit(url)
        except ValueError:
            return False
        host = (parts.hostname or "").lower()
        path = parts.path or ""
        host_path = f"{host}{path}"

        for pattern in self.domain_patterns:
            if fnmatch.fnmatchcase(host, pattern.lower()):
                return True
        for pattern in self.path_patterns:
            if fnmatch.fnmatchcase(host_path, pattern.lower()):
                return True
        return False


def load_domain_policy(path: Path | str | None = None) -> DomainPolicy:
    """Load policy from `domain_policy.yml`. Missing file → DomainPolicyError.

    An existing-but-empty file is a deliberate "no rules" statement and
    yields an empty policy; a missing or malformed file aborts (SPEC
    Decision 5: never silently default to scrape-everything).

    Schema (per the canonical `domain_policy.yml`):

        domains:
          - example.com
          - "*.banking.com"
        paths:
          - "github.com/private-org/*"
    """
    if path is None:
        # Default to the repo-root copy.
        path = Path(__file__).resolve().parents[3] / "domain_policy.yml"
    p = Path(path)
    if not p.exists():
        raise DomainPolicyError(
            f"domain_policy.yml not found at {p}. Refusing to default to "
            f"scrape-everything (SPEC Decision 5). Create the file — an "
            f"empty file explicitly means 'no rules'."
        )

    raw = p.read_text(encoding="utf-8")
    if not raw.strip():
        return DomainPolicy.empty()

    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        raise DomainPolicyError(f"{p} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise DomainPolicyError(f"{p} must be a mapping with 'domains'/'paths' keys")
    domains = tuple(_clean_list(data.get("domains"), "domains", p))
    paths = tuple(_clean_list(data.get("paths"), "paths", p))
    return DomainPolicy(domain_patterns=domains, path_patterns=paths)


def _clean_list(value: object, key: str, source: Path) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise DomainPolicyError(f"{source}: '{key}' must be a list, got {type(value).__name__}")
    return [str(item).strip() for item in value if item is not None and str(item).strip()]
