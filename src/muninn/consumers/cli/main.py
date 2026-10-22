"""Muninn CLI dispatcher.

Registered in `pyproject.toml` as `muninn = "muninn.consumers.cli.main:cli"`.

All cross-package callouts (ingest, scrape, enrich, synthesis, vault,
parquet, timeline, mcp) are LAZY — imported inside the command body so
`muninn --help` always works even if a sibling stream is mid-rewrite or has
import-time side effects.

Subcommand layout matches the SPEC's pipeline order:

    muninn ingest <path>              # same as `muninn ingest html <path>`
    muninn ingest x [--max-pages N | --all] [--full] [--resume] [--dry-run]
    muninn x auth                     # OAuth 2.0 PKCE login for `ingest x`
    muninn scrape [--concurrency N] [--limit N] [--source S]... [--refresh]
    muninn enrich [--dry-run] [--force] [--prompt-version per_bookmark_vN]
    muninn enrich --task suitability [--source x] [--since D] [--until D]
                  [--limit N] [--max-cost-usd 1.00] [--model M] [--profile P] [--dry-run]
    muninn synthesize era <era_label>
    muninn synthesize deep-pass <id> | --pending
    muninn synthesize analyze "<prompt>"
    muninn triage [--json]
    muninn status
    muninn deep-pass <id>             # just sets the flag
    muninn export parquet --out p.parquet
    muninn export packets [--out DIR] [--item ID]... [--since D] [--disposition D]... [--dry-run]
    muninn export packets --withdraw PACKET_ID@REV --reason REASON
    muninn timeline [--out p.json]
    muninn vault compile
    muninn mcp
    muninn db migrate [--dry-run]
    muninn db status
"""

from __future__ import annotations

import sys

import click

from .deep_pass import deep_pass_cmd
from .status import status_cmd
from .triage import triage_cmd


class MuninnGroup(click.Group):
    """Root group: turns a schema-version mismatch from any command into a
    one-line error instead of a traceback."""

    def invoke(self, ctx: click.Context):
        try:
            return super().invoke(ctx)
        except click.ClickException:
            raise
        except Exception as exc:
            from muninn.db import SchemaVersionError  # lazy

            if isinstance(exc, SchemaVersionError):
                raise click.ClickException(str(exc)) from exc
            raise


@click.group(cls=MuninnGroup)
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


