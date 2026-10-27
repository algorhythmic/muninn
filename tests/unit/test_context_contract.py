"""Bounded context contract with synthetic Unicode and injected fake backends."""
import json
from dataclasses import replace

import pytest

from muninn.search.context import context_json, estimate_entry_cost, finalize_context, build_context, DIAGNOSTIC_MAX_BYTES
from muninn.search.documents import ContextEntry, ContextRequest, DocumentBackendUnavailable


@pytest.mark.parametrize("change", [{"budget": -1}, {"budget": True}, {"budget": 10001},
    {"max_bytes": 1}, {"max_bytes": 65537}, {"optional_roots": 13}, {"namespaces": ()},
    {"anchors": ("/private/file",)}, {"required_sources": ("vault:wiki/concepts/../x.md",)},
    {"required_sources": ("vault:wiki/bookmarks/compiled.md",)}, {"query": " "}])
def test_request_limits_and_trusted_reference_shapes(change):
    with pytest.raises(ValueError):
        ContextRequest(**{"query": "synthetic", **change})


def test_exact_unicode_array_bytes_and_whole_result_refusal():
    entry = ContextEntry("synthetic", "context_excerpt", "Café 🦊 <quoted>\n", {"citation": "synthetic"})
    raw = context_json([entry])
    request = ContextRequest("synthetic", max_bytes=len(raw), budget=10000)
    result = finalize_context([entry], request, backend="fake")
    assert result.context_bytes == len(raw)
    assert result.accounting["delivered"] == estimate_entry_cost(entry)
    too_small = finalize_context([entry], replace(request, max_bytes=len(raw)-1), backend="fake")
    assert too_small.context == () and too_small.state == "abstained"
    assert too_small.context_bytes == 2


def test_diagnostics_have_an_independent_hard_cap():
    result = finalize_context([], ContextRequest("synthetic"), backend="fake",
        generation={"oversized": "x" * 20000}, accounting={"oversized": "x" * 20000},
        reason="private prose " * 10000, omissions=({"large": "x" * 20000},))
    diagnostics = result.to_dict()
    diagnostics.pop("context")
    assert len(json.dumps(diagnostics, ensure_ascii=False, separators=(",", ":")).encode()) <= DIAGNOSTIC_MAX_BYTES
    assert result.context_bytes == 2
    assert result.reason == "backend_unavailable"


def test_expected_generation_never_falls_back(monkeypatch):
    class Missing:
        def build(self, request):
            raise DocumentBackendUnavailable("generation_unavailable")
    import muninn.search.context_fallback as fallback
    monkeypatch.setattr(fallback, "legacy_context", lambda *a, **kw: pytest.fail("must not fall back"))
    result = build_context("synthetic", expected_generation="synthetic-generation", backend=Missing())
    assert result.state == "unavailable"


def test_mcp_context_serializes_the_counted_array_and_wrapper(monkeypatch):
    from muninn.consumers.mcp import tools, server
    entry = ContextEntry("synthetic", "context_excerpt", "🦊", {})
    class Fake:
        def build(self, request):
            return finalize_context([entry], request, backend="fake")
    result = json.loads(tools.search_context("synthetic", context_backend=Fake()))
    assert len(json.dumps(result["context"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()) == result["context_bytes"]
    assert json.loads(tools.search_context("", context_backend=Fake()))["code"] == "invalid_context_request"
    monkeypatch.setattr(tools, "search_context", lambda *args: "synthetic wrapper result")
    assert server.search_context("synthetic") == "synthetic wrapper result"
