"""
tests/test_bp_rating.py — tests for favorites / star ratings (#123)

Run from repo root:
    python -m pytest tests/test_bp_rating.py -v
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock


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


# ===========================================================================
# Task 2 — FlickrClient remove_tag + Scanner apple_favorite
# ===========================================================================


class TestFlickrRemoveTag(unittest.TestCase):
    """remove_tag must call flickr.photos.removeTag with the correct tag_id."""

    def test_remove_tag_calls_correct_api(self):
        """remove_tag calls flickr.photos.removeTag with tag_id param."""
        from flickr.flickr_client import FlickrClient

        client = FlickrClient.__new__(FlickrClient)
        client._call = MagicMock(return_value={})
        client.remove_tag("tag-id-abc123")
        client._call.assert_called_once_with(
            "flickr.photos.removeTag",
            {"tag_id": "tag-id-abc123"},
            http_method="POST",
        )


class TestScannerAppleFavorite(unittest.TestCase):
    """photos_record_to_db must include apple_favorite from photo.favorite."""

    def _make_mock_photo(self, favorite: bool) -> MagicMock:
        photo = MagicMock()
        photo.uuid = "scan-uuid-001"
        photo.original_filename = "IMG_001.JPG"
        photo.date = None
        photo.date_added = None
        photo.media_analysis = {}
        photo.exif_info = None
        photo.latitude = None
        photo.place = None
        photo.title = ""
        photo.description = ""
        photo.keywords = []
        photo.labels = []
        photo.persons = []
        photo.score = None
        photo.screenshot = False
        photo.selfie = False
        photo.live_photo = False
        photo.ismovie = False
        photo.fingerprint = ""
        photo.width = 4032
        photo.height = 3024
        photo.favorite = favorite
        return photo

    def test_favorite_true_gives_apple_favorite_1(self):
        """favorite=True → apple_favorite=1 in the row dict."""
        from poller.scanner import photos_record_to_db

        photo = self._make_mock_photo(favorite=True)
        row = photos_record_to_db(photo)
        self.assertEqual(row["apple_favorite"], 1)

    def test_favorite_false_gives_apple_favorite_0(self):
        """favorite=False → apple_favorite=0 in the row dict."""
        from poller.scanner import photos_record_to_db

        photo = self._make_mock_photo(favorite=False)
        row = photos_record_to_db(photo)
        self.assertEqual(row["apple_favorite"], 0)


# ===========================================================================
# Task 3 — Poller: Flickr seed + tag write-back
# ===========================================================================


class TestPollerRatingTag(unittest.TestCase):
    """Tests for bp:rating=N tag parsing and write-back in the poller."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = Database(Path(self.tmp) / "test.db")
        self.photo_id = self.db.upsert_photo(
            {
                "uuid": "poller-uuid",
                "flickr_id": "flickr-001",
                "original_filename": "IMG_P.JPG",
                "privacy_state": "candidate_public",
                "apple_persons": [],
                "proposed_tags": [],
            }
        )

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp)

    # --- _parse_bp_rating_from_tags ---

    def test_parse_bp_rating_from_tag_string(self):
        """_parse_bp_rating_from_tags extracts N from 'bp:rating=N' tag."""
        from poller.poller import _parse_bp_rating_from_tags

        tags = ["landscape", "bp:rating=4", "nature"]
        rating, tag_ids = _parse_bp_rating_from_tags(tags)
        self.assertEqual(rating, 4)
        self.assertEqual(tag_ids, [])  # no id dict supplied

    def test_parse_bp_rating_absent_returns_zero(self):
        """_parse_bp_rating_from_tags returns 0 when no bp:rating tag."""
        from poller.poller import _parse_bp_rating_from_tags

        rating, tag_ids = _parse_bp_rating_from_tags(["landscape", "nature"])
        self.assertEqual(rating, 0)
        self.assertEqual(tag_ids, [])

    def test_parse_bp_rating_with_tag_dicts(self):
        """_parse_bp_rating_from_tags returns tag_ids from getInfo tag dicts."""
        from poller.poller import _parse_bp_rating_from_tags

        tag_items = [
            {"raw": "landscape", "id": "id-001"},
            {"raw": "bp:rating=3", "id": "id-002"},
            {"raw": "nature", "id": "id-003"},
        ]
        rating, tag_ids = _parse_bp_rating_from_tags(tag_items)
        self.assertEqual(rating, 3)
        self.assertEqual(tag_ids, ["id-002"])

    def test_parse_bp_rating_multiple_tags_keeps_all_ids(self):
        """Multiple bp:rating=* tags → return all their IDs (for dedup)."""
        from poller.poller import _parse_bp_rating_from_tags

        tag_items = [
            {"raw": "bp:rating=3", "id": "id-001"},
            {"raw": "bp:rating=5", "id": "id-002"},
        ]
        rating, tag_ids = _parse_bp_rating_from_tags(tag_items)
        # Returns the highest value (for dedup consistency)
        self.assertEqual(rating, 5)
        self.assertIn("id-001", tag_ids)
        self.assertIn("id-002", tag_ids)

    # --- _sync_rating_tag (write-back) ---

    def test_sync_rating_adds_tag_when_missing(self):
        """bp_rating=4, no existing tag → add_tags called with bp:rating=4."""
        from poller.poller import _sync_rating_tag

        client = MagicMock()
        self.db.set_bp_rating(self.photo_id, 4)

        tag_items = [{"raw": "landscape", "id": "id-001"}]
        _sync_rating_tag(client, self.db, "flickr-001", self.photo_id, tag_items)

        client.add_tags.assert_called_once_with("flickr-001", ["bp:rating=4"])
        client.remove_tag.assert_not_called()

    def test_sync_rating_removes_tag_when_zero(self):
        """bp_rating=0, tag exists → remove_tag called; never add bp:rating=0."""
        from poller.poller import _sync_rating_tag

        client = MagicMock()
        # bp_rating is 0 (default)

        tag_items = [{"raw": "bp:rating=3", "id": "tag-id-999"}]
        _sync_rating_tag(client, self.db, "flickr-001", self.photo_id, tag_items)

        client.remove_tag.assert_called_once_with("tag-id-999")
        client.add_tags.assert_not_called()

    def test_sync_rating_no_call_when_already_correct(self):
        """bp_rating=3, bp:rating=3 tag already present → no API call."""
        from poller.poller import _sync_rating_tag

        client = MagicMock()
        self.db.set_bp_rating(self.photo_id, 3)

        tag_items = [{"raw": "bp:rating=3", "id": "tag-id-999"}]
        _sync_rating_tag(client, self.db, "flickr-001", self.photo_id, tag_items)

        client.add_tags.assert_not_called()
        client.remove_tag.assert_not_called()

    def test_sync_rating_replaces_wrong_tag(self):
        """bp_rating=4, bp:rating=2 tag on Flickr → remove old, add new."""
        from poller.poller import _sync_rating_tag

        client = MagicMock()
        self.db.set_bp_rating(self.photo_id, 4)

        tag_items = [{"raw": "bp:rating=2", "id": "old-tag-id"}]
        _sync_rating_tag(client, self.db, "flickr-001", self.photo_id, tag_items)

        client.remove_tag.assert_called_once_with("old-tag-id")
        client.add_tags.assert_called_once_with("flickr-001", ["bp:rating=4"])

    def test_sync_rating_never_adds_zero_tag(self):
        """bp_rating=0, no existing tag → no API call at all."""
        from poller.poller import _sync_rating_tag

        client = MagicMock()
        # bp_rating is 0 (default), no tag items

        _sync_rating_tag(client, self.db, "flickr-001", self.photo_id, [])

        client.add_tags.assert_not_called()
        client.remove_tag.assert_not_called()


