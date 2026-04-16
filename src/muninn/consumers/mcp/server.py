"""FastMCP server (stdio) exposing the Muninn corpus.

Tools are thin wrappers around `tools.py` callables. Run via:

    muninn mcp

…or directly:

    python -m muninn.consumers.mcp.server
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from . import tools as _tools


mcp = FastMCP("muninn")


@mcp.tool()
def semantic_search(query: str, limit: int = 10) -> str:
    """Semantic similarity search over the bookmark corpus.

    Falls back to FTS when Qdrant is unreachable or the collection is empty.
    """
    return _tools.semantic_search(query, limit)


@mcp.tool()
def fts_search(query: str, limit: int = 10) -> str:
    """Full-text search against the FTS5 index of titles, summaries,
    scraped content_text, and tags."""
    return _tools.fts_search(query, limit)


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


def main() -> None:
    """Entry point for `muninn mcp`."""
    mcp.run()


if __name__ == "__main__":
    main()
