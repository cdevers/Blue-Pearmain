"""
migrate_011_folders.py

Adds:
  1. folders table — self-referential (parent_id → folders.id), tracks
     Apple Photos folder hierarchy and corresponding Flickr Collection IDs.
  2. albums.folder_id — nullable FK to folders.id (ON DELETE SET NULL).

Safe to run multiple times (idempotent — checks schema_migrations).

Usage:
    python db/migrations/migrate_011_folders.py --config config/config.yml
"""

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import yaml


MIGRATION_NAME = "migrate_011_folders"


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
        print("  [dry-run] Would create folders table")
        print("  [dry-run] Would add albums.folder_id column")
        conn.close()
        return

    conn.execute("BEGIN")

    # 1. Create folders table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS folders (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            apple_uuid           TEXT NOT NULL UNIQUE,
            name                 TEXT NOT NULL,
            parent_id            INTEGER REFERENCES folders(id) ON DELETE SET NULL,
            flickr_collection_id TEXT,
            created_at           TEXT,
            updated_at           TEXT
        )
    """)

    # 2. Add folder_id to albums (idempotent: only if column absent)
    existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(albums)").fetchall()}
    if "folder_id" not in existing_cols:
        conn.execute(
            "ALTER TABLE albums ADD COLUMN folder_id INTEGER REFERENCES folders(id) ON DELETE SET NULL"
        )

    # 3. Record migration
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (name, applied_at) VALUES (?, ?)",
        (MIGRATION_NAME, now_iso()),
    )
    conn.commit()
    conn.close()
    print("  Applied:  folders table created; albums.folder_id added")


def main():
    parser = argparse.ArgumentParser(description="Blue Pearmain DB migration 011")
    parser.add_argument("--config", default="config/config.yml")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with open(args.config) as f:
        config = yaml.safe_load(f)

    db_path = str(Path(config["database"]["path"]).expanduser())
    print(f"Database: {db_path}")
    run(db_path, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
