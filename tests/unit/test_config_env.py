"""Unit tests for the minimal .env loader in muninn.config."""

from __future__ import annotations

import os

from muninn import config


def test_loads_values_and_real_env_wins(tmp_path, monkeypatch):
    envfile = tmp_path / ".env"
    envfile.write_text(
        "# comment line\n"
        "\n"
        "MUNINN_TEST_PLAIN=hello\n"
        'MUNINN_TEST_QUOTED="wor=ld"\n'
        "MUNINN_TEST_SINGLE='spaced value'\n"
        "MUNINN_TEST_EXISTING=file-value\n"
        "this line has no assignment\n"
    )
    monkeypatch.setenv("MUNINN_TEST_EXISTING", "env-value")
    monkeypatch.delenv("MUNINN_TEST_PLAIN", raising=False)
    try:
        config._load_dotenv(envfile)
        assert os.environ["MUNINN_TEST_PLAIN"] == "hello"
        assert os.environ["MUNINN_TEST_QUOTED"] == "wor=ld"
        assert os.environ["MUNINN_TEST_SINGLE"] == "spaced value"
        # A real environment variable is never overwritten by the file.
        assert os.environ["MUNINN_TEST_EXISTING"] == "env-value"
    finally:
        for key in ("MUNINN_TEST_PLAIN", "MUNINN_TEST_QUOTED", "MUNINN_TEST_SINGLE"):
            os.environ.pop(key, None)


def test_missing_file_is_noop(tmp_path):
    config._load_dotenv(tmp_path / ".env")  # must not raise
