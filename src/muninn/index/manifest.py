"""Private durable generation metadata. Readiness does not imply activation."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from muninn.index.embedding_profile import EmbeddingProfile

SCHEMA = "muninn-index-generation/1"


def encoded(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fsync_dir(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


@dataclass(frozen=True)
class Manifest:
    corpus_id: str
    dataset_id: str
    generation_id: str
    namespaces: tuple[str, ...]
    source_epoch: int
    authored_inventory_digest: str | None
    embedding_profile: dict
    profile_id: str
    source_count: int
    chunk_count: int
    omissions: tuple[dict, ...]
    braid: dict
    created_at: int
    projection_version: str = "muninn-braid-projection/1"
    schema: str = SCHEMA
    contract_revision: int = 1

    @property
    def digest(self) -> str:
        return hashlib.sha256(encoded(asdict(self))).hexdigest()

    def profile(self) -> EmbeddingProfile:
        fields = dict(self.embedding_profile)
        fields["runtime_versions"] = tuple(tuple(pair) for pair in fields["runtime_versions"])
        profile = EmbeddingProfile(**fields)
        profile.require_complete()
        if not profile.semantic_eligible or profile.profile_id != self.profile_id:
            raise ValueError("manifest_profile_mismatch")
        return profile

    def write(self, path: Path) -> None:
        self.profile()
        atomic_write(path, encoded(asdict(self)))

    @classmethod
    def read(cls, path: Path, *, expected_digest: str | None = None) -> Manifest:
        with path.open("rb") as stream:
            raw = stream.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            raise ValueError("manifest_too_large")
        fields = json.loads(raw)
        fields["namespaces"] = tuple(fields["namespaces"])
        fields["omissions"] = tuple(fields["omissions"])
        fields["embedding_profile"]["runtime_versions"] = tuple(
            tuple(pair) for pair in fields["embedding_profile"]["runtime_versions"]
        )
        manifest = cls(**fields)
        if manifest.schema != SCHEMA or manifest.contract_revision != 1:
            raise ValueError("manifest_schema_mismatch")
        manifest.profile()
        if expected_digest is not None and manifest.digest != expected_digest:
            raise ValueError("manifest_digest_mismatch")
        return manifest