class _IngestGroup(click.Group):
    """`muninn ingest <path>` keeps working: an argument that is not a
    subcommand name is routed to `ingest html` (a file literally named `x`
    needs `muninn ingest html x` or `./x`)."""

    def resolve_command(self, ctx: click.Context, args: list[str]):
        if args and args[0] not in self.commands and not args[0].startswith("-"):
            return "html", self.commands["html"], args
        return super().resolve_command(ctx, args)

    def format_usage(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        formatter.write_usage(ctx.command_path, "[PATH | html PATH | x [OPTIONS]]")


@cli.group("ingest", cls=_IngestGroup)
@click.pass_context
def ingest_group(ctx: click.Context) -> None:
    """Ingest saved material: a bookmarks.html file (`ingest PATH`) or X bookmarks (`ingest x`)."""


@ingest_group.command("html")
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


def _x_oauth(settings, *, require_redirect: bool):
    from muninn.ingest.x import OAuthClientConfig  # lazy (httpx)

    if not settings.client_id:
        raise click.ClickException(
            "MUNINN_X_CLIENT_ID is not set (the X developer app's OAuth 2.0 client ID)."
        )
    if require_redirect and not settings.redirect_uri:
        raise click.ClickException(
            "MUNINN_X_REDIRECT_URI is not set (the callback URL registered on the X app)."
        )
    return OAuthClientConfig(
        client_id=settings.client_id,
        client_secret=settings.client_secret,
        redirect_uri=settings.redirect_uri or "",
    )


@ingest_group.command("x")
@click.option("--max-pages", type=click.IntRange(min=1), default=None,
              help="Stop after N pages (default 10, about 1000 posts).")
@click.option("--all", "all_pages", is_flag=True, help="No page bound: walk until done.")
@click.option("--full", is_flag=True,
              help="Walk the whole list instead of stopping at the first page of known bookmarks.")
@click.option("--resume", is_flag=True,
              help="Continue the newest unfinished run (paused, exhausted, failed or interrupted).")
@click.option("--dry-run", is_flag=True, help="Print the plan and cost bound; no API call, no writes.")
@click.option("--token-file", type=click.Path(dir_okay=False), default=None,
              help="X token file (default: MUNINN_X_TOKEN_FILE or $MUNINN_DATA_DIR/x-tokens.json).")
@click.pass_context
def ingest_x_cmd(
    ctx: click.Context,
    max_pages: int | None,
    all_pages: bool,
    full: bool,
    resume: bool,
    dry_run: bool,
    token_file: str | None,
) -> None:
    """Sync X bookmarks (posts, linked pages and repos) via the X API.

    Incremental by default: starts at the newest bookmark and stops after
    the first page whose posts are all known. Each page costs money
    (pay-per-use), so runs are bounded by --max-pages unless --all.
    """
    from pathlib import Path

    from muninn.config import load_x_settings  # lazy
    from muninn.ingest.x_store import (  # lazy (httpx)
        DEFAULT_MAX_PAGES,
        NothingToResume,
        SyncOptions,
        XPricing,
        format_usd,
    )

    if all_pages and max_pages is not None:
        raise click.UsageError("--max-pages and --all are mutually exclusive.")
    pages = None if all_pages else (max_pages or DEFAULT_MAX_PAGES)
    settings = load_x_settings()
    token_path = Path(token_file).expanduser() if token_file else settings.token_path
    options = SyncOptions(max_pages=pages, mode="full" if full else "incremental", resume=resume)
    pricing = XPricing()
    per_page = 100
    if pages is None:
        bound = "no page bound (--all)"
    else:
        bound = (f"up to {pages} page(s) x {per_page} posts: about "
                 f"{format_usd(pricing.estimate_posts(pages, per_page))} for posts")
    click.echo(
        f"X bookmark sync ({options.mode}{', resume' if resume else ''}): {bound}, "
        f"estimated at {format_usd(pricing.micro_usd_per_resource)} per resource "
        "(owned reads; expansions may add)."
    )
    if dry_run:
        from muninn.db import connect  # lazy
        from muninn.ingest.x_store import find_resumable

        click.echo(f"Token file: {token_path} ({'present' if token_path.is_file() else 'missing'})")
        if resume:
            conn = connect(ctx.obj.get("db"))
            try:
                row = find_resumable(conn, account_ref=None)
            finally:
                conn.close()
            if row is None:
                click.echo("Nothing to resume.")
            else:
                click.echo(
                    f"Would resume run {row['run_ulid']} ({row['status']}, "
                    f"{row['pages_fetched']} page(s) so far)."
                )
        click.echo("Dry run: no API call, nothing written.")
        return

    if not token_path.is_file():
        raise click.ClickException(f"No X tokens at {token_path}: run `muninn x auth` first.")
    oauth = _x_oauth(settings, require_redirect=False)

    from muninn.db import connect  # lazy
    from muninn.ingest.x import JsonFileTokenStore, XClient, XError
    from muninn.ingest.x_store import sync_bookmarks

    conn = connect(ctx.obj.get("db"))
    try:
        with XClient(oauth, JsonFileTokenStore(token_path)) as client:
            report = sync_bookmarks(conn, client, options=options, pricing=pricing)
    except NothingToResume as exc:
        raise click.ClickException(str(exc)) from exc
    except XError as exc:
        raise click.ClickException(f"X API: {exc}") from exc
    finally:
        conn.close()

    click.echo(
        f"Run {report.run_ulid}: {report.status} ({report.stop_reason}); "
        f"pages={report.pages} posts={report.posts_seen} new={report.posts_new} "
        f"problems={report.problems} new_revisions={report.revisions_new}; "
        f"estimated cost {format_usd(report.cost_micro_usd)} ({report.resources} resources)."
    )
    if report.cap_reached:
        click.echo("Note: reached the configured bookmark cap; older bookmarks may be invisible to the API.")
    if report.status == "paused":
        when = f" after epoch {report.resume_after}" if report.resume_after else ""
        click.echo(f"Paused ({report.stop_reason}); continue{when} with `muninn ingest x --resume`.")
    elif report.status == "exhausted":
        click.echo("X usage cap reached; after a top-up or the next cycle, run `muninn ingest x --resume`.")
    elif report.status == "failed":
        raise click.ClickException(
            f"X sync failed ({report.error_code}): {report.error_detail}. "
            "Fix the cause, then `muninn ingest x --resume`."
        )


@cli.group("x")
@click.pass_context
def x_group(ctx: click.Context) -> None:
    """X account access for `muninn ingest x`."""


@x_group.command("auth")
@click.option("--token-file", type=click.Path(dir_okay=False), default=None,
              help="Where to save tokens (default: MUNINN_X_TOKEN_FILE or $MUNINN_DATA_DIR/x-tokens.json).")
def x_auth_cmd(token_file: str | None) -> None:
    """Log in to X (OAuth 2.0 PKCE) and save tokens (mode 0600).

    Open the printed URL, approve access, then paste the full redirect URL
    (or just its `code` value). Needs MUNINN_X_CLIENT_ID and
    MUNINN_X_REDIRECT_URI (MUNINN_X_CLIENT_SECRET for a confidential app).
    """
    from pathlib import Path
    from urllib.parse import parse_qs, urlsplit

    from muninn.config import load_x_settings  # lazy
    from muninn.ingest.x import JsonFileTokenStore, XError, exchange_code, start_authorization

    settings = load_x_settings()
    token_path = Path(token_file).expanduser() if token_file else settings.token_path
    oauth = _x_oauth(settings, require_redirect=True)
    request = start_authorization(oauth)
    click.echo("Open this URL in a browser and approve access:\n")
    click.echo(request.url)
    click.echo("")
    # Hidden: the redirect carries the authorization code, a short-lived credential.
    pasted = click.prompt(
        "Paste the redirect URL (or the code; input is hidden)", prompt_suffix=": ", hide_input=True
    ).strip()
    if "?" in pasted or "://" in pasted:
        query = parse_qs(urlsplit(pasted).query)
        if "error" in query:
            raise click.ClickException(f"Authorization was not granted: {query['error'][0]}")
        code = (query.get("code") or [""])[0]
        state = (query.get("state") or [None])[0]
        if state is not None and state != request.state:
            raise click.ClickException("State mismatch: the redirect is not from this login; try again.")
    else:
        code = pasted
    if not code:
        raise click.ClickException("No authorization code found in the input.")
    try:
        tokens = exchange_code(oauth, code=code, code_verifier=request.pkce.verifier)
    except XError as exc:
        raise click.ClickException(f"Token exchange failed: {exc}") from exc
    JsonFileTokenStore(token_path).save(tokens)
    refresh = "with" if tokens.refresh_token else "without"
    click.echo(f"Saved X tokens ({refresh} refresh token) to {token_path} (mode 0600).")


@cli.command("scrape")
@click.option(
    "--concurrency",
    type=int,
    default=4,
    help="Max concurrent bookmarks scraped (each runs dual-pass serially).",
)
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Scrape only the N most recently captured bookmarks (newest first).",
)
@click.option(
    "--source",
    "sources",
    multiple=True,
    help="Only rows from this bookmarks.source (repeatable), e.g. x_link. X posts are never scraped.",
)
@click.option(
    "--refresh",
    is_flag=True,
    help="Bypass the live HTTP cache so changed pages are re-captured (archive snapshots stay cached).",
)
@click.pass_context
def scrape_cmd(
    ctx: click.Context, concurrency: int, limit: int | None, sources: tuple[str, ...], refresh: bool
) -> None:
    """Run dual-pass scrape (live + at_capture + recent_archive fallback) for visible bookmarks."""
    from muninn.scrape.pipeline import run_scrape  # lazy

    result = run_scrape(
        db_path=ctx.obj.get("db"),
        concurrency=concurrency,
        limit=limit,
        refresh=refresh,
        sources=sources or None,
    )
    click.echo(f"Scrape complete: {result}")


