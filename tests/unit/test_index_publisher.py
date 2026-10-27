"""Synthetic staged publication with a fake Braid and fake semantic encoder."""

from dataclasses import asdict, replace
import json
from pathlib import Path
import sqlite3

import pytest

from muninn.db import connect, init_db
from muninn.index import publisher, registry
from muninn.index.embedding_profile import EmbeddingProfile
from muninn.index.manifest import Manifest, atomic_write
from muninn.index.paths import load_index_paths


class FakeTokenizer:
    digest = "synthetic-tokenizer"
    max_seq_length = 768

    def count_tokens(self, text):
        return len(text) + 2


class FakeBraid:
    def __init__(self, *, generation_dir, dataset_id, profile_id, binary):
        self.directory = generation_dir
        self.identity = {"binary": "synthetic-fake", "revision": "fake-revision"}
        self.dataset_id = dataset_id
        self.profile_id = profile_id
        self.nodes, self.vectors, self.edges = [], [], []
        self.closed = False

    def publish_nodes(self, values):
        self.nodes.extend(values)

    def publish_embeddings(self, values):
        self.vectors.extend(values)

    def publish_edges(self, values):
        self.edges.extend(values)

    def finish(self):
        conn = sqlite3.connect(self.directory / "braid.db")
        conn.execute("CREATE TABLE synthetic_nodes(id TEXT)")
        conn.executemany("INSERT INTO synthetic_nodes VALUES(?)", [(n["id"],) for n in self.nodes])
        conn.commit()
        conn.close()
        self.closed = True
        return {"dataset_id": self.dataset_id, "revision": 3}

    def close(self):
        self.closed = True


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("MUNINN_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("MUNINN_VAULT_DIR", raising=False)
    monkeypatch.delenv("MUNINN_PERSONAL_VAULT_DIR", raising=False)
    path = tmp_path / "synthetic.db"
    conn = init_db(path)
    conn.execute("INSERT INTO bookmarks(bookmark_id,source,source_id,captured_at,title,enrichment_source) VALUES(1,'synthetic','one',1,'Synthetic title','live_fallback')")
    conn.execute("INSERT INTO scrape_results(bookmark_id,pass,fetched_at,scrape_status,content_text) VALUES(1,'live',1,'ok','Synthetic body paragraph.')")
    conn.commit()
    conn.close()
    profile = EmbeddingProfile("synthetic-model", "a" * 40, 2, "query-digest", "document-digest",
                               "synthetic-tokenizer", (("fake-runtime", "1"),))
    clients = []
    publisher_connections = []
    real_connect = publisher.connect

    def tracked_connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        publisher_connections.append(connection)
        return connection

    monkeypatch.setattr(publisher, "connect", tracked_connect)

    def factory(**kwargs):
        client = FakeBraid(**kwargs)
        clients.append(client)
        return client

    def encoder(texts, profile, **kwargs):
        for text in texts:
            assert text
            # The canonical read snapshot must have ended before inference.
            assert not publisher_connections[-1].in_transaction
            other = connect(path)
            other.execute("BEGIN IMMEDIATE")
            other.rollback()
            other.close()
            yield [0.6, 0.8]

    def build(**kwargs):
        options = dict(namespaces=("bookmarks",), profile=profile, tokenizer=FakeTokenizer(),
                       client_factory=factory, encoder=encoder)
        options.update(kwargs)
        return publisher.build_generation(path, **options)

    return path, profile, clients, build


def test_build_ready_without_implicit_activation_then_explicit_switch(setup):
    path, profile, clients, build = setup
    manifest = build()
    conn = connect(path)
    assert registry.active_generation(conn) is None
    generation = registry.generation(conn, manifest.generation_id)
    assert generation.state == "ready"
    assert generation.manifest_digest == manifest.digest
    assert manifest.source_count == 1
    assert manifest.chunk_count == 1
    assert manifest.profile_id == profile.profile_id
    assert manifest.contract_revision == 1
    client = clients[0]
    assert len(client.nodes) == 4
    assert len(client.vectors) == 3
    assert len(client.edges) == 3
    assert {v["model"] for v in client.vectors} == {profile.profile_id}
    assert all(v["vec"] == [0.6, 0.8] for v in client.vectors)
    assert client.closed
    assert Path(generation.manifest_path).is_relative_to(load_index_paths().root)
    activated = publisher.activate_generation(manifest.generation_id, path)
    assert activated == manifest
    assert activated.digest == manifest.digest
    assert activated.profile() == manifest.profile()
    assert registry.active_generation(conn).id == manifest.generation_id
    conn.close()


def test_canonical_change_rejects_activation_and_preserves_prior_active(setup):
    path, _, _, build = setup
    first = build()
    publisher.activate_generation(first.generation_id, path)
    second = build()
    conn = connect(path)
    conn.execute("UPDATE bookmarks SET content_visible=0")
    conn.commit()
    with pytest.raises(registry.RegistryError, match="source_epoch_changed"):
        publisher.activate_generation(second.generation_id, path)
    assert registry.active_generation(conn).id == first.generation_id
    conn.close()


def test_authored_change_rejects_activation(setup, tmp_path, monkeypatch):
    path, _, _, build = setup
    vault = tmp_path / "vault"
    (vault / "wiki" / "concepts").mkdir(parents=True)
    note = vault / "wiki" / "concepts" / "synthetic.md"
    note.write_text("# Synthetic\n\nFirst content.\n")
    monkeypatch.setenv("MUNINN_VAULT_DIR", str(vault))
    manifest = build(namespaces=("concepts",))
    assert manifest.authored_inventory_digest
    note.write_text("# Synthetic\n\nChanged content.\n")
    with pytest.raises(publisher.PublishError, match="authored_inventory_changed"):
        publisher.activate_generation(manifest.generation_id, path)


def test_incomplete_authored_scan_abandons_stage(setup, tmp_path, monkeypatch):
    path, _, _, build = setup
    vault = tmp_path / "vault"
    (vault / "wiki").mkdir(parents=True)
    monkeypatch.setenv("MUNINN_VAULT_DIR", str(vault))
    from muninn.index.authored import Inventory
    monkeypatch.setattr(publisher, "inventory_authored", lambda *a, **k: Inventory((), (), (("wiki/concepts", "scan_failed"),), False))
    with pytest.raises(publisher.PublishError, match="inventory_incomplete"):
        build(namespaces=("concepts",))
    conn = connect(path)
    assert conn.execute("SELECT state FROM index_generations").fetchone()[0] == "failed"
    assert registry.active_generation(conn) is None
    conn.close()


def test_unknown_client_mutation_failure_preserves_active_and_marks_failed(setup):
    path, _, clients, build = setup
    first = build()
    publisher.activate_generation(first.generation_id, path)

    class BrokenBraid(FakeBraid):
        def publish_embeddings(self, values):
            super().publish_embeddings(values)
            raise RuntimeError("synthetic unknown mutation outcome")

    def factory(**kwargs):
        client = BrokenBraid(**kwargs)
        clients.append(client)
        return client

    with pytest.raises(RuntimeError, match="unknown mutation"):
        build(client_factory=factory)
    conn = connect(path)
    assert registry.active_generation(conn).id == first.generation_id
    assert [row[0] for row in conn.execute("SELECT state FROM index_generations ORDER BY rowid")] == ["ready", "failed"]
    assert clients[-1].closed
    assert not list(load_index_paths().staging_dir.glob("*/manifest.json"))
    conn.close()


def test_spool_bound_fails_before_client_creation(setup):
    path, _, clients, build = setup
    with pytest.raises(publisher.PublishError, match="spool_limit"):
        build(max_spool_bytes=1)
    assert not clients
    conn = connect(path)
    assert conn.execute("SELECT state FROM index_generations").fetchone()[0] == "failed"
    conn.close()


@pytest.mark.parametrize("count", [0, 4])
def test_encoder_count_mismatch_fails_without_ready_generation(setup, count):
    path, _, _, build = setup

    def encoder(*args, **kwargs):
        yield from [[0.6, 0.8]] * count

    with pytest.raises(publisher.PublishError, match="encoder_result_count"):
        build(encoder=encoder)
    conn = connect(path)
    assert conn.execute("SELECT state FROM index_generations").fetchone()[0] == "failed"
    assert registry.active_generation(conn) is None
    conn.close()


def test_hash_profile_and_unbound_tokenizer_refused_before_allocation(setup):
    path, profile, clients, build = setup
    with pytest.raises(publisher.PublishError, match="semantic_profile_required"):
        build(profile=replace(profile, backend="hash"))
    tokenizer = FakeTokenizer()
    tokenizer.digest = "different-tokenizer"
    with pytest.raises(publisher.PublishError, match="tokenizer_profile_mismatch"):
        build(tokenizer=tokenizer)
    conn = connect(path)
    assert not conn.execute("SELECT 1 FROM index_generations").fetchone()
    assert not clients
    conn.close()


def test_manifest_roundtrip_digest_profile_and_corruption_rejection(setup, tmp_path):
    _, _, _, build = setup
    manifest = build()
    path = tmp_path / "manifest.json"
    manifest.write(path)
    loaded = Manifest.read(path, expected_digest=manifest.digest)
    assert loaded == manifest
    assert loaded.digest == manifest.digest
    assert loaded.profile() == manifest.profile()
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="digest_mismatch"):
        Manifest.read(path, expected_digest="0" * 64)
    fields = asdict(manifest)
    fields["profile_id"] = "wrong-profile"
    path.write_text(json.dumps(fields))
    with pytest.raises(ValueError, match="profile_mismatch"):
        Manifest.read(path)
    path.write_text("corrupted")
    with pytest.raises(ValueError):
        Manifest.read(path)


