"""Table-driven tests for sanitize_url() — covers all eight rule classes.

See SPEC.md §"Test coverage requirements" (lines 1401–1412) for the
target distribution. Combined with test_sanitize_tokens.py we exceed
the ≥80-case floor.
"""

from __future__ import annotations

import pytest

from muninn.sanitize import SanitizationResult, sanitize_url


# ═══════════════════════════════════════════════════════════════════════
# Rule 1 — Dangerous query param name denylist
# ═══════════════════════════════════════════════════════════════════════

class TestRule1DangerousParams:
    @pytest.mark.parametrize("param", [
        # OAuth / session
        "access_token", "refresh_token", "id_token", "token", "auth_token",
        "authorization", "bearer", "jwt", "oauth_token", "oauth_verifier",
        # OAuth flow
        "code", "state", "nonce", "grant_type",
        # Sessions
        "session", "sessionid", "session_id", "sid",
        "phpsessid", "jsessionid", "x-auth-token",
        # Credentials
        "password", "passwd", "pwd", "pass",
        "secret", "client_secret", "apikey", "api_key",
        "api_secret", "private_token", "private_key",
        "credentials", "key", "secret_key", "signing_key",
        # Magic-link
        "reset_token", "reset_password_token", "confirmation_token",
        "verification_token", "magic_link_token", "login_token", "auth_code",
    ])
    def test_dangerous_param_stripped(self, param):
        url = f"https://example.com/?{param}=leaked_value&safe=keep"
        result = sanitize_url(url)
        assert result.sanitized_url is not None
        assert "leaked_value" not in result.sanitized_url
        assert param.lower() in result.redacted_param_names
        assert "safe=keep" in result.sanitized_url

    def test_multiple_dangerous_params(self):
        url = "https://example.com/?token=x&api_key=y&q=search"
        result = sanitize_url(url)
        assert "token=" not in result.sanitized_url
        assert "api_key=" not in result.sanitized_url
        assert "q=search" in result.sanitized_url
        assert sorted(result.redacted_param_names) == ["api_key", "token"]

    def test_case_insensitive_dangerous_param(self):
        url = "https://example.com/?TOKEN=leaked"
        result = sanitize_url(url)
        assert "leaked" not in result.sanitized_url
        assert "token" in result.redacted_param_names

    def test_no_dangerous_params_no_redaction(self):
        url = "https://example.com/?page=1&sort=date"
        result = sanitize_url(url)
        assert result.redacted_param_names == []
        assert "page=1" in result.sanitized_url

    @pytest.mark.parametrize("param", [
        "x-amz-signature", "x-amz-credential", "x-amz-security-token",
        "x-amz-algorithm", "x-amz-date", "awsaccesskeyid",
        "x-goog-signature", "x-goog-credential", "x-goog-algorithm",
    ])
    def test_aws_gcs_signed_url_params_stripped(self, param):
        url = f"https://bucket.s3.amazonaws.com/file.zip?{param}=SECRET"
        result = sanitize_url(url)
        assert "SECRET" not in result.sanitized_url
        assert param.lower() in result.redacted_param_names

    @pytest.mark.parametrize("param", [
        "sig", "sv", "se", "sp", "sr", "st", "spr", "ss", "srt",
    ])
    def test_azure_sas_short_params_stripped(self, param):
        url = f"https://acct.blob.core.windows.net/c/file?{param}=secretvalue"
        result = sanitize_url(url)
        assert "secretvalue" not in result.sanitized_url
        assert param in result.redacted_param_names

    def test_zoom_join_token_stripped(self):
        url = "https://zoom.us/j/1234567890?tk=ABCDEFGSECRET"
        result = sanitize_url(url)
        assert "ABCDEFGSECRET" not in result.sanitized_url
        assert "tk" in result.redacted_param_names


# ═══════════════════════════════════════════════════════════════════════
# Rule 1b — Tracking params (silent strip)
# ═══════════════════════════════════════════════════════════════════════

