"""Optional supervised publisher: one coalesced job, never auto-activation.

The canonical registry is the only durable authority. Each pass holds its
OS-backed publisher lock; staging from a dead publisher is marked failed and
rebuilt rather than replaying an unknown mutation. No source content is logged.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import time
from typing import Callable
import uuid

from muninn.db import connect
from muninn.index import registry


@dataclass(frozen=True)
class PublisherStatus:
    job_id: str | None
    state: str | None
    pending_epoch: int | None
    generation_id: str | None
    failure_category: str | None
    updated_at: int | None


def status(conn) -> PublisherStatus:
    row = conn.execute(
        "SELECT publisher_job_id,publisher_state,publisher_pending_epoch,"
        "publisher_generation_id,publisher_failure_category,publisher_updated_at "
        "FROM index_corpus WHERE singleton=1"
    ).fetchone()
    return PublisherStatus(*row)



@dataclass(frozen=True)
class AuthoredObservation:
    inventory_scope: str | None
    inventory_digest: str | None
    inventory_observed_at: int | None
    inventory_failure_category: str | None


def authored_status(conn) -> AuthoredObservation:
    row = conn.execute("SELECT publisher_inventory_scope,publisher_inventory_digest,"
                       "publisher_inventory_observed_at,publisher_inventory_failure_category "
                       "FROM index_corpus WHERE singleton=1").fetchone()
    return AuthoredObservation(*row)


def enqueue(conn) -> PublisherStatus:
    """Coalesce explicit/authored-change requests without overlapping builds."""
    with registry._write(conn):
        conn.execute(
            "UPDATE index_corpus SET publisher_job_id=coalesce(publisher_job_id,?),"
            "publisher_pending_epoch=source_epoch,"
            "publisher_state=CASE WHEN publisher_state='building' THEN 'building' ELSE 'pending' END,"
            "publisher_failure_category=NULL,publisher_updated_at=unixepoch() WHERE singleton=1",
            (uuid.uuid4().hex,),
        )
    return status(conn)


def reconcile_staging(conn) -> int:
    """Caller holds publisher_lock; never delete files or infer activation."""
    rows = conn.execute("SELECT generation_id FROM index_generations WHERE state='staging'").fetchall()
    for row in rows:
        registry.mark_failed(conn, row[0], "interrupted_build")
    if rows:
        with registry._write(conn):
            conn.execute("UPDATE index_corpus SET publisher_state='failed',publisher_failure_category='interrupted_build',"
                         "publisher_pending_epoch=source_epoch,publisher_updated_at=unixepoch() "
                         "WHERE singleton=1 AND publisher_state='building'")
    return len(rows)


def build_started(conn, generation_id):
    """Record explicit sync in the same job slot used by the optional worker."""
    with registry._write(conn):
        conn.execute("UPDATE index_corpus SET publisher_job_id=CASE WHEN publisher_state='building' "
                     "THEN publisher_job_id ELSE ? END,publisher_state='building',publisher_generation_id=?,"
                     "publisher_pending_epoch=CASE WHEN publisher_state='building' THEN publisher_pending_epoch ELSE NULL END,"
                     "publisher_failure_category=NULL,publisher_updated_at=unixepoch() "
                     "WHERE singleton=1", (uuid.uuid4().hex, generation_id))


def build_finished(conn, generation_id, *, failed=False):
    with registry._write(conn):
        generation = registry.get_generation(conn, generation_id)
        stale = generation is not None and generation.source_epoch != registry.source_epoch(conn)
        conn.execute("UPDATE index_corpus SET publisher_state=?,publisher_failure_category=?,"
                     "publisher_pending_epoch=CASE WHEN ? THEN source_epoch ELSE publisher_pending_epoch END,"
                     "publisher_updated_at=unixepoch() WHERE singleton=1 AND publisher_generation_id=?",
                     ("failed" if failed else "stale" if stale else "ready",
                      "build_failed" if failed else None, failed or stale, generation_id))



def _observation_scope(conn, requested):
    from muninn.index.publisher import _scope
    from muninn.consumers.mcp.vault_fs import NAMESPACES

    if requested is not None:
        return _scope(requested)
    row = conn.execute("SELECT scope_json FROM index_generations WHERE state='ready' "
                       "ORDER BY ready_at DESC,generation_id DESC LIMIT 1").fetchone()
    return _scope(json.loads(row[0]) if row else NAMESPACES)


def observe_authored(conn, scope) -> bool:
    """Bounded B inventory scan; False means unavailable, never empty corpus.

    Only successful scans replace the durable baseline. Scope is part of the
    comparison identity; a change coalesces in the existing pending epoch even
    when authored filesystem changes do not increment SQLite's source epoch.
    """
    from muninn.consumers.mcp.vault_fs import AUTHORED_NAMESPACES
    from muninn.index.publisher import _inventory, PublishError

    if not set(scope).intersection(AUTHORED_NAMESPACES):
        return True
    key = json.dumps(sorted(scope), separators=(",", ":"))
    try:
        inventory = _inventory(conn, scope)
        if not inventory.complete:
            raise PublishError("authored_inventory_incomplete")
        digest = inventory.digest
    except (OSError, RuntimeError, ValueError):
        with registry._write(conn):
            conn.execute("UPDATE index_corpus SET publisher_inventory_failure_category='authored_inventory_unavailable',"
                         "publisher_job_id=coalesce(publisher_job_id,?),"
                         "publisher_state=CASE WHEN publisher_state IN ('ready','active','stale') THEN 'stale' ELSE 'failed' END,"
                         "publisher_failure_category='authored_inventory_unavailable',"
                         "publisher_pending_epoch=source_epoch,publisher_updated_at=unixepoch() WHERE singleton=1",
                         (uuid.uuid4().hex,))
        return False
    with registry._write(conn):
        before = authored_status(conn)
        changed = before.inventory_scope != key or before.inventory_digest != digest
        conn.execute("UPDATE index_corpus SET publisher_inventory_scope=?,publisher_inventory_digest=?,"
                     "publisher_inventory_observed_at=unixepoch(),publisher_inventory_failure_category=NULL WHERE singleton=1",
                     (key, digest))
        if changed:
            conn.execute("UPDATE index_corpus SET publisher_job_id=coalesce(publisher_job_id,?),"
                         "publisher_state=CASE WHEN publisher_state='building' THEN 'building' "
                         "WHEN publisher_state IN ('ready','active','stale') THEN 'stale' ELSE 'pending' END,"
                         "publisher_pending_epoch=source_epoch,publisher_failure_category=NULL,"
                         "publisher_updated_at=unixepoch() WHERE singleton=1", (uuid.uuid4().hex,))
    return True


def _seed_ready_inventory(conn, scope):
    """Reuse only digest-verified ready manifests; never trust an arbitrary path."""
    from muninn.index.manifest import Manifest
    from muninn.index.paths import load_index_paths
    from pathlib import Path

    key = json.dumps(sorted(scope), separators=(",", ":"))
    if authored_status(conn).inventory_scope == key:
        return
    row = conn.execute("SELECT generation_id,manifest_path,manifest_digest FROM index_generations "
                       "WHERE state='ready' AND scope_json=? ORDER BY ready_at DESC,generation_id DESC LIMIT 1", (key,)).fetchone()
    if row is None:
        return
    try:
        path = load_index_paths().manifest_path(row[0])
        if Path(row[1]).resolve() != path.resolve():
            return
        manifest = Manifest.read(path, expected_digest=row[2])
        if manifest.generation_id != row[0] or tuple(manifest.namespaces) != tuple(scope):
            return
        _bind_built_inventory(conn, manifest, scope)
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        return


def _bind_built_inventory(conn, manifest, scope):
    digest = getattr(manifest, "authored_inventory_digest", None)
    if digest is not None:
        with registry._write(conn):
            conn.execute("UPDATE index_corpus SET publisher_inventory_scope=?,publisher_inventory_digest=?,"
                         "publisher_inventory_observed_at=unixepoch(),publisher_inventory_failure_category=NULL WHERE singleton=1",
                         (json.dumps(sorted(scope), separators=(",", ":")), digest))


def run_once(db_path=None, *, build: Callable | None = None, **build_options) -> PublisherStatus:
    """Run at most one build; injected builders are synthetic test seams.

    A busy publisher raises RegistryError without changing this job. New source
    mutations during inference remain coalesced in publisher_pending_epoch.
    Failures retain pending work and the active reference; only safe categories
    are persisted, never exception messages containing source information.
    """
    from muninn.index.publisher import build_generation

    conn = connect(db_path)
    try:
        with registry.publisher_lock(conn):
            scope = _observation_scope(conn, build_options.get("namespaces"))
            build_options["namespaces"] = scope
            abandoned = reconcile_staging(conn)
            if abandoned and status(conn).pending_epoch is None:
                enqueue(conn)
            observed = status(conn)
            if observed.state == "building":
                # The OS lock proves the previous process no longer owns this
                # build. A stored PID or lease expiry alone cannot prove that.
                with registry._write(conn):
                    conn.execute("UPDATE index_corpus SET publisher_state='failed',"
                                 "publisher_failure_category='interrupted_build',"
                                 "publisher_pending_epoch=source_epoch,publisher_updated_at=unixepoch() "
                                 "WHERE singleton=1")
                observed = status(conn)
            _seed_ready_inventory(conn, scope)
            if not observe_authored(conn, scope):
                return status(conn)
            observed = status(conn)
            if observed.pending_epoch is None:
                return observed
            job_id = uuid.uuid4().hex
            with registry._write(conn):
                conn.execute(
                    "UPDATE index_corpus SET publisher_job_id=?,publisher_state='building',"
                    "publisher_pending_epoch=NULL,publisher_failure_category=NULL,"
                    "publisher_updated_at=unixepoch() WHERE singleton=1", (job_id,),
                )
            try:
                manifest = (build or build_generation)(db_path, **build_options)
            except Exception:
                with registry._write(conn):
                    conn.execute(
                        "UPDATE index_corpus SET publisher_state='failed',"
                        "publisher_pending_epoch=source_epoch,publisher_failure_category='build_failed',"
                        "publisher_updated_at=unixepoch() WHERE singleton=1 AND publisher_job_id=?", (job_id,),
                    )
                return status(conn)
            with registry._write(conn):
                # Do not discard a newer epoch observed while inference ran.
                current = registry.source_epoch(conn)
                stale = current != manifest.source_epoch
                conn.execute(
                    "UPDATE index_corpus SET publisher_state=?,publisher_generation_id=?,"
                    "publisher_pending_epoch=CASE WHEN ? THEN source_epoch ELSE publisher_pending_epoch END,"
                    "publisher_updated_at=unixepoch() WHERE singleton=1 AND publisher_job_id=?",
                    ("stale" if stale else "ready", manifest.generation_id, stale, job_id),
                )
            _bind_built_inventory(conn, manifest, scope)
            observe_authored(conn, scope)
            return status(conn)
    finally:
        conn.close()


def supervise(db_path=None, *, poll_seconds: float = 5.0, stop: Callable[[], bool] = lambda: False,
              **build_options) -> None:
    """Foreground process for an external supervisor; no daemon or activation.

    Captured epochs and complete bounded authored observations coalesce in
    the same canonical job slot. Failed scans/builds retry no faster than poll.
    """
    if not 0.1 <= poll_seconds <= 3600:
        raise ValueError("poll_seconds must be between 0.1 and 3600")
    while not stop():
        try:
            run_once(db_path, **build_options)
        except registry.RegistryError:
            pass  # Another explicit sync/worker owns the canonical OS lock.
        if not stop():
            time.sleep(poll_seconds)
