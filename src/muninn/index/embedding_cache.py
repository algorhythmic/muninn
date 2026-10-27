"""Private, disposable document vectors bound to an exact embedding space.

Only digests and numeric vectors are persisted. Publishing may retain complete
entries after cancellation; entries are never evidence of a published generation.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:
    from muninn.index.embedding_profile import EmbeddingProfile


def default_cache_dir() -> Path:
    # Lazy config import keeps model libraries out of module import paths.
    from muninn.index.paths import load_index_paths

    return load_index_paths().embedding_cache_dir


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _vector(values: Sequence[float], dimension: int) -> list[float]:
    if len(values) != dimension or not values:
        raise ValueError("embedding dimension mismatch")
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values):
        raise ValueError("embedding values must be numbers")
    result = [float(value) for value in values]
    if any(not math.isfinite(value) for value in result) or not any(result):
        raise ValueError("embedding must be finite and nonzero")
    return result


class EmbeddingCache:
    """Atomic per-input cache; explicit roots are useful for temporary builds."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else default_cache_dir()

    def _path(self, profile: EmbeddingProfile, text: str) -> Path:
        profile.require_complete()
        # Hash the profile too, so even an invalid caller-supplied identifier
        # cannot introduce a path separator or expose model names in filenames.
        return self.root / _digest(profile.profile_id) / (_digest(text) + ".json")

    def get(self, profile: EmbeddingProfile, text: str) -> list[float] | None:
        path = self._path(profile, text)
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
            if (
                entry["version"] != 1
                or entry["profile_id"] != profile.profile_id
                or entry["input_digest"] != _digest(text)
                or entry["dimension"] != profile.dimension
                or entry["encoding"] != "float64-json"
            ):
                return None
            return _vector(entry["vector"], profile.dimension)
        except (OSError, ValueError, TypeError, KeyError, OverflowError):
            # Corruption or stale metadata never permits changing embedding space.
            return None

    def put(self, profile: EmbeddingProfile, text: str, vector: Sequence[float]) -> None:
        path = self._path(profile, text)
        values = _vector(vector, profile.dimension)
        entry = {
            "version": 1,
            "profile_id": profile.profile_id,
            "input_digest": _digest(text),
            "dimension": profile.dimension,
            "encoding": "float64-json",
            "vector": values,
        }
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        path.parent.mkdir(exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        temporary: str | None = None
        try:
            fd, temporary = tempfile.mkstemp(prefix=".entry-", dir=path.parent)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(entry, stream, allow_nan=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            temporary = None
            # Persist both file contents and the directory entry before success.
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)


def get(profile: EmbeddingProfile, text: str) -> list[float] | None:
    return EmbeddingCache().get(profile, text)


def put(profile: EmbeddingProfile, text: str, vector: Sequence[float]) -> None:
    EmbeddingCache().put(profile, text, vector)
