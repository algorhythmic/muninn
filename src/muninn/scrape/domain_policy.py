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
    """Load policy from `domain_policy.yml`. Missing/empty file → empty policy.

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
        return DomainPolicy.empty()

    raw = p.read_text(encoding="utf-8")
    if not raw.strip():
        return DomainPolicy.empty()

    data = yaml.safe_load(raw) or {}
    domains = tuple(_clean_list(data.get("domains")))
    paths = tuple(_clean_list(data.get("paths")))
    return DomainPolicy(domain_patterns=domains, path_patterns=paths)


def _clean_list(value: object) -> list[str]:
    if not value:
        return []
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if item is not None and str(item).strip()]
