"""Synthetic corpus, valid outputs and fakes for the synthesis tests.

The Anthropic client is a ``MagicMock`` whose ``messages.stream(...)``
context manager yields SDK-shaped final messages (text + thinking blocks,
``usage``, ``stop_reason``, ``model``, ``id``) — the same injection pattern
``test_pipeline_idempotent`` uses for ``messages.create``. No network.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from muninn.synthesis.api_runner import RunnerResult


def populate(conn: sqlite3.Connection) -> sqlite3.Connection:
    """Three 'early-web' bookmarks with captured text, plus bookmark 99 in
    another era (a cross-ref target only when passed explicitly)."""
    cur = conn.cursor()
    now = int(time.time())
    for bm_id, title in [(1, "HTML 4 spec"), (2, "Mosaic browser"), (3, "Yahoo dir")]:
        cur.execute(
            "INSERT INTO bookmarks (bookmark_id, source, source_id, captured_at, "
            "title, url, era_label, domain, enrichment_source) "
            "VALUES (?, 'netscape', ?, ?, ?, 'http://example.com/' || ?, "
            "'early-web', 'example.com', 'at_capture')",
            (bm_id, f"src{bm_id}", now - 86400 * bm_id, title, bm_id),
        )
        cur.execute(
            "INSERT INTO scrape_results (bookmark_id, pass, fetched_at, "
            "scrape_status, content_text) "
            "VALUES (?, 'at_capture', ?, 'ok', ?)",
            (bm_id, now, f"Content for bookmark {bm_id}: verbatim quote from source"),
        )
    cur.execute(
        "INSERT INTO bookmarks (bookmark_id, source, source_id, captured_at, "
        "title, url, era_label, domain, enrichment_source) "
        "VALUES (99, 'netscape', 'src99', ?, 'Other', 'http://x', "
        "'late-web', 'x.com', 'at_capture')",
        (now,),
    )
    conn.commit()
    return conn


def era_output() -> dict:
    return {
        "era_label": "early-web",
        "narrative": "A" * 60,
        "inferred_year": 1995,
        "dominant_topics": ["web", "html"],
        "dominant_domains": ["example.com"],
        "synthesis_metadata": {
            "model": "claude-opus-4-6",
            "prompt_version": "synthesis_v1",
            "input_token_count": 5000,
            "output_token_count": 1200,
            "neighboring_eras_used": [],
        },
    }


def deep_pass_output(target_id: int = 2) -> dict:
    return {
        "bookmark_id": 1,
        "summary": "S" * 120,
        "tags": ["web", "history"],
        "entities": [{"name": "HTML", "type": "spec"}],
        "content_type": "article",
        "language": "en",
        "word_count": 500,
        "key_quotes": ["verbatim quote from source"],
        "cross_references": [
            {
                "target_bookmark_id": target_id,
                "relationship": "cites",
                "confidence": 0.9,
            }
        ],
        "synthesis_metadata": {
            "model": "claude-opus-4-6",
            "prompt_version": "synthesis_v1",
            "input_token_count": 8000,
            "output_token_count": 2000,
        },
    }


def adhoc_output() -> dict:
    return {
        "prompt": "what themes",
        "narrative": "N" * 220,
        "key_findings": [
            {"finding": "the early web was personal", "supporting_evidence": ["1"]}
        ],
        "referenced_bookmarks": [1, 2, 3],
        "synthesis_metadata": {
            "model": "claude-opus-4-6",
            "prompt_version": "synthesis_v1",
            "input_token_count": 10000,
            "output_token_count": 3000,
        },
    }


@dataclass
class MockRunner:
    """A free runner (no estimate, no cost) returning canned outputs in order."""
    outputs: list[dict[str, Any]] = field(default_factory=list)
    failure_on: list[int] = field(default_factory=list)  # 1-indexed attempts
    call_count: int = 0
    received_tasks: list[dict[str, Any]] = field(default_factory=list)

    def run(self, task: dict[str, Any]) -> RunnerResult:
        self.call_count += 1
        self.received_tasks.append(task)
        if task["attempt"] in self.failure_on:
            return RunnerResult(error="harness exploded", runner_ref=f"mock-{self.call_count}")
        idx = self.call_count - 1
        out = self.outputs[idx] if idx < len(self.outputs) else self.outputs[-1]
        return RunnerResult(output_json=json.loads(json.dumps(out)), runner_ref=f"mock-{self.call_count}")


def message(
    output: dict | str,
    *,
    input_tokens: int = 1000,
    output_tokens: int = 500,
    stop_reason: str = "end_turn",
    model: str = "claude-opus-5-5",
    msg_id: str = "msg_test",
) -> SimpleNamespace:
    """An SDK-shaped final Message: an (omitted-display) thinking block,
    then the text block carrying the JSON."""
    text = output if isinstance(output, str) else json.dumps(output)
    return SimpleNamespace(
        id=msg_id,
        model=model,
        stop_reason=stop_reason,
        content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text=text),
        ],
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    )


def fake_client(*messages: Any) -> MagicMock:
    """A client whose successive ``messages.stream(...)`` calls yield the
    given final messages (an Exception instance is raised instead)."""
    client = MagicMock()
    stream = client.messages.stream.return_value.__enter__.return_value
    stream.get_final_message.side_effect = list(messages)
    return client


def runs(conn: sqlite3.Connection, task_id: str) -> list[tuple]:
    return [
        tuple(r)
        for r in conn.execute(
            "SELECT attempt, status FROM synthesis_runs WHERE task_id = ? ORDER BY attempt",
            (task_id,),
        )
    ]
