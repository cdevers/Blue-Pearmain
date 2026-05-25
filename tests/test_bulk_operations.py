"""
tests/test_bulk_operations.py — tests for bulk operations (#133)

Run from repo root:
    python -m pytest tests/test_bulk_operations.py -v
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.db import Database  # noqa: E402


# ===========================================================================
# Task 1 — Migration 023
# ===========================================================================


def _import_migration_023():
    spec = importlib.util.spec_from_file_location(
        "migrate_023_bulk_batches",
        Path(__file__).parent.parent / "db" / "migrations" / "migrate_023_bulk_batches.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestMigration023(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "test.db")
        conn = sqlite3.connect(self.db_path)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS schema_migrations
                (id INTEGER PRIMARY KEY, name TEXT UNIQUE, applied_at TEXT);
            CREATE TABLE IF NOT EXISTS photos (id INTEGER PRIMARY KEY, uuid TEXT);
            CREATE TABLE IF NOT EXISTS metadata_proposals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                photo_id INTEGER NOT NULL REFERENCES photos(id),
                field TEXT NOT NULL,
                proposed_value TEXT,
                source TEXT NOT NULL,
                target TEXT NOT NULL,
                conflict_type TEXT NOT NULL,
                source_hash_at_creation TEXT,
                target_hash_at_creation TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                resolved_at TEXT,
                resolution_note TEXT
            );
        """)
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_creates_bulk_batches_table(self):
        mod = _import_migration_023()
        mod.run(self.db_path)
        conn = sqlite3.connect(self.db_path)
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        conn.close()
        self.assertIn("bulk_batches", tables)

    def test_adds_batch_id_to_proposals(self):
        mod = _import_migration_023()
        mod.run(self.db_path)
        conn = sqlite3.connect(self.db_path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(metadata_proposals)").fetchall()}
        conn.close()
        self.assertIn("batch_id", cols)

    def test_batch_id_is_nullable(self):
        """Existing proposals survive migration with batch_id=NULL."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO photos (uuid) VALUES ('u1')")
        conn.execute("""INSERT INTO metadata_proposals
            (photo_id, field, source, target, conflict_type, status, created_at)
            VALUES (1, 'title', 'flickr', 'photos', 'non_conflict', 'pending', '2026-01-01')""")
        conn.commit()
        conn.close()
        mod = _import_migration_023()
        mod.run(self.db_path)
        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT batch_id FROM metadata_proposals WHERE id=1").fetchone()
        conn.close()
        self.assertIsNone(row[0])

    def test_migration_idempotent(self):
        mod = _import_migration_023()
        mod.run(self.db_path)
        mod.run(self.db_path)  # must not raise

    def test_bulk_batches_columns(self):
        mod = _import_migration_023()
        mod.run(self.db_path)
        conn = sqlite3.connect(self.db_path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(bulk_batches)").fetchall()}
        conn.close()
        self.assertGreaterEqual(
            cols,
            {"id", "operation", "field", "value", "tags", "filter", "photo_count", "created_at"},
        )


# ===========================================================================
# Task 2 — library_photos query methods
# ===========================================================================


class TestLibraryPhotos(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = Database(Path(self.tmp) / "test.db")
        # Seed photos with varied attributes
        self.p1 = self.db.upsert_photo(
            {
                "uuid": "u1",
                "original_filename": "A.JPG",
                "privacy_state": "already_public",
                "flickr_id": "f1",
                "date_taken": "2024-05-10 12:00:00",
                "flickr_title": "Paris Trip",
                "flickr_tags": json.dumps(["paris", "france"]),
                "photos_tags": json.dumps([]),
                "apple_persons": [],
                "proposed_tags": [],
            }
        )
        self.p2 = self.db.upsert_photo(
            {
                "uuid": "u2",
                "original_filename": "B.JPG",
                "privacy_state": "needs_review",
                "flickr_id": "f2",
                "date_taken": "2024-06-15 08:00:00",
                "flickr_title": "",
                "flickr_tags": json.dumps(["london"]),
                "photos_tags": json.dumps([]),
                "apple_persons": [],
                "proposed_tags": [],
            }
        )
        self.p3 = self.db.upsert_photo(
            {
                "uuid": "u3",
                "original_filename": "C.JPG",
                "privacy_state": "auto_private",
                "flickr_id": "f3",
                "date_taken": "2024-07-20 10:00:00",
                "flickr_title": None,
                "flickr_tags": json.dumps([]),
                "photos_tags": json.dumps([]),
                "apple_persons": [],
                "proposed_tags": [],
            }
        )

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp)

    def test_library_photos_returns_all(self):
        rows = self.db.library_photos()
        self.assertEqual(len(rows), 3)

    def test_library_photos_date_from_filter(self):
        rows = self.db.library_photos(date_from="2024-06-01")
        ids = {r["id"] for r in rows}
        self.assertIn(self.p2, ids)
        self.assertIn(self.p3, ids)
        self.assertNotIn(self.p1, ids)

    def test_library_photos_date_to_filter(self):
        rows = self.db.library_photos(date_to="2024-06-01")
        ids = {r["id"] for r in rows}
        self.assertIn(self.p1, ids)
        self.assertNotIn(self.p2, ids)

    def test_library_photos_status_public(self):
        rows = self.db.library_photos(status="public")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], self.p1)

    def test_library_photos_status_private(self):
        rows = self.db.library_photos(status="private")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], self.p3)

    def test_library_photos_status_pending(self):
        rows = self.db.library_photos(status="pending")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], self.p2)

    def test_library_photos_untitled_only(self):
        rows = self.db.library_photos(untitled_only=True)
        ids = {r["id"] for r in rows}
        self.assertIn(self.p2, ids)
        self.assertIn(self.p3, ids)
        self.assertNotIn(self.p1, ids)

    def test_library_photos_tag_filter(self):
        rows = self.db.library_photos(tag="paris")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], self.p1)

    def test_library_photo_count(self):
        self.assertEqual(self.db.library_photo_count(), 3)

    def test_library_photo_count_with_filter(self):
        self.assertEqual(self.db.library_photo_count(status="public"), 1)

    def test_library_photo_ids(self):
        ids = self.db.library_photo_ids(status="public")
        self.assertEqual(ids, [self.p1])

    def test_library_photo_ids_all(self):
        ids = self.db.library_photo_ids()
        self.assertEqual(len(ids), 3)

    def test_library_photos_pagination(self):
        rows = self.db.library_photos(limit=2, offset=0)
        self.assertEqual(len(rows), 2)
        rows2 = self.db.library_photos(limit=2, offset=2)
        self.assertEqual(len(rows2), 1)

    def test_get_all_albums_empty(self):
        albums = self.db.get_all_albums()
        self.assertEqual(albums, [])

    def test_get_all_albums_returns_non_deleted(self):
        from datetime import datetime, timezone

        self.db.upsert_album("album-uuid-1", "My Album")
        self.db.upsert_album("album-uuid-2", "Deleted Album")
        # Mark second album deleted
        self.db.conn.execute(
            "UPDATE albums SET deleted_at=? WHERE apple_uuid=?",
            (datetime.now(timezone.utc).isoformat(), "album-uuid-2"),
        )
        self.db.conn.commit()
        albums = self.db.get_all_albums()
        names = [a["name"] for a in albums]
        self.assertIn("My Album", names)
        self.assertNotIn("Deleted Album", names)

    def test_library_album_filter_excludes_tombstoned_membership(self):
        """Photos removed from an album (removed_at set) should not appear in album filter."""
        from datetime import datetime, timezone

        album_id = self.db.upsert_album("album-tomb-1", "Tombstone Album")
        # Add p1 to the album, then tombstone it
        self.db.upsert_photo_album(self.p1, album_id)
        self.db.conn.execute(
            "UPDATE photo_albums SET removed_at=? WHERE photo_id=? AND album_id=?",
            (datetime.now(timezone.utc).isoformat(), self.p1, album_id),
        )
        self.db.conn.commit()
        rows = self.db.library_photos(album_id=album_id)
        ids = {r["id"] for r in rows}
        self.assertNotIn(self.p1, ids)