class TestRule1bTrackingParams:
    @pytest.mark.parametrize("param", [
        "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
        "utm_id", "utm_name",
        "fbclid", "gclid", "gbraid", "wbraid", "gclsrc", "dclid",
        "msclkid", "mc_eid", "mc_cid", "yclid", "_ga", "_gl",
        "_hsenc", "_hsmi", "_openstat", "ref", "referrer",
        "__s", "spm", "vero_id", "wickedid", "twclid", "li_fat_id",
    ])
    def test_tracking_param_stripped_silently(self, param):
        url = f"https://example.com/?{param}=tracking_value&q=keep"
        result = sanitize_url(url)
        assert result.sanitized_url is not None
        assert "tracking_value" not in result.sanitized_url
        # NOT recorded in redacted_param_names — silent strip.
        assert param not in result.redacted_param_names
        assert "q=keep" in result.sanitized_url

    def test_mixed_tracking_and_dangerous(self):
        url = "https://example.com/?utm_source=fb&token=secret&q=hi"
        result = sanitize_url(url)
        assert "utm_source" not in result.sanitized_url
        assert "token=" not in result.sanitized_url
        assert "q=hi" in result.sanitized_url
        assert "token" in result.redacted_param_names
        assert "utm_source" not in result.redacted_param_names


# ═══════════════════════════════════════════════════════════════════════
# Rule 2 — JWT-shape detection
# ═══════════════════════════════════════════════════════════════════════

