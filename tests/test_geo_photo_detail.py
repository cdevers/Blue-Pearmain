# tests/test_geo_photo_detail.py
"""Photo detail page — 3-state geo section (#145)."""

from __future__ import annotations
import tempfile
from pathlib import Path
import pytest
import reviewer.app as app_module
from db.db import Database


def _photo(i: int, **kwargs) -> dict:
    return {
        "uuid": f"gpd-u{i}",
        "flickr_id": f"gpd-f{i}",
        "original_filename": f"IMG_{i:04d}.JPG",
        "privacy_state": "needs_review",
        "apple_persons": [],
        "apple_labels": [],
        **kwargs,
    }


@pytest.fixture()
def client_geo_detail():
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "t.db")
        geo_id = db.upsert_photo(_photo(1, latitude=42.3601, longitude=-71.0589))
        no_geo_id = db.upsert_photo(_photo(2))
        confirmed_id = db.upsert_photo(_photo(3, geo_confirmed_none=1))
        app_module._db = db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test"
        app_module._config = {"flickr": {"username": "testuser"}}
        with app_module.app.test_client() as c:
            yield c, geo_id, no_geo_id, confirmed_id
        app_module._db = None
        app_module._config = {}


class TestPhotoDetailGeoSection:
    def test_geotagged_shows_formatted_coords(self, client_geo_detail):
        c, geo_id, _, _ = client_geo_detail
        html = c.get(f"/photo/{geo_id}").data.decode()
        assert "42.3601" in html
        assert "71.0589" in html

    def test_geotagged_shows_view_on_map_link(self, client_geo_detail):
        c, geo_id, _, _ = client_geo_detail
        html = c.get(f"/photo/{geo_id}").data.decode()
        assert f"/map?photo_id={geo_id}" in html

    def test_geotagged_shows_flickr_edit_link(self, client_geo_detail):
        c, geo_id, _, _ = client_geo_detail
        html = c.get(f"/photo/{geo_id}").data.decode()
        assert "flickr.com/photos/testuser/gpd-f1/edit/" in html

    def test_geotagged_shows_edit_in_photos_link(self, client_geo_detail):
        c, geo_id, _, _ = client_geo_detail
        html = c.get(f"/photo/{geo_id}").data.decode()
        assert "photos://uuid/gpd-u1" in html

    def test_no_geo_shows_no_location_label(self, client_geo_detail):
        c, _, no_geo_id, _ = client_geo_detail
        html = c.get(f"/photo/{no_geo_id}").data.decode()
        assert "No location" in html

    def test_no_geo_shows_mark_as_correct_link(self, client_geo_detail):
        c, _, no_geo_id, _ = client_geo_detail
        html = c.get(f"/photo/{no_geo_id}").data.decode()
        assert "geo_confirm_none" in html
        assert "Mark as correct" in html

    def test_confirmed_none_shows_confirmed_label(self, client_geo_detail):
        c, _, _, confirmed_id = client_geo_detail
        html = c.get(f"/photo/{confirmed_id}").data.decode()
        assert "No location (confirmed)" in html

    def test_confirmed_none_shows_undo_link(self, client_geo_detail):
        c, _, _, confirmed_id = client_geo_detail
        html = c.get(f"/photo/{confirmed_id}").data.decode()
        assert "Undo" in html or "clear" in html

    def test_confirmed_none_still_shows_edit_links(self, client_geo_detail):
        c, _, _, confirmed_id = client_geo_detail
        html = c.get(f"/photo/{confirmed_id}").data.decode()
        assert "flickr.com/photos/testuser/gpd-f3/edit/" in html
