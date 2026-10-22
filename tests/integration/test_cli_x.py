"""CLI surface for the X slice: both `ingest` forms, `ingest x`, `x auth`,
the friendly schema-version error, and DuckDB readers refusing a behind DB.

No network (respx), no credentials: every token and secret is synthetic.
"""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import stat
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
import respx
from click.testing import CliRunner

from muninn.consumers.cli.main import cli
from muninn.db import SchemaVersionError
from muninn.ingest.x import JsonFileTokenStore, XTokens

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "tests" / "fixtures" / "x"
HTML = ROOT / "tests" / "fixtures" / "bookmarks.html"
API = "https://api.x.com/2"
TOKEN_URL = f"{API}/oauth2/token"
BOOKMARKS = f"{API}/users/9000000001/bookmarks"
PAGES = {None: "bookmarks_page1", "synthetic_next_token_page2": "bookmarks_page2",
         "synthetic_next_token_page3": "bookmarks_page3"}
SECRET_CANARY = "synthetic-client-secret-CANARY-0001"


def fx(name: str) -> httpx.Response:
    meta = json.loads((FIX / f"{name}.headers.json").read_text())
    return httpx.Response(meta["status"], headers=meta["headers"],
                          json=json.loads((FIX / f"{name}.json").read_text()))


@pytest.fixture
def x_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MUNINN_X_CLIENT_ID", "synthetic-client-id")
    monkeypatch.setenv("MUNINN_X_REDIRECT_URI", "http://127.0.0.1:8765/callback")
    monkeypatch.setenv("MUNINN_X_TOKEN_FILE", str(tmp_path / "x-tokens.json"))
    monkeypatch.delenv("MUNINN_X_CLIENT_SECRET", raising=False)
    return tmp_path / "x-tokens.json"


def invoke(db_path, *args, input=None):
    return CliRunner().invoke(cli, ["--db", str(db_path), *args], input=input)


# ── ingest: both forms ──────────────────────────────────────────────────

def test_ingest_path_and_ingest_html_path_are_the_same_command(tmp_path) -> None:
    from muninn.db import init_db

    outputs = []
    for args in (["ingest", str(HTML)], ["ingest", "html", str(HTML)]):
        db = tmp_path / f"{len(outputs)}.db"
        init_db(db).close()
        result = invoke(db, *args)
        assert result.exit_code == 0, result.output
        outputs.append(result.output.replace(str(HTML), "<path>"))
        conn = sqlite3.connect(db)
        assert conn.execute("SELECT COUNT(*) FROM bookmarks WHERE source = 'bookmarks_html'").fetchone()[0] > 0
        conn.close()
    assert outputs[0] == outputs[1] and outputs[0].startswith("Ingested <path>: parsed=")


def test_ingest_missing_path_is_a_usage_error(db_path) -> None:
    result = invoke(db_path, "ingest", "no-such-file.html")
    assert result.exit_code == 2 and "does not exist" in result.output


def test_ingest_help_lists_both_forms(db_path) -> None:
    result = invoke(db_path, "ingest", "--help")
    assert result.exit_code == 0 and "html" in result.output and "x" in result.output


# ── ingest x ────────────────────────────────────────────────────────────

def test_ingest_x_dry_run_prints_the_bound_and_makes_no_call(db_path, x_env) -> None:
    with respx.mock(assert_all_called=False) as mock:
        route = mock.route()
        result = invoke(db_path, "ingest", "x", "--dry-run")
    assert result.exit_code == 0, result.output
    assert "up to 10 page(s) x 100 posts: about $1.000" in result.output
    assert "$0.001 per resource" in result.output and "Dry run" in result.output
    assert not route.called
    assert sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM sync_checkpoints").fetchone()[0] == 0

    unbounded = invoke(db_path, "ingest", "x", "--all", "--dry-run", "--resume")
    assert "no page bound" in unbounded.output and "Nothing to resume." in unbounded.output


def test_ingest_x_rejects_all_with_max_pages(db_path, x_env) -> None:
    result = invoke(db_path, "ingest", "x", "--all", "--max-pages", "2")
    assert result.exit_code == 2 and "mutually exclusive" in result.output


def test_ingest_x_without_tokens_points_at_x_auth(db_path, x_env) -> None:
    result = invoke(db_path, "ingest", "x")
    assert result.exit_code == 1 and "run `muninn x auth` first" in result.output


