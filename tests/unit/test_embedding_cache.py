"""Synthetic vectors only: these tests make no semantic retrieval claim."""

from dataclasses import dataclass
import json

import pytest

from muninn.index.embedding_cache import EmbeddingCache, default_cache_dir


@dataclass
class FakeProfile:
    profile_id: str = "synthetic-space-a"
    dimension: int = 2
    complete: bool = True

    def require_complete(self):
        if not self.complete:
            raise ValueError("incomplete profile")


def test_round_trip_exact_input_and_profile_isolation(tmp_path):
    cache = EmbeddingCache(tmp_path)
    profile = FakeProfile()
    cache.put(profile, "synthetic input", [0.25, -0.5])
    assert cache.get(profile, "synthetic input") == [0.25, -0.5]
    assert cache.get(profile, "synthetic input ") is None
    assert cache.get(FakeProfile(profile_id="synthetic-space-b"), "synthetic input") is None
    assert cache.get(FakeProfile(dimension=3), "synthetic input") is None
    entry = next(tmp_path.rglob("*.json"))
    assert "synthetic input" not in entry.read_text()
    assert "synthetic input" not in str(entry)
    assert entry.stat().st_mode & 0o777 == 0o600
    assert entry.parent.stat().st_mode & 0o777 == 0o700
    assert tmp_path.stat().st_mode & 0o777 == 0o700
    assert not list(tmp_path.rglob(".entry-*"))


@pytest.mark.parametrize("vector", [[], [1], [0, 0], [float("nan"), 1], [float("inf"), 1], [True, 1], ["1", 2]])
def test_invalid_vectors_rejected_before_write(tmp_path, vector):
    with pytest.raises(ValueError):
        EmbeddingCache(tmp_path).put(FakeProfile(), "input", vector)
    assert not list(tmp_path.rglob("*.json"))


@pytest.mark.parametrize("change", [
    {"version": 9}, {"profile_id": "other"}, {"input_digest": "other"},
    {"dimension": 3}, {"encoding": "other"}, {"vector": [0, 0]},
    {"vector": [float("nan"), 1]}, {"vector": None},
])
def test_incompatible_entries_are_misses(tmp_path, change):
    cache = EmbeddingCache(tmp_path)
    cache.put(FakeProfile(), "input", [1, 2])
    path = next(tmp_path.rglob("*.json"))
    data = json.loads(path.read_text())
    data.update(change)
    path.write_text(json.dumps(data))
    assert cache.get(FakeProfile(), "input") is None


@pytest.mark.parametrize("contents", ["not-json", "[]", "null", "{}", b"\xff"])
def test_corrupt_entries_are_misses(tmp_path, contents):
    cache = EmbeddingCache(tmp_path)
    profile = FakeProfile()
    cache.put(profile, "input", [1, 2])
    path = next(tmp_path.rglob("*.json"))
    path.write_bytes(contents if isinstance(contents, bytes) else contents.encode())
    assert cache.get(profile, "input") is None


def test_incomplete_profile_refused(tmp_path):
    cache = EmbeddingCache(tmp_path)
    profile = FakeProfile(complete=False)
    with pytest.raises(ValueError, match="incomplete"):
        cache.get(profile, "input")
    with pytest.raises(ValueError, match="incomplete"):
        cache.put(profile, "input", [1, 2])


def test_failed_replace_preserves_complete_entry_and_cleans_temp(tmp_path, monkeypatch):
    cache = EmbeddingCache(tmp_path)
    profile = FakeProfile()
    cache.put(profile, "input", [1, 2])

    def fail(*args):
        raise OSError("synthetic interruption")

    monkeypatch.setattr("muninn.index.embedding_cache.os.replace", fail)
    with pytest.raises(OSError, match="interruption"):
        cache.put(profile, "input", [3, 4])
    assert cache.get(profile, "input") == [1, 2]
    assert not list(tmp_path.rglob(".entry-*"))


def test_default_cache_resolves_under_data_dir(monkeypatch, tmp_path):
    from muninn.config import load_paths

    monkeypatch.delenv("MUNINN_DATA_DIR", raising=False)
    assert default_cache_dir().is_relative_to(load_paths().data_dir)
    monkeypatch.setenv("MUNINN_DATA_DIR", str(tmp_path / "external-data"))
    assert default_cache_dir() == load_paths().data_dir / "index" / "embedding-cache"
