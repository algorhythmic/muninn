"""Versioned internal document results, independent of bookmark SearchHit."""
from dataclasses import asdict, dataclass, field

DOCUMENT_SCHEMA = "muninn-document-search/1"


class DocumentBackendUnavailable(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class DocumentHit:
    document_id: str
    namespace: str
    source_ref: str
    source_revision: str
    score: float
    matched_stages: tuple[str, ...] = ()
    chunk_refs: tuple[dict, ...] = ()
    payload: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class DocumentSearchResult:
    hits: tuple[DocumentHit, ...]
    backend: str = "braid"
    generation: dict | None = None
    coverage: dict = field(default_factory=dict)
    fallback_reason: str | None = None
    warnings: tuple[str, ...] = ()
    schema: str = DOCUMENT_SCHEMA

    def to_dict(self):
        return asdict(self)
