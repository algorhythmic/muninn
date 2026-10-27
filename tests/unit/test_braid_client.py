"""Protocol fakes and stdlib subprocesses only; no Braid/model/service required."""
import json
import sqlite3
import sys

import pytest

from muninn.index.braid_client import BraidClient, BraidError, REQUIRED_CAPABILITIES, StdioTransport


class FakeTransport:
    def __init__(self, limit=1000):
        self.limit = limit
        self.requests = []
        self.state = {"id": "synthetic", "revision": 0, "digest": "initial"}
        self.closed = False
        self.fail = None
        self.capabilities = list(REQUIRED_CAPABILITIES)

    def exchange(self, request, **kwargs):
        value = json.loads(request)
        self.requests.append((request, value))
        if self.fail:
            return self.fail(value) if callable(self.fail) else self.fail
        method = value["method"]
        result = self.state.copy()
        if method == "hello":
            result = {"protocol_version": 1, "capabilities": self.capabilities,
                "max_request_bytes": self.limit, "ranking_version": "synthetic-ranking", "dataset": result}
        elif method == "apply":
            assert value["update"]["expected_revision"] == self.state["revision"]
            self.state["revision"] += 1
            self.state["digest"] = str(self.state["revision"])
            result = self.state.copy()
        return json.dumps({"protocol_version": 1, "id": value["id"], "result": result}).encode()

    def close(self):
        self.closed = True


def client(fake=None, **kwargs):
    return BraidClient(fake or FakeTransport(), dataset_id="synthetic", profile_id="fake-profile", **kwargs)


def test_encoded_byte_batches_chain_revisions_nodes_vectors_then_edges(tmp_path):
    fake = FakeTransport(800)
    instance = client(fake, generation_dir=tmp_path)
    instance.publish_nodes({"id": str(i), "type": "chunk", "text": "🦊" * 30} for i in range(10))
    instance.publish_embeddings({"node_id": str(i), "model": "fake-profile", "vec": [1.0, 0.0]} for i in range(10))
    instance.publish_edges([{"src": "0", "dst": "1", "type": "contains", "weight": 1.0}])
    applies = [(raw, req) for raw, req in fake.requests if req["method"] == "apply"]
    assert len(applies) > 3
    assert all(len(raw) < 800 for raw, _ in applies)
    assert [req["update"]["expected_revision"] for _, req in applies] == list(range(len(applies)))
    assert sum(len(req["update"]["upsert"]["nodes"]) for _, req in applies) == 10
    assert sum(len(req["update"]["upsert"]["embeddings"]) for _, req in applies) == 10
    assert applies[-1][1]["update"]["upsert"]["edges"]
    journal = [json.loads(line) for line in (tmp_path / "braid-journal.jsonl").read_text().splitlines()]
    assert len(journal) == len(applies) * 2
    assert journal[-1]["dataset"] == instance.state
    assert (tmp_path / "braid-journal.jsonl").stat().st_mode & 0o777 == 0o600


def test_negotiation_requires_revision1_extension():
    fake = FakeTransport()
    fake.capabilities.remove("query_embedding")
    with pytest.raises(BraidError, match="capability_mismatch"):
        client(fake)


def test_bound_dataset_checked_before_writes():
    fake = FakeTransport()
    fake.state["id"] = "other"
    with pytest.raises(BraidError, match="dataset_mismatch"):
        client(fake)
    assert len(fake.requests) == 1


@pytest.mark.parametrize("code", ["operation_failed", "revision_conflict", "dataset_mismatch"])
def test_mutation_error_abandons_without_retry(code):
    fake = FakeTransport()
    instance = client(fake)
    fake.fail = lambda req: json.dumps({"protocol_version": 1, "id": req["id"],
        "error": {"code": code, "message": "sensitive upstream diagnostic"}}).encode()
    with pytest.raises(BraidError) as error:
        instance.publish_nodes([{"id": "1"}])
    assert str(error.value) == code
    assert instance.poisoned
    with pytest.raises(BraidError, match="session_abandoned"):
        instance.publish_nodes([{"id": "1"}])
    assert len(fake.requests) == 2


