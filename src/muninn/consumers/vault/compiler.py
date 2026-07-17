"""Vault compiler — one Obsidian-compatible markdown page per visible bookmark.

Reads bookmarks + enriched rows from the canonical SQLite store and renders
them through `templates/bookmark_page.md.j2` into the unified vault's
`wiki/bookmarks/` namespace (ADR-001/ADR-008: the output dir is the vault
ROOT; this compiler owns — and only writes — `wiki/bookmarks/` beneath it).
Hidden bookmarks (`content_visible = 0`) are skipped entirely — no page is
written and no wikilink target is registered.

CRITICAL: this module enforces SPEC.md Decision 2 (two distinct vaults — the
COMPILED Muninn output vault must NEVER be the same as the user's PERSONAL
input vault). The guard runs before any file is written.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Iterable

from jinja2 import Environment, FileSystemLoader

from muninn.config import load_paths
from muninn.db import connect


TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
TEMPLATE_NAME = "bookmark_page.md.j2"


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


def _build_cross_refs(
    bookmark_id: int,
    era_label: str | None,
    tags: list,
    conn: sqlite3.Connection,
    slug_by_id: dict[int, str],
) -> list[str]:
    """Other visible bookmarks sharing era_label or any tag with this one."""
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


# ── entry point ────────────────────────────────────────────────────


def compile_vault(
    db_path: str | Path | None = None,
    output_dir: str | Path | None = None,
    personal_dir: str | Path | None = None,
) -> int:
    """Compile the vault. Returns the count of pages written.

    `output_dir` is the vault ROOT (the muninn-vault repo checkout); pages
    are written to `{output_dir}/wiki/bookmarks/{slug}.md`.

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

    conn = connect(db_path) if db_path else connect()
    try:
        bookmarks = conn.execute(
            "SELECT bookmark_id, source, source_id, captured_at, title, url, "
            "       folder_path, era_label, domain, content_visible, "
            "       enrichment_source "
            "FROM bookmarks WHERE content_visible = 1 ORDER BY bookmark_id"
        ).fetchall()

        slug_by_id = {r["bookmark_id"]: _slug_for(r) for r in bookmarks}

        count = 0
        for bm in bookmarks:
            enr = conn.execute(
                "SELECT summary, tags, entities, key_quotes "
                "FROM enriched WHERE bookmark_id = ?",
                (bm["bookmark_id"],),
            ).fetchone()

            summary = enr["summary"] if enr else None
            tags = _parse_json_list(enr["tags"]) if enr else []
            entities = _parse_json_list(enr["entities"]) if enr else []
            key_quotes = _parse_json_list(enr["key_quotes"]) if enr else []

            cross_refs = _build_cross_refs(
                bm["bookmark_id"], bm["era_label"], tags, conn, slug_by_id
            )

            bookmark_view = dict(bm)
            bookmark_view["folder_path"] = _parse_json_list(bm["folder_path"])

            content = template.render(
                bookmark=bookmark_view,
                summary=summary,
                tags=tags,
                entities=entities,
                key_quotes=key_quotes,
                cross_refs=cross_refs,
            )

            (pages_dir / f"{slug_by_id[bm['bookmark_id']]}.md").write_text(content)
            count += 1

        return count
    finally:
        conn.close()


# Back-compat alias used by the CLI.
def generate_vault(*args, **kwargs) -> int:
    return compile_vault(*args, **kwargs)


__all__ = [
    "VaultPathConflictError",
    "_validate_vault_paths",
    "compile_vault",
    "generate_vault",
]
