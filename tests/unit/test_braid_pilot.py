"""Synthetic fake Braid pilot contracts; no model downloads or live corpus."""
import hashlib
import json
import threading

import pytest

from muninn.index.braid_client import BraidError
from muninn.index.pilot import load_pilot_pin, open_pilot
from tests.unit.test_index_runtime import setup as runtime_setup
from tests.unit.test_index_publisher import setup


def pin(tmp_path, monkeypatch, *, enabled=True):
    binary = tmp_path / "fake-braid"
    binary.write_bytes(b"synthetic executable identity; never executed")
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    value = {"schema": "muninn-braid-pilot/1", "enabled": enabled,
             "binary_path": str(binary), "binary_sha256": digest, "source_commit": "a" * 40}
    (tmp_path / "fake-braid.provenance.json").write_text(json.dumps({"binary_sha256": digest, "source_commit": "a" * 40}))
    path = tmp_path / "pilot.json"
    path.write_text(json.dumps(value))
    monkeypatch.setenv("MUNINN_BRAID_PILOT_CONFIG", str(path))
    return path, value


def test_default_off_and_lazy_stub_start(tmp_path, monkeypatch):
    pool, options, opened = runtime_setup(tmp_path)
    monkeypatch.delenv("MUNINN_BRAID_PILOT_CONFIG", raising=False)
    with pytest.raises(BraidError, match="pilot_disabled"):
        open_pilot(pool, **options)
    assert opened == []
    path, value = pin(tmp_path, monkeypatch)
    assert opened == []  # Parsing config never starts a process.
    # Low-level fake pool has no canonical watcher; intercept only watcher flag.
    class Runtime:
        def acquire(self, **kwargs):
            kwargs["watch_active"] = False
            return pool.acquire(**kwargs)
    lease = open_pilot(Runtime(), **options)
    assert len(opened) == 1
    lease.close()
    value["enabled"] = False
    path.write_text(json.dumps(value))
    with pytest.raises(BraidError, match="pilot_disabled"):
        open_pilot(pool, **options)
    assert opened[0].closed
    pool.shutdown()
    assert opened[0].closed


@pytest.mark.parametrize("change,reason", [
    ({"enabled": False}, "pilot_disabled"),
    ({"source_commit": "b" * 40}, "pilot_provenance_mismatch"),
    ({"binary_sha256": "0" * 64}, "pilot_provenance_mismatch"),
    ({"binary_path": "relative"}, "pilot_config_invalid"),
    ({"enabled": "true"}, "pilot_config_invalid"),
])
def test_pin_refuses_mismatch_before_start(tmp_path, monkeypatch, change, reason):
    path, value = pin(tmp_path, monkeypatch)
    value.update(change)
    path.write_text(json.dumps(value))
    with pytest.raises(BraidError, match=reason):
        load_pilot_pin()


def test_changed_binary_refused(tmp_path, monkeypatch):
    _, value = pin(tmp_path, monkeypatch)
    from pathlib import Path
    Path(value["binary_path"]).write_bytes(b"changed")
    with pytest.raises(BraidError, match="binary_identity_mismatch"):
        load_pilot_pin()


def test_idle_timer_and_shutdown_close_fake_client(tmp_path):
    pool, options, opened = runtime_setup(tmp_path, idle_seconds=.01, poll_seconds=.005)
    lease = pool.acquire(**options)
    closed = threading.Event()
    original = opened[0].close
    def close():
        original()
        closed.set()
    opened[0].close = close
    lease.close()
    assert closed.wait(1)
    assert not pool.leased_generations()
    held = pool.acquire(**options)
    pool.shutdown()
    assert opened[-1].closed
    with pytest.raises(BraidError, match="generation_drained"):
        held.request("query")
    held.close()


@pytest.mark.parametrize("reason", ["transport_timeout", "protocol_fault", "executable_unavailable"])
def test_whole_request_fallback_on_pilot_start_or_exchange_error(setup, tmp_path, monkeypatch, reason):
    from tests.unit.test_braid_documents import backend
    from muninn.index.runtime import runtime
    from muninn.search.braid import BraidDocumentBackend
    from muninn.search.documents import DocumentSearchResult
    from muninn.search.routing import route_request
    backend(setup)  # synthetic source, manifest and explicit test activation
    pin(tmp_path, monkeypatch)
    def unavailable(**kwargs):
        raise BraidError(reason)
    monkeypatch.setattr(runtime, "acquire", unavailable)
    engine = BraidDocumentBackend(setup[0])
    result = route_request(lambda: engine.search_documents("synthetic", ("bookmarks",), 5),
                           lambda reason: DocumentSearchResult((), backend="fts5-qdrant"))
    assert result.backend == "fts5-qdrant" and result.fallback_reason == reason


def test_mcp_shutdown_closes_runtime_even_when_server_raises(monkeypatch):
    from muninn.consumers.mcp import server
    from muninn.index.runtime import runtime
    closed = []
    monkeypatch.setattr(runtime, "shutdown", lambda: closed.append(True))
    monkeypatch.setattr(server.mcp, "run", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("synthetic")))
    with pytest.raises(RuntimeError):
        server.main()
    assert closed == [True]
