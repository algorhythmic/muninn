"""The default synthesis runner: one budgeted, streamed Messages API call.

It runs a task file exactly as any other harness would: the task's
``instructions`` are the system prompt, and the rest of the task object
(materials, result schema, correction instructions) is the user message.
The reply must be the result's ``output`` object.

* **Model**: ``--model``, else ``MUNINN_SYNTHESIS_MODEL``, else
  ``claude-opus-5-5`` (ADR-013). It must have a price in
  ``muninn.enrich.pricing.PRICES``, or its spend could not be bounded.
* **Thinking and effort**: adaptive thinking, and ``output_config.effort``
  from ``MUNINN_SYNTHESIS_EFFORT`` (default ``high``; Opus 5.5's own default
  is ``medium``). Thinking tokens are output tokens, so ``max_tokens``
  bounds them too.
* **Streaming**: ``client.messages.stream(...)`` + ``get_final_message()``,
  so a long output does not hit the SDK's non-streaming timeout.
* **Worst case** (checked before the call): the request's characters at
  ``CHARS_PER_TOKEN_BOUND`` chars per token, priced at the dearer of the
  input and cache-write rates, plus ``max_tokens`` of output.
* **Cost** (after the call): priced from the response's measured ``usage``;
  a response without usable usage is charged the worst case.

Server-side refusal fallbacks are deliberately not enabled: a fallback
could be served by a model whose price the pre-call bound did not cover.
A refusal is a failed run. The Message Batches API (50% off) is future
work; see docs/synthesis-contract.md.
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from muninn.config import (
    SYNTHESIS_DEFAULT_EFFORT,
    SYNTHESIS_DEFAULT_MODEL,
    SYNTHESIS_EFFORT_ENV,
    SYNTHESIS_MODEL_ENV,
)
from muninn.enrich.haiku import _parse_json_response
from muninn.enrich.pricing import (
    UnknownModelPrice,
    cost_micro_usd,
    price_for,
    worst_case_micro_usd,
)
from muninn.synthesis.budget import SynthesisConfigError

if TYPE_CHECKING:  # pragma: no cover
    import anthropic

log = logging.getLogger(__name__)

MAX_OUTPUT_TOKENS = 32_000
# Conservative chars-per-token for the pre-call bound. Materials are JSON
# (ids, timestamps, punctuation), which tokenizes denser than prose, so
# this is tighter than suitability's 3.
CHARS_PER_TOKEN_BOUND = 2
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

USER_PREAMBLE = (
    "Run this muninn synthesis task. Reply with the result object only: one JSON "
    "object that validates against result_schema.\n\n"
)


def default_model() -> str:
    return os.environ.get(SYNTHESIS_MODEL_ENV) or SYNTHESIS_DEFAULT_MODEL


def default_effort() -> str:
    return os.environ.get(SYNTHESIS_EFFORT_ENV) or SYNTHESIS_DEFAULT_EFFORT


@dataclass
class RunnerResult:
    """One attempt's outcome, from any runner.

    ``error`` set: the runner produced no output (API error, refusal) →
    ``container_failed``. ``parse_errors`` set: it produced output that is
    not a JSON object (or was truncated) → ``validation_failed``.
    ``cost_micro_usd`` is None for a runner that spends nothing muninn
    accounts for.
    """

    output_json: dict[str, Any] | None = None
    error: str | None = None
    parse_errors: list[str] = field(default_factory=list)
    runner_ref: str | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    usage: dict[str, int] | None = None
    cost_micro_usd: int | None = None


def task_message(task: dict[str, Any]) -> str:
    body = {k: v for k, v in task.items() if k != "instructions"}
    return USER_PREAMBLE + json.dumps(body, ensure_ascii=False)


def _int(value: Any) -> int:
    return value if isinstance(value, int) and value >= 0 else 0


def _measured_usage(message: Any) -> dict[str, int] | None:
    usage = getattr(message, "usage", None)
    if usage is None or not isinstance(getattr(usage, "input_tokens", None), int):
        return None
    return {
        "uncached_input_tokens": _int(usage.input_tokens),
        "cache_write_tokens": _int(getattr(usage, "cache_creation_input_tokens", 0)),
        "cache_read_tokens": _int(getattr(usage, "cache_read_input_tokens", 0)),
        "output_tokens": _int(getattr(usage, "output_tokens", 0)),
    }


def _text(message: Any) -> str:
    return "".join(
        getattr(block, "text", "") or ""
        for block in (getattr(message, "content", None) or [])
        if getattr(block, "type", None) == "text"
    )


class ApiRunner:
    """Budgeted direct Anthropic API runner. ``client`` is injectable for
    tests; otherwise one is built on the first call."""

    name = "anthropic-api"

    def __init__(
        self,
        client: "anthropic.Anthropic | None" = None,
        *,
        model: str | None = None,
        effort: str | None = None,
        max_output_tokens: int = MAX_OUTPUT_TOKENS,
    ) -> None:
        self.model = model or default_model()
        try:
            self.price = price_for(self.model)
        except UnknownModelPrice as exc:
            raise SynthesisConfigError(str(exc.args[0])) from exc
        self.effort = effort or default_effort()
        if self.effort not in EFFORT_LEVELS:
            raise SynthesisConfigError(
                f"synthesis effort {self.effort!r} is not one of {', '.join(EFFORT_LEVELS)}"
            )
        self.max_output_tokens = max_output_tokens
        self._client = client

    def _get_client(self) -> "anthropic.Anthropic":
        if self._client is None:
            import anthropic  # lazy: emit/ingest never need the SDK

            self._client = anthropic.Anthropic()
        return self._client

    def estimate(self, task: dict[str, Any]) -> int:
        chars = len(task["instructions"]) + len(task_message(task))
        tokens = math.ceil(chars / CHARS_PER_TOKEN_BOUND)
        return worst_case_micro_usd(self.price, input_tokens=tokens, max_output_tokens=self.max_output_tokens)

    def run(self, task: dict[str, Any]) -> RunnerResult:
        client = self._get_client()
        try:
            with client.messages.stream(
                model=self.model,
                max_tokens=self.max_output_tokens,
                system=task["instructions"],
                messages=[{"role": "user", "content": task_message(task)}],
                thinking={"type": "adaptive"},
                output_config={"effort": self.effort},
            ) as stream:
                message = stream.get_final_message()
        except Exception as exc:  # noqa: BLE001 - an API failure is a recorded run, not a crash
            log.error("synthesis API call failed for %s: %s", task["task_id"], type(exc).__name__)
            return RunnerResult(error=f"API call failed: {type(exc).__name__}: {exc}"[:500], model=self.model)

        served_model = getattr(message, "model", None) or self.model
        usage = _measured_usage(message)
        if usage is None:
            cost = self.estimate(task)  # unmeasurable: charge the bound
        else:
            cost = cost_micro_usd(
                self.price,
                input_tokens=usage["uncached_input_tokens"],
                cache_write_tokens=usage["cache_write_tokens"],
                cache_read_tokens=usage["cache_read_tokens"],
                output_tokens=usage["output_tokens"],
            )
        result = RunnerResult(
            runner_ref=f"{self.name}:{getattr(message, 'id', None) or 'unknown'}",
            model=served_model,
            usage=usage,
            cost_micro_usd=cost,
        )
        if usage is not None:
            result.input_tokens = (
                usage["uncached_input_tokens"] + usage["cache_write_tokens"] + usage["cache_read_tokens"]
            )
            result.output_tokens = usage["output_tokens"]

        stop = getattr(message, "stop_reason", None)
        if stop == "refusal":
            result.error = "model refused the task (stop_reason=refusal)"
            return result
        if stop == "max_tokens":
            result.parse_errors = [
                f"output truncated at max_tokens={self.max_output_tokens}; emit a shorter result"
            ]
            return result
        try:
            data = _parse_json_response(_text(message))
        except ValueError:
            result.parse_errors = ["(root): output is not a JSON object"]
            return result
        if not isinstance(data, dict):
            result.parse_errors = ["(root): output is not a JSON object"]
            return result
        _stamp_metadata(data, task, result)
        result.output_json = data
        return result


def _stamp_metadata(output: dict[str, Any], task: dict[str, Any], result: RunnerResult) -> None:
    """Overwrite ``synthesis_metadata`` with what the runner measured: the
    served model, the task's prompt version and the measured token counts.
    The model cannot know its own token counts."""
    meta = output.get("synthesis_metadata")
    if not isinstance(meta, dict):
        meta = {}
    meta["model"] = result.model
    meta["prompt_version"] = task["prompt_version"]
    meta["input_token_count"] = result.input_tokens or 0
    meta["output_token_count"] = result.output_tokens or 0
    if task["task_type"] == "era_narrative":
        meta.setdefault("neighboring_eras_used", [])
    output["synthesis_metadata"] = meta
