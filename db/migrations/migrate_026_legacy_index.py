"""
migrate_026_legacy_index.py

Adds two tables for the legacy (migrated iPhoto/Photos 4) library indexer (#162):

  legacy_libraries  one row per indexed source library; holds path-independent
                    identity (library_uuid) plus cache-validity metadata
                    (db_mtime, db_size, db_head_hash) used to decide whether the
                    local DB cache can be reused.

  legacy_assets     one row per old-library asset, keyed by the path-independent
                    identity (library_uuid, asset_uuid). Mirrors the existing
                    apple_persons JSON-array convention rather than normalized
                    person tables.

Safe to run multiple times (idempotent via schema_migrations).

Usage:
    python db/migrations/migrate_026_legacy_index.py --config config/config.yml
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import yaml

MIGRATION_NAME = "migrate_026_legacy_index"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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

    conn.execute("""
        CREATE TABLE IF NOT EXISTS legacy_libraries (
            library_uuid          TEXT PRIMARY KEY,
            display_name          TEXT,
            source_path_last_seen TEXT,
            schema_version        INTEGER,
            db_mtime              TEXT,
            db_size               INTEGER,
            db_head_hash          TEXT,
            asset_count           INTEGER NOT NULL DEFAULT 0,
            indexed_at            TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS legacy_assets (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            library_uuid        TEXT NOT NULL REFERENCES legacy_libraries(library_uuid) ON DELETE CASCADE,
            asset_uuid          TEXT NOT NULL,
            original_filename   TEXT,
            fingerprint         TEXT,
            date_taken          TEXT,
            width               INTEGER,
            height              INTEGER,
            latitude            REAL,
            longitude           REAL,
            title               TEXT,
            description         TEXT,
            keywords            TEXT,
            labels              TEXT,
            persons             TEXT,
            named_face_count    INTEGER NOT NULL DEFAULT 0,
            unknown_face_count  INTEGER NOT NULL DEFAULT 0,
            master_rel_path     TEXT,
            thumbnail_cache_key TEXT,
            thumbnail_status    TEXT,
            indexed_at          TEXT,
            UNIQUE(library_uuid, asset_uuid)
        )
    """)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_legacy_assets_date ON legacy_assets(date_taken)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_legacy_assets_dims ON legacy_assets(width, height)"
    )

    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (name, applied_at) VALUES (?, ?)",
        (MIGRATION_NAME, _now_iso()),
    )
    conn.commit()


def run(db_path: str, dry_run: bool = False) -> None:
    if dry_run:
        print("  [dry-run] Would create legacy_libraries and legacy_assets tables")
        return
    conn = sqlite3.connect(db_path)
    run_on_conn(conn)
    conn.close()
    print("  Applied:  migrate_026_legacy_index")


def main() -> int:
    parser = argparse.ArgumentParser(description="Migration 026: legacy library index tables")
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
