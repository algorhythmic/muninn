"""A robots.txt matcher that follows RFC 9309 (stdlib only).

``urllib.robotparser`` is not RFC 9309: it takes the first rule that
matches in file order (no longest-match precedence), has no ``*`` or ``$``
wildcards, does not merge groups naming the same agent, and matches the
product token as a substring. This module does what the RFC says:

- **Groups** (§2.1): one or more ``user-agent`` lines followed by rules.
  Rules before the first ``user-agent`` line belong to no group and are
  ignored, as are unknown keys (``sitemap``, ``crawl-delay``, …), lines
  without a colon and comments (``#`` to end of line).
- **Agent selection** (§2.2.1): a group applies when one of its
  ``user-agent`` values names the crawler's product token, compared
  exactly and case-insensitively (the value's leading run of
  ``[A-Za-z_-]`` characters is its token, so ``Muninn/1.0`` names
  ``Muninn`` but ``Mun`` does not). Every group that names the agent is
  merged; with none, every ``*`` group is merged; with neither, nothing is
  disallowed.
- **Matching** (§2.2.2): a rule's path matches when it is a prefix of the
  URL's path and query, with ``*`` matching any run of characters and a
  trailing ``$`` anchoring the end. The longest matching path (in octets)
  wins; an ``allow`` wins a tie. An empty ``disallow`` (or ``allow``)
  value is no rule. Both sides are percent-encoding-normalized first:
  escapes of unreserved characters are decoded, other escapes upper-cased,
  and non-ASCII octets (UTF-8) encoded.
- ``/robots.txt`` itself is always allowed (§2.2.2).
"""

from __future__ import annotations

import re
import string
from dataclasses import dataclass, field
from urllib.parse import urlsplit

_UNRESERVED = frozenset(string.ascii_letters + string.digits + "-._~")
_HEX = frozenset(string.hexdigits)
# Octets that are never literal in a URI: controls, space, non-ASCII, and
# the characters RFC 3986 excludes.
_ENCODE = frozenset(range(0x00, 0x21)) | frozenset(range(0x7F, 0x100)) | frozenset(b'"<>\\^`{|}')
_TOKEN = re.compile(r"[A-Za-z_-]+")


def normalize(path: str) -> str:
    """Percent-encoding-normalize a URL path or a robots.txt path pattern.

    ``*`` and ``$`` are left as they are (literal in a URL, operators in a
    pattern)."""
    raw = path.encode("utf-8")
    out: list[str] = []
    i = 0
    while i < len(raw):
        octet = raw[i]
        if octet == 0x25 and i + 2 < len(raw) and chr(raw[i + 1]) in _HEX and chr(raw[i + 2]) in _HEX:
            hex_pair = raw[i + 1:i + 3].decode("ascii").upper()
            char = chr(int(hex_pair, 16))
            out.append(char if char in _UNRESERVED else f"%{hex_pair}")
            i += 3
            continue
        out.append(f"%{octet:02X}" if octet in _ENCODE else chr(octet))
        i += 1
    return "".join(out)


@dataclass(frozen=True)
class Rule:
    allow: bool
    path: str  # normalized pattern
    regex: re.Pattern = field(compare=False, repr=False)

    @classmethod
    def make(cls, allow: bool, value: str) -> "Rule":
        path = normalize(value)
        anchored = path.endswith("$")
        body = path[:-1] if anchored else path
        pattern = ".*".join(re.escape(part) for part in body.split("*"))
        return cls(allow, path, re.compile(pattern + (r"\Z" if anchored else ""), re.DOTALL))


@dataclass
class _Group:
    agents: list[str] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)


@dataclass(frozen=True)
class RobotsRules:
    """A parsed robots.txt: each group's agent tokens (lower-cased; ``*``
    for the wildcard) and rules."""

    groups: tuple[tuple[tuple[str, ...], tuple[Rule, ...]], ...]

    def rules_for(self, product_token: str) -> tuple[Rule, ...]:
        """The merged rules of every group naming ``product_token``, else
        of every ``*`` group, else none."""
        token = product_token.lower()
        named = [rules for agents, rules in self.groups if token in agents]
        if not named:
            named = [rules for agents, rules in self.groups if "*" in agents]
        return tuple(rule for rules in named for rule in rules)

    def can_fetch(self, product_token: str, url: str) -> bool:
        target = _path_of(url)
        if target == "/robots.txt":
            return True
        best: Rule | None = None
        for rule in self.rules_for(product_token):
            if not rule.regex.match(target):
                continue
            if (
                best is None
                or len(rule.path) > len(best.path)
                or (len(rule.path) == len(best.path) and rule.allow and not best.allow)
            ):
                best = rule
        return best is None or best.allow


def _path_of(url: str) -> str:
    """The URL's path and query, normalized (``/`` for an empty path)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "/"
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return normalize(path)


def _agent_token(value: str) -> str | None:
    value = value.strip()
    if value.startswith("*"):
        return "*"
    match = _TOKEN.match(value)
    return match.group(0).lower() if match else None


def parse(text: str) -> RobotsRules:
    """Parse robots.txt text (already cut to the size the caller reads)."""
    groups: list[_Group] = []
    current: _Group | None = None
    in_rules = False
    for raw_line in text.lstrip("﻿").splitlines():
        line = raw_line.split("#", 1)[0]
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key, value = key.strip().lower(), value.strip()
        if key == "user-agent":
            if current is None or in_rules:
                current = _Group()
                groups.append(current)
                in_rules = False
            token = _agent_token(value)
            if token:
                current.agents.append(token)
        elif key in ("allow", "disallow"):
            if current is None:
                continue  # a rule outside any group
            in_rules = True
            if value:  # an empty value is no rule
                current.rules.append(Rule.make(key == "allow", value))
    return RobotsRules(tuple(
        (tuple(g.agents), tuple(g.rules)) for g in groups if g.agents
    ))


__all__ = ["RobotsRules", "Rule", "normalize", "parse"]
