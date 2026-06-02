"""
migrate_028_rebuild_fk_tables.py

Rebuild operation_log, metadata_conflicts, and duplicate_groups to clear
stale SQLite internal FK metadata.

After migrations 001 and 016 rebuilt the `photos` table via the standard
rename/create/copy/drop pattern, any table that was created *before* those
migrations and holds a FK → photos retains stale internal metadata pointing
to the old, now-dropped `photos_old` stub.  With `PRAGMA foreign_keys = ON`,
any write to such a table fails with "no such table: main.photos_old".

Affected tables (confirmed via sqlite_master inspection):
  - operation_log        (photo_id REFERENCES "photos_old"(id))
  - metadata_conflicts   (photo_id NOT NULL REFERENCES "photos_old"(id))
  - duplicate_groups     (keeper_id REFERENCES "photos_old"(id))

Impact discovered: all reclassify_legacy_match() and apply_legacy_metadata()
calls in `match-legacy --apply` fail silently — both write to operation_log
inside a transaction, so the FK error rolls back the entire photo write.

Fix: rebuild each table with the same rename/create/copy/drop pattern used by
migrations 001, 016, and 027.  Fresh CREATE TABLE statements reference the
current `photos` table correctly.

Idempotent: skips if already applied.

Related: #178 (photo_albums, fixed by migration 027 in v1.5.3).
"""

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import yaml

MIGRATION_NAME = "migrate_028_rebuild_fk_tables"


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

    conn.execute("PRAGMA foreign_keys = OFF")

    # -----------------------------------------------------------------------
    # operation_log
    # -----------------------------------------------------------------------
    conn.execute("BEGIN")
    conn.execute("ALTER TABLE operation_log RENAME TO operation_log_old")
    conn.execute(
        """
        CREATE TABLE operation_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            occurred_at TEXT NOT NULL,
            photo_id    INTEGER REFERENCES photos(id),
            operation   TEXT NOT NULL,
            target      TEXT,
            old_value   TEXT,
            new_value   TEXT,
            trigger     TEXT,
            actor       TEXT NOT NULL DEFAULT 'bp'
        )
        """
    )
    conn.execute("INSERT INTO operation_log SELECT * FROM operation_log_old")
    conn.execute("DROP TABLE operation_log_old")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_operation_log_photo ON operation_log(photo_id)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_operation_log_operation ON operation_log(operation)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_operation_log_occurred ON operation_log(occurred_at)"
    )
    conn.execute("COMMIT")

    # -----------------------------------------------------------------------
    # metadata_conflicts
    # -----------------------------------------------------------------------
    conn.execute("BEGIN")
    conn.execute("ALTER TABLE metadata_conflicts RENAME TO metadata_conflicts_old")
    conn.execute(
        """
        CREATE TABLE metadata_conflicts (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            photo_id        INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
            field           TEXT NOT NULL
                                CHECK(field IN ('title', 'description', 'tags')),
            flickr_value    TEXT,
            photos_value    TEXT,
            resolved        INTEGER DEFAULT 0,
            resolution      TEXT
                                CHECK(resolution IS NULL OR
                                      resolution IN ('flickr', 'photos', 'manual')),
            resolved_at     TEXT,
            created_at      TEXT NOT NULL,
            UNIQUE(photo_id, field)
        )
        """
    )
    conn.execute("INSERT INTO metadata_conflicts SELECT * FROM metadata_conflicts_old")
    conn.execute("DROP TABLE metadata_conflicts_old")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_metadata_conflicts_photo ON metadata_conflicts(photo_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_metadata_conflicts_unresolved "
        "ON metadata_conflicts(resolved) WHERE resolved = 0"
    )
    conn.execute("COMMIT")

    # -----------------------------------------------------------------------
    # duplicate_groups
    # -----------------------------------------------------------------------
    conn.execute("BEGIN")
    conn.execute("ALTER TABLE duplicate_groups RENAME TO duplicate_groups_old")
    conn.execute(
        """
        CREATE TABLE duplicate_groups (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            match_key       TEXT NOT NULL UNIQUE,
            group_type      TEXT NOT NULL,
            photo_count     INTEGER NOT NULL DEFAULT 0,
            keeper_id       INTEGER REFERENCES photos(id),
            resolved        INTEGER NOT NULL DEFAULT 0,
            resolved_at     TEXT,
            notes           TEXT,
            created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        )
        """
    )
    conn.execute("INSERT INTO duplicate_groups SELECT * FROM duplicate_groups_old")
    conn.execute("DROP TABLE duplicate_groups_old")
    conn.execute("COMMIT")

    # -----------------------------------------------------------------------
    # Record migration
    # -----------------------------------------------------------------------
    conn.execute("BEGIN")
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (name, applied_at) VALUES (?, ?)",
        (MIGRATION_NAME, datetime.now(timezone.utc).isoformat()),
    )
    conn.execute("COMMIT")

    conn.execute("PRAGMA foreign_keys = ON")


def run(db_path: str, dry_run: bool = False) -> None:
    if dry_run:
        print(
            "  [dry-run] Would rebuild operation_log, metadata_conflicts, "
            "duplicate_groups to clear stale FK metadata"
        )
        return
    conn = sqlite3.connect(db_path)
    run_on_conn(conn)
    conn.close()
    print("  Applied:  migrate_028_rebuild_fk_tables")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migration 028: rebuild operation_log/metadata_conflicts/"
        "duplicate_groups to clear stale SQLite FK metadata"
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
