"""
tests/test_bp_rating.py — tests for favorites / star ratings (#123)

Run from repo root:
    python -m pytest tests/test_bp_rating.py -v
"""

from __future__ import annotations

import importlib
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent))

from db.db import Database


# ===========================================================================
# Task 1 — Migration 022 + DB foundation
# ===========================================================================


class TestMigration022(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "test.db")
        # Minimal DB: just the two tables the migration needs
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(id INTEGER PRIMARY KEY, name TEXT UNIQUE, applied_at TEXT)"
        )
        conn.execute("CREATE TABLE IF NOT EXISTS photos (id INTEGER PRIMARY KEY, uuid TEXT)")
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _import_migration(self):
        spec = importlib.util.spec_from_file_location(
            "migrate_022_bp_rating",
            Path(__file__).parent.parent / "db" / "migrations" / "migrate_022_bp_rating.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_migration_adds_bp_rating_column(self):
        """After migration, photos table has bp_rating column."""
        mod = self._import_migration()
        mod.run(self.db_path)
        conn = sqlite3.connect(self.db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(photos)").fetchall()}
        conn.close()
        self.assertIn("bp_rating", cols)

    def test_migration_idempotent(self):
        """Running migration twice does not raise."""
        mod = self._import_migration()
        mod.run(self.db_path)
        mod.run(self.db_path)  # Must not raise

    def test_migration_default_zero(self):
        """Existing rows get bp_rating=0 after migration (SQLite DEFAULT)."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO photos (uuid) VALUES ('existing-uuid')")
        conn.commit()
        conn.close()
        mod = self._import_migration()
        mod.run(self.db_path)
        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT bp_rating FROM photos WHERE uuid = 'existing-uuid'").fetchone()
        conn.close()
        self.assertEqual(row[0], 0)


class TestDBFoundation(unittest.TestCase):
    """Tests for the new db.py functions and review_queue change."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = Database(Path(self.tmp) / "test.db")
        # Seed one test photo
        self.photo_id = self.db.upsert_photo(
            {
                "uuid": "test-uuid-001",
                "original_filename": "IMG_001.JPG",
                "privacy_state": "candidate_public",
                "apple_persons": [],
                "proposed_tags": [],
            }
        )

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp)

    # --- set_bp_rating ---

    def test_set_bp_rating_updates_db(self):
        """set_bp_rating stores the value directly."""
        self.db.set_bp_rating(self.photo_id, 4)
        row = self.db.conn.execute(
            "SELECT bp_rating FROM photos WHERE id = ?", (self.photo_id,)
        ).fetchone()
        self.assertEqual(row["bp_rating"], 4)

    def test_set_bp_rating_logs_operation(self):
        """set_bp_rating writes a set_rating entry to operation_log."""
        self.db.set_bp_rating(self.photo_id, 3)
        logs = self.db.get_operation_log(photo_id=self.photo_id, operation="set_rating")
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["new_value"], "3")

    # --- get_photo_uuid ---

    def test_get_photo_uuid_returns_uuid(self):
        """get_photo_uuid returns the Apple Photos UUID for a valid photo_id."""
        uuid = self.db.get_photo_uuid(self.photo_id)
        self.assertEqual(uuid, "test-uuid-001")

    def test_get_photo_uuid_returns_none_for_missing(self):
        """get_photo_uuid returns None for a photo_id that doesn't exist."""
        self.assertIsNone(self.db.get_photo_uuid(99999))

    # --- review_queue includes bp_rating ---

    def test_review_queue_includes_bp_rating(self):
        """review_queue rows include bp_rating field."""
        self.db.set_bp_rating(self.photo_id, 2)
        photos = self.db.review_queue(states=["candidate_public"])
        self.assertTrue(len(photos) >= 1)
        found = next((p for p in photos if p["id"] == self.photo_id), None)
        self.assertIsNotNone(found)
        self.assertEqual(found["bp_rating"], 2)

    # --- apply_scanner_rating (sync table) ---

    def test_apply_scanner_heart_true_and_zero_seeds(self):
        """Favorite=True + bp_rating=0 → bp_rating becomes 1."""
        # bp_rating starts at 0 (default)
        self.db.apply_scanner_rating(self.photo_id, apple_favorite=1)
        row = self.db.conn.execute(
            "SELECT bp_rating FROM photos WHERE id = ?", (self.photo_id,)
        ).fetchone()
        self.assertEqual(row["bp_rating"], 1)

    def test_apply_scanner_heart_true_and_rated_unchanged(self):
        """Favorite=True + bp_rating=3 → bp_rating stays 3 (no downgrade)."""
        self.db.set_bp_rating(self.photo_id, 3)
        self.db.apply_scanner_rating(self.photo_id, apple_favorite=1)
        row = self.db.conn.execute(
            "SELECT bp_rating FROM photos WHERE id = ?", (self.photo_id,)
        ).fetchone()
        self.assertEqual(row["bp_rating"], 3)

    def test_apply_scanner_heart_false_and_zero_unchanged(self):
        """Favorite=False + bp_rating=0 → bp_rating stays 0."""
        self.db.apply_scanner_rating(self.photo_id, apple_favorite=0)
        row = self.db.conn.execute(
            "SELECT bp_rating FROM photos WHERE id = ?", (self.photo_id,)
        ).fetchone()
        self.assertEqual(row["bp_rating"], 0)

    def test_apply_scanner_heart_false_and_rated_clears(self):
        """Favorite=False + bp_rating=3 → bp_rating becomes 0 (un-heart clears)."""
        self.db.set_bp_rating(self.photo_id, 3)
        self.db.apply_scanner_rating(self.photo_id, apple_favorite=0)
        row = self.db.conn.execute(
            "SELECT bp_rating FROM photos WHERE id = ?", (self.photo_id,)
        ).fetchone()
        self.assertEqual(row["bp_rating"], 0)

    def test_apply_scanner_seed_logs_to_journal(self):
        """apply_scanner_rating logs seed_rating_from_photos when it sets rating to 1."""
        self.db.apply_scanner_rating(self.photo_id, apple_favorite=1)
        logs = self.db.get_operation_log(
            photo_id=self.photo_id, operation="seed_rating_from_photos"
        )
        self.assertEqual(len(logs), 1)

    def test_apply_scanner_clear_logs_to_journal(self):
        """apply_scanner_rating logs clear_rating_from_photos when it clears rating."""
        self.db.set_bp_rating(self.photo_id, 2)
        self.db.apply_scanner_rating(self.photo_id, apple_favorite=0)
        logs = self.db.get_operation_log(
            photo_id=self.photo_id, operation="clear_rating_from_photos"
        )
        self.assertEqual(len(logs), 1)

    # --- seed_flickr_rating ---

    def test_seed_flickr_rating_seeds_when_unrated(self):
        """seed_flickr_rating sets bp_rating when db is 0."""
        self.db.seed_flickr_rating(self.photo_id, flickr_rating=3)
        row = self.db.conn.execute(
            "SELECT bp_rating FROM photos WHERE id = ?", (self.photo_id,)
        ).fetchone()
        self.assertEqual(row["bp_rating"], 3)

    def test_seed_flickr_rating_ignored_when_already_rated(self):
        """seed_flickr_rating does not overwrite an existing non-zero bp_rating."""
        self.db.set_bp_rating(self.photo_id, 2)
        self.db.seed_flickr_rating(self.photo_id, flickr_rating=5)
        row = self.db.conn.execute(
            "SELECT bp_rating FROM photos WHERE id = ?", (self.photo_id,)
        ).fetchone()
        self.assertEqual(row["bp_rating"], 2)

    def test_seed_flickr_rating_logs_to_journal(self):
        """seed_flickr_rating logs seed_rating_from_flickr when it seeds."""
        self.db.seed_flickr_rating(self.photo_id, flickr_rating=4)
        logs = self.db.get_operation_log(
            photo_id=self.photo_id, operation="seed_rating_from_flickr"
        )
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["new_value"], "4")
