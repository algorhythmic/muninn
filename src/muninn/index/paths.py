"""All disposable index artifacts default beneath Muninn's configured data dir."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class IndexPaths:
    root: Path

    @property
    def embedding_cache_dir(self) -> Path:
        return self.root / "embedding-cache"

    @property
    def generations_dir(self) -> Path:
        return self.root / "generations"

    @property
    def staging_dir(self) -> Path:
        return self.root / "staging"

    @property
    def build_dir(self) -> Path:
        return self.root / "braid-build"

    def generation_dir(self, generation_id: str, *, staging: bool = False) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", generation_id):
            raise ValueError("invalid generation identifier")
        return (self.staging_dir if staging else self.generations_dir) / generation_id

    def manifest_path(self, generation_id: str, *, staging: bool = False) -> Path:
        return self.generation_dir(generation_id, staging=staging) / "manifest.json"


def load_index_paths() -> IndexPaths:
    from muninn.config import load_paths

    return IndexPaths(load_paths().data_dir / "index")
