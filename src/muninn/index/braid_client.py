"""Bounded protocol-v1 subprocess adapter for disposable Braid generations.

Contract consumer: QUERY-VECTOR-CONTRACT revision 1. Mutations are never retried;
any failed write poisons this session so the publisher must abandon its stage.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import select
import shutil
import sqlite3
import subprocess
import time
import threading
from typing import Iterable, Protocol

REQUIRED_CAPABILITIES = (
    "query_embedding", "apply", "dataset", "dataset_binding",
    "strict_requests", "structured_errors",
)


class BraidError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _encode(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":"), sort_keys=True).encode("utf-8")


class Transport(Protocol):
    def exchange(self, request: bytes, *, timeout: float, max_response_bytes: int) -> bytes: ...
    def close(self) -> None: ...


class StdioTransport:
    """Sequential pipe transport, bounded in bytes and by one wall deadline."""
    def __init__(self, command: list[str], *, cwd: Path | None = None, ownership_fd: int | None = None):
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=0, cwd=cwd,
            env={"PATH": os.defpath, "LANG": "C.UTF-8"},
            pass_fds=() if ownership_fd is None else (ownership_fd,))
        os.set_blocking(self.process.stdin.fileno(), False)
        os.set_blocking(self.process.stdout.fileno(), False)

    def exchange(self, request: bytes, *, timeout: float, max_response_bytes: int) -> bytes:
        deadline = time.monotonic() + timeout
        payload = memoryview(request + b"\n")
        incoming = bytearray()
        write_fd = self.process.stdin.fileno()
        read_fd = self.process.stdout.fileno()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BraidError("transport_timeout")
            readable, writable, _ = select.select([read_fd], [write_fd] if payload else [], [], remaining)
            if writable:
                try:
                    count = os.write(write_fd, payload[:65536])
                except (BrokenPipeError, OSError):
                    raise BraidError("transport_closed") from None
                payload = payload[count:]
            if readable:
                data = os.read(read_fd, min(65536, max_response_bytes + 1 - len(incoming)))
                if not data:
                    raise BraidError("transport_closed")
                incoming.extend(data)
                if b"\n" in incoming:
                    line, rest = incoming.split(b"\n", 1)
                    if rest or payload:
                        raise BraidError("protocol_fault")
                    if len(line) > max_response_bytes:
                        raise BraidError("response_too_large")
                    return bytes(line)
                if len(incoming) > max_response_bytes:
                    raise BraidError("response_too_large")

    def close(self) -> None:
        self.process.stdin.close()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=1)
            raise BraidError("process_close_timeout")
        finally:
            self.process.stdout.close()
        if self.process.returncode:
            raise BraidError("process_failed")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


class BraidClient:
    def __init__(self, transport: Transport, *, dataset_id: str, profile_id: str,
                 timeout: float = 10, max_request_bytes: int = 8 * 1024 * 1024,
                 max_response_bytes: int = 8 * 1024 * 1024,
                 generation_dir: Path | None = None, binary_digest: str | None = None):
        if (not isinstance(dataset_id, str) or not dataset_id.strip()
                or not isinstance(profile_id, str) or not profile_id.strip()
                or not math.isfinite(timeout) or timeout <= 0):
            raise ValueError("dataset/profile identities and positive timeout required")
        if max_request_bytes < 256 or max_response_bytes < 256:
            raise ValueError("byte limits must be at least 256")
        self.transport = transport
        self.dataset_id = dataset_id
        self.profile_id = profile_id
        self.timeout = timeout
        self.max_request_bytes = min(max_request_bytes, 16 * 1024 * 1024)
        self.max_response_bytes = min(max_response_bytes, 16 * 1024 * 1024)
        self.generation_dir = generation_dir
        self.poisoned = False
        self.closed = False
        self._sequence = 0
        self._request_lock = threading.Lock()
        self._phase = "nodes"
        hello = self.request("hello", versions=[1], required_capabilities=list(REQUIRED_CAPABILITIES))
        caps = hello.get("capabilities")
        advertised = hello.get("max_request_bytes")
        if (type(hello.get("protocol_version")) is not int or hello["protocol_version"] != 1
                or not isinstance(caps, list) or not all(isinstance(cap, str) for cap in caps)
                or not set(REQUIRED_CAPABILITIES).issubset(caps)
                or type(advertised) is not int or advertised < 256
                or not isinstance(hello.get("ranking_version"), str) or not hello["ranking_version"]):
            raise BraidError("capability_mismatch")
        self.max_request_bytes = min(self.max_request_bytes, advertised)
        self.capabilities = frozenset(caps)
        self.state = self._state(hello.get("dataset"))
        self.identity = {"protocol_version": 1, "contract_revision": 1,
                         "ranking_version": hello["ranking_version"],
                         "binary_digest": binary_digest, "profile_id": profile_id}

    @classmethod
    def create(cls, binary: Path, generation_dir: Path, dataset_id: str, profile_id: str, **kwargs):
        generation_dir = Path(generation_dir).resolve()
        generation_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if any((generation_dir / name).exists() for name in ("braid.db", "braid.db-wal", "braid.db-shm")):
            raise BraidError("stage_not_fresh")
        config = generation_dir / "braid.json"
        value = {"version": 1, "dataset_id": dataset_id,
            "store": {"backend": "sqlite", "path": "braid.db"},
            "embed": {"provider": "none", "model": profile_id},
            "index": {"nodes": {"document": ["text"], "chunk": ["text"], "context_excerpt": ["text"]}},
            "candidate_limit": 512,
            "defaults": {"mmr_lambda": 1,
                         "weights": {"lexical": 1, "dense": 1, "graph": 0, "temporal": 0}}}
        fd = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as file:
            file.write(_encode(value))
            file.flush()
            os.fsync(file.fileno())
        context_config = json.loads(json.dumps(value))
        context_config["defaults"]["mmr_lambda"] = 0.7
        context_fd = os.open(generation_dir / "braid-context.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(context_fd, "wb") as file:
            file.write(_encode(context_config))
            file.flush()
            os.fsync(file.fileno())
        return cls._open(binary, generation_dir, dataset_id, profile_id, **kwargs)

    @classmethod
    def open_context(cls, binary: Path, generation_dir: Path, dataset_id: str, profile_id: str, **kwargs):
        """Use a build-time context projection without changing search config."""
        generation_dir = Path(generation_dir).resolve()
        if not (generation_dir / "braid.db").is_file():
            raise BraidError("generation_unavailable")
        base = json.loads((generation_dir / "braid.json").read_text())
        config = json.loads((generation_dir / "braid-context.json").read_text())
        expected = json.loads(json.dumps(base))
        expected.setdefault("defaults", {})["mmr_lambda"] = 0.7
        if (config != expected or config.get("dataset_id") != dataset_id
                or config.get("embed") != {"provider": "none", "model": profile_id}
                or config.get("store") != {"backend": "sqlite", "path": "braid.db"}
                or config.get("index", {}).get("nodes", {}).get("context_excerpt") != ["text"]):
            raise BraidError("context_config_mismatch")
        return cls._open(binary, generation_dir, dataset_id, profile_id,
                         config_name="braid-context.json", **kwargs)

    @classmethod
    def open_existing(cls, binary: Path, generation_dir: Path, dataset_id: str, profile_id: str, **kwargs):
        generation_dir = Path(generation_dir).resolve()
        if not (generation_dir / "braid.db").is_file():
            raise BraidError("generation_unavailable")
        config = json.loads((generation_dir / "braid.json").read_text())
        if (config.get("dataset_id") != dataset_id or config.get("embed") != {"provider": "none", "model": profile_id}
                or config.get("store") != {"backend": "sqlite", "path": "braid.db"}):
            raise BraidError("dataset_mismatch")
        return cls._open(binary, generation_dir, dataset_id, profile_id, **kwargs)

    @classmethod
    def _open(cls, binary, generation_dir, dataset_id, profile_id, *, config_name="braid.json", ownership_fd=None, **kwargs):
        candidate = str(binary)
        if not Path(candidate).is_absolute() and len(Path(candidate).parts) == 1:
            candidate = shutil.which(candidate)
            if candidate is None:
                raise BraidError("executable_unavailable")
        binary = Path(candidate).resolve()
        with binary.open("rb") as file:
            digest = hashlib.file_digest(file, "sha256").hexdigest()
        transport = StdioTransport([str(binary), "serve", "--stdio", "--config", str(generation_dir / config_name)],
                                   cwd=generation_dir, ownership_fd=ownership_fd)
        try:
            return cls(transport, dataset_id=dataset_id, profile_id=profile_id,
                       generation_dir=generation_dir, binary_digest=digest, **kwargs)
        except BaseException:
            try:
                transport.close()
            except Exception:
                pass  # Preserve the negotiation/startup failure, stage abandoned.
            raise

    def _state(self, value):
        if (not isinstance(value, dict) or value.get("id") != self.dataset_id
                or type(value.get("revision")) is not int or value["revision"] < 0
                or not isinstance(value.get("digest"), str) or not value["digest"]):
            raise BraidError("dataset_mismatch")
        return dict(value)

    def _envelope(self, method, payload):
        return {"id": str(self._sequence + 1), "method": method, **payload}

    def request(self, method: str, **payload):
        deadline = time.monotonic() + self.timeout
        acquired = self._request_lock.acquire(timeout=max(0, deadline - time.monotonic()))
        if not acquired:
            self.poisoned = True
            raise BraidError("transport_timeout")
        try:
            return self._request(method, deadline=deadline, **payload)
        except BaseException:
            # Late responses cannot be paired with another caller's request.
            self.poisoned = True
            raise
        finally:
            self._request_lock.release()

    def _request(self, method: str, *, deadline: float, **payload):
        if self.poisoned or self.closed:
            raise BraidError("session_abandoned")
        if "id" in payload or "method" in payload:
            raise BraidError("invalid_request")
        envelope = self._envelope(method, payload)
        encoded = _encode(envelope)
        if len(encoded) >= self.max_request_bytes:
            raise BraidError("request_too_large")
        self._sequence += 1
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BraidError("transport_timeout")
        raw = self.transport.exchange(encoded, timeout=remaining, max_response_bytes=self.max_response_bytes)
        if len(raw) > self.max_response_bytes:
            raise BraidError("response_too_large")
        try:
            response = json.loads(raw, object_pairs_hook=_unique_object,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
        except (ValueError, UnicodeError):
            raise BraidError("protocol_fault") from None
        if (not isinstance(response, dict) or type(response.get("protocol_version")) is not int
                or response["protocol_version"] != 1
                or response.get("id") != envelope["id"]
                or ("result" in response) == ("error" in response)):
            raise BraidError("protocol_fault")
        if "error" in response:
            error = response["error"]
            code = error.get("code") if isinstance(error, dict) else None
            if not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code):
                raise BraidError("protocol_fault")
            raise BraidError(code)
        if not isinstance(response["result"], dict):
            raise BraidError("protocol_fault")
        return response["result"]

    def _update(self, batch):
        return {"dataset_id": self.dataset_id, "expected_revision": self.state["revision"],
                "upsert": batch, "delete_nodes": [], "delete_edges": []}

    def _journal(self, value):
        if self.generation_dir:
            fd = os.open(self.generation_dir / "braid-journal.jsonl",
                         os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "ab") as file:
                file.write(_encode(value) + b"\n")
                file.flush()
                os.fsync(file.fileno())

    def _apply(self, batch):
        try:
            update = self._update(batch)
            self._journal({"phase": "attempt", "revision": self.state["revision"],
                           "request_digest": hashlib.sha256(_encode(update)).hexdigest()})
            state = self._state(self.request("apply", update=update))
            delta = state["revision"] - self.state["revision"]
            if delta not in (0, 1) or (delta == 0) != (state["digest"] == self.state["digest"]):
                raise BraidError("revision_invalid")
            self.state = state
            self._journal({"phase": "committed", "dataset": state})
        except BaseException:
            self.poisoned = True
            raise

    def _batches(self, field: str, records: Iterable[dict]):
        if self.closed or self.poisoned:
            raise BraidError("session_abandoned")
        batch = {"nodes": [], "edges": [], "embeddings": []}
        for record in records:
            if not isinstance(record, dict):
                raise ValueError("Braid records must be objects")
            if field == "embeddings" and record.get("model") != self.profile_id:
                raise BraidError("embedding_model_mismatch")
            batch[field].append(record)
            size = len(_encode(self._envelope("apply", {"update": self._update(batch)})))
            if size >= self.max_request_bytes:
                batch[field].pop()
                if not batch[field]:
                    raise BraidError("record_too_large")
                self._apply(batch)
                batch = {"nodes": [], "edges": [], "embeddings": []}
                batch[field].append(record)
                if len(_encode(self._envelope("apply", {"update": self._update(batch)}))) >= self.max_request_bytes:
                    raise BraidError("record_too_large")
        if batch[field]:
            self._apply(batch)

    def publish_nodes(self, nodes: Iterable[dict]):
        if self._phase != "nodes":
            raise BraidError("invalid_publish_order")
        self._publish("nodes", nodes)

    def publish_embeddings(self, embeddings: Iterable[dict]):
        if self._phase == "edges":
            raise BraidError("invalid_publish_order")
        self._phase = "embeddings"
        self._publish("embeddings", embeddings)

    def publish_edges(self, edges: Iterable[dict]):
        self._phase = "edges"
        self._publish("edges", edges)

    def _publish(self, field, records):
        try:
            self._batches(field, records)
        except BaseException:
            # A source generator may fail after earlier batches committed.
            self.poisoned = True
            raise

    def finish(self):
        state = self._state(self.request("dataset"))
        if state != self.state:
            self.poisoned = True
            raise BraidError("revision_conflict")
        self.close()
        if self.generation_dir:
            # The child has exited. Checkpoint in place; never copy an open
            # main database file or remove another reader's WAL sidecars.
            database = self.generation_dir / "braid.db"
            if not database.is_file():
                raise BraidError("generation_unavailable")
            conn = sqlite3.connect(database, timeout=self.timeout)
            try:
                if conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] != 0:
                    raise BraidError("checkpoint_busy")
            finally:
                conn.close()
            with database.open("rb") as file:
                os.fsync(file.fileno())
            fd = os.open(self.generation_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        return state

    def close(self):
        if not self.closed:
            self.closed = True
            self.transport.close()
