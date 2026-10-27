"""Synthetic registry jobs and fake builders; no Braid/model process."""
from types import SimpleNamespace

import pytest

from muninn.db import init_db, connect
from muninn.index import registry, worker


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setenv('MUNINN_DATA_DIR', str(tmp_path / 'data'))
    path = tmp_path / 'synthetic.db'
    conn = init_db(path)
    conn.close()
    return path


def mutate(path, title='synthetic'):
    conn = connect(path)
    conn.execute("INSERT INTO bookmarks(bookmark_id,source,source_id,captured_at,title) VALUES(NULL,'synthetic',?,1,?)", (title, title))
    conn.commit()
    conn.close()


def ready_builder(path, **kwargs):
    conn = connect(path)
    with registry.publisher_lease(conn) as lease:
        registry.allocate_generation(conn, 'fake-ready', ('bookmarks',), lease_token=lease.token)
        registry.mark_ready(conn, 'fake-ready', '/synthetic/manifest.json', 'a' * 64, lease_token=lease.token)
    epoch = registry.source_epoch(conn)
    conn.close()
    return SimpleNamespace(generation_id='fake-ready', source_epoch=epoch)


def test_epoch_coalesces_in_source_transaction_and_rollback(database):
    conn = connect(database)
    assert worker.status(conn).state is None
    conn.execute("INSERT INTO bookmarks(bookmark_id,source,source_id,captured_at,title) VALUES(1,'synthetic','1',1,'synthetic')")
    assert worker.status(conn).state == 'pending'
    assert worker.status(conn).pending_epoch == registry.source_epoch(conn)
    conn.rollback()
    assert worker.status(conn).state is None
    conn.close()
    mutate(database, 'first')
    mutate(database, 'second')
    conn = connect(database)
    assert worker.status(conn).pending_epoch == 2
    conn.close()


def test_explicit_request_builds_ready_without_activation(database):
    conn = connect(database)
    first = worker.enqueue(conn)
    second = worker.enqueue(conn)
    assert first.job_id == second.job_id
    conn.close()
    result = worker.run_once(database, build=ready_builder)
    assert result.state == 'ready'
    assert result.pending_epoch is None
    assert result.generation_id == 'fake-ready'
    conn = connect(database)
    assert registry.active_generation(conn) is None
    registry.activate(conn, 'fake-ready', registry.source_epoch(conn))
    assert worker.status(conn).state == 'active'
    conn.close()


def test_idle_does_not_build(database):
    def forbidden(*args, **kwargs):
        raise AssertionError('idle worker must not build')
    assert worker.run_once(database, build=forbidden).state is None


def test_new_mutation_during_build_is_one_pending_epoch(database):
    mutate(database)
    def build(path, **kwargs):
        manifest = ready_builder(path)
        mutate(path, 'later')
        mutate(path, 'newest')
        return manifest
    result = worker.run_once(database, build=build)
    assert result.state == 'stale'
    assert result.pending_epoch == 3
    conn = connect(database)
    assert registry.active_generation(conn) is None
    conn.close()


def test_failure_preserves_active_and_safe_category(database):
    ready_builder(database)
    conn = connect(database)
    registry.activate(conn, 'fake-ready', registry.source_epoch(conn))
    worker.enqueue(conn)
    conn.close()
    def fail(*args, **kwargs):
        raise RuntimeError('SECRET fake query/source text')
    result = worker.run_once(database, build=fail)
    assert result.state == 'failed'
    assert result.failure_category == 'build_failed'
    assert 'SECRET' not in repr(result)
    conn = connect(database)
    assert registry.active_generation(conn).id == 'fake-ready'
    conn.close()


def test_restart_reconciles_staging_before_retry(database):
    conn = connect(database)
    registry.allocate_generation(conn, 'abandoned', ('bookmarks',))
    worker.enqueue(conn)
    conn.execute("UPDATE index_corpus SET publisher_state='building'")
    conn.commit()
    conn.close()
    def verify(path, **kwargs):
        check = connect(path)
        assert registry.get_generation(check, 'abandoned').state == 'failed'
        check.close()
        return ready_builder(path)
    result = worker.run_once(database, build=verify)
    assert result.state == 'ready'


def test_supervisor_validates_poll_without_starting(database):
    with pytest.raises(ValueError):
        worker.supervise(database, poll_seconds=0)
    worker.supervise(database, stop=lambda: True)


def test_busy_owner_does_not_change_job_or_build(database):
    import threading
    conn = connect(database)
    before = worker.enqueue(conn)
    conn.close()
    entered, release = threading.Event(), threading.Event()
    def hold():
        other = connect(database)
        try:
            with registry.publisher_lock(other):
                entered.set()
                release.wait(5)
        finally:
            other.close()
    thread = threading.Thread(target=hold)
    thread.start()
    assert entered.wait(5)
    try:
        with pytest.raises(registry.RegistryError, match='publisher_lease_busy'):
            worker.run_once(database, build=ready_builder)
        check = connect(database)
        assert worker.status(check) == before
        check.close()
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()


def test_explicit_enqueue_while_building_is_not_lost(database):
    conn = connect(database)
    worker.enqueue(conn)
    conn.close()
    def build(path, **kwargs):
        other = connect(path)
        worker.enqueue(other)  # authored inventory can change without an epoch.
        other.close()
        return ready_builder(path)
    result = worker.run_once(database, build=build)
    assert result.state == 'ready'
    assert result.pending_epoch == 0


def test_publisher_hook_preserves_same_epoch_authored_notification(database):
    conn = connect(database)
    registry.allocate_generation(conn, 'synthetic', ('bookmarks',))
    worker.build_started(conn, 'synthetic')
    identity = worker.status(conn).job_id
    worker.enqueue(conn)
    worker.build_started(conn, 'synthetic')
    assert worker.status(conn).pending_epoch == registry.source_epoch(conn)
    assert worker.status(conn).job_id == identity
    worker.build_finished(conn, 'synthetic')
    assert worker.status(conn).state == 'ready'
    assert worker.status(conn).pending_epoch == 0
    conn.close()


def test_explicit_restart_reconciles_old_job_identity(database):
    conn = connect(database)
    registry.allocate_generation(conn, 'abandoned', ('bookmarks',))
    worker.build_started(conn, 'abandoned')
    identity = worker.status(conn).job_id
    with registry.publisher_lock(conn):
        assert worker.reconcile_staging(conn) == 1
        assert worker.status(conn).failure_category == 'interrupted_build'
        registry.allocate_generation(conn, 'replacement', ('bookmarks',))
        worker.build_started(conn, 'replacement')
    assert worker.status(conn).job_id != identity
    assert registry.get_generation(conn, 'abandoned').state == 'failed'
    conn.close()
