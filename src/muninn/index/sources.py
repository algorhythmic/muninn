"""Read-only captured-source projection, shared with response hydration.

The caller owns the SQLite transaction: a publisher should enumerate and resolve
inside one read transaction, then release it before inference. No embeddings,
filesystem artifacts or backend dependencies are loaded here. Authored inventory
and relationships are separate work; this module does not imply their coverage.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from typing import Iterator

from muninn.consumers.packets.canonical import content_digest, normalize_text
from muninn.consumers.vault import x_pages


VISIBLE_ROW_SQL = (
    "SELECT b.bookmark_id, b.title, b.url, b.era_label, b.domain, "
    "b.source, b.source_id, b.source_metadata, e.summary "
    "FROM bookmarks b LEFT JOIN enriched e ON e.bookmark_id = b.bookmark_id "
    "WHERE b.bookmark_id = ? AND b.content_visible = 1"
)


def visible_row(conn: sqlite3.Connection, bookmark_id: int) -> sqlite3.Row | None:
    return conn.execute(VISIBLE_ROW_SQL, (bookmark_id,)).fetchone()


def hidden_urls(conn: sqlite3.Connection, row: sqlite3.Row) -> set[str]:
    if row["source"] not in x_pages.X_SOURCES:
        return set()
    return x_pages.hidden_child_urls(conn, row["bookmark_id"])


def visible_title(row: sqlite3.Row, hidden: set[str]) -> str | None:
    if row["source"] != "x":
        return row["title"]
    if not row["title"]:
        return x_pages.display_name(row)
    return x_pages.mask_hidden_title(row["title"], hidden)


def visible_summary(row: sqlite3.Row, hidden: set[str]) -> str | None:
    summary = row["summary"] if "summary" in row.keys() else None
    if summary and hidden:
        summary, _, _, _ = x_pages.withhold_enrichment(hidden, summary, None, None)
    return summary


@dataclass(frozen=True)
class CapturedBody:
    """Exact local capture plus normalized, visibility-filtered body.

    Legacy rows retain a local record reference and never gain an invented
    archival revision. ``source_digest`` describes the captured text;
    ``text_digest`` describes the text permitted for indexing and citation.
    """

    source_reference: str
    revision_ulid: str | None
    source_digest: str
    text_digest: str
    text: str
    scrape_pass: str
    freshness: str
    coverage: str
    retrieved_at: int
    archive_snapshot_at: int | None
    published_at: int | None


@dataclass(frozen=True)
class CapturedSource:
    corpus_id: str
    bookmark_id: int
    title: str | None
    url: str | None
    summary: str | None
    tags: tuple[str, ...]
    source: str
    captured_at: int
    body: CapturedBody | None
    omissions: tuple[str, ...]

    @property
    def document_id(self) -> str:
        # Encode rather than concatenate caller-controlled corpus delimiters.
        return "captured:" + json.dumps([self.corpus_id, self.bookmark_id], separators=(",", ":"))

    @property
    def projection_revision(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _body(conn: sqlite3.Connection, row: sqlite3.Row, hidden: set[str]) -> tuple[CapturedBody | None, str | None]:
    # Same selection as X vault serving: selected enrichment capture, else live.
    # X post text itself is always its current API/live row.
    selected_pass = x_pages.selected_scrape_pass(row, for_body=True)
    scrape = conn.execute(
        "SELECT * FROM scrape_results WHERE bookmark_id = ? AND pass = ?",
        (row["bookmark_id"], selected_pass),
    ).fetchone()
    if scrape is None and selected_pass != "live":
        scrape = conn.execute(
            "SELECT * FROM scrape_results WHERE bookmark_id = ? AND pass = 'live'",
            (row["bookmark_id"],),
        ).fetchone()
    if scrape is None:
        return None, "body_missing"
    if scrape["scrape_status"] not in ("ok", "partial"):
        return None, "body_unavailable:" + scrape["scrape_status"]
    raw = normalize_text(scrape["content_text"])
    if not raw:
        return None, "body_empty"
    digest = content_digest(raw)
    # Only attach a revision with exact content and capture provenance. A local
    # mutable scrape row plus its digest remains sufficient for legacy records.
    revision = conn.execute(
        "SELECT revision_ulid, published_at FROM scrape_history "
        "WHERE bookmark_id = ? AND content_sha256 = ? AND withdrawn_at IS NULL "
        "AND ((scrape_pass = ?) OR (retrieval_method = 'x_api' AND ? = 'live')) "
        "ORDER BY retrieved_at DESC, scrape_history_id DESC LIMIT 1",
        (row["bookmark_id"], digest, scrape["pass"], scrape["pass"]),
    ).fetchone()
    text = normalize_text(x_pages.mask_hidden_links(raw, hidden)) if hidden else raw
    return CapturedBody(
        source_reference=f"scrape_results:{scrape['scrape_result_id']}",
        revision_ulid=revision["revision_ulid"] if revision else None,
        source_digest=digest, text_digest=content_digest(text), text=text,
        scrape_pass=scrape["pass"],
        freshness="historical" if scrape["pass"] in ("at_capture", "recent_archive") else "current",
        coverage="partial" if scrape["scrape_status"] == "partial" or scrape["extraction_quality"] == "partial" else "full",
        retrieved_at=scrape["fetched_at"], archive_snapshot_at=scrape["actual_snapshot_at"],
        published_at=revision["published_at"] if revision else None,
    ), None


def resolve_captured(
    conn: sqlite3.Connection, corpus_id: str, bookmark_id: int,
    *, expected_revision: str | None = None,
) -> CapturedSource | None:
    """Re-read canonical visibility/content; stale projections return None.

    This is suitable for request-time hydration: an indexed document never
    grants access to an older body, even when its bookmark remains visible.
    """
    row = conn.execute(
        "SELECT b.*, e.summary, e.tags, e.key_quotes, e.entities "
        "FROM bookmarks b LEFT JOIN enriched e ON e.bookmark_id = b.bookmark_id "
        "WHERE b.bookmark_id = ? AND b.content_visible = 1", (bookmark_id,),
    ).fetchone()
    if row is None:
        return None
    hidden = hidden_urls(conn, row)
    summary = row["summary"]
    omissions = []
    tags = json.loads(row["tags"] or "[]")
    tags = [t for t in tags if isinstance(t, str)] if isinstance(tags, list) else []
    if hidden:
        summary, _, _, withheld = x_pages.withhold_enrichment(
            hidden, summary, row["key_quotes"], row["entities"]
        )
        # Tags are derived too. Once any enrichment field demonstrates a hidden
        # input, suppress all derived metadata, including paraphrased tags.
        if withheld or x_pages.enrichment_quotes_hidden(hidden, tags):
            summary = None
            tags = []
            omissions.append("enrichment_withheld")
    tags = tuple(tags)
    body, omission = _body(conn, row, hidden)
    if omission:
        omissions.append(omission)
    projected = CapturedSource(
        corpus_id, bookmark_id, visible_title(row, hidden), row["url"], summary,
        tags, row["source"], row["captured_at"], body, tuple(omissions),
    )
    if expected_revision is not None and projected.projection_revision != expected_revision:
        return None
    return projected


def iter_captured(conn: sqlite3.Connection, corpus_id: str) -> Iterator[CapturedSource]:
    """Complete captured inventory, streamed in deterministic bookmark order.

    No query-ranked scan or limit is applied. The caller must keep a read
    transaction open for a consistent snapshot and spool before inference.
    """
    for row in conn.execute("SELECT bookmark_id FROM bookmarks WHERE content_visible = 1 ORDER BY bookmark_id"):
        source = resolve_captured(conn, corpus_id, row["bookmark_id"])
        if source is not None:
            yield source
