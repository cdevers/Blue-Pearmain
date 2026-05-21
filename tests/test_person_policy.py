"""
tests/test_person_policy.py — tests for per-person privacy policy

Run from repo root:
    python -m pytest tests/test_person_policy.py -v
"""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.db import Database
from db.migrations.migrate_019_person_policies import run as run_migration, _already_migrated


def _tmp_db() -> tuple[sqlite3.Connection, str]:
    """Create a minimal throw-away SQLite DB."""
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    conn = sqlite3.connect(f.name)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    conn.commit()
    return conn, f.name


class TestMigration019(unittest.TestCase):
    def test_creates_person_policies_table(self):
        conn, path = _tmp_db()
        run_migration(path)
        tables = [
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        ]
        self.assertIn("person_policies", tables)
        conn.close()

    def test_table_has_expected_columns(self):
        conn, path = _tmp_db()
        run_migration(path)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(person_policies)").fetchall()]
        for col in ("id", "person_name", "policy", "created_at"):
            self.assertIn(col, cols)
        conn.close()

    def test_idempotent_second_run_does_not_fail(self):
        conn, path = _tmp_db()
        run_migration(path)
        run_migration(path)  # must not raise
        conn.close()

    def test_already_migrated_returns_true_after_run(self):
        conn, path = _tmp_db()
        run_migration(path)
        self.assertTrue(_already_migrated(conn))
        conn.close()

    def test_already_migrated_returns_false_before_run(self):
        conn, path = _tmp_db()
        self.assertFalse(_already_migrated(conn))
        conn.close()


def _make_db_with_migration() -> Database:
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    db = Database(Path(f.name))
    from db.migrations.migrate_019_person_policies import run as migrate

    migrate(f.name)
    return db


class TestPersonPolicyDbMethods(unittest.TestCase):
    def test_get_person_policies_returns_empty_dict_initially(self):
        db = _make_db_with_migration()
        result = db.get_person_policies()
        db.close()
        self.assertEqual(result, {})

    def test_set_person_policy_stores_a_policy(self):
        db = _make_db_with_migration()
        db.set_person_policy("Alice", "always_private")
        result = db.get_person_policies()
        db.close()
        self.assertEqual(result.get("Alice"), "always_private")

    def test_set_person_policy_upserts_on_duplicate_name(self):
        db = _make_db_with_migration()
        db.set_person_policy("Alice", "always_private")
        db.set_person_policy("Alice", "always_private")  # must not raise
        result = db.get_person_policies()
        db.close()
        self.assertEqual(list(result.keys()).count("Alice"), 1)

    def test_delete_person_policy_removes_entry(self):
        db = _make_db_with_migration()
        db.set_person_policy("Bob", "always_private")
        db.delete_person_policy("Bob")
        result = db.get_person_policies()
        db.close()
        self.assertNotIn("Bob", result)

    def test_delete_person_policy_no_op_when_not_present(self):
        db = _make_db_with_migration()
        db.delete_person_policy("Nobody")  # must not raise
        db.close()

    def test_get_person_policies_returns_all_policies(self):
        db = _make_db_with_migration()
        db.set_person_policy("Alice", "always_private")
        db.set_person_policy("Charlie", "always_private")
        result = db.get_person_policies()
        db.close()
        self.assertIn("Alice", result)
        self.assertIn("Charlie", result)
