# tests/test_geo_library_ui.py
"""Library UI — no-location chip, no-loc pill, bulk action (#145)."""

from __future__ import annotations
import tempfile
from pathlib import Path
import pytest
import reviewer.app as app_module
from db.db import Database


def _photo(i: int, **kwargs) -> dict:
    return {
        "uuid": f"glui-u{i}",
        "original_filename": f"IMG_{i:04d}.JPG",
        "privacy_state": "needs_review",
        "apple_persons": [],
        "apple_labels": [],
        **kwargs,
    }


@pytest.fixture()
def client_lib():
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "t.db")
        db.upsert_photo(_photo(1, latitude=42.3601, longitude=-71.0589))
        db.upsert_photo(_photo(2))
        db.upsert_photo(_photo(3))
        db.upsert_photo(_photo(4, geo_confirmed_none=1))
        app_module._db = db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test"
        with app_module.app.test_client() as c:
            yield c
        app_module._db = None


class TestLibraryGeoUI:
    def test_no_location_chip_present(self, client_lib):
        html = client_lib.get("/library").data.decode()
        assert "No location" in html

    def test_no_location_badge_count_shown(self, client_lib):
        html = client_lib.get("/library").data.decode()
        # 2 ungeotagged + unconfirmed photos
        assert "2" in html

    def test_no_location_filter_active_shows_only_untagged(self, client_lib):
        html = client_lib.get("/library?no_location=1").data.decode()
        # Template renders thumbnails as id="thumb-N"; photos 2 and 3 are untagged/unconfirmed
        assert 'id="thumb-2"' in html or 'id="thumb-3"' in html
        assert "42.3601" not in html  # geotagged photo not shown

    def test_no_loc_pill_on_ungeotagged_thumbnails(self, client_lib):
        html = client_lib.get("/library").data.decode()
        # The pill div is rendered for untagged, unconfirmed photos
        assert '<div class="no-loc-pill">' in html

    def test_no_loc_pill_absent_for_geotagged(self, client_lib):
        html = client_lib.get("/library").data.decode()
        # Only the 2 unconfirmed untagged photos (glui-u2, glui-u3) have the pill div
        assert html.count('<div class="no-loc-pill">') == 2

    def test_no_loc_pill_absent_for_confirmed_none(self, client_lib):
        html = client_lib.get("/library").data.decode()
        # glui-u4 has geo_confirmed_none=1, so no pill for it
        assert html.count('<div class="no-loc-pill">') == 2

    def test_mark_no_location_button_in_action_bar(self, client_lib):
        html = client_lib.get("/library").data.decode()
        assert "geo_confirm_none" in html
        assert "no location" in html.lower()
