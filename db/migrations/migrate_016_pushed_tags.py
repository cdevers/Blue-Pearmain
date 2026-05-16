"""
migrate_016_pushed_tags.py

Adds pushed_tags TEXT column to the photos table.

pushed_tags is the write ledger: the cumulative set of tags BP has
ever successfully pushed to Flickr for a photo. NULL means nothing
has been pushed and confirmed. Existing rows get NULL (correct default).

Safe to run multiple times (idempotent).

Usage:
    python db/migrations/migrate_016_pushed_tags.py --config config/config.yml
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import yaml

MIGRATION_NAME = "migrate_016_pushed_tags"


def _already_migrated(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute(
            "SELECT id FROM schema_migrations WHERE name = ?", (MIGRATION_NAME,)
        ).fetchone()
        if row is not None:
            return True
    except Exception:
        pass
    cols = [row[1] for row in conn.execute("PRAGMA table_info(photos)").fetchall()]
    return "pushed_tags" in cols


def run(db_path: str, dry_run: bool = False) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    if _already_migrated(conn):
        print("  Skipped:  migration already applied")
        conn.close()
        return

    if not dry_run:
        conn.execute("ALTER TABLE photos ADD COLUMN pushed_tags TEXT")
        conn.execute(
            "INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)",
            (MIGRATION_NAME, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        print("  Applied:  added pushed_tags column to photos")
    else:
        print("  Dry-run:  would add pushed_tags column to photos")

    conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Migration 016 — add pushed_tags column")
    parser.add_argument("--config", default="config/config.yml")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with open(args.config) as f:
        config = yaml.safe_load(f)

    db_path = str(Path(config["database"]["path"]).expanduser())
    run(db_path, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
