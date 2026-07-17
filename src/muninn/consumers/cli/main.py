"""Muninn CLI dispatcher.

Registered in `pyproject.toml` as `muninn = "muninn.consumers.cli.main:cli"`.

All cross-package callouts (ingest, scrape, enrich, synthesis, vault,
parquet, timeline, mcp) are LAZY — imported inside the command body so
`muninn --help` always works even if a sibling stream is mid-rewrite or has
import-time side effects.

Subcommand layout matches the SPEC's pipeline order:

    muninn ingest <path>
    muninn scrape [--concurrency N]
    muninn enrich [--dry-run] [--force] [--prompt-version vN]
    muninn synthesize era <era_label>
    muninn synthesize deep-pass <id> | --pending
    muninn synthesize analyze "<prompt>"
    muninn triage [--json]
    muninn status
    muninn deep-pass <id>             # just sets the flag
    muninn export parquet --out p.parquet
    muninn timeline [--out p.json]
    muninn vault compile
    muninn mcp
"""

from __future__ import annotations

import sys

import click

from .deep_pass import deep_pass_cmd
from .status import status_cmd
from .triage import triage_cmd


@click.group()
@click.option(
    "--db",
    envvar="MUNINN_DB_PATH",
    default=None,
    help="Path to the muninn SQLite database (overrides MUNINN_DB_PATH).",
)
@click.pass_context
def cli(ctx: click.Context, db: str | None) -> None:
    """Muninn — bookmark corpus pipeline + consumers."""
    ctx.ensure_object(dict)
    ctx.obj["db"] = db


# ── pipeline (lazy-imported owners) ────────────────────────────────


@cli.command("ingest")
@click.argument("path", type=click.Path(exists=True, dir_okay=False))
@click.pass_context
def ingest_cmd(ctx: click.Context, path: str) -> None:
    """Ingest a Netscape bookmarks.html file into normalized rows."""
    from muninn.db import connect  # lazy
    from muninn.ingest.pipeline import ingest_html  # lazy

    conn = connect(ctx.obj.get("db"))
    try:
        stats = ingest_html(path, conn)
    finally:
        conn.close()
    click.echo(
        f"Ingested {path}: parsed={stats.parsed} "
        f"upserted={stats.inserted_or_updated} "
        f"hidden_by_policy={stats.hidden_by_policy} "
        f"parse_errors={stats.parse_errors}"
    )


@cli.command("scrape")
@click.option(
    "--concurrency",
    type=int,
    default=4,
    help="Max concurrent bookmarks scraped (each runs dual-pass serially).",
)
@click.pass_context
def scrape_cmd(ctx: click.Context, concurrency: int) -> None:
    """Run dual-pass scrape (live + at_capture + recent_archive fallback) for visible bookmarks."""
    from muninn.scrape.pipeline import run_scrape  # lazy

    result = run_scrape(db_path=ctx.obj.get("db"), concurrency=concurrency)
    click.echo(f"Scrape complete: {result}")


@cli.command("enrich")
@click.option("--dry-run", is_flag=True, help="Plan calls without invoking Anthropic API or Qdrant.")
@click.option("--force", is_flag=True, help="Re-enrich even when the idempotency triple matches (burns tokens).")
@click.option("--prompt-version", default=None, help="Override the enrichment prompt version (re-enriches everything under the new triple).")
@click.pass_context
def enrich_cmd(ctx: click.Context, dry_run: bool, force: bool, prompt_version: str | None) -> None:
    """Per-bookmark Haiku enrichment with idempotency by content_hash."""
    from muninn.db import connect  # lazy
    from muninn.enrich.pipeline import enrich_all  # lazy

    conn = connect(ctx.obj.get("db"))
    try:
        stats = enrich_all(conn, dry_run=dry_run, force=force, prompt_version=prompt_version)
    finally:
        conn.close()
    click.echo(f"Enrich complete: {stats}")


# ── synthesize ─────────────────────────────────────────────────────


@cli.group("synthesize")
@click.pass_context
def synthesize_group(ctx: click.Context) -> None:
    """Opus-driven synthesis: era narratives, deep passes, ad-hoc analyses."""


@synthesize_group.command("era")
@click.argument("era_label")
@click.pass_context
def synthesize_era_cmd(ctx: click.Context, era_label: str) -> None:
    """Generate the per-era narrative for `<era_label>`."""
    from muninn.db import connect  # lazy
    from muninn.synthesis.orchestrator import run_era_narrative  # lazy

    conn = connect(ctx.obj.get("db"))
    try:
        result = run_era_narrative(era_label, conn=conn)
    finally:
        conn.close()
    click.echo(f"Synthesize era '{era_label}': {result.status} (task_id={result.task_id})")


