"""Complete, bounded, read-only authored inventory; failures never mean deletion."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from muninn.consumers.mcp import vault_fs
from muninn.consumers.packets.canonical import TEXT_NORMALIZATION, content_digest, normalize_text


def _signature(st):
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


def _open_directory(wiki: Path, relative: Path) -> int:
    fd = os.open(wiki.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in (*wiki.parts[1:], *relative.parts):
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


@dataclass(frozen=True)
class AuthoredSource:
    path: str
    namespace: str
    file_digest: str
    body_digest: str
    body: str
    # Offset in decoded source, before normalization; citations use normalized
    # body offsets, never pretend the frontmatter/title is inside the body.
    source_body_start: int
    normalization: str
    title: str
    bookmark_id: int | None
    validation_stamp: tuple[int, ...] = field(repr=False, compare=False)


@dataclass(frozen=True)
class Inventory:
    sources: tuple[AuthoredSource, ...]
    namespaces: tuple[tuple[str, str], ...]
    omissions: tuple[tuple[str, str], ...]
    complete: bool

    @property
    def digest(self) -> str:
        # File bytes bind all derived body/title fields. Avoid serializing every
        # full body again merely to hash the inventory, and exclude stat stamps.
        payload = json.dumps({
            "sources": [(s.path, s.file_digest) for s in self.sources],
            "namespaces": self.namespaces, "omissions": self.omissions,
            "complete": self.complete,
        }, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


def _read(wiki: Path, rel: str) -> tuple[bytes, str, tuple[int, ...]]:
    # Apply existing serving guards, then use no-follow directory descriptors
    # for the digest read so replacing a parent with a symlink cannot escape.
    _, served = vault_fs.read_page(wiki, rel)
    parts = vault_fs.page_parts(rel)[1:]
    fd = _open_directory(wiki, Path())
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
        try:
            before = os.fstat(file_fd)
            with os.fdopen(os.dup(file_fd), "rb") as stream:
                data = stream.read(vault_fs.MAX_PAGE_BYTES + 1)
            after = os.fstat(file_fd)
        finally:
            os.close(file_fd)
    finally:
        os.close(fd)
    if len(data) > vault_fs.MAX_PAGE_BYTES:
        raise ValueError("too_large")
    resolved = vault_fs.resolve_page(wiki, rel)
    if _signature(before) != _signature(after) or _signature(after) != _signature(resolved.stat()):
        raise ValueError("source_changed")
    text = data.decode("utf-8", errors="replace")
    if text != served:
        raise ValueError("source_changed")
    return data, text, _signature(after)


def resolve_authored(
    vault_dir: Path, personal_dir: Path | None, rel: str, *,
    row_visible: Callable[[int], bool] | None = None,
    expected_digest: str | None = None,
) -> AuthoredSource | None:
    """Exact hydration: recheck path, bytes and current bookmark visibility.

    Raises on unavailable scans/unsafe files; returns None for hidden or changed
    sources. Callers must treat either outcome as unavailable, never deleted.
    """
    wiki = vault_fs.resolve_wiki_dir(vault_dir, personal_dir)
    parts = vault_fs.page_parts(rel)
    namespace = vault_fs.namespace_of(rel)
    if namespace not in vault_fs.AUTHORED_NAMESPACES:
        raise ValueError("not_authored")
    candidate = wiki
    for component in parts[1:]:
        candidate = candidate / component
        if candidate.is_symlink():
            raise ValueError("symlink")
    data, text, validation_stamp = _read(wiki, rel)
    digest = hashlib.sha256(data).hexdigest()
    if expected_digest is not None and digest != expected_digest:
        return None
    info = vault_fs.page_info(wiki, rel, text)
    def checked_visibility(bookmark_id):
        if row_visible is None:
            raise ValueError("visibility_unavailable")
        try:
            return row_visible(bookmark_id)
        except Exception as exc:
            raise ValueError("visibility_unavailable") from exc
    if not vault_fs.page_visible(namespace, info.bookmark_id, checked_visibility):
        return None
    _, raw_body = vault_fs.split_frontmatter(text)
    body = normalize_text(raw_body)
    return AuthoredSource(rel, namespace, digest, content_digest(body), body,
                         len(text) - len(raw_body), TEXT_NORMALIZATION, info.title, info.bookmark_id,
                         validation_stamp)


def inventory_authored(
    vault_dir: Path, personal_dir: Path | None = None, *,
    row_visible: Callable[[int], bool] | None = None,
    max_entries: int = 100_000, max_files: int = 100_000,
    max_total_bytes: int = 1024 * 1024 * 1024,
) -> Inventory:
    """Enumerate authored namespaces directly, with every exclusion reported.

    ``complete`` means a complete inventory under the declared inclusion policy.
    Hidden/symlink/non-Markdown entries are intentional omissions; bounds,
    unreadable files, failed visibility checks and changed directories are
    incomplete. Only a complete inventory can support a remove/add comparison.
    """
    if min(max_entries, max_files, max_total_bytes) < 1:
        raise ValueError("inventory bounds must be positive")
    try:
        wiki = vault_fs.resolve_wiki_dir(vault_dir, personal_dir)
    except (OSError, vault_fs.VaultAccessError) as exc:
        return Inventory((), (), (("wiki", getattr(exc, "code", "scan_failed")),), False)
    try:
        root_stamp = _signature(wiki.lstat())
    except OSError:
        return Inventory((), (), (("wiki", "scan_failed"),), False)
    sources, omissions, namespaces, directories = [], [], [], [(wiki, root_stamp)]
    complete = True
    entries_seen = files_seen = total_bytes = 0
    stopped = False
    for namespace in vault_fs.AUTHORED_NAMESPACES:
        top = wiki / namespace
        if stopped:
            namespaces.append((namespace, "unscanned"))
            continue
        if top.is_symlink():
            namespaces.append((namespace, "excluded_symlink"))
            omissions.append((f"wiki/{namespace}", "symlink"))
            continue
        if not top.exists():
            namespaces.append((namespace, "absent"))
            directories.append((top, None))
            continue
        if not top.is_dir():
            namespaces.append((namespace, "unavailable"))
            omissions.append((f"wiki/{namespace}", "not_directory"))
            complete = False
            continue
        namespaces.append((namespace, "present"))
        stack = [top]
        while stack and not stopped:
            directory = stack.pop()
            directory_fd = None
            try:
                before = directory.lstat()
                directory_fd = _open_directory(wiki, directory.relative_to(wiki))
                if _signature(before) != _signature(os.fstat(directory_fd)):
                    raise ValueError("directory_changed")
                entries = []
                with os.scandir(directory_fd) as scan:
                    for entry in scan:
                        entries_seen += 1
                        if entries_seen > max_entries:
                            raise ValueError("max_entries")
                        entries.append(entry)
                directories.append((directory, _signature(before)))
                for entry in sorted(entries, key=lambda e: e.name):
                    entry_path = directory / entry.name
                    rel = "wiki/" + entry_path.relative_to(wiki).as_posix()
                    if entry.name.startswith(".") or entry.is_symlink():
                        omissions.append((rel, "hidden" if entry.name.startswith(".") else "symlink"))
                    elif entry.is_dir(follow_symlinks=False):
                        stack.append(entry_path)
                    elif not entry.is_file(follow_symlinks=False) or not entry.name.endswith(".md"):
                        omissions.append((rel, "not_markdown"))
                    else:
                        files_seen += 1
                        if files_seen > max_files:
                            raise ValueError("max_files")
                        size = entry.stat(follow_symlinks=False).st_size
                        if total_bytes + size > max_total_bytes:
                            raise ValueError("max_total_bytes")
                        total_bytes += size
                        try:
                            source = resolve_authored(vault_dir, personal_dir, rel, row_visible=row_visible)
                            if source is None:
                                omissions.append((rel, "not_visible"))
                            else:
                                sources.append(source)
                        except (OSError, ValueError, vault_fs.VaultAccessError) as exc:
                            omissions.append((rel, getattr(exc, "code", str(exc) if isinstance(exc, ValueError) else "read_failed")))
                            complete = False
            except (OSError, ValueError) as exc:
                omissions.append(("wiki/" + directory.relative_to(wiki).as_posix(), str(exc) if isinstance(exc, ValueError) else "scan_failed"))
                complete = False
                stopped = isinstance(exc, ValueError)
            finally:
                if directory_fd is not None:
                    os.close(directory_fd)
    for directory, signature in directories:
        try:
            changed = directory.exists() if signature is None else _signature(directory.lstat()) != signature
            if changed:
                omissions.append(("wiki/" + directory.relative_to(wiki).as_posix(), "directory_changed"))
                complete = False
        except OSError:
            omissions.append(("wiki/" + directory.relative_to(wiki).as_posix(), "scan_failed"))
            complete = False
    # A write to an existing file does not change its parent directory's mtime.
    # Revalidate every captured file too, after finishing the entire traversal.
    for source in sources:
        try:
            file = vault_fs.resolve_page(wiki, source.path)
            if _signature(file.stat()) != source.validation_stamp:
                omissions.append((source.path, "source_changed"))
                complete = False
        except (OSError, vault_fs.VaultAccessError):
            omissions.append((source.path, "source_changed"))
            complete = False
    return Inventory(tuple(sorted(sources, key=lambda s: s.path)), tuple(namespaces), tuple(sorted(omissions)), complete)
