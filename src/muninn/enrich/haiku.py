"""Anthropic Haiku client for per-bookmark enrichment.

The system prompt is loaded from ``prompts/<PER_BOOKMARK_PROMPT_VERSION>.md``
and sent with ``cache_control={"type": "ephemeral"}`` so bulk passes hit
the prompt-caching path on the second and subsequent requests. The PRD
calls for ≥80% cache hit rate after the first ~100 calls — see
``EnrichmentStats.cache_hit_rate`` in the pipeline module.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from muninn.config import HAIKU_MODEL, PER_BOOKMARK_PROMPT_VERSION

if TYPE_CHECKING:  # pragma: no cover
    import anthropic

logger = logging.getLogger(__name__)

PROMPT_DIR = Path(__file__).parent / "prompts"

# Hard cap on user-message content length to keep token spend predictable.
# Roughly 12k chars ~ 3k tokens at typical English densities.
MAX_CONTENT_CHARS = 12_000

# Max output tokens for the JSON response.
MAX_OUTPUT_TOKENS = 1024


@dataclass
class EnrichmentResult:
    """Parsed Haiku response plus prompt-cache telemetry.

    ``cache_hit`` reflects whether the system prompt was served from cache
    on this specific request (i.e. ``cache_read_input_tokens > 0``).
    """

    summary: str
    tags: list[str]
    entities: list[str]
    content_type: str
    language: str
    cache_hit: bool
    input_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    output_tokens: int


def _load_system_prompt(version: str = PER_BOOKMARK_PROMPT_VERSION) -> str:
    """Load the system prompt body for the given ``<task>_v<N>`` version."""
    prompt_file = PROMPT_DIR / f"{version}.md"
    return prompt_file.read_text()


def _build_user_message(title: str, content: str) -> str:
    """Render the per-bookmark user message, with hard truncation."""
    if len(content) > MAX_CONTENT_CHARS:
        content = content[:MAX_CONTENT_CHARS] + "\n[... content truncated]"
    return f"Title: {title}\n\nContent:\n{content}"


def _parse_json_response(raw_text: str) -> dict:
    """Parse Haiku's response, tolerating markdown fences and preambles."""
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        pass
    # Fallback: yank the first {...} block out of the response.
    match = re.search(r"\{.*\}", raw_text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Failed to parse LLM response as JSON: {raw_text[:200]}"
            ) from exc
    raise ValueError(f"Failed to parse LLM response as JSON: {raw_text[:200]}")


def enrich_bookmark(
    title: str,
    content: str,
    client: "anthropic.Anthropic | None" = None,
    *,
    model: str = HAIKU_MODEL,
    prompt_version: str = PER_BOOKMARK_PROMPT_VERSION,
) -> EnrichmentResult:
    """Call Haiku once for a single bookmark.

    The system prompt carries ``cache_control={"type": "ephemeral"}`` so
    every call after the first within a 5-minute window of the same
    prompt+model pair reads it from cache. The returned
    ``EnrichmentResult`` exposes per-call cache telemetry; the pipeline
    aggregates it across the run.
    """
    if client is None:
        import anthropic  # local import: keeps tests importable without env vars

        client = anthropic.Anthropic()

    system_prompt = _load_system_prompt(prompt_version)
    user_msg = _build_user_message(title, content)

    response = client.messages.create(
        model=model,
        max_tokens=MAX_OUTPUT_TOKENS,
        system=[
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[
            {"role": "user", "content": user_msg},
        ],
    )

    raw_text = response.content[0].text
    data = _parse_json_response(raw_text)

    usage = response.usage
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_creation = getattr(usage, "cache_creation_input_tokens", 0) or 0
    output_tokens = getattr(usage, "output_tokens", 0) or 0

    return EnrichmentResult(
        summary=data.get("summary", ""),
        tags=list(data.get("tags") or []),
        entities=list(data.get("entities") or []),
        content_type=data.get("content_type", "other"),
        language=data.get("language", "en"),
        cache_hit=cache_read > 0,
        input_tokens=usage.input_tokens,
        cache_read_tokens=cache_read,
        cache_creation_tokens=cache_creation,
        output_tokens=output_tokens,
    )


def build_embedding_text(title: str, summary: str, tags: list[str]) -> str:
    """Compose the text fed into the embedding model.

    Embeddings live in Qdrant only — the canonical SQL schema does not
    persist the embedding text — so this is recomputed on demand both at
    enrich time and at reconcile time.
    """
    tag_str = ", ".join(tags) if tags else ""
    return f"{title}\n{summary}\n{tag_str}"
