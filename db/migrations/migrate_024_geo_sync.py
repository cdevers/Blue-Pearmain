"""
migrate_024_geo_sync.py

## Coordinate model (three tiers)

  canonical  latitude / longitude
      The currently-accepted display value for this photo.

  source-specific  flickr_latitude / flickr_longitude  (Flickr's last-known value)
                   photos_latitude / photos_longitude   (Photos.app's last-known value)
      Populated by the poller (Flickr) and scanner (Photos) on every sync run.
      sync_geo() diffs these two to detect discrepancies without hitting any API.

  proposals  metadata_proposals WHERE field='geo_location'
      The reconciliation layer.

## Changes

Adds to photos:
  geo_confirmed_none INTEGER NOT NULL DEFAULT 0
  flickr_latitude    REAL
  flickr_longitude   REAL
  photos_latitude    REAL
  photos_longitude   REAL

Recreates metadata_proposals with geo_location added to field CHECK.
Idempotent (checks schema_migrations).

Usage:
    python db/migrations/migrate_024_geo_sync.py --config config/config.yml
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import yaml

MIGRATION_NAME = "migrate_024_geo_sync"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_on_conn(conn: sqlite3.Connection) -> None:
    """Run migration on an existing connection (used by tests and run()).

    Caller is responsible for setting conn.row_factory and enabling
    PRAGMA foreign_keys if needed (run() and tests both do this).
    We set it here defensively to avoid subtle bugs if called from contexts
    that forget to set it beforehand.
    """
    if conn.row_factory is None:
        conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    # Idempotency check
    try:
        row = conn.execute(
            "SELECT id FROM schema_migrations WHERE name = ?", (MIGRATION_NAME,)
        ).fetchone()
        if row is not None:
            return
    except Exception:
        pass

    conn.execute("BEGIN")

    # 1. Add new columns to photos (ALTER TABLE — safe, additive)
    photo_cols = {r[1] for r in conn.execute("PRAGMA table_info(photos)").fetchall()}
    for col, defn in [
        ("geo_confirmed_none", "INTEGER NOT NULL DEFAULT 0"),
        ("flickr_latitude", "REAL"),
        ("flickr_longitude", "REAL"),
        ("photos_latitude", "REAL"),
        ("photos_longitude", "REAL"),
    ]:
        if col not in photo_cols:
            conn.execute(f"ALTER TABLE photos ADD COLUMN {col} {defn}")

    # 2. Recreate metadata_proposals with geo_location in CHECK constraint.
    conn.execute("""
        CREATE TABLE metadata_proposals_new (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            photo_id                INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
            field                   TEXT NOT NULL
                                        CHECK(field IN ('title', 'description', 'tags', 'geo_location')),
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
            resolution_note         TEXT,
            batch_id                INTEGER REFERENCES bulk_batches(id)
        )
    """)
    # Copy all existing rows (old field values remain valid)
    conn.execute("""
        INSERT INTO metadata_proposals_new
        SELECT id, photo_id, field, proposed_value, source, target, conflict_type,
               source_hash_at_creation, target_hash_at_creation, status,
               created_at, resolved_at, resolution_note, batch_id
        FROM metadata_proposals
    """)
    conn.execute("DROP TABLE metadata_proposals")
    conn.execute("ALTER TABLE metadata_proposals_new RENAME TO metadata_proposals")

    # Recreate indexes (all must match the originals from migrate_007 and migrate_023)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_proposals_photo
        ON metadata_proposals(photo_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_proposals_pending
        ON metadata_proposals(status)
        WHERE status = 'pending'
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_proposals_field_target
        ON metadata_proposals(field, target, status)
        WHERE status = 'pending'
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_proposals_identity
        ON metadata_proposals(photo_id, field, proposed_value, target, source)
        WHERE status = 'pending'
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_proposals_batch
        ON metadata_proposals(batch_id)
        WHERE batch_id IS NOT NULL
    """)

    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (name, applied_at) VALUES (?, ?)",
        (MIGRATION_NAME, _now_iso()),
    )
    conn.commit()


def run(db_path: str, dry_run: bool = False) -> None:
    conn = sqlite3.connect(db_path)

    if dry_run:
        print("  [dry-run] Would add geo columns to photos and recreate metadata_proposals")
        conn.close()
        return

    # Idempotency check is performed inside run_on_conn()
    run_on_conn(conn)
    conn.close()
    print("  Applied:  migrate_024_geo_sync")


def main() -> int:
    parser = argparse.ArgumentParser(description="Migration 024: geo sync columns")
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
