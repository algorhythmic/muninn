#!/usr/bin/env python3
"""Write or check the muninn-research-intake release manifest.

The manifest (sha256sum format) lists the contract, every intake schema and
every intake fixture. The contract's Releases table names it; see the
contract's section 11. Regenerate it while a version is a draft; never edit
a released version's manifest.

    python scripts/intake-release-manifest.py            # rewrite the draft manifest
    python scripts/intake-release-manifest.py --check    # exit 1 if it is stale
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.1.0-draft"
CONTRACT = ROOT / "docs" / "contracts" / "MUNINN-RESEARCH-INTAKE.md"
MANIFEST = ROOT / "docs" / "contracts" / f"MUNINN-RESEARCH-INTAKE-{VERSION}.sha256"


def listed_files() -> list[Path]:
    schemas = sorted((ROOT / "schemas" / "json" / "intake").glob("*.schema.json"))
    fixtures = sorted(
        p for p in (ROOT / "tests" / "fixtures" / "intake").rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    )
    return [CONTRACT, *schemas, *fixtures]


def render() -> str:
    lines = []
    for path in sorted(listed_files(), key=lambda p: p.relative_to(ROOT).as_posix()):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {path.relative_to(ROOT).as_posix()}\n")
    return "".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="Fail if the manifest is stale.")
    args = parser.parse_args()
    text = render()
    if args.check:
        if not MANIFEST.exists() or MANIFEST.read_text(encoding="utf-8") != text:
            print(f"stale: {MANIFEST.relative_to(ROOT)}", file=sys.stderr)
            return 1
        print("ok")
        return 0
    MANIFEST.write_text(text, encoding="utf-8")
    print(f"wrote {MANIFEST.relative_to(ROOT)} ({text.count(chr(10))} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
