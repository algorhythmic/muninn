"""Cross-process ownership of private generation files using stable flock inodes.

Lease files are deliberately retained forever: unlinking a locked inode would
allow a second process to acquire a different inode for the same generation.
No PID or expiry timestamp authorizes reclaiming a live reader's files.
"""
from __future__ import annotations

import fcntl
import os
import stat

from muninn.index.paths import IndexPaths, load_index_paths


class GenerationBusy(RuntimeError):
    code = "generation_in_use"


class GenerationLock:
    def __init__(self, fd):
        self.fd = fd

    def close(self):
        if self.fd is not None:
            fd, self.fd = self.fd, None
            # Close-only: an inherited managed-child fd must keep the same
            # open-file-description lock until the child has also exited.
            os.close(fd)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def generation_lock(generation_id: str, *, exclusive=False, blocking=False,
                    paths: IndexPaths | None = None) -> GenerationLock:
    paths = paths or load_index_paths()
    paths.generation_dir(generation_id)  # Shared identifier validation.
    directory = paths.root / "leases"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        fd = os.open(generation_id + ".lock",
                     os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                     dir_fd=directory_fd)
    finally:
        os.close(directory_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("generation lease must be a private regular file")
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        if not blocking:
            operation |= fcntl.LOCK_NB
        try:
            fcntl.flock(fd, operation)
        except BlockingIOError:
            raise GenerationBusy("generation_in_use") from None
        return GenerationLock(fd)
    except BaseException:
        os.close(fd)
        raise
