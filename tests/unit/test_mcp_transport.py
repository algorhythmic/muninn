"""`muninn mcp` transport options: stdio by default, streamable HTTP on request.

Nothing binds a port: ``FastMCP.run`` is replaced with a recorder, and the
server's settings are copied per test so the module-level server is left
as it was.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from muninn.consumers.cli.main import cli
from muninn.consumers.mcp import server
from muninn.consumers.mcp.server import (
    NO_AUTH_WARNING,
    TransportConfigError,
    plan_transport,
)


@pytest.fixture
def runs(monkeypatch):
    calls = []
    monkeypatch.setattr(server.mcp, "settings", server.mcp.settings.model_copy(deep=True))
    monkeypatch.setattr(server.mcp, "run", lambda **kw: calls.append(kw))
    return calls


# ── plan_transport ──────────────────────────────────────────────────


def test_stdio_is_the_default():
    plan = plan_transport()
    assert plan.transport == "stdio"
    assert plan.host is None and plan.security is None and plan.notices == []


@pytest.mark.parametrize(
    "kwargs", [{"host": "127.0.0.1"}, {"port": 9000}, {"allowed_hosts": ["a.example"]}]
)
def test_http_options_rejected_on_stdio(kwargs):
    with pytest.raises(TransportConfigError, match="--transport http"):
        plan_transport("stdio", **kwargs)


def test_unknown_transport():
    with pytest.raises(TransportConfigError, match="unknown transport"):
        plan_transport("sse")


def test_http_defaults_to_loopback_and_fastmcp_port():
    plan = plan_transport("http")
    assert plan.transport == "streamable-http"
    assert (plan.host, plan.port) == ("127.0.0.1", 8000)
    assert plan.url == "http://127.0.0.1:8000/mcp"
    assert plan.notices == []  # loopback only: no warning
    sec = plan.security
    assert sec.enable_dns_rebinding_protection
    assert {"127.0.0.1:*", "localhost:*", "[::1]:*"} <= set(sec.allowed_hosts)


def test_http_tailnet_host_warns_and_allows_only_that_host():
    plan = plan_transport("http", host="100.64.0.7", port=8765)
    assert plan.notices == [NO_AUTH_WARNING]
    assert plan.url == "http://100.64.0.7:8765/mcp"
    assert plan.security.allowed_hosts == ["100.64.0.7", "100.64.0.7:*"]
    assert "http://100.64.0.7:*" in plan.security.allowed_origins
    assert "localhost:*" not in plan.security.allowed_hosts


def test_http_allowed_host_for_tailscale_serve_warns():
    plan = plan_transport("http", allowed_hosts=["muninn.tailnet-example.ts.net"])
    assert plan.host == "127.0.0.1"
    assert plan.notices == [NO_AUTH_WARNING]
    assert {"muninn.tailnet-example.ts.net", "muninn.tailnet-example.ts.net:*"} <= set(
        plan.security.allowed_hosts
    )
    assert "https://muninn.tailnet-example.ts.net" in plan.security.allowed_origins


def test_http_ipv6_hosts_are_bracketed():
    plan = plan_transport("http", host="fd7a:115c:a1e0::1", port=0)
    assert plan.security.allowed_hosts == ["[fd7a:115c:a1e0::1]", "[fd7a:115c:a1e0::1]:*"]
    assert plan.url == "http://[fd7a:115c:a1e0::1]:0/mcp"
    assert plan_transport("http", host="::1").notices == []


@pytest.mark.parametrize("host", ["0.0.0.0", "::"])
def test_http_wildcard_bind_needs_allowed_host(host):
    with pytest.raises(TransportConfigError, match="--allowed-host"):
        plan_transport("http", host=host)
    plan = plan_transport("http", host=host, allowed_hosts=["100.64.0.7"])
    assert plan.security.allowed_hosts == ["100.64.0.7", "100.64.0.7:*"]
    assert plan.notices == [NO_AUTH_WARNING]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"host": ""},
        {"host": "example.test:8000"},
        {"host": "http://example.test"},
        {"port": 70000},
        {"port": -1},
        {"allowed_hosts": ["muninn.example.test:443"]},
        {"allowed_hosts": ["*"]},
        {"allowed_hosts": ["https://muninn.example.test"]},
        {"allowed_hosts": [" "]},
    ],
)
def test_http_rejects_bad_values(kwargs):
    with pytest.raises(TransportConfigError):
        plan_transport("http", **kwargs)


# ── main() and the CLI ──────────────────────────────────────────────


def test_main_stdio_runs_without_touching_settings(runs):
    before = server.mcp.settings.model_dump()
    server.main()
    assert runs == [{"transport": "stdio"}]
    assert server.mcp.settings.model_dump() == before


def test_main_http_applies_plan_before_run(runs, capsys):
    server.main("http", host="100.64.0.7", port=0)
    assert runs == [{"transport": "streamable-http"}]
    settings = server.mcp.settings
    assert (settings.host, settings.port) == ("100.64.0.7", 0)
    assert settings.transport_security.allowed_hosts == ["100.64.0.7", "100.64.0.7:*"]
    err = capsys.readouterr()
    assert err.out == ""  # nothing on stdout
    assert NO_AUTH_WARNING in err.err and "http://100.64.0.7:0/mcp" in err.err


def test_cli_defaults_to_stdio(runs):
    result = CliRunner().invoke(cli, ["mcp"])
    assert result.exit_code == 0, result.output
    assert runs == [{"transport": "stdio"}]


def test_cli_http_options(runs):
    result = CliRunner().invoke(
        cli,
        ["mcp", "--transport", "http", "--port", "0", "--allowed-host", "muninn.example.ts.net"],
    )
    assert result.exit_code == 0, result.output
    assert runs == [{"transport": "streamable-http"}]
    assert server.mcp.settings.host == "127.0.0.1"
    assert server.mcp.settings.port == 0
    assert "muninn.example.ts.net:*" in server.mcp.settings.transport_security.allowed_hosts
    assert "NO authentication" in result.output  # CliRunner mixes stderr in


def test_plan_security_gates_host_headers_on_the_installed_fastmcp():
    """The plan's settings on a throwaway FastMCP's streamable-HTTP app
    (Starlette TestClient: no socket). A tailnet name passes; a foreign
    Host header, or localhost on a tailnet bind, is refused."""
    from mcp.server.fastmcp import FastMCP
    from starlette.testclient import TestClient

    plan = plan_transport("http", host="100.64.0.7", allowed_hosts=["muninn.example.ts.net"])
    probe = FastMCP("probe", host=plan.host, port=plan.port, transport_security=plan.security)
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "probe", "version": "0"},
        },
    }
    headers = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
    statuses = {}
    with TestClient(probe.streamable_http_app()) as client:
        for host in ("100.64.0.7:8000", "muninn.example.ts.net", "evil.example", "localhost:8000"):
            response = client.post("/mcp", json=init, headers={**headers, "host": host})
            statuses[host] = response.status_code
    assert statuses == {
        "100.64.0.7:8000": 200,
        "muninn.example.ts.net": 200,
        "evil.example": 421,
        "localhost:8000": 421,
    }


@pytest.mark.parametrize(
    "args, message",
    [
        (["--host", "100.64.0.7"], "--transport http"),
        (["--transport", "http", "--host", "0.0.0.0"], "--allowed-host"),
        (["--transport", "sse"], "Invalid value"),
        (["--transport", "http", "--port", "99999"], "Invalid value"),
    ],
)
def test_cli_usage_errors_do_not_run(runs, args, message):
    result = CliRunner().invoke(cli, ["mcp", *args])
    assert result.exit_code == 2
    assert message in result.output
    assert runs == []
