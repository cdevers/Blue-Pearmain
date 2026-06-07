"""
migrate_031_legacy_upload.py

Add uploaded_flickr_id and uploaded_at columns to legacy_assets.
These support idempotent re-runs of bp upload-legacy-unmatched (#230):
uploaded_flickr_id is set immediately after a successful Flickr upload,
before the photos row is created, so a re-run skips assets already uploaded.

Uniqueness invariant: uploaded_flickr_id is not enforced UNIQUE by the schema.
Uniqueness is maintained by the upload loop — one upload produces one
mark_legacy_uploaded call. The column is TEXT NULL; NULL means not yet uploaded.

Idempotent: skips if already applied.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import yaml

MIGRATION_NAME = "migrate_031_legacy_upload"


def run_on_conn(conn: sqlite3.Connection) -> None:
    if conn.row_factory is None:
        conn.row_factory = sqlite3.Row

    try:
        row = conn.execute(
            "SELECT id FROM schema_migrations WHERE name = ?", (MIGRATION_NAME,)
        ).fetchone()
        if row is not None:
            return
    except sqlite3.OperationalError:
        # schema_migrations table doesn't exist yet — proceed with migration
        pass

    with conn:
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        # legacy_assets absent if migration 026 was never applied; still record
        # this migration so it isn't re-attempted on installs without the table.
        if "legacy_assets" in tables:
            existing = {r[1] for r in conn.execute("PRAGMA table_info(legacy_assets)").fetchall()}
            if "uploaded_flickr_id" not in existing:
                conn.execute("ALTER TABLE legacy_assets ADD COLUMN uploaded_flickr_id TEXT")
            if "uploaded_at" not in existing:
                conn.execute("ALTER TABLE legacy_assets ADD COLUMN uploaded_at TEXT")

        conn.execute(
            "INSERT INTO schema_migrations (name, applied_at) "
            "VALUES (?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))",
            (MIGRATION_NAME,),
        )


def run(db_path: str, dry_run: bool = False) -> None:
    if dry_run:
        print("  [dry-run] Would add uploaded_flickr_id and uploaded_at to legacy_assets")
        return
    conn = sqlite3.connect(db_path)
    run_on_conn(conn)
    conn.close()
    print("  Applied:  migrate_031_legacy_upload")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migration 031: add uploaded_flickr_id + uploaded_at to legacy_assets"
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
    raise SystemExit(main())
