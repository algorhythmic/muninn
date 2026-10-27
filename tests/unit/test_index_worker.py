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
    result = worker.run_once(database, namespaces=("bookmarks",), build=ready_builder)
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
    assert worker.run_once(database, namespaces=("bookmarks",), build=forbidden).state is None


def test_new_mutation_during_build_is_one_pending_epoch(database):
    mutate(database)
    def build(path, **kwargs):
        manifest = ready_builder(path)
        mutate(path, 'later')
        mutate(path, 'newest')
        return manifest
    result = worker.run_once(database, namespaces=("bookmarks",), build=build)
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
    result = worker.run_once(database, namespaces=("bookmarks",), build=fail)
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
    result = worker.run_once(database, namespaces=("bookmarks",), build=verify)
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
            worker.run_once(database, namespaces=("bookmarks",), build=ready_builder)
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
    result = worker.run_once(database, namespaces=("bookmarks",), build=build)
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


@pytest.fixture
def authored(database, tmp_path, monkeypatch):
    vault = tmp_path / 'vault'
    page = vault / 'wiki/concepts/synthetic.md'
    page.parent.mkdir(parents=True)
    page.write_text('# Synthetic\n\nA synthetic authored body.')
    monkeypatch.setenv('MUNINN_VAULT_DIR', str(vault))
    monkeypatch.delenv('MUNINN_PERSONAL_VAULT_DIR', raising=False)
    return page


def observation(path):
    conn = connect(path)
    try:
        return worker.authored_status(conn)
    finally:
        conn.close()


def authored_builder(path, **kwargs):
    import uuid
    from muninn.index.publisher import _inventory
    conn = connect(path)
    scope = kwargs['namespaces']
    digest = _inventory(conn, scope).digest
    name = 'synthetic-' + uuid.uuid4().hex
    registry.allocate_generation(conn, name, scope)
    registry.mark_ready(conn, name, '/synthetic/manifest.json', 'a' * 64)
    epoch = registry.source_epoch(conn)
    conn.close()
    return SimpleNamespace(generation_id=name, source_epoch=epoch, authored_inventory_digest=digest)


def test_first_authored_observation_builds_once_then_idle(database, authored):
    result = worker.run_once(database, namespaces=('concepts',), build=authored_builder)
    assert result.state == 'ready'
    assert observation(database).inventory_digest is not None
    assert observation(database).inventory_failure_category is None
    identity = result.job_id
    again = worker.run_once(database, build=authored_builder)
    assert again.job_id == identity
    assert again.pending_epoch is None


def test_authored_edit_without_epoch_coalesces_new_build(database, authored):
    before = worker.run_once(database, namespaces=('concepts',), build=authored_builder)
    conn = connect(database)
    epoch = registry.source_epoch(conn)
    conn.close()
    before_observation = observation(database)
    authored.write_text('# Synthetic\n\nChanged authored body.')
    after = worker.run_once(database, build=authored_builder)
    assert after.state == 'ready'
    assert after.job_id != before.job_id
    assert observation(database).inventory_digest != before_observation.inventory_digest
    conn = connect(database)
    assert registry.source_epoch(conn) == epoch
    assert registry.active_generation(conn) is None
    conn.close()


def test_authored_edit_during_inference_retains_pending(database, authored):
    def changing(path, **kwargs):
        manifest = authored_builder(path, **kwargs)
        authored.write_text('# Synthetic\n\nEdited during fake inference.')
        return manifest
    result = worker.run_once(database, namespaces=('concepts',), build=changing)
    assert result.state == 'stale'
    assert result.pending_epoch == 0


def test_missing_scan_preserves_digest_and_active(database, authored):
    before = worker.run_once(database, namespaces=('concepts',), build=authored_builder)
    conn = connect(database)
    registry.activate(conn, before.generation_id, registry.source_epoch(conn))
    conn.close()
    before_observation = observation(database)
    authored.parent.parent.rename(authored.parent.parent.with_name('unavailable'))
    after = worker.run_once(database, build=authored_builder)
    assert after.state == 'stale'
    assert observation(database).inventory_digest == before_observation.inventory_digest
    assert observation(database).inventory_failure_category == 'authored_inventory_unavailable'
    assert after.pending_epoch == 0
    conn = connect(database)
    assert registry.active_generation(conn).id == before.generation_id
    conn.close()


