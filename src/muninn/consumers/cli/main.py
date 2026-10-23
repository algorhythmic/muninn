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
    muninn synthesize era <era_label> [--emit-task DIR] [--model M] [--max-cost-usd 3.00]
    muninn synthesize deep-pass <id> | --pending [--emit-task DIR] [--model M] [--max-cost-usd X]
    muninn synthesize analyze "<prompt>" [--title T] [--bookmark-id N]... [--emit-task DIR] ...
    muninn synthesize ingest-result FILE [--retry-dir DIR]
    muninn triage [--json]
    muninn status
    muninn deep-pass <id>             # just sets the flag
    muninn export parquet --out p.parquet
    muninn export packets [--out DIR] [--item ID]... [--since D] [--disposition D]... [--dry-run]
    muninn export packets --withdraw PACKET_ID@REV --reason REASON
    muninn timeline [--out p.json]
    muninn vault compile
    muninn mcp [--transport stdio|http] [--host H] [--port P] [--allowed-host NAME]...
    muninn db migrate [--dry-run]
    muninn db status
    muninn db rebuild-fts
    muninn policy apply [--dry-run] [--policy-file P]
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
    Before syncing, domain policy is re-applied to every stored X row
    (as `muninn policy apply` does), not only to the rows the run sees.
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

    if report.policy is not None and report.policy.changed:
        click.echo(f"Domain policy re-applied to stored X rows: {report.policy.summary()}")
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
    (its `state` is checked against this login; a bare `code` value is
    accepted with a warning, unchecked). Needs MUNINN_X_CLIENT_ID and
    MUNINN_X_REDIRECT_URI (MUNINN_X_CLIENT_SECRET for a confidential app).
    """
    import secrets
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
        if state is None:
            raise click.ClickException(
                "The pasted redirect URL has no `state` parameter, so it cannot be checked "
                "against this login; paste the full redirect URL from the browser's address bar."
            )
        if not secrets.compare_digest(state, request.state):
            raise click.ClickException("State mismatch: the redirect is not from this login; try again.")
    else:
        code = pasted
        click.echo(
            "Warning: a bare code skips the state (CSRF) check; paste the full redirect URL "
            "to have it verified.",
            err=True,
        )
    if not code:
        raise click.ClickException("No authorization code found in the input.")
    try:
        tokens = exchange_code(oauth, code=code, code_verifier=request.pkce.verifier)
    except XError as exc:
        raise click.ClickException(f"Token exchange failed: {exc}") from exc
    JsonFileTokenStore(token_path).save(tokens)
    refresh = "with" if tokens.refresh_token else "without"
    click.echo(f"Saved X tokens ({refresh} refresh token) to {token_path} (mode 0600).")


@cli.group("policy")
@click.pass_context
def policy_group(ctx: click.Context) -> None:
    """Domain policy (domain_policy.yml) over the stored rows."""


@policy_group.command("apply")
@click.option("--dry-run", is_flag=True, help="Report what would change; write nothing.")
@click.option("--policy-file", type=click.Path(dir_okay=False), default=None,
              help="Policy file (default: the repo's domain_policy.yml).")
@click.pass_context
def policy_apply_cmd(ctx: click.Context, dry_run: bool, policy_file: str | None) -> None:
    """Re-apply domain policy to every stored row, with no API call or fetch.

    Recomputes content_visible from each row's stored url (browser and X
    rows), canonicalizes X post locators, titles hidden X posts with the
    handle only (hidden articles: none), and masks a hidden item's locator
    as [hidden link] in the current text and title of the visible X posts
    that link it (a new content revision; earlier revisions are kept).
    `muninn ingest x` runs the same pass over X rows before every sync.

    Un-hiding (removing a rule) makes rows visible again, but text already
    masked as [hidden link] is restored only by re-reading it from X:
    `muninn ingest x --full`.
    """
    from muninn.db import connect  # lazy
    from muninn.ingest.policy import apply_policy  # lazy
    from muninn.scrape.domain_policy import DomainPolicyError, load_domain_policy  # lazy

    try:
        policy = load_domain_policy(policy_file)
    except DomainPolicyError as exc:
        raise click.ClickException(str(exc)) from exc
    conn = connect(ctx.obj.get("db"))
    try:
        report = apply_policy(conn, policy, dry_run=dry_run)
    finally:
        conn.close()
    prefix = "Dry run (nothing written): would change" if dry_run else "Applied domain policy:"
    click.echo(f"{prefix} {report.summary()}")
    if report.newly_visible:
        click.echo("Rows made visible again keep any [hidden link] masks until "
                   "`muninn ingest x --full` re-reads them.")


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
    from muninn.intake_records import to_epoch  # lazy (stdlib-only)

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
@click.option("--prompt-version", default=None, help="Override the prompt version: a prompt file stem for the chosen --task, <task>_vN (per_bookmark_v2 for the default task, suitability_v2 with --task suitability); re-runs everything under the new key.")
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
    if prompt_version is not None:
        from muninn.enrich.haiku import check_prompt_version  # lazy

        try:
            check_prompt_version(task, prompt_version)
        except ValueError as exc:
            raise click.BadParameter(str(exc), param_hint="--prompt-version") from exc
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
    """Synthesis: era narratives, deep passes, ad-hoc analyses.

    By default each task runs with the budgeted direct API runner. With
    --emit-task DIR the task file is written for another harness and
    nothing runs; `synthesize ingest-result FILE` takes its result back.
    See docs/synthesis-contract.md.
    """


def _synthesis_options(f):
    """Options shared by the three task commands."""
    f = click.option(
        "--max-cost-usd", default=None,
        help="Per-run spend cap for the API runner in USD (default 3.00). The monthly cap "
             "is MUNINN_SYNTHESIS_MONTHLY_BUDGET_USD.",
    )(f)
    f = click.option(
        "--model", default=None,
        help="API runner model (default $MUNINN_SYNTHESIS_MODEL, else claude-opus-5-5).",
    )(f)
    f = click.option(
        "--emit-task", "emit_dir", default=None,
        type=click.Path(file_okay=False, writable=True),
        help="Write the task file to DIR for another harness; run nothing.",
    )(f)
    return f


def _synthesis_runner(emit_dir: str | None, model: str | None, max_cost_usd: str | None, conn):
    """(runner, budget) for a run, or (None, None) when emitting."""
    if emit_dir is not None:
        if model is not None or max_cost_usd is not None:
            raise click.UsageError("--model and --max-cost-usd apply to API runs, not --emit-task")
        return None, None
    from muninn.enrich.pricing import usd_to_micro  # lazy
    from muninn.synthesis.api_runner import ApiRunner  # lazy
    from muninn.synthesis.budget import DEFAULT_RUN_CAP_MICRO_USD, Budget, SynthesisConfigError  # lazy

    try:
        cap = usd_to_micro(max_cost_usd) if max_cost_usd is not None else DEFAULT_RUN_CAP_MICRO_USD
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint="--max-cost-usd") from exc
    try:
        runner = ApiRunner(model=model)
    except SynthesisConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    return runner, Budget(conn, run_cap_micro_usd=cap)


def _synthesis_errors():
    """Exceptions a synthesis command turns into a one-line error."""
    from muninn.synthesis.budget import SynthesisConfigError  # lazy
    from muninn.synthesis.contract import ContractError  # lazy

    # LookupError: an unknown or policy-hidden bookmark id.
    return (SynthesisConfigError, ContractError, LookupError)


def _echo_synthesis(label: str, result) -> None:
    task_id = getattr(result, "task_id", None)
    if result.status == "emitted":
        click.echo(f"{label}: task file {result.task_file} (task_id={task_id})")
        return
    click.echo(f"{label}: {result.status}" + (f" (task_id={task_id})" if task_id else ""))
    for error in getattr(result, "errors", ())[:10]:
        click.echo(f"  {error}")


@synthesize_group.command("era")
@click.argument("era_label")
@_synthesis_options
@click.pass_context
def synthesize_era_cmd(
    ctx: click.Context, era_label: str, emit_dir: str | None, model: str | None,
    max_cost_usd: str | None,
) -> None:
    """Generate the per-era narrative for `<era_label>`."""
    from muninn.db import connect  # lazy
    from muninn.synthesis import orchestrator  # lazy

    conn = connect(ctx.obj.get("db"))
    try:
        runner, budget = _synthesis_runner(emit_dir, model, max_cost_usd, conn)
        if emit_dir is not None:
            result = orchestrator.emit_era_narrative_task(era_label, emit_dir, conn=conn)
        else:
            result = orchestrator.run_era_narrative(era_label, conn=conn, runner=runner, budget=budget)
    except _synthesis_errors() as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        conn.close()
    _echo_synthesis(f"Synthesize era '{era_label}'", result)


@synthesize_group.command("deep-pass")
@click.argument("bookmark_id", required=False, type=int)
@click.option("--pending", is_flag=True, help="Process all rows with deep_pass_requested=1.")
@_synthesis_options
@click.pass_context
def synthesize_deep_pass_cmd(
    ctx: click.Context, bookmark_id: int | None, pending: bool, emit_dir: str | None,
    model: str | None, max_cost_usd: str | None,
) -> None:
    """Run the deep pass for one bookmark (or `--pending` for the queue)."""
    if not pending and bookmark_id is None:
        click.echo("Error: pass a BOOKMARK_ID or use --pending.", err=True)
        sys.exit(2)

    from muninn.db import connect  # lazy
    from muninn.synthesis import orchestrator  # lazy

    conn = connect(ctx.obj.get("db"))
    completed = 0
    try:
        runner, budget = _synthesis_runner(emit_dir, model, max_cost_usd, conn)

        def one(bid: int):
            if emit_dir is not None:
                return orchestrator.emit_deep_pass_task(bid, emit_dir, conn=conn)
            return orchestrator.run_deep_pass(bid, conn=conn, runner=runner, budget=budget)

        if pending:
            ids = [
                r[0]
                for r in conn.execute(
                    "SELECT e.bookmark_id FROM enriched e JOIN bookmarks b USING (bookmark_id) "
                    "WHERE e.deep_pass_requested = 1 AND b.content_visible = 1"
                ).fetchall()
            ]
            if not ids:
                click.echo("No pending deep-pass rows.")
                return
            for index, bid in enumerate(ids):
                result = one(bid)
                completed += result.status == "completed"
                shown = result.task_file if result.status == "emitted" else result.status
                click.echo(f"  bookmark_id={bid}: {shown}")
                if result.status == "cap_hit":
                    click.echo(f"Budget reached: {len(ids) - index - 1} pending row(s) not attempted.")
                    break
        else:
            result = one(bookmark_id)
            completed += result.status == "completed"
            _echo_synthesis(f"Synthesize deep-pass {bookmark_id}", result)
    except _synthesis_errors() as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        conn.close()
    if completed:
        _deep_pass_reconcile_hint(completed)


def _deep_pass_reconcile_hint(completed: int) -> None:
    # The deep pass refreshed FTS in its own transaction; the Qdrant
    # points are now stale until the (network) reconcile re-embeds them.
    click.echo(
        f"Deep pass rewrote {completed} summary(ies): run "
        "`python scripts/reconcile-vector-index.py` to refresh the vector index."
    )


@synthesize_group.command("analyze")
@click.argument("prompt")
@click.option("--title", default=None, help="Title for the analyses row (default: from the output or prompt).")
@click.option("--bookmark-id", "bookmark_ids", type=int, multiple=True,
              help="Analyze exactly these bookmarks (repeatable). Default: the 500 most recent visible.")
@_synthesis_options
@click.pass_context
def synthesize_analyze_cmd(
    ctx: click.Context, prompt: str, title: str | None, bookmark_ids: tuple[int, ...],
    emit_dir: str | None, model: str | None, max_cost_usd: str | None,
) -> None:
    """Run an ad-hoc cross-corpus analysis with `prompt`."""
    from muninn.db import connect  # lazy
    from muninn.synthesis import orchestrator  # lazy

    conn = connect(ctx.obj.get("db"))
    kwargs = {"title": title, "bookmark_ids": list(bookmark_ids) or None}
    try:
        runner, budget = _synthesis_runner(emit_dir, model, max_cost_usd, conn)
        if emit_dir is not None:
            result = orchestrator.emit_ad_hoc_analysis_task(prompt, emit_dir, conn=conn, **kwargs)
        else:
            result = orchestrator.run_ad_hoc_analysis(prompt, conn=conn, runner=runner, budget=budget, **kwargs)
    except _synthesis_errors() as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        conn.close()
    _echo_synthesis("Synthesize analyze", result)


@synthesize_group.command("ingest-result")
@click.argument("path", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--retry-dir", default=None, type=click.Path(file_okay=False, writable=True),
    help="If an attempt-1 result fails validation, write the attempt-2 task file "
         "(with the correction instructions) to DIR.",
)
@click.pass_context
def synthesize_ingest_result_cmd(ctx: click.Context, path: str, retry_dir: str | None) -> None:
    """Validate a result file against its emitted task and write it."""
    from muninn.db import connect  # lazy
    from muninn.synthesis import orchestrator  # lazy

    conn = connect(ctx.obj.get("db"))
    try:
        result = orchestrator.ingest_result(path, conn=conn, retry_dir=retry_dir)
    except _synthesis_errors() as exc:
        raise click.ClickException(f"Result rejected, nothing written: {exc}") from exc
    finally:
        conn.close()
    click.echo(f"Ingest {path}: {result.status} (task_id={result.task_id})")
    if result.status == "completed":
        if result.task_type == "deep_pass":
            _deep_pass_reconcile_hint(1)
        return
    for error in result.errors[:10]:
        click.echo(f"  {error}")
    if result.task_file is not None:
        click.echo(f"Retry task file: {result.task_file}")
    sys.exit(1)


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
    """Export Research Intake evidence packets (contract muninn-research-intake).

    Every run first withdraws active packets whose subject or linked items
    were deleted (source_deleted) or hidden by domain policy (policy_hidden),
    then exports. --dry-run reports both without writing."""
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
            cleared = f"; {result.revisions_withdrawn} content revision(s) withdrawn" if result.revisions_withdrawn else ""
            click.echo(f"Withdrawn {result.ref} ({reason}); tombstone: {result.path}{cleared}")
            return
        run = export_packets(
            conn, out, item_ids=item_ids or None, since=since,
            dispositions=dispositions or ("candidate",), limit=limit, dry_run=dry_run,
        )
    finally:
        conn.close()
    for tomb, why in run.withdrawn:
        click.echo(f"  withdrew {tomb.ref} ({why}) {tomb.path}")
    for due in run.would_withdraw:
        click.echo(f"  would withdraw {due.ref} ({due.reason})")
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


@cli.command("lint")
@click.option(
    "--vault", "vault_dir", default=None, type=click.Path(file_okay=False),
    help="Compiled vault root to lint (default: $MUNINN_VAULT_DIR).",
)
@click.option("--json", "as_json", is_flag=True, help="Print the report as JSON.")
@click.option(
    "--report", "report_path", default=None, type=click.Path(dir_okay=False),
    help="Write the report to FILE (outside the vault) and print a one-line summary.",
)
@click.option("--strict", is_flag=True, help="Exit 1 when there is any finding.")
def lint_cmd(vault_dir: str | None, as_json: bool, report_path: str | None, strict: bool) -> None:
    """Lint the compiled vault: orphans, missing links, broken links.

    Model-free and read-only: it never edits a vault file, authored or
    compiled. Refuses a vault that overlaps MUNINN_PERSONAL_VAULT_DIR.
    See docs/vault-lint.md. (Contradictions and superseded claims are the
    model tier: `muninn synthesize lint --tag TAG`.)
    """
    from pathlib import Path

    from muninn.config import load_paths  # lazy
    from muninn.consumers.mcp.vault_fs import VaultAccessError  # lazy
    from muninn.consumers.vault.lint import lint_vault, render_json, render_text  # lazy

    try:
        report = lint_vault(vault_dir)
    except VaultAccessError as exc:
        raise click.ClickException(str(exc)) from exc
    output = render_json(report) if as_json else render_text(report)
    if report_path is None:
        click.echo(output, nl=False)
    else:
        target = Path(report_path).expanduser().resolve()
        personal = load_paths().personal_vault_dir
        for root, what in ((Path(report.vault), "the vault"), (personal, "the personal vault")):
            if root is not None and target.is_relative_to(Path(root).resolve()):
                raise click.ClickException(
                    f"--report {target} is inside {what}; lint never writes a vault file"
                )
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(output, encoding="utf-8")
        tmp.replace(target)
        click.echo(
            f"Vault lint: {len(report.orphans)} orphan(s), {len(report.suggestions)} suggested "
            f"link(s), {len(report.unresolved)} unresolved link(s); report: {report_path}"
        )
    if strict and report.finding_count:
        sys.exit(1)


@cli.command("mcp")
@click.option(
    "--transport",
    type=click.Choice(["stdio", "http"]),
    default="stdio",
    show_default=True,
    help="stdio for a local client; http for FastMCP's streamable HTTP at /mcp.",
)
@click.option(
    "--host",
    default=None,
    help="HTTP bind address [default: 127.0.0.1]. Anything else must be a "
    "tailnet-only address: the endpoint has no authentication.",
)
@click.option(
    "--port",
    type=click.IntRange(0, 65535),
    default=None,
    help="HTTP port [default: 8000].",
)
@click.option(
    "--allowed-host",
    "allowed_hosts",
    multiple=True,
    help="Extra Host header name to accept over HTTP, e.g. the MagicDNS name "
    "`tailscale serve` forwards. Repeatable.",
)
@click.pass_context
def mcp_cmd(
    ctx: click.Context,
    transport: str,
    host: str | None,
    port: int | None,
    allowed_hosts: tuple[str, ...],
) -> None:
    """Run the FastMCP server (stdio by default; see docs/mcp-api.md)."""
    from muninn.consumers.mcp.server import TransportConfigError  # lazy
    from muninn.consumers.mcp.server import main as mcp_main

    try:
        mcp_main(transport, host=host, port=port, allowed_hosts=allowed_hosts)
    except TransportConfigError as exc:
        raise click.UsageError(str(exc)) from None


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


@db_group.command("rebuild-fts")
@click.pass_context
def db_rebuild_fts_cmd(ctx: click.Context) -> None:
    """Recreate the fts_bookmarks keyword index as migration 001 defines it.

    Backs the DB up, drops the index, recreates it and refills it from the
    enriched rows (visible bookmarks only). The fix for an early database
    whose contentless index `db migrate` refuses to stamp; safe on any DB,
    since the index is derived data.
    """
    from muninn.db import MigrationError, rebuild_fts  # lazy

    try:
        result = rebuild_fts(ctx.obj.get("db"))
    except MigrationError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Database: {result.db_path}")
    if result.backup_path:
        click.echo(f"Backup: {result.backup_path}")
    click.echo(f"Rebuilt fts_bookmarks: {result.rows} row(s) indexed.")


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
