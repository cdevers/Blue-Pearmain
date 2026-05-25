"""
migrate_010_stale_uuid.py

Two changes:
  1. Recreates metadata_proposals with 'failed' added to the status CHECK
     constraint (SQLite cannot ALTER a CHECK constraint — table recreation required).
  2. Adds photos.uuid_stale INTEGER NOT NULL DEFAULT 0.

Safe to run multiple times (idempotent — checks schema_migrations).

Usage:
    python db/migrations/migrate_010_stale_uuid.py --config config/config.yml
"""

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import yaml


MIGRATION_NAME = "migrate_010_stale_uuid"


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
        print("  [dry-run] Would recreate metadata_proposals with 'failed' status")
        print("  [dry-run] Would add photos.uuid_stale column")
        conn.close()
        return

    # ------------------------------------------------------------------
    # 1. Recreate metadata_proposals with 'failed' in status CHECK
    # ------------------------------------------------------------------
    conn.execute("ALTER TABLE metadata_proposals RENAME TO metadata_proposals_old")

    conn.executescript("""
        CREATE TABLE metadata_proposals (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            photo_id                INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
            field                   TEXT NOT NULL
                                        CHECK(field IN ('title', 'description', 'tags')),
            proposed_value          TEXT,
            source                  TEXT NOT NULL
                                        CHECK(source IN ('flickr', 'photos', 'manual')),
            target                  TEXT NOT NULL
                                        CHECK(target IN ('flickr', 'photos')),
            conflict_type           TEXT NOT NULL
                                        CHECK(conflict_type IN ('non_conflict', 'divergence', 'collision')),
            source_hash_at_creation TEXT,
            target_hash_at_creation TEXT,
            status                  TEXT NOT NULL DEFAULT 'pending'
                                        CHECK(status IN ('pending', 'applied', 'rejected', 'superseded', 'failed')),
            created_at              TEXT NOT NULL,
            resolved_at             TEXT,
            resolution_note         TEXT
        );

        INSERT INTO metadata_proposals
            (id, photo_id, field, proposed_value, source, target, conflict_type,
             source_hash_at_creation, target_hash_at_creation, status,
             created_at, resolved_at, resolution_note)
            SELECT id, photo_id, field, proposed_value, source, target, conflict_type,
                   source_hash_at_creation, target_hash_at_creation, status,
                   created_at, resolved_at, resolution_note
            FROM metadata_proposals_old;

        DROP TABLE metadata_proposals_old;

        CREATE INDEX idx_proposals_photo
            ON metadata_proposals(photo_id);
        CREATE INDEX idx_proposals_pending
            ON metadata_proposals(status)
            WHERE status = 'pending';
        CREATE INDEX idx_proposals_field_target
            ON metadata_proposals(field, target, status)
            WHERE status = 'pending';
        CREATE UNIQUE INDEX idx_proposals_identity
            ON metadata_proposals(photo_id, field, proposed_value, target, source)
            WHERE status = 'pending';
    """)

    # ------------------------------------------------------------------
    # 2. Add uuid_stale column to photos
    # ------------------------------------------------------------------
    existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(photos)").fetchall()}
    if "uuid_stale" not in existing_cols:
        conn.execute("ALTER TABLE photos ADD COLUMN uuid_stale INTEGER NOT NULL DEFAULT 0")

    # ------------------------------------------------------------------
    # 3. Record migration
    # ------------------------------------------------------------------
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (name, applied_at) VALUES (?, ?)",
        (MIGRATION_NAME, now_iso()),
    )
    conn.commit()
    conn.close()
    print("  Applied:  metadata_proposals.status now includes 'failed'; photos.uuid_stale added")


def main():
    parser = argparse.ArgumentParser(description="Blue Pearmain DB migration 010")
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
