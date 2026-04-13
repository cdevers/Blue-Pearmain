"""
db/migrations/migrate_003_albums.py — add albums and photo_albums tables

Idempotent: uses CREATE TABLE IF NOT EXISTS and INSERT OR IGNORE.

Usage:
    python db/migrations/migrate_003_albums.py --config config/config.yml
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

MIGRATION_NAME = "migrate_003_albums"

DDL = """
CREATE TABLE IF NOT EXISTS albums (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    apple_uuid      TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    flickr_set_id   TEXT,
    flickr_set_url  TEXT,
    created_at      TEXT,
    updated_at      TEXT
);

CREATE TABLE IF NOT EXISTS photo_albums (
    photo_id        INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    album_id        INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
    flickr_pushed   INTEGER DEFAULT 0,
    pushed_at       TEXT,
    PRIMARY KEY (photo_id, album_id)
);

CREATE INDEX IF NOT EXISTS idx_photo_albums_photo   ON photo_albums(photo_id);
CREATE INDEX IF NOT EXISTS idx_photo_albums_album   ON photo_albums(album_id);
CREATE INDEX IF NOT EXISTS idx_photo_albums_pending ON photo_albums(flickr_pushed)
    WHERE flickr_pushed = 0;
"""


def run(db_path: Path) -> None:
    from db.db import Database
    db = Database(db_path)

    already = db.conn.execute(
        "SELECT id FROM schema_migrations WHERE name = ?", (MIGRATION_NAME,)
    ).fetchone()
    if already:
        print(f"Migration {MIGRATION_NAME} already applied — skipping.")
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
    parser = argparse.ArgumentParser(description="Migrate: add albums and photo_albums tables")
    parser.add_argument("--config", default="config/config.yml")
    args = parser.parse_args()

    with open(args.config) as f:
        config = yaml.safe_load(f)

    db_path = Path(config["database"]["path"]).expanduser()
    run(db_path)


if __name__ == "__main__":
    main()
