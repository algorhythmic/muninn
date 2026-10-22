#!/usr/bin/env python3
"""Create a fresh SQLite file at MUNINN_DB_PATH and migrate it to head.

Upgrading an existing DB is `muninn db migrate`, not this script."""

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
        help="Delete existing DB at the path before creating it.",
    )
    args = parser.parse_args()

    db_path = args.db or load_paths().db_path
    if db_path.exists():
        if not args.force:
            print(
                f"DB already exists at {db_path}. Use --force to recreate, "
                "or `muninn db migrate` to upgrade it.",
                file=sys.stderr,
            )
            return 1
        # A leftover -wal would be replayed into the new file (SQLite
        # "mispaired database files"), so remove the WAL pair too.
        for suffix in ("", "-wal", "-shm"):
            db_path.with_name(db_path.name + suffix).unlink(missing_ok=True)

    conn = init_db(db_path)
    conn.close()
    print(f"Initialized {db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