def _parse_when(ctx: click.Context, param: click.Parameter, value: str | None) -> int | None:
    """Click callback: epoch seconds from ``YYYY-MM-DD``, ISO-8601 or epoch digits (UTC)."""
    if value is None:
        return None
    from muninn.consumers.packets.records import to_epoch  # lazy (stdlib-only)

    epoch = int(value) if value.strip().isdigit() else to_epoch(value)
    if epoch is None:
        raise click.BadParameter("expected YYYY-MM-DD, an ISO-8601 time or epoch seconds")
    return epoch


@cli.command("enrich")
@click.option(
    "--task",
    type=click.Choice(["per_bookmark", "suitability"]),
    default="per_bookmark",
    show_default=True,
    help="per_bookmark: summary/tags enrichment. suitability: Research Intake assessment against the interest profile.",
)
@click.option("--dry-run", is_flag=True, help="Plan calls without invoking Anthropic API or Qdrant (suitability: plan + cost upper bound, no writes).")
@click.option("--force", is_flag=True, help="Re-enrich even when the idempotency triple matches (burns tokens). per_bookmark only.")
@click.option("--prompt-version", default=None, help="Override the prompt version: a prompt file stem such as per_bookmark_v2 or suitability_v2 (re-runs everything under the new key).")
@click.option("--source", "sources", multiple=True, help="[suitability] bookmarks.source to select (repeatable; default: x).")
@click.option("--since", callback=_parse_when, default=None, help="[suitability] Bookmark observation time >= this (YYYY-MM-DD, ISO-8601 or epoch; UTC).")
@click.option("--until", callback=_parse_when, default=None, help="[suitability] Bookmark observation time < this.")
@click.option("--limit", type=click.IntRange(min=1), default=None, help="[suitability] Max model calls this run (default 20).")
@click.option("--max-cost-usd", default=None, help="[suitability] Per-run spend cap in USD, from measured usage (default 1.00).")
@click.option("--model", default=None, help="[suitability] Model id (default: $MUNINN_SUITABILITY_MODEL, else the per-bookmark model).")
@click.option("--profile", "profile_path", default=None, type=click.Path(dir_okay=False), help="[suitability] Interest profile YAML (default: $MUNINN_INTEREST_PROFILE).")
@click.pass_context
def enrich_cmd(
    ctx: click.Context,
    task: str,
    dry_run: bool,
    force: bool,
    prompt_version: str | None,
    sources: tuple[str, ...],
    since: int | None,
    until: int | None,
    limit: int | None,
    max_cost_usd: str | None,
    model: str | None,
    profile_path: str | None,
) -> None:
    """Per-bookmark Haiku enrichment with idempotency by content_hash, or
    (--task suitability) bounded, budgeted suitability assessment."""
    if task == "suitability":
        if force:
            raise click.UsageError("--force is not available for --task suitability: assessments are append-only")
        _enrich_suitability(
            ctx, dry_run=dry_run, prompt_version=prompt_version, sources=sources, since=since,
            until=until, limit=limit, max_cost_usd=max_cost_usd, model=model, profile_path=profile_path,
        )
        return
    suitability_only = {
        "--source": bool(sources), "--since": since is not None, "--until": until is not None,
        "--limit": limit is not None, "--max-cost-usd": max_cost_usd is not None,
        "--model": model is not None, "--profile": profile_path is not None,
    }
    given = [name for name, used in suitability_only.items() if used]
    if given:
        raise click.UsageError(f"{', '.join(given)} only apply to --task suitability")

    from muninn.db import connect  # lazy
    from muninn.enrich.pipeline import enrich_all  # lazy

    conn = connect(ctx.obj.get("db"))
    try:
        stats = enrich_all(conn, dry_run=dry_run, force=force, prompt_version=prompt_version)
    except FileNotFoundError as exc:
        raise click.ClickException(f"Unknown prompt version {prompt_version!r}: {exc}") from exc
    finally:
        conn.close()
    click.echo(f"Enrich complete: {stats}")


