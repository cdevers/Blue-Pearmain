"""
tests/test_operation_log.py — unit tests for the operation_log feature

Run from repo root:
    python -m pytest tests/test_operation_log.py -v
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.migrations.migrate_020_operation_log import run as run_migration


def _tmp_db_path() -> str:
    """Create a minimal throw-away SQLite DB with schema_migrations table."""
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    conn = sqlite3.connect(f.name)
    conn.execute("CREATE TABLE schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    conn.commit()
    conn.close()
    return f.name


class TestMigration020(unittest.TestCase):
    def test_creates_operation_log_table(self):
        path = _tmp_db_path()
        try:
            run_migration(path)
            conn = sqlite3.connect(path)
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            conn.close()
            self.assertIn("operation_log", tables)
        finally:
            os.unlink(path)

    def test_table_has_required_columns(self):
        path = _tmp_db_path()
        try:
            run_migration(path)
            conn = sqlite3.connect(path)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(operation_log)").fetchall()}
            conn.close()
            for col in (
                "id",
                "occurred_at",
                "photo_id",
                "operation",
                "target",
                "old_value",
                "new_value",
                "trigger",
                "actor",
            ):
                self.assertIn(col, cols)
        finally:
            os.unlink(path)

    def test_idempotent_when_run_twice(self):
        path = _tmp_db_path()
        try:
            run_migration(path)
            run_migration(path)  # Must not raise or duplicate anything
            conn = sqlite3.connect(path)
            count = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='operation_log'"
            ).fetchone()[0]
            conn.close()
            self.assertEqual(count, 1)
        finally:
            os.unlink(path)
