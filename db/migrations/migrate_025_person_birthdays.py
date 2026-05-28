"""
migrate_025_person_birthdays.py

Adds the person_birthdays table. Each row stores an optional birthday
for a named person so the app can display age-at-time and filter by
birthday in the map and library views.

birthday is stored as 'MM-DD' (recurring annual) or 'YYYY-MM-DD'
(full known date, allows age calculation).

Safe to run multiple times (idempotent).

Usage:
    python db/migrations/migrate_025_person_birthdays.py --config config/config.yml
"""

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import yaml

MIGRATION_NAME = "migrate_025_person_birthdays"


def _already_migrated(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute(
            "SELECT name FROM schema_migrations WHERE name = ?", (MIGRATION_NAME,)
        ).fetchone()
        if row is not None:
            return True
    except Exception:
        pass
    tables = [
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    ]
    return "person_birthdays" in tables


def run(db_path: str, dry_run: bool = False) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    if _already_migrated(conn):
        print("  Skipped:  migration already applied")
        conn.close()
        return

    ddl = """
        CREATE TABLE person_birthdays (
            person_name  TEXT PRIMARY KEY,
            birthday     TEXT NOT NULL,
            created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        )
    """

    if not dry_run:
        conn.execute(ddl)
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (name, applied_at) VALUES (?, ?)",
            (MIGRATION_NAME, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        print("  Applied:  created person_birthdays table")
    else:
        print("  Dry-run:  would create person_birthdays table")

    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/config.yml")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    with open(args.config) as f:
        config = yaml.safe_load(f)
    db_path = str(Path(config["database"]["path"]).expanduser())
    run(db_path, dry_run=args.dry_run)
