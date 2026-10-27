"""Explicit staged full-generation builds; failures never replace active data."""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from muninn.config import load_paths
from muninn.consumers.mcp.vault_fs import AUTHORED_NAMESPACES, NAMESPACES
from muninn.db import connect
from muninn.index.authored import Inventory, inventory_authored
from muninn.index.embedding_cache import EmbeddingCache
from muninn.index.embedding_profile import available_profile, document_embeddings, document_tokenizer
from muninn.index.manifest import Manifest, atomic_write, encoded, fsync_dir
from muninn.index.paths import load_index_paths


class PublishError(RuntimeError):
    """Safe publication category, without queries or source content."""


def _scope(namespaces) -> tuple[str, ...]:
    result = tuple(sorted(set(namespaces)))
    if not result or any(ns not in NAMESPACES for ns in result):
        raise ValueError("invalid index namespace scope")
    return result


def _inventory(conn, scope) -> Inventory:
    if not set(scope).intersection(AUTHORED_NAMESPACES):
        return Inventory((), (), (), True)
    paths = load_paths()
    if paths.vault_dir is None:
        raise PublishError("authored_inventory_unavailable")
    result = inventory_authored(
        paths.vault_dir, paths.personal_vault_dir,
        row_visible=lambda bid: conn.execute(
            "SELECT 1 FROM bookmarks WHERE bookmark_id=? AND content_visible=1", (bid,)
        ).fetchone() is not None,
    )
    if not result.complete:
        raise PublishError("authored_inventory_incomplete")
    return result


def _records(path: Path):
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            yield json.loads(line)


