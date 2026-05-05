"""
migrate_009_placeholder.py

Migration 009 was reserved for canonical metadata columns (photos_title_canonical,
photos_description_canonical) planned in Phase 7 of the metadata sync architecture.
That feature was deferred; the columns were never added.

This placeholder keeps the numbering contiguous so the gap is self-documenting
rather than mysterious. It is a no-op: it makes no schema changes.

Safe to run multiple times (idempotent).

Usage:
    python db/migrations/migrate_009_placeholder.py --config config/config.yml
"""

import argparse
from datetime import datetime, timezone
from pathlib import Path

import sqlite3
import yaml


MIGRATION_NAME = "migrate_009_placeholder"


def run(db_path: str, dry_run: bool = False):
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
        print("  [dry-run] No-op placeholder — nothing to do")
        conn.close()
        return

    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (name, applied_at) VALUES (?, ?)",
        (MIGRATION_NAME, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()
    print("  Applied:  No-op placeholder for reserved migration 009")


def main():
    parser = argparse.ArgumentParser(description="Blue Pearmain DB migration 009 (placeholder)")
    parser.add_argument("--config",  default="config/config.yml")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with open(args.config) as f:
        config = yaml.safe_load(f)

    db_path = str(Path(config["database"]["path"]).expanduser())
    print(f"Database: {db_path}")
    run(db_path, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
