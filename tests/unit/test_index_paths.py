"""Derived artifacts must never escape the configured ignored data tree."""

import pytest

from muninn.config import load_paths
from muninn.index.paths import load_index_paths


@pytest.mark.parametrize("custom", [False, True])
def test_default_artifacts_resolve_under_data_dir(monkeypatch, tmp_path, custom):
    monkeypatch.delenv("MUNINN_DATA_DIR", raising=False)
    if custom:
        monkeypatch.setenv("MUNINN_DATA_DIR", str(tmp_path / "private-data"))
    paths = load_index_paths()
    artifacts = [paths.root, paths.embedding_cache_dir, paths.generations_dir,
                 paths.staging_dir, paths.build_dir,
                 paths.generation_dir("synthetic-001"),
                 paths.generation_dir("synthetic-001", staging=True),
                 paths.manifest_path("synthetic-001"),
                 paths.manifest_path("synthetic-001", staging=True)]
    assert all(path.resolve().is_relative_to(load_paths().data_dir.resolve())
               for path in artifacts)
    assert not paths.root.exists()


@pytest.mark.parametrize("identifier", ["", ".", "..", "../outside", "/tmp/out", "a/b", "a.b", "a" * 129])
def test_generation_paths_reject_unsafe_identifiers(identifier):
    with pytest.raises(ValueError, match="generation identifier"):
        load_index_paths().manifest_path(identifier)
