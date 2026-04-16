"""Container launch + tmux output capture for the synthesis container.

The container runs `claude --dangerously-skip-permissions` inside a tmux
session against the saga-claude-credentials volume (subscription billing).
We never use `claude -p` and never set CLAUDE_CODE_OAUTH_TOKEN — both
silently force API billing (see docs/SAGA_ARCHITECTURE.MD line 141).

This module is responsible for:
1. `docker run` invocation with the credentials volume mounted read-only
2. Capturing the container's tmux pane output
3. Recognizing rate-limit / cap-hit error patterns in that output
4. Surfacing the container ID for audit logging
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from muninn.config import SAGA_CREDENTIALS_VOLUME, SYNTHESIS_WORKSPACE


SYNTHESIS_IMAGE = os.environ.get("MUNINN_SYNTHESIS_IMAGE", "muninn-synthesis:latest")


# Patterns that indicate Claude Max account hit a usage cap / rate limit.
# We scan the captured tmux output for any of these substrings.
CAP_HIT_PATTERNS = (
    re.compile(r"rate.?limit.*exceed", re.IGNORECASE),
    re.compile(r"usage limit.*reached", re.IGNORECASE),
    re.compile(r"approaching.*usage limit", re.IGNORECASE),
    re.compile(r"5.?hour limit", re.IGNORECASE),
    re.compile(r"weekly limit", re.IGNORECASE),
    re.compile(r"please try again later", re.IGNORECASE),
    re.compile(r"quota.*exceeded", re.IGNORECASE),
)


class CapHitError(Exception):
    """Raised when the container output indicates the subscription cap was hit."""


class ContainerLaunchError(Exception):
    """Raised when `docker run` itself fails (non-cap, non-validation)."""


@dataclass
class ContainerResult:
    """Captured outcome of a single container launch."""
    container_id: str | None
    output_json: dict[str, Any] | None
    raw_stdout: str
    raw_stderr: str
    exit_code: int
    cap_hit: bool = False
    cap_hit_evidence: str | None = None
    workspace: Path | None = None


@dataclass
class ContainerLauncher:
    """Wraps `docker run` for the synthesis image.

    `image`, `credentials_volume`, and `extra_args` may be overridden for
    testing. `workspace_root` is where per-task host-side directories
    (input/output/status) get created and bind-mounted into /workspace.
    """
    image: str = SYNTHESIS_IMAGE
    credentials_volume: str = SAGA_CREDENTIALS_VOLUME
    workspace_root: Path = field(default_factory=lambda: Path(SYNTHESIS_WORKSPACE))
    extra_run_args: list[str] = field(default_factory=list)
    docker_bin: str = "docker"

    def __post_init__(self) -> None:
        self.workspace_root = Path(self.workspace_root)

    def launch(self, task_input: dict[str, Any]) -> ContainerResult:
        """Launch the container for a single task attempt and capture output.

        Writes task_input to a per-task workspace, bind-mounts it into the
        container at /workspace, runs the container synchronously, then reads
        back the JSON the container wrote to /workspace/output/<task-id>.json.
        """
        task_id = task_input.get("task_id") or str(uuid.uuid4())
        attempt = task_input.get("attempt", 1)
        workspace = self._prepare_workspace(task_id, attempt, task_input)

        container_name = f"muninn-synthesis-{task_id}-a{attempt}-{uuid.uuid4().hex[:6]}"
        cmd = self._build_docker_cmd(container_name, workspace, task_id)

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=int(os.environ.get("MUNINN_SYNTHESIS_TIMEOUT", "3600")),
            )
        except FileNotFoundError as exc:
            raise ContainerLaunchError(f"docker binary not found: {exc}") from exc

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        combined = stdout + "\n" + stderr

        cap_hit, evidence = detect_cap_hit(combined)

        container_id = _read_container_id(workspace)
        output_json = _read_output_json(workspace, task_id)

        return ContainerResult(
            container_id=container_id or container_name,
            output_json=output_json,
            raw_stdout=stdout,
            raw_stderr=stderr,
            exit_code=proc.returncode,
            cap_hit=cap_hit,
            cap_hit_evidence=evidence,
            workspace=workspace,
        )

    # ── helpers ────────────────────────────────────────────────────────

    def _prepare_workspace(
        self, task_id: str, attempt: int, task_input: dict[str, Any]
    ) -> Path:
        """Create per-task workspace dirs and write the input JSON."""
        ws = self.workspace_root / f"{task_id}-a{attempt}"
        (ws / "input").mkdir(parents=True, exist_ok=True)
        (ws / "output").mkdir(parents=True, exist_ok=True)
        (ws / "status").mkdir(parents=True, exist_ok=True)

        input_path = ws / "input" / f"{task_id}.json"
        with input_path.open("w") as fh:
            json.dump(task_input, fh, indent=2)
        return ws

    def _build_docker_cmd(
        self, container_name: str, workspace: Path, task_id: str
    ) -> list[str]:
        cmd = [
            self.docker_bin, "run",
            "--name", container_name,
            "--rm",
            # Subscription-mode credential mount, READ-ONLY.
            "-v", f"{self.credentials_volume}:/home/vscode/.claude:ro",
            # Bind per-task workspace so input/output/status are visible to host.
            "-v", f"{workspace}:/workspace",
            # Surface the container ID into the workspace for audit logging.
            "--cidfile", str(workspace / "container.cid"),
            # Reject API billing env vars at the entrypoint level (entrypoint.sh
            # also enforces this) by ensuring we never forward them.
            "-e", "TASK_ID=" + task_id,
            "-l", f"muninn.task.id={task_id}",
            *self.extra_run_args,
            self.image,
            f"{task_id}.json",
        ]
        return cmd


def detect_cap_hit(captured_output: str) -> tuple[bool, str | None]:
    """Scan tmux/stdout/stderr for known rate-limit / cap-hit patterns.

    Returns (True, matched_line) if any pattern matches; (False, None) otherwise.
    """
    for line in captured_output.splitlines():
        for pat in CAP_HIT_PATTERNS:
            if pat.search(line):
                return True, line.strip()
    return False, None


def _read_container_id(workspace: Path) -> str | None:
    cid_file = workspace / "container.cid"
    if cid_file.exists():
        try:
            return cid_file.read_text().strip() or None
        except OSError:
            return None
    return None


def _read_output_json(workspace: Path, task_id: str) -> dict[str, Any] | None:
    out_path = workspace / "output" / f"{task_id}.json"
    if not out_path.exists():
        return None
    try:
        with out_path.open() as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
