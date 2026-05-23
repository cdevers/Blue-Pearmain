"""
migrate_022_bp_rating.py

Adds:
  photos.bp_rating INTEGER NOT NULL DEFAULT 0

bp_rating is the canonical 0–5 star rating stored in BP.
  0 = unrated, 1–5 = star count.

No backfill: the scanner seeds values from photo.favorite on its next run.

Safe to run multiple times (idempotent — checks schema_migrations).

Usage:
    python db/migrations/migrate_022_bp_rating.py --config config/config.yml
"""

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import yaml

MIGRATION_NAME = "migrate_022_bp_rating"


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

    existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(photos)").fetchall()}

    if dry_run:
        if "bp_rating" not in existing_cols:
            print("  [dry-run] Would add photos.bp_rating column")
        else:
            print("  [dry-run] photos.bp_rating already exists")
        conn.close()
        return

    conn.execute("BEGIN")

    if "bp_rating" not in existing_cols:
        conn.execute("ALTER TABLE photos ADD COLUMN bp_rating INTEGER NOT NULL DEFAULT 0")

    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (name, applied_at) VALUES (?, ?)",
        (MIGRATION_NAME, now_iso()),
    )
    conn.commit()
    conn.close()
    print("  Applied:  migrate_022_bp_rating")


def main() -> int:
    parser = argparse.ArgumentParser(description="Migration 022: add bp_rating column")
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
