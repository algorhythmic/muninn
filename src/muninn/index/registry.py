"""Canonical generation state and captured-source invalidation fencing.

Mutators own short immediate transactions and reject a caller's open transaction
rather than committing unrelated writes. Source projection owns its separate
read transaction. Derived files remain the publisher's responsibility.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import fcntl
import os
import threading
from pathlib import Path
import re
import sqlite3
from typing import Iterator
import uuid


class RegistryError(RuntimeError):
    pass


@dataclass(frozen=True)
class Generation:
    generation_id: str
    corpus_id: str
    scope: tuple[str, ...]
    source_epoch: int
    state: str
    manifest_path: str | None
    manifest_digest: str | None

    @property
    def id(self) -> str:
        return self.generation_id


@contextmanager
def _write(conn: sqlite3.Connection) -> Iterator[None]:
    if conn.in_transaction:
        raise RegistryError("registry requires a connection without an open transaction")
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def ensure_corpus(conn: sqlite3.Connection) -> str:
    """Migration 005 initializes identity once; this call is read-only."""
    row = conn.execute("SELECT corpus_id FROM index_corpus WHERE singleton=1").fetchone()
    if row is None:
        raise RegistryError("corpus registry missing: run database migrations")
    return row[0]


def source_epoch(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT source_epoch FROM index_corpus WHERE singleton=1").fetchone()[0]


def _fence(conn: sqlite3.Connection, token: str | None) -> None:
    if token is not None and not conn.execute(
        "SELECT 1 FROM index_corpus WHERE singleton=1 AND lease_token=? "
        "AND lease_expires_at > unixepoch()", (token,),
    ).fetchone():
        raise RegistryError("publisher_lease_lost")


def allocate_generation(conn: sqlite3.Connection, generation_id: str,
                        scope: tuple[str, ...], *, lease_token: str | None = None) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", generation_id):
        raise ValueError("invalid generation identifier")
    if not isinstance(scope, tuple) or not scope or any(not isinstance(v, str) or not v for v in scope):
        raise ValueError("nonempty tuple of scope names required")
    if len(set(scope)) != len(scope):
        raise ValueError("duplicate scope names")
    with _write(conn):
        _fence(conn, lease_token)
        conn.execute(
            "INSERT INTO index_generations(generation_id,corpus_id,scope_json,source_epoch,state) "
            "SELECT ?,corpus_id,?,source_epoch,'staging' FROM index_corpus WHERE singleton=1",
            (generation_id, json.dumps(sorted(scope), separators=(",", ":"))),
        )


def set_generation_snapshot(conn: sqlite3.Connection, generation_id: str, epoch: int, *,
                            lease_token: str | None = None) -> None:
    """Bind the epoch actually read by the publisher's completed read snapshot."""
    if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 0:
        raise ValueError("invalid source epoch")
    with _write(conn):
        _fence(conn, lease_token)
        if epoch > source_epoch(conn):
            raise RegistryError("snapshot_epoch_ahead")
        changed = conn.execute(
            "UPDATE index_generations SET source_epoch=? WHERE generation_id=? AND state='staging'",
            (epoch, generation_id),
        ).rowcount
        if not changed:
            raise RegistryError("generation_not_staging")


def mark_ready(conn: sqlite3.Connection, generation_id: str, manifest_path: Path | str,
               manifest_digest: str, *, lease_token: str | None = None) -> None:
    if not str(manifest_path) or not re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", manifest_digest):
        raise ValueError("manifest path and SHA256 digest required")
    with _write(conn):
        _fence(conn, lease_token)
        changed = conn.execute(
            "UPDATE index_generations SET state='ready',manifest_path=?,manifest_digest=?,ready_at=unixepoch() "
            "WHERE generation_id=? AND state='staging'", (str(manifest_path), manifest_digest, generation_id),
        ).rowcount
        if not changed:
            raise RegistryError("generation_not_staging")


def mark_failed(conn: sqlite3.Connection, generation_id: str, reason: str = "build_failed", *,
                lease_token: str | None = None) -> None:
    with _write(conn):
        _fence(conn, lease_token)
        changed = conn.execute(
            "UPDATE index_generations SET state='failed',failure_reason=? "
            "WHERE generation_id=? AND state='staging'", (reason, generation_id),
        ).rowcount
        if not changed:
            raise RegistryError("generation_not_staging")


def _generation(row) -> Generation | None:
    return None if row is None else Generation(row[0], row[1], tuple(json.loads(row[2])), *row[3:])


_GENERATION_COLUMNS = "generation_id,corpus_id,scope_json,source_epoch,state,manifest_path,manifest_digest"


def get_generation(conn: sqlite3.Connection, generation_id: str) -> Generation | None:
    return _generation(conn.execute(
        f"SELECT {_GENERATION_COLUMNS} FROM index_generations WHERE generation_id=?", (generation_id,),
    ).fetchone())


def generation(conn: sqlite3.Connection, generation_id: str) -> Generation | None:
    """Return a staging/ready/failed generation by its identity."""
    return get_generation(conn, generation_id)


def active_generation(conn: sqlite3.Connection) -> Generation | None:
    return _generation(conn.execute(
        f"SELECT {_GENERATION_COLUMNS} FROM index_generations WHERE generation_id="
        "(SELECT active_generation_id FROM index_corpus WHERE singleton=1)",
    ).fetchone())


