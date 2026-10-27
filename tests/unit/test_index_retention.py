"""Synthetic filesystem retention with fake Braid and real OS ownership locks."""
import subprocess
import sys

import pytest

from muninn.db import connect
from muninn.index import publisher, registry
from muninn.index.paths import load_index_paths
from tests.unit.test_index_publisher import setup  # synthetic fake embedder/Braid fixture


class ClosedRuntime:
    def close_generation(self, identity):
        return True


def test_cleanup_preserves_active_previous_and_removes_sqlite_sidecars(setup):
    path, _, _, build = setup
    first, second, third = build(), build(), build()
    publisher.activate_generation(first.generation_id, path)
    publisher.activate_generation(second.generation_id, path)
    directory = load_index_paths().generation_dir(third.generation_id)
    (directory / 'braid.db-wal').write_bytes(b'synthetic')
    (directory / 'braid.db-shm').write_bytes(b'synthetic')
    result = publisher.cleanup_generations(path, runtime=ClosedRuntime())
    assert result.removed == (third.generation_id,)
    assert not directory.exists()
    for identity in (first.generation_id, second.generation_id):
        assert load_index_paths().generation_dir(identity).is_dir()
    assert publisher.cleanup_generations(path, runtime=ClosedRuntime()).removed == ()
    conn = connect(path)
    assert registry.get_generation(conn, third.generation_id) is not None
    conn.close()


def test_crossprocess_reader_prevents_cleanup_until_release(setup):
    path, _, _, build = setup
    manifest = build()
    script = """
import sys
from muninn.index.ownership import generation_lock
with generation_lock(sys.argv[1]):
    print('locked', flush=True)
    sys.stdin.readline()
"""
    child = subprocess.Popen([sys.executable, '-c', script, manifest.generation_id],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'locked'
        result = publisher.cleanup_generations(path, runtime=ClosedRuntime())
        assert result.removed == ()
        assert result.skipped == ((manifest.generation_id, 'generation_in_use'),)
    finally:
        child.communicate('\n', timeout=5)
    assert child.returncode == 0
    assert publisher.cleanup_generations(path, runtime=ClosedRuntime()).removed == (manifest.generation_id,)


def test_cleanup_rejects_symlink_escape(setup, tmp_path):
    path, _, _, build = setup
    manifest = build()
    directory = load_index_paths().generation_dir(manifest.generation_id)
    outside = tmp_path / 'canonical'
    directory.rename(outside)
    directory.symlink_to(outside, target_is_directory=True)
    result = publisher.cleanup_generations(path, runtime=ClosedRuntime())
    assert result.skipped == ((manifest.generation_id, 'cleanup_path_unsafe'),)
    assert (outside / 'braid.db').is_file()


@pytest.mark.parametrize("setting", ["MUNINN_VAULT_DIR", "MUNINN_PERSONAL_VAULT_DIR", "MUNINN_DB_PATH", "MUNINN_RAW_DIR"])
def test_cleanup_never_deletes_configured_authored_or_canonical_paths(setup, monkeypatch, setting):
    path, _, _, build = setup
    manifest = build()
    directory = load_index_paths().generation_dir(manifest.generation_id)
    monkeypatch.setenv(setting, str(directory / 'canonical.db') if setting == 'MUNINN_DB_PATH' else str(directory))
    result = publisher.cleanup_generations(path, runtime=ClosedRuntime())
    assert result.skipped == ((manifest.generation_id, 'cleanup_protected_path'),)
    assert directory.is_dir()


def test_cleanup_reconciles_interrupted_staging_and_partial_deletion(setup):
    path, _, _, build = setup
    conn = connect(path)
    registry.allocate_generation(conn, 'interrupted', ('bookmarks',))
    conn.close()
    directory = load_index_paths().generation_dir('interrupted', staging=True)
    directory.mkdir(parents=True)
    (directory / 'braid.db-wal').write_bytes(b'synthetic partial build')
    assert publisher.cleanup_generations(path, runtime=ClosedRuntime()).removed == ('interrupted',)
    # A crash after file unlink but before directory unlink is safe to resume.
    directory.mkdir()
    conn = connect(path)
    conn.execute("UPDATE index_generations SET failure_reason='interrupted_build' WHERE generation_id='interrupted'")
    conn.commit()
    conn.close()
    assert publisher.cleanup_generations(path, runtime=ClosedRuntime()).removed == ('interrupted',)
    assert not directory.exists()


def test_cleanup_limit_and_live_local_session(setup):
    path, _, _, build = setup
    first, second = build(), build()
    class BusyRuntime:
        def close_generation(self, identity):
            return False
    result = publisher.cleanup_generations(path, limit=1, runtime=BusyRuntime())
    assert len(result.skipped) == 1
    assert result.skipped[0][1] == 'generation_in_use'
    assert all(load_index_paths().generation_dir(item.generation_id).is_dir() for item in (first, second))
    with pytest.raises(ValueError):
        publisher.cleanup_generations(path, limit=0, runtime=ClosedRuntime())


def test_failed_local_client_close_keeps_os_lease_until_verified_exit(setup, tmp_path):
    from muninn.index.braid_client import BraidError
    from muninn.index.runtime import BraidRuntime
    from tests.unit.test_index_runtime import FakeClient
    path, _, _, build = setup
    manifest = build()
    binary = tmp_path / 'synthetic-binary'
    binary.write_bytes(b'never executed: synthetic identity only')
    clients = []
    def opener(**kwargs):
        client = FakeClient(**kwargs)
        clients.append(client)
        return client
    runtime = BraidRuntime(opener=opener)
    directory = load_index_paths().generation_dir(manifest.generation_id)
    runtime.acquire(binary=binary, generation_dir=directory, dataset_id=manifest.dataset_id,
                    profile_id=manifest.profile_id).close()
    def failed_close():
        raise BraidError('synthetic_close_failure')
    clients[0].close = failed_close
    result = publisher.cleanup_generations(path, runtime=runtime)
    assert result.removed == ()
    assert result.skipped == ((manifest.generation_id, 'generation_in_use'),)
    assert directory.exists()
    # A retry may be a no-op after BraidClient marked itself closed. Require
    # explicit child-exit evidence rather than interpreting that as success.
    from types import SimpleNamespace
    clients[0].transport = SimpleNamespace(process=SimpleNamespace(poll=lambda: 0))
    clients[0].close = lambda: setattr(clients[0], 'closed', True)
    assert publisher.cleanup_generations(path, runtime=runtime).removed == (manifest.generation_id,)
    assert clients[0].closed
    runtime.close()


def test_publisher_passes_generation_ownership_to_default_child_factory(setup, monkeypatch):
    import os
    from muninn.index.braid_client import BraidClient
    from muninn.index.ownership import generation_lock, GenerationBusy
    from tests.unit.test_index_publisher import FakeBraid
    path, _, _, build = setup
    def factory(**kwargs):
        descriptor = kwargs.pop("ownership_fd")
        os.fstat(descriptor)
        identity = kwargs["generation_dir"].name
        client = FakeBraid(**kwargs)
        original = client.finish
        def finish():
            with pytest.raises(GenerationBusy):
                generation_lock(identity, exclusive=True)
            return original()
        client.finish = finish
        return client
    monkeypatch.setattr(BraidClient, "create", factory)
    manifest = build(client_factory=None)
    with generation_lock(manifest.generation_id, exclusive=True):
        pass
