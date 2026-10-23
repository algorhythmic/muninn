"""Deterministic vault lint (``muninn lint``): orphans, missing links, broken links.

Free and model-free. It reads the compiled vault's ``wiki/`` tree and
returns a report; it never writes, renames or deletes a vault file. That
holds for both lanes (ADR-008): findings on authored pages
(``concepts/``, ``infra/``, ``people/``, ``projects/``, ``log.md``) are
report-only, and so are findings on compiled ``bookmarks/`` pages, whose
only writer is the compiler. See docs/vault-lint.md.

Checks:

* **Orphans**: pages with no inbound link from any other page. Hub and
  index pages (:data:`HUB_STEMS`) and pages whose frontmatter says
  ``lint_ignore: orphan`` are excluded, and the report lists those
  exclusions.
* **Suggested links**: a page mentions another page's title, alias or
  file name (whole word, case-insensitive) without linking it. Targets
  are authored pages (:data:`SUGGEST_TARGET_NAMESPACES`). Frontmatter,
  code, HTML comments, URLs and existing links are not scanned; terms
  shorter than :data:`MIN_TERM_CHARS`, made only of common words, or
  shared by two pages are dropped; at most one suggestion per target per
  page.
* **Unresolved links**: ``[[wikilinks]]`` (and relative ``.md`` links)
  that resolve to no page.

Links resolve the way Obsidian does: ``[[target]]``, ``[[target|display]]``,
``[[target#heading]]``, ``[[target#^block]]``, ``![[embed]]``, a path
(``[[concepts/foo]]``, ``[[wiki/concepts/foo]]``) or a file name,
case-insensitively, with or without ``.md``; a page's ``aliases`` resolve
too. Relative Markdown links to ``.md`` files count as links, and so do
wikilinks in frontmatter values (Obsidian property links).

Stdlib only at import time; the vault guard (the compiler's) is imported
when the vault is resolved.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote

from muninn.consumers.mcp.vault_fs import (
    AUTHORED_NAMESPACES,
    CAPTURED_NAMESPACE,
    MAX_PAGE_BYTES,
    iter_page_paths,
)

REPORT_VERSION = "muninn-vault-lint/1"

#: Pages named like this (any directory, case-insensitive) are hubs or
#: indexes: they are entry points, so having no inbound link is expected.
HUB_STEMS = ("index", "_index", "readme", "home", "log")
#: Frontmatter key an operator can set on a page to opt it out of a check:
#: ``lint_ignore: orphan``, ``lint_ignore: suggest`` (no suggestions *on*
#: this page) or a list of both.
IGNORE_KEY = "lint_ignore"
IGNORE_VALUES = ("orphan", "suggest")
#: Pages whose title/aliases/file name are matched for suggested links.
SUGGEST_TARGET_NAMESPACES = AUTHORED_NAMESPACES
#: A term shorter than this is never matched.
MIN_TERM_CHARS = 4
#: A term made only of these words is never matched.
COMMON_WORDS = frozenset(
    """
    about above after again also back been before being below between both
    could does doing down during each from further have having here into
    just more most much must only other over same should some such than that
    their them then there these they this those through under until very
    were what when where which while will with would your
    note notes page pages link links index home readme todo summary tags
    misc draft untitled overview general
    """.split()
)

_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
_KEY_RE = re.compile(r"\A([A-Za-z_][\w-]*):[ \t]*(.*?)[ \t]*\Z")
_LIST_ITEM_RE = re.compile(r"\A[ \t]+-[ \t]+(.*?)[ \t]*\Z|\A-[ \t]+(.*?)[ \t]*\Z")
_H1_RE = re.compile(r"^#[ \t]+(.+?)[ \t#]*$", re.M)

_FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})[^\n]*\n.*?(?:^[ \t]{0,3}\1[ \t]*$|\Z)", re.S | re.M)
_INLINE_CODE_RE = re.compile(r"(`+)(?!`).+?(?<!`)\1", re.S)
_COMMENT_RE = re.compile(r"<!--.*?(?:-->|\Z)", re.S)
_WIKILINK_RE = re.compile(r"!?\[\[([^\[\]\n]+?)\]\]")
_MDLINK_RE = re.compile(r"!?\[([^\]\n]*)\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"\n]*\")?\s*\)")
_URL_RE = re.compile(r"\b(?:https?|ftp|file|mailto):[^\s<>\"')\]]+", re.I)
_SCHEME_RE = re.compile(r"\A[a-z][a-z0-9+.-]*:", re.I)
_WS_RE = re.compile(r"\s+")


# ── pages ──────────────────────────────────────────────────────────


@dataclass
class Link:
    target: str  # the page part: no |display, no #heading; "" for a self-link
    line: int
    kind: str  # "wikilink" or "markdown"


@dataclass
class Page:
    rel: str  # "wiki/concepts/foo.md"
    lane: str  # "captured" or "authored"
    namespace: str | None
    stem: str
    title: str
    aliases: list[str]
    ignore: set[str]
    prose: str  # body with frontmatter, code, comments, links and URLs blanked
    body_line: int  # 1-based line number of the body's first line
    links: list[Link] = field(default_factory=list)

    @property
    def key(self) -> str:
        """``wiki/concepts/foo`` lowercased: the path a link can name."""
        return self.rel[: -len(".md")].casefold()


def _blank(match: re.Match[str]) -> str:
    """Same length, newlines kept, so offsets and line numbers survive."""
    return re.sub(r"[^\n]", " ", match.group(0))


def _scalar(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith('"'):
        try:
            value = json.loads(raw)
            return value if isinstance(value, str) else str(value)
        except json.JSONDecodeError:
            return raw.strip('"')
    if len(raw) >= 2 and raw[0] == raw[-1] == "'":
        return raw[1:-1].replace("''", "'")
    return raw


def _inline_list(raw: str) -> list[str]:
    inner = raw.strip()[1:-1]
    try:
        value = json.loads("[" + inner + "]")
        if all(isinstance(v, str) for v in value):
            return value
    except json.JSONDecodeError:
        pass
    return [_scalar(part) for part in inner.split(",") if part.strip()]


def parse_frontmatter(text: str) -> tuple[dict[str, Any], int]:
    """Top-level keys of a leading YAML block (scalars, ``[a, b]`` lists and
    ``- item`` block lists) and the offset where the body starts.

    Deliberately not a YAML parser: nesting, anchors and tags are ignored,
    so an authored page can't make lint do unbounded work."""
    m = _FRONTMATTER_RE.match(text)
    if m is None:
        return {}, 0
    meta: dict[str, Any] = {}
    current: str | None = None
    for line in m.group(1).splitlines():
        item = _LIST_ITEM_RE.match(line)
        if item is not None and current is not None:
            value = _scalar(item.group(1) if item.group(1) is not None else item.group(2))
            if isinstance(meta.get(current), list) and value:
                meta[current].append(value)
            continue
        km = _KEY_RE.match(line)
        if km is None:
            current = None
            continue
        key, raw = km.group(1), km.group(2)
        if not raw:
            meta[key] = []  # a block list may follow
            current = key
        elif raw.startswith("[") and raw.endswith("]"):
            meta[key] = _inline_list(raw)
            current = None
        else:
            meta[key] = _scalar(raw)
            current = None
    return meta, m.end()


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str) and v.strip()]
    if isinstance(value, str) and value.strip():
        return [part.strip() for part in value.split(",") if part.strip()]
    return []