def _enrich_suitability(
    ctx: click.Context,
    *,
    dry_run: bool,
    prompt_version: str | None,
    sources: tuple[str, ...],
    since: int | None,
    until: int | None,
    limit: int | None,
    max_cost_usd: str | None,
    model: str | None,
    profile_path: str | None,
) -> None:
    import json

    from muninn.config import SUITABILITY_PROMPT_VERSION  # lazy
    from muninn.db import connect  # lazy
    from muninn.enrich.interest_profile import InterestProfileError, load_interest_profile  # lazy
    from muninn.enrich.pricing import usd_to_micro  # lazy
    from muninn.enrich.suitability import (  # lazy
        DEFAULT_LIMIT,
        DEFAULT_MAX_COST_MICRO_USD,
        DEFAULT_SOURCES,
        SuitabilityError,
        assess_suitability,
    )

    try:
        budget = usd_to_micro(max_cost_usd) if max_cost_usd is not None else DEFAULT_MAX_COST_MICRO_USD
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint="--max-cost-usd") from exc
    try:
        profile = load_interest_profile(profile_path)
    except InterestProfileError as exc:
        raise click.ClickException(str(exc)) from exc

    conn = connect(ctx.obj.get("db"))
    try:
        run = assess_suitability(
            conn,
            profile,
            sources=sources or DEFAULT_SOURCES,
            since=since,
            until=until,
            limit=limit or DEFAULT_LIMIT,
            max_cost_micro_usd=budget,
            model=model,
            prompt_version=prompt_version or SUITABILITY_PROMPT_VERSION,
            dry_run=dry_run,
        )
    except SuitabilityError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        conn.close()
    for outcome in run.outcomes:
        extra = f" {outcome.disposition}" if outcome.disposition else ""
        detail = f" ({outcome.detail})" if outcome.detail else ""
        click.echo(f"  {outcome.status:<20} {outcome.item_id}{extra}{detail}")
    click.echo("Suitability " + ("plan: " if dry_run else "complete: ") + json.dumps(run.summary()))


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


