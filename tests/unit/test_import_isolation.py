"""Light entry points must not drag in heavy optional consumers.

The CLI root and the ingest path run without Qdrant, DuckDB, Parquet, MCP,
Jinja or local embeddings; each check runs in a clean interpreter so
earlier imports in the test session can't mask a regression.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_SRC = str(Path(__file__).resolve().parents[2] / "src")
HEAVY = (
    "qdrant_client",
    "duckdb",
    "pyarrow",
    "mcp",
    "jinja2",
    "sentence_transformers",
    "torch",
)


@pytest.mark.parametrize(
    "module",
    ["muninn.consumers.cli.main", "muninn.ingest", "muninn.sanitize", "muninn.enrich"],
)
def test_module_does_not_import_heavy_deps(module: str) -> None:
    code = (
        "import importlib, json, sys\n"
        f"importlib.import_module({module!r})\n"
        f"print(json.dumps(sorted(m for m in {HEAVY!r} if m in sys.modules)))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "PYTHONPATH": _SRC},
    )
    assert json.loads(out.stdout) == []


def test_ingest_does_not_import_scrape_transport() -> None:
    code = (
        "import json, sys\n"
        "import muninn.ingest\n"
        "print(json.dumps(sorted(m for m in ('httpx', 'selectolax') if m in sys.modules)))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "PYTHONPATH": _SRC},
    )
    assert json.loads(out.stdout) == []