def _split_link(inner: str) -> str:
    """``target#heading|display`` → ``target`` (escaped table pipes too)."""
    target = inner.split("|", 1)[0]
    target = target.rstrip("\\")  # [[page\|display]] inside a table
    return target.split("#", 1)[0].strip()


def _line_at(text: str, offset: int, first_line: int) -> int:
    return first_line + text.count("\n", 0, offset)


def build_page(rel: str, text: str) -> Page:
    meta, body_start = parse_frontmatter(text)
    body = text[body_start:]
    body_line = text.count("\n", 0, body_start) + 1
    parts = rel.split("/")
    namespace = parts[1] if len(parts) > 2 else None
    stem = PurePosixPath(rel).stem
    h1 = _H1_RE.search(body)
    title = meta["title"] if isinstance(meta.get("title"), str) and meta["title"].strip() else (
        h1.group(1).strip() if h1 else stem
    )
    ignore = {v.casefold() for v in _as_list(meta.get(IGNORE_KEY))}

    # Blank what is never prose: code first (a link inside code is not a
    # link), then comments; collect links; then blank links and URLs.
    prose = _FENCE_RE.sub(_blank, body)
    prose = _INLINE_CODE_RE.sub(_blank, prose)
    prose = _COMMENT_RE.sub(_blank, prose)
    # Wikilinks in frontmatter values are Obsidian property links
    # (``superseded_by: ["[[page]]"]``): they count as links too.
    frontmatter = text[:body_start]
    links: list[Link] = [
        Link(_split_link(m.group(1)), _line_at(frontmatter, m.start(), 1), "wikilink")
        for m in _WIKILINK_RE.finditer(frontmatter)
    ]
    for m in _WIKILINK_RE.finditer(prose):
        links.append(Link(_split_link(m.group(1)), _line_at(prose, m.start(), body_line), "wikilink"))
    for m in _MDLINK_RE.finditer(prose):
        href = m.group(2)
        if _SCHEME_RE.match(href) or href.startswith("#"):
            continue
        path = unquote(href.split("#", 1)[0])
        if path.casefold().endswith(".md"):
            links.append(Link(path, _line_at(prose, m.start(), body_line), "markdown"))
    prose = _WIKILINK_RE.sub(_blank, prose)
    prose = _MDLINK_RE.sub(_blank, prose)
    prose = _URL_RE.sub(_blank, prose)

    return Page(
        rel=rel,
        lane="captured" if namespace == CAPTURED_NAMESPACE else "authored",
        namespace=namespace,
        stem=stem,
        title=" ".join(title.split()),
        aliases=_as_list(meta.get("aliases")) + _as_list(meta.get("alias")),
        ignore=ignore,
        prose=prose,
        body_line=body_line,
        links=links,
    )


