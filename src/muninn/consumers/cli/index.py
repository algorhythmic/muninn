"""Explicit builds and activation; CLI import performs no model loading."""

import json

import click


@click.group("index")
def index_group():
    """Build and explicitly activate disposable Braid index generations."""


@index_group.command("sync")
@click.option("--binary", default="braid", show_default=True)
@click.option("--namespace", "namespaces", multiple=True,
              type=click.Choice(["bookmarks", "concepts", "infra", "people", "projects"]))
@click.pass_context
def sync(ctx, binary, namespaces):
    """Build a ready generation; activation is a separate explicit command."""
    from muninn.consumers.mcp.vault_fs import NAMESPACES
    from muninn.index.publisher import build_generation

    try:
        result = build_generation(ctx.obj.get("db"), binary=binary,
                                  namespaces=namespaces or NAMESPACES)
    except (OSError, RuntimeError, ValueError) as exc:
        raise click.ClickException("index build failed: " + type(exc).__name__) from exc
    click.echo(json.dumps({"generation_id": result.generation_id, "state": "ready",
                           "source_count": result.source_count, "chunk_count": result.chunk_count}))


@index_group.command("activate")
@click.argument("generation_id")
@click.pass_context
def activate(ctx, generation_id):
    """Switch active reference only after current-source checks succeed."""
    from muninn.index.publisher import activate_generation

    try:
        result = activate_generation(generation_id, ctx.obj.get("db"))
    except (OSError, RuntimeError, ValueError) as exc:
        raise click.ClickException("index activation refused: " + type(exc).__name__) from exc
    click.echo(json.dumps({"generation_id": result.generation_id, "state": "active"}))


@index_group.command("status")
@click.pass_context
def status(ctx):
    """Report captured epoch and active generation without loading a model."""
    from dataclasses import asdict
    from muninn.db import connect_readonly
    from muninn.index.registry import active_generation, ensure_corpus, source_epoch

    conn = connect_readonly(ctx.obj.get("db"))
    try:
        active = active_generation(conn)
        click.echo(json.dumps({"corpus_id": ensure_corpus(conn), "source_epoch": source_epoch(conn),
                               "active": asdict(active) if active else None}))
    finally:
        conn.close()
