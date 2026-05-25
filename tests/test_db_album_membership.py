"""
tests/test_db_album_membership.py — unit tests for album membership DB methods (#135)

Run from repo root:
    python -m pytest tests/test_db_album_membership.py -v
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from db.db import Database


def _make_db() -> tuple[Database, str]:
    tmp = tempfile.mkdtemp()
    db = Database(Path(tmp) / "test.db")
    return db, tmp


def _seed(db: Database) -> tuple[int, int, int, int]:
    """Return (photo1_id, photo2_id, album1_id, album2_id)."""
    p1 = db.upsert_photo(
        {
            "uuid": "u1",
            "original_filename": "A.JPG",
            "privacy_state": "needs_review",
            "apple_persons": [],
            "apple_labels": [],
            "apple_unknown_faces": 0,
            "apple_named_faces": 0,
        }
    )
    p2 = db.upsert_photo(
        {
            "uuid": "u2",
            "original_filename": "B.JPG",
            "privacy_state": "needs_review",
            "apple_persons": [],
            "apple_labels": [],
            "apple_unknown_faces": 0,
            "apple_named_faces": 0,
        }
    )
    a1 = db.upsert_album("album-uuid-1", "Summer 2024")
    a2 = db.upsert_album("album-uuid-2", "Trips")
    return p1, p2, a1, a2


class TestGetAllAlbumsWithCounts(unittest.TestCase):
    def setUp(self):
        self.db, self.tmp = _make_db()
        self.p1, self.p2, self.a1, self.a2 = _seed(self.db)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp)

    def test_returns_all_albums(self):
        albums = self.db.get_all_albums_with_counts()
        names = {a["name"] for a in albums}
        self.assertIn("Summer 2024", names)
        self.assertIn("Trips", names)

    def test_photo_count_zero_when_no_members(self):
        albums = self.db.get_all_albums_with_counts()
        for a in albums:
            self.assertEqual(a["photo_count"], 0)

    def test_photo_count_reflects_active_members(self):
        self.db.upsert_photo_album(self.p1, self.a1)
        self.db.upsert_photo_album(self.p2, self.a1)
        albums = self.db.get_all_albums_with_counts()
        summer = next(a for a in albums if a["name"] == "Summer 2024")
        self.assertEqual(summer["photo_count"], 2)

    def test_tombstoned_members_not_counted(self):
        self.db.upsert_photo_album(self.p1, self.a1)
        self.db.mark_photo_album_removed(self.p1, self.a1)
        albums = self.db.get_all_albums_with_counts()
        summer = next(a for a in albums if a["name"] == "Summer 2024")
        self.assertEqual(summer["photo_count"], 0)

    def test_deleted_albums_excluded(self):
        self.db.mark_album_deleted(self.a2)
        albums = self.db.get_all_albums_with_counts()
        names = {a["name"] for a in albums}
        self.assertNotIn("Trips", names)


class TestGetAlbumMembershipForPhotos(unittest.TestCase):
    def setUp(self):
        self.db, self.tmp = _make_db()
        self.p1, self.p2, self.a1, self.a2 = _seed(self.db)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp)

    def test_empty_photo_ids_returns_empty_dict(self):
        result = self.db.get_album_membership_for_photos([])
        self.assertEqual(result, {})

    def test_returns_active_memberships(self):
        self.db.upsert_photo_album(self.p1, self.a1)
        self.db.upsert_photo_album(self.p2, self.a1)
        result = self.db.get_album_membership_for_photos([self.p1, self.p2])
        self.assertIn(self.a1, result)
        self.assertIn(self.p1, result[self.a1])
        self.assertIn(self.p2, result[self.a1])

    def test_tombstoned_memberships_excluded(self):
        self.db.upsert_photo_album(self.p1, self.a1)
        self.db.mark_photo_album_removed(self.p1, self.a1)
        result = self.db.get_album_membership_for_photos([self.p1])
        self.assertNotIn(self.a1, result)

    def test_only_queried_photos_returned(self):
        self.db.upsert_photo_album(self.p1, self.a1)
        self.db.upsert_photo_album(self.p2, self.a1)
        result = self.db.get_album_membership_for_photos([self.p1])
        if self.a1 in result:
            self.assertNotIn(self.p2, result[self.a1])


class TestBulkUpsertPhotoAlbums(unittest.TestCase):
    def setUp(self):
        self.db, self.tmp = _make_db()
        self.p1, self.p2, self.a1, self.a2 = _seed(self.db)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp)

    def test_adds_new_memberships(self):
        n = self.db.bulk_upsert_photo_albums([self.p1, self.p2], self.a1)
        self.db.conn.commit()
        self.assertEqual(n, 2)
        row = self.db.conn.execute(
            "SELECT removed_at FROM photo_albums WHERE photo_id=? AND album_id=?",
            (self.p1, self.a1),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertIsNone(row["removed_at"])

    def test_idempotent_already_active(self):
        self.db.upsert_photo_album(self.p1, self.a1)
        n = self.db.bulk_upsert_photo_albums([self.p1], self.a1)
        self.db.conn.commit()
        self.assertEqual(n, 0)  # already active — not counted
        count = self.db.conn.execute(
            "SELECT COUNT(*) FROM photo_albums WHERE photo_id=? AND album_id=?",
            (self.p1, self.a1),
        ).fetchone()[0]
        self.assertEqual(count, 1)  # still exactly one row

    def test_reactivates_tombstoned_row(self):
        self.db.upsert_photo_album(self.p1, self.a1)
        self.db.mark_photo_album_removed(self.p1, self.a1)
        n = self.db.bulk_upsert_photo_albums([self.p1], self.a1)
        self.db.conn.commit()
        self.assertEqual(n, 1)  # re-activation counted
        row = self.db.conn.execute(
            "SELECT removed_at FROM photo_albums WHERE photo_id=? AND album_id=?",
            (self.p1, self.a1),
        ).fetchone()
        self.assertIsNone(row["removed_at"])

    def test_empty_photo_ids_returns_zero(self):
        n = self.db.bulk_upsert_photo_albums([], self.a1)
        self.db.conn.commit()
        self.assertEqual(n, 0)


class TestBulkRemovePhotoAlbums(unittest.TestCase):
    def setUp(self):
        self.db, self.tmp = _make_db()
        self.p1, self.p2, self.a1, self.a2 = _seed(self.db)
        self.db.upsert_photo_album(self.p1, self.a1)
        self.db.upsert_photo_album(self.p2, self.a1)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp)

    def test_tombstones_active_members(self):
        n = self.db.bulk_remove_photo_albums([self.p1, self.p2], self.a1)
        self.db.conn.commit()
        self.assertEqual(n, 2)
        row = self.db.conn.execute(
            "SELECT removed_at FROM photo_albums WHERE photo_id=? AND album_id=?",
            (self.p1, self.a1),
        ).fetchone()
        self.assertIsNotNone(row["removed_at"])

    def test_idempotent_already_tombstoned(self):
        self.db.mark_photo_album_removed(self.p1, self.a1)
        n = self.db.bulk_remove_photo_albums([self.p1], self.a1)
        self.db.conn.commit()
        self.assertEqual(n, 0)  # already tombstoned — not double-counted

    def test_empty_photo_ids_returns_zero(self):
        n = self.db.bulk_remove_photo_albums([], self.a1)
        self.db.conn.commit()
        self.assertEqual(n, 0)


class TestAddRemoveCycleInvariant(unittest.TestCase):
    """After repeated add/remove/add cycles, exactly one row must exist and be active."""

    def setUp(self):
        self.db, self.tmp = _make_db()
        self.p1, _, self.a1, _ = _seed(self.db)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp)

    def test_single_row_after_repeated_cycles(self):
        for _ in range(3):
            self.db.bulk_upsert_photo_albums([self.p1], self.a1)
            self.db.conn.commit()
            self.db.bulk_remove_photo_albums([self.p1], self.a1)
            self.db.conn.commit()
        self.db.bulk_upsert_photo_albums([self.p1], self.a1)
        self.db.conn.commit()

        rows = self.db.conn.execute(
            "SELECT removed_at FROM photo_albums WHERE photo_id=? AND album_id=?",
            (self.p1, self.a1),
        ).fetchall()
        self.assertEqual(len(rows), 1, "Must be exactly one row")
        self.assertIsNone(rows[0]["removed_at"], "Row must be active")