@pytest.mark.parametrize("raw", [b"not json", b'{"id":"2","id":"2"}',
    b'{"protocol_version":1,"id":"wrong","result":{}}',
    b'{"protocol_version":1,"id":"2","result":{},"error":{}}',
    b'{"protocol_version":1,"id":"2","result":{"x":NaN}}'])
def test_malformed_mutation_response_abandons(raw):
    fake = FakeTransport()
    instance = client(fake)
    fake.fail = raw
    with pytest.raises(BraidError, match="protocol_fault"):
        instance.publish_nodes([{"id": "1"}])
    assert instance.poisoned


def test_local_limits_and_profile_validation():
    instance = client(FakeTransport(400))
    with pytest.raises(BraidError, match="record_too_large"):
        instance.publish_nodes([{"id": "1", "text": "x" * 1000}])
    with pytest.raises(BraidError, match="session_abandoned"):
        instance.finish()
    with pytest.raises(BraidError, match="embedding_model_mismatch"):
        client().publish_embeddings([{"model": "wrong", "vec": [1]}])


def test_edges_prevent_later_nodes_or_vectors():
    instance = client()
    instance.publish_edges([])
    with pytest.raises(BraidError, match="invalid_publish_order"):
        instance.publish_nodes([])
    with pytest.raises(BraidError, match="invalid_publish_order"):
        instance.publish_embeddings([])


def test_noop_update_keeps_revision_and_finish_closes():
    fake = FakeTransport()
    instance = client(fake)
    fake.fail = lambda req: json.dumps({"protocol_version": 1, "id": req["id"], "result": fake.state}).encode()
    instance.publish_nodes([{"id": "already-present"}])
    assert instance.finish()["revision"] == 0
    assert fake.closed


def test_transport_pipe_echo_and_response_bound():
    transport = StdioTransport([sys.executable, "-I", "-c", "import sys; [print(s.strip(), flush=True) for s in sys.stdin]"])
    try:
        assert transport.exchange(b'{"synthetic":1}', timeout=1, max_response_bytes=100) == b'{"synthetic":1}'
        with pytest.raises(BraidError, match="response_too_large"):
            transport.exchange(b"x" * 200, timeout=1, max_response_bytes=100)
    finally:
        transport.close()


def test_transport_watchdog_and_eof():
    transport = StdioTransport([sys.executable, "-I", "-c", "import sys,time; sys.stdin.readline(); time.sleep(.1)"])
    try:
        with pytest.raises(BraidError, match="transport_timeout"):
            transport.exchange(b"{}", timeout=.01, max_response_bytes=100)
    finally:
        transport.close()


def test_transport_eof():
    transport = StdioTransport([sys.executable, "-I", "-c", "pass"])
    try:
        with pytest.raises(BraidError, match="transport_closed"):
            transport.exchange(b"{}", timeout=1, max_response_bytes=100)
    finally:
        transport.close()


def test_source_generator_failure_abandons_partial_generation():
    fake = FakeTransport(500)
    instance = client(fake)
    def records():
        for i in range(5):
            yield {"id": str(i), "text": "x" * 100}
        raise OSError("synthetic unavailable source")
    with pytest.raises(OSError):
        instance.publish_nodes(records())
    assert any(req["method"] == "apply" for _, req in fake.requests)
    assert instance.poisoned
    with pytest.raises(BraidError, match="session_abandoned"):
        instance.finish()