def build_generation(db_path=None, *, binary: Path | str = "braid", namespaces=NAMESPACES,
                     profile=None, tokenizer=None, client_factory=None,
                     encoder=document_embeddings, max_spool_bytes=256 * 1024 * 1024) -> Manifest:
    """Build ready files, without activating them. Injected fakes are for tests.

    A durable registry row/journal owns staging. After any unknown mutation
    outcome the disposable build is abandoned; no mutation is blindly retried.
    """
    from muninn.index import registry
    from muninn.index.braid_client import BraidClient
    from muninn.index.projection import project

    scope = _scope(namespaces)
    profile = profile or available_profile()
    profile.require_complete()
    if not profile.semantic_eligible:
        raise PublishError("semantic_profile_required")
    tokenizer = tokenizer or document_tokenizer(profile)
    if tokenizer.digest != profile.tokenizer_digest:
        raise PublishError("tokenizer_profile_mismatch")
    paths = load_index_paths()
    generation_id = uuid.uuid4().hex
    staging = paths.generation_dir(generation_id, staging=True)
    final = paths.generation_dir(generation_id)
    conn = connect(db_path)
    allocated = False
    client = None
    try:
        corpus_id = registry.ensure_corpus(conn)
        with registry.publisher_lease(conn) as lease:
            from muninn.index.worker import reconcile_staging, build_started, build_finished
            reconcile_staging(conn)
            registry.allocate_generation(conn, generation_id, scope, lease_token=lease.token)
            allocated = True
            build_started(conn, generation_id)
            staging.mkdir(parents=True, mode=0o700)
            staging.chmod(0o700)
            journal = staging / "journal.json"
            def phase(state):
                atomic_write(journal, encoded({"generation_id": generation_id, "phase": state}))
            phase("snapshot")
            spool = staging / "projection.jsonl"
            omissions, source_count, chunk_count = [], 0, 0
            conn.execute("BEGIN")
            try:
                epoch = registry.source_epoch(conn)
                if registry.get_generation(conn, generation_id).source_epoch != epoch:
                    raise PublishError("source_changed_before_snapshot")
                inventory = _inventory(conn, scope)
                fd = os.open(spool, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                total_bytes = 0
                with os.fdopen(fd, "wb") as stream:
                    for record in project(conn, corpus_id, inventory, profile, tokenizer, namespaces=scope):
                        item = {"kind": record.kind, "value": record.value,
                                "embedding_input": record.embedding_input}
                        line = encoded(item) + b"\n"
                        total_bytes += len(line)
                        if total_bytes > max_spool_bytes:
                            raise PublishError("projection_spool_limit")
                        stream.write(line)
                        if record.kind == "omission":
                            omissions.append(record.value)
                        if record.kind == "node":
                            kind = record.value.get("attrs", {}).get("kind")
                            source_count += kind == "document"
                            chunk_count += kind == "chunk"
                    stream.flush()
                    os.fsync(stream.fileno())
            finally:
                conn.rollback()  # Read snapshot ends before any embedding inference.
            lease.assert_owned()
            dataset_id = corpus_id + ":scope:" + hashlib.sha256(encoded(scope)).hexdigest()[:24]
            factory = client_factory or BraidClient.create
            client = factory(binary=Path(binary), generation_dir=staging,
                             dataset_id=dataset_id, profile_id=profile.profile_id)
            phase("nodes")
            client.publish_nodes(r["value"] for r in _records(spool) if r["kind"] == "node")
            phase("vectors")
            inputs = (r["embedding_input"] for r in _records(spool) if r["embedding_input"] is not None)
            vectors = encoder(inputs, profile, cache=EmbeddingCache())
            def embeddings():
                for record in _records(spool):
                    if record["embedding_input"] is not None:
                        try:
                            vector = next(vectors)
                        except StopIteration as exc:
                            raise PublishError("encoder_result_count") from exc
                        yield {"node_id": record["value"]["id"], "model": profile.profile_id, "vec": vector}
                if next(vectors, None) is not None:
                    raise PublishError("encoder_result_count")
            client.publish_embeddings(embeddings())
            phase("edges")
            client.publish_edges(r["value"] for r in _records(spool) if r["kind"] == "edge")
            state = client.finish()  # Child exits and database is checkpointed before relocation.
            identity = client.identity
            client = None
            manifest = Manifest(corpus_id, dataset_id, generation_id, scope, epoch,
                                inventory.digest if set(scope).intersection(AUTHORED_NAMESPACES) else None,
                                asdict(profile), profile.profile_id, source_count, chunk_count,
                                tuple(omissions), {"identity": identity, "state": state,
                                                  "context_schema": "muninn-context/1"}, int(time.time()))
            manifest.write(staging / "manifest.json")
            phase("complete")
            lease.assert_owned()
            paths.generations_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.rename(staging, final)
            fsync_dir(paths.generations_dir)
            fsync_dir(paths.staging_dir)
            registry.mark_ready(conn, generation_id, str(final / "manifest.json"), manifest.digest,
                                lease_token=lease.token)
            build_finished(conn, generation_id)
            return manifest
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        if allocated:
            registry.mark_failed(conn, generation_id, "build_failed")
            from muninn.index.worker import build_finished
            build_finished(conn, generation_id, failed=True)
        raise
    finally:
        if client is not None:
            client.close()
        conn.close()


def activate_generation(generation_id: str, db_path=None) -> Manifest:
    """Explicit source-checked active switch. Authored edits still need hydration."""
    from muninn.index import registry

    conn = connect(db_path)
    try:
        with registry.publisher_lease(conn) as lease:
            generation = registry.get_generation(conn, generation_id)
            if generation is None or generation.state != "ready":
                raise PublishError("generation_not_ready")
            expected_path = load_index_paths().manifest_path(generation_id)
            if Path(generation.manifest_path).resolve() != expected_path.resolve():
                raise PublishError("generation_path_mismatch")
            manifest = Manifest.read(expected_path, expected_digest=generation.manifest_digest)
            if manifest.projection_version != "muninn-braid-projection/2":
                raise PublishError("projection_version_mismatch")
            if manifest.generation_id != generation_id or manifest.corpus_id != registry.ensure_corpus(conn):
                raise PublishError("generation_identity_mismatch")
            inventory = _inventory(conn, manifest.namespaces)
            if manifest.authored_inventory_digest is not None and inventory.digest != manifest.authored_inventory_digest:
                raise PublishError("authored_inventory_changed")
            identity = registry.prepare_activation(conn, generation_id, manifest.source_epoch)
            registry.activate(conn, generation_id, manifest.source_epoch, lease_token=lease.token,
                              activation_id=identity)
            previous = conn.execute("SELECT previous_generation_id FROM index_corpus WHERE singleton=1").fetchone()[0]
            if previous is not None and previous != generation_id:
                # Pointer commit is authoritative even if local session cleanup
                # fails. Other processes retain their own bounded read leases.
                try:
                    from muninn.index.runtime import runtime
                    runtime.close_generation(previous)
                except (OSError, RuntimeError):
                    pass
            return manifest
    finally:
        conn.close()


def rollback_generation(generation_id: str, db_path=None) -> Manifest:
    """Explicitly reselect a retained generation under today's source rules.

    Rollback deliberately uses exactly the activation checks, including epoch,
    inventory, manifest integrity and scope. It cannot resurrect removed content.
    """
    from muninn.index import registry
    conn = connect(db_path)
    try:
        # Keep membership and the switch under one OS ownership interval. The
        # inner activation acquires its canonical fencing lease reentrantly.
        with registry.publisher_lock(conn):
            if generation_id not in retained_generations(conn):
                raise PublishError("rollback_generation_not_retained")
            return activate_generation(generation_id, db_path)
    finally:
        conn.close()


def retained_generations(conn) -> set[str]:
    """Canonical active/previous references; reader-owned data must also survive."""
    row = conn.execute("SELECT active_generation_id,previous_generation_id FROM index_corpus WHERE singleton=1").fetchone()
    return {value for value in row if value is not None}


def retention_candidates(db_path=None, *, leased=()) -> tuple[str, ...]:
    """Conservative retention report; destructive cross-process GC is deferred.

    An in-process lease list cannot establish that another process has closed a
    SQLite file. Until a cross-process reader lease is installed this function
    returns eligible identities without unlinking any files or registry rows.
    Canonical sources, authored files and cache are never cleanup candidates.
    """
    from muninn.index import registry
    conn = connect(db_path)
    try:
        with registry.publisher_lease(conn):
            keep = retained_generations(conn) | set(leased)
            return tuple(row[0] for row in conn.execute(
                "SELECT generation_id FROM index_generations WHERE state IN ('ready','failed') ORDER BY generation_id"
            ) if row[0] not in keep)
    finally:
        conn.close()
