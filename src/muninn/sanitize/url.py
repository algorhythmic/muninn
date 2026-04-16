"""sanitize_url() — total function returning a SanitizationResult.

This is the only path from `raw/` URLs to `bookmarks.url`. It MUST NOT raise
under any input. On unparseable input or a rejected scheme it returns a
result with `sanitized_url=None` and `parse_error` populated; the ingest
pipeline then writes the bookmark row with `url=NULL` and
`source_metadata.parse_error`.

See SPEC.md §"Sanitization rules" (lines 1196–1410) for the full spec.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from muninn.sanitize.rules import (
    DANGEROUS_PARAM_NAMES,
    DEFAULT_PORTS,
    PASSTHROUGH_SCHEMES,
    PATH_CREDENTIAL_PATTERNS,
    REJECTED_SCHEMES,
    SUPPORTED_SCHEMES,
    TRACKING_PARAM_NAMES,
)
from muninn.sanitize.tokens import looks_like_jwt


@dataclass
class SanitizationResult:
    sanitized_url: str | None
    redacted_param_names: list[str] = field(default_factory=list)
    path_redacted: bool = False
    userinfo_redacted: bool = False
    parse_error: str | None = None

    @property
    def redacted_param_count(self) -> int:
        return len(self.redacted_param_names)


def sanitize_url(raw_url: str) -> SanitizationResult:
    """Sanitize a URL per the eight-rule spec. Total function — never raises."""
    try:
        return _sanitize(raw_url)
    except Exception as exc:  # pragma: no cover — belt and suspenders
        return SanitizationResult(
            sanitized_url=None,
            parse_error=f"unexpected:{type(exc).__name__}",
        )


# ── Internal ────────────────────────────────────────────────────────────

def _sanitize(raw_url: object) -> SanitizationResult:
    if raw_url is None or not isinstance(raw_url, str):
        return SanitizationResult(sanitized_url=None, parse_error="empty")
    raw = raw_url.strip()
    if not raw:
        return SanitizationResult(sanitized_url=None, parse_error="empty")

    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        return SanitizationResult(sanitized_url=None, parse_error=f"unparseable:{exc}")

    scheme = parsed.scheme.lower()

    # ── Rule 5: Scheme handling ────────────────────────────────────────
    if not scheme:
        return SanitizationResult(sanitized_url=None, parse_error="empty_scheme")
    if scheme in REJECTED_SCHEMES:
        return SanitizationResult(
            sanitized_url=None,
            parse_error=f"unsupported_scheme:{scheme}",
        )
    if scheme in PASSTHROUGH_SCHEMES:
        # mailto:, tel:, sms: — preserve as-is, no other rules apply.
        return SanitizationResult(sanitized_url=raw)
    if scheme not in SUPPORTED_SCHEMES:
        return SanitizationResult(
            sanitized_url=None,
            parse_error=f"unsupported_scheme:{scheme}",
        )

    # ── Rule 6: Userinfo stripping ─────────────────────────────────────
    userinfo_redacted = bool(parsed.username or parsed.password)

    # ── Rule 8: Host normalization (lowercase + IDN punycode) ──────────
    hostname = (parsed.hostname or "").lower()
    if hostname:
        try:
            hostname = hostname.encode("idna").decode("ascii")
        except (UnicodeError, UnicodeDecodeError):
            # Non-IDN-encodable host (already punycode, or contains :, etc.) —
            # leave the lowercased form.
            pass
    port = parsed.port
    netloc = hostname
    if port is not None and port != DEFAULT_PORTS.get(scheme):
        netloc = f"{hostname}:{port}"

    # ── Rule 4: Path-as-credential ─────────────────────────────────────
    path = parsed.path or ""
    path_redacted = False
    for host_re, path_re, replacement in PATH_CREDENTIAL_PATTERNS:
        if not host_re.match(hostname):
            continue
        m = path_re.match(path)
        if not m:
            continue
        path_redacted = True
        if replacement is not None:
            path = replacement
        else:
            # Magic-link pattern: keep prefix, redact the token segment.
            # Group 1 captured the prefix name (`magic-link`, etc.).
            prefix = m.group(1)
            path = f"/{prefix}/[redacted]"
        break

    # ── Rules 1, 1b, 2: Query params ───────────────────────────────────
    sanitized_query, query_redacted = _sanitize_kv_string(parsed.query)

    # ── Rule 7: Fragment — same treatment if it looks key=val ──────────
    fragment = parsed.fragment
    fragment_redacted: list[str] = []
    if fragment and "=" in fragment:
        fragment, fragment_redacted = _sanitize_kv_string(fragment)

    redacted_param_names = sorted(set(query_redacted + fragment_redacted))

    sanitized = urlunsplit((scheme, netloc, path, sanitized_query, fragment))

    return SanitizationResult(
        sanitized_url=sanitized,
        redacted_param_names=redacted_param_names,
        path_redacted=path_redacted,
        userinfo_redacted=userinfo_redacted,
    )


def _sanitize_kv_string(qs: str) -> tuple[str, list[str]]:
    """Apply Rules 1, 1b, 2 to a `k=v&k=v…` string.

    Returns (sorted-and-encoded sanitized string, redacted-name list).
    Tracking params are dropped silently (not in returned list).
    """
    if not qs:
        return "", []

    pairs = parse_qsl(qs, keep_blank_values=True)
    kept: list[tuple[str, str]] = []
    redacted: list[str] = []

    for name, value in pairs:
        lname = name.lower()

        # Rule 1b: tracking params — strip silently.
        if lname in TRACKING_PARAM_NAMES:
            continue

        # Rule 1: dangerous denylist.
        if lname in DANGEROUS_PARAM_NAMES:
            redacted.append(lname)
            continue

        # Rule 2: JWT-shaped value in any param.
        if looks_like_jwt(value):
            redacted.append(lname)
            continue

        kept.append((name, value))

    # Rule 8: sort params alphabetically (case-sensitive on key, then value).
    kept.sort(key=lambda kv: (kv[0], kv[1]))
    encoded = urlencode(kept, doseq=True)
    return encoded, redacted
