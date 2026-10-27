"""Exact context-array accounting and the additive context request router."""
from __future__ import annotations

import json
import math
import re
from dataclasses import asdict

from muninn.search.documents import CONTEXT_ESTIMATOR, ContextEntry, ContextRequest, ContextResult

DIAGNOSTIC_MAX_BYTES = 4096


def context_json(entries) -> bytes:
    return json.dumps([asdict(e) if isinstance(e, ContextEntry) else e for e in entries],
                      ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def estimate_entry_cost(entry) -> int:
    value = asdict(entry) if isinstance(entry, ContextEntry) else entry
    return max(1, math.ceil(len(json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")) / 4))


def finalize_context(entries, request: ContextRequest, *, backend, generation=None,
                     accounting=None, coverage=None, state="ok", reason=None,
                     fallback_reason=None, omissions=(), warnings=()) -> ContextResult:
    entries = tuple(e if isinstance(e, ContextEntry) else ContextEntry(**e) for e in entries)
    size = len(context_json(entries))
    cost = sum(estimate_entry_cost(e) for e in entries)
    if size > request.max_bytes or cost > request.budget:
        entries, size, cost, state, reason = (), 2, 0, "abstained", "infeasible_budget"
    accounting = {**(accounting or {}), "unit": "estimated_text_cost", "estimator": CONTEXT_ESTIMATOR,
                  "delivered": cost, "budget": request.budget}
    def safe_reason(value):
        return value if value is None or isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", value) else "backend_unavailable"
    reason, fallback_reason = safe_reason(reason), safe_reason(fallback_reason)
    result = ContextResult(entries, backend, generation, size, accounting, coverage or {},
                           state, reason, fallback_reason, tuple(omissions), tuple(warnings))
    diagnostics = result.to_dict()
    diagnostics.pop("context")
    if len(json.dumps(diagnostics, ensure_ascii=False, separators=(",", ":")).encode()) > DIAGNOSTIC_MAX_BYTES:
        result = ContextResult(entries, backend, generation, size, accounting,
                               {"diagnostics_truncated": True}, state, reason, fallback_reason,
                               (), ("diagnostics_truncated",))
        reduced = result.to_dict()
        reduced.pop("context")
        if len(json.dumps(reduced, ensure_ascii=False, separators=(",", ":")).encode()) > DIAGNOSTIC_MAX_BYTES:
            result = ContextResult(entries, backend, None, size,
                                   {"unit": "estimated_text_cost", "estimator": CONTEXT_ESTIMATOR,
                                    "delivered": cost, "budget": request.budget},
                                   {"diagnostics_truncated": True}, state, reason, fallback_reason,
                                   (), ("diagnostics_truncated",))
    return result


def build_context(query, namespaces=None, budget=2500, max_bytes=16384, anchors=(),
                  required_sources=(), expected_generation=None, *, optional_roots=12,
                  db_path=None, backend=None):
    if isinstance(namespaces, str) or isinstance(anchors, str) or isinstance(required_sources, str):
        raise ValueError("invalid_context_sequence")
    request = ContextRequest(query, tuple(namespaces) if namespaces is not None else ContextRequest.__dataclass_fields__["namespaces"].default,
                             budget, max_bytes, tuple(anchors), tuple(required_sources), expected_generation, optional_roots)
    from muninn.search.routing import route_request
    from muninn.search.context_braid import BraidContextBackend
    from muninn.search.context_fallback import legacy_context

    primary = backend or BraidContextBackend(db_path=db_path)
    def fallback(reason):
        if expected_generation is not None:
            return finalize_context((), request, backend="unavailable", state="unavailable",
                                    reason="expected_generation_unavailable", fallback_reason=reason)
        return legacy_context(request, db_path=db_path, reason=reason)
    return route_request(lambda: primary.build(request), fallback)