def test_failed_postbuild_scan_does_not_erase_complete_digest(database, authored):
    def vanished(path, **kwargs):
        manifest = authored_builder(path, **kwargs)
        authored.parent.parent.rename(authored.parent.parent.with_name('unavailable'))
        return manifest
    result = worker.run_once(database, namespaces=('concepts',), build=vanished)
    assert result.state == 'stale'
    assert observation(database).inventory_digest is not None
    assert observation(database).inventory_failure_category == 'authored_inventory_unavailable'
    assert result.pending_epoch == 0


def test_explicit_bookmark_scope_does_not_scan_authored(database, monkeypatch):
    from muninn.index import publisher
    def forbidden(*args):
        raise AssertionError('bookmarks scope must not scan vault')
    monkeypatch.setattr(publisher, '_inventory', forbidden)
    result = worker.run_once(database, namespaces=('bookmarks',), build=ready_builder)
    assert result.state is None
    assert observation(database).inventory_digest is None


def test_incomplete_scan_keeps_last_complete_observation(database, authored, monkeypatch):
    from muninn.index import publisher
    from muninn.index.authored import Inventory
    worker.run_once(database, namespaces=('concepts',), build=authored_builder)
    before = observation(database)
    monkeypatch.setattr(publisher, '_inventory', lambda *_: Inventory((), (), (('wiki', 'max_files'),), False))
    result = worker.run_once(database, build=authored_builder)
    assert result.state == 'stale'
    assert observation(database).inventory_digest == before.inventory_digest
    assert observation(database).inventory_observed_at == before.inventory_observed_at
    assert observation(database).inventory_failure_category == 'authored_inventory_unavailable'


def test_verified_ready_manifest_seeds_observation_without_rebuild(tmp_path, monkeypatch):
    from tests.unit.test_index_publisher import FakeTokenizer, FakeBraid
    from muninn.index.embedding_profile import EmbeddingProfile
    from muninn.index.publisher import build_generation
    monkeypatch.setenv('MUNINN_DATA_DIR', str(tmp_path / 'data'))
    vault = tmp_path / 'vault'
    (vault / 'wiki/concepts').mkdir(parents=True)
    (vault / 'wiki/concepts/a.md').write_text('# Synthetic\n\nA fake authored note.')
    monkeypatch.setenv('MUNINN_VAULT_DIR', str(vault))
    monkeypatch.delenv('MUNINN_PERSONAL_VAULT_DIR', raising=False)
    path = tmp_path / 'synthetic.db'
    init_db(path).close()
    profile = EmbeddingProfile('synthetic-model', 'a' * 40, 2, 'query-digest', 'document-digest',
                               'synthetic-tokenizer', (('fake-runtime', '1'),))
    manifest = build_generation(path, namespaces=('concepts',), profile=profile, tokenizer=FakeTokenizer(),
                                client_factory=FakeBraid,
                                encoder=lambda texts, profile, **kwargs: ([0.6, 0.8] for _ in texts))
    def forbidden(*args, **kwargs):
        raise AssertionError('verified unchanged ready manifest must not rebuild')
    result = worker.run_once(path, build=forbidden)
    assert result.state == 'ready'
    assert result.generation_id == manifest.generation_id
    assert result.pending_epoch is None
    assert observation(path).inventory_digest == manifest.authored_inventory_digest


def test_missing_initial_authored_scan_never_builds_empty_corpus(database, monkeypatch):
    monkeypatch.delenv('MUNINN_VAULT_DIR', raising=False)
    def forbidden(*args, **kwargs):
        raise AssertionError('missing inventory cannot build empty corpus')
    result = worker.run_once(database, namespaces=('concepts',), build=forbidden)
    assert result.state == 'failed'
    assert result.failure_category == 'authored_inventory_unavailable'
    assert result.pending_epoch == 0
    assert observation(database).inventory_digest is None
