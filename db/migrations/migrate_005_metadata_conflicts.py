"""
db/migrations/migrate_005_metadata_conflicts.py — add metadata_conflicts table

Idempotent: uses CREATE TABLE IF NOT EXISTS and INSERT OR IGNORE.

Usage:
    python db/migrations/migrate_005_metadata_conflicts.py --config config/config.yml
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

MIGRATION_NAME = "migrate_005_metadata_conflicts"

DDL = """
CREATE TABLE IF NOT EXISTS metadata_conflicts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    photo_id        INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    field           TEXT NOT NULL
                        CHECK(field IN ('title', 'description', 'tags')),
    flickr_value    TEXT,
    photos_value    TEXT,
    resolved        INTEGER DEFAULT 0,
    resolution      TEXT
                        CHECK(resolution IS NULL OR
                              resolution IN ('flickr', 'photos', 'manual')),
    resolved_at     TEXT,
    created_at      TEXT NOT NULL,
    UNIQUE(photo_id, field)
);

CREATE INDEX IF NOT EXISTS idx_metadata_conflicts_photo
    ON metadata_conflicts(photo_id);
CREATE INDEX IF NOT EXISTS idx_metadata_conflicts_unresolved
    ON metadata_conflicts(resolved)
    WHERE resolved = 0;
"""


def run(db_path: Path, dry_run: bool = False) -> None:
    from db.db import Database

    db = Database(db_path)

    already = db.conn.execute(
        "SELECT id FROM schema_migrations WHERE name = ?", (MIGRATION_NAME,)
    ).fetchone()
    if already:
        print(f"Migration {MIGRATION_NAME} already applied — skipping.")
        db.close()
        return

    if dry_run:
        print(f"[dry-run] Would apply migration {MIGRATION_NAME}.")
        db.close()
        return

    db.conn.executescript(DDL)
    db.conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (name, applied_at) VALUES (?, ?)",
        (MIGRATION_NAME, datetime.now(timezone.utc).isoformat()),
    )
    db.conn.commit()
    print(f"Migration {MIGRATION_NAME} applied.")
    db.close()


def main():
    parser = argparse.ArgumentParser(description="Migrate: add metadata_conflicts table")
    parser.add_argument("--config", default="config/config.yml")
    args = parser.parse_args()

    with open(args.config) as f:
        config = yaml.safe_load(f)

    db_path = Path(config["database"]["path"]).expanduser()
    run(db_path)


if __name__ == "__main__":
    main()