def iter_wiki_pages(wiki: Path) -> list[str]:
    """Every ``.md`` page under ``wiki/``, sorted: pages at the root, then
    each top-level directory (symlinks and dot-prefixed entries skipped,
    as the MCP server does)."""
    rels: list[str] = []
    try:
        entries = sorted(os.scandir(wiki), key=lambda e: e.name)
    except OSError:
        return rels
    for entry in entries:
        if entry.name.startswith(".") or entry.is_symlink():
            continue
        if entry.is_dir(follow_symlinks=False):
            rels.extend(iter_page_paths(wiki, entry.name))
        elif entry.name.endswith(".md") and entry.is_file(follow_symlinks=False):
            rels.append(f"wiki/{entry.name}")
    return sorted(rels)


def load_pages(wiki: Path) -> tuple[list[Page], list[dict[str, str]]]:
    """Read every page (read-only, size-capped). Returns (pages, skipped)."""
    pages: list[Page] = []
    skipped: list[dict[str, str]] = []
    for rel in iter_wiki_pages(wiki):
        path = wiki.joinpath(*rel.split("/")[1:])
        try:
            with path.open("rb") as fh:
                data = fh.read(MAX_PAGE_BYTES + 1)
        except OSError as exc:
            skipped.append({"page": rel, "reason": f"unreadable ({type(exc).__name__})"})
            continue
        if len(data) > MAX_PAGE_BYTES:
            skipped.append({"page": rel, "reason": f"larger than {MAX_PAGE_BYTES} bytes"})
            continue
        pages.append(build_page(rel, data.decode("utf-8", errors="replace")))
    return pages, skipped


# ── link resolution ────────────────────────────────────────────────


class Resolver:
    """Obsidian-style link resolution over a set of pages."""

    def __init__(self, pages: list[Page]) -> None:
        self.by_key: dict[str, list[Page]] = {}
        self.by_stem: dict[str, list[Page]] = {}
        self.by_alias: dict[str, list[Page]] = {}
        for page in pages:
            self.by_key.setdefault(page.key, []).append(page)
            self.by_stem.setdefault(page.stem.casefold(), []).append(page)
            for alias in page.aliases:
                self.by_alias.setdefault(_norm(alias), []).append(page)

    def resolve(self, target: str, source: Page, kind: str = "wikilink") -> Page | None:
        if not target:
            return source  # [[#heading]]: a link within the page
        t = target.replace("\\", "/").strip()
        if t.casefold().endswith(".md"):
            t = t[:-3]
        if kind == "markdown" and not t.startswith("/"):
            # Relative to the linking page's directory.
            base = PurePosixPath(source.rel).parent
            joined = os.path.normpath((base / t).as_posix())
            hit = self.by_key.get(joined.casefold())
            if hit:
                return _pick(hit, source)
        t = t.lstrip("/")
        folded = t.casefold()
        if "/" in folded:
            candidates = self.by_key.get(folded) or self.by_key.get("wiki/" + folded)
            if not candidates:
                candidates = [
                    p for ps in self.by_key.values() for p in ps if p.key.endswith("/" + folded)
                ]
            return _pick(candidates, source) if candidates else None
        candidates = self.by_stem.get(folded) or self.by_alias.get(_norm(t))
        return _pick(candidates, source) if candidates else None

    def link_text(self, page: Page, mention: str) -> str:
        """The shortest wikilink that resolves to ``page``."""
        target = page.stem if len(self.by_stem.get(page.stem.casefold(), [])) == 1 else page.rel[:-3]
        if _norm(mention) == _norm(target):
            return f"[[{target}]]"
        return f"[[{target}|{mention}]]"


