# tests/test_db_legacy_upload.py
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.db import Database  # noqa: E402


def _make_db(tmp_path) -> Database:
    from db.migrations.migrate_020_operation_log import run as run_op_log
    from db.migrations.migrate_026_legacy_index import run_on_conn as run_legacy
    from db.migrations.migrate_031_legacy_upload import run_on_conn as run_upload

    db = Database(str(tmp_path / "curator.db"))
    run_op_log(str(tmp_path / "curator.db"))
    run_legacy(db.conn)
    run_upload(db.conn)
    return db


def _seed(db, library_uuid="L", asset_uuid="A", date_taken="2005-06-01 12:00:00"):
    db.set_legacy_library({"library_uuid": library_uuid, "display_name": "Test"})
    db.upsert_legacy_asset(
        {
            "library_uuid": library_uuid,
            "asset_uuid": asset_uuid,
            "original_filename": "img.jpg",
            "date_taken": date_taken,
            "named_face_count": 0,
            "unknown_face_count": 0,
            "title": "Vacation",
            "description": "sunny day",
            "keywords": '["beach"]',
        }
    )


class TestMarkLegacyUploaded:
    def test_sets_uploaded_flickr_id(self, tmp_path):
        db = _make_db(tmp_path)
        _seed(db)
        db.mark_legacy_uploaded("L", "A", "flickr123")
        row = db.conn.execute(
            "SELECT uploaded_flickr_id, uploaded_at FROM legacy_assets WHERE asset_uuid='A'"
        ).fetchone()
        assert row["uploaded_flickr_id"] == "flickr123"
        assert row["uploaded_at"] is not None

    def test_overwrites_previous_value(self, tmp_path):
        db = _make_db(tmp_path)
        _seed(db)
        db.mark_legacy_uploaded("L", "A", "first")
        db.mark_legacy_uploaded("L", "A", "second")
        row = db.conn.execute(
            "SELECT uploaded_flickr_id FROM legacy_assets WHERE asset_uuid='A'"
        ).fetchone()
        assert row["uploaded_flickr_id"] == "second"


class TestIterUnrecoveredLegacyUploads:
    def test_returns_asset_when_no_photos_row(self, tmp_path):
        db = _make_db(tmp_path)
        _seed(db)
        db.mark_legacy_uploaded("L", "A", "flickr999")
        results = db.iter_unrecovered_legacy_uploads("L")
        assert len(results) == 1
        assert results[0]["asset_uuid"] == "A"
        assert results[0]["uploaded_flickr_id"] == "flickr999"

    def test_excludes_asset_when_photos_row_exists(self, tmp_path):
        db = _make_db(tmp_path)
        _seed(db)
        db.mark_legacy_uploaded("L", "A", "flickr999")
        db.conn.execute(
            "INSERT INTO photos (flickr_id, uuid, privacy_state) VALUES ('flickr999', NULL, 'auto_private')"
        )
        db.conn.commit()
        assert db.iter_unrecovered_legacy_uploads("L") == []

    def test_excludes_asset_with_no_uploaded_flickr_id(self, tmp_path):
        db = _make_db(tmp_path)
        _seed(db)
        assert db.iter_unrecovered_legacy_uploads("L") == []

    def test_scoped_to_library(self, tmp_path):
        db = _make_db(tmp_path)
        _seed(db, library_uuid="L1", asset_uuid="X")
        _seed(db, library_uuid="L2", asset_uuid="Y")
        db.set_legacy_library({"library_uuid": "L2", "display_name": "Other"})
        db.mark_legacy_uploaded("L2", "Y", "flickr777")
        assert db.iter_unrecovered_legacy_uploads("L1") == []
        assert len(db.iter_unrecovered_legacy_uploads("L2")) == 1

    def test_excludes_asset_when_duplicate_photos_row_present(self, tmp_path):
        """Corruption guard: if photos row already exists for uploaded_flickr_id,
        do not surface it as unrecovered — avoids a double-write attempt."""
        db = _make_db(tmp_path)
        _seed(db)
        db.mark_legacy_uploaded("L", "A", "flickr_dup")
        # Simulate corruption: photos row already exists (e.g. from a prior recovery run)
        db.conn.execute(
            "INSERT INTO photos (flickr_id, uuid, privacy_state) "
            "VALUES ('flickr_dup', NULL, 'candidate_public')"
        )
        db.conn.commit()
        # Must not surface the asset — the photos row already exists
        assert db.iter_unrecovered_legacy_uploads("L") == []


class TestRecordLegacyUpload:
    def test_creates_photos_row_and_operation_log(self, tmp_path):
        db = _make_db(tmp_path)
        photo_id = db.record_legacy_upload(
            flickr_id="flickr42",
            privacy_state="auto_private",
            privacy_reason="geofence: home",
            date_taken="2005-06-01 12:00:00",
            width=4000,
            height=3000,
            flickr_title="Vacation",
            flickr_tags='["beach"]',
            flickr_description="sunny day",
            trigger="legacy:A clf=1",
        )
        row = db.conn.execute("SELECT * FROM photos WHERE id = ?", (photo_id,)).fetchone()
        assert row["flickr_id"] == "flickr42"
        assert row["uuid"] is None
        assert row["privacy_state"] == "auto_private"
        assert row["flickr_title"] == "Vacation"

        log_row = db.conn.execute(
            "SELECT * FROM operation_log WHERE photo_id = ?", (photo_id,)
        ).fetchone()
        assert log_row["operation"] == "upload_legacy_asset"
        assert log_row["target"] == "flickr_id"
        assert log_row["old_value"] is None
        assert log_row["new_value"] == "flickr42"
        assert log_row["trigger"] == "legacy:A clf=1"
        assert log_row["actor"] == "bp"

    def test_photos_row_and_log_roll_back_together(self, tmp_path):
        """If the operation_log INSERT fails, the photos INSERT should also roll back."""
        db = _make_db(tmp_path)
        # Drop operation_log to force INSERT failure
        db.conn.execute("DROP TABLE operation_log")
        db.conn.commit()

        try:
            db.record_legacy_upload(
                flickr_id="flickr99",
                privacy_state="auto_private",
                privacy_reason="test",
                date_taken=None,
                width=None,
                height=None,
                flickr_title="",
                flickr_tags="[]",
                flickr_description="",
                trigger="legacy:X clf=1",
            )
        except Exception:
            pass

        count = db.conn.execute(
            "SELECT COUNT(*) FROM photos WHERE flickr_id='flickr99'"
        ).fetchone()[0]
        assert count == 0
