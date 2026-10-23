"""Read-only access to the compiled vault's ``wiki/`` tree for the MCP server.

Stdlib only at import time (the compiler's path guard, which pulls in
Jinja, is imported when a vault is first resolved).

Everything here is about *files*; SQLite visibility and the X-lane
masking live in :mod:`muninn.consumers.mcp.tools`, which passes a
``visible`` callback in where a page's ``bookmark_id`` must be checked.

Rules enforced here:

- The vault comes from ``MUNINN_VAULT_DIR``. It must be set, must have a
  ``wiki/`` directory, and must not equal, contain or sit inside
  ``MUNINN_PERSONAL_VAULT_DIR`` (the compiler's guard, SPEC Decision 2).
- A page path is relative, starts with ``wiki/``, ends in ``.md``, and
  has no empty, ``.``, ``..`` or dot-prefixed component, no backslash
  and no NUL. After resolving symlinks it must still be a ``.md`` file
  inside ``<vault>/wiki/``. Pages larger than :data:`MAX_PAGE_BYTES` are
  refused.
- Listing and scanning never follow symlinks and are bounded (files
  examined, bytes read).
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from muninn.config import load_paths

#: The captured lane: compiled from SQLite, only the compiler writes it.
CAPTURED_NAMESPACE = "bookmarks"
#: The authored lane: hand-written pages, never in SQLite (ADR-008/011).
AUTHORED_NAMESPACES = ("concepts", "infra", "people", "projects")
NAMESPACES = (CAPTURED_NAMESPACE, *AUTHORED_NAMESPACES)

MAX_PATH_CHARS = 512
#: ``get_page`` refuses files larger than this.
MAX_PAGE_BYTES = 1024 * 1024
#: Bytes read from the top of a page for its frontmatter and summary.
HEAD_BYTES = 8192
SUMMARY_CHARS = 200

#: Authored-lane keyword scan bounds, per search call.
SCAN_MAX_FILES = 2000
SCAN_MAX_FILE_BYTES = 256 * 1024
SCAN_MAX_TOTAL_BYTES = 32 * 1024 * 1024
#: A title match counts this many body matches.
SCAN_TITLE_WEIGHT = 5

#: ``list_namespace`` bounds.
LIST_DEFAULT_LIMIT = 50
LIST_MAX_LIMIT = 200
#: Pages examined per call (hidden bookmark pages are skipped, so a page
#: of results may need more than ``limit`` reads).
LIST_MAX_EXAMINED = 1000
#: Directory entries walked per call, before sorting for the cursor.
LIST_MAX_ENTRIES = 100_000


class VaultAccessError(Exception):
    """A vault request was refused. ``str(exc)`` is safe to show a client
    (no host paths); ``code`` is a stable machine-readable reason."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def not_found(path: str) -> VaultAccessError:
    """The one error for a missing page *and* a withheld one, so a client
    can't tell them apart."""
    return VaultAccessError("not_found", f"Page {path!r} not found")


# ── the vault root ─────────────────────────────────────────────────


def vault_wiki_dir() -> Path:
    """Resolved ``<MUNINN_VAULT_DIR>/wiki``, after the personal-vault guard."""
    paths = load_paths()
    return resolve_wiki_dir(paths.vault_dir, paths.personal_vault_dir)


