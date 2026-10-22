# Muninn MCP API

This is the contract for MCP consumers of Muninn: Odin (as its optional `recall` scope), Claude Code sessions, the Claude.ai remote connector, and an operator debugging the server. The code lives in `src/muninn/consumers/mcp/` (`server.py` holds the FastMCP wrappers, `tools.py` the implementations, `vault_fs.py` the vault file access).

**Muninn is recall only.** It serves saved material and curated notes. It holds no canonical project or session state (ADR-011): Odin owns commitments, project state and decisions, and pages in `wiki/projects/` are operator-written reference notes. A consumer that needs project truth asks Odin, not Muninn. Neither side reads the other's database.

All examples below are synthetic.

## Running the server

```
muninn mcp                                   # stdio (the default)
muninn mcp --transport http                  # streamable HTTP on 127.0.0.1:8000/mcp
muninn mcp --transport http --host 100.64.0.7 --port 8000
muninn mcp --transport http --allowed-host muninn.example.ts.net   # behind `tailscale serve`
```

| Option | Default | Meaning |
|---|---|---|
| `--transport stdio\|http` | `stdio` | `http` is FastMCP's *streamable HTTP* transport (mcp 1.x, verified against 1.30). The endpoint path is `/mcp`. |
| `--host H` | `127.0.0.1` | HTTP bind address. Only valid with `--transport http`. |
| `--port P` | `8000` | HTTP port (FastMCP's default; `0` lets the OS pick). Only valid with `--transport http`. |
| `--allowed-host NAME` | none | Repeatable. An extra `Host` header value to accept (a bare name or address, no port). |

**The HTTP endpoint has no authentication.** It must only ever be reachable on the tailnet: either bind the machine's Tailscale address with `--host`, or keep the default `127.0.0.1` and put `tailscale serve` in front of it. Muninn does not configure Tailscale. A non-loopback `--host`, or any `--allowed-host`, prints a warning to stderr saying so. Never bind a LAN or public interface.

**Host-header checks.** FastMCP's DNS-rebinding protection is always on for HTTP. A request whose `Host` header isn't allowed gets `421 Invalid Host header`. The allowed names are:

- the bind address (an IPv6 literal is bracketed, e.g. `[fd7a::1]`);
- `127.0.0.1`, `localhost` and `[::1]` when the bind address is loopback;
- every `--allowed-host`, e.g. the MagicDNS name that clients or `tailscale serve` put in `Host`.

Each name is accepted with or without a port, and the matching `http://` and `https://` origins are allowed. A wildcard bind (`0.0.0.0` or `::`) has no usable name of its own, so it is refused unless `--allowed-host` is given.

Notices go to stderr. On stdio, stdout is the protocol stream.

### Configuration

The server reads its configuration from the environment (and the repo `.env`, where real environment variables win). The paths and the backend choice are read on every call. `QDRANT_URL` and the embedding settings are read when the server starts.

| Variable | Used for |
|---|---|
| `MUNINN_DB_PATH` (or `MUNINN_DATA_DIR`) | The canonical SQLite store. Every visibility check reads it. |
| `MUNINN_VAULT_DIR` | The compiled vault root (the `muninn-vault` checkout). `get_page`, `list_namespace` and the authored half of `search` need it. |
| `MUNINN_PERSONAL_VAULT_DIR` | The operator's personal vault. If it equals, contains or sits inside `MUNINN_VAULT_DIR`, the server refuses to serve the vault at all (SPEC Decision 2, the compiler's guard). |
| `MUNINN_SEARCH_BACKEND` | The retrieval backend (ADR-014). The default is `fts5-qdrant`. |
| `MUNINN_EMBEDDING_BACKEND`, `QDRANT_URL` | Semantic search for the default backend. |

## Namespaces

The vault is `<MUNINN_VAULT_DIR>/wiki/`, and its namespaces are its top-level directories (ADR-001, as amended by ADR-011):

