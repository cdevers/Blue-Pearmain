"""
tests/test_map_routes.py — integration tests for map view (#140)

Run from repo root:
    python -m pytest tests/test_map_routes.py -v
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

import reviewer.app as app_module
from db.db import Database


def _photo(i: int, **kwargs) -> dict:
    """Base photo payload; override any field via kwargs."""
    base: dict = {
        "uuid": f"map-u{i}",
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
def client_geo():
    """DB with three geotagged photos and one without coordinates."""
    with tempfile.TemporaryDirectory() as tmp:
        test_db = Database(Path(tmp) / "test.db")

        # p1 — full data: photos_title, flickr_id, date
        p1 = test_db.upsert_photo(
            _photo(
                1,
                latitude=48.8566,
                longitude=2.3522,
                photos_title="Paris Street",
                date_taken="2023-10-15T12:00:00",
                flickr_id="flickr-123",
            )
        )
        # p2 — no photos_title (fallback to flickr_title), no flickr_id
        p2 = test_db.upsert_photo(
            _photo(
                2,
                latitude=40.7128,
                longitude=-74.0060,
                flickr_title="NYC Shot",
                date_taken="2022-07-04T10:00:00",
            )
        )
        # p3 — no title at all (fallback to "(untitled)"), no date
        p3 = test_db.upsert_photo(
            _photo(
                3,
                latitude=51.5074,
                longitude=-0.1278,
            )
        )
        # p4 — no coordinates; must be excluded from /api/map-photos
        p4 = test_db.upsert_photo(_photo(4, photos_title="No Location"))

        app_module._db = test_db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test-secret"
        with app_module.app.test_client() as c:
            yield c, p1, p2, p3, p4, test_db
        app_module._db = None


@pytest.fixture()
def client_no_geo():
    """DB with photos that have no coordinates — tests default centre."""
    with tempfile.TemporaryDirectory() as tmp:
        test_db = Database(Path(tmp) / "test.db")
        test_db.upsert_photo(_photo(1, photos_title="No GPS"))
        app_module._db = test_db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test-secret"
        with app_module.app.test_client() as c:
            yield c
        app_module._db = None


# ---------------------------------------------------------------------------
# GET /map
# ---------------------------------------------------------------------------


class TestMapPage:
    def test_returns_200(self, client_geo):
        c, *_ = client_geo
        assert c.get("/map").status_code == 200

    def test_contains_leaflet_init(self, client_geo):
        c, *_ = client_geo
        html = c.get("/map").data.decode()
        assert "setView" in html
        assert "markerClusterGroup" in html or "MarkerClusterGroup" in html

    def test_default_centre_when_no_geotagged_photos(self, client_no_geo):
        html = client_no_geo.get("/map").data.decode()
        assert "20.0" in html  # default center_lat
        assert "0.0" in html  # default center_lon


# ---------------------------------------------------------------------------
# GET /api/map-photos
# ---------------------------------------------------------------------------


class TestMapPhotosApi:
    def test_returns_200_and_list(self, client_geo):
        c, *_ = client_geo
        resp = c.get("/api/map-photos")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, list)

    def test_excludes_photos_without_coordinates(self, client_geo):
        c, p1, p2, p3, p4, _ = client_geo
        data = c.get("/api/map-photos").get_json()
        ids = {item["id"] for item in data}
        assert p1 in ids
        assert p2 in ids
        assert p3 in ids
        assert p4 not in ids  # p4 has no lat/lon

    def test_each_item_has_required_fields(self, client_geo):
        c, *_ = client_geo
        data = c.get("/api/map-photos").get_json()
        for item in data:
            for field in ("id", "lat", "lon", "title", "date", "flickr_url"):
                assert field in item, f"missing field {field!r} in {item}"

    def test_title_uses_photos_title_first(self, client_geo):
        c, p1, *_ = client_geo
        data = c.get("/api/map-photos").get_json()
        p = next(x for x in data if x["id"] == p1)
        assert p["title"] == "Paris Street"

    def test_title_falls_back_to_flickr_title(self, client_geo):
        c, _, p2, *_ = client_geo
        data = c.get("/api/map-photos").get_json()
        p = next(x for x in data if x["id"] == p2)
        assert p["title"] == "NYC Shot"

    def test_title_falls_back_to_untitled(self, client_geo):
        c, _, _, p3, *_ = client_geo
        data = c.get("/api/map-photos").get_json()
        p = next(x for x in data if x["id"] == p3)
        assert p["title"] == "(untitled)"

    def test_date_is_yyyy_mm_dd(self, client_geo):
        c, p1, *_ = client_geo
        data = c.get("/api/map-photos").get_json()
        p = next(x for x in data if x["id"] == p1)
        assert p["date"] == "2023-10-15"

    def test_date_is_empty_string_when_none(self, client_geo):
        c, _, _, p3, *_ = client_geo
        data = c.get("/api/map-photos").get_json()
        p = next(x for x in data if x["id"] == p3)
        assert p["date"] == ""

    def test_flickr_url_null_when_no_flickr_id(self, client_geo):
        c, _, p2, *_ = client_geo
        data = c.get("/api/map-photos").get_json()
        p = next(x for x in data if x["id"] == p2)
        assert p["flickr_url"] is None

    def test_flickr_url_built_when_flickr_id_and_username_set(self, client_geo):
        c, p1, *_ = client_geo
        orig_config = app_module._config
        app_module._config = {"flickr": {"username": "testuser"}}
        try:
            data = c.get("/api/map-photos").get_json()
            p = next(x for x in data if x["id"] == p1)
            assert p["flickr_url"] == "https://www.flickr.com/photos/testuser/flickr-123"
        finally:
            app_module._config = orig_config

    def test_flickr_url_null_when_no_username_configured(self, client_geo):
        c, p1, *_ = client_geo
        orig_config = app_module._config
        app_module._config = {}  # no flickr key
        try:
            data = c.get("/api/map-photos").get_json()
            p = next(x for x in data if x["id"] == p1)
            assert p["flickr_url"] is None
        finally:
            app_module._config = orig_config


@pytest.fixture()
def client_with_deleted():
    """One live geotagged photo and one deleted geotagged photo."""
    with tempfile.TemporaryDirectory() as tmp:
        test_db = Database(Path(tmp) / "test.db")
        live = test_db.upsert_photo(
            _photo(20, latitude=42.38, longitude=-71.10, photos_title="Live photo")
        )
        deleted = test_db.upsert_photo(
            _photo(
                21, latitude=42.39, longitude=-71.09, photos_title="Deleted photo", flickr_deleted=1
            )
        )
        app_module._db = test_db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test-secret"
        with app_module.app.test_client() as c:
            yield c, live, deleted
        app_module._db = None


class TestMapPhotosDeletedFilter:
    def test_deleted_photos_excluded_from_map(self, client_with_deleted):
        c, live, deleted = client_with_deleted
        r = c.get("/api/map-photos")
        assert r.status_code == 200
        data = r.get_json()
        ids = [p["id"] for p in data]
        assert live in ids
        assert deleted not in ids