# ===========================================================================
# Task 4 — Reconcile: singleton constraint enforcement
# ===========================================================================


class TestReconcileSingleton(unittest.TestCase):
    """check_photo with --fix must deduplicate multiple bp:rating=* tags."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = Database(Path(self.tmp) / "test.db")
        # Seed person_policies table (migration 019 not in schema.sql)
        self.db.conn.execute(
            "CREATE TABLE IF NOT EXISTS person_policies "
            "(id INTEGER PRIMARY KEY, person_name TEXT NOT NULL UNIQUE, "
            "policy TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        self.db.conn.commit()
        self.photo_id = self.db.upsert_photo(
            {
                "uuid": "recon-uuid",
                "flickr_id": "flickr-recon",
                "original_filename": "IMG_R.JPG",
                "privacy_state": "approved_public",
                "apple_persons": [],
                "proposed_tags": [],
                "perms_pushed_flickr": 1,
                "tags_pushed_flickr": 1,
            }
        )
        self.db.set_bp_rating(self.photo_id, 3)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp)

    def _make_info_with_duplicate_rating_tags(self) -> dict:
        """Flickr getInfo response with two conflicting bp:rating=* tags."""
        return {
            "photo": {
                "visibility": {"ispublic": 1, "isfriend": 0, "isfamily": 0},
                "tags": {
                    "tag": [
                        {"raw": "landscape", "id": "tag-land"},
                        {"raw": "bp:rating=3", "id": "tag-rat-3"},
                        {"raw": "bp:rating=5", "id": "tag-rat-5"},
                    ]
                },
            }
        }

    def test_dedup_removes_lower_keeps_higher(self):
        """With two bp:rating=* tags, fix mode removes all but the highest."""
        from poller.reconcile import check_photo

        client = MagicMock()
        client.get_photo_info.return_value = self._make_info_with_duplicate_rating_tags()

        row = dict(
            self.db.conn.execute(
                "SELECT id, flickr_id, privacy_state, pushed_tags, "
                "perms_pushed_flickr, tags_pushed_flickr FROM photos WHERE id = ?",
                (self.photo_id,),
            ).fetchone()
        )

        check_photo(client, row, self.db, fix=True, verbose=False)
        # The lower-valued tag (bp:rating=3) must have been removed
        removed_ids = [call.args[0] for call in client.remove_tag.call_args_list]
        self.assertIn("tag-rat-3", removed_ids)
        self.assertNotIn("tag-rat-5", removed_ids)

    def test_dedup_logs_rating_tag_dedup_to_journal(self):
        """Singleton dedup logs rating_tag_dedup to operation_log."""
        from poller.reconcile import check_photo

        client = MagicMock()
        client.get_photo_info.return_value = self._make_info_with_duplicate_rating_tags()

        row = dict(
            self.db.conn.execute(
                "SELECT id, flickr_id, privacy_state, pushed_tags, "
                "perms_pushed_flickr, tags_pushed_flickr FROM photos WHERE id = ?",
                (self.photo_id,),
            ).fetchone()
        )

        check_photo(client, row, self.db, fix=True, verbose=False)

        logs = self.db.get_operation_log(photo_id=self.photo_id, operation="rating_tag_dedup")
        self.assertGreater(len(logs), 0)


# ===========================================================================
# Task 5 — Explain: rating drift
# ===========================================================================


class TestExplainRatingDrift(unittest.TestCase):
    """run_explain must detect and report bp_rating vs Flickr tag drift."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = Database(Path(self.tmp) / "test.db")
        # Seed person_policies table
        self.db.conn.execute(
            "CREATE TABLE IF NOT EXISTS person_policies "
            "(id INTEGER PRIMARY KEY, person_name TEXT NOT NULL UNIQUE, "
            "policy TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        self.db.conn.commit()

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp)

    def test_rating_drift_reported_in_explain(self):
        """Photo with bp_rating=4 and Flickr tag bp:rating=2 → drift in explain."""
        photo_id = self.db.upsert_photo(
            {
                "uuid": "explain-uuid",
                "flickr_id": "flickr-explain",
                "original_filename": "IMG_E.JPG",
                "privacy_state": "approved_public",
                "apple_persons": [],
                "proposed_tags": [],
                "perms_pushed_flickr": 1,
                "tags_pushed_flickr": 1,
                "flickr_tags": json.dumps(["landscape", "bp:rating=2"]),
            }
        )
        self.db.set_bp_rating(photo_id, 4)

        from poller.explain import run_explain, format_explain_text

        explanations = run_explain(self.db, limit=50, flickr_username="testuser")
        # Call format_explain_text to verify it doesn't raise on rating drift
        format_explain_text(explanations, flickr_username="testuser")

        # At least one entry should mention the rating drift
        rating_entries = [e for e in explanations if e.get("rating")]
        self.assertGreater(len(rating_entries), 0, "Expected rating drift entry")
        drift_entry = rating_entries[0]["rating"]
        self.assertEqual(drift_entry["db_rating"], 4)
        self.assertEqual(drift_entry["flickr_rating"], 2)

    def test_no_drift_when_rating_matches(self):
        """Photo with bp_rating=3 and bp:rating=3 Flickr tag → no drift."""
        photo_id = self.db.upsert_photo(
            {
                "uuid": "explain-match-uuid",
                "flickr_id": "flickr-match",
                "original_filename": "IMG_M.JPG",
                "privacy_state": "approved_public",
                "apple_persons": [],
                "proposed_tags": [],
                "perms_pushed_flickr": 1,
                "tags_pushed_flickr": 1,
                "flickr_tags": json.dumps(["bp:rating=3"]),
            }
        )
        self.db.set_bp_rating(photo_id, 3)

        from poller.explain import run_explain

        explanations = run_explain(self.db, limit=50, flickr_username="testuser")
        rating_drifts = [e for e in explanations if e.get("rating")]
        self.assertEqual(len(rating_drifts), 0)
