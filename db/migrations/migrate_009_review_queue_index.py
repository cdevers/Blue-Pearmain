"""
migrate_009_review_queue_index.py

Adds a composite index on (privacy_state, date_taken DESC, id DESC) so the
review-grid query can use the index for both filtering and ordering, avoiding
a full 120k-row temp B-tree sort on every page load.

Safe to run multiple times (idempotent — uses CREATE INDEX IF NOT EXISTS).

Usage:
    python db/migrations/migrate_009_review_queue_index.py --config config/config.yml
"""

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import yaml


MIGRATION_NAME = "migrate_009_review_queue_index"


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
        print("  [dry-run] Would create idx_photos_review_queue")
        conn.close()
        return

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_photos_review_queue "
        "ON photos(privacy_state, date_taken DESC, id DESC)"
    )
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (name, applied_at) VALUES (?, ?)",
        (MIGRATION_NAME, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()
    print("  Applied:  Created idx_photos_review_queue")


def main():
    parser = argparse.ArgumentParser(description="Blue Pearmain DB migration 009")
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
