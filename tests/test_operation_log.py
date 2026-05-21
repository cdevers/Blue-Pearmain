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

from db.db import Database
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


def _make_db() -> Database:
    """Create a fresh DB with migration 020 applied and placeholder photo rows."""
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    db = Database(Path(f.name))
    run_migration(str(f.name))
    # Seed placeholder photo rows so FK constraints on operation_log are satisfied.
    # (Database._connect sets PRAGMA foreign_keys = ON, so log_operation INSERTs
    # with non-NULL photo_ids silently fail without matching rows in photos.)
    for pid, uid in [
        (1, "uuid-01"),
        (2, "uuid-02"),
        (3, "uuid-03"),
        (5, "uuid-05"),
        (7, "uuid-07"),
        (42, "uuid-42"),
        (99, "uuid-99"),
    ]:
        db.conn.execute(
            "INSERT OR IGNORE INTO photos (id, uuid, privacy_state) VALUES (?, ?, 'needs_review')",
            (pid, uid),
        )
    db.conn.commit()
    return db


class TestLogOperation(unittest.TestCase):
    def test_log_operation_inserts_a_row(self):
        db = _make_db()
        db.log_operation(
            photo_id=None,
            operation="review_decision",
            target="privacy_state",
            old_value="needs_review",
            new_value="approved_public",
            trigger="decision:make_public",
            actor="user",
        )
        rows = db.conn.execute("SELECT * FROM operation_log").fetchall()
        db.close()
        self.assertEqual(len(rows), 1)

    def test_log_operation_stores_all_fields(self):
        db = _make_db()
        db.log_operation(
            photo_id=99,
            operation="reconcile_fix",
            target="flickr_permissions",
            old_value="private",
            new_value="public",
            trigger="reconcile_fix",
            actor="bp",
        )
        row = dict(db.conn.execute("SELECT * FROM operation_log").fetchone())
        db.close()
        self.assertEqual(row["operation"], "reconcile_fix")
        self.assertEqual(row["target"], "flickr_permissions")
        self.assertEqual(row["old_value"], "private")
        self.assertEqual(row["new_value"], "public")
        self.assertEqual(row["actor"], "bp")

    def test_log_operation_never_raises_on_missing_table(self):
        """Even if operation_log doesn't exist, log_operation is a no-op."""
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        f.close()
        db = Database(Path(f.name))
        # Do NOT run the migration — table does not exist
        try:
            db.log_operation(
                photo_id=None,
                operation="test",
            )
        except Exception as e:
            self.fail(f"log_operation raised unexpectedly: {e}")
        finally:
            db.close()
            os.unlink(f.name)

    def test_log_operation_actor_defaults_to_bp(self):
        db = _make_db()
        db.log_operation(photo_id=None, operation="test_op")
        row = dict(db.conn.execute("SELECT * FROM operation_log").fetchone())
        db.close()
        self.assertEqual(row["actor"], "bp")


class TestGetOperationLog(unittest.TestCase):
    def test_returns_empty_list_for_empty_table(self):
        db = _make_db()
        result = db.get_operation_log()
        db.close()
        self.assertEqual(result, [])

    def test_returns_inserted_entry(self):
        db = _make_db()
        db.log_operation(photo_id=1, operation="review_decision", actor="user")
        result = db.get_operation_log()
        db.close()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["operation"], "review_decision")

    def test_filters_by_photo_id(self):
        db = _make_db()
        db.log_operation(photo_id=1, operation="review_decision")
        db.log_operation(photo_id=2, operation="reconcile_fix")
        result = db.get_operation_log(photo_id=1)
        db.close()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["operation"], "review_decision")

    def test_filters_by_operation(self):
        db = _make_db()
        db.log_operation(photo_id=1, operation="review_decision")
        db.log_operation(photo_id=2, operation="reconcile_fix")
        result = db.get_operation_log(operation="reconcile_fix")
        db.close()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["operation"], "reconcile_fix")

    def test_respects_limit(self):
        db = _make_db()
        for i in range(5):
            db.log_operation(photo_id=None, operation="test_op")
        result = db.get_operation_log(limit=3)
        db.close()
        self.assertEqual(len(result), 3)

    def test_returns_empty_list_when_table_missing(self):
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        f.close()
        db = Database(Path(f.name))
        result = db.get_operation_log()
        db.close()
        os.unlink(f.name)
        self.assertEqual(result, [])


class TestReviewDecisionLogging(unittest.TestCase):
    """Verify the log_operation DB contract for review decision logging."""

    def test_log_operation_stores_review_decision_entry(self):
        """Verify the DB fields written by a review decision log entry."""
        db = _make_db()
        db.log_operation(
            photo_id=42,
            operation="review_decision",
            target="privacy_state",
            old_value="needs_review",
            new_value="approved_public",
            trigger="decision:make_public",
            actor="user",
        )
        entries = db.get_operation_log(operation="review_decision")
        db.close()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["actor"], "user")
        self.assertEqual(entries[0]["trigger"], "decision:make_public")
        self.assertEqual(entries[0]["old_value"], "needs_review")
        self.assertEqual(entries[0]["new_value"], "approved_public")


class TestProposalApplyLogging(unittest.TestCase):
    """Verify the log_operation DB contract for proposal auto-apply logging."""

    def test_log_operation_stores_auto_apply_entry(self):
        db = _make_db()
        db.log_operation(
            photo_id=7,
            operation="auto_apply_proposal",
            target="tags→flickr",
            old_value=None,
            new_value='["beach", "scanned-film"]',
            trigger="proposal_id=42",
            actor="bp",
        )
        entries = db.get_operation_log(operation="auto_apply_proposal")
        db.close()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["photo_id"], 7)
        self.assertIn("proposal_id=42", entries[0]["trigger"])
        self.assertEqual(entries[0]["target"], "tags→flickr")
