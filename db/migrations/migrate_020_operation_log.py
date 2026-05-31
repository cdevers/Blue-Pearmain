"""
migrate_020_operation_log.py

Creates the operation_log table: an append-only journal of every
significant mutation BP makes (review decisions, proposal applies,
reconcile fixes, tag writebacks).

Safe to run multiple times (idempotent).

Usage:
    python db/migrations/migrate_020_operation_log.py --config config/config.yml
"""

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import yaml

MIGRATION_NAME = "migrate_020_operation_log"


def _already_migrated(conn: sqlite3.Connection) -> bool:
    # Skip only when this migration's name is recorded. Do NOT short-circuit on
    # table existence: operation_log is bootstrapped by Database._ensure_schema,
    # so a table-existence check would skip without ever recording the name and
    # leave the migration perpetually "pending" (#170). The DDL below is all
    # CREATE ... IF NOT EXISTS, so re-running it when the table exists is safe.
    try:
        row = conn.execute(
            "SELECT id FROM schema_migrations WHERE name = ?", (MIGRATION_NAME,)
        ).fetchone()
        return row is not None
    except Exception:
        return False


def run(db_path: str, dry_run: bool = False) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    if _already_migrated(conn):
        print("  Skipped:  migration already applied")
        conn.close()
        return

    if not dry_run:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS operation_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                occurred_at TEXT NOT NULL,
                photo_id    INTEGER REFERENCES photos(id),
                operation   TEXT NOT NULL,
                target      TEXT,
                old_value   TEXT,
                new_value   TEXT,
                trigger     TEXT,
                actor       TEXT NOT NULL DEFAULT 'bp'
            );
            CREATE INDEX IF NOT EXISTS idx_operation_log_photo
                ON operation_log(photo_id);
            CREATE INDEX IF NOT EXISTS idx_operation_log_operation
                ON operation_log(operation);
            CREATE INDEX IF NOT EXISTS idx_operation_log_occurred
                ON operation_log(occurred_at);
        """)
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (name, applied_at) VALUES (?, ?)",
            (MIGRATION_NAME, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        print("  Applied:  created operation_log table and indexes")
    else:
        print("  Dry-run:  would create operation_log table and indexes")

    conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Migration 020 — create operation_log table")
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
