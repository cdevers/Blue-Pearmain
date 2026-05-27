"""Tests that poller and scanner populate the new geo cache columns (#145)."""

from __future__ import annotations
import tempfile
from pathlib import Path
import pytest
from db.db import Database


def _db_with_photo(**kwargs) -> tuple[Database, int]:
    tmp = tempfile.mkdtemp()
    db = Database(Path(tmp) / "t.db")
    pid = db.upsert_photo(
        {
            "uuid": "test-uuid-1",
            "original_filename": "IMG_001.JPG",
            "privacy_state": "needs_review",
            "apple_persons": [],
            "apple_labels": [],
            **kwargs,
        }
    )
    return db, pid


class TestGeoCache:
    def test_flickr_lat_lon_written_when_present(self):
        db, pid = _db_with_photo(flickr_id="12345")
        db.upsert_photo(
            {
                "flickr_id": "12345",
                "flickr_latitude": 42.3601,
                "flickr_longitude": -71.0589,
            }
        )
        row = db.conn.execute(
            "SELECT flickr_latitude, flickr_longitude FROM photos WHERE id=?", (pid,)
        ).fetchone()
        assert row["flickr_latitude"] == pytest.approx(42.3601)
        assert row["flickr_longitude"] == pytest.approx(-71.0589)

    def test_photos_lat_lon_written_when_present(self):
        db, pid = _db_with_photo()
        db.upsert_photo(
            {
                "uuid": "test-uuid-1",
                "photos_latitude": 42.3601,
                "photos_longitude": -71.0589,
            }
        )
        row = db.conn.execute(
            "SELECT photos_latitude, photos_longitude FROM photos WHERE id=?", (pid,)
        ).fetchone()
        assert row["photos_latitude"] == pytest.approx(42.3601)
        assert row["photos_longitude"] == pytest.approx(-71.0589)

    def test_geo_cache_null_when_not_set(self):
        db, pid = _db_with_photo()
        row = db.conn.execute(
            "SELECT flickr_latitude, flickr_longitude, photos_latitude, photos_longitude FROM photos WHERE id=?",
            (pid,),
        ).fetchone()
        assert row["flickr_latitude"] is None
        assert row["photos_latitude"] is None
