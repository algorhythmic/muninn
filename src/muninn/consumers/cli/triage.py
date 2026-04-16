"""`muninn triage` — list visible bookmarks needing attention.

Canonical query (from CLAUDE.md):
    visible bookmarks where enrichment is missing OR any scrape pass failed.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import click

from muninn.db import connect


TRIAGE_SQL = """
SELECT b.bookmark_id, b.title, b.url, b.enrichment_source,
       (SELECT GROUP_CONCAT(pass || ':' || scrape_status, ', ')
        FROM scrape_results WHERE bookmark_id = b.bookmark_id) AS scrape_summary
FROM bookmarks b
WHERE  b.content_visible = 1
  AND  (b.enrichment_source = 'none' OR b.enrichment_source IS NULL
        OR EXISTS (SELECT 1 FROM scrape_results sr
                   WHERE sr.bookmark_id = b.bookmark_id
                     AND sr.scrape_status NOT IN ('ok', 'partial')))
ORDER BY b.bookmark_id
"""


def run_triage(db_path: str | Path | None = None) -> list[dict]:
    """Return triage rows as a list of dicts (testable, no I/O on stdout)."""
    conn = connect(db_path) if db_path else connect()
    try:
        rows = conn.execute(TRIAGE_SQL).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@click.command("triage")
@click.option("--json", "as_json", is_flag=True, help="Output machine-parseable JSON.")
@click.pass_context
def triage_cmd(ctx: click.Context, as_json: bool) -> None:
    """List visible bookmarks needing attention (failed scrapes, un-enriched)."""
    db_path = ctx.obj.get("db") if ctx.obj else None
    rows = run_triage(db_path)

    if as_json:
        click.echo(json.dumps({"triage": rows}, indent=2, default=str))
        return

    if not rows:
        click.echo("Nothing to triage. Pipeline is clean.")
        return

    click.echo(f"Triage ({len(rows)} row(s) need attention):")
    for r in rows:
        title = r.get("title") or "(untitled)"
        src = r.get("enrichment_source") or "none"
        scrape = r.get("scrape_summary") or "-"
        click.echo(
            f"  [{r['bookmark_id']}] {title}  "
            f"enrichment_source={src}  scrape={scrape}"
        )


__all__ = ["triage_cmd", "run_triage", "TRIAGE_SQL"]
