"""Tool implementations for the Muninn MCP server.

Pure functions — no MCP decorators here so the same callables can be unit
tested without standing up a stdio transport. `server.py` wraps each one as a
FastMCP tool.

All output is JSON strings (so the MCP layer can hand them straight to the
caller). Schema is the canonical one in /schema.sql. The consumer contract
(every tool, its output shapes and the privacy rules) is ``docs/mcp-api.md``.

Visibility is enforced here, never trusted to an index: every bookmark a
search backend returns is re-read from SQLite with ``content_visible = 1``,
and every vault page carrying a ``bookmark_id`` is served only while that
row is visible.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from muninn.consumers.mcp import vault_fs
from muninn.consumers.vault import x_pages
from muninn.db import connect
from muninn.index.sources import (
    hidden_urls as _hidden_urls,
    visible_row as _visible_row,
    visible_summary as _summary,
    visible_title as _title,
)
from muninn.search import (
    DEFAULT_BACKEND,
    RRF_K,
    SearchBackend,
    SearchHit,
    get_search_backend,
    hybrid_search,
)


# ── helpers ────────────────────────────────────────────────────────


def _open(db_path: str | Path | None) -> sqlite3.Connection:
    return connect(db_path) if db_path else connect()


def _row_to_dict(row: sqlite3.Row | None) -> dict:
    return dict(row) if row else {}


def _item_kind(row: sqlite3.Row) -> str | None:
    """Contract item kind (``x_post``, ``x_article``, ``github_repo``,
    ``web_page``) for X-lane rows; None for browser bookmarks."""
    if row["source"] not in x_pages.X_SOURCES:
        return None
    return x_pages.item_kind_for(row)


def _bookmark_summary(
    conn: sqlite3.Connection, row: sqlite3.Row, score: float | None = None
) -> dict:
    """Compact bookmark view for search results."""
    hidden = _hidden_urls(conn, row)
    out = {
        "bookmark_id": row["bookmark_id"],
        "title": _title(row, hidden),
        "url": row["url"],
        "era_label": row["era_label"],
        "domain": row["domain"],
        "summary": _summary(row, hidden),
        "source": row["source"],
        "item_kind": _item_kind(row),
    }
    if score is not None:
        out["score"] = score
    return out


def _backend(
    backend: SearchBackend | None,
    db_path: str | Path | None,
    client=None,
) -> SearchBackend:
    """Resolve the search backend: explicit > legacy Qdrant client > config."""
    if backend is not None:
        return backend
    if client is not None:
        # Legacy hook: a bare Qdrant client means the default backend.
        return get_search_backend(DEFAULT_BACKEND, db_path=db_path, qdrant_client=client)
    return get_search_backend(db_path=db_path)


def _hydrate(
    db_path: str | Path | None,
    hits: list[SearchHit],
    *,
    with_score: bool,
) -> list[dict]:
    """Hydrate hits from SQLite, in hit order, dropping hidden/missing rows.

    This is the visibility gate for every backend: an index that doesn't
    know about ``content_visible`` can never leak a hidden bookmark.
    """
    conn = _open(db_path)
    try:
        output = []
        for hit in hits:
            row = _visible_row(conn, hit.bookmark_id)
            if row is not None:
                output.append(
                    _bookmark_summary(conn, row, score=hit.score if with_score else None)
                )
        return output
    finally:
        conn.close()


def _error(code: str, message: str) -> str:
    return json.dumps({"error": message, "code": code})


class _Visibility:
    """Lazy SQLite visibility checks for vault pages (one connection per
    tool call, opened only when something needs a check).

    Called with a :class:`vault_fs.PageInfo`, it answers whether the page
    may be served:

    - a page with a ``bookmark_id`` only while that row is visible
      (a stale compiled page for a now-hidden row is withheld);
    - a page in ``wiki/bookmarks/`` without one is not a compiled page,
      and that namespace serves compiled pages only;
    - any other page (the authored lane) always.

    If the database can't be opened, pages that need a check are
    withheld (fail closed).
    """

    def __init__(self, db_path: str | Path | None) -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._failed = False
        self._rows: dict[int, sqlite3.Row | None] = {}

    @property
    def conn(self) -> sqlite3.Connection | None:
        if self._conn is None and not self._failed:
            try:
                self._conn = _open(self._db_path)
            except Exception:  # noqa: BLE001 — missing/behind DB ⇒ withhold
                self._failed = True
        return self._conn

    def row(self, bookmark_id: int) -> sqlite3.Row | None:
        if bookmark_id not in self._rows:
            conn = self.conn
            self._rows[bookmark_id] = _visible_row(conn, bookmark_id) if conn else None
        return self._rows[bookmark_id]

    def __call__(self, info: vault_fs.PageInfo) -> bool:
        return vault_fs.page_visible(
            info.namespace, info.bookmark_id, lambda bid: self.row(bid) is not None
        )

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


def _hidden_neighbour_urls(conn: sqlite3.Connection, bookmark_id: int) -> set[str]:
    """Locators of hidden items linked to this row in either direction
    (a compiled X page lists its children *and* the items linking to it)."""
    if not x_pages.has_table(conn, "item_links"):
        return set()
    return {
        r["url"]
        for r in conn.execute(
            "SELECT url FROM bookmarks WHERE content_visible = 0 "
            "AND url IS NOT NULL AND url != '' AND bookmark_id IN ("
            "  SELECT child_bookmark_id FROM item_links WHERE parent_bookmark_id = ? "
            "  UNION SELECT parent_bookmark_id FROM item_links WHERE child_bookmark_id = ?)",
            (bookmark_id, bookmark_id),
        )
    }


_WIKILINK_RE = re.compile(r"\[\[([^\]|\n]+)(?:\|[^\]\n]*)?\]\]")


def _compiled_page_file(wiki: Path, slug: str) -> Path | None:
    """``wiki/bookmarks/<slug>.md`` if ``slug`` is a plain file name there."""
    if not slug or "/" in slug or "\\" in slug or slug.startswith("."):
        return None
    file = wiki / vault_fs.CAPTURED_NAMESPACE / f"{slug}.md"
    return file if file.is_file() and not file.is_symlink() else None


def _compiled_page_id(file: Path) -> int | None:
    try:
        meta, _ = vault_fs.split_frontmatter(vault_fs.read_head(file))
    except OSError:
        return None
    return vault_fs.bookmark_id_of(meta)


def _mask_compiled_page(wiki: Path, vis: _Visibility, bookmark_id: int, text: str) -> str:
    """Withhold what a stale compiled page could still say about hidden rows.

    Pages are served as last compiled. Until the next ``muninn vault
    compile``, a visible page can still quote a now-hidden neighbour's
    locator or link to its page. Locators of hidden linked items become
    ``[hidden link]``, and so does every wikilink whose target is not a
    currently visible compiled bookmark page (compiled pages link only to
    each other, so this fails closed).
    """
    conn = vis.conn
    if conn is not None:
        text = x_pages.mask_hidden_links(text, _hidden_neighbour_urls(conn, bookmark_id))

    verdicts: dict[str, bool] = {}

    def allowed(slug: str) -> bool:
        if slug not in verdicts:
            file = _compiled_page_file(wiki, slug.strip())
            bid = _compiled_page_id(file) if file is not None else None
            verdicts[slug] = bid is not None and vis.row(bid) is not None
        return verdicts[slug]

    return _WIKILINK_RE.sub(
        lambda m: m.group(0) if allowed(m.group(1)) else x_pages.HIDDEN_LINK_TEXT, text
    )


def _compiled_path(wiki: Path | None, row: sqlite3.Row) -> str | None:
    """Vault path of a visible row's compiled page, if it has been compiled.

    Recomputes the compiler's slug (an X-lane slug may carry its
    ``-b<bookmark_id>`` collision suffix) and confirms the file's
    frontmatter names this row; ``None`` without a vault or a page.
    """
    if wiki is None:
        return None
    bid = row["bookmark_id"]
    if row["source"] in x_pages.X_SOURCES:
        base = x_pages.x_slug(row)
        slugs = [base, f"{base}-b{bid}"]
    else:
        from muninn.consumers.vault.compiler import _slug_for  # lazy: pulls in Jinja

        slugs = [_slug_for(row)]
    for slug in slugs:
        file = _compiled_page_file(wiki, slug)
        if file is not None and _compiled_page_id(file) == bid:
            return f"wiki/{vault_fs.CAPTURED_NAMESPACE}/{slug}.md"
    return None


# ── tools ──────────────────────────────────────────────────────────


def semantic_search(
    query: str,
    limit: int = 10,
    db_path: str | Path | None = None,
    client=None,
    backend: SearchBackend | None = None,
) -> str:
    """Search bookmarks by semantic similarity via the search backend.

    The default backend embeds the query locally with the same model (and
    asymmetric query prompt) used to index documents, then queries Qdrant.
    Falls back to keyword search if the semantic index is unavailable or
    returns nothing. Result rows are hydrated from SQLite so the schema is
    canonical. ``client`` injects a Qdrant client into the default backend.
    """
    search = _backend(backend, db_path, client)
    try:
        hits = search.semantic(query, limit)
        if not hits:
            return fts_search(query, limit, db_path=db_path, backend=search)
        return json.dumps(_hydrate(db_path, hits, with_score=True), indent=2)
    except Exception:
        return fts_search(query, limit, db_path=db_path, backend=search)


def fts_search(
    query: str,
    limit: int = 10,
    db_path: str | Path | None = None,
    backend: SearchBackend | None = None,
) -> str:
    """Keyword search via the search backend (FTS5 ``fts_bookmarks`` by default)."""
    hits = _backend(backend, db_path).keyword(query, limit)
    return json.dumps(_hydrate(db_path, hits, with_score=False), indent=2)


def get_bookmark(bookmark_id: int, db_path: str | Path | None = None) -> str:
    """Full bookmark view — bookmarks JOIN enriched plus per-pass scrape rows.

    Hidden bookmarks (`content_visible = 0`) are indistinguishable from
    missing IDs: same not-found error, so MCP callers can't enumerate them.
    """
    conn = _open(db_path)
    try:
        row = conn.execute(
            "SELECT b.*, e.summary, e.tags, e.entities, e.content_type, "
            "       e.language, e.word_count, e.enrichment_model, "
            "       e.enrichment_prompt_version, e.enriched_at, "
            "       e.deep_pass_requested, e.key_quotes "
            "FROM bookmarks b LEFT JOIN enriched e "
            "  ON e.bookmark_id = b.bookmark_id "
            "WHERE b.bookmark_id = ? AND b.content_visible = 1",
            (bookmark_id,),
        ).fetchone()
        if row is None:
            return json.dumps({"error": f"Bookmark {bookmark_id} not found"})

        scrape = conn.execute(
            "SELECT pass, scrape_status, fetched_at, http_status, "
            "       extraction_quality, error_detail "
            "FROM scrape_results WHERE bookmark_id = ? ORDER BY fetched_at",
            (bookmark_id,),
        ).fetchall()

        out = _row_to_dict(row)
        hidden = _hidden_urls(conn, row)
        out["title"] = _title(row, hidden)
        if hidden:
            out["summary"], out["key_quotes"], out["entities"], withheld = (
                x_pages.withhold_enrichment(
                    hidden, out.get("summary"), out.get("key_quotes"), out.get("entities")
                )
            )
            if withheld:
                out["enrichment_withheld"] = True
        for key in ("summary", "key_quotes", "entities", "tags"):  # JSON text or prose
            if hidden and isinstance(out.get(key), str):
                out[key] = x_pages.mask_hidden_links(out[key], hidden)
        out["item_kind"] = _item_kind(row)
        out["scrape_results"] = [_row_to_dict(s) for s in scrape]
        return json.dumps(out, indent=2, default=str)
    finally:
        conn.close()


def get_era(era_label: str, db_path: str | Path | None = None) -> str:
    """Era narrative + dominant topics/domains by `era_label` PK."""
    conn = _open(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM eras WHERE era_label = ?", (era_label,)
        ).fetchone()
        if row is None:
            return json.dumps({"error": f"Era '{era_label}' not found"})
        return json.dumps(_row_to_dict(row), indent=2, default=str)
    finally:
        conn.close()


def list_eras(db_path: str | Path | None = None) -> str:
    """All known eras with bookmark counts (live, not the cached
    `eras.bookmark_count`) so re-classified folders are reflected."""
    conn = _open(db_path)
    try:
        rows = conn.execute(
            "SELECT e.era_label, e.inferred_year, e.start_date, e.end_date, "
            "       e.narrative, e.dominant_topics, e.dominant_domains, "
            "       (SELECT COUNT(*) FROM bookmarks "
            "          WHERE era_label = e.era_label) AS bookmark_count "
            "FROM eras e ORDER BY e.start_date NULLS LAST, e.era_label"
        ).fetchall()
        return json.dumps(
            [_row_to_dict(r) for r in rows], indent=2, default=str
        )
    finally:
        conn.close()


# ── recall over the whole vault (Phase 2) ──────────────────────────

#: Upper bound on ``search(top_k=…)``.
SEARCH_MAX_TOP_K = 50
#: Hybrid hits requested per result slot, so hidden rows dropped at
#: hydration don't leave the captured lane short.
_CAPTURED_HEADROOM = 2


def _unknown_namespace(namespace: object) -> str:
    return _error(
        "unknown_namespace",
        f"Unknown namespace {namespace!r}; expected one of {list(vault_fs.NAMESPACES)}",
    )


def _merge_lanes(lanes: list[list[dict]], top_k: int) -> list[dict]:
    """Interleave per-lane rankings by reciprocal rank, ``1 / (RRF_K +
    rank)``; ties go to the earlier lane (captured before authored)."""
    scored = [
        (1.0 / (RRF_K + rank), lane_index, rank, item)
        for lane_index, lane in enumerate(lanes)
        for rank, item in enumerate(lane, start=1)
    ]
    scored.sort(key=lambda t: (-t[0], t[1], t[2]))
    out = []
    for score, _, _, item in scored[:top_k]:
        item["score"] = score
        out.append(item)
    return out


def search(
    query: str,
    namespace: str | None = None,
    top_k: int = 10,
    db_path: str | Path | None = None,
    backend: SearchBackend | None = None,
) -> str:
    """Hybrid recall across the vault's namespaces.

    - ``bookmarks`` (captured lane): the search backend's hybrid ranking
      (keyword + semantic, reciprocal rank fusion by default; keyword
      only when the semantic index is unavailable), hydrated from SQLite.
    - ``concepts``, ``infra``, ``people``, ``projects`` (authored lane):
      a bounded keyword scan of ``<vault>/wiki/<namespace>/``.

    ``namespace=None`` searches all of them and interleaves the two lanes
    by rank. Output shape: ``docs/mcp-api.md``.
    """
    if not isinstance(query, str) or not query.strip():
        return _error("invalid_query", "query is empty")
    if namespace is not None and namespace not in vault_fs.NAMESPACES:
        return _unknown_namespace(namespace)
    top_k = max(1, min(int(top_k), SEARCH_MAX_TOP_K))
    want_captured = namespace in (None, vault_fs.CAPTURED_NAMESPACE)
    if namespace is None:
        authored: tuple[str, ...] = vault_fs.AUTHORED_NAMESPACES
    elif namespace in vault_fs.AUTHORED_NAMESPACES:
        authored = (namespace,)
    else:
        authored = ()

    warnings: list[str] = []
    wiki: Path | None = None
    try:
        wiki = vault_fs.vault_wiki_dir()
    except vault_fs.VaultAccessError as exc:
        if namespace in vault_fs.AUTHORED_NAMESPACES:
            return _error(exc.code, str(exc))
        if authored:
            warnings.append(f"authored namespaces not searched: {exc}")

    vis = _Visibility(db_path)
    try:
        captured: list[dict] = []
        if want_captured:
            hits = hybrid_search(_backend(backend, db_path), query, top_k * _CAPTURED_HEADROOM)
            for hit in hits:
                row = vis.row(hit.bookmark_id)
                if row is None:
                    continue
                item = {"namespace": vault_fs.CAPTURED_NAMESPACE, "path": _compiled_path(wiki, row)}
                item.update(_bookmark_summary(vis.conn, row))
                item["matched"] = hit.index.split("+")
                captured.append(item)
                if len(captured) >= top_k:
                    break

        pages: list[dict] = []
        if authored and wiki is not None:
            scan = vault_fs.scan_namespaces(wiki, authored, query, visible=vis)
            if scan.truncated:
                warnings.append(
                    "authored-lane scan stopped at its file/byte bound; results may be incomplete"
                )
            for page in scan.hits[:top_k]:
                pages.append(
                    {
                        "namespace": page.info.namespace,
                        "path": page.info.path,
                        "title": page.info.title,
                        "summary": page.info.summary,
                        "matched": ["vault-scan"],
                    }
                )
        results = _merge_lanes([captured, pages], top_k)
    finally:
        vis.close()
    return json.dumps(
        {"query": query, "namespace": namespace, "results": results, "warnings": warnings},
        indent=2,
    )


def get_page(path: str, db_path: str | Path | None = None) -> str:
    """One ``wiki/*.md`` page from ``$MUNINN_VAULT_DIR``, by vault-relative path.

    Refused: an unset vault or one overlapping the personal vault; a path
    that is absolute, outside ``wiki/``, not ``.md``, has ``..`` or dot
    components, or resolves (through symlinks) outside ``wiki/``; a page
    over the size cap. A compiled page whose row is hidden or gone gets
    the same ``not_found`` error as a missing file.
    """
    try:
        wiki = vault_fs.vault_wiki_dir()
        file, text = vault_fs.read_page(wiki, path)
    except vault_fs.VaultAccessError as exc:
        return _error(exc.code, str(exc))

    # Visibility follows the file actually read, not the name asked for.
    info = vault_fs.page_info(wiki, "wiki/" + file.relative_to(wiki).as_posix(), text)
    vis = _Visibility(db_path)
    try:
        if not vis(info):
            return _error("not_found", str(vault_fs.not_found(path)))
        title = info.title
        if info.bookmark_id is not None:
            row = vis.row(info.bookmark_id)
            title = _title(row, _hidden_urls(vis.conn, row)) or title
            text = _mask_compiled_page(wiki, vis, info.bookmark_id, text)
    finally:
        vis.close()
    return json.dumps(
        {
            "path": path,
            "namespace": vault_fs.namespace_of(path),
            "title": title,
            "bookmark_id": info.bookmark_id,
            "size_bytes": len(text.encode("utf-8")),
            "content": text,
        },
        indent=2,
    )


def list_namespace(
    namespace: str,
    limit: int = vault_fs.LIST_DEFAULT_LIMIT,
    cursor: str | None = None,
    db_path: str | Path | None = None,
) -> str:
    """Pages in ``wiki/<namespace>/`` (recursive, path order) with a
    one-line title and summary each, ``limit`` per call; pass the
    returned ``next_cursor`` back for the next page (``null`` at the end).

    Compiled bookmark pages are listed only while their row is visible,
    with title and summary from SQLite (masked like search results).
    """
    if namespace not in vault_fs.NAMESPACES:
        return _unknown_namespace(namespace)
    limit = max(1, min(int(limit), vault_fs.LIST_MAX_LIMIT))
    if cursor is not None and (
        not isinstance(cursor, str)
        or len(cursor) > vault_fs.MAX_PATH_CHARS
        or not cursor.startswith(f"wiki/{namespace}/")
    ):
        return _error("invalid_cursor", "cursor must be a next_cursor from this namespace")
    try:
        wiki = vault_fs.vault_wiki_dir()
    except vault_fs.VaultAccessError as exc:
        return _error(exc.code, str(exc))

    vis = _Visibility(db_path)
    try:
        infos, next_cursor = vault_fs.list_pages(
            wiki, namespace, limit=limit, cursor=cursor, visible=vis
        )
        pages = []
        for info in infos:
            title, summary = info.title, info.summary
            if info.bookmark_id is not None:
                row = vis.row(info.bookmark_id)
                hidden = _hidden_urls(vis.conn, row)
                title = _title(row, hidden) or title
                if row["summary"]:
                    summary = _summary(row, hidden)
                elif summary and hidden:
                    summary, _, _, _ = x_pages.withhold_enrichment(hidden, summary, None, None)
            pages.append(
                {
                    "path": info.path,
                    "title": vault_fs.one_line(title),
                    "summary": vault_fs.one_line(summary) if summary else None,
                    "bookmark_id": info.bookmark_id,
                }
            )
    finally:
        vis.close()
    return json.dumps({"namespace": namespace, "pages": pages, "next_cursor": next_cursor}, indent=2)


__all__ = [
    "semantic_search",
    "fts_search",
    "get_bookmark",
    "get_era",
    "list_eras",
    "search",
    "get_page",
    "list_namespace",
]
