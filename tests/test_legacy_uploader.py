# tests/test_legacy_uploader.py
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "poller"))


def _make_db(tmp_path):
    from db.db import Database
    from db.migrations.migrate_020_operation_log import run as run_op_log
    from db.migrations.migrate_026_legacy_index import run_on_conn as run_legacy
    from db.migrations.migrate_031_legacy_upload import run_on_conn as run_upload

    db = Database(str(tmp_path / "curator.db"))
    run_op_log(str(tmp_path / "curator.db"))
    run_legacy(db.conn)
    run_upload(db.conn)
    return db


def _seed_lib(db, library_uuid="L"):
    db.set_legacy_library({"library_uuid": library_uuid, "display_name": "Test"})


def _seed_asset(
    db,
    asset_uuid="A",
    date_taken="2005-06-01 12:00:00",
    library_uuid="L",
    master_rel_path="Masters/img.jpg",
    **kw,
):
    db.upsert_legacy_asset(
        {
            "library_uuid": library_uuid,
            "asset_uuid": asset_uuid,
            "original_filename": "img.jpg",
            "date_taken": date_taken,
            "named_face_count": 0,
            "unknown_face_count": 0,
            "master_rel_path": master_rel_path,
            "title": "Vacation",
            "description": "sunny",
            "keywords": '["beach"]',
            **kw,
        }
    )


class _StubFlickr:
    """Records upload calls; returns sequential fake flickr_ids."""

    def __init__(self, fail=False, date_set_ok=True):
        self.calls = []
        self._fail = fail
        self._date_set_ok = date_set_ok
        self._counter = 0

    def upload_photo(
        self,
        path,
        *,
        title="",
        description="",
        tags="",
        date_taken=None,
        is_public=0,
        is_friend=0,
        is_family=0,
    ):
        from flickr.flickr_client import FlickrError

        self.calls.append({"path": path, "date_taken": date_taken})
        if self._fail:
            raise FlickrError(-1, "simulated upload failure")
        self._counter += 1
        return f"flickr{self._counter:04d}", self._date_set_ok


def _run(db, library_uuid, library_path, flickr, *, dry_run=False, limit=None):
    from legacy_uploader import upload_unmatched_assets
    from analyzer.privacy import CLASSIFIER_VERSION

    return upload_unmatched_assets(
        db,
        library_uuid,
        library_path,
        flickr,
        self_name="",
        zones=[],
        person_policies={},
        classifier_version=CLASSIFIER_VERSION,
        limit=limit,
        dry_run=dry_run,
    )