class TestRule2JWT:
    def test_jwt_in_param_value_stripped(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV"
        url = f"https://example.com/?ctx={jwt}&page=1"
        result = sanitize_url(url)
        assert jwt not in result.sanitized_url
        assert "ctx" in result.redacted_param_names
        assert "page=1" in result.sanitized_url

    def test_non_jwt_value_kept(self):
        url = "https://example.com/?ctx=normal_value"
        result = sanitize_url(url)
        assert "ctx=normal_value" in result.sanitized_url

    def test_jwt_like_but_missing_third_segment(self):
        url = "https://example.com/?ctx=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ"
        result = sanitize_url(url)
        assert "ctx=" in result.sanitized_url

    def test_jwt_without_eyJ_prefix(self):
        url = "https://example.com/?ctx=abc.def.ghi"
        result = sanitize_url(url)
        assert "ctx=" in result.sanitized_url

    def test_jwt_inside_innocuous_param_name(self):
        # Dangerous-name denylist would miss "data="; JWT detector catches it.
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJ1c2VyIjoiZG9lIn0.kkkSIGNkkkk"
        url = f"https://api.example.com/v1?data={jwt}"
        result = sanitize_url(url)
        assert jwt not in result.sanitized_url
        assert "data" in result.redacted_param_names

    def test_jwt_in_fragment_param(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.signaturePart_001"
        url = f"https://example.com/cb#id_token={jwt}"
        result = sanitize_url(url)
        assert jwt not in result.sanitized_url

    def test_first_segment_eyj_only(self):
        # Second segment must also start eyJ.
        url = "https://example.com/?x=eyJhbGciOiJIUzI1NiJ9.something.signature"
        result = sanitize_url(url)
        # Not a JWT by our tighter pattern → kept.
        assert "x=" in result.sanitized_url

    def test_jwt_state_param_redacted_by_name_first(self):
        # `state` is in the denylist → should be removed regardless of JWT-shape.
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.sig_xyz"
        url = f"https://example.com/?state={jwt}"
        result = sanitize_url(url)
        assert jwt not in result.sanitized_url
        assert "state" in result.redacted_param_names


# ═══════════════════════════════════════════════════════════════════════
# Rule 4 — Path-as-credential
# ═══════════════════════════════════════════════════════════════════════

class TestRule4PathCredential:
    def test_slack_webhook_redacted(self):
        url = "https://hooks.slack.com/services/T123ABC/B456DEF/xyzSecret789"
        result = sanitize_url(url)
        assert result.path_redacted is True
        assert "xyzSecret789" not in result.sanitized_url
        assert "/services/[redacted]" in result.sanitized_url

    def test_slack_non_webhook_path_not_redacted(self):
        # Negative case — different path on the same host should pass through.
        url = "https://hooks.slack.com/healthz"
        result = sanitize_url(url)
        assert result.path_redacted is False
        assert "/healthz" in result.sanitized_url

    def test_discord_webhook_redacted(self):
        url = "https://discord.com/api/webhooks/123456789/ABCsecretToken"
        result = sanitize_url(url)
        assert result.path_redacted is True
        assert "ABCsecretToken" not in result.sanitized_url

    def test_discordapp_webhook_redacted(self):
        url = "https://discordapp.com/api/webhooks/123456789/ABCsecretToken"
        result = sanitize_url(url)
        assert result.path_redacted is True

    def test_discord_user_endpoint_not_redacted(self):
        # Negative — /api/users/123 is not a webhook.
        url = "https://discord.com/api/users/123"
        result = sanitize_url(url)
        assert result.path_redacted is False

    def test_telegram_bot_redacted(self):
        url = "https://api.telegram.org/bot123456:ABC-DEF1234ghIkl/getUpdates"
        result = sanitize_url(url)
        assert result.path_redacted is True
        assert "ABC-DEF1234" not in result.sanitized_url
        assert "/[redacted]" in result.sanitized_url

    def test_telegram_non_bot_path_not_redacted(self):
        url = "https://api.telegram.org/healthz"
        result = sanitize_url(url)
        assert result.path_redacted is False

    def test_magic_link_token_redacted(self):
        url = "https://app.example.com/magic-link/abc123def456ghi789jkl"
        result = sanitize_url(url)
        assert result.path_redacted is True
        assert "abc123def456ghi789jkl" not in result.sanitized_url
        assert "/magic-link/[redacted]" in result.sanitized_url

    def test_reset_password_token_redacted(self):
        url = "https://app.example.com/reset-password/abcdefghijklmnop1234"
        result = sanitize_url(url)
        assert result.path_redacted is True

    def test_short_segment_after_magic_link_not_redacted(self):
        # < 16 chars → not a credible token, pass through.
        url = "https://app.example.com/magic-link/short"
        result = sanitize_url(url)
        assert result.path_redacted is False

    def test_normal_path_not_redacted(self):
        url = "https://example.com/api/v1/users"
        result = sanitize_url(url)
        assert result.path_redacted is False


# ═══════════════════════════════════════════════════════════════════════
# Rule 5 — Scheme handling
# ═══════════════════════════════════════════════════════════════════════

class TestRule5Scheme:
    def test_http_allowed(self):
        result = sanitize_url("http://example.com/page")
        assert result.sanitized_url is not None
        assert result.parse_error is None

    def test_https_allowed(self):
        result = sanitize_url("https://example.com/page")
        assert result.sanitized_url is not None

    @pytest.mark.parametrize("rejected", [
        "javascript:alert(1)",
        "data:text/html,<h1>hi</h1>",
        "file:///etc/passwd",
        "vbscript:msgbox(1)",
    ])
    def test_rejected_schemes(self, rejected):
        result = sanitize_url(rejected)
        assert result.sanitized_url is None
        assert result.parse_error is not None
        assert "unsupported_scheme" in result.parse_error

    def test_ftp_unsupported(self):
        result = sanitize_url("ftp://files.example.com/pub")
        assert result.sanitized_url is None
        assert "unsupported_scheme" in (result.parse_error or "")

    def test_empty_scheme(self):
        result = sanitize_url("://example.com")
        assert result.sanitized_url is None

    def test_mailto_passthrough(self):
        result = sanitize_url("mailto:alice@example.com")
        assert result.sanitized_url == "mailto:alice@example.com"
        assert result.parse_error is None

    def test_tel_passthrough(self):
        result = sanitize_url("tel:+15551234567")
        assert result.sanitized_url == "tel:+15551234567"

    def test_sms_passthrough(self):
        result = sanitize_url("sms:+15551234567")
        assert result.sanitized_url == "sms:+15551234567"


# ═══════════════════════════════════════════════════════════════════════
# Rule 6 — Userinfo
# ═══════════════════════════════════════════════════════════════════════

class TestRule6Userinfo:
    def test_user_pass_stripped(self):
        result = sanitize_url("https://admin:password123@example.com/dashboard")
        assert result.userinfo_redacted is True
        assert "admin" not in result.sanitized_url
        assert "password123" not in result.sanitized_url
        assert "example.com/dashboard" in result.sanitized_url

    def test_user_only_stripped(self):
        result = sanitize_url("https://admin@example.com/")
        assert result.userinfo_redacted is True
        assert "admin@" not in result.sanitized_url

    def test_no_userinfo(self):
        result = sanitize_url("https://example.com/")
        assert result.userinfo_redacted is False


# ═══════════════════════════════════════════════════════════════════════
# Rule 7 — Fragment sanitization
# ═══════════════════════════════════════════════════════════════════════

class TestRule7Fragment:
    def test_access_token_in_fragment(self):
        url = "https://example.com/cb#access_token=secret123&token_type=bearer"
        result = sanitize_url(url)
        assert "secret123" not in result.sanitized_url
        assert "access_token" in result.redacted_param_names

    def test_tracking_in_fragment(self):
        url = "https://example.com/page#utm_source=twitter&section=intro"
        result = sanitize_url(url)
        assert "utm_source" not in result.sanitized_url
        assert "section=intro" in result.sanitized_url

    def test_normal_fragment_preserved(self):
        url = "https://example.com/page#section-2"
        result = sanitize_url(url)
        assert "section-2" in result.sanitized_url

    def test_jwt_in_fragment(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.abc123def456_xyz"
        url = f"https://example.com/#id_token={jwt}"
        result = sanitize_url(url)
        assert jwt not in result.sanitized_url


# ═══════════════════════════════════════════════════════════════════════
# Rule 8 — Normalization
# ═══════════════════════════════════════════════════════════════════════

class TestRule8Normalization:
    def test_scheme_lowercased(self):
        result = sanitize_url("HTTP://example.com/page")
        assert result.sanitized_url.startswith("http://")

    def test_host_lowercased(self):
        result = sanitize_url("https://Example.COM/Path")
        assert "example.com" in result.sanitized_url
        # Path case preserved.
        assert "/Path" in result.sanitized_url

    def test_default_http_port_stripped(self):
        result = sanitize_url("http://example.com:80/page")
        assert ":80" not in result.sanitized_url

    def test_default_https_port_stripped(self):
        result = sanitize_url("https://example.com:443/page")
        assert ":443" not in result.sanitized_url

    def test_nondefault_port_kept(self):
        result = sanitize_url("https://example.com:8443/page")
        assert ":8443" in result.sanitized_url

    def test_params_sorted(self):
        result = sanitize_url("https://example.com/?z=1&a=2&m=3")
        assert result.sanitized_url == "https://example.com/?a=2&m=3&z=1"

    def test_idn_to_punycode(self):
        # Naïve.example → xn--nave-6pa.example
        result = sanitize_url("https://Naïve.example/")
        assert "xn--" in result.sanitized_url

    def test_trailing_slash_preserved(self):
        with_slash = sanitize_url("https://example.com/foo/")
        without_slash = sanitize_url("https://example.com/foo")
        assert with_slash.sanitized_url.endswith("/foo/")
        assert without_slash.sanitized_url.endswith("/foo")


# ═══════════════════════════════════════════════════════════════════════
# Edge cases / total-function contract
# ═══════════════════════════════════════════════════════════════════════

class TestEdgeCases:
    def test_empty_string(self):
        r = sanitize_url("")
        assert r.sanitized_url is None and r.parse_error == "empty"

    def test_whitespace_only(self):
        r = sanitize_url("   ")
        assert r.sanitized_url is None and r.parse_error == "empty"

    def test_url_with_surrounding_whitespace_stripped(self):
        r = sanitize_url("  https://example.com/  ")
        assert r.sanitized_url == "https://example.com/"

    def test_no_query_no_fragment(self):
        r = sanitize_url("https://example.com/path/to/page")
        assert r.sanitized_url == "https://example.com/path/to/page"
        assert r.redacted_param_names == []
        assert r.path_redacted is False
        assert r.userinfo_redacted is False
        assert r.parse_error is None

    def test_complex_url_all_rules(self):
        url = (
            "HTTPS://user:pass@EXAMPLE.COM:443/page"
            "?token=secret&utm_source=fb&q=hello&z=1&a=2"
            "#access_token=leaked"
        )
        r = sanitize_url(url)
        assert r.sanitized_url is not None
        assert r.userinfo_redacted is True
        assert "user" not in r.sanitized_url
        assert "pass" not in r.sanitized_url
        assert r.sanitized_url.startswith("https://example.com/")
        assert ":443" not in r.sanitized_url
        assert "token=" not in r.sanitized_url
        assert "utm_source" not in r.sanitized_url
        assert "leaked" not in r.sanitized_url
        assert "token" in r.redacted_param_names
        assert "access_token" in r.redacted_param_names

    @pytest.mark.parametrize("garbage", [None, "", "not-a-url", "://", "\x00\x01\x02"])
    def test_never_raises_on_garbage(self, garbage):
        # Must not raise — returns SanitizationResult with parse_error or None.
        r = sanitize_url(garbage)
        assert isinstance(r, SanitizationResult)
        # Either we couldn't parse it (None) or it parsed cleanly.
        assert r.sanitized_url is None or isinstance(r.sanitized_url, str)

    def test_integer_input_does_not_raise(self):
        r = sanitize_url(123)  # type: ignore[arg-type]
        assert r.sanitized_url is None
        assert r.parse_error == "empty"

    def test_redacted_param_count_matches_list(self):
        r = sanitize_url("https://example.com/?token=x&password=y&q=z")
        assert r.redacted_param_count == len(r.redacted_param_names)
        assert r.redacted_param_count == 2

    def test_only_dangerous_params_yields_empty_query(self):
        r = sanitize_url("https://example.com/?token=x&api_key=y")
        # Either trailing `?` or no query — both acceptable, neither leaks.
        assert "token" not in r.sanitized_url
        assert "api_key" not in r.sanitized_url


# ═══════════════════════════════════════════════════════════════════════
# Negative cases — must pass through unchanged (over-strip regression guard)
# ═══════════════════════════════════════════════════════════════════════

class TestPassThroughCommonURLs:
    """Catch regressions that over-strip legitimate long IDs / params."""

    @pytest.mark.parametrize("url", [
        "https://en.wikipedia.org/wiki/Uniform_Resource_Locator",
        "https://github.com/anthropics/anthropic-sdk-python/blob/main/README.md",
        "https://news.ycombinator.com/item?id=12345678",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC",
        "https://docs.google.com/document/d/1abcDEF_xyz123/edit",
        "https://arxiv.org/abs/2401.12345",
        "https://stackoverflow.com/questions/1234567/how-do-i",
        "https://www.reddit.com/r/programming/comments/abc123/title/",
        "https://twitter.com/user/status/1234567890123456789",
        "https://medium.com/@author/article-slug-abc123",
        "https://www.amazon.com/dp/B08N5WRWNW",
        "https://www.npmjs.com/package/express",
        "https://crates.io/crates/tokio",
        "https://hub.docker.com/_/postgres",
    ])
    def test_legitimate_url_passes_through_with_no_redactions(self, url):
        r = sanitize_url(url)
        assert r.sanitized_url is not None, f"Failed to sanitize {url}"
        assert r.parse_error is None
        assert r.redacted_param_names == [], (
            f"Over-stripping on {url}: dropped {r.redacted_param_names}"
        )
        assert r.path_redacted is False, f"Wrong path-redact on {url}"
        assert r.userinfo_redacted is False
