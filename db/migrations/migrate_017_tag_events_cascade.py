"""
migrate_015_tag_events_cascade.py

Adds ON DELETE CASCADE to tag_events.photo_id so that deleting a photo
(e.g. during ghost-photo cleanup) doesn't raise a FK constraint error.

SQLite doesn't support ALTER COLUMN, so this recreates the table:
  1. Rename tag_events → tag_events_old
  2. Create tag_events with ON DELETE CASCADE
  3. Copy all rows
  4. Drop tag_events_old
  5. Recreate the index

Safe to run multiple times (idempotent — checks schema_migrations).

Usage:
    python db/migrations/migrate_015_tag_events_cascade.py --config config/config.yml
"""

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import yaml

MIGRATION_NAME = "migrate_015_tag_events_cascade"


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
        print("  [dry-run] Would recreate tag_events with ON DELETE CASCADE on photo_id")
        conn.close()
        return

    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("BEGIN")

    conn.execute("ALTER TABLE tag_events RENAME TO tag_events_old")

    conn.execute("""
        CREATE TABLE tag_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            photo_id    INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
            event_at    TEXT NOT NULL,
            destination TEXT NOT NULL,
            tags_before TEXT,
            tags_after  TEXT,
            success     INTEGER DEFAULT 1,
            error       TEXT
        )
    """)

    conn.execute("""
        INSERT INTO tag_events
            (id, photo_id, event_at, destination, tags_before, tags_after, success, error)
        SELECT id, photo_id, event_at, destination, tags_before, tags_after, success, error
        FROM tag_events_old
    """)

    conn.execute("DROP TABLE tag_events_old")

    conn.execute("CREATE INDEX IF NOT EXISTS idx_tag_events_photo ON tag_events(photo_id)")

    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (name, applied_at) VALUES (?, ?)",
        (MIGRATION_NAME, now_iso()),
    )
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")
    conn.close()
    print("  Applied:  migrate_015_tag_events_cascade")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migration 015: add ON DELETE CASCADE to tag_events.photo_id"
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
