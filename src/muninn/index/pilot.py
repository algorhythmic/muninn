"""Explicit private pilot pin. No config means no Braid process may start."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re

from muninn.index.braid_client import BraidError


@dataclass(frozen=True)
class PilotPin:
    binary: Path
    sha256: str
    source_commit: str


def _read(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise BraidError("pilot_config_invalid")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique)


def load_pilot_pin() -> PilotPin:
    """Validate operator config against build provenance and actual executable.

    MUNINN_BRAID_PILOT_CONFIG selects an explicit private JSON configuration.
    The adjacent <binary>.provenance.json binds the archived source commit to
    the binary digest; it is build evidence, not a signed supply-chain claim.
    """
    path = os.environ.get("MUNINN_BRAID_PILOT_CONFIG")
    if not path:
        raise BraidError("pilot_disabled")
    try:
        if not Path(path).is_absolute():
            raise ValueError("absolute config path required")
        value = _read(path)
        if not isinstance(value, dict) or type(value.get("enabled")) is not bool:
            raise ValueError("invalid flag")
        if not value["enabled"]:
            raise BraidError("pilot_disabled")
        if (set(value) != {"schema", "enabled", "binary_path", "binary_sha256", "source_commit"}
                or value["schema"] != "muninn-braid-pilot/1"):
            raise ValueError("invalid schema")
        binary = Path(value["binary_path"])
        digest, commit = value["binary_sha256"], value["source_commit"]
        if (not binary.is_absolute() or not isinstance(digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit)):
            raise ValueError("invalid pin")
        provenance = _read(str(binary) + ".provenance.json")
        if provenance != {"source_commit": commit, "binary_sha256": digest}:
            raise BraidError("pilot_provenance_mismatch")
        with binary.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != digest:
            raise BraidError("binary_identity_mismatch")
        return PilotPin(binary.resolve(strict=True), digest, commit)
    except BraidError:
        raise
    except (OSError, ValueError, TypeError, KeyError):
        raise BraidError("pilot_config_invalid") from None


def require_pilot_enabled():
    """Cheap admission gate before any canonical read or authored inventory.

    Full pin verification remains immediately before acquiring the subprocess.
    Disabled pilots must have the legacy request's cost, not an index scan's.
    """
    try:
        path = os.environ.get("MUNINN_BRAID_PILOT_CONFIG")
        if not path:
            raise BraidError("pilot_disabled")
        if not Path(path).is_absolute():
            raise BraidError("pilot_config_invalid")
        value = _read(path)
        if not isinstance(value, dict) or type(value.get("enabled")) is not bool:
            raise BraidError("pilot_config_invalid")
        if not value["enabled"]:
            raise BraidError("pilot_disabled")
    except (OSError, ValueError, TypeError):
        raise BraidError("pilot_config_invalid") from None
    except BraidError:
        from muninn.index.runtime import runtime
        runtime.close()
        raise


def open_pilot(runtime, *, context=False, **kwargs):
    try:
        pin = load_pilot_pin()
    except BraidError:
        runtime.close()  # Revocation stops cached sessions as well as new admission.
        raise
    requested = Path(kwargs.pop("binary", "braid"))
    if requested != Path("braid") and requested.resolve() != pin.binary:
        raise BraidError("binary_identity_mismatch")
    return runtime.acquire(binary=pin.binary, context=context, watch_active=True,
                           expected_binary_digest=pin.sha256, **kwargs)
