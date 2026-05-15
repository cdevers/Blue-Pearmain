"""
migrate_014_merged_into_id.py

Adds:
  photos.merged_into_id INTEGER REFERENCES photos(id)

When the duplicates UI soft-merges a Flickr-only donor record into a
Photos-linked target, the donor row is kept but marked with merged_into_id
pointing to the record it was merged into.

Safe to run multiple times (idempotent — checks schema_migrations).

Usage:
    python db/migrations/migrate_014_merged_into_id.py --config config/config.yml
"""

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import yaml

MIGRATION_NAME = "migrate_014_merged_into_id"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run(db_path: str, dry_run: bool = False) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    try:
        row = conn.execute(
            "SELECT id FROM schema_migrations WHERE name = ?", (MIGRATION_NAME,)
        ).fetchone()
        if row is not None:
            print("  Skipped:  migration already applied")
            conn.close()
            return
    except Exception:
        pass

    existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(photos)").fetchall()}
    if dry_run:
        if "merged_into_id" not in existing_cols:
            print("  [dry-run] Would add photos.merged_into_id column")
        else:
            print("  [dry-run] photos.merged_into_id already exists")
        conn.close()
        return

    conn.execute("BEGIN")

    if "merged_into_id" not in existing_cols:
        conn.execute("ALTER TABLE photos ADD COLUMN merged_into_id INTEGER REFERENCES photos(id)")

    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (name, applied_at) VALUES (?, ?)",
        (MIGRATION_NAME, now_iso()),
    )
    conn.commit()
    conn.close()
    print("  Applied:  migrate_014_merged_into_id")


def main() -> int:
    parser = argparse.ArgumentParser(description="Migration 014: add merged_into_id")
    parser.add_argument("--config", default="config/config.yml")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with open(args.config) as f:
        config = yaml.safe_load(f)

    db_path = str(Path(config["database"]["path"]).expanduser())
    run(db_path, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
