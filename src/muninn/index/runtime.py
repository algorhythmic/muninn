"""Bounded shared, read-only Braid sessions; canonical registry owns activation.

Leases pin immutable generation files. This runtime never chooses or activates a
new generation: callers supply the generation pinned by their source snapshot.
"""
from __future__ import annotations

from dataclasses import dataclass
import atexit
import hashlib
import math
from pathlib import Path
import threading
import sqlite3
import time

from muninn.index.braid_client import BraidClient, BraidError, REQUIRED_CAPABILITIES
from muninn.index.ownership import generation_lock, GenerationBusy

READ_METHODS = frozenset({"query", "dataset", "export_context"})
CONTEXT_CAPABILITIES = frozenset({"required_context", "context_export", "get_many", "allowed_ids", "read_revision"})


@dataclass
class _Session:
    key: tuple
    client: object
    generation: str
    references: int = 0
    draining: bool = False
    idle_since: float = 0
    drain_at: float = 0
    disposed: bool = False
    close_failed: bool = False
    ownership: object = None
    db_path: Path | None = None


class ReadLease:
    """Expose protocol reads only; close releases a pool lease, not the pipe."""
    def __init__(self, runtime, session):
        self._runtime, self._session, self._closed = runtime, session, False
        self.state = dict(session.client.state)
        self.identity = dict(session.client.identity)
        self.capabilities = session.client.capabilities

    def request(self, method, **payload):
        if self._closed:
            raise BraidError("session_abandoned")
        if self._session.disposed:
            raise BraidError("generation_drained")
        if method not in READ_METHODS:
            raise BraidError("read_only_session")
        if self._session.draining and time.monotonic() >= self._session.drain_at:
            raise BraidError("generation_drained")
        return self._session.client.request(method, **payload)

    def close(self):
        if not self._closed:
            self._closed = True
            self._runtime._release(self._session)


