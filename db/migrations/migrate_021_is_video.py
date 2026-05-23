"""
migrate_021_is_video.py

Adds:
  photos.is_video INTEGER NOT NULL DEFAULT 0

Backfills is_video=1 for existing rows where the filename extension is
.mov, .mp4, or .m4v (case-insensitive). HEIC files are NOT videos in BP's
model — they are handled as stills. Live Photos (.heic with embedded clip)
are likewise treated as stills.

Safe to run multiple times (idempotent — checks schema_migrations).

Usage:
    python db/migrations/migrate_021_is_video.py --config config/config.yml
"""

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import yaml

MIGRATION_NAME = "migrate_021_is_video"


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
        if "is_video" not in existing_cols:
            print("  [dry-run] Would add photos.is_video column")
            print("  [dry-run] Would backfill is_video=1 for .mov/.mp4/.m4v files")
        else:
            print("  [dry-run] photos.is_video already exists")
        conn.close()
        return

    conn.execute("BEGIN")

    if "is_video" not in existing_cols:
        conn.execute("ALTER TABLE photos ADD COLUMN is_video INTEGER NOT NULL DEFAULT 0")

    conn.execute(
        """UPDATE photos
           SET is_video = 1
           WHERE lower(original_filename) LIKE '%.mov'
              OR lower(original_filename) LIKE '%.mp4'
              OR lower(original_filename) LIKE '%.m4v'"""
    )

    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (name, applied_at) VALUES (?, ?)",
        (MIGRATION_NAME, now_iso()),
    )
    conn.commit()
    conn.close()
    print("  Applied:  migrate_021_is_video")


def main() -> int:
    parser = argparse.ArgumentParser(description="Migration 021: add is_video flag")
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
