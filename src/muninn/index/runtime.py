"""Bounded shared, read-only Braid sessions; canonical registry owns activation.

Leases pin immutable generation files. This runtime never chooses or activates a
new generation: callers supply the generation pinned by their source snapshot.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import threading
import time

from muninn.index.braid_client import BraidClient, BraidError, REQUIRED_CAPABILITIES

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
                 backoff_seconds=1, opener=None, context_opener=None):
        if max_sessions < 1 or min(idle_seconds, drain_seconds, backoff_seconds) <= 0:
            raise ValueError("positive runtime bounds required")
        self.max_sessions, self.idle_seconds = max_sessions, idle_seconds
        self.drain_seconds, self.backoff_seconds = drain_seconds, backoff_seconds
        self.opener = opener or BraidClient.open_existing
        self.context_opener = context_opener or BraidClient.open_context
        self._lock = threading.RLock()
        self._sessions, self._failures, self._identities = {}, {}, {}

    def _dispose(self, session):
        if session.disposed:
            return
        session.disposed = True
        if self._sessions.get(session.key) is session:
            self._sessions.pop(session.key, None)
        try:
            session.client.close()
        except (BraidError, OSError):
            pass

    def acquire(self, *, binary, generation_dir, dataset_id, profile_id,
                context=False, **kwargs):
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
        if expected_digest is not None and digest != expected_digest:
            raise BraidError("binary_identity_mismatch")
        directory = Path(generation_dir).resolve()
        key = (str(binary), str(directory), dataset_id, profile_id, context)
        now = time.monotonic()
        with self._lock:
            for old in list(self._sessions.values()):
                if not old.references and (old.draining or old.client.poisoned
                        or now - old.idle_since >= self.idle_seconds):
                    self._dispose(old)
            session = self._sessions.get(key)
            if session is not None and (session.draining or session.client.poisoned):
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
                client = None
                try:
                    opener = self.context_opener if context else self.opener
                    client = opener(binary=binary, generation_dir=directory,
                                    dataset_id=dataset_id, profile_id=profile_id, **kwargs)
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
                    session = _Session(key, client, directory.name, idle_since=now)
                    self._sessions[key] = session
                    self._failures.pop(key, None)
                except Exception:
                    if client is not None:
                        try:
                            client.close()
                        except (BraidError, OSError):
                            pass
                    self._failures[key] = (now + min(60, self.backoff_seconds * 2 ** min(failures, 6)), failures + 1)
                    raise
            session.references += 1
            return ReadLease(self, session)

    def _release(self, session):
        with self._lock:
            session.references -= 1
            session.idle_since = time.monotonic()
            if not session.references and (session.draining or session.client.poisoned):
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
            for generation in self.leased_generations():
                self.close_generation(generation)


runtime = BraidRuntime()


def open_search(**kwargs):
    return runtime.acquire(**kwargs)


def open_context(**kwargs):
    return runtime.acquire(context=True, **kwargs)