def resolve_wiki_dir(
    vault_dir: Path | None, personal_dir: Path | None, *, action: str = "serve"
) -> Path:
    """Resolved ``<vault_dir>/wiki``, after the personal-vault guard.

    The guard is the compiler's (SPEC Decision 2): the vault must not
    equal, contain or sit inside ``personal_dir``, and neither may its
    ``wiki/`` once symlinks are resolved. ``action`` only words the
    refusal ("refusing to <action> the vault"). Used by the MCP server
    (:func:`vault_wiki_dir`) and by ``muninn lint``."""
    if vault_dir is None:
        raise VaultAccessError("vault_unconfigured", "MUNINN_VAULT_DIR is not set")
    from muninn.consumers.vault.compiler import (  # lazy: pulls in Jinja
        VaultPathConflictError,
        _validate_vault_paths,
    )

    root = Path(vault_dir).expanduser().resolve()
    wiki = (root / "wiki").resolve()
    try:
        _validate_vault_paths(root, personal_dir)
        # wiki/ itself may be a symlink; what it points at is what we serve.
        _validate_vault_paths(wiki, personal_dir)
    except VaultPathConflictError:
        raise VaultAccessError(
            "vault_conflict",
            f"MUNINN_VAULT_DIR overlaps MUNINN_PERSONAL_VAULT_DIR; refusing to {action} the vault",
        ) from None
    if not wiki.is_relative_to(root):
        raise VaultAccessError("vault_unconfigured", "the vault's wiki/ resolves outside the vault")
    if not wiki.is_dir():
        raise VaultAccessError("vault_unconfigured", "the vault has no wiki/ directory")
    return wiki


# ── path safety ────────────────────────────────────────────────────

_DRIVE_RE = re.compile(r"\A[A-Za-z]:")


def page_parts(path: str) -> list[str]:
    """Validate a client page path and split it; raises ``invalid_path``."""
    def bad(why: str) -> VaultAccessError:
        return VaultAccessError("invalid_path", f"Invalid page path: {why}")

    if not isinstance(path, str) or not path:
        raise bad("empty")
    if len(path) > MAX_PATH_CHARS:
        raise bad(f"longer than {MAX_PATH_CHARS} characters")
    if "\x00" in path or "\\" in path:
        raise bad("contains a NUL or backslash")
    if path.startswith(("/", "~")) or _DRIVE_RE.match(path):
        raise bad("must be relative to the vault, e.g. 'wiki/concepts/example.md'")
    parts = path.split("/")
    if parts[0] != "wiki" or len(parts) < 2:
        raise bad("must start with 'wiki/'")
    for part in parts[1:]:
        if part in ("", ".", ".."):
            raise bad("empty, '.' or '..' component")
        if part.startswith("."):
            raise bad("hidden (dot-prefixed) component")
    if not parts[-1].endswith(".md") or parts[-1] == ".md":
        raise bad("only .md pages are served")
    return parts


def resolve_page(wiki: Path, path: str) -> Path:
    """The real file behind a validated client path, inside ``wiki``."""
    parts = page_parts(path)
    candidate = wiki.joinpath(*parts[1:])
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):  # missing, or a symlink loop
        raise not_found(path) from None
    if not resolved.is_relative_to(wiki):
        raise VaultAccessError("invalid_path", "Invalid page path: resolves outside the vault's wiki/")
    if resolved.suffix != ".md" or not resolved.is_file():
        raise not_found(path)
    return resolved


def namespace_of(rel: str) -> str | None:
    """``wiki/<namespace>/…`` → namespace; a page at ``wiki/`` root → None."""
    parts = rel.split("/")
    return parts[1] if len(parts) > 2 else None


def read_page(wiki: Path, path: str) -> tuple[Path, str]:
    """Resolve and read a page (size-capped); returns (file, text)."""
    resolved = resolve_page(wiki, path)
    size = resolved.stat().st_size
    if size > MAX_PAGE_BYTES:
        raise VaultAccessError(
            "too_large", f"Page {path!r} is {size} bytes; the cap is {MAX_PAGE_BYTES}"
        )
    with resolved.open("rb") as fh:
        data = fh.read(MAX_PAGE_BYTES + 1)
    if len(data) > MAX_PAGE_BYTES:  # grew since stat
        raise VaultAccessError("too_large", f"Page {path!r} exceeds the {MAX_PAGE_BYTES}-byte cap")
    return resolved, data.decode("utf-8", errors="replace")


def read_head(file: Path, limit: int = HEAD_BYTES) -> str:
    with file.open("rb") as fh:
        return fh.read(limit).decode("utf-8", errors="replace")


# ── frontmatter and summaries ──────────────────────────────────────

