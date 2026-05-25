"""
migrate_023_bulk_batches.py

Adds:
  bulk_batches table — one row per confirmed bulk edit operation
  metadata_proposals.batch_id INTEGER (nullable FK → bulk_batches.id)

Safe to run multiple times (idempotent — checks schema_migrations).

Usage:
    python db/migrations/migrate_023_bulk_batches.py --config config/config.yml
"""

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import yaml

MIGRATION_NAME = "migrate_023_bulk_batches"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run(db_path: str, dry_run: bool = False) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

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

    tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    proposal_cols = {r[1] for r in conn.execute("PRAGMA table_info(metadata_proposals)").fetchall()}

    if dry_run:
        if "bulk_batches" not in tables:
            print("  [dry-run] Would create bulk_batches table")
        if "batch_id" not in proposal_cols:
            print("  [dry-run] Would add metadata_proposals.batch_id column")
        conn.close()
        return

    conn.execute("BEGIN")

    if "bulk_batches" not in tables:
        conn.execute("""
            CREATE TABLE bulk_batches (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                operation   TEXT NOT NULL,
                field       TEXT,
                value       TEXT,
                tags        TEXT,
                filter      TEXT,  -- audit metadata only, not executable replay state
                photo_count INTEGER NOT NULL,
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)

    if "batch_id" not in proposal_cols:
        conn.execute(
            "ALTER TABLE metadata_proposals ADD COLUMN batch_id INTEGER REFERENCES bulk_batches(id)"
        )
        # Index for fast batch-level lookups (grouping + batch-reject)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_proposals_batch
            ON metadata_proposals(batch_id)
            WHERE batch_id IS NOT NULL
        """)

    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (name, applied_at) VALUES (?, ?)",
        (MIGRATION_NAME, now_iso()),
    )
    conn.commit()
    conn.close()
    print("  Applied:  migrate_023_bulk_batches")


def main() -> int:
    parser = argparse.ArgumentParser(description="Migration 023: bulk_batches table")
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
