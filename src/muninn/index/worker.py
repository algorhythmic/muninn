"""Optional supervised publisher: one coalesced job, never auto-activation.

The canonical registry is the only durable authority. Each pass holds its
OS-backed publisher lock; staging from a dead publisher is marked failed and
rebuilt rather than replaying an unknown mutation. No source content is logged.
"""
from __future__ import annotations

from dataclasses import dataclass
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
    return len(rows)


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
            return status(conn)
    finally:
        conn.close()


def supervise(db_path=None, *, poll_seconds: float = 5.0, stop: Callable[[], bool] = lambda: False,
              **build_options) -> None:
    """Foreground process for an external supervisor; no daemon or activation.

    Only captured epochs enqueue automatically; callers detecting authored
    inventory changes use enqueue. Failed jobs retry no faster than the poll.
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
