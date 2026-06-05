"""
migrate_029_date_precision.py

Add date_precision and date_approximate columns to photos and legacy_assets.

date_precision  — TEXT NOT NULL DEFAULT 'exact'
    One of: exact | day | month | year | decade | unknown
    Controls how date_taken is displayed and interpreted.

date_approximate — INTEGER NOT NULL DEFAULT 0
    When 1, the date is a best guess ("c. 1975" rather than "1975").

Idempotent: skips if already applied.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import yaml

MIGRATION_NAME = "migrate_029_date_precision"


def run_on_conn(conn: sqlite3.Connection) -> None:
    if conn.row_factory is None:
        conn.row_factory = sqlite3.Row

    try:
        row = conn.execute(
            "SELECT id FROM schema_migrations WHERE name = ?", (MIGRATION_NAME,)
        ).fetchone()
        if row is not None:
            return
    except Exception:
        pass

    conn.execute("BEGIN")

    existing_photos = {r[1] for r in conn.execute("PRAGMA table_info(photos)").fetchall()}
    if "date_precision" not in existing_photos:
        conn.execute(
            "ALTER TABLE photos ADD COLUMN date_precision TEXT NOT NULL DEFAULT 'exact' "
            "CHECK(date_precision IN ('exact','day','month','year','decade','unknown'))"
        )
    if "date_approximate" not in existing_photos:
        conn.execute("ALTER TABLE photos ADD COLUMN date_approximate INTEGER NOT NULL DEFAULT 0")

    existing_legacy = {r[1] for r in conn.execute("PRAGMA table_info(legacy_assets)").fetchall()}
    if "date_precision" not in existing_legacy:
        conn.execute(
            "ALTER TABLE legacy_assets ADD COLUMN date_precision TEXT NOT NULL DEFAULT 'exact' "
            "CHECK(date_precision IN ('exact','day','month','year','decade','unknown'))"
        )
    if "date_approximate" not in existing_legacy:
        conn.execute(
            "ALTER TABLE legacy_assets ADD COLUMN date_approximate INTEGER NOT NULL DEFAULT 0"
        )

    conn.execute(
        "INSERT INTO schema_migrations (name, applied_at) "
        "VALUES (?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))",
        (MIGRATION_NAME,),
    )
    conn.execute("COMMIT")


def run(db_path: str, dry_run: bool = False) -> None:
    if dry_run:
        print(
            "  [dry-run] Would add date_precision and date_approximate to photos and legacy_assets"
        )
        return
    conn = sqlite3.connect(db_path)
    run_on_conn(conn)
    conn.close()
    print("  Applied:  migrate_029_date_precision")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migration 029: add date_precision + date_approximate columns"
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
    raise SystemExit(main())
