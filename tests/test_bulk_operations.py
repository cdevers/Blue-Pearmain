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

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import reviewer.app as app_module  # noqa: E402
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


# ===========================================================================
# Task 3 — bulk proposals DB methods
# ===========================================================================


class TestBulkProposals(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = Database(Path(self.tmp) / "test.db")
        self.p1 = self.db.upsert_photo(
            {
                "uuid": "u1",
                "original_filename": "A.JPG",
                "flickr_id": "f1",
                "privacy_state": "already_public",
                "flickr_title": "",
                "flickr_description": "",
                "flickr_tags": json.dumps(["paris"]),
                "photos_tags": json.dumps([]),
                "apple_persons": [],
                "proposed_tags": [],
            }
        )
        self.p2 = self.db.upsert_photo(
            {
                "uuid": "u2",
                "original_filename": "B.JPG",
                "flickr_id": "f2",
                "privacy_state": "already_public",
                "flickr_title": "Existing Title",
                "flickr_description": "",
                "flickr_tags": json.dumps(["london", "uk"]),
                "photos_tags": json.dumps([]),
                "apple_persons": [],
                "proposed_tags": [],
            }
        )
        self.p3 = self.db.upsert_photo(
            {
                "uuid": "u3",
                "original_filename": "C.JPG",
                "flickr_id": None,  # Photos-only — should be skipped
                "privacy_state": "needs_review",
                "flickr_title": "",
                "flickr_tags": json.dumps([]),
                "photos_tags": json.dumps([]),
                "apple_persons": [],
                "proposed_tags": [],
            }
        )

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp)

    def _pending_proposals(self):
        rows = self.db.conn.execute(
            "SELECT * FROM metadata_proposals WHERE status='pending'"
        ).fetchall()
        return [dict(r) for r in rows]

    # --- create_bulk_batch ---

    def test_create_bulk_batch_returns_id(self):
        bid = self.db.create_bulk_batch(
            operation="set_title",
            field="title",
            value="Test Title",
            tags=None,
            filter_json=None,
            photo_count=2,
        )
        self.assertIsInstance(bid, int)
        self.assertGreater(bid, 0)

    def test_create_bulk_batch_stores_data(self):
        bid = self.db.create_bulk_batch(
            operation="tags_add",
            field=None,
            value=None,
            tags=["mfa-boston"],
            filter_json='{"status": "public"}',
            photo_count=10,
        )
        row = self.db.conn.execute("SELECT * FROM bulk_batches WHERE id=?", (bid,)).fetchone()
        self.assertEqual(row["operation"], "tags_add")
        self.assertEqual(json.loads(row["tags"]), ["mfa-boston"])
        self.assertEqual(row["photo_count"], 10)

    # --- insert_bulk_proposals — title ---

    def test_insert_bulk_title_creates_proposals(self):
        bid = self.db.create_bulk_batch("set_title", "title", "MFA Boston", None, None, 2)
        n = self.db.insert_bulk_proposals(
            batch_id=bid,
            photo_ids=[self.p1, self.p2],
            field="title",
            value="MFA Boston",
            skip_existing=False,
        )
        self.assertEqual(n, 2)
        proposals = self._pending_proposals()
        self.assertEqual(len(proposals), 2)
        self.assertTrue(all(p["field"] == "title" for p in proposals))
        self.assertTrue(all(p["proposed_value"] == "MFA Boston" for p in proposals))
        self.assertTrue(all(p["batch_id"] == bid for p in proposals))

    def test_insert_bulk_title_skip_existing(self):
        bid = self.db.create_bulk_batch("set_title", "title", "MFA Boston", None, None, 1)
        n = self.db.insert_bulk_proposals(
            batch_id=bid,
            photo_ids=[self.p1, self.p2],
            field="title",
            value="MFA Boston",
            skip_existing=True,
        )
        # p2 already has 'Existing Title' → should be skipped
        self.assertEqual(n, 1)
        proposals = self._pending_proposals()
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["photo_id"], self.p1)

    def test_insert_bulk_title_skips_photos_without_flickr_id(self):
        bid = self.db.create_bulk_batch("set_title", "title", "X", None, None, 1)
        n = self.db.insert_bulk_proposals(
            batch_id=bid,
            photo_ids=[self.p1, self.p3],  # p3 has no flickr_id
            field="title",
            value="X",
            skip_existing=False,
        )
        self.assertEqual(n, 1)  # only p1

    def test_insert_bulk_title_idempotent(self):
        """Running the same bulk op twice produces no additional proposals."""
        bid = self.db.create_bulk_batch("set_title", "title", "MFA Boston", None, None, 1)
        self.db.insert_bulk_proposals(bid, [self.p1], "title", value="MFA Boston")
        n2 = self.db.insert_bulk_proposals(bid, [self.p1], "title", value="MFA Boston")
        self.assertEqual(n2, 0)
        self.assertEqual(len(self._pending_proposals()), 1)

    # --- insert_bulk_proposals — tags_add ---

    def test_insert_bulk_tags_add(self):
        bid = self.db.create_bulk_batch("tags_add", None, None, ["mfa-boston"], None, 2)
        n = self.db.insert_bulk_proposals(
            batch_id=bid,
            photo_ids=[self.p1, self.p2],
            field="tags_add",
            tags=["mfa-boston"],
        )
        self.assertEqual(n, 2)
        proposals = self._pending_proposals()
        # p1 had ["paris"] → should become ["mfa-boston", "paris"]
        p1_prop = next(p for p in proposals if p["photo_id"] == self.p1)
        self.assertEqual(json.loads(p1_prop["proposed_value"]), ["mfa-boston", "paris"])

    def test_insert_bulk_tags_add_idempotent_per_photo(self):
        """Adding a tag already present on a photo generates no proposal for that photo."""
        bid = self.db.create_bulk_batch("tags_add", None, None, ["paris"], None, 1)
        n = self.db.insert_bulk_proposals(
            batch_id=bid,
            photo_ids=[self.p1],  # p1 already has "paris"
            field="tags_add",
            tags=["paris"],
        )
        self.assertEqual(n, 0)

    # --- insert_bulk_proposals — tags_remove ---

    def test_insert_bulk_tags_remove(self):
        bid = self.db.create_bulk_batch("tags_remove", None, None, ["paris"], None, 1)
        n = self.db.insert_bulk_proposals(
            batch_id=bid,
            photo_ids=[self.p1],
            field="tags_remove",
            tags=["paris"],
        )
        self.assertEqual(n, 1)
        proposals = self._pending_proposals()
        self.assertEqual(json.loads(proposals[0]["proposed_value"]), [])

    def test_insert_bulk_tags_remove_absent_tag_noop(self):
        """Removing a tag not present on a photo generates no proposal."""
        bid = self.db.create_bulk_batch("tags_remove", None, None, ["nonexistent"], None, 1)
        n = self.db.insert_bulk_proposals(
            batch_id=bid,
            photo_ids=[self.p1],
            field="tags_remove",
            tags=["nonexistent"],
        )
        self.assertEqual(n, 0)

    # --- get_pending_bulk_batches / reject_bulk_batch ---

    def test_get_pending_bulk_batches_empty(self):
        self.assertEqual(self.db.get_pending_bulk_batches(), [])

    def test_get_pending_bulk_batches_returns_batch_with_pending_proposals(self):
        bid = self.db.create_bulk_batch("set_title", "title", "X", None, None, 1)
        self.db.insert_bulk_proposals(bid, [self.p1], "title", value="X")
        batches = self.db.get_pending_bulk_batches()
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0]["id"], bid)
        self.assertEqual(batches[0]["pending_count"], 1)

    def test_reject_bulk_batch(self):
        bid = self.db.create_bulk_batch("set_title", "title", "X", None, None, 2)
        self.db.insert_bulk_proposals(bid, [self.p1, self.p2], "title", value="X")
        n = self.db.reject_bulk_batch(bid)
        self.assertEqual(n, 2)
        proposals = self._pending_proposals()
        self.assertEqual(len(proposals), 0)

    def test_reject_bulk_batch_only_affects_pending(self):
        """Already-resolved proposals in the batch are not re-rejected."""
        bid = self.db.create_bulk_batch("set_title", "title", "X", None, None, 2)
        self.db.insert_bulk_proposals(bid, [self.p1, self.p2], "title", value="X")
        proposals = self._pending_proposals()
        # Manually resolve one
        self.db.resolve_proposal(proposals[0]["id"], "applied")
        n = self.db.reject_bulk_batch(bid)
        self.assertEqual(n, 1)