| Namespace | Lane | Contents | Searched by |
|---|---|---|---|
| `bookmarks` | captured | Saved material: X posts, X articles, linked repos and web pages, and browser bookmarks. The vault compiler compiles these from SQLite, and the item's source is in its frontmatter. | The search backend (FTS5 + Qdrant), hydrated from SQLite |
| `concepts` | authored | Curated concept notes | A bounded keyword scan of the files |
| `infra` | authored | Homelab and infrastructure notes | A bounded keyword scan of the files |
| `people` | authored | People notes | A bounded keyword scan of the files |
| `projects` | authored | Operator reference notes. They are **not canonical**: Odin owns project state. | A bounded keyword scan of the files |

`sessions/` is retired and is not a namespace. Authored pages live only in the vault (git is canonical); they are not in SQLite or in any search index.

## Privacy and visibility

These rules hold for every tool:

- **Backends return ids; the server enforces visibility.** A search backend returns bookmark ids and scores only. Every id is re-read from SQLite with `content_visible = 1` before anything is returned. An index that knows nothing about visibility (Qdrant) can't leak a hidden row, and neither can a stale index entry for a deleted row.
- **A hidden bookmark is indistinguishable from a missing one.** `get_bookmark` returns the same `not found` error for both, and so does `get_page` (`code: "not_found"`).
- **A vault page that carries a `bookmark_id` in its frontmatter is served only while that row is visible.** This applies in every namespace. A compiled page left on disk for a row hidden since the last `muninn vault compile` is never served, listed or returned by search. If the database can't be opened, such pages are withheld (fail closed).
- **`wiki/bookmarks/` serves compiled pages only.** A file there without a `bookmark_id` is not served.
- **Stale compiled pages are masked.** Pages are served as last compiled. In a compiled page, the server:
  - replaces with `[hidden link]` the locators of hidden items linked to it (`item_links`, in either direction);
  - replaces with `[hidden link]` every wikilink whose target isn't a currently visible compiled bookmark page.

  Titles and summaries in results come from SQLite and are masked the same way the vault masks them. Recompiling after a policy change is still the fix; the masking is defence in depth.
- **The personal vault is never read.** A vault directory that overlaps it is refused outright. A symlink resolving into it, or anywhere outside `<vault>/wiki/`, is refused.
- **Refusals carry no host paths.** Vault and path errors never name a filesystem location.

## Tools

Every tool returns a JSON string. An error is an object with `error` (a human-readable message) and, for the tools added in Phase 2, `code` (stable and machine-readable).

### `search(query, namespace=None, top_k=10)`

Hybrid recall across the vault. This is the main entry point for consumers.

- `query` is natural language or keywords. It must not be empty.
- `namespace` is `None` (all namespaces), `"bookmarks"`, or one authored namespace.
- `top_k` is clamped to 1–50.

How it ranks:

- **Captured lane** (`bookmarks`): the backend's hybrid ranking. Backends that fuse natively implement `hybrid`; every other backend gets reciprocal rank fusion (RRF) of `keyword` and `semantic`, where each bookmark scores `Σ 1/(60 + rank)` over the two rankings (`RRF_K = 60`, Cormack et al. 2009, in `muninn.search.hybrid`). Each ranking is fetched twice as deep as needed.
  - If the semantic index is unavailable (`SearchUnavailable`, e.g. Qdrant down), the lane falls back to keyword only, and the server logs a warning.
  - The default backend retries a query that isn't valid FTS5 syntax as its quoted word tokens, so punctuation in natural language doesn't fail the call.
  - Hidden rows are dropped at hydration.
- **Authored lane**: a keyword scan of `wiki/<namespace>/**/*.md`. A page matches when it contains every word of the query (case-insensitive, whole words). Its score is the number of occurrences, with a title occurrence counting 5.
  - The scan is bounded at 2,000 files, 256 KiB per file and 32 MiB in total per call; hitting a bound adds a warning.
  - It skips symlinks and dot-files.
  - FTS operators (`AND`, `OR`, `NOT`, `NEAR`) are ignored.
