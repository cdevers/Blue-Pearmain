"""
migrate_030_nominatim_cache.py

Create the nominatim_cache table for reverse geocoding results (#217).

Idempotent: skips if already applied.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import yaml

MIGRATION_NAME = "migrate_030_nominatim_cache"


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

    conn.execute("BEGIN")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS nominatim_cache (
            lat_rounded        REAL NOT NULL,
            lon_rounded        REAL NOT NULL,
            place_city         TEXT,
            place_state        TEXT,
            place_country      TEXT,
            place_country_code TEXT,
            place_neighborhood TEXT,
            place_address      TEXT,
            fetched_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            PRIMARY KEY (lat_rounded, lon_rounded)
        )
    """)

    conn.execute(
        "INSERT INTO schema_migrations (name, applied_at) "
        "VALUES (?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))",
        (MIGRATION_NAME,),
    )
    conn.execute("COMMIT")


def run(db_path: str, dry_run: bool = False) -> None:
    if dry_run:
        print("  [dry-run] Would create nominatim_cache table")
        return
    conn = sqlite3.connect(db_path)
    run_on_conn(conn)
    conn.close()
    print("  Applied:  migrate_030_nominatim_cache")


def main() -> int:
    parser = argparse.ArgumentParser(description="Migration 030: create nominatim_cache table")
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
