"""
migrate_001_privacy_state_check.py — add CHECK constraint on privacy_state

SQLite doesn't support ALTER TABLE ADD CONSTRAINT, so we rebuild the
photos table with the constraint and copy data across.

This migration is safe to run multiple times (idempotent).

Usage:
    python db/migrate_001_privacy_state_check.py --config config/config.yml
"""

import argparse
import sqlite3
from pathlib import Path

import yaml

VALID_STATES = {
    "auto_private",
    "needs_review",
    "candidate_public",
    "approved_public",
    "keep_private",
    "already_public",
    "skipped",
    "duplicate_flickr",
}


def run(db_path: str, dry_run: bool = False):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Check if constraint already exists
    schema = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='photos'"
    ).fetchone()
    if schema and "CHECK" in (schema["sql"] or ""):
        print("CHECK constraint already present — nothing to do.")
        conn.close()
        return

    # Audit: find any rows with invalid states first
    rows = conn.execute("SELECT id, privacy_state FROM photos").fetchall()
    invalid = [
        (r["id"], r["privacy_state"]) for r in rows if r["privacy_state"] not in VALID_STATES
    ]

    if invalid:
        print(f"WARNING: {len(invalid)} rows have invalid privacy_state values:")
        for row_id, state in invalid[:10]:
            print(f"  id={row_id}: {state!r}")
        if len(invalid) > 10:
            print(f"  ... and {len(invalid) - 10} more")
        print("\nThese will be reset to 'needs_review' before adding the constraint.")
        if not dry_run:
            for row_id, _ in invalid:
                conn.execute(
                    "UPDATE photos SET privacy_state = 'needs_review' WHERE id = ?", (row_id,)
                )
            conn.commit()
            print(f"Reset {len(invalid)} rows.")

    if dry_run:
        print(f"[dry-run] Would migrate {len(rows)} rows, fix {len(invalid)} invalid states.")
        conn.close()
        return

    print(f"Migrating photos table ({len(rows)} rows)...")

    # Rebuild table with CHECK constraint
    # SQLite requires: rename → create new → copy → drop old
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("BEGIN")

    try:
        conn.execute("ALTER TABLE photos RENAME TO photos_old")

        # Read new schema from schema.sql
        schema_path = Path(__file__).parent / "schema.sql"
        new_schema = schema_path.read_text()

        # Execute just the CREATE TABLE photos statement
        import re

        match = re.search(
            r"(CREATE TABLE IF NOT EXISTS photos\s*\(.*?\);)",
            new_schema,
            re.DOTALL,
        )
        if not match:
            raise RuntimeError("Could not find CREATE TABLE photos in schema.sql")
        conn.executescript(match.group(1))

        # Copy data
        cols = [r[1] for r in conn.execute("PRAGMA table_info(photos_old)").fetchall()]
        new_cols = [r[1] for r in conn.execute("PRAGMA table_info(photos)").fetchall()]
        shared = [c for c in cols if c in new_cols]
        col_list = ", ".join(shared)
        conn.execute(f"INSERT INTO photos ({col_list}) SELECT {col_list} FROM photos_old")

        conn.execute("DROP TABLE photos_old")
        conn.execute("COMMIT")
        conn.execute("PRAGMA foreign_keys = ON")

        # Verify
        count = conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
        print(f"Migration complete. {count} rows in new table.")

    except Exception as e:
        conn.execute("ROLLBACK")
        # Restore if needed
        try:
            conn.execute("ALTER TABLE photos_old RENAME TO photos")
        except Exception:
            pass
        raise RuntimeError(f"Migration failed: {e}") from e
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Blue Pearmain DB migration 001")
    parser.add_argument("--config", default="config/config.yml")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with open(args.config) as f:
        config = yaml.safe_load(f)

    db_path = str(Path(config["database"]["path"]).expanduser())
    print(f"Database: {db_path}")

    run(db_path, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