class TestUploadUnmatchedAssets:
    def test_successful_upload_creates_photos_row(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_lib(db)
        _seed_asset(db)
        photo_file = tmp_path / "Masters" / "img.jpg"
        photo_file.parent.mkdir(parents=True)
        photo_file.write_bytes(b"JPEG")

        flickr = _StubFlickr()
        counts = _run(db, "L", tmp_path, flickr)

        assert counts["uploaded"] == 1
        assert counts["upload_failed"] == 0
        row = db.conn.execute("SELECT * FROM photos WHERE flickr_id = 'flickr0001'").fetchone()
        assert row is not None
        assert row["uuid"] is None

    def test_uploaded_flickr_id_set_after_upload(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_lib(db)
        _seed_asset(db)
        (tmp_path / "Masters").mkdir()
        (tmp_path / "Masters" / "img.jpg").write_bytes(b"JPEG")

        _run(db, "L", tmp_path, _StubFlickr())

        row = db.conn.execute(
            "SELECT uploaded_flickr_id FROM legacy_assets WHERE asset_uuid='A'"
        ).fetchone()
        assert row["uploaded_flickr_id"] == "flickr0001"

    def test_dry_run_makes_no_writes(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_lib(db)
        _seed_asset(db)
        (tmp_path / "Masters").mkdir()
        (tmp_path / "Masters" / "img.jpg").write_bytes(b"JPEG")

        flickr = _StubFlickr()
        counts = _run(db, "L", tmp_path, flickr, dry_run=True)

        assert flickr.calls == []
        assert db.conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == 0
        assert counts["candidate_public"] == 1

    def test_asset_with_uploaded_flickr_id_is_skipped(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_lib(db)
        _seed_asset(db)
        db.mark_legacy_uploaded("L", "A", "already_done")

        flickr = _StubFlickr()
        counts = _run(db, "L", tmp_path, flickr)

        assert flickr.calls == []
        assert counts["skipped_already_uploaded"] == 1

    def test_missing_file_is_skipped(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_lib(db)
        _seed_asset(db)  # file does not exist on disk

        flickr = _StubFlickr()
        counts = _run(db, "L", tmp_path, flickr)

        assert flickr.calls == []
        assert counts["skipped_missing_file"] == 1
        assert counts["uploaded"] == 0

    def test_upload_failure_is_isolated(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_lib(db)
        _seed_asset(db, asset_uuid="A")
        _seed_asset(
            db, asset_uuid="B", date_taken="2006-01-01 10:00:00", master_rel_path="Masters/b.jpg"
        )
        (tmp_path / "Masters").mkdir()
        (tmp_path / "Masters" / "img.jpg").write_bytes(b"JPEG")
        (tmp_path / "Masters" / "b.jpg").write_bytes(b"JPEG")

        call_count = [0]

        class _FailFirst(_StubFlickr):
            def upload_photo(self, path, **kw):
                call_count[0] += 1
                if call_count[0] == 1:
                    from flickr.flickr_client import FlickrError

                    raise FlickrError(-1, "first fails")
                self._counter += 1
                return f"flickr{self._counter:04d}", True

        counts = _run(db, "L", tmp_path, _FailFirst())
        assert counts["upload_failed"] == 1
        assert counts["uploaded"] == 1

    def test_date_set_failed_is_counted(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_lib(db)
        _seed_asset(db)
        (tmp_path / "Masters").mkdir()
        (tmp_path / "Masters" / "img.jpg").write_bytes(b"JPEG")

        counts = _run(db, "L", tmp_path, _StubFlickr(date_set_ok=False))
        assert counts["date_set_failed"] == 1
        assert counts["uploaded"] == 1  # still uploaded

    def test_phase1_recovery_creates_photos_row(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_lib(db)
        _seed_asset(db)
        # Simulate: uploaded but photos row write failed
        db.mark_legacy_uploaded("L", "A", "orphan001")
        # No photos row for orphan001

        flickr = _StubFlickr()
        counts = _run(db, "L", tmp_path, flickr)

        assert counts["recovered"] == 1
        assert flickr.calls == []  # no new upload
        row = db.conn.execute("SELECT * FROM photos WHERE flickr_id='orphan001'").fetchone()
        assert row is not None

    def test_phase1_recovery_reports_only_in_dry_run(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_lib(db)
        _seed_asset(db)
        db.mark_legacy_uploaded("L", "A", "orphan002")

        counts = _run(db, "L", tmp_path, _StubFlickr(), dry_run=True)

        assert counts["recovered"] == 1
        assert db.conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == 0

    def test_privacy_state_stored_correctly(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_lib(db)
        _seed_asset(db, named_face_count=1, persons='["Alice"]')
        (tmp_path / "Masters").mkdir()
        (tmp_path / "Masters" / "img.jpg").write_bytes(b"JPEG")

        _run(db, "L", tmp_path, _StubFlickr())

        row = db.conn.execute("SELECT privacy_state FROM photos").fetchone()
        assert row["privacy_state"] == "needs_review"

    def test_limit_caps_uploads(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_lib(db)
        for i in range(5):
            _seed_asset(
                db,
                asset_uuid=f"A{i}",
                date_taken=f"200{i}-01-01 10:00:00",
                master_rel_path=f"Masters/img{i}.jpg",
            )
            (tmp_path / "Masters").mkdir(exist_ok=True)
            (tmp_path / f"Masters/img{i}.jpg").write_bytes(b"JPEG")

        flickr = _StubFlickr()
        counts = _run(db, "L", tmp_path, flickr, limit=2)
        assert counts["uploaded"] == 2

    def test_dry_run_recovered_excluded_from_eligible(self, tmp_path):
        """Recovery candidates (uploaded_flickr_id set) must not inflate 'eligible'.
        'eligible' counts Phase 2 candidates only; 'recovered' is a separate counter."""
        db = _make_db(tmp_path)
        _seed_lib(db)
        # Asset A: uploaded but photos row missing (Phase 1 recovery candidate)
        _seed_asset(db, asset_uuid="A", date_taken="2003-01-01 10:00:00")
        db.mark_legacy_uploaded("L", "A", "orphan_recover")
        # Asset B: unmatched, not yet uploaded (Phase 2 candidate)
        _seed_asset(
            db, asset_uuid="B", date_taken="2004-01-01 10:00:00", master_rel_path="Masters/b.jpg"
        )
        (tmp_path / "Masters").mkdir()
        (tmp_path / "Masters" / "b.jpg").write_bytes(b"JPEG")

        counts = _run(db, "L", tmp_path, _StubFlickr(), dry_run=True)

        # Recovery candidate is NOT in eligible
        assert counts["eligible"] == 1  # only asset B
        assert counts["recovered"] == 1  # asset A reported separately
        assert counts["candidate_public"] == 1  # asset B classified

    def test_phase1_skips_asset_when_photos_row_already_exists(self, tmp_path):
        """Corruption resilience: if a photos row already exists for uploaded_flickr_id
        (e.g. from a previous recovery run), Phase 1 silently skips it — no error,
        no double-write."""
        db = _make_db(tmp_path)
        _seed_lib(db)
        _seed_asset(db)
        db.mark_legacy_uploaded("L", "A", "already_there")
        # Simulate the photos row already existing (prior successful recovery)
        db.conn.execute(
            "INSERT INTO photos (flickr_id, uuid, privacy_state) "
            "VALUES ('already_there', NULL, 'candidate_public')"
        )
        db.conn.commit()

        flickr = _StubFlickr()
        counts = _run(db, "L", tmp_path, flickr)

        # Phase 1 finds no unrecovered assets (iter_unrecovered excludes it)
        assert counts["recovered"] == 0
        # No duplicate photos row created
        assert (
            db.conn.execute(
                "SELECT COUNT(*) FROM photos WHERE flickr_id='already_there'"
            ).fetchone()[0]
            == 1
        )