def test_create_relocate_reopen_and_checkpoint(tmp_path, monkeypatch):
    import muninn.index.braid_client as module

    binary = tmp_path / "synthetic-binary"
    binary.write_bytes(b"synthetic identity; never executed")
    captures = []
    def transport(command, *, cwd):
        captures.append((command, cwd))
        config = json.loads((cwd / "braid.json").read_text())
        assert config["store"]["path"] == "braid.db"
        assert config["embed"] == {"provider": "none", "model": "fake-profile"}
        assert config["candidate_limit"] == 512
        assert config["defaults"]["mmr_lambda"] == 1
        with sqlite3.connect(cwd / "braid.db") as db:
            db.execute("CREATE TABLE IF NOT EXISTS synthetic(id INTEGER)")
        return FakeTransport()
    monkeypatch.setattr(module, "StdioTransport", transport)
    stage = tmp_path / "stage"
    instance = BraidClient.create(binary, stage, "synthetic", "fake-profile")
    assert instance.identity["binary_digest"]
    assert instance.finish()["revision"] == 0
    assert (stage / "braid.db").is_file()
    published = tmp_path / "published"
    stage.rename(published)
    reopened = BraidClient.open_existing(binary, published, "synthetic", "fake-profile")
    assert reopened.finish()["revision"] == 0
    assert captures[-1][1] == published
    with pytest.raises(BraidError, match="stage_not_fresh"):
        BraidClient.create(binary, published, "synthetic", "fake-profile")
    with pytest.raises(BraidError, match="dataset_mismatch"):
        BraidClient.open_existing(binary, published, "wrong", "fake-profile")
    names = []
    monkeypatch.setattr(module.shutil, "which", lambda name: names.append(name) or str(binary))
    from_path = BraidClient.create("synthetic-braid", tmp_path / "from-path", "synthetic", "fake-profile")
    from_path.finish()
    assert names == ["synthetic-braid"]


def test_context_configuration_is_separate_and_validated(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(BraidClient, "_open", classmethod(lambda cls, *a, **kw: calls.append(kw) or "fake"))
    directory = tmp_path / "generation"
    BraidClient.create(tmp_path / "unused-binary", directory, "synthetic", "fake-profile")
    sqlite3.connect(directory / "braid.db").close()
    base = json.loads((directory / "braid.json").read_text())
    context_path = directory / "braid-context.json"
    context = json.loads(context_path.read_text())
    assert base["defaults"]["mmr_lambda"] == 1
    assert context["defaults"]["mmr_lambda"] == 0.7
    assert context["index"]["nodes"]["context_excerpt"] == ["text"]
    assert BraidClient.open_context(tmp_path / "unused-binary", directory, "synthetic", "fake-profile") == "fake"
    assert calls[-1]["config_name"] == "braid-context.json"
    assert json.loads((directory / "braid.json").read_text()) == base
    context["store"]["path"] = "../untrusted.db"
    context_path.write_text(json.dumps(context))
    with pytest.raises(BraidError, match="context_config_mismatch"):
        BraidClient.open_context(tmp_path / "unused-binary", directory, "synthetic", "fake-profile")


def test_queue_wait_counts_inside_request_deadline_and_invalidates_session():
    import threading
    fake = FakeTransport()
    instance = client(fake, timeout=.03)
    instance._request_lock.acquire()
    outcome = []
    def queued():
        try:
            instance.request("dataset")
        except BraidError as exc:
            outcome.append(exc.code)
    thread = threading.Thread(target=queued)
    thread.start()
    thread.join(timeout=1)
    instance._request_lock.release()
    assert not thread.is_alive()
    assert outcome == ["transport_timeout"]
    assert instance.poisoned
    assert len(fake.requests) == 1  # hello only; queued call never reached pipe


def test_request_response_pairing_is_serialized_for_concurrent_callers():
    import threading
    import time
    fake = FakeTransport()
    instance = client(fake)
    original = fake.exchange
    active = 0
    overlap = []
    def exchange(*args, **kwargs):
        nonlocal active
        active += 1
        overlap.append(active)
        time.sleep(.005)
        try:
            return original(*args, **kwargs)
        finally:
            active -= 1
    fake.exchange = exchange
    results = []
    threads = [threading.Thread(target=lambda: results.append(instance.request("dataset"))) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=1)
    assert len(results) == 4 and max(overlap) == 1
    assert [req[1]["id"] for req in fake.requests] == [str(i) for i in range(1, 6)]
