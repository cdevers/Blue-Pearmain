"""
migrate_002_updated_at_and_indexes.py

Adds:
  - photos.updated_at column (ISO8601, set on every upsert going forward)
  - Composite index on (privacy_state, perms_pushed_flickr) for stats/reconcile queries
  - Index on updated_at
  - schema_migrations table for tracking applied migrations

Safe to run multiple times (idempotent).

Usage:
    python db/migrate_002_updated_at_and_indexes.py --config config/config.yml
"""

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import yaml


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run(db_path: str, dry_run: bool = False):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    steps_applied = []
    steps_skipped = []

    # ------------------------------------------------------------------
    # 1. schema_migrations table
    # ------------------------------------------------------------------
    existing = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if not existing:
        if not dry_run:
            conn.execute("""
                CREATE TABLE schema_migrations (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    name        TEXT NOT NULL UNIQUE,
                    applied_at  TEXT NOT NULL
                )
            """)
            conn.commit()
        steps_applied.append("Created schema_migrations table")
    else:
        steps_skipped.append("schema_migrations table already exists")

    # Helper: check if migration already recorded
    def already_applied(name):
        try:
            row = conn.execute(
                "SELECT id FROM schema_migrations WHERE name = ?", (name,)
            ).fetchone()
            return row is not None
        except Exception:
            return False

    def record_migration(name):
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (name, applied_at) VALUES (?, ?)",
            (name, now_iso()),
        )
        conn.commit()

    # ------------------------------------------------------------------
    # 2. updated_at column
    # ------------------------------------------------------------------
    migration_name = "migrate_002_updated_at"
    cols = [r[1] for r in conn.execute("PRAGMA table_info(photos)").fetchall()]
    if "updated_at" not in cols:
        if not dry_run:
            conn.execute("ALTER TABLE photos ADD COLUMN updated_at TEXT")
            conn.commit()
            record_migration(migration_name)
        steps_applied.append("Added photos.updated_at column")
    else:
        steps_skipped.append("photos.updated_at already exists")

    # ------------------------------------------------------------------
    # 3. Indexes
    # ------------------------------------------------------------------
    existing_indexes = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
    }

    new_indexes = {
        "idx_photos_push_state": (
            "CREATE INDEX idx_photos_push_state ON photos(privacy_state, perms_pushed_flickr)"
        ),
        "idx_photos_tags_pushed": (
            "CREATE INDEX idx_photos_tags_pushed ON photos(tags_pushed_flickr)"
        ),
        "idx_photos_updated": ("CREATE INDEX idx_photos_updated ON photos(updated_at)"),
    }

    for idx_name, ddl in new_indexes.items():
        if idx_name not in existing_indexes:
            if not dry_run:
                conn.execute(ddl)
                conn.commit()
            steps_applied.append(f"Created index {idx_name}")
        else:
            steps_skipped.append(f"Index {idx_name} already exists")

    # ------------------------------------------------------------------
    # 4. Record migrate_001 as applied if it was already run
    # ------------------------------------------------------------------
    schema = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='photos'"
    ).fetchone()
    if schema and "CHECK" in (schema["sql"] or ""):
        if not already_applied("migrate_001_privacy_state_check"):
            if not dry_run:
                conn.execute(
                    "INSERT OR IGNORE INTO schema_migrations (name, applied_at) VALUES (?, ?)",
                    ("migrate_001_privacy_state_check", now_iso()),
                )
                conn.commit()
            steps_applied.append("Recorded migrate_001 as applied")
        else:
            steps_skipped.append("migrate_001 already recorded")

    # ------------------------------------------------------------------
    # Report
    # ------------------------------------------------------------------
    prefix = "[dry-run] " if dry_run else ""
    for s in steps_applied:
        print(f"  {prefix}Applied:  {s}")
    for s in steps_skipped:
        print(f"  Skipped:  {s}")

    if not steps_applied:
        print("  Nothing to do.")

    conn.close()


def main():
    parser = argparse.ArgumentParser(description="Blue Pearmain DB migration 002")
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