@export_group.command("packets")
@click.option("--out", "out_dir", default=None, type=click.Path(file_okay=False), help="Output directory (default: $MUNINN_PACKETS_DIR, else <data dir>/packets).")
@click.option("--item", "item_ids", multiple=True, help="Only this subject item_id (repeatable), e.g. x:post:123.")
@click.option("--since", callback=_parse_when, default=None, help="Only subjects whose primary assessment is at or after this time.")
@click.option("--disposition", "dispositions", multiple=True, type=click.Choice(["candidate", "reference_only", "needs_evidence", "deferred", "rejected"]), help="Primary-assessment dispositions to export (repeatable; default: candidate).")
@click.option("--limit", type=click.IntRange(min=1), default=None, help="Max subjects considered.")
@click.option("--dry-run", is_flag=True, help="Build and validate packets; write nothing.")
@click.option("--withdraw", "withdraw_ref", default=None, metavar="PACKET_ID@REVISION", help="Withdraw one exported packet revision (replaced by its tombstone).")
@click.option("--reason", type=click.Choice(["retention", "operator_request", "source_deleted", "policy_hidden"]), default=None, help="Withdrawal reason (required with --withdraw).")
@click.pass_context
def export_packets_cmd(
    ctx: click.Context,
    out_dir: str | None,
    item_ids: tuple[str, ...],
    since: int | None,
    dispositions: tuple[str, ...],
    limit: int | None,
    dry_run: bool,
    withdraw_ref: str | None,
    reason: str | None,
) -> None:
    """Export Research Intake evidence packets (contract muninn-research-intake)."""
    import json
    import os

    from muninn.config import load_paths  # lazy
    from muninn.consumers.packets.export import export_packets, withdraw_exported_packet  # lazy
    from muninn.db import connect  # lazy

    out = out_dir or os.environ.get("MUNINN_PACKETS_DIR") or str(load_paths().data_dir / "packets")
    if withdraw_ref is not None:
        if reason is None:
            raise click.UsageError("--withdraw needs --reason")
        if item_ids or since is not None or dispositions or limit is not None or dry_run:
            raise click.UsageError("--withdraw takes only --reason and --out")
    elif reason is not None:
        raise click.UsageError("--reason is only used with --withdraw")

    conn = connect(ctx.obj.get("db"))
    try:
        if withdraw_ref is not None:
            try:
                result = withdraw_exported_packet(conn, out, withdraw_ref, reason=reason)
            except (LookupError, ValueError) as exc:
                raise click.ClickException(str(exc)) from exc
            click.echo(f"Withdrawn {result.ref} ({reason}); tombstone: {result.path}")
            return
        run = export_packets(
            conn, out, item_ids=item_ids or None, since=since,
            dispositions=dispositions or ("candidate",), limit=limit, dry_run=dry_run,
        )
    finally:
        conn.close()
    for exported in run.written:
        click.echo(f"  wrote {exported.ref} {exported.digest} {exported.path}")
    for subject, errors in run.invalid:
        click.echo(f"  refused {subject}: {'; '.join(errors[:3])}", err=True)
    click.echo(("Packet plan: " if dry_run else "Packets exported: ") + json.dumps(run.summary()))
    if run.invalid:
        sys.exit(1)


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
@click.option(
    "--no-prune",
    is_flag=True,
    help="Keep compiled pages this run did not write (default: remove them).",
)
@click.pass_context
def vault_compile_cmd(
    ctx: click.Context, vault_dir: str | None, personal_dir: str | None, no_prune: bool
) -> None:
    """Compile the vault. Refuses to run if output and personal paths overlap.

    Compiled pages in wiki/bookmarks/ that this run did not write (their row
    is now hidden or gone, or their slug changed) are removed; files without
    the compiler's frontmatter marker are never touched.
    """
    from muninn.consumers.vault.compiler import (  # lazy
        VaultPathConflictError,
        compile_vault_report,
    )

    try:
        report = compile_vault_report(
            db_path=ctx.obj.get("db"),
            output_dir=vault_dir,
            personal_dir=personal_dir,
            prune=not no_prune,
        )
    except VaultPathConflictError as exc:
        click.echo(f"Vault path conflict: {exc}", err=True)
        sys.exit(1)
    for name in report.removed:
        click.echo(f"  removed stale page wiki/bookmarks/{name}")
    click.echo(f"Compiled {report.written} vault page(s); removed {len(report.removed)} stale page(s).")


