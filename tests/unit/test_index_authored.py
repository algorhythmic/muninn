"""Authored inventory tests use only temporary synthetic Markdown."""

import hashlib

import pytest

from muninn.index.authored import inventory_authored, resolve_authored


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "vault"
    (root / "wiki" / "concepts").mkdir(parents=True)
    return root


def page(vault, path="concepts/a.md", text="# Synthetic\n\nBody"):
    file = vault / "wiki" / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_bytes(text.encode())
    return file


def test_direct_complete_inventory_digest_and_exact_hydration(vault):
    raw = "---\ntitle: Synthetic heading\n---\n\n# Body heading\r\n\nSynthetic body\n"
    file = page(vault, text=raw)
    page(vault, "projects/reference.md")
    page(vault, "bookmarks/compiled.md")
    result = inventory_authored(vault)
    assert result.complete
    assert len(result.sources) == 2
    source = result.sources[0]
    assert source.file_digest == hashlib.sha256(file.read_bytes()).hexdigest()
    assert source.body == "# Body heading\n\nSynthetic body"
    assert raw[source.source_body_start:].startswith("\n# Body heading")
    assert dict(result.namespaces)["people"] == "absent"
    assert result.digest == inventory_authored(vault).digest
    assert resolve_authored(vault, None, source.path, expected_digest=source.file_digest) == source
    file.write_text("Changed body")
    assert resolve_authored(vault, None, source.path, expected_digest=source.file_digest) is None


def test_exclusions_and_bookmark_visibility(vault):
    page(vault, text="---\nbookmark_id: 7\n---\nHidden derived note")
    page(vault, "concepts/.hidden.md")
    page(vault, "concepts/not-markdown.txt")
    (vault / "wiki/concepts/symlink.md").symlink_to(vault / "wiki/concepts/a.md")
    result = inventory_authored(vault, row_visible=lambda _: False)
    assert result.complete
    assert not result.sources
    assert {reason for _, reason in result.omissions} == {"not_visible", "hidden", "symlink", "not_markdown"}
    assert len(inventory_authored(vault, row_visible=lambda bid: bid == 7).sources) == 1
    with pytest.raises(ValueError, match="not_authored"):
        resolve_authored(vault, None, "wiki/bookmarks/compiled.md")


@pytest.mark.parametrize("bounds", [{"max_files": 1}, {"max_entries": 1}, {"max_total_bytes": 1}])
def test_bounds_are_explicit_and_never_complete(vault, bounds):
    page(vault)
    page(vault, "concepts/b.md")
    result = inventory_authored(vault, **bounds)
    assert not result.complete
    assert any(reason.startswith("max_") for _, reason in result.omissions)


def test_scan_failure_distinct_from_missing_namespace(vault, monkeypatch):
    import muninn.index.authored as module
    real = module.os.scandir
    def failing(path):
        raise PermissionError("synthetic")
    monkeypatch.setattr(module.os, "scandir", failing)
    result = inventory_authored(vault)
    assert not result.complete
    assert ("wiki/concepts", "scan_failed") in result.omissions
    assert dict(result.namespaces)["people"] == "absent"


def test_personal_vault_guard_and_oversize_failure(vault):
    assert not inventory_authored(vault, personal_dir=vault).complete
    page(vault, text="x" * (1024 * 1024 + 1))
    result = inventory_authored(vault)
    assert not result.complete
    assert ("wiki/concepts/a.md", "too_large") in result.omissions


def test_visibility_scan_failure_is_incomplete(vault):
    page(vault, text="---\nbookmark_id: 7\n---\nSynthetic")
    def unavailable(_):
        raise RuntimeError("synthetic unavailable database")
    result = inventory_authored(vault, row_visible=unavailable)
    assert not result.complete
    assert ("wiki/concepts/a.md", "visibility_unavailable") in result.omissions


def test_directory_replaced_with_symlink_cannot_mean_complete_empty_inventory(vault, tmp_path, monkeypatch):
    import muninn.index.authored as module
    page(vault)
    outside = tmp_path / "outside"
    outside.mkdir()
    original = module._open_directory
    changed = False
    def swap(wiki, relative):
        nonlocal changed
        if not changed:
            changed = True
            (wiki / "concepts").rename(wiki / "old-concepts")
            (wiki / "concepts").symlink_to(outside, target_is_directory=True)
        return original(wiki, relative)
    monkeypatch.setattr(module, "_open_directory", swap)
    result = inventory_authored(vault)
    assert not result.complete
    assert ("wiki/concepts", "scan_failed") in result.omissions


def test_file_change_during_exact_read_is_incomplete(vault, monkeypatch):
    import muninn.index.authored as module
    file = page(vault)
    original = module.vault_fs.read_page
    def change(*args):
        result = original(*args)
        file.write_text("Changed while reading")
        return result
    monkeypatch.setattr(module.vault_fs, "read_page", change)
    result = inventory_authored(vault)
    assert not result.complete
    assert ("wiki/concepts/a.md", "source_changed") in result.omissions
