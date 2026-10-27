"""FastMCP server exposing the Muninn corpus and vault (contract: docs/mcp-api.md).

Tools are thin wrappers around `tools.py` callables. Run via:

    muninn mcp                                  # stdio (default)
    muninn mcp --transport http [--host H] [--port P] [--allowed-host NAME]...

…or directly (stdio):

    python -m muninn.consumers.mcp.server

The HTTP transport is FastMCP's streamable HTTP, served at ``/mcp``. It
has no authentication layer: it binds ``127.0.0.1`` unless ``--host`` is
given, and must only ever be reachable on the tailnet.

Search goes through the retrieval interface (:mod:`muninn.search`,
ADR-014): the backend comes from ``$MUNINN_SEARCH_BACKEND`` unless one is
injected with :func:`set_search_backend`.
"""

from __future__ import annotations

import ipaddress
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from muninn.search import SearchBackend

from . import tools as _tools


mcp = FastMCP("muninn")

# Injected backend (tests, embedding callers); None ⇒ resolve from config
# on every call, as the tools do.
_search_backend: SearchBackend | None = None


def set_search_backend(backend: SearchBackend | None) -> None:
    """Pin the search backend for this server; ``None`` restores config."""
    global _search_backend
    _search_backend = backend


@mcp.tool()
def semantic_search(query: str, limit: int = 10) -> str:
    """Semantic similarity search over the bookmark corpus.

    Falls back to FTS when Qdrant is unreachable or the collection is empty.
    """
    return _tools.semantic_search(query, limit, backend=_search_backend)


@mcp.tool()
def fts_search(query: str, limit: int = 10) -> str:
    """Full-text search against the FTS5 index of titles, summaries,
    scraped content_text, and tags."""
    return _tools.fts_search(query, limit, backend=_search_backend)


@mcp.tool()
def get_bookmark(bookmark_id: int) -> str:
    """Fetch a bookmark by its INTEGER primary key, with enriched columns
    and the per-pass scrape_results child rows."""
    return _tools.get_bookmark(bookmark_id)


@mcp.tool()
def get_era(era_label: str) -> str:
    """Fetch an era narrative by its TEXT primary key (era_label)."""
    return _tools.get_era(era_label)


@mcp.tool()
def list_eras() -> str:
    """List all eras with live bookmark counts."""
    return _tools.list_eras()


@mcp.tool()
def search(query: str, namespace: str | None = None, top_k: int = 10) -> str:
    """Hybrid (keyword + semantic) recall over the vault, best first.

    namespace: omit for all; "bookmarks" for saved material (X posts,
    articles, repos, browser bookmarks); "concepts", "infra", "people" or
    "projects" for curated notes. Project notes are reference only: Odin
    owns project state. Each result has a vault "path" for get_page when
    a page exists, and bookmark results a "bookmark_id" for get_bookmark.
    """
    return _tools.search(query, namespace, top_k, backend=_search_backend)


@mcp.tool()
def search_context(query: str, namespaces: list[str] | None = None, budget: int = 2500,
                   max_bytes: int = 16384, anchors: list[str] | None = None,
                   required_sources: list[str] | None = None,
                   expected_generation: str | None = None) -> str:
    """Return exact cited excerpts in complete attribution bundles.

    Defaults: 2500 estimated cost units, 16384 UTF-8 JSON context bytes.
    References are bookmark:N or vault:wiki/<authored namespace>/<page>.md.
    An expected generation is a consistency precondition; it never silently
    switches to legacy recall. The diagnostic envelope has a separate cap.
    """
    return _tools.search_context(query, namespaces, budget, max_bytes, anchors,
                                 required_sources, expected_generation)


@mcp.tool()
def get_page(path: str) -> str:
    """Fetch one vault page by its vault-relative path, e.g.
    "wiki/concepts/example.md" (as returned by search or list_namespace)."""
    return _tools.get_page(path)


@mcp.tool()
def list_namespace(namespace: str, limit: int = 50, cursor: str | None = None) -> str:
    """List pages in a vault namespace ("bookmarks", "concepts", "infra",
    "people", "projects") with a one-line summary each. Pass the returned
    next_cursor back for the next page; it is null at the end."""
    return _tools.list_namespace(namespace, limit, cursor)


# ── transports ─────────────────────────────────────────────────────

#: CLI transport name → FastMCP (mcp 1.x) transport name.
TRANSPORTS = {"stdio": "stdio", "http": "streamable-http"}
DEFAULT_HTTP_HOST = "127.0.0.1"
#: FastMCP's own default port.
DEFAULT_HTTP_PORT = 8000
_LOOPBACK_NAMES = ("127.0.0.1", "localhost", "[::1]")

