"""OS locks and synthetic private generation paths only; no service or socket."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from muninn.index.ownership import generation_lock, GenerationBusy
from muninn.index.paths import IndexPaths


def test_shared_readers_block_exclusive_cleanup_and_inode_is_retained(tmp_path):
    paths = IndexPaths(tmp_path / "index")
    first = generation_lock("synthetic", paths=paths)
    second = generation_lock("synthetic", paths=paths)
    lock_path = paths.root / "leases" / "synthetic.lock"
    inode = lock_path.stat().st_ino
    with pytest.raises(GenerationBusy):
        generation_lock("synthetic", exclusive=True, paths=paths)
    first.close()
    with pytest.raises(GenerationBusy):
        generation_lock("synthetic", exclusive=True, paths=paths)
    second.close()
    with generation_lock("synthetic", exclusive=True, paths=paths):
        with pytest.raises(GenerationBusy):
            generation_lock("synthetic", paths=paths)
    assert lock_path.stat().st_ino == inode


def test_process_exit_releases_ownership_without_pid_takeover(tmp_path):
    paths = IndexPaths(tmp_path / "index")
    script = """
import sys
from pathlib import Path
from muninn.index.ownership import generation_lock
from muninn.index.paths import IndexPaths
with generation_lock('synthetic', paths=IndexPaths(Path(sys.argv[1]))):
    print('ready', flush=True)
    sys.stdin.readline()
"""
    child = subprocess.Popen([sys.executable, "-c", script, str(paths.root)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "ready"
        with pytest.raises(GenerationBusy):
            generation_lock("synthetic", paths=paths, exclusive=True)
        child.stdin.write("release\n")
        child.stdin.flush()
        assert child.wait(timeout=5) == 0
        with generation_lock("synthetic", paths=paths, exclusive=True):
            pass
    finally:
        if child.poll() is None:
            child.terminate()  # Only the exact subprocess started by this test.
            child.wait(timeout=5)
        child.stdin.close()
        child.stdout.close()


def test_lock_is_cloexec_and_rejects_path_escape_and_symlink(tmp_path):
    paths = IndexPaths(tmp_path / "index")
    with generation_lock("synthetic", paths=paths) as lease:
        assert not os.get_inheritable(lease.fd)
    with pytest.raises(ValueError):
        generation_lock("../escape", paths=paths)
    (paths.root / "leases" / "evil.lock").symlink_to(tmp_path / "target")
    with pytest.raises(OSError):
        generation_lock("evil", paths=paths)


def test_managed_child_keeps_ownership_after_parent_exit(tmp_path):
    """Explicit inherited fd covers the parent-death/child-stdin-EOF interval."""
    import time
    paths = IndexPaths(tmp_path / "index")
    release = tmp_path / "release-child"
    holder = """
import os,sys,time
from pathlib import Path
release=Path(sys.argv[1])
end=time.monotonic()+5
while not release.exists() and time.monotonic()<end:
    time.sleep(.01)
os._exit(0)
"""
    owner = """
import subprocess,sys,os
from pathlib import Path
from muninn.index.ownership import generation_lock
from muninn.index.paths import IndexPaths
lease=generation_lock('synthetic', paths=IndexPaths(Path(sys.argv[1])))
p=subprocess.Popen([sys.executable,'-c',sys.argv[3],sys.argv[2]],
    stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
    pass_fds=(lease.fd,))
print(p.pid,flush=True)
os._exit(0)  # Abrupt owner death; do not run the lease destructor/unlock.
"""
    parent = subprocess.Popen([sys.executable, "-c", owner, str(paths.root), str(release), holder],
                              stdout=subprocess.PIPE, text=True)
    try:
        assert int(parent.stdout.readline().strip()) > 0
        assert parent.wait(timeout=5) == 0
        with pytest.raises(GenerationBusy):
            generation_lock("synthetic", exclusive=True, paths=paths)
        release.touch()
        deadline = time.monotonic() + 3
        while True:
            try:
                lease = generation_lock("synthetic", exclusive=True, paths=paths)
                lease.close()
                break
            except GenerationBusy:
                assert time.monotonic() < deadline
                time.sleep(.01)
    finally:
        release.touch()
        parent.stdout.close()
        if parent.poll() is None:
            parent.terminate()
            parent.wait(timeout=5)


def test_stdio_transport_explicitly_inherits_generation_ownership(tmp_path):
    from muninn.index.braid_client import StdioTransport
    paths = IndexPaths(tmp_path / "index")
    lease = generation_lock("synthetic", paths=paths)
    script = "import sys; sys.stdin.readline(); print('{}',flush=True); sys.stdin.read()"
    transport = StdioTransport([sys.executable, "-c", script], ownership_fd=lease.fd)
    try:
        assert transport.exchange(b"{}", timeout=2, max_response_bytes=256) == b"{}"
        lease.close()
        with pytest.raises(GenerationBusy):
            generation_lock("synthetic", exclusive=True, paths=paths)
    finally:
        lease.close()
        transport.close()
    with generation_lock("synthetic", exclusive=True, paths=paths):
        pass