_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
_META_LINE_RE = re.compile(r"\A([A-Za-z_][\w-]*):[ \t]*(.*?)[ \t]*\Z")
_HEADING_RE = re.compile(r"\A#{1,6}[ \t]+(.+?)[ \t#]*\Z")


def _scalar(raw: str) -> str | None:
    if not raw:
        return None
    if raw.startswith('"'):
        try:
            value = json.loads(raw)  # the compiler writes titles with tojson
        except json.JSONDecodeError:
            return raw.strip('"')
        return value if isinstance(value, str) else str(value)
    if raw.startswith("'") and raw.endswith("'") and len(raw) >= 2:
        return raw[1:-1].replace("''", "'")
    return raw


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Top-level ``key: scalar`` pairs of a leading YAML block, and the body.

    Deliberately not a YAML parser: no aliases, tags or nesting, so an
    authored page can't make the server do unbounded work. Lists and
    nested values are skipped.
    """
    m = _FRONTMATTER_RE.match(text)
    if m is None:
        return {}, text
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        lm = _META_LINE_RE.match(line)
        if lm is None:
            continue
        value = _scalar(lm.group(2))
        if value is not None:
            meta[lm.group(1)] = value
    return meta, text[m.end():]


def bookmark_id_of(meta: dict[str, str]) -> int | None:
    raw = meta.get("bookmark_id")
    return int(raw) if raw is not None and raw.isdigit() else None


def one_line(text: str, limit: int = SUMMARY_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def first_heading(body: str) -> str | None:
    for line in body.splitlines():
        m = _HEADING_RE.match(line.strip())
        if m:
            return m.group(1)
    return None


def first_line(body: str) -> str | None:
    """First prose line: not a heading, blockquote banner, comment, rule or table."""
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ">", "<!--", "---", "|", "```")):
            continue
        return line
    return None


def page_title(meta: dict[str, str], body: str, file_stem: str) -> str:
    return one_line(meta.get("title") or first_heading(body) or file_stem)


def page_summary(meta: dict[str, str], body: str) -> str | None:
    """Frontmatter ``summary`` (or ``description``), else the first prose
    line, else the first heading."""
    text = meta.get("summary") or meta.get("description") or first_line(body) or first_heading(body)
    return one_line(text) if text else None


# ── walking a namespace ────────────────────────────────────────────


def iter_page_paths(wiki: Path, namespace: str, *, max_entries: int | None = None) -> Iterator[str]:
    """``wiki/<namespace>/…/*.md`` relative paths under a namespace, in
    sorted order per directory. Symlinks (files and directories) and
    dot-prefixed entries are skipped."""
    top = wiki / namespace
    if top.is_symlink() or not top.is_dir():
        return
    seen = 0
    stack = [top]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(os.scandir(current), key=lambda e: e.name)
        except OSError:
            continue
        subdirs = []
        for entry in entries:
            seen += 1
            if max_entries is not None and seen > max_entries:
                return
            if entry.name.startswith(".") or entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                subdirs.append(Path(entry.path))
            elif entry.name.endswith(".md") and entry.is_file(follow_symlinks=False):
                yield "wiki/" + Path(entry.path).relative_to(wiki).as_posix()
        stack.extend(reversed(subdirs))


@dataclass(frozen=True)
class PageInfo:
    path: str
    namespace: str | None
    title: str
    summary: str | None
    bookmark_id: int | None


def page_info(wiki: Path, rel: str, text: str) -> PageInfo:
    meta, body = split_frontmatter(text)
    return PageInfo(
        path=rel,
        namespace=namespace_of(rel),
        title=page_title(meta, body, Path(rel).stem),
        summary=page_summary(meta, body),
        bookmark_id=bookmark_id_of(meta),
    )


def list_pages(
    wiki: Path,
    namespace: str,
    *,
    limit: int,
    cursor: str | None,
    visible: Callable[[PageInfo], bool],
) -> tuple[list[PageInfo], str | None]:
    """One page of a namespace listing, in path order after ``cursor``.

    ``cursor`` is the last path a previous call examined; the returned
    cursor is ``None`` when the namespace is exhausted.
    """
    paths = sorted(iter_page_paths(wiki, namespace, max_entries=LIST_MAX_ENTRIES))
    if cursor is not None:
        paths = [p for p in paths if p > cursor]
    out: list[PageInfo] = []
    last: str | None = None
    for examined, rel in enumerate(paths, start=1):
        last = rel
        try:
            info = page_info(wiki, rel, read_head(wiki.joinpath(*rel.split("/")[1:])))
        except OSError:
            continue
        if visible(info):
            out.append(info)
        if len(out) >= limit or examined >= LIST_MAX_EXAMINED:
            break
    more = last is not None and last != (paths[-1] if paths else None)
    return out, (last if more else None)


# ── authored-lane keyword scan ─────────────────────────────────────

_TERM_RE = re.compile(r"\w+")
# FTS5 operators a caller may pass through from keyword syntax.
_OPERATORS = {"and", "or", "not", "near"}


def scan_terms(query: str) -> list[str]:
    """Case-folded word terms of ``query``, operators dropped, deduplicated."""
    seen: dict[str, None] = {}
    for term in _TERM_RE.findall(query.casefold()):
        if term not in _OPERATORS:
            seen.setdefault(term, None)
    return list(seen)


@dataclass(frozen=True)
class ScanHit:
    info: PageInfo
    score: int


@dataclass
class ScanResult:
    hits: list[ScanHit]
    truncated: bool = False


def scan_namespaces(
    wiki: Path,
    namespaces: tuple[str, ...] | list[str],
    query: str,
    *,
    visible: Callable[[PageInfo], bool],
    max_files: int = SCAN_MAX_FILES,
    max_file_bytes: int = SCAN_MAX_FILE_BYTES,
    max_total_bytes: int = SCAN_MAX_TOTAL_BYTES,
) -> ScanResult:
    """Pages containing every query term, best first.

    Score: term occurrences in the page (frontmatter and body), plus
    :data:`SCAN_TITLE_WEIGHT` per occurrence in the title. Ties go to
    path order. Each file is read up to ``max_file_bytes``; the scan stops
    (``truncated=True``) after ``max_files`` files or ``max_total_bytes``.
    """
    terms = scan_terms(query)
    result = ScanResult(hits=[])
    if not terms:
        return result
    files = 0
    total = 0
    for namespace in namespaces:
        for rel in iter_page_paths(wiki, namespace):
            if files >= max_files or total >= max_total_bytes:
                result.truncated = True
                break
            files += 1
            try:
                text = read_head(wiki.joinpath(*rel.split("/")[1:]), max_file_bytes)
            except OSError:
                continue
            total += len(text)
            folded = text.casefold()
            if not all(t in folded for t in terms):
                continue
            info = page_info(wiki, rel, text)
            if not visible(info):
                continue
            title = info.title.casefold()
            score = sum(
                len(re.findall(rf"\b{re.escape(t)}\b", folded))
                + SCAN_TITLE_WEIGHT * len(re.findall(rf"\b{re.escape(t)}\b", title))
                for t in terms
            )
            if score:
                result.hits.append(ScanHit(info=info, score=score))
        if result.truncated:
            break
    result.hits.sort(key=lambda h: (-h.score, h.info.path))
    return result


__all__ = [
    "AUTHORED_NAMESPACES",
    "CAPTURED_NAMESPACE",
    "MAX_PAGE_BYTES",
    "NAMESPACES",
    "PageInfo",
    "ScanHit",
    "ScanResult",
    "VaultAccessError",
    "iter_page_paths",
    "list_pages",
    "not_found",
    "one_line",
    "page_info",
    "page_parts",
    "read_page",
    "resolve_wiki_dir",
    "resolve_page",
    "scan_namespaces",
    "scan_terms",
    "split_frontmatter",
    "vault_wiki_dir",
]
