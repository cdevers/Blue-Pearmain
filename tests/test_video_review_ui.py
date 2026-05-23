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
