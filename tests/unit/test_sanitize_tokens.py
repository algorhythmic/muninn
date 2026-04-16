"""Token-leakage prevention tests for sanitize_url() + looks_like_jwt().

Complements test_sanitize_url.py by asserting credential-shaped strings
NEVER survive sanitization. If a single one of these regresses, secrets
are committed to the DB and propagated downstream — see SPEC.md
§"Defense-in-depth" (line 1423).
"""

from __future__ import annotations

import pytest

from muninn.sanitize import sanitize_url
from muninn.sanitize.rules import DANGEROUS_PARAM_NAMES
from muninn.sanitize.tokens import JWT_PATTERN, looks_like_jwt


class TestLooksLikeJWT:
    @pytest.mark.parametrize("jwt", [
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.SflKxwRJSMeKKF2QT4fwpMeJf36",
        "eyJ0eXAiOiJKV1QifQ.eyJpYXQiOjE3MDB9.signature_with_underscores_AB",
        "eyJabc.eyJdef.signature-part-with-dashes_and_under",
    ])
    def test_recognizes_jwt_shape(self, jwt):
        assert looks_like_jwt(jwt) is True

    @pytest.mark.parametrize("not_jwt", [
        "",
        "not-a-jwt",
        "abc.def.ghi",
        "eyJonly.onepart",
        "eyJonepart.eyJtwopart",
        "header.eyJpayload.signature",  # missing first eyJ
        "eyJabc.payload.signature",     # second segment doesn't start eyJ
    ])
    def test_rejects_non_jwt(self, not_jwt):
        assert looks_like_jwt(not_jwt) is False

    def test_non_string_input_returns_false(self):
        assert looks_like_jwt(None) is False  # type: ignore[arg-type]
        assert looks_like_jwt(12345) is False  # type: ignore[arg-type]

    def test_pattern_is_anchored(self):
        # Surrounding noise should not cause a positive match by `match()`.
        prefixed = "garbage_eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.signature"
        assert looks_like_jwt(prefixed) is False
        # But the pattern itself can be `search()`'d for substring matching.
        assert JWT_PATTERN.search(prefixed) is None  # leading underscore breaks anchor


class TestNoCredentialEverSurvives:
    """For each dangerous param name, an obviously-secret value must not
    appear anywhere in the sanitized output."""

    SECRET = "LEAKED_SECRET_VALUE_12345"

    @pytest.mark.parametrize("param_name", sorted(DANGEROUS_PARAM_NAMES))
    def test_each_dangerous_param_redacted(self, param_name):
        url = f"https://example.com/page?{param_name}={self.SECRET}&safe=ok"
        r = sanitize_url(url)
        assert self.SECRET not in (r.sanitized_url or ""), (
            f"Secret leaked for {param_name}"
        )
        assert param_name.lower() in r.redacted_param_names

    def test_oauth_callback_access_token_in_fragment(self):
        url = (
            "https://app.example.com/oauth/callback"
            "#access_token=ya29.a0AfH6SMBxLEAKED&token_type=Bearer"
        )
        r = sanitize_url(url)
        assert "ya29.a0AfH6SMBxLEAKED" not in (r.sanitized_url or "")
        assert "access_token" in r.redacted_param_names

    def test_oauth_callback_access_token_in_query(self):
        url = "https://app.example.com/oauth/cb?access_token=ya29.a0AfH6SMBxLEAKED"
        r = sanitize_url(url)
        assert "ya29.a0AfH6SMBxLEAKED" not in (r.sanitized_url or "")

    def test_session_cookie_in_url(self):
        url = "https://example.com/dashboard?session_id=abc123def456&view=main"
        r = sanitize_url(url)
        assert "abc123def456" not in (r.sanitized_url or "")

    def test_api_key_mixed_case(self):
        url = "https://api.example.com/v1/data?API_KEY=sk-12345abcdef&format=json"
        r = sanitize_url(url)
        assert "sk-12345abcdef" not in (r.sanitized_url or "")

    def test_bearer_token_in_query(self):
        url = "https://api.example.com/?bearer=eyToken123leaked&action=list"
        r = sanitize_url(url)
        assert "eyToken123leaked" not in (r.sanitized_url or "")

    def test_jwt_in_state_param(self):
        jwt = "eyJhbGciOiJSUzI1NiJ9.eyJpc3MiOiJodHRwczovL2EuY29tIn0.signature_HERE"
        url = f"https://example.com/login?state={jwt}&nonce=abc"
        r = sanitize_url(url)
        assert jwt not in (r.sanitized_url or "")

    def test_jwt_in_custom_param(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJ1c2VyX2lkIjoxMjN9.kWOVtIx2mUDjZbAj4gY3HA"
        url = f"https://example.com/?ctx={jwt}"
        r = sanitize_url(url)
        assert jwt not in (r.sanitized_url or "")
        assert "ctx" in r.redacted_param_names

    def test_slack_webhook_full_path_redacted(self):
        url = "https://hooks.slack.com/services/T00000000/B00000000/XXXXXXXXXXXXXXXX"
        r = sanitize_url(url)
        assert "XXXXXXXXXXXXXXXX" not in (r.sanitized_url or "")
        assert r.path_redacted is True

    def test_discord_webhook_token_redacted(self):
        url = (
            "https://discord.com/api/webhooks/1234567890/"
            "abcdefghijklmnopqrstuvwxyz1234567890ABCDEF"
        )
        r = sanitize_url(url)
        assert "abcdefghijklmnopqrstuvwxyz" not in (r.sanitized_url or "")
        assert r.path_redacted is True

    def test_telegram_bot_token_redacted(self):
        url = "https://api.telegram.org/bot110201543:AAHdqTcvCH1vGWJxfSeofSAs0K5/sendMessage"
        r = sanitize_url(url)
        assert "AAHdqTcvCH1vGWJxfSeofSAs0K5" not in (r.sanitized_url or "")

    def test_userinfo_credentials_stripped(self):
        url = "https://deploy:ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx@github.com/o/r"
        r = sanitize_url(url)
        assert "deploy" not in (r.sanitized_url or "")
        assert "ghp_" not in (r.sanitized_url or "")
        assert r.userinfo_redacted is True

    def test_basic_auth_stripped_nondefault_port(self):
        url = "http://admin:supersecret@internal.corp.example:8080/api"
        r = sanitize_url(url)
        assert "admin" not in (r.sanitized_url or "")
        assert "supersecret" not in (r.sanitized_url or "")
        assert ":8080" in r.sanitized_url

    def test_multiple_jwt_values_all_caught(self):
        jwt1 = "eyJhbGciOiJIUzI1NiJ9.eyJhIjoiMSJ9.signature_for_first_token"
        jwt2 = "eyJhbGciOiJIUzI1NiJ9.eyJiIjoiMiJ9.signature_for_second_token"
        url = f"https://example.com/?a={jwt1}&b={jwt2}&page=1"
        r = sanitize_url(url)
        assert jwt1 not in (r.sanitized_url or "")
        assert jwt2 not in (r.sanitized_url or "")
        assert "page=1" in r.sanitized_url

    def test_dangerous_param_in_fragment_with_equals(self):
        url = "https://example.com/page#token=secret123&session_id=abcSecretSession"
        r = sanitize_url(url)
        assert "secret123" not in (r.sanitized_url or "")
        assert "abcSecretSession" not in (r.sanitized_url or "")
