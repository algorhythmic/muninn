"""Validate records against the contract's JSON Schemas.

The schemas live in ``schemas/json/intake/`` and reference each other by
``$id``, so all nine are loaded into one registry. ``jsonschema`` is
imported on first use, which keeps ``import muninn.consumers.packets``
stdlib-only.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

SCHEMA_DIR = Path(__file__).resolve().parents[4] / "schemas" / "json" / "intake"

RECORD_TYPES: tuple[str, ...] = (
    "source-item",
    "item-link",
    "bookmark-observation",
    "content-revision",
    "interest-profile",
    "assessment",
    "evidence-packet",
    "usage-record",
)


@lru_cache(maxsize=1)
def _validators() -> dict[str, Any]:
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource

    schemas = {
        p.name.removesuffix(".schema.json"): json.loads(p.read_text(encoding="utf-8"))
        for p in SCHEMA_DIR.glob("*.schema.json")
    }
    missing = set(RECORD_TYPES) - set(schemas)
    if missing:
        raise FileNotFoundError(f"intake schemas missing from {SCHEMA_DIR}: {sorted(missing)}")
    registry = Registry().with_resources(
        (s["$id"], Resource.from_contents(s)) for s in schemas.values()
    )
    return {name: Draft202012Validator(schemas[name], registry=registry) for name in RECORD_TYPES}


def schema_errors(record_type: str, record: Any) -> list[str]:
    """Schema violations of ``record`` as ``"<json path>: <reason>"`` strings.

    The reason names the failed keyword, never the offending value: a
    rejected locator may be exactly the credential-carrying URL the schema
    exists to stop, and these strings reach logs and the terminal.
    """
    validator = _validators()[record_type]
    out: list[str] = []

    def walk(errors: Any) -> None:
        for e in sorted(errors, key=lambda e: list(map(str, e.absolute_path))):
            out.append(f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {_reason(e)}")
            if e.context:  # oneOf / anyOf branches: say why each failed
                walk(e.context)

    walk(validator.iter_errors(record))
    return out[:50]


def _reason(error: Any) -> str:
    if error.validator in ("required", "additionalProperties"):
        return error.message[:300]  # names properties, not values
    if error.validator == "type":
        return f"is not of type {error.validator_value!r}"
    return f"fails {error.validator!r}"
