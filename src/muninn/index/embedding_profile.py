"""Profile-bound inference for derived indexes, independent of Qdrant's API.

Profile discovery is explicit and lazy. A mutable model alias alone is never a
resolved artifact. Query vectors are sent only after capability negotiation.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import re
from numbers import Real
from dataclasses import asdict, dataclass
from typing import Iterable, Iterator, TYPE_CHECKING

if TYPE_CHECKING:
    from muninn.index.embedding_cache import EmbeddingCache

ADAPTER_VERSION = "muninn-profile-encoder/1"
PROFILE_SCHEMA = "muninn-embedding-profile/1"
QUERY_CAPABILITY = "query_embedding"  # contract revision 1; negotiate first


class ProfileUnavailable(RuntimeError):
    """The exact published embedding space cannot be reproduced locally."""


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


@dataclass(frozen=True)
class EmbeddingProfile:
    model: str
    artifact_revision: str | None
    dimension: int
    query_prompt_digest: str | None
    document_prompt_digest: str | None
    tokenizer_digest: str | None
    runtime_versions: tuple[tuple[str, str], ...] = ()
    backend: str = "sentence-transformers"
    normalization: str = "l2"
    query_prompt_name: str = "query"
    document_prompt_name: str = "document"
    adapter_version: str = ADAPTER_VERSION
    schema: str = PROFILE_SCHEMA

    @property
    def profile_id(self) -> str:
        return "muninn-embedding-profile:" + _digest(_canonical(asdict(self)))

    @property
    def incomplete_reasons(self) -> tuple[str, ...]:
        reasons = []
        for name in ("model", "artifact_revision", "query_prompt_digest",
                     "document_prompt_digest", "tokenizer_digest"):
            if not getattr(self, name):
                reasons.append(name)
        if self.dimension <= 0:
            reasons.append("dimension")
        if self.backend == "sentence-transformers" and not self.runtime_versions:
            reasons.append("runtime_versions")
        if self.normalization != "l2" or self.schema != PROFILE_SCHEMA:
            reasons.append("unsupported_schema_or_normalization")
        return tuple(reasons)

    @property
    def semantic_eligible(self) -> bool:
        return not self.incomplete_reasons and self.backend == "sentence-transformers"

    def require_complete(self) -> None:
        if self.incomplete_reasons:
            raise ProfileUnavailable("incomplete_profile:" + ",".join(self.incomplete_reasons))


def _model_profile(model) -> EmbeddingProfile:
    from muninn.config import EMBEDDING_DIM, EMBEDDING_MODEL

    first = model._first_module()
    config = getattr(getattr(first, "auto_model", None), "config", None)
    revision = getattr(config, "_commit_hash", None)
    # Hugging Face snapshot commits are immutable; aliases/branch names aren't.
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40,64}", revision):
        revision = None
    prompts = getattr(model, "prompts", {})
    tokenizer = getattr(first, "tokenizer", None)
    tokenizer_digest = None
    backend_tokenizer = getattr(tokenizer, "backend_tokenizer", None)
    if backend_tokenizer is not None:
        tokenizer_digest = _digest(_canonical({
            "serialized": backend_tokenizer.to_str(),
            "class": type(tokenizer).__qualname__,
            "max_seq_length": getattr(model, "max_seq_length", None),
            "model_max_length": getattr(tokenizer, "model_max_length", None),
            "truncation_side": getattr(tokenizer, "truncation_side", None),
            "padding_side": getattr(tokenizer, "padding_side", None),
            "special_tokens_map": getattr(tokenizer, "special_tokens_map", {}),
        }))
    versions = []
    for package in ("sentence-transformers", "transformers", "tokenizers", "torch"):
        try:
            versions.append((package, importlib.metadata.version(package)))
        except importlib.metadata.PackageNotFoundError:
            return EmbeddingProfile(EMBEDDING_MODEL, revision, EMBEDDING_DIM,
                                    None, None, tokenizer_digest)
    return EmbeddingProfile(
        EMBEDDING_MODEL, revision, EMBEDDING_DIM,
        _digest(prompts["query"]) if isinstance(prompts.get("query"), str) else None,
        _digest(prompts["document"]) if isinstance(prompts.get("document"), str) else None,
        tokenizer_digest, tuple(versions),
    )


def available_profile() -> EmbeddingProfile:
    """Resolve the loaded model without substituting another backend or prompt."""
    from muninn.vector import embed

    if embed._backend() != "sentence-transformers":
        raise ProfileUnavailable("semantic_backend_unavailable")
    try:
        with embed._st_inference_lock:
            return _model_profile(embed._get_st_model())
    except ProfileUnavailable:
        raise
    except (RuntimeError, OSError, ValueError) as exc:
        raise ProfileUnavailable("encoder_unavailable") from exc


def validate_vector(vector: Iterable[float], dimension: int) -> list[float]:
    values = list(vector)
    if (len(values) != dimension or any(isinstance(v, bool) or not isinstance(v, Real)
                                      or not math.isfinite(v) for v in values)
            or not any(values)):
        raise ProfileUnavailable("invalid_vector")
    return [float(v) for v in values]


def _encode(profile: EmbeddingProfile, texts: list[str], prompt_name: str) -> list[list[float]]:
    from muninn.vector import embed

    profile.require_complete()
    if not profile.semantic_eligible or embed._backend() != "sentence-transformers":
        raise ProfileUnavailable("semantic_backend_unavailable")
    try:
        # Shared with the existing encoder: model calls serialize, loading uses
        # its existing separate lock. No unprompted fallback in this adapter.
        with embed._st_inference_lock:
            model = embed._get_st_model()
            if _model_profile(model).profile_id != profile.profile_id:
                raise ProfileUnavailable("profile_mismatch")
            vectors = model.encode(texts, prompt_name=prompt_name,
                                   normalize_embeddings=True, batch_size=len(texts))
            if len(vectors) != len(texts):
                raise ProfileUnavailable("invalid_vector_count")
            return [validate_vector(vector.tolist() if hasattr(vector, "tolist") else vector,
                                    profile.dimension)
                    for vector in vectors]
    except ProfileUnavailable:
        raise
    except (RuntimeError, OSError, KeyError, ValueError) as exc:
        raise ProfileUnavailable("encoder_unavailable") from exc


def query_embedding(text: str, profile: EmbeddingProfile, *,
                    capabilities: Iterable[str], protocol_version: int = 1) -> dict:
    """Contract revision 1 field; consumers must negotiate before encoding."""
    if protocol_version != 1 or QUERY_CAPABILITY not in capabilities:
        raise ProfileUnavailable("query_embedding_capability_unavailable")
    if not text.strip():
        raise ValueError("query must not be empty")
    if len(text.encode("utf-8")) > 65536:
        raise ValueError("query exceeds encoding limit")
    return {"model": profile.profile_id,
            "vector": _encode(profile, [text], profile.query_prompt_name)[0]}


@dataclass(frozen=True)
class DocumentTokenizer:
    """Count complete prompted document inputs without model truncation.

    This adapter includes the actual document prompt and special tokens. Pass
    zero additional prompt overhead to the chunker and cap its max_tokens at
    max_seq_length. Tokenization serializes with inference on the shared model.
    """

    profile: EmbeddingProfile
    digest: str
    max_seq_length: int

    def count_tokens(self, text: str) -> int:
        from muninn.vector import embed

        if embed._backend() != "sentence-transformers":
            raise ProfileUnavailable("semantic_backend_unavailable")
        try:
            with embed._st_inference_lock:
                model = embed._get_st_model()
                if _model_profile(model).profile_id != self.profile.profile_id:
                    raise ProfileUnavailable("profile_mismatch")
                tokenizer = model._first_module().tokenizer
                prompt = model.prompts[self.profile.document_prompt_name]
                tokens = tokenizer.encode(prompt + text, add_special_tokens=True,
                                          truncation=False)
                return len(tokens)
        except ProfileUnavailable:
            raise
        except (RuntimeError, OSError, ValueError, KeyError, AttributeError, TypeError) as exc:
            raise ProfileUnavailable("tokenizer_unavailable") from exc


def document_tokenizer(profile: EmbeddingProfile) -> DocumentTokenizer:
    """Resolve a tokenizer only for the exact complete profile requested."""
    from muninn.vector import embed

    profile.require_complete()
    if not profile.semantic_eligible or embed._backend() != "sentence-transformers":
        raise ProfileUnavailable("semantic_backend_unavailable")
    try:
        with embed._st_inference_lock:
            model = embed._get_st_model()
            if _model_profile(model).profile_id != profile.profile_id:
                raise ProfileUnavailable("profile_mismatch")
            maximum = getattr(model, "max_seq_length", None)
            if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum <= 0:
                raise ProfileUnavailable("tokenizer_max_length_unavailable")
            tokenizer = getattr(model._first_module(), "tokenizer", None)
            if not callable(getattr(tokenizer, "encode", None)):
                raise ProfileUnavailable("tokenizer_unavailable")
            return DocumentTokenizer(profile, profile.tokenizer_digest, maximum)
    except ProfileUnavailable:
        raise
    except (RuntimeError, OSError, ValueError, KeyError, AttributeError, TypeError) as exc:
        raise ProfileUnavailable("tokenizer_unavailable") from exc


def document_embeddings(texts: Iterable[str], profile: EmbeddingProfile, *,
                        cache: EmbeddingCache | None = None, batch_size: int = 32,
                        max_batch_bytes: int = 262144,
                        max_input_bytes: int = 65536) -> Iterator[list[float]]:
    """Yield in input order with bounded inference batches and cache writes.

    Complete entries survive cancellation. Callers must separately enforce the
    tokenizer's chunk budget; these byte bounds limit adapter memory/work.
    """
    profile.require_complete()
    if not profile.semantic_eligible:
        raise ProfileUnavailable("semantic_backend_unavailable")
    if not 1 <= batch_size <= 256 or not 1 <= max_input_bytes <= max_batch_bytes <= 8388608:
        raise ValueError("invalid encoding bounds")

    def encode_batch(batch: list[str]) -> list[list[float]]:
        results = [cache.get(profile, text) if cache else None for text in batch]
        missing = [i for i, result in enumerate(results) if result is None]
        if missing:
            unique = list(dict.fromkeys(batch[i] for i in missing))
            vectors = _encode(profile, unique, profile.document_prompt_name)
            by_text = dict(zip(unique, vectors))
            for text, vector in by_text.items():
                if cache:
                    cache.put(profile, text, vector)
            for i in missing:
                results[i] = by_text[batch[i]].copy()
        return results  # type: ignore[return-value]

    batch: list[str] = []
    size = 0
    for text in texts:
        length = len(text.encode("utf-8"))
        if length > max_input_bytes:
            raise ValueError("document input exceeds encoding limit")
        if batch and (len(batch) == batch_size or size + length > max_batch_bytes):
            yield from encode_batch(batch)
            batch, size = [], 0
        batch.append(text)
        size += length
    if batch:
        yield from encode_batch(batch)
