"""
tests/test_person_birthdays.py — tests for person birthday feature (#152)

Covers:
  - Migration 025: creates person_birthdays table
  - CuratorDB: get/set/delete_person_birthday
  - time_patterns.birthday_clause: exact-day and expanded-window SQL generation
  - API: POST /api/person-birthday, DELETE /api/person-birthday/<name>

Run from repo root:
    python -m pytest tests/test_person_birthdays.py -v
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import reviewer.app as app_module
from db.db import Database
from db.migrations.migrate_025_person_birthdays import (
    MIGRATION_NAME,
    run as run_migration,
)
from db.time_patterns import birthday_clause


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tmp_db() -> tuple[sqlite3.Connection, str]:
    """Create a minimal throw-away SQLite DB with schema_migrations table."""
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    conn = sqlite3.connect(f.name)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    conn.commit()
    return conn, f.name


def _db_with_migration() -> tuple[Database, str]:
    """Return a CuratorDB instance backed by a fresh DB with migration 025 applied."""
    conn, path = _tmp_db()
    conn.close()
    run_migration(path)
    return Database(path), path


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


class TestMigration025(unittest.TestCase):
    def test_creates_person_birthdays_table(self) -> None:
        conn, path = _tmp_db()
        conn.close()
        run_migration(path)
        conn = sqlite3.connect(path)
        tables = [
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        ]
        self.assertIn("person_birthdays", tables)
        conn.close()

    def test_idempotent(self) -> None:
        """Running twice should not raise."""
        conn, path = _tmp_db()
        conn.close()
        run_migration(path)
        run_migration(path)  # should not raise

    def test_records_in_schema_migrations(self) -> None:
        conn, path = _tmp_db()
        conn.close()
        run_migration(path)
        conn = sqlite3.connect(path)
        row = conn.execute(
            "SELECT name FROM schema_migrations WHERE name = ?", (MIGRATION_NAME,)
        ).fetchone()
        self.assertIsNotNone(row)
        conn.close()


# ---------------------------------------------------------------------------
# CuratorDB CRUD
# ---------------------------------------------------------------------------


class TestPersonBirthdayCRUD(unittest.TestCase):
    def setUp(self) -> None:
        self.db, self.path = _db_with_migration()

    def test_get_empty(self) -> None:
        self.assertEqual(self.db.get_person_birthdays(), {})

    def test_set_and_get_mm_dd(self) -> None:
        self.db.set_person_birthday("Alice", "05-15")
        result = self.db.get_person_birthdays()
        self.assertEqual(result, {"Alice": "05-15"})

    def test_set_and_get_full_date(self) -> None:
        self.db.set_person_birthday("Bob", "1990-11-03")
        result = self.db.get_person_birthdays()
        self.assertEqual(result, {"Bob": "1990-11-03"})

    def test_upsert_updates_value(self) -> None:
        self.db.set_person_birthday("Alice", "05-15")
        self.db.set_person_birthday("Alice", "05-16")
        result = self.db.get_person_birthdays()
        self.assertEqual(result["Alice"], "05-16")

    def test_multiple_people(self) -> None:
        self.db.set_person_birthday("Alice", "05-15")
        self.db.set_person_birthday("Bob", "11-03")
        result = self.db.get_person_birthdays()
        self.assertEqual(len(result), 2)
        self.assertEqual(result["Alice"], "05-15")
        self.assertEqual(result["Bob"], "11-03")

    def test_delete_removes_entry(self) -> None:
        self.db.set_person_birthday("Alice", "05-15")
        self.db.delete_person_birthday("Alice")
        self.assertEqual(self.db.get_person_birthdays(), {})

    def test_delete_nonexistent_is_noop(self) -> None:
        """Deleting an unknown person should not raise."""
        self.db.delete_person_birthday("Nobody")  # should not raise

    def test_get_returns_empty_if_table_missing(self) -> None:
        """get_person_birthdays returns {} gracefully if migration not applied."""
        conn, path = _tmp_db()
        conn.close()
        db_no_table = Database(path)
        self.assertEqual(db_no_table.get_person_birthdays(), {})


# ---------------------------------------------------------------------------
# birthday_clause
# ---------------------------------------------------------------------------


class TestBirthdayClause(unittest.TestCase):
    def test_exact_day_single_year(self) -> None:
        frag, params = birthday_clause(5, 15, 0, [2020])
        self.assertIn("strftime('%Y-%m-%d'", frag)
        self.assertIn("2020-05-15", params)

    def test_exact_day_multiple_years(self) -> None:
        frag, params = birthday_clause(5, 15, 0, [2020, 2021, 2022])
        self.assertEqual(len(params), 3)
        self.assertIn("2021-05-15", params)

    def test_expanded_window_returns_between_clause(self) -> None:
        frag, params = birthday_clause(5, 15, 2, [2020])
        self.assertIn("BETWEEN", frag)
        # lo = 2020-05-13, hi = 2020-05-17T23:59:59
        self.assertIn("2020-05-13", params)
        self.assertIn("2020-05-17T23:59:59", params)

    def test_empty_years_returns_no_op(self) -> None:
        frag, params = birthday_clause(5, 15, 0, [])
        self.assertEqual(frag, "1=1")
        self.assertEqual(params, [])

    def test_leap_day_skipped_in_non_leap_years(self) -> None:
        # Feb 29 only valid in 2020 and 2024 (leap), not 2021/2022/2023
        frag, params = birthday_clause(2, 29, 0, [2020, 2021, 2022, 2023, 2024])
        self.assertIn("2020-02-29", params)
        self.assertIn("2024-02-29", params)
        self.assertNotIn("2021-02-29", params)
        self.assertNotIn("2022-02-29", params)
        self.assertNotIn("2023-02-29", params)

    def test_all_non_leap_returns_no_op(self) -> None:
        frag, params = birthday_clause(2, 29, 0, [2021, 2022, 2023])
        self.assertEqual(frag, "1=1")
        self.assertEqual(params, [])


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------


class TestPersonBirthdayAPI(unittest.TestCase):
    def setUp(self) -> None:
        self.db, self.path = _db_with_migration()
        app_module._db = self.db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test-secret"
        self.client = app_module.app.test_client()

    def tearDown(self) -> None:
        app_module._db = None

    def _post(self, payload: dict):
        return self.client.post(
            "/api/person-birthday",
            data=json.dumps(payload),
            content_type="application/json",
        )

    def _delete(self, name: str):
        from urllib.parse import quote

        return self.client.delete(f"/api/person-birthday/{quote(name)}")

    def test_set_birthday_mm_dd(self) -> None:
        resp = self._post({"person_name": "Alice", "birthday": "05-15"})
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertTrue(data["ok"])

    def test_set_birthday_full_date(self) -> None:
        resp = self._post({"person_name": "Bob", "birthday": "1990-11-03"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(json.loads(resp.data)["ok"])

    def test_set_birthday_bad_format_returns_400(self) -> None:
        resp = self._post({"person_name": "Alice", "birthday": "not-a-date"})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(json.loads(resp.data)["ok"])

    def test_set_birthday_missing_fields_returns_400(self) -> None:
        resp = self._post({"person_name": "Alice"})
        self.assertEqual(resp.status_code, 400)

    def test_delete_birthday(self) -> None:
        self._post({"person_name": "Alice", "birthday": "05-15"})
        resp = self._delete("Alice")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(json.loads(resp.data)["ok"])
        # Confirm removed
        bdays = Database(self.path).get_person_birthdays()
        self.assertNotIn("Alice", bdays)


if __name__ == "__main__":
    unittest.main()
