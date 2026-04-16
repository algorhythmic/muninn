"""URL sanitization rule data.

Rules live in code, not config. Any change here REQUIRES corresponding
test-case updates in the same PR (see SPEC.md §"CI gate").

Each block carries the rationale for the entry — when grepping/auditing,
you should be able to answer "why is `sig` in the denylist" without
spelunking external docs.
"""

from __future__ import annotations

import re

# ── Rule 1 — Dangerous query parameter names ────────────────────────────
# Case-insensitive denylist. Stripped, with the (lowercased) name appended
# to redacted_param_names so downstream auditors can see what was dropped.
DANGEROUS_PARAM_NAMES: frozenset[str] = frozenset(
    name.lower() for name in [
        # ── OAuth / session tokens ──────────────────────────────────────
        "access_token",     # OAuth bearer access token
        "refresh_token",    # OAuth refresh token
        "id_token",         # OIDC ID token (JWT)
        "token",            # generic — used by many magic-link flows
        "auth_token",
        "authentication_token",
        "authorization",    # rare in URLs but happens
        "bearer",
        "jwt",
        "oauth_token",
        "oauth_verifier",

        # ── OAuth flow artifacts ───────────────────────────────────────
        "code",             # OAuth authorization code (single-use, but stripping is safer)
        "state",            # OAuth state — usually CSRF token; can encode session
        "nonce",            # OIDC nonce
        "grant_type",

        # ── Session identifiers ────────────────────────────────────────
        "session", "sessionid", "session_id", "sid",
        "phpsessid", "jsessionid", "aspsessionid",
        "x-auth-token",

        # ── Credentials ────────────────────────────────────────────────
        "password", "passwd", "pwd", "pass",
        "secret", "client_secret",
        "apikey", "api_key", "app_key", "app_secret", "api_secret",
        "private_token", "private_key",
        "credentials", "key", "secret_key", "signing_key",

        # ── Magic links / single-use credentials ───────────────────────
        "reset_token", "reset_password_token",
        "confirmation_token", "verification_token", "verify_token",
        "magic_link_token", "login_token", "auth_code", "auth",

        # ── AWS pre-signed URLs ────────────────────────────────────────
        "x-amz-signature", "x-amz-credential", "x-amz-security-token",
        "x-amz-algorithm", "x-amz-date", "x-amz-expires",
        "x-amz-signedheaders",
        "awsaccesskeyid",   # S3 v2 signing
        "signature",        # S3 v2 — generic name; might over-strip but safer
        "expires",          # S3 v2 — over-strip risk acceptable

        # ── GCS pre-signed URLs ────────────────────────────────────────
        "x-goog-signature", "x-goog-credential", "x-goog-algorithm",
        "x-goog-date", "x-goog-expires", "x-goog-signedheaders",

        # ── Azure SAS tokens ───────────────────────────────────────────
        # Short-named; over-strip risk on params like "sr" (search results)
        # is acceptable for the leakage protection.
        "sig", "sv", "se", "sp", "sr", "st", "spr", "ss", "srt",

        # ── Zoom / meeting passwords ───────────────────────────────────
        "tk",               # Zoom join token
        # "pwd" already covered above
    ]
)


# ── Rule 1b — Tracking parameter names ──────────────────────────────────
# Cosmetic, stripped silently. Not appended to redacted_param_names — they
# carry no leakage risk worth auditing, only noise reduction.
TRACKING_PARAM_NAMES: frozenset[str] = frozenset(
    name.lower() for name in [
        "utm_source", "utm_medium", "utm_campaign", "utm_term",
        "utm_content", "utm_id", "utm_name",
        "fbclid",                       # Facebook click ID
        "gclid", "gbraid", "wbraid",    # Google ad click IDs
        "gclsrc", "dclid",              # Google DoubleClick
        "mc_eid", "mc_cid",             # Mailchimp tracking
        "_ga", "_gl",                   # Google Analytics linker
        "_hsenc", "_hsmi",              # HubSpot
        "_openstat",                    # Yandex stats
        "yclid",                        # Yandex click ID
        "msclkid",                      # Microsoft ads
        "twclid",                       # Twitter ads
        "li_fat_id",                    # LinkedIn
        "ref", "referrer",              # generic referrer-tracking
        "__s",                          # Drip
        "spm",                          # Alibaba
        "vero_id",                      # Vero
        "wickedid",                     # WickedReports
    ]
)


# ── Rule 4 — Path-as-credential domain patterns ─────────────────────────
# Each entry: (host_regex, path_regex, replacement_path).
# A `replacement_path` of None means: replace just the matched token
# segment (used by the magic-link generic pattern). Order matters — the
# first matching tuple wins.
PATH_CREDENTIAL_PATTERNS: list[tuple[re.Pattern[str], re.Pattern[str], str | None]] = [
    # Slack incoming webhooks: hooks.slack.com/services/T<id>/B<id>/<token>
    (re.compile(r"^hooks\.slack\.com$", re.IGNORECASE),
     re.compile(r"^/services/.+"),
     "/services/[redacted]"),

    # Discord webhooks: discord.com/api/webhooks/<id>/<token>
    (re.compile(r"^discord(app)?\.com$", re.IGNORECASE),
     re.compile(r"^/api/webhooks/.+"),
     "/api/webhooks/[redacted]"),

    # Telegram bot API: api.telegram.org/bot<id>:<token>/<method>
    (re.compile(r"^api\.telegram\.org$", re.IGNORECASE),
     re.compile(r"^/bot[^/]+(/.*)?$"),
     "/[redacted]"),

    # Generic magic-link patterns — domain-agnostic. The path component
    # name suggests credential semantics AND the following segment is
    # ≥16 chars (long enough to plausibly be a token). The token segment
    # is replaced; the prefix is preserved.
    (re.compile(r".*"),
     re.compile(
         r"^/(magic-link|passwordless|verify-email|reset-password|confirm-email|email-confirmation)/[A-Za-z0-9_-]{16,}.*"
     ),
     None),
]


# ── Rule 5 — Scheme handling ────────────────────────────────────────────
SUPPORTED_SCHEMES: frozenset[str] = frozenset(["http", "https"])

# Schemes the URL doesn't survive — bookmark row inserted with url=NULL
# and source_metadata.parse_error populated.
REJECTED_SCHEMES: frozenset[str] = frozenset([
    "javascript",   # XSS payloads — never store
    "data",         # data URLs can be huge and contain anything
    "vbscript",
    "file",         # leaks local filesystem layout
])

# Schemes that pass through unchanged. The body of a "mailto:" or "tel:"
# is the value being preserved — the user bookmarked it for a reason.
PASSTHROUGH_SCHEMES: frozenset[str] = frozenset(["mailto", "tel", "sms"])


# ── Rule 8 — Default ports for normalization stripping ──────────────────
DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}