def activate(conn: sqlite3.Connection, generation_id: str, expected_epoch: int, *,
             lease_token: str | None = None, activation_id: str | None = None) -> None:
    with _write(conn):
        _fence(conn, lease_token)
        generation = get_generation(conn, generation_id)
        if generation is None or generation.state != "ready":
            raise RegistryError("generation_not_ready")
        if generation.source_epoch != expected_epoch or source_epoch(conn) != expected_epoch:
            raise RegistryError("source_epoch_changed")
        if activation_id is None:
            activation_id = uuid.uuid4().hex
            conn.execute("INSERT INTO index_activations(activation_id,generation_id,previous_generation_id,expected_epoch,state) "
                         "SELECT ?,?,active_generation_id,?,'prepared' FROM index_corpus WHERE singleton=1",
                         (activation_id, generation_id, expected_epoch))
        intent = conn.execute("SELECT generation_id,expected_epoch,state FROM index_activations WHERE activation_id=?",
                              (activation_id,)).fetchone()
        if intent is None or tuple(intent) != (generation_id, expected_epoch, "prepared"):
            raise RegistryError("activation_identity_mismatch")
        conn.execute("UPDATE index_corpus SET previous_generation_id=CASE WHEN active_generation_id IS NOT ? "
                     "THEN active_generation_id ELSE previous_generation_id END,active_generation_id=?,activation_id=? WHERE singleton=1",
                     (generation_id, generation_id, activation_id))
        conn.execute("UPDATE index_activations SET state='committed',committed_at=unixepoch() WHERE activation_id=?", (activation_id,))



def prepare_activation(conn: sqlite3.Connection, generation_id: str, expected_epoch: int) -> str:
    """Persist an intent before the pointer transaction; identity resolves lost returns."""
    identity = uuid.uuid4().hex
    with _write(conn):
        conn.execute("INSERT INTO index_activations(activation_id,generation_id,previous_generation_id,expected_epoch,state) "
                     "SELECT ?,?,active_generation_id,?,'prepared' FROM index_corpus WHERE singleton=1",
                     (identity, generation_id, expected_epoch))
    return identity


def activation_outcome(conn: sqlite3.Connection, identity: str) -> str:
    """Read the canonical pointer; never retry an unknown activation blindly."""
    row = conn.execute("SELECT a.state,c.activation_id,c.active_generation_id,a.generation_id "
                       "FROM index_activations a CROSS JOIN index_corpus c WHERE a.activation_id=?", (identity,)).fetchone()
    if row is None:
        return "unknown"
    if row[1] == identity and row[2] == row[3]:
        return "active"
    return "superseded" if row[0] == "committed" else "not_committed"


_lock_guard = threading.Lock()
_locks: dict[str, tuple[int, int, int]] = {}


@contextmanager
def publisher_lock(conn: sqlite3.Connection) -> Iterator[None]:
    """Nonblocking OS lock; lease expiry never authorizes stealing live work.

    Nested acquisition by the owning thread is allowed for the supervised worker.
    The lock is tied to the canonical database inode's adjacent private lock file.
    """
    database = next((r[2] for r in conn.execute("PRAGMA database_list") if r[1] == "main"), "")
    if not database:
        raise RegistryError("publisher_requires_file_database")
    key = str(Path(database).resolve()) + ".publisher.lock"
    owner = threading.get_ident()
    with _lock_guard:
        held = _locks.get(key)
        if held is not None:
            if held[0] != owner:
                raise RegistryError("publisher_lease_busy")
            _locks[key] = (owner, held[1], held[2] + 1)
        else:
            fd = os.open(key, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                os.close(fd)
                raise RegistryError("publisher_lease_busy") from exc
            _locks[key] = (owner, fd, 1)
    try:
        yield
    finally:
        with _lock_guard:
            held = _locks[key]
            if held[2] > 1:
                _locks[key] = (owner, held[1], held[2] - 1)
            else:
                del _locks[key]
                os.close(held[1])


@dataclass(frozen=True)
class PublisherLease:
    conn: sqlite3.Connection
    token: str

    def assert_owned(self) -> None:
        _fence(self.conn, self.token)


_active_leases: set[str] = set()


@contextmanager
def publisher_lease(conn: sqlite3.Connection, *, ttl_seconds: int = 3600) -> Iterator[PublisherLease]:
    """OS ownership plus short canonical fencing lease; crashed owners reconcile.

    The OS lock, never a stored PID or elapsed TTL, authorizes reclaiming an
    orphaned token. Expiry still fences a publisher whose build exceeded its TTL.
    """
    if not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool) or ttl_seconds <= 0:
        raise ValueError("positive lease TTL required")
    key = str(Path(conn.execute("PRAGMA database_list").fetchone()[2]).resolve())
    with publisher_lock(conn):
        if key in _active_leases:
            raise RegistryError("publisher_lease_busy")
        _active_leases.add(key)
        token = uuid.uuid4().hex
        try:
            with _write(conn):
                conn.execute("UPDATE index_corpus SET lease_token=?,lease_expires_at=unixepoch()+? WHERE singleton=1",
                             (token, ttl_seconds))
            yield PublisherLease(conn, token)
        finally:
            try:
                with _write(conn):
                    conn.execute("UPDATE index_corpus SET lease_token=NULL,lease_expires_at=NULL "
                                 "WHERE singleton=1 AND lease_token=?", (token,))
            finally:
                _active_leases.remove(key)