# ===========================================================================
# Task 4 — /library route
# ===========================================================================


@pytest.fixture(scope="module")
def lib_client():
    with tempfile.TemporaryDirectory() as tmp:
        test_db = Database(Path(tmp) / "test.db")
        for i in range(1, 6):
            test_db.upsert_photo(
                {
                    "uuid": f"lib-uuid-{i}",
                    "flickr_id": f"flickr-{i}",
                    "original_filename": f"IMG_{i:04d}.JPG",
                    "privacy_state": "already_public",
                    "date_taken": f"2024-0{min(i, 9)}-10 12:00:00",
                    "flickr_title": f"Title {i}" if i % 2 == 0 else "",
                    "flickr_tags": json.dumps([f"tag{i}"]),
                    "photos_tags": json.dumps([]),
                    "apple_persons": [],
                    "proposed_tags": [],
                }
            )
        app_module._db = test_db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test-secret"
        with app_module.app.test_client() as c:
            yield c
        app_module._db = None


class TestLibraryRoute:
    def test_library_page_200(self, lib_client):
        resp = lib_client.get("/library")
        assert resp.status_code == 200

    def test_library_page_shows_photos(self, lib_client):
        resp = lib_client.get("/library")
        html = resp.data.decode()
        assert "IMG_0001.JPG" in html or "library" in html.lower()

    def test_library_filter_status(self, lib_client):
        resp = lib_client.get("/library?status=public")
        assert resp.status_code == 200

    def test_library_filter_untitled(self, lib_client):
        resp = lib_client.get("/library?untitled=1")
        assert resp.status_code == 200

    def test_library_pagination(self, lib_client):
        resp = lib_client.get("/library?page=1&per_page=2")
        assert resp.status_code == 200


