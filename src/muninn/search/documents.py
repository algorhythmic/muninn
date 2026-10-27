"""Versioned internal document results, independent of bookmark SearchHit."""
from dataclasses import asdict, dataclass, field
import re

DOCUMENT_SCHEMA = "muninn-document-search/1"
CONTEXT_SCHEMA = "muninn-context/1"
CONTEXT_ESTIMATOR = "muninn-json-utf8-quarter/1"
CONTEXT_MAX_BUDGET = 10000
CONTEXT_MAX_BYTES = 65536
CONTEXT_MAX_ROOTS = 12


@dataclass(frozen=True)
class ContextRequest:
    query: str
    namespaces: tuple[str, ...] = ("bookmarks", "concepts", "infra", "people", "projects")
    budget: int = 2500
    max_bytes: int = 16384
    anchors: tuple[str, ...] = ()
    required_sources: tuple[str, ...] = ()
    expected_generation: str | None = None
    optional_roots: int = 12

    def __post_init__(self):
        if not isinstance(self.query, str) or not self.query.strip() or len(self.query.encode()) > 65536:
            raise ValueError("invalid_query")
        if (not isinstance(self.namespaces, tuple) or not self.namespaces
                or len(set(self.namespaces)) != len(self.namespaces)
                or set(self.namespaces) - {"bookmarks", "concepts", "infra", "people", "projects"}):
            raise ValueError("invalid_namespaces")
        for name, low, high in (("budget", 0, CONTEXT_MAX_BUDGET),
                                ("max_bytes", 2, CONTEXT_MAX_BYTES),
                                ("optional_roots", 0, CONTEXT_MAX_ROOTS)):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError("invalid_" + name)
        for name in ("anchors", "required_sources"):
            refs = getattr(self, name)
            if not isinstance(refs, tuple) or len(refs) > 24 or len(set(refs)) != len(refs):
                raise ValueError("invalid_" + name)
            for ref in refs:
                if not isinstance(ref, str):
                    raise ValueError("invalid_source_reference")
                if re.fullmatch(r"bookmark:[1-9][0-9]*", ref):
                    continue
                parts = ref.removeprefix("vault:").split("/")
                if (not ref.startswith("vault:wiki/") or len(parts) < 3
                        or parts[1] not in ("concepts", "infra", "people", "projects")
                        or not ref.endswith(".md") or len(ref) > 512 or "\\" in ref or "\x00" in ref
                        or any(not p or p.startswith(".") for p in parts)):
                    raise ValueError("invalid_source_reference")
        if self.expected_generation is not None and (not isinstance(self.expected_generation, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", self.expected_generation)):
            raise ValueError("invalid_expected_generation")


@dataclass(frozen=True)
class ContextEntry:
    id: str
    type: str
    text: str
    attrs: dict


@dataclass(frozen=True)
class ContextResult:
    context: tuple[ContextEntry, ...] = ()
    backend: str = "unavailable"
    generation: dict | None = None
    context_bytes: int = 2
    accounting: dict = field(default_factory=dict)
    coverage: dict = field(default_factory=dict)
    state: str = "ok"
    reason: str | None = None
    fallback_reason: str | None = None
    omissions: tuple[dict, ...] = ()
    warnings: tuple[str, ...] = ()
    schema: str = CONTEXT_SCHEMA

    def to_dict(self):
        return asdict(self)


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
