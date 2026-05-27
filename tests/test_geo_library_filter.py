"""Library DB filter for photos with no location (#145)."""

from __future__ import annotations
import tempfile
from pathlib import Path
import pytest
from db.db import Database


def _photo(i: int, **kwargs) -> dict:
    return {
        "uuid": f"geo-filter-u{i}",
        "original_filename": f"IMG_{i:04d}.JPG",
        "privacy_state": "needs_review",
        "apple_persons": [],
        "apple_labels": [],
        **kwargs,
    }


@pytest.fixture()
def geo_db():
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "t.db")
        db.upsert_photo(_photo(1, latitude=42.3601, longitude=-71.0589))  # geotagged
        db.upsert_photo(_photo(2))  # no geo, unconfirmed
        db.upsert_photo(_photo(3))  # no geo, unconfirmed
        db.upsert_photo(_photo(4, geo_confirmed_none=1))  # confirmed none
        yield db


class TestNoLocationFilter:
    def test_no_location_count_excludes_geotagged(self, geo_db):
        assert geo_db.no_location_count() == 2

    def test_no_location_count_excludes_confirmed_none(self, geo_db):
        # photo 4 has geo_confirmed_none=1, should not be counted
        assert geo_db.no_location_count() == 2

    def test_library_photos_no_location_returns_only_untagged_unconfirmed(self, geo_db):
        photos = geo_db.library_photos(no_location=True)
        ids = {p["uuid"] for p in photos}
        assert "geo-filter-u2" in ids
        assert "geo-filter-u3" in ids
        assert "geo-filter-u1" not in ids  # has coords
        assert "geo-filter-u4" not in ids  # confirmed none

    def test_library_photo_count_no_location(self, geo_db):
        assert geo_db.library_photo_count(no_location=True) == 2

    def test_no_location_and_bbox_are_mutually_exclusive(self, geo_db):
        # If no_location=True, bbox is ignored; only untagged photos returned
        photos = geo_db.library_photos(
            no_location=True, lat_min=0.0, lat_max=90.0, lon_min=-180.0, lon_max=180.0
        )
        uuids = {p["uuid"] for p in photos}
        # The geotagged photo should NOT be in results
        assert "geo-filter-u1" not in uuids

    def test_library_photos_returns_latitude_and_geo_confirmed_none(self, geo_db):
        photos = geo_db.library_photos()
        geo_tagged = next(p for p in photos if p["uuid"] == "geo-filter-u1")
        assert geo_tagged["latitude"] is not None
        no_geo = next(p for p in photos if p["uuid"] == "geo-filter-u2")
        assert no_geo["latitude"] is None
        assert "geo_confirmed_none" in no_geo