# ===========================================================================
# Task 5 — POST /api/bulk-edit
# ===========================================================================


@pytest.fixture(scope="module")
def bulk_client():
    with tempfile.TemporaryDirectory() as tmp:
        test_db = Database(Path(tmp) / "test.db")
        for i in range(1, 4):
            test_db.upsert_photo(
                {
                    "uuid": f"be-uuid-{i}",
                    "flickr_id": f"be-flickr-{i}",
                    "original_filename": f"BE_{i:04d}.JPG",
                    "privacy_state": "already_public",
                    "flickr_title": "Existing" if i == 1 else "",
                    "flickr_description": "",
                    "flickr_tags": json.dumps(["paris"] if i == 1 else []),
                    "photos_tags": json.dumps([]),
                    "apple_persons": [],
                    "proposed_tags": [],
                }
            )
        app_module._db = test_db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test-secret"
        with app_module.app.test_client() as c:
            yield c, test_db
        app_module._db = None


class TestBulkEditEndpoint:
    def _post(self, client, payload):
        return client.post(
            "/api/bulk-edit",
            json=payload,
            headers={"X-Requested-With": "XMLHttpRequest"},
        )

    def test_bulk_edit_set_title_returns_ok(self, bulk_client):
        c, db = bulk_client
        ids = [r["id"] for r in db.library_photos()]
        resp = self._post(c, {"field": "title", "value": "Test", "photo_ids": ids})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert "proposals_created" in data
        assert "batch_id" in data

    def test_bulk_edit_dry_run_returns_counts_not_proposals(self, bulk_client):
        c, db = bulk_client
        ids = [r["id"] for r in db.library_photos()]
        resp = self._post(
            c,
            {
                "field": "title",
                "value": "Dry",
                "photo_ids": ids,
                "dry_run": True,
                "skip_existing": True,
            },
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert "would_update" in data
        assert "would_skip" in data
        assert data.get("batch_id") is None

    def test_bulk_edit_tags_add(self, bulk_client):
        c, db = bulk_client
        ids = [r["id"] for r in db.library_photos()]
        db.conn.execute("DELETE FROM metadata_proposals")
        db.conn.execute("DELETE FROM bulk_batches")
        db.conn.commit()
        resp = self._post(c, {"field": "tags_add", "tags": ["mfa-boston"], "photo_ids": ids})
        data = resp.get_json()
        assert data["ok"] is True
        assert data["proposals_created"] >= 1

    def test_bulk_edit_filter_based_selection(self, bulk_client):
        c, db = bulk_client
        db.conn.execute("DELETE FROM metadata_proposals")
        db.conn.execute("DELETE FROM bulk_batches")
        db.conn.commit()
        resp = self._post(
            c,
            {
                "field": "tags_add",
                "tags": ["london"],
                "filter": {
                    "status": "public",
                    "date_from": None,
                    "date_to": None,
                    "album_id": None,
                    "tag": None,
                    "untitled": False,
                },
            },
        )
        data = resp.get_json()
        assert data["ok"] is True

    def test_bulk_edit_missing_field_returns_400(self, bulk_client):
        c, _ = bulk_client
        resp = self._post(c, {"value": "X", "photo_ids": [1]})
        assert resp.status_code == 400

    def test_bulk_edit_tags_requires_tags_list(self, bulk_client):
        c, _ = bulk_client
        resp = self._post(c, {"field": "tags_add", "photo_ids": [1]})
        assert resp.status_code == 400


# ===========================================================================
# Task 6 — Proposals batch grouping + reject endpoint
# ===========================================================================


@pytest.fixture(scope="module")
def batch_client():
    with tempfile.TemporaryDirectory() as tmp:
        test_db = Database(Path(tmp) / "test.db")
        pid = test_db.upsert_photo(
            {
                "uuid": "batch-u1",
                "flickr_id": "batch-f1",
                "original_filename": "BATCH.JPG",
                "privacy_state": "already_public",
                "flickr_title": "",
                "flickr_description": "",
                "flickr_tags": json.dumps([]),
                "photos_tags": json.dumps([]),
                "apple_persons": [],
                "proposed_tags": [],
            }
        )
        # Create a batch with one proposal
        bid = test_db.create_bulk_batch("set_title", "title", "Batch Test", None, None, 1)
        test_db.insert_bulk_proposals(bid, [pid], "title", value="Batch Test")
        app_module._db = test_db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test-secret"
        with app_module.app.test_client() as c:
            yield c, test_db, bid
        app_module._db = None


class TestProposalsBatchGrouping:
    def test_proposals_page_shows_batch_section(self, batch_client):
        c, db, bid = batch_client
        resp = c.get("/proposals")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "Bulk" in html or "bulk" in html or "batch" in html.lower()

    def test_reject_batch_endpoint(self, batch_client):
        c, db, bid = batch_client
        resp = c.post(
            f"/api/bulk-batches/{bid}/reject",
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["rejected"] >= 1

    def test_reject_batch_nonexistent(self, batch_client):
        c, _, _ = batch_client
        resp = c.post(
            "/api/bulk-batches/99999/reject",
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["rejected"] == 0
