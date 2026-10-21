"""The extraction backend must fail loudly, never degrade to empty text.

selectolax 1.x removed ``selectolax.parser``; the old fallback silently
turned every scrape into ``partial``/``none``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

_SRC = str(Path(__file__).resolve().parents[2] / "src")


def test_missing_backend_raises_instead_of_returning_empty() -> None:
    code = textwrap.dedent(
        """
        import sys
        sys.modules["selectolax.parser"] = None  # simulate a missing module
        try:
            import muninn.scrape.extract  # noqa: F401
        except ImportError as exc:
            assert "selectolax" in str(exc)
            print("raised")
        else:
            print("imported")
        """
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "PYTHONPATH": _SRC},
    )
    assert out.stdout.strip() == "raised"


def test_backend_extracts_text() -> None:
    from muninn.scrape.extract import extract_text

    assert "hello world" in extract_text("<html><body><p>hello world</p></body></html>")