def _pick(candidates: list[Page], source: Page) -> Page:
    """Several pages share a name: prefer the linking page's directory,
    then the shortest path, then path order (deterministic)."""
    here = PurePosixPath(source.rel).parent
    return min(
        candidates,
        key=lambda p: (PurePosixPath(p.rel).parent != here, len(p.rel), p.rel),
    )


def _norm(text: str) -> str:
    return _WS_RE.sub(" ", text).strip().casefold()


# ── the checks ─────────────────────────────────────────────────────


def _terms_for(page: Page) -> set[str]:
    raw = [page.title, *page.aliases, page.stem.replace("-", " ").replace("_", " ")]
    terms = set()
    for term in raw:
        norm = _norm(term)
        if len(norm) < MIN_TERM_CHARS or not re.search(r"[^\W\d_]", norm):
            continue
        words = re.findall(r"\w+", norm)
        if words and all(w in COMMON_WORDS for w in words):
            continue
        terms.add(norm)
    return terms


def _term_index(pages: list[Page]) -> dict[str, Page]:
    """Matchable term → its one target page (terms two pages share are dropped)."""
    owners: dict[str, set[str]] = {}
    by_rel = {p.rel: p for p in pages}
    for page in pages:
        if page.namespace not in SUGGEST_TARGET_NAMESPACES:
            continue
        for term in _terms_for(page):
            owners.setdefault(term, set()).add(page.rel)
    return {t: by_rel[next(iter(rels))] for t, rels in owners.items() if len(rels) == 1}


def _term_regex(terms: list[str]) -> re.Pattern[str] | None:
    if not terms:
        return None
    ordered = sorted(terms, key=lambda t: (-len(t), t))  # longest match wins
    alternation = "|".join(r"\s+".join(map(re.escape, t.split(" "))) for t in ordered)
    return re.compile(rf"(?<!\w)(?:{alternation})(?!\w)", re.IGNORECASE)


@dataclass
class LintReport:
    vault: str
    pages: int
    lanes: dict[str, int]
    orphans: list[dict[str, Any]]
    suggestions: list[dict[str, Any]]
    unresolved: list[dict[str, Any]]
    skipped: list[dict[str, str]]

    @property
    def finding_count(self) -> int:
        return len(self.orphans) + len(self.suggestions) + len(self.unresolved)

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_version": REPORT_VERSION,
            "vault": self.vault,
            "writes": "none: lint never edits a vault file",
            "pages_scanned": self.pages,
            "pages_by_lane": self.lanes,
            "counts": {
                "orphans": len(self.orphans),
                "suggestions": len(self.suggestions),
                "unresolved_links": len(self.unresolved),
                "skipped": len(self.skipped),
            },
            "orphans": self.orphans,
            "suggestions": self.suggestions,
            "unresolved_links": self.unresolved,
            "skipped": self.skipped,
            "exclusions": {
                "orphan_hub_names": list(HUB_STEMS),
                "opt_out_frontmatter": f"{IGNORE_KEY}: orphan | suggest",
                "suggestion_targets": list(SUGGEST_TARGET_NAMESPACES),
                "min_term_chars": MIN_TERM_CHARS,
                "not_scanned": ["frontmatter", "code", "html comments", "urls", "existing links"],
            },
        }


