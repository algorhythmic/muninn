"""Synthetic interest profiles and a fake Anthropic client for intake tests.

The client is a ``MagicMock`` whose ``messages.create`` returns
SDK-shaped responses (``content[0].text`` plus ``usage``), the same
injection pattern ``test_pipeline_idempotent`` uses. No network, no model.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import MagicMock

PROFILE_V1 = """\
# synthetic test profile
profile_id: test-research
label: Synthetic test interests
version_label: v1
interests:
  - interest_id: retrieval-eval
    label: Retrieval evaluation
    description: Benchmarks and methods for evaluating search over small corpora.
    weight: 80
  - interest_id: sqlite-tooling
    label: SQLite tooling
    description: Migrations, FTS5 and embedded analytics.
    weight: 60
exclusions:
  - exclusion_id: hype
    description: Announcements without method or data.
"""

PROFILE_V2 = PROFILE_V1.replace("version_label: v1", "version_label: v2").replace("weight: 80", "weight: 90")

_SUBJECT_RE = re.compile(r"^SUBJECT ITEM\nitem_id: (\S+)", re.M)


def write_profile(dir_: Path, text: str = PROFILE_V1, name: str = "profile.yml") -> Path:
    path = dir_ / name
    path.write_text(text, encoding="utf-8")
    return path


def judgement(disposition: str = "candidate", **overrides: Any) -> dict[str, Any]:
    out = {
        "disposition": disposition,
        "relevance": 80,
        "confidence": 70,
        "rationale": "States a method and a measured result relevant to retrieval evaluation.",
        "matched_interests": ["retrieval-eval"],
        "unresolved_questions": ["Is the benchmark corpus available?"],
    }
    out.update(overrides)
    return out


def response(
    payload: Any,
    *,
    input_tokens: int = 1000,
    output_tokens: int = 200,
    cache_read: int = 0,
    cache_write: int = 0,
    stop_reason: str = "end_turn",
) -> SimpleNamespace:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_write,
        ),
        stop_reason=stop_reason,
    )


def subject_of(kwargs: dict[str, Any]) -> str:
    m = _SUBJECT_RE.search(kwargs["messages"][0]["content"])
    assert m, "user message has no subject item"
    return m.group(1)


def fake_client(
    by_item: dict[str, Any] | None = None,
    default: Callable[[str], Any] | Any = None,
    **usage: int,
) -> MagicMock:
    """A client answering per subject item_id; ``default`` for the rest.

    A value may be a judgement dict, raw text, a ready response, or an
    exception instance (raised).
    """
    by_item = by_item or {}

    def create(**kwargs: Any) -> Any:
        item = subject_of(kwargs)
        value = by_item.get(item, default if default is not None else judgement())
        if callable(value) and not isinstance(value, dict):
            value = value(item)
        if isinstance(value, BaseException):
            raise value
        if isinstance(value, SimpleNamespace):
            return value
        return response(value, **usage)

    client = MagicMock()
    client.messages.create.side_effect = create
    return client
