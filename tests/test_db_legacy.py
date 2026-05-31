from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.db import Database  # noqa: E402


def _migrate(db: Database) -> None:
    from db.migrations.migrate_026_legacy_index import run_on_conn

    run_on_conn(db.conn)


def _make_db(tmp_path) -> Database:
    db = Database(str(tmp_path / "curator.db"))
    _migrate(db)
    return db


def _asset(library_uuid="L", asset_uuid="A", **over) -> dict:
    row = {
        "library_uuid": library_uuid,
        "asset_uuid": asset_uuid,
        "original_filename": "img.jpg",
        "fingerprint": "fp",
        "date_taken": "2010-06-01 12:00:00",
        "width": 4000,
        "height": 3000,
        "latitude": None,
        "longitude": None,
        "title": "Birthday",
        "description": None,
        "keywords": "[]",
        "labels": "[]",
        "persons": '["Isaac"]',
        "named_face_count": 1,
        "unknown_face_count": 0,
        "master_rel_path": "Masters/2010/img.jpg",
        "thumbnail_cache_key": "deadbeef",
        "thumbnail_status": "ok",
    }
    row.update(over)
    return row


class TestLibraryMetadata:
    def test_set_then_get(self, tmp_path):
        db = _make_db(tmp_path)
        db.set_legacy_library(
            {
                "library_uuid": "L",
                "display_name": "Old",
                "source_path_last_seen": "/mnt/x",
                "schema_version": 5002,
                "db_mtime": "2026-01-01T00:00:00",
                "db_size": 123,
                "db_head_hash": "abc",
                "asset_count": 0,
            }
        )
        lib = db.get_legacy_library("L")
        assert lib["db_head_hash"] == "abc"
        assert lib["schema_version"] == 5002

    def test_get_missing_returns_none(self, tmp_path):
        db = _make_db(tmp_path)
        assert db.get_legacy_library("nope") is None

    def test_set_is_upsert(self, tmp_path):
        db = _make_db(tmp_path)
        db.set_legacy_library({"library_uuid": "L", "db_head_hash": "a", "asset_count": 0})
        db.set_legacy_library({"library_uuid": "L", "db_head_hash": "b", "asset_count": 5})
        lib = db.get_legacy_library("L")
        assert lib["db_head_hash"] == "b"
        assert lib["asset_count"] == 5


class TestUpsertAsset:
    def test_insert(self, tmp_path):
        db = _make_db(tmp_path)
        db.set_legacy_library({"library_uuid": "L", "asset_count": 0})
        db.upsert_legacy_asset(_asset())
        assert db.legacy_asset_count("L") == 1

    def test_update_same_identity_no_duplicate(self, tmp_path):
        db = _make_db(tmp_path)
        db.set_legacy_library({"library_uuid": "L", "asset_count": 0})
        db.upsert_legacy_asset(_asset(title="Birthday"))
        db.upsert_legacy_asset(_asset(title="Party"))
        assert db.legacy_asset_count("L") == 1
        rows = list(db.iter_legacy_assets("L"))
        assert rows[0]["title"] == "Party"

    def test_reindex_from_different_path_updates_same_row(self, tmp_path):
        db = _make_db(tmp_path)
        db.set_legacy_library({"library_uuid": "L", "asset_count": 0})
        db.upsert_legacy_asset(_asset(master_rel_path="Masters/a.jpg"))
        db.upsert_legacy_asset(_asset(master_rel_path="originals/a.jpg"))
        assert db.legacy_asset_count("L") == 1


class TestIterAssets:
    def test_iter_filtered_by_library(self, tmp_path):
        db = _make_db(tmp_path)
        db.set_legacy_library({"library_uuid": "L", "asset_count": 0})
        db.set_legacy_library({"library_uuid": "M", "asset_count": 0})
        db.upsert_legacy_asset(_asset("L", "A"))
        db.upsert_legacy_asset(_asset("M", "B"))
        uuids = {r["asset_uuid"] for r in db.iter_legacy_assets("L")}
        assert uuids == {"A"}


class TestReconcileDelete:
    def test_deletes_unseen_returns_cache_keys(self, tmp_path):
        db = _make_db(tmp_path)
        db.set_legacy_library({"library_uuid": "L", "asset_count": 0})
        db.upsert_legacy_asset(_asset("L", "A", thumbnail_cache_key="key-a"))
        db.upsert_legacy_asset(_asset("L", "B", thumbnail_cache_key="key-b"))
        removed = db.delete_legacy_assets_not_in("L", {"A"})
        assert removed == ["key-b"]
        assert db.legacy_asset_count("L") == 1

    def test_empty_seen_set_deletes_all(self, tmp_path):
        db = _make_db(tmp_path)
        db.set_legacy_library({"library_uuid": "L", "asset_count": 0})
        db.upsert_legacy_asset(_asset("L", "A", thumbnail_cache_key="key-a"))
        removed = db.delete_legacy_assets_not_in("L", set())
        assert removed == ["key-a"]
        assert db.legacy_asset_count("L") == 0