- **Merging**: the two lanes are interleaved by reciprocal rank (`1/(60 + rank within lane)`), with ties going to the captured lane. `score` is that final value: higher is better, and it is comparable only within one response.

If `MUNINN_VAULT_DIR` is unset or refused and `namespace` is `None`, the search returns bookmark results only, with a warning. For an explicit authored namespace, it returns the vault error instead.

```json
{
  "query": "retrieval evaluation",
  "namespace": null,
  "results": [
    {
      "namespace": "bookmarks",
      "path": "wiki/bookmarks/x-synth-author-1900000000000000001.md",
      "bookmark_id": 42,
      "title": "@synth_author: Thread on evaluating retrieval",
      "url": "https://x.com/synth_author/status/1900000000000000001",
      "era_label": null,
      "domain": "x.com",
      "summary": "A synthetic thread about retrieval benchmarks.",
      "source": "x",
      "item_kind": "x_post",
      "matched": ["fts5", "qdrant"],
      "score": 0.01639344262295082
    },
    {
      "namespace": "concepts",
      "path": "wiki/concepts/retrieval-evaluation.md",
      "title": "Retrieval evaluation",
      "summary": "Recall, MRR and nDCG over labelled queries.",
      "matched": ["vault-scan"],
      "score": 0.01639344262295082
    }
  ],
  "warnings": []
}
```

Fields of a bookmark result:

