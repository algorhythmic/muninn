"""`muninn status` — pipeline state at a glance.

Counts (from CLAUDE.md):
  - bookmark count
  - per-pass scrape counts
  - enriched count
  - last 10 synthesis_runs (most recent first)
"""

from __future__ import annotations

import click

from muninn.db import connect


def collect_status(db_path=None) -> dict:
    """Pure function so the same data is testable without click capture."""
    conn = connect(db_path) if db_path else connect()
    try:
        bookmark_count = conn.execute(
            "SELECT COUNT(*) FROM bookmarks"
        ).fetchone()[0]

        scrape_rows = conn.execute(
            "SELECT pass, scrape_status, COUNT(*) AS cnt "
            "FROM scrape_results "
            "GROUP BY pass, scrape_status "
            "ORDER BY pass, scrape_status"
        ).fetchall()
        scrape_counts = [dict(r) for r in scrape_rows]

        enriched_count = conn.execute(
            "SELECT COUNT(*) FROM enriched"
        ).fetchone()[0]

        synth_rows = conn.execute(
            "SELECT synthesis_run_id, task_id, task_type, attempt, status, "
            "       started_at, completed_at "
            "FROM synthesis_runs "
            "ORDER BY synthesis_run_id DESC LIMIT 10"
        ).fetchall()
        recent_synthesis_runs = [dict(r) for r in synth_rows]

        return {
            "bookmark_count": bookmark_count,
            "scrape_counts": scrape_counts,
            "enriched_count": enriched_count,
            "recent_synthesis_runs": recent_synthesis_runs,
        }
    finally:
        conn.close()


@click.command("status")
@click.pass_context
def status_cmd(ctx: click.Context) -> None:
    """Show pipeline state: bookmark count, scrape stats, enrichment, synth runs."""
    db_path = ctx.obj.get("db") if ctx.obj else None
    s = collect_status(db_path)

    click.echo(f"Bookmarks: {s['bookmark_count']}")
    click.echo(f"Enriched: {s['enriched_count']}")
    click.echo("")
    click.echo("Scrape results by (pass, status):")
    if s["scrape_counts"]:
        for r in s["scrape_counts"]:
            click.echo(f"  {r['pass']:<16} {r['scrape_status']:<20} {r['cnt']}")
    else:
        click.echo("  (none)")

    click.echo("")
    click.echo("Recent synthesis runs:")
    if s["recent_synthesis_runs"]:
        for r in s["recent_synthesis_runs"]:
            click.echo(
                f"  [{r['synthesis_run_id']}] {r['task_type']:<16} "
                f"attempt={r['attempt']} status={r['status']} "
                f"task_id={r['task_id']}"
            )
    else:
        click.echo("  (none)")


__all__ = ["status_cmd", "collect_status"]