@synthesize_group.command("deep-pass")
@click.argument("bookmark_id", required=False, type=int)
@click.option("--pending", is_flag=True, help="Process all rows with deep_pass_requested=1.")
@click.pass_context
def synthesize_deep_pass_cmd(
    ctx: click.Context, bookmark_id: int | None, pending: bool
) -> None:
    """Run the Opus deep-pass for one bookmark (or `--pending` for the queue)."""
    if not pending and bookmark_id is None:
        click.echo("Error: pass a BOOKMARK_ID or use --pending.", err=True)
        sys.exit(2)

    from muninn.db import connect  # lazy
    from muninn.synthesis.orchestrator import run_deep_pass  # lazy

    conn = connect(ctx.obj.get("db"))
    try:
        if pending:
            ids = [
                r[0]
                for r in conn.execute(
                    "SELECT bookmark_id FROM enriched WHERE deep_pass_requested = 1"
                ).fetchall()
            ]
            if not ids:
                click.echo("No pending deep-pass rows.")
                return
            for bid in ids:
                result = run_deep_pass(bid, conn=conn)
                click.echo(f"  bookmark_id={bid}: {result.status}")
        else:
            result = run_deep_pass(bookmark_id, conn=conn)
            click.echo(f"Synthesize deep-pass {bookmark_id}: {result.status}")
    finally:
        conn.close()


@synthesize_group.command("analyze")
@click.argument("prompt")
@click.pass_context
def synthesize_analyze_cmd(ctx: click.Context, prompt: str) -> None:
    """Run an ad-hoc cross-corpus analysis with `prompt`."""
    from muninn.db import connect  # lazy
    from muninn.synthesis.orchestrator import run_ad_hoc_analysis  # lazy

    conn = connect(ctx.obj.get("db"))
    try:
        result = run_ad_hoc_analysis(prompt, conn=conn)
    finally:
        conn.close()
    click.echo(f"Synthesize analyze: {result.status}")


# ── consumer commands (this stream owns these) ─────────────────────


cli.add_command(triage_cmd)
cli.add_command(status_cmd)
cli.add_command(deep_pass_cmd)


@cli.group("export")
@click.pass_context
def export_group(ctx: click.Context) -> None:
    """Export the corpus to external formats."""


@export_group.command("parquet")
@click.option("--out", required=True, type=click.Path(dir_okay=False), help="Output .parquet path.")
@click.pass_context
def export_parquet_cmd(ctx: click.Context, out: str) -> None:
    """Export bookmarks ⨝ enriched as Parquet via DuckDB."""
    from muninn.consumers.parquet.export import export_parquet  # lazy

    count = export_parquet(out_path=out, db_path=ctx.obj.get("db"))
    click.echo(f"Exported {count} row(s) to {out}.")


@cli.command("timeline")
@click.option("--out", default=None, type=click.Path(dir_okay=False), help="Write to file (default: stdout).")
@click.pass_context
def timeline_cmd(ctx: click.Context, out: str | None) -> None:
    """Build per-era timeline JSON via DuckDB."""
    from muninn.consumers.timeline.builder import build_timeline  # lazy

    payload = build_timeline(db_path=ctx.obj.get("db"), out_path=out)
    if out:
        click.echo(f"Timeline written to {out}.")
    else:
        click.echo(payload)


@cli.group("vault")
@click.pass_context
def vault_group(ctx: click.Context) -> None:
    """Compile/manage the Obsidian-compatible output vault."""


@vault_group.command("compile")
@click.option(
    "--vault-dir",
    envvar="MUNINN_VAULT_DIR",
    default=None,
    help="Override MUNINN_VAULT_DIR (output vault).",
)
@click.option(
    "--personal-dir",
    envvar="MUNINN_PERSONAL_VAULT_DIR",
    default=None,
    help="Override MUNINN_PERSONAL_VAULT_DIR (used by the never-same-vault guard).",
)
@click.pass_context
def vault_compile_cmd(
    ctx: click.Context, vault_dir: str | None, personal_dir: str | None
) -> None:
    """Compile the vault. Refuses to run if output and personal paths overlap."""
    from muninn.consumers.vault.compiler import (  # lazy
        VaultPathConflictError,
        compile_vault,
    )

    try:
        count = compile_vault(
            db_path=ctx.obj.get("db"),
            output_dir=vault_dir,
            personal_dir=personal_dir,
        )
    except VaultPathConflictError as exc:
        click.echo(f"Vault path conflict: {exc}", err=True)
        sys.exit(1)
    click.echo(f"Compiled {count} vault page(s).")


@cli.command("mcp")
@click.pass_context
def mcp_cmd(ctx: click.Context) -> None:
    """Run the FastMCP server on stdio."""
    from muninn.consumers.mcp.server import main as mcp_main  # lazy

    mcp_main()


if __name__ == "__main__":
    cli()
