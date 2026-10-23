"""Where muninn's schema files live, in a checkout or an installed wheel.

The SQL migrations (``schemas/sql``) and the JSON Schemas (``schemas/json``)
sit at the repository root, outside the ``muninn`` package. A built wheel
carries copies of both inside the package, as ``muninn/_schemas/sql`` and
``muninn/_schemas/json`` (hatchling ``force-include`` in ``pyproject.toml``),
so a non-editable install can migrate a database and validate records with
no checkout around it. A checkout (editable or ``PYTHONPATH=src``) has no
``muninn/_schemas`` and reads the repository's own ``schemas/``.

Stdlib only.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

#: The checked-in schemas of a source checkout (``<repo>/schemas``).
REPO_SCHEMAS = Path(__file__).resolve().parents[2] / "schemas"


def schemas_dir() -> Path:
    """The directory holding ``sql/`` and ``json/``: the copy packaged in
    the wheel when there is one, else the repository's ``schemas/``."""
    packaged = resources.files("muninn") / "_schemas"
    if isinstance(packaged, Path) and packaged.is_dir():
        return packaged
    return REPO_SCHEMAS


__all__ = ["REPO_SCHEMAS", "schemas_dir"]
