"""
migrate_015_album_removal.py

Adds:
  photo_albums.removed_at TEXT  — tombstone: scanner detected photo was removed from album
  albums.deleted_at        TEXT  — tombstone: scanner detected album was deleted in Apple Photos

Both columns are nullable. NULL = current state; non-NULL = pending Flickr reconciliation.

Safe to run multiple times (idempotent — checks schema_migrations).

Usage:
    python db/migrations/migrate_015_album_removal.py --config config/config.yml
"""

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import yaml

MIGRATION_NAME = "migrate_015_album_removal"


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

    pa_cols = {r[1] for r in conn.execute("PRAGMA table_info(photo_albums)").fetchall()}
    al_cols = {r[1] for r in conn.execute("PRAGMA table_info(albums)").fetchall()}

    if dry_run:
        if "removed_at" not in pa_cols:
            print("  [dry-run] Would add photo_albums.removed_at column")
        if "deleted_at" not in al_cols:
            print("  [dry-run] Would add albums.deleted_at column")
        conn.close()
        return

    conn.execute("BEGIN")

    if "removed_at" not in pa_cols:
        conn.execute("ALTER TABLE photo_albums ADD COLUMN removed_at TEXT")

    if "deleted_at" not in al_cols:
        conn.execute("ALTER TABLE albums ADD COLUMN deleted_at TEXT")

    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (name, applied_at) VALUES (?, ?)",
        (MIGRATION_NAME, now_iso()),
    )
    conn.commit()
    conn.close()
    print("  Applied:  migrate_015_album_removal")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migration 015: add album removal tombstone columns"
    )
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
