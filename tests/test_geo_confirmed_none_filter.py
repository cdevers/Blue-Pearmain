"""Library DB filter and count for geo_confirmed_none=1 photos (#148)."""

from __future__ import annotations
import tempfile
from pathlib import Path
import pytest
from db.db import Database


def _photo(i: int, **kwargs) -> dict:
    return {
        "uuid": f"gcn-u{i}",
        "original_filename": f"IMG_{i:04d}.JPG",
        "privacy_state": "needs_review",
        "apple_persons": [],
        "apple_labels": [],
        **kwargs,
    }


@pytest.fixture()
def gcn_db():
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "t.db")
        db.upsert_photo(_photo(1, latitude=42.3601, longitude=-71.0589))  # geotagged
        db.upsert_photo(_photo(2))  # unreviewed missing
        db.upsert_photo(_photo(3))  # unreviewed missing
        db.upsert_photo(_photo(4, geo_confirmed_none=1))  # confirmed none
        db.upsert_photo(_photo(5, geo_confirmed_none=1))  # confirmed none
        yield db


class TestConfirmedNoneFilter:
    def test_confirmed_none_filter_returns_only_confirmed_none_photos(self, gcn_db):
        photos = gcn_db.library_photos(confirmed_none=True)
        uuids = {p["uuid"] for p in photos}
        assert uuids == {"gcn-u4", "gcn-u5"}

    def test_confirmed_none_filter_excludes_geotagged_and_unreviewed(self, gcn_db):
        photos = gcn_db.library_photos(confirmed_none=True)
        uuids = {p["uuid"] for p in photos}
        assert "gcn-u1" not in uuids  # has coords
        assert "gcn-u2" not in uuids  # unreviewed missing
        assert "gcn-u3" not in uuids  # unreviewed missing

    def test_confirmed_none_count(self, gcn_db):
        assert gcn_db.confirmed_none_count() == 2

    def test_confirmed_none_count_excludes_deleted(self, gcn_db):
        gcn_db.conn.execute("UPDATE photos SET flickr_deleted=1 WHERE uuid='gcn-u4'")
        gcn_db.conn.commit()
        assert gcn_db.confirmed_none_count() == 1

    def test_confirmed_none_and_no_location_mutually_exclusive(self, gcn_db):
        with pytest.raises(ValueError, match="mutually exclusive"):
            gcn_db.library_photos(no_location=True, confirmed_none=True)

    def test_library_photo_count_confirmed_none(self, gcn_db):
        assert gcn_db.library_photo_count(confirmed_none=True) == 2

    def test_library_photo_ids_confirmed_none(self, gcn_db):
        ids = gcn_db.library_photo_ids(confirmed_none=True)
        assert len(ids) == 2
