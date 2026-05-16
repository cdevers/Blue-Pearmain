"""
migrate_015_friends_family.py

Widens the privacy_state CHECK constraint on the photos table to add three
new Friends/Family visibility states:
  - 'approved_friends'
  - 'approved_family'
  - 'approved_friends_family'

SQLite cannot ALTER a CHECK constraint in place, so this uses the standard
rename / recreate / copy / drop approach.

Safe to run multiple times (idempotent — detects new states already present).

Usage:
    python db/migrations/migrate_015_friends_family.py --config config/config.yml
"""

import argparse
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import yaml

MIGRATION_NAME = "migrate_015_friends_family"
_NEW_STATES = ("approved_friends", "approved_family", "approved_friends_family")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _already_migrated(conn: sqlite3.Connection) -> bool:
    """Return True if the migration has already been recorded or the new states are in the schema."""
    try:
        row = conn.execute(
            "SELECT id FROM schema_migrations WHERE name = ?", (MIGRATION_NAME,)
        ).fetchone()
        if row is not None:
            return True
    except Exception:
        pass

    schema_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='photos'"
    ).fetchone()
    if schema_row and "approved_friends" in (schema_row[0] or ""):
        return True

    return False


def run(db_path: str, dry_run: bool = False) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    if _already_migrated(conn):
        print("  Skipped:  migration already applied")
        conn.close()
        return

    row_count = conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0]

    if dry_run:
        print(f"  [dry-run] Would widen privacy_state CHECK ({row_count} rows)")
        conn.close()
        return

    schema_path = Path(__file__).parent.parent / "schema.sql"
    new_schema = schema_path.read_text()
    match = re.search(
        r"(CREATE TABLE IF NOT EXISTS photos\s*\(.*?\);)",
        new_schema,
        re.DOTALL,
    )
    if not match:
        conn.close()
        raise RuntimeError("Could not find CREATE TABLE photos in schema.sql")

    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("BEGIN")

    try:
        conn.execute("ALTER TABLE photos RENAME TO photos_old")

        conn.executescript(match.group(1))

        cols_old = [r[1] for r in conn.execute("PRAGMA table_info(photos_old)").fetchall()]
        cols_new = [r[1] for r in conn.execute("PRAGMA table_info(photos)").fetchall()]
        shared = [c for c in cols_old if c in cols_new]
        col_list = ", ".join(shared)
        conn.execute(f"INSERT INTO photos ({col_list}) SELECT {col_list} FROM photos_old")

        conn.execute("DROP TABLE photos_old")

        # Recreate photos-specific indexes from schema.sql
        for stmt in re.findall(
            r"CREATE INDEX IF NOT EXISTS \S+ ON photos\b.*?;", new_schema, re.DOTALL
        ):
            conn.execute(stmt)

        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (name, applied_at) VALUES (?, ?)",
            (MIGRATION_NAME, now_iso()),
        )
        conn.execute("COMMIT")
        conn.execute("PRAGMA foreign_keys = ON")
        print(f"  Applied:  {MIGRATION_NAME} ({row_count} rows migrated)")

    except Exception as e:
        conn.execute("ROLLBACK")
        try:
            conn.execute("ALTER TABLE photos_old RENAME TO photos")
        except Exception:
            pass
        conn.close()
        raise RuntimeError(f"Migration failed: {e}") from e

    conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migration 015: widen privacy_state CHECK for friends/family states"
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
    import sys

    sys.exit(main())
