"""Synthetic client sessions; no Braid binary, model or service."""
import hashlib
from pathlib import Path
import threading
import time

import pytest

from muninn.index.braid_client import BraidError, REQUIRED_CAPABILITIES
from muninn.index.runtime import BraidRuntime, CONTEXT_CAPABILITIES


class FakeClient:
    def __init__(self, **kwargs):
        self.state = {"id": kwargs["dataset_id"], "revision": 1, "digest": "synthetic"}
        self.identity = {"binary_digest": hashlib.sha256(Path(kwargs["binary"]).read_bytes()).hexdigest(),
                         "protocol_version": 1, "ranking_version": "fake/1"}
        self.capabilities = frozenset(REQUIRED_CAPABILITIES) | CONTEXT_CAPABILITIES
        self.closed = self.poisoned = False
        self.requests = []

    def request(self, method, **payload):
        self.requests.append(method)
        return {"synthetic": True}

    def close(self):
        self.closed = True


def setup(tmp_path, **options):
    binary = tmp_path / "fake-braid"
    binary.write_bytes(b"synthetic executable identity; never executed")
    kwargs = dict(binary=binary, generation_dir=tmp_path / "generation", dataset_id="fake", profile_id="hash")
    opened = []
    def opener(**kw):
        client = FakeClient(**kw)
        opened.append(client)
        return client
    return BraidRuntime(opener=opener, context_opener=opener, **options), kwargs, opened


def test_shared_generation_lease_is_readonly_and_retained_until_closed(tmp_path):
    pool, kwargs, opened = setup(tmp_path)
    first, second = pool.acquire(**kwargs), pool.acquire(**kwargs)
    assert len(opened) == 1
    assert pool.leased_generations() == {"generation"}
    with pytest.raises(BraidError, match="read_only_session"):
        first.request("apply", update={})
    assert first.request("query") == {"synthetic": True}
    first.close()
    assert not opened[0].closed
    assert not pool.close_generation("generation")
    with pytest.raises(BraidError, match="generation_draining"):
        pool.acquire(**kwargs)
    second.close()
    assert opened[0].closed
    assert not pool.leased_generations()


def test_runtime_reconnect_negotiates_and_rejects_binary_replacement(tmp_path):
    pool, kwargs, opened = setup(tmp_path, idle_seconds=.001)
    pool.acquire(**kwargs).close()
    time.sleep(.005)
    pool.acquire(**kwargs).close()
    assert len(opened) == 2 and opened[0].closed
    kwargs["binary"].write_bytes(b"different build")
    with pytest.raises(BraidError, match="binary_identity_mismatch"):
        pool.acquire(**kwargs)


def test_poisoned_connection_closed_and_restart_is_backed_off(tmp_path):
    pool, kwargs, opened = setup(tmp_path)
    lease = pool.acquire(**kwargs)
    opened[0].poisoned = True
    lease.close()
    assert opened[0].closed
    with pytest.raises(BraidError, match="runtime_backoff"):
        pool.acquire(**kwargs)


def test_pool_bound_never_closes_in_use_generation(tmp_path):
    pool, kwargs, opened = setup(tmp_path, max_sessions=1)
    lease = pool.acquire(**kwargs)
    other = {**kwargs, "generation_dir": tmp_path / "other"}
    with pytest.raises(BraidError, match="runtime_busy"):
        pool.acquire(**other)
    assert not opened[0].closed
    lease.close()
    pool.acquire(**other).close()
    assert opened[0].closed


def test_context_uses_separate_readonly_session_and_bounds_drain(tmp_path):
    pool, kwargs, opened = setup(tmp_path, drain_seconds=.001)
    first = pool.acquire(**kwargs)
    context = pool.acquire(**kwargs, context=True)
    assert len(opened) == 2
    assert not pool.close_generation("generation")
    time.sleep(.005)
    with pytest.raises(BraidError, match="generation_drained"):
        context.request("export_context")
    first.close()
    context.close()
    assert all(c.closed for c in opened)