- `path` is the compiled page (for `get_page`), or `null` if there's no vault or the row hasn't been compiled yet.
- `bookmark_id` is for `get_bookmark`.
- `source` is e.g. `x`, `x_article`, `x_link` or `netscape`.
- `item_kind` is the Research Intake contract kind (`x_post`, `x_article`, `github_repo`, `web_page`), or `null` for browser bookmarks.
- `matched` lists the indexes that ranked it (`fts5`, `qdrant`, or a custom backend's names).

An authored result has `namespace`, `path`, `title`, `summary`, `matched` (`["vault-scan"]`) and `score`.

Error codes are `invalid_query`, `unknown_namespace`, `vault_unconfigured` and `vault_conflict`.

### `get_page(path)`

This returns one page.

`path` is vault-relative, exactly as `search` and `list_namespace` return it, e.g. `wiki/concepts/retrieval-evaluation.md`. It must:

- start with `wiki/` and end in `.md`;
- contain no empty, `.`, `..` or dot-prefixed component, and no backslash or NUL;
- not be absolute (no `/…`, `~…` or `C:…`);
- be at most 512 characters.

After symlinks are resolved, the file must still be a `.md` file inside `<vault>/wiki/`. Files over 1 MiB are refused. Pages at the `wiki/` root (e.g. `wiki/index.md`) can be fetched, but are not a namespace.

```json
{
  "path": "wiki/concepts/retrieval-evaluation.md",
  "namespace": "concepts",
  "title": "Retrieval evaluation",
  "bookmark_id": null,
  "size_bytes": 812,
  "content": "---\ntitle: Retrieval evaluation\n---\n\n# Retrieval evaluation\n\n…"
}
```

- `content` is the whole page, frontmatter included. For a compiled page it is masked as described above.
- `title` comes from SQLite for a compiled page. For any other page it comes from the frontmatter `title`, else the first heading, else the file name.

Error codes:

- `invalid_path`: the path is malformed, or it resolves outside `wiki/`.
- `not_found`: the page is missing, or it is a hidden or uncompiled bookmark page. These two cases are deliberately indistinguishable.
- `too_large`
- `vault_unconfigured`
- `vault_conflict`

### `list_namespace(namespace, limit=50, cursor=None)`

This lists the pages in `wiki/<namespace>/`, in path order, including subdirectories. Symlinks and dot-files are skipped. `limit` is clamped to 1–200.

```json
{
  "namespace": "concepts",
  "pages": [
    {
      "path": "wiki/concepts/consensus.md",
      "title": "Consensus",
      "summary": "Agreement among replicas despite failures.",
      "bookmark_id": null
    }
  ],
  "next_cursor": "wiki/concepts/consensus.md"
}
```

**Summaries.** Each summary is one line of at most 200 characters, taken from the first of these that exists:

1. the frontmatter `summary` (or `description`);
2. the first prose line, which skips headings, blockquote banners such as the `projects/` "Reference, not canonical" banner, comments, rules, tables and code fences;
3. the first heading.

For a compiled bookmark page, the title and summary come from SQLite instead, masked.

**Pagination.** Pass `next_cursor` back as `cursor` to get the next page; it is `null` at the end. The cursor is the last path examined, so pages added or removed between calls don't cause repeats or skips before the cursor. Hidden bookmark pages are skipped and don't count towards `limit`. A call stops after examining 1,000 files, so a page of results can be short with a non-null cursor; keep paging.

A namespace directory that doesn't exist lists as empty. Error codes are `unknown_namespace`, `invalid_cursor` (a cursor not from this namespace), `vault_unconfigured` and `vault_conflict`.

### Bookmark-level tools (unchanged)

These predate Phase 2. Their outputs are unchanged, and their errors carry only `error`.

- **`semantic_search(query, limit=10)`**: a list of bookmark results. Each has `bookmark_id`, `title`, `url`, `era_label`, `domain`, `summary`, `source`, `item_kind` and `score` (Qdrant similarity). It falls back to `fts_search` (no `score`) when semantic search raises or returns nothing.
- **`fts_search(query, limit=10)`**: the same list without `score`. `query` is FTS5 MATCH syntax, and invalid syntax is an error.
- **`get_bookmark(bookmark_id)`**: the full row. It contains the `bookmarks` columns and the `enriched` columns (`summary`, `tags`, `entities`, `content_type`, `language`, `word_count`, `enrichment_model`, `enrichment_prompt_version`, `enriched_at`, `deep_pass_requested`, `key_quotes`), plus `item_kind` and `scrape_results` (one row per pass: `pass`, `scrape_status`, `fetched_at`, `http_status`, `extraction_quality`, `error_detail`). If the row is hidden or missing, it returns `{"error": "Bookmark N not found"}`.
- **`get_era(era_label)`**: an `eras` row, or `{"error": "Era '…' not found"}`.
- **`list_eras()`**: all eras, with live bookmark counts.

Prefer `search` for recall. The bookmark-level tools stay for bookmark-specific drill-down (`get_bookmark` after a `search` hit) and for existing clients.

## Errors and failure behaviour

- **Tool-level refusals** (bad input, a refused path, a vault problem) are JSON error objects, as above, never exceptions.
- **Infrastructure failures** are raised, and FastMCP reports them as tool errors:
  - a missing or outdated database (`run muninn db migrate`);
  - an unknown `MUNINN_SEARCH_BACKEND`;
  - a non-semantic backend failure.
- **An unavailable semantic index** (`SearchUnavailable`) never fails a search. `search` and `semantic_search` both degrade to keyword search.

## Operator debugging

- **Calling the tools directly.** The tools are plain functions, so you can call them from Python, e.g. `python -c 'from muninn.consumers.mcp import tools; print(tools.search("raft", "concepts"))'`.
- **The stdio server.** Run it under any MCP client or inspector, e.g. `npx @modelcontextprotocol/inspector muninn mcp`.
- **The HTTP endpoint.** Check it with an `initialize` request:

  ```
  curl -s http://127.0.0.1:8000/mcp -H 'content-type: application/json' \
    -H 'accept: application/json, text/event-stream' \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
  ```

  A `421 Invalid Host header` response means the name the client used isn't allowed; add it with `--allowed-host`.
- **Missing results.** If a result you expected is missing, check these in order:
  1. Is the row visible (`content_visible`)?
  2. Is the semantic index up (look for the stderr warning "semantic index unavailable")?
  3. For authored pages, does the page contain every query word, and did the scan hit a bound (`warnings`)?
- **Missing `path` on a bookmark result.** The vault hasn't been compiled since the row arrived; run `muninn vault compile`.
