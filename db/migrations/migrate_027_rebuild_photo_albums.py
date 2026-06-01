"""
migrate_027_rebuild_photo_albums.py

Rebuild photo_albums to clear stale SQLite internal FK metadata.

After migrations 001 and 016 rebuilt the `photos` table via the standard
rename/create/copy/drop pattern (`RENAME TO photos_old` → CREATE new `photos`
→ INSERT SELECT → `DROP TABLE photos_old`), SQLite 3.51+ fails with
"no such table: main.photos_old" on any write to `photo_albums`.  This happens
because `photo_albums` was created before those rebuilds and its internal FK
parent reference still points to the old, now-dropped `photos_old` stub.

Rebuilding `photo_albums` with the same rename/create/copy/drop pattern clears
the stale reference.  After this migration all writes to `photo_albums`
(INSERT, UPDATE) work correctly.

Idempotent: skips if already applied.
"""

import argparse
import sqlite3
from pathlib import Path

import yaml

MIGRATION_NAME = "migrate_027_rebuild_photo_albums"


def run_on_conn(conn: sqlite3.Connection) -> None:
    if conn.row_factory is None:
        conn.row_factory = sqlite3.Row

    try:
        row = conn.execute(
            "SELECT id FROM schema_migrations WHERE name = ?", (MIGRATION_NAME,)
        ).fetchone()
        if row is not None:
            return
    except Exception:
        pass

    conn.execute("BEGIN")

    # Rebuild photo_albums with all current columns in the CREATE TABLE so that
    # SQLite's internal FK metadata references the current photos table, not the
    # long-dropped photos_old stub left by migrations 001/016.
    conn.execute("ALTER TABLE photo_albums RENAME TO photo_albums_old")
    conn.execute(
        """
        CREATE TABLE photo_albums (
            photo_id        INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
            album_id        INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
            flickr_pushed   INTEGER DEFAULT 0,
            pushed_at       TEXT,
            removed_at      TEXT,
            PRIMARY KEY (photo_id, album_id)
        )
        """
    )
    conn.execute("INSERT INTO photo_albums SELECT * FROM photo_albums_old")
    conn.execute("DROP TABLE photo_albums_old")

    # Recreate indexes
    conn.execute("CREATE INDEX IF NOT EXISTS idx_photo_albums_photo ON photo_albums(photo_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_photo_albums_album ON photo_albums(album_id)")

    from datetime import datetime, timezone

    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (name, applied_at) VALUES (?, ?)",
        (MIGRATION_NAME, datetime.now(timezone.utc).isoformat()),
    )
    conn.execute("COMMIT")


def run(db_path: str, dry_run: bool = False) -> None:
    if dry_run:
        print("  [dry-run] Would rebuild photo_albums to clear stale FK metadata")
        return
    conn = sqlite3.connect(db_path)
    run_on_conn(conn)
    conn.close()
    print("  Applied:  migrate_027_rebuild_photo_albums")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migration 027: rebuild photo_albums to clear stale SQLite FK metadata"
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
