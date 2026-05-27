"""
tests/test_photo_minimap.py — photo detail page mini-map rendering (#146)

Run from repo root:
    python -m pytest tests/test_photo_minimap.py -v
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

import reviewer.app as app_module
from db.db import Database


def _photo(i: int, **kwargs) -> dict:
    base: dict = {
        "uuid": f"minimap-u{i}",
        "original_filename": f"IMG_{i:04d}.JPG",
        "privacy_state": "needs_review",
        "apple_persons": [],
        "apple_labels": [],
        "apple_unknown_faces": 0,
        "apple_named_faces": 0,
    }
    base.update(kwargs)
    return base


@pytest.fixture()
def client_detail():
    """DB with one geotagged photo (Boston) and one ungeotagged photo."""
    with tempfile.TemporaryDirectory() as tmp:
        test_db = Database(Path(tmp) / "test.db")
        geo_id = test_db.upsert_photo(
            _photo(
                1,
                latitude=42.3601,
                longitude=-71.0589,
                photos_title="Fenway Park",
            )
        )
        no_geo_id = test_db.upsert_photo(_photo(2, photos_title="Screenshot"))
        app_module._db = test_db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test-secret"
        with app_module.app.test_client() as c:
            yield c, geo_id, no_geo_id
        app_module._db = None


class TestPhotoDetailMinimap:
    def test_minimap_div_present_for_geotagged_photo(self, client_detail):
        c, geo_id, _ = client_detail
        html = c.get(f"/photo/{geo_id}").data.decode()
        assert 'id="mini-map"' in html

    def test_leaflet_css_loaded_for_geotagged_photo(self, client_detail):
        c, geo_id, _ = client_detail
        html = c.get(f"/photo/{geo_id}").data.decode()
        assert "leaflet@1.9.4/dist/leaflet.css" in html

    def test_leaflet_js_loaded_for_geotagged_photo(self, client_detail):
        c, geo_id, _ = client_detail
        html = c.get(f"/photo/{geo_id}").data.decode()
        assert "leaflet@1.9.4/dist/leaflet.js" in html

    def test_coordinates_displayed_for_geotagged_photo(self, client_detail):
        c, geo_id, _ = client_detail
        html = c.get(f"/photo/{geo_id}").data.decode()
        assert "42.3601" in html  # latitude
        assert "71.0589" in html  # longitude abs value

    def test_view_full_map_link_uses_photo_id_param(self, client_detail):
        c, geo_id, _ = client_detail
        html = c.get(f"/photo/{geo_id}").data.decode()
        assert f"/map?photo_id={geo_id}" in html

    def test_minimap_absent_for_ungeotagged_photo(self, client_detail):
        c, _, no_geo_id = client_detail
        html = c.get(f"/photo/{no_geo_id}").data.decode()
        assert 'id="mini-map"' not in html

    def test_leaflet_not_loaded_for_ungeotagged_photo(self, client_detail):
        c, _, no_geo_id = client_detail
        html = c.get(f"/photo/{no_geo_id}").data.decode()
        assert "leaflet@1.9.4/dist/leaflet.css" not in html
