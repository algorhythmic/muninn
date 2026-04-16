"""JWT-shape detection per SPEC.md Rule 2.

JWT structure: <header>.<payload>.<signature> where header and payload are
base64url-encoded JSON objects. Both objects start with `{`, which base64-
encodes to a prefix of `eyJ` — so genuine JWTs always begin `eyJ…`.
"""

from __future__ import annotations

import re

# Anchored full-string match. The dataclass-level `re.search` variant is also
# exposed via JWT_PATTERN for callers that want substring matching, but
# `looks_like_jwt` matches the full string only (per SPEC §"Rule 2").
JWT_PATTERN = re.compile(
    r"^eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$"
)


def looks_like_jwt(value: str) -> bool:
    """True if `value` is a full-string JWT (three base64url segments,
    first two prefixed `eyJ`). Total function — never raises."""
    if not isinstance(value, str):
        return False
    return bool(JWT_PATTERN.match(value))
