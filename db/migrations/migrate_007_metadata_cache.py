"""
migrate_007_metadata_cache.py

Phase 1 of the metadata harmonization plan (docs/metadata-sync-architecture.md).

Adds to the photos table:
  - flickr_title, flickr_description, flickr_tags, flickr_tags_hash,
    flickr_last_updated  — last known state from Flickr
  - photos_title, photos_description, photos_tags, photos_tags_hash
    — last known state from Apple Photos
  - meta_synced_flickr_at, meta_synced_photos_at, meta_last_harmonized_at
    — staleness timestamps
  - tags_truncated_for_flickr  — flag for 75-tag truncation

Creates the metadata_proposals table and its indexes.

Sets meta_last_harmonized_at = NOW() for all existing rows so the sync
engine treats the current state as "assumed in-sync at migration time"
rather than generating a noise burst of proposals on first run.

Safe to run multiple times (idempotent).

Usage:
    python db/migrations/migrate_007_metadata_cache.py --config config/config.yml
"""

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import yaml


MIGRATION_NAME = "migrate_007_metadata_cache"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run(db_path: str, dry_run: bool = False) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    applied: list[str] = []
    skipped: list[str] = []

    def already_applied(name: str) -> bool:
        try:
            return (
                conn.execute("SELECT id FROM schema_migrations WHERE name = ?", (name,)).fetchone()
                is not None
            )
        except Exception:
            return False

    def record(name: str) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (name, applied_at) VALUES (?, ?)",
            (name, now_iso()),
        )
        conn.commit()

    existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(photos)").fetchall()}

    # ------------------------------------------------------------------
    # 1. New columns on photos
    # ------------------------------------------------------------------
    new_columns = [
        ("flickr_title", "TEXT"),
        ("flickr_description", "TEXT"),
        ("flickr_tags", "TEXT"),
        ("flickr_tags_hash", "TEXT"),
        ("flickr_last_updated", "TEXT"),
        ("photos_title", "TEXT"),
        ("photos_description", "TEXT"),
        ("photos_tags", "TEXT"),
        ("photos_tags_hash", "TEXT"),
        ("meta_synced_flickr_at", "TEXT"),
        ("meta_synced_photos_at", "TEXT"),
        ("meta_last_harmonized_at", "TEXT"),
        ("tags_truncated_for_flickr", "INTEGER DEFAULT 0"),
    ]

    for col, coltype in new_columns:
        if col not in existing_cols:
            if not dry_run:
                conn.execute(f"ALTER TABLE photos ADD COLUMN {col} {coltype}")
                conn.commit()
            applied.append(f"Added photos.{col}")
        else:
            skipped.append(f"photos.{col} already exists")

    # ------------------------------------------------------------------
    # 2. metadata_proposals table
    # ------------------------------------------------------------------
    existing_tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }

    if "metadata_proposals" not in existing_tables:
        if not dry_run:
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
                                                CHECK(status IN ('pending', 'applied', 'rejected', 'superseded')),
                    created_at              TEXT NOT NULL,
                    resolved_at             TEXT,
                    resolution_note         TEXT
                );

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
            conn.commit()
        applied.append("Created metadata_proposals table and indexes")
    else:
        skipped.append("metadata_proposals table already exists")

    # ------------------------------------------------------------------
    # 3. Indexes on photos for new columns
    # ------------------------------------------------------------------
    existing_indexes = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
    }

    new_indexes = {
        "idx_photos_flickr_tags_hash": "CREATE INDEX idx_photos_flickr_tags_hash ON photos(flickr_tags_hash)",
        "idx_photos_photos_tags_hash": "CREATE INDEX idx_photos_photos_tags_hash ON photos(photos_tags_hash)",
        "idx_photos_meta_harmonized": "CREATE INDEX idx_photos_meta_harmonized  ON photos(meta_last_harmonized_at)",
    }

    for idx_name, ddl in new_indexes.items():
        if idx_name not in existing_indexes:
            if not dry_run:
                conn.execute(ddl)
                conn.commit()
            applied.append(f"Created index {idx_name}")
        else:
            skipped.append(f"Index {idx_name} already exists")

    # ------------------------------------------------------------------
    # 4. Baseline: mark all existing rows as "assumed in-sync"
    #    so the sync engine doesn't generate a proposal explosion
    #    on its first run. This is not a verified sync state —
    #    it just suppresses retroactive proposals.
    # ------------------------------------------------------------------
    baseline_migration = "migrate_008_baseline_harmonized"
    if not already_applied(baseline_migration):
        if not dry_run:
            ts = now_iso()
            conn.execute(
                "UPDATE photos SET meta_last_harmonized_at = ? WHERE meta_last_harmonized_at IS NULL",
                (ts,),
            )
            conn.commit()
            record(baseline_migration)
        applied.append("Set meta_last_harmonized_at baseline for existing rows")
    else:
        skipped.append("Baseline meta_last_harmonized_at already set")

    # ------------------------------------------------------------------
    # 5. Record the migration
    # ------------------------------------------------------------------
    if not already_applied(MIGRATION_NAME):
        if not dry_run:
            record(MIGRATION_NAME)
        applied.append(f"Recorded {MIGRATION_NAME}")
    else:
        skipped.append(f"{MIGRATION_NAME} already recorded")

    # ------------------------------------------------------------------
    # Report
    # ------------------------------------------------------------------
    prefix = "[dry-run] " if dry_run else ""
    for s in applied:
        print(f"  {prefix}Applied:  {s}")
    for s in skipped:
        print(f"  Skipped:  {s}")
    if not applied:
        print("  Nothing to do.")

    conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Blue Pearmain DB migration 007")
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