NO_AUTH_WARNING = (
    "WARNING: the muninn MCP HTTP endpoint has NO authentication. Expose it on "
    "the tailnet only (bind the Tailscale address, or keep 127.0.0.1 behind "
    "`tailscale serve`); never on a LAN or public interface."
)


class TransportConfigError(ValueError):
    """Transport options that can't be honoured (a CLI usage error)."""


@dataclass(frozen=True)
class TransportPlan:
    """What :func:`main` will run, worked out before anything binds."""

    transport: str  # FastMCP's name: "stdio" or "streamable-http"
    host: str | None = None
    port: int | None = None
    security: TransportSecuritySettings | None = None
    notices: list[str] = field(default_factory=list)

    @property
    def url(self) -> str | None:
        if self.transport == "stdio" or self.host is None:
            return None
        return f"http://{_host_header(self.host)}:{self.port}{mcp.settings.streamable_http_path}"


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _is_wildcard(host: str) -> bool:
    try:
        return ipaddress.ip_address(host.strip("[]")).is_unspecified
    except ValueError:
        return False


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


def _host_header(host: str) -> str:
    """``host`` as it appears in a Host header (IPv6 literals bracketed)."""
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def plan_transport(
    transport: str = "stdio",
    *,
    host: str | None = None,
    port: int | None = None,
    allowed_hosts: Sequence[str] = (),
) -> TransportPlan:
    """Validate transport options and build the HTTP security settings.

    FastMCP's streamable HTTP transport rejects a request whose ``Host``
    header isn't allowed (DNS-rebinding protection; mcp only turns it on
    by itself for a loopback host given at construction). The allowed
    names are the bind address (plus the loopback names for a loopback
    bind) and every ``allowed_hosts`` entry: the MagicDNS name or address
    a client, or ``tailscale serve``, will put in ``Host``. A wildcard
    bind (``0.0.0.0``/``::``) has no usable name of its own, so it
    requires ``allowed_hosts``.
    """
    if transport not in TRANSPORTS:
        raise TransportConfigError(
            f"unknown transport {transport!r}; expected one of {sorted(TRANSPORTS)}"
        )
    if transport == "stdio":
        if host is not None or port is not None or allowed_hosts:
            raise TransportConfigError(
                "--host, --port and --allowed-host apply to --transport http only"
            )
        return TransportPlan(transport="stdio")

    host = (host if host is not None else DEFAULT_HTTP_HOST).strip()
    port = port if port is not None else DEFAULT_HTTP_PORT
    if not host or "/" in host or (":" in host and not _is_ip(host)):
        raise TransportConfigError(
            f"invalid --host {host!r}; give an address or host name to bind, no port"
        )
    if not 0 <= port <= 65535:
        raise TransportConfigError(f"--port {port} is out of range")
    names: list[str] = []
    if _is_wildcard(host):
        if not allowed_hosts:
            raise TransportConfigError(
                f"binding {host} needs --allowed-host NAME (the tailnet name or "
                "address clients connect to); requests naming any other host "
                "are rejected"
            )
    else:
        names.append(_host_header(host))
    if _is_loopback(host):
        names.extend(n for n in _LOOPBACK_NAMES if n not in names)
    for raw in allowed_hosts:
        name = raw.strip()
        if not name or "/" in name or "*" in name or (":" in name and not _is_ip(name)):
            raise TransportConfigError(
                f"invalid --allowed-host {raw!r}; give a bare host name or address, no port"
            )
        name = _host_header(name)
        if name not in names:
            names.append(name)
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[pattern for n in names for pattern in (n, f"{n}:*")],
        allowed_origins=[
            origin
            for n in names
            for scheme in ("http", "https")
            for origin in (f"{scheme}://{n}", f"{scheme}://{n}:*")
        ],
    )
    notices = [NO_AUTH_WARNING] if not _is_loopback(host) or allowed_hosts else []
    return TransportPlan(
        transport=TRANSPORTS[transport],
        host=host,
        port=port,
        security=security,
        notices=notices,
    )


def main(
    transport: str = "stdio",
    *,
    host: str | None = None,
    port: int | None = None,
    allowed_hosts: Sequence[str] = (),
) -> None:
    """Entry point for `muninn mcp`. Notices go to stderr: on stdio,
    stdout is the protocol stream."""
    plan = plan_transport(transport, host=host, port=port, allowed_hosts=allowed_hosts)
    if plan.transport != "stdio":
        mcp.settings.host = plan.host
        mcp.settings.port = plan.port
        mcp.settings.transport_security = plan.security
        for notice in plan.notices:
            print(notice, file=sys.stderr)
        print(f"muninn MCP (streamable HTTP) at {plan.url}", file=sys.stderr)
    mcp.run(transport=plan.transport)


if __name__ == "__main__":
    main()
