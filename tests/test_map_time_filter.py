"""Integration tests for time_pattern filter on GET /api/map-photos."""

import tempfile
import pytest
from pathlib import Path
from db.db import Database
import reviewer.app as app_module


def _photo(i: int, **kwargs) -> dict:
    base: dict = {
        "uuid": f"mtp-u{i}",
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
def client_mtp():
    """
    Fixture with 3 photos:
      p_oct — geotagged, October (month 10, fall)
      p_jul — geotagged, July (month 07, summer)
      p_none — no location (never appears in map results)
    """
    with tempfile.TemporaryDirectory() as tmp:
        test_db = Database(Path(tmp) / "test.db")
        p_oct = test_db.upsert_photo(
            _photo(10, latitude=48.8566, longitude=2.3522, date_taken="2023-10-16T12:00:00")
        )
        p_jul = test_db.upsert_photo(
            _photo(11, latitude=40.7128, longitude=-74.0060, date_taken="2023-07-04T12:00:00")
        )
        p_none = test_db.upsert_photo(
            _photo(12, date_taken="2023-10-16T12:00:00")  # no lat/lon
        )
        app_module._db = test_db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test-secret"
        with app_module.app.test_client() as c:
            yield c, p_oct, p_jul, p_none, test_db
        app_module._db = None


def _ids(resp) -> set[int]:
    return {item["id"] for item in resp.get_json()}


class TestMapTimeFilter:
    def test_no_filter_returns_all_geotagged(self, client_mtp):
        c, p_oct, p_jul, p_none, _ = client_mtp
        r = c.get("/api/map-photos")
        assert r.status_code == 200
        ids = _ids(r)
        assert p_oct in ids
        assert p_jul in ids
        assert p_none not in ids  # no location

    def test_month_filter(self, client_mtp):
        c, p_oct, p_jul, p_none, _ = client_mtp
        r = c.get("/api/map-photos?time_pattern=month:10")
        assert r.status_code == 200
        ids = _ids(r)
        assert p_oct in ids
        assert p_jul not in ids
        assert p_none not in ids

    def test_season_summer(self, client_mtp):
        c, p_oct, p_jul, p_none, _ = client_mtp
        r = c.get("/api/map-photos?time_pattern=season:summer")
        ids = _ids(r)
        assert p_jul in ids  # July ∈ summer
        assert p_oct not in ids  # October not in summer

    def test_daytype_weekend(self, client_mtp):
        c, p_oct, p_jul, p_none, _ = client_mtp
        # Oct 16 = Monday, Jul 4 = Tuesday — neither is weekend
        r = c.get("/api/map-photos?time_pattern=daytype:weekend")
        ids = _ids(r)
        assert p_oct not in ids
        assert p_jul not in ids

    def test_daytype_weekday(self, client_mtp):
        c, p_oct, p_jul, p_none, _ = client_mtp
        r = c.get("/api/map-photos?time_pattern=daytype:weekday")
        ids = _ids(r)
        assert p_oct in ids
        assert p_jul in ids

    def test_holiday_thanksgiving_not_in_fixture(self, client_mtp):
        c, p_oct, p_jul, p_none, _ = client_mtp
        # Neither photo is near Thanksgiving (Nov 21–25 2023)
        r = c.get("/api/map-photos?time_pattern=holiday:thanksgiving&expand=1")
        assert r.status_code == 200
        assert r.get_json() == []

    def test_unknown_pattern_returns_all_geotagged(self, client_mtp):
        c, p_oct, p_jul, p_none, _ = client_mtp
        r = c.get("/api/map-photos?time_pattern=unknown:xyz")
        assert r.status_code == 200
        ids = _ids(r)
        assert p_oct in ids
        assert p_jul in ids
        assert p_none not in ids

    def test_json_structure_unchanged(self, client_mtp):
        c, p_oct, p_jul, p_none, _ = client_mtp
        r = c.get("/api/map-photos?time_pattern=month:10")
        data = r.get_json()
        assert len(data) == 1
        item = data[0]
        assert set(item.keys()) >= {"id", "lat", "lon", "title", "date", "flickr_url"}