class BraidRuntime:
    def __init__(self, *, max_sessions=4, idle_seconds=60, drain_seconds=10,
                 backoff_seconds=1, opener=None, context_opener=None, poll_seconds=.25):
        durations = (idle_seconds, drain_seconds, backoff_seconds, poll_seconds)
        if (type(max_sessions) is not int or max_sessions < 1 or any(
                not isinstance(value, (int, float)) or isinstance(value, bool)
                or not math.isfinite(value) or value <= 0 for value in durations)):
            raise ValueError("positive runtime bounds required")
        self.max_sessions, self.idle_seconds = max_sessions, idle_seconds
        self.drain_seconds, self.backoff_seconds = drain_seconds, backoff_seconds
        self.opener = opener or BraidClient.open_existing
        self.context_opener = context_opener or BraidClient.open_context
        self._lock = threading.RLock()
        self._sessions, self._failures, self._identities = {}, {}, {}
        self.poll_seconds = poll_seconds
        self._watcher = None
        self._stop = threading.Event()

    def _dispose(self, session):
        session.disposed = True
        try:
            session.client.close()
        except (BraidError, OSError):
            session.close_failed = True
        if session.close_failed:
            process = getattr(getattr(session.client, "transport", None), "process", None)
            if process is None or process.poll() is None:
                # Failed close is not evidence the child stopped using SQLite.
                # Keep the OS lease and pool record until exit can be verified.
                return
        if self._sessions.get(session.key) is session:
            self._sessions.pop(session.key, None)
        if session.ownership is not None:
            session.ownership.close()

    @staticmethod
    def _active(db_path):
        conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=.1)
        try:
            row = conn.execute("SELECT active_generation_id FROM index_corpus WHERE singleton=1").fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def _watch(self, stop):
        while not stop.wait(self.poll_seconds):
            with self._lock:
                sessions = list(self._sessions.values())
            for session in sessions:
                if session.disposed or session.draining:
                    continue
                with self._lock:
                    if not session.references and time.monotonic() - session.idle_since >= self.idle_seconds:
                        self.close_generation(session.generation)
                        continue
                if session.db_path is None:
                    continue
                try:
                    active = self._active(session.db_path)
                except (OSError, sqlite3.Error):
                    active = None  # Failed observation drains, never extends ownership.
                if active != session.generation:
                    self.close_generation(session.generation)

    def acquire(self, *, binary, generation_dir, dataset_id, profile_id,
                context=False, db_path=None, watch_active=False, ownership_paths=None,
                expected_binary_digest=None, **kwargs):
        binary = Path(binary)
        from muninn.config import load_braid_settings
        settings = load_braid_settings()
        expected_digest = settings.sha256 if (settings.binary is not None
            and binary.is_absolute() and binary.resolve() == settings.binary.resolve()) else None
        if not binary.is_absolute():
            if str(binary) != "braid":
                raise BraidError("binary_path_required")
            binary = settings.binary
            expected_digest = settings.sha256
            if binary is None:
                raise BraidError("binary_path_required")
        binary = binary.resolve(strict=True)
        with binary.open("rb") as file:
            digest = hashlib.file_digest(file, "sha256").hexdigest()
        if expected_binary_digest is not None and digest != expected_binary_digest:
            raise BraidError("binary_identity_mismatch")
        if expected_digest is not None and digest != expected_digest:
            raise BraidError("binary_identity_mismatch")
        directory = Path(generation_dir).resolve()
        if watch_active:
            from muninn.config import load_paths
            db_path = Path(db_path or load_paths().db_path).resolve()
            try:
                if self._active(db_path) != directory.name:
                    raise BraidError("generation_draining")
            except sqlite3.Error:
                raise BraidError("generation_unavailable") from None
        else:
            db_path = None
        key = (str(binary), str(directory), dataset_id, profile_id, context, db_path)
        now = time.monotonic()
        with self._lock:
            for old in list(self._sessions.values()):
                if not old.references and (old.draining or old.client.poisoned
                        or now - old.idle_since >= self.idle_seconds):
                    self._dispose(old)
            session = self._sessions.get(key)
            if session is not None and (session.draining or session.disposed or session.client.poisoned):
                raise BraidError("generation_draining")
            pinned = self._identities.get(key)
            if pinned is not None and pinned.get("binary_digest") != digest:
                raise BraidError("binary_identity_mismatch")
            if session is None:
                retry_at, failures = self._failures.get(key, (0, 0))
                if now < retry_at:
                    raise BraidError("runtime_backoff")
                if len(self._sessions) >= self.max_sessions:
                    idle = [s for s in self._sessions.values() if not s.references]
                    if not idle:
                        raise BraidError("runtime_busy")
                    self._dispose(min(idle, key=lambda s: s.idle_since))
                    if len(self._sessions) >= self.max_sessions:
                        raise BraidError("runtime_busy")
                client = ownership = None
                try:
                    ownership = generation_lock(directory.name, paths=ownership_paths)
                    if not directory.is_dir():
                        raise BraidError("generation_unavailable")
                    opener = self.context_opener if context else self.opener
                    client = opener(binary=binary, generation_dir=directory,
                                    dataset_id=dataset_id, profile_id=profile_id,
                                    ownership_fd=ownership.fd, **kwargs)
                    required = set(REQUIRED_CAPABILITIES) | (CONTEXT_CAPABILITIES if context else set())
                    if not required.issubset(client.capabilities):
                        raise BraidError("capability_mismatch")
                    if client.identity.get("protocol_version") != 1:
                        raise BraidError("capability_mismatch")
                    if client.identity.get("binary_digest") != digest:
                        raise BraidError("binary_identity_mismatch")
                    if pinned is not None and client.identity != pinned:
                        raise BraidError("binary_identity_mismatch")
                    self._identities[key] = dict(client.identity)
                    session = _Session(key, client, directory.name, idle_since=now,
                                       ownership=ownership, db_path=db_path)
                    self._sessions[key] = session
                    self._failures.pop(key, None)
                    if self._watcher is None or self._stop.is_set() or not self._watcher.is_alive():
                        self._stop = threading.Event()
                        self._watcher = threading.Thread(target=self._watch, args=(self._stop,), name="muninn-generation-drain", daemon=True)
                        self._watcher.start()
                except Exception as exc:
                    close_failed = False
                    if client is not None:
                        try:
                            client.close()
                        except (BraidError, OSError):
                            close_failed = True
                    if close_failed:
                        failed = _Session(key, client, directory.name, disposed=True,
                                          close_failed=True, ownership=ownership)
                        self._sessions[key] = failed
                        self._dispose(failed)
                    elif ownership is not None:
                        ownership.close()
                    self._failures[key] = (now + min(60, self.backoff_seconds * 2 ** min(failures, 6)), failures + 1)
                    if isinstance(exc, GenerationBusy):
                        raise BraidError("generation_in_use") from None
                    raise
            session.references += 1
            return ReadLease(self, session)

    def _release(self, session):
        with self._lock:
            session.references -= 1
            session.idle_since = time.monotonic()
            if not session.references and (session.draining or session.disposed or session.client.poisoned):
                if session.client.poisoned:
                    self._failures[session.key] = (time.monotonic() + self.backoff_seconds, 1)
                self._dispose(session)

    def leased_generations(self):
        """All open files, including idle sessions, prevent retention cleanup."""
        with self._lock:
            return frozenset(s.generation for s in self._sessions.values())

    def _expire(self, session):
        with self._lock:
            if session.draining:
                self._dispose(session)

    def close_generation(self, generation_id):
        """Stop admission, drain bounded requests, close before file removal."""
        with self._lock:
            sessions = [s for s in self._sessions.values() if s.generation == generation_id]
            for session in sessions:
                if not session.draining:
                    session.draining = True
                    session.drain_at = time.monotonic() + self.drain_seconds
                    if session.references:
                        timer = threading.Timer(self.drain_seconds, self._expire, args=(session,))
                        timer.daemon = True
                        timer.start()
                if not session.references:
                    self._dispose(session)
            return not any(s.references for s in sessions)

    def close(self):
        with self._lock:
            self._stop.set()
            watcher = self._watcher
            for generation in self.leased_generations():
                self.close_generation(generation)
        if watcher is not None and watcher is not threading.current_thread():
            watcher.join(timeout=self.poll_seconds + .5)

    def shutdown(self):
        """Process exit closes every child synchronously, including held proxies."""
        with self._lock:
            self._stop.set()
            for session in list(self._sessions.values()):
                self._dispose(session)


runtime = BraidRuntime()
atexit.register(runtime.shutdown)


def open_search(**kwargs):
    from muninn.index.pilot import open_pilot
    return open_pilot(runtime, **kwargs)


def open_context(**kwargs):
    from muninn.index.pilot import open_pilot
    return open_pilot(runtime, context=True, **kwargs)
