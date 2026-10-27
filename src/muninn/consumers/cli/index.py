"""Explicit builds and activation; CLI import performs no model loading."""

import json

import click


@click.group("index")
def index_group():
    """Build and explicitly activate disposable Braid index generations."""


@index_group.command("sync")
@click.option("--backend", type=click.Choice(["braid"]), default="braid", show_default=True)
@click.option("--binary", type=click.Path(path_type=str))
@click.option("--namespace", "namespaces", multiple=True,
              type=click.Choice(["bookmarks", "concepts", "infra", "people", "projects"]))
@click.pass_context
def sync(ctx, backend, binary, namespaces):
    """Build a ready generation; activation is a separate explicit command."""
    from muninn.consumers.mcp.vault_fs import NAMESPACES
    from muninn.index.publisher import build_generation
    from muninn.config import load_braid_settings

    try:
        binary = binary or load_braid_settings().binary
        if binary is None:
            raise ValueError("absolute Braid binary required")
        from pathlib import Path
        if not Path(binary).is_absolute():
            raise ValueError("absolute Braid binary required")
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
    from muninn.index.status import index_status
    click.echo(json.dumps(index_status(ctx.obj.get("db"))))


@index_group.command("rollback")
@click.argument("generation_id")
@click.pass_context
def rollback(ctx, generation_id):
    """Explicitly select the retained previous generation after source checks."""
    from muninn.index.publisher import rollback_generation
    try:
        result = rollback_generation(generation_id, ctx.obj.get("db"))
    except (OSError, RuntimeError, ValueError) as exc:
        raise click.ClickException("index rollback refused: " + type(exc).__name__) from exc
    click.echo(json.dumps({"generation_id": result.generation_id, "state": "active"}))


@index_group.command("worker")
@click.option("--once", is_flag=True, help="Observe changes, process at most one build, and exit.")
@click.option("--poll-seconds", default=5.0, type=click.FloatRange(min=0.1))
@click.pass_context
def worker(ctx, once, poll_seconds):
    """Optional foreground publisher; supervision is the operator's responsibility.

    Never activates a generation. No daemon or service is installed.
    """
    from dataclasses import asdict
    from muninn.config import load_braid_settings
    from muninn.index.worker import run_once, supervise
    try:
        binary = load_braid_settings().binary
        if binary is None:
            raise ValueError("absolute Braid binary required")
        if once:
            click.echo(json.dumps(asdict(run_once(ctx.obj.get("db"), binary=binary))))
        else:
            supervise(ctx.obj.get("db"), poll_seconds=poll_seconds, binary=binary)
    except (OSError, RuntimeError, ValueError) as exc:
        raise click.ClickException("index worker failed: " + type(exc).__name__) from exc
