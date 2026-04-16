"""Environment-driven config: paths, model IDs, prompt versions."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


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
OPUS_MODEL = "claude-opus-4-6"

# ── Prompt versions ───────────────────────────────────────────────
PER_BOOKMARK_PROMPT_VERSION = "per_bookmark_v1"

# ── Qdrant ────────────────────────────────────────────────────────
QDRANT_URL = os.environ.get("QDRANT_URL", "http://192.168.86.19:6333")
QDRANT_COLLECTION = os.environ.get("QDRANT_COLLECTION", "muninn_bookmarks")
QDRANT_VECTOR_DIM = 1024

# ── Anthropic ─────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

# ── Scrape ────────────────────────────────────────────────────────
LIVE_DOMAIN_RPS = 1.0
IA_GLOBAL_RPS = 0.5
AT_CAPTURE_WINDOW_DAYS = 365
HTTP_TIMEOUT_SECONDS = 30

# ── Synthesis container ───────────────────────────────────────────
SAGA_CREDENTIALS_VOLUME = os.environ.get("SAGA_CREDENTIALS_VOLUME", "saga-claude-credentials")
SYNTHESIS_WORKSPACE = os.environ.get("MUNINN_SYNTHESIS_WORKSPACE", "/tmp/muninn-synthesis")