def test_atomic_manifest_interruption_preserves_previous_bytes(tmp_path, monkeypatch):
    path = tmp_path / "manifest.json"
    atomic_write(path, b"previous-complete")

    def fail(*args):
        raise OSError("synthetic interruption")

    monkeypatch.setattr("muninn.index.manifest.os.replace", fail)
    with pytest.raises(OSError, match="interruption"):
        atomic_write(path, b"partial-new")
    assert path.read_bytes() == b"previous-complete"
    assert not list(tmp_path.glob(".pending-*"))


def test_corrupted_manifest_cannot_replace_active(setup):
    path, _, _, build = setup
    first = build()
    publisher.activate_generation(first.generation_id, path)
    second = build()
    manifest_path = load_index_paths().manifest_path(second.generation_id)
    fields = json.loads(manifest_path.read_text())
    fields["source_count"] += 1
    manifest_path.write_text(json.dumps(fields))
    with pytest.raises(ValueError, match="digest_mismatch"):
        publisher.activate_generation(second.generation_id, path)
    conn = connect(path)
    assert registry.active_generation(conn).id == first.generation_id
    conn.close()


def test_cli_status_reports_registry_and_activation_is_explicit(setup):
    from click.testing import CliRunner
    from muninn.consumers.cli.main import cli

    path, _, _, build = setup
    manifest = build()
    runner = CliRunner()
    result = runner.invoke(cli, ["--db", str(path), "index", "status"])
    assert result.exit_code == 0, result.output
    status = json.loads(result.output)
    assert status["active"] is None
    assert status["source_epoch"] == manifest.source_epoch
    activated = runner.invoke(cli, ["--db", str(path), "index", "activate", manifest.generation_id])
    assert activated.exit_code == 0, activated.output
    assert json.loads(activated.output) == {"generation_id": manifest.generation_id, "state": "active"}
    result = runner.invoke(cli, ["--db", str(path), "index", "status"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["active"]["generation_id"] == manifest.generation_id


def test_all_default_artifact_paths_resolve_under_configured_data_dir(setup):
    from muninn.config import load_paths
    from muninn.index.embedding_cache import default_cache_dir

    paths = load_index_paths()
    root = load_paths().data_dir
    for path in (paths.root, paths.generations_dir, paths.staging_dir, paths.build_dir,
                 paths.embedding_cache_dir, default_cache_dir(), paths.manifest_path("synthetic")):
        assert path.resolve().is_relative_to(root.resolve())


def test_rollback_obeys_current_epoch_and_retains_previous(setup):
    path, profile, clients, build = setup
    first, second, third = build(), build(), build()
    publisher.activate_generation(first.generation_id, path)
    publisher.activate_generation(second.generation_id, path)
    conn = connect(path)
    assert publisher.retained_generations(conn) == {first.generation_id, second.generation_id}
    conn.close()
    assert publisher.retention_candidates(path) == (third.generation_id,)
    with pytest.raises(publisher.PublishError, match="rollback_generation_not_retained"):
        publisher.rollback_generation(third.generation_id, path)
    # GC is advisory until cross-process reader ownership can be proved.
    assert load_index_paths().generation_dir(third.generation_id).exists()
    publisher.rollback_generation(first.generation_id, path)
    conn = connect(path)
    conn.execute("UPDATE bookmarks SET content_visible=0")
    conn.commit()
    conn.close()
    with pytest.raises(registry.RegistryError, match="source_epoch_changed"):
        publisher.rollback_generation(second.generation_id, path)
    conn = connect(path)
    assert registry.active_generation(conn).id == first.generation_id
    conn.close()
