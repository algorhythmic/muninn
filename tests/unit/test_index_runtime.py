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
    from muninn.index.paths import IndexPaths
    (tmp_path / "generation").mkdir()
    (tmp_path / "other").mkdir()
    kwargs = dict(binary=binary, generation_dir=tmp_path / "generation", dataset_id="fake", profile_id="hash",
                  ownership_paths=IndexPaths(tmp_path / "private-index"))
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


def test_expired_drain_closes_even_a_leaked_proxy(tmp_path):
    pool, kwargs, opened = setup(tmp_path, drain_seconds=.005)
    lease = pool.acquire(**kwargs)
    closed = threading.Event()
    original = opened[0].close
    def close():
        original()
        closed.set()
    opened[0].close = close
    assert not pool.close_generation("generation")
    assert closed.wait(timeout=1)
    assert not pool.leased_generations()
    with pytest.raises(BraidError, match="generation_drained"):
        lease.request("query")
    # Releasing the old proxy cannot remove a newly created session.
    next_lease = pool.acquire(**kwargs)
    lease.close()
    assert pool.leased_generations() == {"generation"}
    next_lease.close()
    pool.close()


def test_absolute_binary_honors_configured_digest(tmp_path, monkeypatch):
    pool, kwargs, opened = setup(tmp_path)
    monkeypatch.setenv("MUNINN_BRAID_BINARY", str(kwargs["binary"]))
    monkeypatch.setenv("MUNINN_BRAID_SHA256", "0" * 64)
    with pytest.raises(BraidError, match="binary_identity_mismatch"):
        pool.acquire(**kwargs)
    assert not opened


def test_runtime_reader_holds_os_ownership_until_verified_close(tmp_path):
    from muninn.index.ownership import generation_lock, GenerationBusy
    pool, kwargs, opened = setup(tmp_path)
    lease = pool.acquire(**kwargs)
    lease.close()  # Idle pool still has an open SQLite client.
    with pytest.raises(GenerationBusy):
        generation_lock("generation", exclusive=True, paths=kwargs["ownership_paths"])
    original = opened[0].close
    def failed_close():
        raise BraidError("process_close_timeout")
    opened[0].close = failed_close
    pool.close_generation("generation")
    with pytest.raises(GenerationBusy):
        generation_lock("generation", exclusive=True, paths=kwargs["ownership_paths"])
    opened[0].close = original
    from types import SimpleNamespace
    opened[0].transport = SimpleNamespace(process=SimpleNamespace(poll=lambda: 0))
    pool.close_generation("generation")
    with generation_lock("generation", exclusive=True, paths=kwargs["ownership_paths"]):
        pass


def test_removed_generation_cannot_be_reopened_after_exclusive_delete(tmp_path):
    pool, kwargs, opened = setup(tmp_path)
    kwargs["generation_dir"].rmdir()
    with pytest.raises(BraidError, match="generation_unavailable"):
        pool.acquire(**kwargs)
    assert not opened


def test_canonical_pointer_change_drains_idle_and_pinned_reader(tmp_path):
    import sqlite3
    pool, kwargs, opened = setup(tmp_path, poll_seconds=.005, drain_seconds=.01)
    db = tmp_path / "canonical.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE index_corpus(singleton INTEGER, active_generation_id TEXT)")
    conn.execute("INSERT INTO index_corpus VALUES(1,'generation')")
    conn.commit()
    lease = pool.acquire(**kwargs, db_path=db, watch_active=True)
    closed = threading.Event()
    original = opened[0].close
    def close():
        original()
        closed.set()
    opened[0].close = close
    # A separate process activates by changing the one canonical pointer.
    import subprocess
    import sys
    subprocess.run([sys.executable, "-c",
        "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); "
        "c.execute(\"UPDATE index_corpus SET active_generation_id='new-generation'\"); c.commit(); c.close()",
        str(db)], check=True, timeout=5)
    assert closed.wait(timeout=1)
    with pytest.raises(BraidError, match="generation_drained"):
        lease.request("query")
    with pytest.raises(BraidError, match="generation_draining"):
        pool.acquire(**kwargs, db_path=db, watch_active=True)
    lease.close()
    conn.close()
    pool.close()
    assert not pool._watcher.is_alive()


def test_failed_pointer_observation_drains_instead_of_extending_lease(tmp_path):
    import sqlite3
    pool, kwargs, opened = setup(tmp_path, poll_seconds=.005, drain_seconds=.01)
    db = tmp_path / "canonical.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE index_corpus(singleton INTEGER, active_generation_id TEXT)")
    conn.execute("INSERT INTO index_corpus VALUES(1,'generation')")
    conn.commit()
    lease = pool.acquire(**kwargs, db_path=db, watch_active=True)
    conn.execute("DROP TABLE index_corpus")
    conn.commit()
    deadline = time.monotonic() + 1
    while not opened[0].closed and time.monotonic() < deadline:
        time.sleep(.005)
    assert opened[0].closed
    lease.close()
    conn.close()
    pool.close()
    assert not pool._watcher.is_alive()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 0, -1, True])
def test_runtime_poll_is_finite_positive(value):
    with pytest.raises(ValueError):
        BraidRuntime(poll_seconds=value)


def test_runtime_close_stops_and_restarts_pointer_watcher(tmp_path):
    import sqlite3
    pool, kwargs, opened = setup(tmp_path, poll_seconds=.005)
    db = tmp_path / "canonical.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE index_corpus(singleton INTEGER, active_generation_id TEXT)")
        conn.execute("INSERT INTO index_corpus VALUES(1,'generation')")
    lease = pool.acquire(**kwargs, db_path=db, watch_active=True)
    lease.close()
    watcher = pool._watcher
    pool.close()
    assert not watcher.is_alive()
    pool.acquire(**kwargs, db_path=db, watch_active=True).close()
    assert pool._watcher is not watcher and pool._watcher.is_alive()
    pool.close()
    assert not pool._watcher.is_alive()
