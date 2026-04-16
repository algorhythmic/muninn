"""End-to-end ingest of a Netscape Bookmark HTML file.

  raw/bookmarks.html  →  parse  →  sanitize_url  →  domain_policy  →  upsert

Idempotent: re-running yields the same `bookmarks.bookmark_id` for the
same `(source, source_id)` because we upsert via the unique constraint
and let SQLite preserve the auto-increment row id.

Per-bookmark `source_id` is `SHA-256(raw_url + "|" + add_date_str)`. The
raw URL is used here (NOT the sanitized form) because the same site
may appear with different secret-bearing URLs that, post-sanitization,
collapse to the same canonical URL — keeping each event distinct
preserves the bookmark-as-event semantics. The raw URL itself is never
stored after this point; only its hash is.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from muninn.ingest.bookmarks_html import ParsedBookmark, parse_bookmarks_html
from muninn.sanitize import sanitize_url
from muninn.scrape.domain_policy import DomainPolicy, load_domain_policy

SOURCE_NAME = "bookmarks_html"


@dataclass
class IngestStats:
    parsed: int = 0
    inserted_or_updated: int = 0
    parse_errors: int = 0
    hidden_by_policy: int = 0
    bookmark_ids: list[int] = field(default_factory=list)


def ingest_html(
    html_path: Path | str,
    conn: sqlite3.Connection,
    policy: DomainPolicy | None = None,
) -> IngestStats:
    """Ingest a Netscape Bookmark HTML export into the `bookmarks` table.

    Args:
        html_path: path to the export.
        conn: open SQLite connection (canonical Muninn schema applied).
        policy: optional pre-loaded `DomainPolicy`. If None, loads the
            repo-root `domain_policy.yml`.

    Returns:
        IngestStats summarizing the run. Re-running on the same input
        leaves row count unchanged and `bookmark_id` values stable.
    """
    html_path = Path(html_path)
    if policy is None:
        policy = load_domain_policy()

    content = html_path.read_text(encoding="utf-8", errors="replace")
    parsed = parse_bookmarks_html(content)

    stats = IngestStats(parsed=len(parsed))

    for bm in parsed:
        result = sanitize_url(bm.raw_url)

        source_id = _source_id(bm)
        captured_at = bm.add_date if bm.add_date is not None else 0
        url = result.sanitized_url
        domain = _domain_of(url)

        # Domain policy gates content_visible (per Decision 5: never excludes
        # the row from `bookmarks`, only gates downstream stages).
        hidden = policy.matches(url) if url is not None else False
        if hidden:
            stats.hidden_by_policy += 1
        content_visible = 0 if hidden else 1

        source_metadata: dict[str, object] = {}
        if bm.icon_uri:
            source_metadata["icon_uri"] = bm.icon_uri
        if bm.tags:
            source_metadata["tags"] = bm.tags
        if bm.last_modified is not None:
            source_metadata["last_modified"] = bm.last_modified
        if result.userinfo_redacted:
            source_metadata["userinfo_redacted"] = True
        if result.parse_error is not None:
            source_metadata["parse_error"] = result.parse_error
            stats.parse_errors += 1

        folder_path_json = (
            json.dumps(bm.folder_path) if bm.folder_path else None
        )
        redacted_names_json = (
            json.dumps(result.redacted_param_names)
            if result.redacted_param_names
            else None
        )
        metadata_json = json.dumps(source_metadata) if source_metadata else None

        conn.execute(
            """
            INSERT INTO bookmarks (
                source, source_id, captured_at, title, url,
                folder_path, era_label, domain, source_metadata,
                redacted_param_count, redacted_param_names,
                path_redacted, content_visible
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (source, source_id) DO UPDATE SET
                captured_at = excluded.captured_at,
                title = excluded.title,
                url = excluded.url,
                folder_path = excluded.folder_path,
                era_label = excluded.era_label,
                domain = excluded.domain,
                source_metadata = excluded.source_metadata,
                redacted_param_count = excluded.redacted_param_count,
                redacted_param_names = excluded.redacted_param_names,
                path_redacted = excluded.path_redacted,
                content_visible = excluded.content_visible
            """,
            (
                SOURCE_NAME,
                source_id,
                captured_at,
                bm.title,
                url,
                folder_path_json,
                bm.era_label,
                domain,
                metadata_json,
                result.redacted_param_count,
                redacted_names_json,
                1 if result.path_redacted else 0,
                content_visible,
            ),
        )
        stats.inserted_or_updated += 1

        row = conn.execute(
            "SELECT bookmark_id FROM bookmarks WHERE source = ? AND source_id = ?",
            (SOURCE_NAME, source_id),
        ).fetchone()
        if row is not None:
            stats.bookmark_ids.append(row[0])

    conn.commit()
    return stats


# ── helpers ─────────────────────────────────────────────────────────────

def _source_id(bm: ParsedBookmark) -> str:
    """Deterministic per-bookmark identifier within `bookmarks_html` source.

    Uses the **raw** URL plus add_date so two distinct bookmarks of the
    same canonical URL (e.g., one with a different secret query param)
    remain separate rows. The raw URL is hashed, not stored.
    """
    add_date_part = "" if bm.add_date is None else str(bm.add_date)
    payload = f"{bm.raw_url}|{add_date_part}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _domain_of(url: str | None) -> str | None:
    if not url:
        return None
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    return host.lower() if host else None