def lint_pages(pages: list[Page], *, vault: str = "", skipped: list[dict[str, str]] | None = None) -> LintReport:
    """Run every check over already-loaded pages (pure; no I/O)."""
    resolver = Resolver(pages)
    inbound: dict[str, set[str]] = {p.rel: set() for p in pages}
    linked: dict[str, set[str]] = {p.rel: set() for p in pages}
    unresolved: list[dict[str, Any]] = []
    for page in pages:
        for link in page.links:
            target = resolver.resolve(link.target, page, link.kind)
            if target is None:
                unresolved.append({
                    "page": page.rel, "lane": page.lane, "line": link.line, "target": link.target,
                })
                continue
            linked[page.rel].add(target.rel)
            if target.rel != page.rel:
                inbound[target.rel].add(page.rel)

    orphans = [
        {"page": p.rel, "lane": p.lane, "namespace": p.namespace}
        for p in pages
        if not inbound[p.rel]
        and p.stem.casefold() not in HUB_STEMS
        and "orphan" not in p.ignore
    ]

    index = _term_index(pages)
    pattern = _term_regex(list(index))
    suggestions: list[dict[str, Any]] = []
    if pattern is not None:
        for page in pages:
            if "suggest" in page.ignore:
                continue
            seen: set[str] = set()
            for m in pattern.finditer(page.prose):
                target = index.get(_norm(m.group(0)))
                if target is None or target.rel == page.rel:
                    continue
                if target.rel in linked[page.rel] or target.rel in seen:
                    continue
                seen.add(target.rel)
                mention = " ".join(m.group(0).split())
                suggestions.append({
                    "page": page.rel,
                    "lane": page.lane,
                    "line": _line_at(page.prose, m.start(), page.body_line),
                    "mention": mention,
                    "target": target.rel,
                    "link": resolver.link_text(target, mention),
                })

    lanes = {"captured": 0, "authored": 0}
    for p in pages:
        lanes[p.lane] += 1
    return LintReport(
        vault=vault,
        pages=len(pages),
        lanes=lanes,
        orphans=orphans,
        suggestions=sorted(suggestions, key=lambda s: (s["page"], s["line"], s["target"])),
        unresolved=sorted(unresolved, key=lambda u: (u["page"], u["line"], u["target"])),
        skipped=skipped or [],
    )


def lint_vault(vault_dir: Path | str | None = None, personal_dir: Path | str | None = None) -> LintReport:
    """Lint ``<vault_dir>/wiki`` (default ``MUNINN_VAULT_DIR``) read-only.

    Refuses (``VaultAccessError``) a vault that overlaps the personal vault
    (default ``MUNINN_PERSONAL_VAULT_DIR``), with the compiler's guard."""
    from muninn.config import load_paths
    from muninn.consumers.mcp.vault_fs import resolve_wiki_dir

    paths = load_paths()
    vault = Path(vault_dir) if vault_dir is not None else paths.vault_dir
    personal = Path(personal_dir) if personal_dir is not None else paths.personal_vault_dir
    wiki = resolve_wiki_dir(vault, personal, action="lint")
    pages, skipped = load_pages(wiki)
    return lint_pages(pages, vault=str(Path(vault).expanduser().resolve()), skipped=skipped)


# ── output ─────────────────────────────────────────────────────────


def render_text(report: LintReport) -> str:
    out = [
        f"Vault lint ({REPORT_VERSION}): {report.vault}",
        f"Scanned {report.pages} page(s): {report.lanes['captured']} compiled "
        f"(wiki/bookmarks/), {report.lanes['authored']} authored.",
        "Report only: no vault file was changed.",
        "",
        f"Orphans ({len(report.orphans)}): no inbound link from another page",
    ]
    out += [f"  {o['page']} [{o['lane']}]" for o in report.orphans] or ["  none"]
    out += ["", f"Suggested links ({len(report.suggestions)}): mentions another page without linking it"]
    out += [
        f"  {s['page']}:{s['line']} [{s['lane']}] \"{s['mention']}\" -> {s['link']} ({s['target']})"
        for s in report.suggestions
    ] or ["  none"]
    out += ["", f"Unresolved links ({len(report.unresolved)}): resolve to no page"]
    out += [f"  {u['page']}:{u['line']} [{u['lane']}] [[{u['target']}]]" for u in report.unresolved] or ["  none"]
    if report.skipped:
        out += ["", f"Skipped ({len(report.skipped)}):"]
        out += [f"  {s['page']}: {s['reason']}" for s in report.skipped]
    out += [
        "",
        "Not orphan-checked: hub pages named " + ", ".join(HUB_STEMS)
        + f", and pages with `{IGNORE_KEY}: orphan`. Suggestions target "
        + ", ".join(f"{ns}/" for ns in SUGGEST_TARGET_NAMESPACES) + f" pages; `{IGNORE_KEY}: suggest` "
        "turns them off on a page.",
    ]
    return "\n".join(out) + "\n"


def render_json(report: LintReport) -> str:
    return json.dumps(report.to_dict(), indent=2, ensure_ascii=False) + "\n"


__all__ = [
    "COMMON_WORDS",
    "HUB_STEMS",
    "IGNORE_KEY",
    "LintReport",
    "MIN_TERM_CHARS",
    "Page",
    "REPORT_VERSION",
    "Resolver",
    "SUGGEST_TARGET_NAMESPACES",
    "build_page",
    "lint_pages",
    "lint_vault",
    "load_pages",
    "parse_frontmatter",
    "render_json",
    "render_text",
]
