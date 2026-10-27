"""Deterministic normalized-body spans with explicitly supplied token counting.

The caller supplies the pinned model tokenizer and prompt overhead. No model is
loaded here. Token counts are checked on the complete prefixed embedding input;
the prefix is never part of a citation's body coordinates.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Iterator, Protocol

from muninn.consumers.packets.canonical import TEXT_NORMALIZATION, normalize_text

CHUNKER_VERSION = "muninn-body-chunker/1"


class PinnedTokenizer(Protocol):
    """Digest identifies exact tokenizer artifacts/config, not a mutable alias.

    count_tokens must count model document tokens deterministically, including
    tokenizer-added special tokens, but excluding separately declared prompt
    overhead. It must not silently truncate its input.
    """

    digest: str

    def count_tokens(self, text: str) -> int: ...


@dataclass(frozen=True)
class ChunkSettings:
    target_tokens: int = 512
    max_tokens: int = 768
    overlap_tokens: int = 64
    prompt_overhead: int = 0

    def __post_init__(self) -> None:
        if not 0 < self.target_tokens <= self.max_tokens:
            raise ValueError("target must be positive and at most max_tokens")
        if not 0 <= self.overlap_tokens < self.target_tokens:
            raise ValueError("overlap must be nonnegative and below target_tokens")
        if not 0 <= self.prompt_overhead < self.max_tokens:
            raise ValueError("prompt overhead must leave document capacity")


@dataclass(frozen=True)
class BodyChunk:
    chunk_id: str
    document_id: str
    projection_revision: str
    profile_id: str
    tokenizer_digest: str
    chunker_version: str
    ordinal: int
    start: int
    end: int
    text: str
    text_digest: str
    prefix: str
    token_count: int
    normalization: str = TEXT_NORMALIZATION

    @property
    def embedding_input(self) -> str:
        return self.prefix + self.text


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _boundaries(text: str) -> tuple[list[int], list[tuple[int, int]]]:
    """Paragraph/heading boundaries outside fenced blocks, plus fence extents."""
    boundaries = []
    fences = []
    opened: tuple[str, int, int] | None = None
    position = 0
    for line in text.splitlines(keepends=True):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if marker:
            run = marker.group(1)
            if opened is None:
                opened = (run[0], len(run), position)
                boundaries.append(position)
            elif run[0] == opened[0] and len(run) >= opened[1]:
                fences.append((opened[2], position + len(line)))
                opened = None
                boundaries.append(position + len(line))
        elif opened is None:
            if not line.strip():
                boundaries.append(position + len(line))
            elif re.match(r"^ {0,3}#{1,6}\s", line):
                boundaries.append(position)
        position += len(line)
    boundaries.append(len(text))
    return sorted(set(boundaries)), fences


def _fit(start: int, end: int, accepts) -> int:
    """Find a fitting endpoint with logarithmically many tokenizer calls.

    Tokenizers need not have strictly monotone counts: only accepted endpoints
    are returned, so irregular counts can shorten a chunk but cannot exceed its
    budget. No assumption is made that a Unicode character equals one token.
    """
    # Grow the probe locally before bisection; a long section must not cause
    # every small chunk to tokenize the entire remaining document.
    origin = start
    good = start
    distance = 1
    while start < end:
        probe = min(origin + distance, end)
        if not accepts(probe):
            end = probe - 1
            break
        good = start = probe
        if probe == end:
            return good
        distance *= 2
    while start < end:
        middle = (start + end + 1) // 2
        if accepts(middle):
            good = middle
            start = middle
        else:
            end = middle - 1
    return good


def chunk_document(
    document_id: str,
    projection_revision: str,
    body: str,
    *,
    profile_id: str,
    tokenizer: PinnedTokenizer,
    title: str = "",
    heading: str = "",
    settings: ChunkSettings = ChunkSettings(),
) -> Iterator[BodyChunk]:
    """Yield exact slices of ``normalize_text(body)`` with complete coverage.

    Empty bodies yield no chunks. An oversized prefix or a body character that
    cannot fit raises ValueError; callers must report this omission. IDs bind
    the effective chunker settings/profile/tokenizer, document, projection and
    ordinal. Input content changes therefore require a new projection revision.
    """
    if not document_id or not projection_revision or not profile_id or not tokenizer.digest:
        raise ValueError("document, projection, profile and pinned tokenizer identities required")
    model_max = getattr(tokenizer, "max_seq_length", None)
    if model_max is not None:
        if not isinstance(model_max, int) or isinstance(model_max, bool) or model_max <= 0:
            raise ValueError("tokenizer max_seq_length must be a positive integer")
        if settings.max_tokens > model_max:
            raise ValueError("configured max_tokens exceeds tokenizer max_seq_length")
    text = normalize_text(body)
    if not text:
        return
    prefix_parts = [normalize_text(part) for part in (title, heading) if normalize_text(part)]
    prefix = "\n".join(prefix_parts) + "\n\n" if prefix_parts else ""

    def tokens(value: str) -> int:
        count = tokenizer.count_tokens(value)
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError("tokenizer must return a nonnegative integer")
        return count

    def full_count(fragment: str) -> int:
        return tokens(prefix + fragment) + settings.prompt_overhead

    if full_count("") >= settings.max_tokens:
        raise ValueError("prefix/prompt leaves no body token capacity")
    version = CHUNKER_VERSION + ":" + _digest({
        "settings": asdict(settings), "tokenizer": tokenizer.digest,
        "profile": profile_id, "prefix": prefix,
    })
    boundaries, fences = _boundaries(text)
    start = 0
    previous_end = 0
    ordinal = 0
    while start < len(text):
        fits = lambda end: full_count(text[start:end]) <= settings.max_tokens
        maximum = _fit(start, len(text), fits)
        if maximum == start:
            raise ValueError(f"body cannot fit token budget at normalized character {start}")
        target = _fit(start, maximum, lambda end: fits(end) and tokens(text[start:end]) <= settings.target_tokens)
        end = target if target > start else maximum
        preferred = [point for point in boundaries if start < point <= end and fits(point)]
        if preferred:
            end = preferred[-1]
        # Keep a code fence together if it begins this chunk and fits the hard
        # limit, even when doing so exceeds the soft target.
        for fence_start, fence_end in fences:
            if fence_start == start and fence_end <= maximum and fits(fence_end):
                end = fence_end
                break
        if end <= previous_end:
            # Context-sensitive tokenization may make an overlapping suffix
            # expensive. Drop overlap and retry rather than emit a chunk that
            # adds no new body coverage.
            start = previous_end
            continue
        fragment = text[start:end]
        yield BodyChunk(
            chunk_id="muninn-chunk:" + _digest([document_id, projection_revision, version, ordinal]),
            document_id=document_id, projection_revision=projection_revision,
            profile_id=profile_id, tokenizer_digest=tokenizer.digest,
            chunker_version=version, ordinal=ordinal, start=start, end=end,
            text=fragment, text_digest=hashlib.sha256(fragment.encode("utf-8")).hexdigest(),
            prefix=prefix, token_count=full_count(fragment),
        )
        if end == len(text):
            break
        previous_end = end
        # Overlap split long sections, but move cleanly across natural block
        # boundaries. Overlapping a tiny paragraph could otherwise emit the
        # same paragraph repeatedly before progressing into the next block.
        if settings.overlap_tokens and end not in boundaries:
            overlap_length = _fit(0, end - start - 1,
                lambda length: tokens(text[end-length:end]) <= settings.overlap_tokens)
        else:
            overlap_length = 0
        start = end - overlap_length
        ordinal += 1
