#!/usr/bin/env python3
"""Apply schema.sql against a fresh SQLite file at MUNINN_DB_PATH."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from muninn.config import load_paths  # noqa: E402
from muninn.db import init_db  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Initialize the muninn SQLite store.")
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="DB path (defaults to MUNINN_DB_PATH or ./data/muninn.db).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Delete existing DB at the path before applying schema.",
    )
    args = parser.parse_args()

    db_path = args.db or load_paths().db_path
    if db_path.exists():
        if not args.force:
            print(f"DB already exists at {db_path}. Use --force to recreate.", file=sys.stderr)
            return 1
        db_path.unlink()

    conn = init_db(db_path)
    conn.close()
    print(f"Initialized {db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
