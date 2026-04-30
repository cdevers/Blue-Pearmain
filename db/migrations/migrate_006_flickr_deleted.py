"""
migrate_006_flickr_deleted.py

Adds photos.flickr_deleted (INTEGER DEFAULT 0) — set when sync-metadata
receives Flickr API error 1 (photo not found), indicating the photo was
deleted from Flickr. Future sync runs skip these photos entirely.

Safe to run multiple times (idempotent).

Usage:
    python db/migrations/migrate_006_flickr_deleted.py --config config/config.yml
"""

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml


MIGRATION_NAME = "migrate_006_flickr_deleted"


def run(db_path: str, dry_run: bool = False):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    def already_applied():
        try:
            row = conn.execute(
                "SELECT id FROM schema_migrations WHERE name = ?", (MIGRATION_NAME,)
            ).fetchone()
            return row is not None
        except Exception:
            return False

    cols = [r[1] for r in conn.execute("PRAGMA table_info(photos)").fetchall()]

    if "flickr_deleted" in cols:
        print("  Skipped:  photos.flickr_deleted already exists")
        conn.close()
        return

    if dry_run:
        print("  [dry-run] Would add: photos.flickr_deleted INTEGER DEFAULT 0")
        conn.close()
        return

    conn.execute("ALTER TABLE photos ADD COLUMN flickr_deleted INTEGER DEFAULT 0")
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (name, applied_at) VALUES (?, ?)",
        (MIGRATION_NAME, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()
    print("  Applied:  Added photos.flickr_deleted column")


def main():
    parser = argparse.ArgumentParser(description="Blue Pearmain DB migration 007")
    parser.add_argument("--config",  default="config/config.yml")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with open(args.config) as f:
        config = yaml.safe_load(f)

    db_path = str(Path(config["database"]["path"]).expanduser())
    print(f"Database: {db_path}")
    run(db_path, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
