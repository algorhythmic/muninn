"""The wheel carries ``schemas/sql`` and ``schemas/json`` (non-editable installs).

Every reader of those files resolves them through
:func:`muninn._resources.schemas_dir`: the copy packaged as
``muninn/_schemas`` when there is one, else the checkout's ``schemas/``.
"""

from __future__ import annotations

import shutil
import subprocess
import tomllib
import zipfile
from pathlib import Path

import pytest

from muninn import _resources

ROOT = Path(__file__).resolve().parents[2]


def test_a_checkout_reads_the_repository_schemas() -> None:
    from muninn import db
    from muninn.consumers.packets import schemas as packet_schemas
    from muninn.synthesis import validation

    assert _resources.schemas_dir() == ROOT / "schemas"
    assert db.MIGRATIONS_DIR == ROOT / "schemas" / "sql"
    assert validation.SCHEMA_DIR == ROOT / "schemas" / "json"
    assert packet_schemas.SCHEMA_DIR == ROOT / "schemas" / "json" / "intake"


def test_an_installed_package_prefers_its_packaged_copy(tmp_path, monkeypatch) -> None:
    pkg = tmp_path / "site-packages" / "muninn"
    (pkg / "_schemas" / "sql").mkdir(parents=True)
    monkeypatch.setattr(_resources.resources, "files", lambda name: pkg)
    assert _resources.schemas_dir() == pkg / "_schemas"


def test_pyproject_force_includes_both_schema_trees() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    include = config["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert include == {"schemas/sql": "muninn/_schemas/sql", "schemas/json": "muninn/_schemas/json"}


def test_the_built_wheel_contains_every_schema_file(tmp_path) -> None:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not installed")
    build = subprocess.run(
        [uv, "build", "--wheel", "--offline", "--out-dir", str(tmp_path), str(ROOT)],
        capture_output=True, text=True,
    )
    if build.returncode != 0:
        pytest.skip(f"wheel build unavailable offline: {build.stderr.strip()[-200:]}")
    (wheel,) = tmp_path.glob("muninn-*.whl")
    names = set(zipfile.ZipFile(wheel).namelist())
    expected = {
        "muninn/_schemas/" + p.relative_to(ROOT / "schemas").as_posix()
        for sub in ("sql", "json") for p in (ROOT / "schemas" / sub).rglob("*") if p.is_file()
    }
    assert expected and expected <= names
    assert "muninn/_resources.py" in names
