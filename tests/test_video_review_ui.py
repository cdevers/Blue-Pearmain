"""
tests/test_video_review_ui.py — tests for video detection in the review UI

Run from repo root:
    python -m pytest tests/test_video_review_ui.py -v
"""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import reviewer.app as app_module
from db.db import Database
from db.migrations.migrate_021_is_video import run as migrate_is_video


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "test.db"
    db = Database(path)
    db.close()
    return path


class TestMigration021:
    def test_migration_adds_is_video_column(self, db_path):
        migrate_is_video(str(db_path))
        conn = sqlite3.connect(db_path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(photos)").fetchall()}
        conn.close()
        assert "is_video" in cols

    def test_migration_backfills_mov(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute(
            "INSERT INTO photos (uuid, original_filename, privacy_state, updated_at, date_synced)"
            " VALUES ('u-mov', 'VID_001.MOV', 'candidate_public', '2024-01-01', '2024-01-01')"
        )
        conn.commit()
        conn.close()
        migrate_is_video(str(db_path))
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT is_video FROM photos WHERE uuid = 'u-mov'").fetchone()
        conn.close()
        assert row["is_video"] == 1

    def test_migration_backfills_mp4(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO photos (uuid, original_filename, privacy_state, updated_at, date_synced)"
            " VALUES ('u-mp4', 'clip.mp4', 'candidate_public', '2024-01-01', '2024-01-01')"
        )
        conn.commit()
        conn.close()
        migrate_is_video(str(db_path))
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT is_video FROM photos WHERE uuid = 'u-mp4'").fetchone()
        conn.close()
        assert row["is_video"] == 1

    def test_migration_backfills_m4v(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO photos (uuid, original_filename, privacy_state, updated_at, date_synced)"
            " VALUES ('u-m4v', 'clip.M4V', 'candidate_public', '2024-01-01', '2024-01-01')"
        )
        conn.commit()
        conn.close()
        migrate_is_video(str(db_path))
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT is_video FROM photos WHERE uuid = 'u-m4v'").fetchone()
        conn.close()
        assert row["is_video"] == 1

    def test_migration_leaves_jpg_as_zero(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO photos (uuid, original_filename, privacy_state, updated_at, date_synced)"
            " VALUES ('u-jpg', 'IMG_001.JPG', 'candidate_public', '2024-01-01', '2024-01-01')"
        )
        conn.commit()
        conn.close()
        migrate_is_video(str(db_path))
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT is_video FROM photos WHERE uuid = 'u-jpg'").fetchone()
        conn.close()
        assert row["is_video"] == 0

    def test_migration_leaves_heic_as_zero(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO photos (uuid, original_filename, privacy_state, updated_at, date_synced)"
            " VALUES ('u-heic', 'IMG_002.HEIC', 'candidate_public', '2024-01-01', '2024-01-01')"
        )
        conn.commit()
        conn.close()
        migrate_is_video(str(db_path))
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT is_video FROM photos WHERE uuid = 'u-heic'").fetchone()
        conn.close()
        assert row["is_video"] == 0

    def test_migration_is_idempotent(self, db_path):
        migrate_is_video(str(db_path))
        # Running twice must not raise
        migrate_is_video(str(db_path))
        conn = sqlite3.connect(db_path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(photos)").fetchall()}
        conn.close()
        assert "is_video" in cols


class TestScannerIsVideo:
    def test_scanner_sets_is_video_for_movie(self):
        """photo.ismovie = True → is_video = 1"""
        from unittest.mock import MagicMock

        from poller.scanner import photos_record_to_db  # noqa: PLC0415

        photo = MagicMock()
        photo.ismovie = True
        photo.live_photo = False
        photo.uuid = "test-uuid"
        photo.original_filename = "VID_001.MOV"
        photo.filename = "VID_001.MOV"
        photo.date = None
        photo.date_added = None
        photo.media_analysis = None
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
        photo.fingerprint = ""
        photo.width = 1920
        photo.height = 1080
        photo.favorite = False

        row = photos_record_to_db(photo)
        assert row.get("is_video") == 1

    def test_scanner_sets_is_video_zero_for_still(self):
        """photo.ismovie = False → is_video = 0"""
        from unittest.mock import MagicMock

        from poller.scanner import photos_record_to_db  # noqa: PLC0415

        photo = MagicMock()
        photo.ismovie = False
        photo.live_photo = False
        photo.uuid = "test-uuid-2"
        photo.original_filename = "IMG_001.JPG"
        photo.filename = "IMG_001.JPG"
        photo.date = None
        photo.date_added = None
        photo.media_analysis = None
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
        photo.fingerprint = ""
        photo.width = 4032
        photo.height = 3024
        photo.favorite = False

        row = photos_record_to_db(photo)
        assert row.get("is_video") == 0

    def test_scanner_live_photo_is_not_video(self):
        """Live Photo: ismovie=False, live_photo=True → is_video = 0"""
        from unittest.mock import MagicMock

        from poller.scanner import photos_record_to_db  # noqa: PLC0415

        photo = MagicMock()
        photo.ismovie = False
        photo.live_photo = True
        photo.uuid = "test-uuid-3"
        photo.original_filename = "IMG_001.HEIC"
        photo.filename = "IMG_001.HEIC"
        photo.date = None
        photo.date_added = None
        photo.media_analysis = None
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
        photo.fingerprint = ""
        photo.width = 4032
        photo.height = 3024
        photo.favorite = False

        row = photos_record_to_db(photo)
        assert row.get("is_video") == 0


class TestPollerIsVideo:
    def test_poller_sets_is_video_for_flickr_video(self):
        """photo dict with media='video' → is_video = 1"""
        from poller.poller import flickr_photo_to_db  # noqa: PLC0415

        photo = {
            "id": "12345",
            "secret": "abc",
            "server": "s1",
            "farm": 1,
            "title": "A video",
            "media": "video",
            "tags": "",
        }
        row = flickr_photo_to_db(photo, info=None)
        assert row.get("is_video") == 1

    def test_poller_sets_is_video_zero_for_photo(self):
        """photo dict with media='photo' → is_video = 0"""
        from poller.poller import flickr_photo_to_db  # noqa: PLC0415

        photo = {
            "id": "67890",
            "secret": "def",
            "server": "s2",
            "farm": 2,
            "title": "A photo",
            "media": "photo",
            "tags": "",
        }
        row = flickr_photo_to_db(photo, info=None)
        assert row.get("is_video") == 0

    def test_poller_sets_is_video_zero_when_media_absent(self):
        """photo dict missing media key → is_video = 0"""
        from poller.poller import flickr_photo_to_db  # noqa: PLC0415

        photo = {
            "id": "11111",
            "secret": "ghi",
            "server": "s3",
            "farm": 3,
            "title": "No media key",
            "tags": "",
        }
        row = flickr_photo_to_db(photo, info=None)
        assert row.get("is_video") == 0


@pytest.fixture()
def db_fixture(tmp_path):
    """DB with migration applied and seeded video + still photos."""
    db = Database(tmp_path / "test.db")
    migrate_is_video(str(tmp_path / "test.db"))

    db.upsert_photo(
        {
            "uuid": "uuid-vid-1",
            "original_filename": "VID_001.MOV",
            "privacy_state": "candidate_public",
            "is_video": 1,
            "apple_persons": [],
            "proposed_tags": [],
        }
    )
    db.upsert_photo(
        {
            "uuid": "uuid-still-1",
            "original_filename": "IMG_001.JPG",
            "privacy_state": "candidate_public",
            "is_video": 0,
            "apple_persons": [],
            "proposed_tags": [],
        }
    )
    return db


class TestReviewQueueIsVideo:
    def test_review_queue_returns_is_video_field(self, db_fixture):
        photos = db_fixture.review_queue(states=["candidate_public"])
        assert len(photos) == 2
        by_uuid = {p["uuid"]: p for p in photos}
        assert by_uuid["uuid-vid-1"]["is_video"] == 1
        assert by_uuid["uuid-still-1"]["is_video"] == 0
        db_fixture.close()


@pytest.fixture()
def flask_client_video(tmp_path):
    """Flask test client with one video and one still photo."""
    db = Database(tmp_path / "test.db")
    migrate_is_video(str(tmp_path / "test.db"))

    db.upsert_photo(
        {
            "uuid": "uuid-vid-flask",
            "original_filename": "VID_FLASK.MOV",
            "privacy_state": "candidate_public",
            "is_video": 1,
            "apple_persons": [],
            "proposed_tags": [],
        }
    )
    db.upsert_photo(
        {
            "uuid": "uuid-still-flask",
            "original_filename": "IMG_FLASK.JPG",
            "privacy_state": "candidate_public",
            "is_video": 0,
            "apple_persons": [],
            "proposed_tags": [],
        }
    )

    app_module._db = db
    app_module.app.config["TESTING"] = True
    app_module.app.config["SECRET_KEY"] = "test"
    with app_module.app.test_client() as c:
        yield c
    app_module._db = None
    db.close()


class TestVideoTemplate:
    def test_video_badge_rendered_for_video_tile(self, flask_client_video):
        r = flask_client_video.get("/review?state=candidate_public")
        html = r.data.decode()
        assert "video-badge" in html
        assert "▶" in html

    def test_video_label_rendered_for_video_tile(self, flask_client_video):
        r = flask_client_video.get("/review?state=candidate_public")
        html = r.data.decode()
        assert "video-label" in html
        assert ">video<" in html

    def test_video_badge_only_on_video_tile(self, flask_client_video):
        r = flask_client_video.get("/review?state=candidate_public")
        html = r.data.decode()
        # One occurrence in CSS definition + one in the rendered video tile = 2 total;
        # the still photo tile must not add a third.
        assert html.count("video-badge") == 2

    def test_video_label_only_on_video_tile(self, flask_client_video):
        r = flask_client_video.get("/review?state=candidate_public")
        html = r.data.decode()
        # One occurrence in CSS definition + one in the rendered video tile = 2 total.
        assert html.count("video-label") == 2
