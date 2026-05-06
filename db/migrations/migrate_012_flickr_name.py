"""
migrate_012_flickr_name.py

Adds:
  albums.flickr_name  TEXT — last album name successfully pushed to Flickr photoset
  folders.flickr_name TEXT — last folder name successfully pushed to Flickr Collection

Safe to run multiple times (idempotent — checks schema_migrations).

Usage:
    python db/migrations/migrate_012_flickr_name.py --config config/config.yml
"""

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import yaml

MIGRATION_NAME = "migrate_012_flickr_name"


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

    if dry_run:
        print("  [dry-run] Would add albums.flickr_name column")
        print("  [dry-run] Would add folders.flickr_name column")
        conn.close()
        return

    conn.execute("BEGIN")

    # Add flickr_name to albums (idempotent: only if column absent)
    existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(albums)").fetchall()}
    if "flickr_name" not in existing_cols:
        conn.execute("ALTER TABLE albums ADD COLUMN flickr_name TEXT")

    # Add flickr_name to folders (idempotent: only if column absent)
    existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(folders)").fetchall()}
    if "flickr_name" not in existing_cols:
        conn.execute("ALTER TABLE folders ADD COLUMN flickr_name TEXT")

    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (name, applied_at) VALUES (?, ?)",
        (MIGRATION_NAME, now_iso()),
    )
    conn.commit()
    conn.close()
    print("  Applied:  migrate_012_flickr_name")


def main() -> int:
    parser = argparse.ArgumentParser(description="Migration 012: add flickr_name columns")
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
