#!/usr/bin/env python3
"""skald-lint — validate Skald Protocol pages (docs/skald-protocol.md §9).

The canonical copy lives in the muninn repo at scripts/skald_lint.py; the copy
in the vault repo's scripts/ is a deployment artifact of it.

Checks (protocol §9):
  1. Frontmatter parses; required fields present; enums valid.
  2. Filename agrees with `date` + primary project slug.
  3. Every `projects:` slug resolves to wiki/projects/{slug}.md in the same tree.
  4. Managed markers balanced on any touched project page.
  5. `sources:` paths that point into raw/ exist.
  6. Bootstrap mode: wiki/log.md gained a matching line (skip: --skip-log-check).

Usage: skald_lint.py [--vault PATH] [--skip-log-check] [page ...]
With no page arguments, lints every wiki/sessions/ page and every
wiki/projects/ page. Vault root resolution: --vault, then $SKALD_VAULT_PATH,
then upward from the first page argument, then the working directory.

Exit codes: 0 clean (warnings allowed), 1 findings, 2 usage error.
Uses PyYAML when available, else a built-in parser sufficient for the
protocol's frontmatter shapes (scalars, inline and block lists, quoted
strings; inline comments after values are not supported).
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # vault clones without a venv still get a working linter
    yaml = None

EMITTERS = {"note-skill", "stop-hook", "bragi"}
OUTCOMES = {"shipped", "partial", "blocked", "exploratory"}
LIST_FIELDS = ("projects", "decisions", "next_actions", "files_touched", "sources", "tags", "agents")
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MARKER_RE = re.compile(r"<!--\s*skald:(begin|end)\s+([a-z0-9-]+)\s*-->")
STANDARD_REGIONS = ("current-state", "open-questions", "recent-sessions")
RECENT_SESSIONS_CAP = 10


class Findings:
    def __init__(self) -> None:
        self.errors: list[tuple[Path, str]] = []
        self.warnings: list[tuple[Path, str]] = []

    def error(self, path: Path, msg: str) -> None:
        self.errors.append((path, msg))

    def warn(self, path: Path, msg: str) -> None:
        self.warnings.append((path, msg))


# --- frontmatter parsing ----------------------------------------------------

def _parse_scalar(tok: str):
    tok = tok.strip()
    if len(tok) >= 2 and tok[0] in "'\"" and tok.endswith(tok[0]):
        return tok[1:-1]
    if re.fullmatch(r"-?\d+", tok):
        return int(tok)
    if tok.lower() in ("true", "false"):
        return tok.lower() == "true"
    return tok


def _split_inline(body: str) -> list[str]:
    items, buf, quote = [], "", None
    for ch in body:
        if quote:
            buf += ch
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
            buf += ch
        elif ch == ",":
            items.append(buf)
            buf = ""
        else:
            buf += ch
    items.append(buf)
    return [x.strip() for x in items if x.strip()]


def _fallback_yaml(block: str) -> dict:
    data: dict = {}
    key: str | None = None
    for raw in block.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        stripped = raw.lstrip()
        if stripped.startswith("- ") or stripped == "-":
            if key is None or not isinstance(data.get(key), list):
                raise ValueError(f"list item outside a list context: {raw!r}")
            data[key].append(_parse_scalar(stripped[1:].strip()))
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):(.*)$", stripped)
        if not m:
            raise ValueError(f"unparseable frontmatter line: {raw!r}")
        key, rest = m.group(1), m.group(2).strip()
        if not rest:
            data[key] = []  # block list (or empty value) follows
        elif rest.startswith("[") and rest.endswith("]"):
            data[key] = [_parse_scalar(x) for x in _split_inline(rest[1:-1])]
        else:
            data[key] = _parse_scalar(rest)
    return data


def _normalize(value):
    if isinstance(value, datetime.date):  # covers datetime.datetime too
        return value.isoformat()
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    return value


def load_frontmatter(path: Path, findings: Findings) -> dict | None:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        findings.error(path, "no frontmatter block (file must start with '---')")
        return None
    end = text.find("\n---", 4)
    if end == -1:
        findings.error(path, "frontmatter block never closed")
        return None
    block = text[4:end]
    try:
        data = yaml.safe_load(block) if yaml is not None else _fallback_yaml(block)
    except Exception as exc:
        findings.error(path, f"frontmatter does not parse: {exc}")
        return None
    if not isinstance(data, dict):
        findings.error(path, "frontmatter is not a mapping")
        return None
    return {k: _normalize(v) for k, v in data.items()}


# --- managed-region markers (check 4) ---------------------------------------

def marker_errors(text: str) -> list[str]:
    errs: list[str] = []
    open_name: str | None = None
    for m in MARKER_RE.finditer(text):
        kind, name = m.group(1), m.group(2)
        if kind == "begin":
            if open_name is not None:
                errs.append(f"'skald:begin {name}' opened while '{open_name}' is still open")
            open_name = name
        else:
            if open_name is None:
                errs.append(f"orphan 'skald:end {name}' with no open region")
            elif open_name != name:
                errs.append(f"'skald:end {name}' closes 'skald:begin {open_name}'")
                open_name = None
            else:
                open_name = None
    if open_name is not None:
        errs.append(f"unclosed 'skald:begin {open_name}'")
    return errs


def _region_body(text: str, name: str) -> str | None:
    m = re.search(
        rf"<!--\s*skald:begin\s+{re.escape(name)}\s*-->(.*?)<!--\s*skald:end\s+{re.escape(name)}\s*-->",
        text,
        re.DOTALL,
    )
    return m.group(1) if m else None


def lint_project_page(path: Path, findings: Findings) -> None:
    text = path.read_text(encoding="utf-8")
    for err in marker_errors(text):
        findings.error(path, err)
    fm = load_frontmatter(path, findings)
    if fm is not None and fm.get("page_type") != "project":
        findings.warn(path, f"page_type is {fm.get('page_type')!r}, expected 'project'")
    for region in STANDARD_REGIONS:
        if f"skald:begin {region}" not in text:
            findings.warn(path, f"standard region '{region}' missing")
    recent = _region_body(text, "recent-sessions")
    if recent is not None:
        entries = [ln for ln in recent.splitlines() if ln.lstrip().startswith("- ")]
        if len(entries) > RECENT_SESSIONS_CAP:
            findings.warn(path, f"recent-sessions has {len(entries)} entries (cap {RECENT_SESSIONS_CAP})")


# --- session pages (checks 1-3, 5, 6) ----------------------------------------

def lint_session_page(
    path: Path,
    vault: Path,
    findings: Findings,
    skip_log_check: bool,
    checked_projects: set[Path],
) -> None:
    fm = load_frontmatter(path, findings)
    if fm is None:
        return

    # Check 1 — required fields, types, enums.
    schema_version = fm.get("schema_version")
    if not isinstance(schema_version, int) or isinstance(schema_version, bool) or schema_version != 1:
        findings.error(path, f"schema_version must be the int 1, got {schema_version!r}")
    if fm.get("page_type") != "session":
        findings.error(path, f"page_type must be 'session', got {fm.get('page_type')!r}")
    title = fm.get("title")
    if not isinstance(title, str) or not title.strip():
        findings.error(path, "title is required and must be a non-empty string")
    date = fm.get("date")
    if not isinstance(date, str) or not DATE_RE.match(date):
        findings.error(path, f"date must be an ISO date (YYYY-MM-DD), got {date!r}")
        date = None
    else:
        try:
            datetime.date.fromisoformat(date)
        except ValueError:
            findings.error(path, f"date {date!r} is not a real calendar date")
            date = None
    emitter = fm.get("emitter")
    if emitter not in EMITTERS:
        findings.error(path, f"emitter {emitter!r} is not in the registry {sorted(EMITTERS)}")
    elif emitter == "stop-hook":
        findings.error(path, "emitter 'stop-hook' never writes wiki/ pages (protocol §3)")
    outcome = fm.get("outcome")
    if outcome not in OUTCOMES:
        findings.error(path, f"outcome {outcome!r} must be one of {sorted(OUTCOMES)}")
    for field in LIST_FIELDS:
        if field in fm and not isinstance(fm[field], list):
            findings.error(path, f"{field} must be a list, got {type(fm[field]).__name__}")
    duration = fm.get("duration_min")
    if duration is not None and (not isinstance(duration, int) or isinstance(duration, bool)):
        findings.error(path, f"duration_min must be an int, got {duration!r}")

    projects = fm.get("projects")
    primary: str | None = None
    if not isinstance(projects, list) or not projects:
        findings.error(path, "projects is required and must be a non-empty list of slugs")
        projects = []
    else:
        for slug in projects:
            if not isinstance(slug, str) or not SLUG_RE.match(slug):
                findings.error(path, f"project slug {slug!r} is not lowercase-kebab")
        if isinstance(projects[0], str):
            primary = projects[0]

    # Check 2 — filename agrees with date + primary project slug.
    stem = path.stem
    if date and primary:
        prefix = f"{date}-{primary}-"
        if not stem.startswith(prefix):
            findings.error(path, f"filename {stem!r} must start with {prefix!r} (date + primary project)")
        elif not SLUG_RE.match(stem[len(prefix):]):
            findings.error(path, f"filename slug {stem[len(prefix):]!r} is not lowercase-kebab")

    # Check 3 — every slug resolves; check 4 on the pages this session touched.
    for slug in projects:
        if not isinstance(slug, str):
            continue
        project_page = vault / "wiki" / "projects" / f"{slug}.md"
        if not project_page.is_file():
            findings.error(path, f"projects: slug '{slug}' does not resolve to wiki/projects/{slug}.md")
        elif project_page not in checked_projects:
            checked_projects.add(project_page)
            lint_project_page(project_page, findings)

    # Check 5 — sources pointing into raw/ must exist (files or evidence dirs).
    for src in fm.get("sources", []) or []:
        if isinstance(src, str) and src.startswith("raw/"):
            target = vault / src.rstrip("/")
            if not target.exists():
                findings.error(path, f"sources: {src!r} does not exist in the vault")

    # Check 6 — bootstrap mode: the log gained a matching line.
    if not skip_log_check:
        log = vault / "wiki" / "log.md"
        if not log.is_file():
            findings.error(path, "wiki/log.md is missing (bootstrap mode requires it)")
        elif f"[[sessions/{stem}]]" not in log.read_text(encoding="utf-8"):
            findings.error(path, f"wiki/log.md has no line referencing [[sessions/{stem}]]")


# --- driver ------------------------------------------------------------------

def resolve_vault(vault_arg: str | None, pages: list[Path]) -> Path | None:
    candidates: list[Path] = []
    if vault_arg:
        candidates.append(Path(vault_arg))
    elif os.environ.get("SKALD_VAULT_PATH"):
        candidates.append(Path(os.environ["SKALD_VAULT_PATH"]))
    else:
        if pages:
            candidates.extend(pages[0].resolve().parents)
        candidates.append(Path.cwd())
    for cand in candidates:
        if (cand / "wiki").is_dir():
            return cand.resolve()
    return None


def _rel(path: Path, vault: Path) -> str:
    try:
        return str(path.resolve().relative_to(vault))
    except ValueError:
        return str(path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Validate Skald Protocol pages (protocol §9).")
    ap.add_argument("pages", nargs="*", type=Path, help="session/project pages; default: whole vault")
    ap.add_argument("--vault", help="vault root (default: $SKALD_VAULT_PATH, page parents, or cwd)")
    ap.add_argument("--skip-log-check", action="store_true", help="skip check 6 (watcher mode)")
    args = ap.parse_args(argv)

    vault = resolve_vault(args.vault, args.pages)
    if vault is None:
        print("skald-lint: cannot find a vault root (no wiki/ directory)", file=sys.stderr)
        return 2

    findings = Findings()
    checked_projects: set[Path] = set()
    sessions_dir = vault / "wiki" / "sessions"
    projects_dir = vault / "wiki" / "projects"

    session_pages: list[Path] = []
    project_pages: list[Path] = []
    if args.pages:
        for page in args.pages:
            page = page.resolve()
            if not page.is_file():
                findings.error(page, "no such file")
            elif page.parent == sessions_dir:
                session_pages.append(page)
            elif page.parent == projects_dir:
                project_pages.append(page)
            else:
                findings.error(page, "not under wiki/sessions/ or wiki/projects/ of the resolved vault")
    else:
        session_pages = sorted(p for p in sessions_dir.glob("*.md") if p.name != "README.md")
        project_pages = sorted(p for p in projects_dir.glob("*.md") if p.name != "README.md")

    for page in session_pages:
        lint_session_page(page, vault, findings, args.skip_log_check, checked_projects)
    for page in project_pages:
        if page not in checked_projects:
            checked_projects.add(page)
            lint_project_page(page, findings)

    for path, msg in findings.errors:
        print(f"ERROR {_rel(path, vault)}: {msg}")
    for path, msg in findings.warnings:
        print(f"WARN  {_rel(path, vault)}: {msg}")
    checked = len(session_pages) + len(checked_projects)
    print(f"skald-lint: {checked} page(s) checked, {len(findings.errors)} error(s), "
          f"{len(findings.warnings)} warning(s)")
    return 1 if findings.errors else 0


if __name__ == "__main__":
    sys.exit(main())
