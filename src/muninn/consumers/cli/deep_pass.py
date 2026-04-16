"""`muninn deep-pass <id>` — flag a bookmark for deep-pass synthesis.

This only flips `enriched.deep_pass_requested = 1`. The actual Opus run is
launched separately by `muninn synthesize deep-pass`, which lives under the
synthesis package.
"""

from __future__ import annotations

import sys

import click

from muninn.db import connect, transaction


def request_deep_pass(bookmark_id: int, db_path=None) -> bool:
    """Set the flag. Returns True on success, False if no enriched row exists."""
    conn = connect(db_path) if db_path else connect()
    try:
        row = conn.execute(
            "SELECT bookmark_id FROM enriched WHERE bookmark_id = ?",
            (bookmark_id,),
        ).fetchone()
        if row is None:
            return False
        with transaction(conn):
            conn.execute(
                "UPDATE enriched SET deep_pass_requested = 1 "
                "WHERE bookmark_id = ?",
                (bookmark_id,),
            )
        return True
    finally:
        conn.close()


@click.command("deep-pass")
@click.argument("bookmark_id", type=int)
@click.pass_context
def deep_pass_cmd(ctx: click.Context, bookmark_id: int) -> None:
    """Flag a bookmark for deep-pass synthesis (sets deep_pass_requested=1)."""
    db_path = ctx.obj.get("db") if ctx.obj else None
    ok = request_deep_pass(bookmark_id, db_path)
    if not ok:
        click.echo(
            f"Error: no enriched row for bookmark {bookmark_id}. "
            f"Run `muninn enrich` first.",
            err=True,
        )
        sys.exit(1)
    click.echo(f"Deep-pass requested for bookmark {bookmark_id}.")


__all__ = ["deep_pass_cmd", "request_deep_pass"]
