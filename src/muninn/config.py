"""Environment-driven config: paths, model IDs, prompt versions.

A `.env` file at the repo root (or at $MUNINN_ENV_FILE) is loaded on import;
real environment variables always win over file values.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: Path | None = None) -> None:
    """Minimal KEY=VALUE loader — no dependency, no interpolation.

    Lines starting with '#' and lines without '=' are ignored. Values may be
    single- or double-quoted. Existing environment variables are never
    overwritten.
    """
    p = path or Path(__file__).resolve().parents[2] / ".env"
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] in "'\"" and value.endswith(value[0]):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


_env_file = os.environ.get("MUNINN_ENV_FILE")
_load_dotenv(Path(_env_file) if _env_file else None)


def _env_path(var: str, default: Path) -> Path:
    raw = os.environ.get(var)
    return Path(raw).expanduser().resolve() if raw else default


@dataclass(frozen=True)
class Paths:
    raw_dir: Path
    data_dir: Path
    db_path: Path
    scrape_cache_dir: Path
    http_cache_dir: Path
    vault_dir: Path | None
    personal_vault_dir: Path | None


def load_paths() -> Paths:
    repo_root = Path(__file__).resolve().parents[2]
    raw_dir = _env_path("MUNINN_RAW_DIR", repo_root / "raw")
    data_dir = _env_path("MUNINN_DATA_DIR", repo_root / "data")
    db_path = _env_path("MUNINN_DB_PATH", data_dir / "muninn.db")
    vault = os.environ.get("MUNINN_VAULT_DIR")
    personal = os.environ.get("MUNINN_PERSONAL_VAULT_DIR")
    return Paths(
        raw_dir=raw_dir,
        data_dir=data_dir,
        db_path=db_path,
        scrape_cache_dir=data_dir / "scrape-cache",
        http_cache_dir=data_dir / "http-cache",
        vault_dir=Path(vault).expanduser().resolve() if vault else None,
        personal_vault_dir=Path(personal).expanduser().resolve() if personal else None,
    )


# ── Model IDs ─────────────────────────────────────────────────────
HAIKU_MODEL = "claude-haiku-4-5-20251001"

# ── Prompt versions ───────────────────────────────────────────────
PER_BOOKMARK_PROMPT_VERSION = "per_bookmark_v1"
SUITABILITY_PROMPT_VERSION = "suitability_v1"

# ── Suitability assessment (Research Intake, ADR-009) ─────────────
# Version of the deterministic rules around the model call (context
# selection, truncation, the no-candidate-without-content rule, output
# validation). Part of the assessment idempotency key: bump it when those
# rules change. See muninn.enrich.suitability.
SUITABILITY_POLICY_VERSION = "intake-policy/1"
# The model is read at call time (CLI/env) so RI-05 can calibrate it;
# the default is the per-bookmark enrichment model.
SUITABILITY_MODEL_ENV = "MUNINN_SUITABILITY_MODEL"
# The operator's interest profile is personal: its path comes from the
# environment. The repo ships only interest_profile.example.yml.
INTEREST_PROFILE_ENV = "MUNINN_INTEREST_PROFILE"

# ── Embeddings ────────────────────────────────────────────────────
# Backend is read at call time (muninn.vector.embed) so tests can switch it;
# model + dim are import-time constants. EmbeddingGemma is Matryoshka-
# truncatable: 768 native, 512/256/128 valid via MUNINN_EMBEDDING_DIM.
EMBEDDING_MODEL = os.environ.get("MUNINN_EMBEDDING_MODEL", "google/embeddinggemma-300m")
EMBEDDING_DIM = int(os.environ.get("MUNINN_EMBEDDING_DIM", "768"))

# ── Qdrant ────────────────────────────────────────────────────────
QDRANT_URL = os.environ.get("QDRANT_URL", "http://192.168.86.19:6333")
QDRANT_COLLECTION = os.environ.get("QDRANT_COLLECTION", "muninn_bookmarks")
QDRANT_VECTOR_DIM = EMBEDDING_DIM  # the collection is sized by the embedding model

# ── Anthropic ─────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

# ── Scrape ────────────────────────────────────────────────────────
LIVE_DOMAIN_RPS = 1.0
IA_GLOBAL_RPS = 0.5
AT_CAPTURE_WINDOW_DAYS = 365
HTTP_TIMEOUT_SECONDS = 30
# Live-pass HTTP cache entries older than this are refetched (RI-01 D9), so a
# later `muninn scrape` can capture a new revision; `--refresh` bypasses the
# cache entirely. Archive snapshot bodies never expire: snapshots are
# immutable.
LIVE_CACHE_TTL_SECONDS = int(os.environ.get("MUNINN_LIVE_CACHE_TTL_SECONDS", str(24 * 3600)))
# The recent_archive pass asks the IA CDX API for the *latest* snapshot,
# which changes as IA archives the page again, so that lookup is cached for
# this long (default 30 days); `--refresh` bypasses it too. The date-bounded
# at_capture lookups and the snapshot bodies stay cached forever.
RECENT_CDX_TTL_SECONDS = int(
    os.environ.get("MUNINN_RECENT_CDX_TTL_SECONDS", str(30 * 24 * 3600))
)
# The live pass honours robots.txt (RFC 9309). Each origin's file is cached
# this long (default 24 hours, the RFC's upper bound); `--refresh` refetches
# it. A file that answers 5xx or cannot be fetched is not cached: that
# origin is skipped for the run and asked again next time.
ROBOTS_CACHE_TTL_SECONDS = int(
    os.environ.get("MUNINN_ROBOTS_CACHE_TTL_SECONDS", str(24 * 3600))
)


# ── X API (muninn ingest x / muninn x auth) ───────────────────────
@dataclass(frozen=True, repr=False)
class XSettings:
    """OAuth 2.0 app settings for the X connector, from the environment.

    ``client_secret`` is set only for a confidential client. Tokens are never
    read from the environment: they live in ``token_path`` (mode 0600),
    written by `muninn x auth`.
    """

    client_id: str | None
    client_secret: str | None
    redirect_uri: str | None
    token_path: Path

    def __repr__(self) -> str:
        secret = "<redacted>" if self.client_secret else None
        return (
            f"XSettings(client_id={self.client_id!r}, client_secret={secret}, "
            f"redirect_uri={self.redirect_uri!r}, token_path={str(self.token_path)!r})"
        )


def load_x_settings() -> XSettings:
    """Read MUNINN_X_CLIENT_ID, MUNINN_X_CLIENT_SECRET (optional),
    MUNINN_X_REDIRECT_URI and MUNINN_X_TOKEN_FILE (default
    $MUNINN_DATA_DIR/x-tokens.json) at call time."""
    data_dir = load_paths().data_dir
    return XSettings(
        client_id=os.environ.get("MUNINN_X_CLIENT_ID") or None,
        client_secret=os.environ.get("MUNINN_X_CLIENT_SECRET") or None,
        redirect_uri=os.environ.get("MUNINN_X_REDIRECT_URI") or None,
        token_path=_env_path("MUNINN_X_TOKEN_FILE", data_dir / "x-tokens.json"),
    )


# ── Synthesis (ADR-013 task/result contract) ──────────────────────
# Prompt version of the synthesis instructions (synthesis/prompts/).
SYNTHESIS_PROMPT_VERSION = "synthesis_v1"
# The default runner is a budgeted direct API call. Model, effort and the
# monthly cap are read at call time (CLI flag, else env) so a run can be
# re-pointed without a code change. Opus 5.5 per ADR-013.
SYNTHESIS_MODEL_ENV = "MUNINN_SYNTHESIS_MODEL"
SYNTHESIS_DEFAULT_MODEL = "claude-opus-5-5"
SYNTHESIS_EFFORT_ENV = "MUNINN_SYNTHESIS_EFFORT"
SYNTHESIS_DEFAULT_EFFORT = "high"
# Monthly cap on synthesis API spend (USD), summed from usage_records over
# the current UTC calendar month. Required: the API runner refuses to
# spend without it (ADR-013: "API spend for synthesis requires a
# configured budget").
SYNTHESIS_MONTHLY_BUDGET_ENV = "MUNINN_SYNTHESIS_MONTHLY_BUDGET_USD"


def synthesis_task_registry() -> Path:
    """Where muninn keeps its own copy of every emitted task file
    ($MUNINN_DATA_DIR/synthesis/tasks). `synthesize ingest-result` checks
    a result against this copy, never against the copy handed out."""
    return load_paths().data_dir / "synthesis" / "tasks"
