"""Heuristic auth-wall detection.

Scrape responses that look like login/sign-in pages are flagged
``scrape_status='auth_required'`` so downstream enrichment skips them.
The threshold is intentionally conservative: at least two of the
``AUTH_WALL_PATTERNS`` must match before we declare the page an auth wall
(any single marker is too likely to occur on legitimate pages with login
links in the chrome — an article that mentions "Sign in" once should not
trigger).
"""

from __future__ import annotations

import re

# Common login-form / auth-wall markers. Mix of structural (form action,
# password input) and copy-based ("Sign in", "Log in", "authentication
# required") signals.
AUTH_WALL_PATTERNS = [
    re.compile(r'<form[^>]*action=["\'][^"\']*login[^"\']*["\']', re.I),
    re.compile(r'<form[^>]*action=["\'][^"\']*sign.?in[^"\']*["\']', re.I),
    re.compile(r'<input[^>]*type=["\']password["\']', re.I),
    re.compile(r'<input[^>]*name=["\']password["\']', re.I),
    re.compile(r"please\s+(log|sign)\s+in", re.I),
    re.compile(r"login\s+required", re.I),
    re.compile(r"authentication\s+required", re.I),
    re.compile(r"\bsign\s+in\b", re.I),
    re.compile(r"\blog\s+in\b", re.I),
]

# Two distinct matches required to flag.
AUTH_WALL_THRESHOLD = 2


def detect_auth_wall(html: str) -> bool:
    """Return ``True`` if the HTML looks like a login or auth-wall page."""
    if not html:
        return False
    matches = sum(1 for p in AUTH_WALL_PATTERNS if p.search(html))
    return matches >= AUTH_WALL_THRESHOLD
