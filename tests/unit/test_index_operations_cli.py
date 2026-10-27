"""Synthetic CLI contracts; no subprocess binary, model or canonical data."""
import json
from types import SimpleNamespace

from click.testing import CliRunner

from muninn.consumers.cli.index import index_group


def test_explicit_sync_builds_without_activating(monkeypatch):
    import muninn.index.publisher as publisher
    calls = []
    monkeypatch.setattr(publisher, "build_generation", lambda *a, **kw: (
        calls.append(kw) or SimpleNamespace(generation_id="fake", source_count=1, chunk_count=1)))
    monkeypatch.setattr(publisher, "activate_generation", lambda *a: (_ for _ in ()).throw(AssertionError("activation forbidden")))
    result = CliRunner().invoke(index_group, ["sync", "--backend", "braid", "--binary", "/synthetic/braid", "--namespace", "bookmarks"], obj={})
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["state"] == "ready"
    assert calls[0]["namespaces"] == ("bookmarks",)


def test_sync_requires_absolute_binary(monkeypatch):
    monkeypatch.delenv("MUNINN_BRAID_BINARY", raising=False)
    result = CliRunner().invoke(index_group, ["sync", "--binary", "braid"], obj={})
    assert result.exit_code != 0
    assert "ValueError" in result.output


def test_worker_once_does_not_enqueue_or_activate(monkeypatch):
    from muninn.index import worker
    monkeypatch.setenv("MUNINN_BRAID_BINARY", "/synthetic/braid")
    monkeypatch.setattr(worker, "run_once", lambda *a, **kw: worker.PublisherStatus("fake", "ready", None, "generation", None, 1))
    monkeypatch.setattr(worker, "enqueue", lambda *a: (_ for _ in ()).throw(AssertionError("enqueue forbidden")))
    result = CliRunner().invoke(index_group, ["worker", "--once"], obj={})
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["state"] == "ready"


def test_index_status_and_command_inventory(tmp_path):
    from muninn.db import init_db
    path = tmp_path / "synthetic.db"
    init_db(path).close()
    result = CliRunner().invoke(index_group, ["status"], obj={"db": path})
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["backend_observation"] == "no_request_observation"
    assert set(index_group.commands) == {"sync", "status", "activate", "rollback", "worker"}