def test_ingest_x_syncs_and_reports_cost(db_path, x_env) -> None:
    JsonFileTokenStore(x_env).save(
        XTokens(access_token="synthetic-access-token-0001", refresh_token="synthetic-refresh-token-0001",
                expires_at=4_000_000_000)
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{API}/users/me").mock(return_value=fx("users_me"))
        mock.get(BOOKMARKS).mock(
            side_effect=lambda req: fx(PAGES[req.url.params.get("pagination_token")])
        )
        result = invoke(db_path, "ingest", "x", "--max-pages", "2")
        assert result.exit_code == 0, result.output
        assert "paused (page_budget)" in result.output and "pages=2 posts=6 new=6" in result.output
        assert "estimated cost $0.012" in result.output
        assert "muninn ingest x --resume" in result.output

        resumed = invoke(db_path, "ingest", "x", "--resume")
    assert resumed.exit_code == 0, resumed.output
    assert "completed (end_of_list)" in resumed.output and "pages=1 posts=2" in resumed.output
    for out in (result.output, resumed.output):
        assert "synthetic-access-token" not in out and "synthetic_next_token" not in out


# ── x auth ──────────────────────────────────────────────────────────────

@pytest.fixture
def fixed_randomness(monkeypatch):
    import muninn.ingest.x as xmod

    monkeypatch.setattr(xmod.secrets, "token_urlsafe", lambda n: f"synthetic-random-{n}")


def test_x_auth_exchanges_the_pasted_redirect_and_saves_tokens(db_path, x_env, monkeypatch, fixed_randomness) -> None:
    monkeypatch.setenv("MUNINN_X_CLIENT_SECRET", SECRET_CANARY)
    redirect = "http://127.0.0.1:8765/callback?state=synthetic-random-32&code=synthetic-auth-code"
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(TOKEN_URL).mock(return_value=fx("token_exchange"))
        result = invoke(db_path, "x", "auth", input=redirect + "\n")
    assert result.exit_code == 0, result.output
    assert "https://x.com/i/oauth2/authorize?" in result.output and "code_challenge=" in result.output
    assert f"Saved X tokens (with refresh token) to {x_env} (mode 0600)." in result.output
    for secret in (SECRET_CANARY, "synthetic-access-token-0001", "synthetic-refresh-token-0001",
                   "synthetic-auth-code", "synthetic-random-64"):
        assert secret not in result.output

    request = route.calls[0].request
    form = parse_qs(request.content.decode())
    assert form["code"] == ["synthetic-auth-code"] and form["code_verifier"] == ["synthetic-random-64"]
    basic = base64.b64encode(f"synthetic-client-id:{SECRET_CANARY}".encode()).decode()
    assert request.headers["authorization"] == f"Basic {basic}"
    assert stat.S_IMODE(os.stat(x_env).st_mode) == 0o600
    saved = JsonFileTokenStore(x_env).load()
    assert saved.access_token == "synthetic-access-token-0001"
    assert saved.refresh_token == "synthetic-refresh-token-0001"


def test_x_auth_accepts_a_bare_code(db_path, x_env, fixed_randomness) -> None:
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(TOKEN_URL).mock(return_value=fx("token_exchange"))
        result = invoke(db_path, "x", "auth", input="synthetic-auth-code\n")
    assert result.exit_code == 0, result.output
    assert parse_qs(route.calls[0].request.content.decode())["client_id"] == ["synthetic-client-id"]


def test_x_auth_rejects_a_state_mismatch_and_a_failed_exchange(db_path, x_env, fixed_randomness) -> None:
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(TOKEN_URL).mock(return_value=fx("token_error_invalid_grant"))
        mismatch = invoke(db_path, "x", "auth",
                          input="http://127.0.0.1:8765/callback?state=other&code=c\n")
        assert mismatch.exit_code == 1 and "State mismatch" in mismatch.output
        assert not route.called
        failed = invoke(db_path, "x", "auth", input="synthetic-auth-code\n")
    assert failed.exit_code == 1 and "Token exchange failed" in failed.output
    assert "synthetic-auth-code" not in failed.output and not x_env.exists()


def test_x_auth_needs_client_settings(db_path, x_env, monkeypatch) -> None:
    monkeypatch.delenv("MUNINN_X_REDIRECT_URI")
    result = invoke(db_path, "x", "auth", input="code\n")
    assert result.exit_code == 1 and "MUNINN_X_REDIRECT_URI is not set" in result.output


# ── schema version ──────────────────────────────────────────────────────

@pytest.fixture
def behind_db(db_path) -> Path:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA user_version = 1")
    conn.close()
    return db_path


@pytest.mark.parametrize("args", [["status"], ["ingest", str(HTML)], ["scrape"], ["timeline"],
                                  ["export", "parquet", "--out", "unused.parquet"]])
def test_behind_db_is_a_friendly_error_for_every_command(behind_db, args) -> None:
    result = invoke(behind_db, *args)
    assert result.exit_code == 1, result.output
    assert "Error: Database schema is v1 but this muninn needs v2: run `muninn db migrate`." in result.output
    assert "Traceback" not in result.output


def test_duckdb_readers_refuse_a_behind_db(behind_db, tmp_path) -> None:
    from muninn.consumers.parquet.export import export_parquet
    from muninn.consumers.timeline.builder import build_timeline

    with pytest.raises(SchemaVersionError, match="muninn db migrate"):
        build_timeline(db_path=behind_db)
    with pytest.raises(SchemaVersionError, match="muninn db migrate"):
        export_parquet(tmp_path / "out.parquet", db_path=behind_db)
    assert not (tmp_path / "out.parquet").exists()
    with pytest.raises(SchemaVersionError, match="No muninn database"):
        build_timeline(db_path=tmp_path / "missing.db")
    assert not (tmp_path / "missing.db").exists()
