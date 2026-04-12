"""
migrate_002_dimensions_and_dedup.py

Adds three groups of columns to the photos table:

  Dimensions (from osxphotos / Flickr extras):
    width   INTEGER   -- pixel width of the original file
    height  INTEGER   -- pixel height of the original file

  Duplicate tracking:
    duplicate_group_id  INTEGER   -- same value for all rows in a duplicate group
    duplicate_role      TEXT      -- 'keeper' | 'discard' | 'review'

  Social metrics (for future keeper-selection by engagement):
    flickr_views  INTEGER
    flickr_faves  INTEGER

This migration is safe to run multiple times (idempotent).

Usage:
    python db/migrate_002_dimensions_and_dedup.py --config config/config.yml
"""

import argparse
import sqlite3
import sys
from pathlib import Path

import yaml


NEW_COLUMNS: list[tuple[str, str, str | None]] = [
    # (column_name, column_def, comment)
    ("width",              "INTEGER",                               "pixel width"),
    ("height",             "INTEGER",                               "pixel height"),
    ("duplicate_group_id", "INTEGER",                               "FK to duplicate_groups.id"),
    ("duplicate_role",     "TEXT CHECK(duplicate_role IN ('keeper','discard','review'))",
                                                                    "role within duplicate group"),
    ("flickr_views",       "INTEGER",                               "Flickr view count"),
    ("flickr_faves",       "INTEGER",                               "Flickr favourite count"),
]

NEW_TABLES = """
CREATE TABLE IF NOT EXISTS duplicate_groups (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    match_key       TEXT NOT NULL UNIQUE,  -- e.g. "DSC_0040.JPG|2024-09-28T14:12:43"
    group_type      TEXT NOT NULL,         -- 'snapbridge' | 'device_upload' | 'uncertain'
    photo_count     INTEGER NOT NULL DEFAULT 0,
    keeper_id       INTEGER REFERENCES photos(id),
    resolved        INTEGER NOT NULL DEFAULT 0,  -- 1 = human has confirmed resolution
    resolved_at     TEXT,
    notes           TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_photos_duplicate_group
    ON photos(duplicate_group_id);

CREATE INDEX IF NOT EXISTS idx_photos_fingerprint
    ON photos(fingerprint);

CREATE INDEX IF NOT EXISTS idx_photos_filename_date
    ON photos(original_filename, date_taken)
    WHERE original_filename IS NOT NULL AND date_taken IS NOT NULL;
"""


def add_column_if_missing(conn: sqlite3.Connection, column: str, col_def: str) -> bool:
    """Add a column to the photos table if it doesn't already exist.
    Returns True if the column was added."""
    existing = {r[1] for r in conn.execute("PRAGMA table_info(photos)").fetchall()}
    if column in existing:
        return False
    conn.execute(f"ALTER TABLE photos ADD COLUMN {column} {col_def}")
    return True


def run(db_path: str, dry_run: bool = False) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    if dry_run:
        print("[dry-run] Would apply the following changes:")
        existing = {r[1] for r in conn.execute("PRAGMA table_info(photos)").fetchall()}
        for col, col_def, comment in NEW_COLUMNS:
            status = "already exists" if col in existing else "ADD"
            print(f"  photos.{col:25s} {status}  ({comment})")
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        status = "already exists" if "duplicate_groups" in tables else "CREATE"
        print(f"  table duplicate_groups          {status}")
        conn.close()
        return

    # ALTER TABLE must run outside an explicit transaction in SQLite.
    # executescript() also issues an implicit COMMIT before running, so we
    # handle each phase separately rather than wrapping in BEGIN/COMMIT.
    added = []
    for col, col_def, comment in NEW_COLUMNS:
        if add_column_if_missing(conn, col, col_def):
            added.append(col)
            print(f"  Added column: photos.{col}")
        else:
            print(f"  Skipped (exists): photos.{col}")
    conn.commit()

    # executescript() handles its own transaction internally
    conn.executescript(NEW_TABLES)
    print("  Created table: duplicate_groups (and indexes)")

    conn.close()
    print(f"\nMigration complete. {len(added)} column(s) added.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/config.yml")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would change without modifying the DB")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"Config not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    with open(config_path) as f:
        config = yaml.safe_load(f)

    db_path = config.get("database", {}).get("path", "data/curator.db")
    print(f"Database: {db_path}")
    run(db_path, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
