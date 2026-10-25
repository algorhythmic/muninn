"""Vault compiler — one Obsidian-compatible markdown page per visible bookmark.

Reads bookmarks + enriched rows from the canonical SQLite store and renders
them through `templates/bookmark_page.md.j2` into the unified vault's
`wiki/bookmarks/` namespace (ADR-001/ADR-008: the output dir is the vault
ROOT; this compiler owns — and only writes — `wiki/bookmarks/` beneath it).
Hidden bookmarks (`content_visible = 0`) are skipped entirely — no page is
written and no wikilink target is registered — and each run then prunes
compiled pages it did not write (a row hidden since the last run, or a
moved slug). Pruning only touches files carrying the compiler's
frontmatter marker, never hand-written ones.

Current findings of the model-tier lint (``lint_findings``: contradictions
and superseded claims) are added to the pages they concern; see
:mod:`muninn.consumers.vault.lint_notes`. A page without one is unchanged.

X-lane rows (`source` in `x`, `x_article`, `x_link`) compile into the same
namespace through `templates/x_item_page.md.j2`, with bidirectional links
from `item_links`; see :mod:`muninn.consumers.vault.x_pages`. Both templates
render persisted `cross_references` in both directions, separately from
era/tag suggestions. Only visible compiled targets get links; stored
relationship prose is not rendered. Browser bookmark slugs are unchanged.

CRITICAL: this module enforces SPEC.md Decision 2 (two distinct vaults — the
COMPILED Muninn output vault must NEVER be the same as the user's PERSONAL
input vault). The guard runs before any file is written.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from muninn.config import load_paths
from muninn.consumers.vault import x_pages
from muninn.consumers.vault.cross_refs import load_cross_refs
from muninn.consumers.vault.lint_notes import apply_lint_notes, load_lint_notes
from muninn.db import connect


TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
TEMPLATE_NAME = "bookmark_page.md.j2"
X_TEMPLATE_NAME = "x_item_page.md.j2"


class VaultPathConflictError(RuntimeError):
    """Raised when MUNINN_VAULT_DIR overlaps MUNINN_PERSONAL_VAULT_DIR.

    Per SPEC Decision 2, the compiled output vault and the personal input
    vault must be distinct directories with no nesting in either direction.
    """


def _validate_vault_paths(output_dir: Path, personal_dir: Path | None) -> None:
    """Refuse to compile when output and personal vaults overlap.

    - Identical resolved paths → conflict.
    - Either path nested inside the other → conflict.
    - personal_dir is None → no comparison; allow opt-out.
    """
    if personal_dir is None:
        return  # personal vault not configured; nothing to compare against
    out = output_dir.resolve()
    pers = personal_dir.resolve()
    if out == pers:
        raise VaultPathConflictError(
            f"MUNINN_VAULT_DIR ({out}) is the same as MUNINN_PERSONAL_VAULT_DIR ({pers}). "
            f"Per SPEC Decision 2: two vaults, never one."
        )
    # Also reject when one is inside the other:
    try:
        out.relative_to(pers)
        raise VaultPathConflictError(
            f"MUNINN_VAULT_DIR ({out}) is INSIDE MUNINN_PERSONAL_VAULT_DIR ({pers}). "
            f"Refusing to write."
        )
    except ValueError:
        pass
    try:
        pers.relative_to(out)
        raise VaultPathConflictError(
            f"MUNINN_PERSONAL_VAULT_DIR ({pers}) is INSIDE MUNINN_VAULT_DIR ({out}). "
            f"Refusing to write."
        )
    except ValueError:
        pass


# ── helpers ────────────────────────────────────────────────────────


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(value: str) -> str:
    """Filesystem-safe slug for a vault page filename."""
    s = _SLUG_RE.sub("-", value.lower()).strip("-")
    return s or "untitled"


def _slug_for(row: sqlite3.Row) -> str:
    """Stable slug for a bookmark row. Always includes the bookmark_id so
    pages remain unique even when titles collide or are missing."""
    title = (row["title"] or "").strip()
    base = _slugify(title) if title else "bookmark"
    return f"{base}-{row['bookmark_id']}"


def _parse_json_list(val: str | None) -> list:
    if not val:
        return []
    try:
        parsed = json.loads(val)
    except (json.JSONDecodeError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _build_related_refs(
    bookmark_id: int,
    era_label: str | None,
    tags: list,
    conn: sqlite3.Connection,
    slug_by_id: dict[int, str],
) -> list[str]:
    """Heuristic suggestions, distinct from persisted cross-references."""
    related: set[int] = set()

    if era_label:
        for r in conn.execute(
            "SELECT bookmark_id FROM bookmarks "
            "WHERE era_label = ? AND bookmark_id != ? AND content_visible = 1",
            (era_label, bookmark_id),
        ):
            related.add(r["bookmark_id"])

    if tags:
        tag_set = set(tags)
        for r in conn.execute(
            "SELECT b.bookmark_id, e.tags FROM bookmarks b "
            "JOIN enriched e ON e.bookmark_id = b.bookmark_id "
            "WHERE b.content_visible = 1 AND b.bookmark_id != ?",
            (bookmark_id,),
        ):
            other = _parse_json_list(r["tags"])
            if tag_set & set(other):
                related.add(r["bookmark_id"])

    return sorted(slug_by_id[bid] for bid in related if bid in slug_by_id)


def _get_template_env() -> Environment:
    return Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=False)


def _get_x_template_env() -> Environment:
    # Separate env: the X template uses block trimming; preserve the
    # browser template's existing whitespace conventions.
    return Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )


def _build_slugs(bookmarks: list[sqlite3.Row]) -> dict[int, str]:
    """Browser slugs exactly as before; X-lane slugs from item identity.

    Browser slugs end in their bookmark_id, so they are unique among
    themselves. An X-lane slug that would collide with any other page
    gets ``-b<bookmark_id>`` appended (deterministic for a given DB)."""
    slug_by_id = {
        r["bookmark_id"]: _slug_for(r)
        for r in bookmarks
        if r["source"] not in x_pages.X_SOURCES
    }
    used = set(slug_by_id.values())
    for r in bookmarks:
        if r["source"] not in x_pages.X_SOURCES:
            continue
        slug = x_pages.x_slug(r)
        if slug in used:
            slug = f"{slug}-b{r['bookmark_id']}"
        used.add(slug)
        slug_by_id[r["bookmark_id"]] = slug
    return slug_by_id


# ── pruning ────────────────────────────────────────────────────────

# Every page either template renders opens with this frontmatter: the
# compiler's marker. A file without it (hand-written, or another tool's)
# is never deleted.
_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.S)
_MARKER_PAGE_TYPE = re.compile(r"^page_type: bookmark$", re.M)
_MARKER_BOOKMARK_ID = re.compile(r"^bookmark_id: \d+$", re.M)
_MARKER_SCAN_BYTES = 8192


def is_compiled_page(path: Path) -> bool:
    """True when ``path`` is a page this compiler generated: a regular
    ``.md`` file whose YAML frontmatter has ``page_type: bookmark`` and an
    integer ``bookmark_id``."""
    if path.suffix != ".md" or path.is_symlink() or not path.is_file():
        return False
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(_MARKER_SCAN_BYTES)
    except OSError:
        return False
    m = _FRONTMATTER_RE.match(head)
    if m is None:
        return False
    block = m.group(1)
    return bool(_MARKER_PAGE_TYPE.search(block) and _MARKER_BOOKMARK_ID.search(block))


def prune_stale_pages(pages_dir: Path, *, keep: set[str]) -> list[str]:
    """Delete compiled pages in ``pages_dir`` (not its subdirectories)
    whose file name is not in ``keep``; return the deleted names, sorted.

    A row that became hidden (``content_visible = 0``) or whose slug changed
    leaves its old page behind; for a hidden row that page is exactly the
    data domain policy says to withdraw. Only files carrying the compiler's
    frontmatter marker are candidates."""
    removed = []
    for path in sorted(pages_dir.glob("*.md")):
        if path.name in keep or not is_compiled_page(path):
            continue
        path.unlink()
        removed.append(path.name)
    return removed


# ── entry point ────────────────────────────────────────────────────


@dataclass(frozen=True)
class CompileReport:
    written: int
    removed: list[str] = field(default_factory=list)


def compile_vault_report(
    db_path: str | Path | None = None,
    output_dir: str | Path | None = None,
    personal_dir: str | Path | None = None,
    *,
    prune: bool = True,
) -> CompileReport:
    """Compile the vault and, unless ``prune=False``, remove stale pages.

    `output_dir` is the vault ROOT (the muninn-vault repo checkout); pages
    are written to `{output_dir}/wiki/bookmarks/{slug}.md`. Afterwards any
    compiled page (see :func:`is_compiled_page`) that this run did not
    write is deleted: its row is now hidden or gone, or its slug moved.

    Resolution order for output_dir / personal_dir:
        explicit argument → muninn.config.load_paths() → error if neither.
    """
    paths = load_paths()
    out = Path(output_dir) if output_dir is not None else paths.vault_dir
    if out is None:
        raise VaultPathConflictError(
            "No vault output directory configured. Set MUNINN_VAULT_DIR or "
            "pass output_dir= explicitly."
        )
    pers = (
        Path(personal_dir)
        if personal_dir is not None
        else paths.personal_vault_dir
    )

    out = Path(out)
    _validate_vault_paths(out, Path(pers) if pers is not None else None)

    pages_dir = out / "wiki" / "bookmarks"
    pages_dir.mkdir(parents=True, exist_ok=True)
    env = _get_template_env()
    template = env.get_template(TEMPLATE_NAME)
    x_template = _get_x_template_env().get_template(X_TEMPLATE_NAME)

    conn = connect(db_path) if db_path else connect()
    try:
        bookmarks = conn.execute(
            "SELECT bookmark_id, source, source_id, captured_at, title, url, "
            "       folder_path, era_label, domain, content_visible, "
            "       enrichment_source, source_metadata "
            "FROM bookmarks WHERE content_visible = 1 ORDER BY bookmark_id"
        ).fetchall()

        slug_by_id = _build_slugs(bookmarks)
        rows_by_id = {r["bookmark_id"]: r for r in bookmarks}
        graph = x_pages.load_link_graph(conn, set(rows_by_id))
        cross_refs_by_id = load_cross_refs(conn, slug_by_id)
        lint_notes = load_lint_notes(conn, set(rows_by_id))

        written: set[str] = set()
        for bm in bookmarks:
            enr = conn.execute(
                "SELECT summary, tags, entities, key_quotes, language "
                "FROM enriched WHERE bookmark_id = ?",
                (bm["bookmark_id"],),
            ).fetchone()

            summary = enr["summary"] if enr else None
            tags = _parse_json_list(enr["tags"]) if enr else []
            entities = _parse_json_list(enr["entities"]) if enr else []
            key_quotes = _parse_json_list(enr["key_quotes"]) if enr else []

            persisted_cross_refs = cross_refs_by_id.get(bm["bookmark_id"], [])
            persisted_slugs = set(persisted_cross_refs)
            cross_refs = [
                slug
                for slug in _build_related_refs(
                    bm["bookmark_id"], bm["era_label"], tags, conn, slug_by_id
                )
                if slug not in persisted_slugs
            ]

            bookmark_view = dict(bm)
            bookmark_view.pop("source_metadata")
            bookmark_view["folder_path"] = _parse_json_list(bm["folder_path"])

            if bm["source"] in x_pages.X_SOURCES:
                # Enrichment written before a policy change hid a linked
                # item can quote its locator, and its prose can paraphrase
                # the hidden page: withhold it whole until re-enrichment.
                hidden_urls = graph.hidden_child_urls.get(bm["bookmark_id"], set())
                if hidden_urls:
                    summary, key_quotes, entities, withheld = x_pages.withhold_enrichment(
                        hidden_urls, summary, key_quotes, entities
                    )
                    if withheld:
                        summary = x_pages.ENRICHMENT_WITHHELD
                view = x_pages.build_view(
                    conn, bm, rows_by_id=rows_by_id, slug_by_id=slug_by_id, graph=graph
                )
                if view["language"] is None and enr is not None:
                    view["language"] = enr["language"]
                # Pages already linked explicitly aren't repeated as "Related".
                linked = {slug_by_id[i] for i in graph.neighbours(bm["bookmark_id"])}
                content = x_template.render(
                    bookmark=bookmark_view,
                    x=view,
                    summary=summary,
                    tags=tags,
                    entities=entities,
                    key_quotes=key_quotes,
                    persisted_cross_refs=persisted_cross_refs,
                    cross_refs=[r for r in cross_refs if r not in linked],
                )
            else:
                content = template.render(
                    bookmark=bookmark_view,
                    summary=summary,
                    tags=tags,
                    entities=entities,
                    key_quotes=key_quotes,
                    persisted_cross_refs=persisted_cross_refs,
                    cross_refs=cross_refs,
                )

            notes = lint_notes.get(bm["bookmark_id"])
            if notes:
                content = apply_lint_notes(content, notes, slug_by_id)

            name = f"{slug_by_id[bm['bookmark_id']]}.md"
            (pages_dir / name).write_text(content)
            written.add(name)
    finally:
        conn.close()

    removed = prune_stale_pages(pages_dir, keep=written) if prune else []
    return CompileReport(written=len(written), removed=removed)


def compile_vault(
    db_path: str | Path | None = None,
    output_dir: str | Path | None = None,
    personal_dir: str | Path | None = None,
    *,
    prune: bool = True,
) -> int:
    """Compile the vault (see :func:`compile_vault_report`). Returns the
    count of pages written."""
    return compile_vault_report(
        db_path=db_path, output_dir=output_dir, personal_dir=personal_dir, prune=prune
    ).written


# Back-compat alias used by the CLI.
def generate_vault(*args, **kwargs) -> int:
    return compile_vault(*args, **kwargs)


__all__ = [
    "CompileReport",
    "VaultPathConflictError",
    "_validate_vault_paths",
    "compile_vault",
    "compile_vault_report",
    "generate_vault",
    "is_compiled_page",
    "prune_stale_pages",
]