@cli.command("mcp")
@click.pass_context
def mcp_cmd(ctx: click.Context) -> None:
    """Run the FastMCP server on stdio."""
    from muninn.consumers.mcp.server import main as mcp_main  # lazy

    mcp_main()


# ── db (schema migrations; explicit, never implicit) ───────────────


@cli.group("db")
@click.pass_context
def db_group(ctx: click.Context) -> None:
    """Schema version and migrations for the SQLite store."""


def _echo_plan(plan) -> None:
    unstamped = " (unstamped; migrate records it)" if plan.unstamped else ""
    click.echo(f"Database: {plan.db_path}")
    click.echo(f"Schema version: v{plan.current}{unstamped}, head: v{plan.head}")
    if plan.current > plan.head:
        click.echo("Database is ahead of this muninn; nothing to apply.")
    for path in plan.pending:
        click.echo(f"  pending: {path.name}")


@db_group.command("migrate")
@click.option("--dry-run", is_flag=True, help="Print the plan (current version, head, pending files) and change nothing.")
@click.pass_context
def db_migrate_cmd(ctx: click.Context, dry_run: bool) -> None:
    """Back up the DB, then apply pending migrations up to head."""
    from muninn.db import MigrationError, migrate  # lazy

    try:
        plan = migrate(ctx.obj.get("db"), dry_run=dry_run)
    except MigrationError as exc:
        raise click.ClickException(str(exc)) from exc
    _echo_plan(plan)
    if dry_run:
        click.echo("Dry run: nothing changed.")
    elif plan.pending or plan.unstamped:
        if plan.backup_path:
            click.echo(f"Backup: {plan.backup_path}")
        click.echo(f"Migrated to v{plan.head}.")
    else:
        click.echo("Nothing to migrate.")


@db_group.command("status")
@click.pass_context
def db_status_cmd(ctx: click.Context) -> None:
    """Show the schema version, head and pending migrations (read-only)."""
    from muninn.db import MigrationError, migrate  # lazy

    try:
        plan = migrate(ctx.obj.get("db"), dry_run=True)
    except MigrationError as exc:
        raise click.ClickException(str(exc)) from exc
    _echo_plan(plan)


if __name__ == "__main__":
    cli()
