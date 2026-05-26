"""Integration tests for time_pattern filter in the library route."""

import tempfile
import pytest
from pathlib import Path
from db.db import Database
import reviewer.app as app_module


def _photo(i: int, **kwargs) -> dict:
    base: dict = {
        "uuid": f"tp-u{i}",
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
def client_tp():
    """
    Fixture with 8 photos covering different months, seasons, weekdays/weekends, holidays.
    All dates verified against Python calendar for 2023:

    Photo 1 — Oct 16 2023 (Monday, fall)
    Photo 2 — Mar 20 2023 (Monday, spring AND winter overlap)
    Photo 3 — Jul  4 2023 (Tuesday, summer)
    Photo 4 — Sep 16 2023 (Saturday, fall/summer overlap)
    Photo 5 — Nov 23 2023 (Thursday, Thanksgiving 2023, fall)
    Photo 6 — Nov 25 2023 (Saturday, within ±2 of Thanksgiving, fall)
    Photo 7 — Nov 20 2023 (Monday, outside ±2 of Thanksgiving, fall)
    Photo 8 — Dec 25 2023 (Monday, Christmas, winter/fall overlap)
    """
    with tempfile.TemporaryDirectory() as tmp:
        test_db = Database(Path(tmp) / "test.db")
        p = [
            test_db.upsert_photo(_photo(1, date_taken="2023-10-16T12:00:00")),
            test_db.upsert_photo(_photo(2, date_taken="2023-03-20T12:00:00")),
            test_db.upsert_photo(_photo(3, date_taken="2023-07-04T12:00:00")),
            test_db.upsert_photo(_photo(4, date_taken="2023-09-16T12:00:00")),
            test_db.upsert_photo(_photo(5, date_taken="2023-11-23T12:00:00")),
            test_db.upsert_photo(_photo(6, date_taken="2023-11-25T12:00:00")),
            test_db.upsert_photo(_photo(7, date_taken="2023-11-20T12:00:00")),
            test_db.upsert_photo(_photo(8, date_taken="2023-12-25T12:00:00")),
        ]
        app_module._db = test_db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test-secret"
        with app_module.app.test_client() as c:
            yield c, p, test_db
        app_module._db = None


def _ids(resp) -> set[int]:
    """Extract photo IDs from library HTML response."""
    import re

    return {int(m) for m in re.findall(r'data-id="(\d+)"', resp.data.decode())}


class TestMonthFilter:
    def test_october_only(self, client_tp):
        c, p, _ = client_tp
        r = c.get("/library?time_pattern=month:10")
        assert r.status_code == 200
        ids = _ids(r)
        assert p[0] in ids  # Oct 16
        assert p[1] not in ids  # Mar
        assert p[2] not in ids  # Jul

    def test_november_includes_all_november_photos(self, client_tp):
        c, p, _ = client_tp
        r = c.get("/library?time_pattern=month:11")
        ids = _ids(r)
        assert {p[4], p[5], p[6]} <= ids  # Nov 23, Nov 25, Nov 20
        assert p[0] not in ids  # Oct


class TestSeasonFilter:
    def test_fall_includes_sep_oct_nov_dec(self, client_tp):
        c, p, _ = client_tp
        r = c.get("/library?time_pattern=season:fall")
        ids = _ids(r)
        # Sep(4), Oct(1), Nov(5,6,7), Dec(8) all in fall
        assert {p[0], p[3], p[4], p[5], p[6], p[7]} <= ids
        assert p[1] not in ids  # Mar — not in fall
        assert p[2] not in ids  # Jul — not in fall

    def test_spring_includes_march(self, client_tp):
        c, p, _ = client_tp
        r = c.get("/library?time_pattern=season:spring")
        ids = _ids(r)
        assert p[1] in ids  # Mar 20 ∈ spring (Mar–Jun)
        assert p[7] not in ids  # Dec — not in spring

    def test_winter_includes_march_overlap(self, client_tp):
        c, p, _ = client_tp
        r = c.get("/library?time_pattern=season:winter")
        ids = _ids(r)
        assert p[1] in ids  # Mar 20 ∈ winter (Dec–Mar) — intentional overlap
        assert p[7] in ids  # Dec 25 ∈ winter

    def test_summer(self, client_tp):
        c, p, _ = client_tp
        r = c.get("/library?time_pattern=season:summer")
        ids = _ids(r)
        assert p[2] in ids  # Jul 4 ∈ summer
        assert p[3] in ids  # Sep 16 ∈ summer (Jun–Sep includes Sep)
        assert p[0] not in ids  # Oct — not in summer


class TestDayTypeFilter:
    def test_weekends(self, client_tp):
        c, p, _ = client_tp
        r = c.get("/library?time_pattern=daytype:weekend")
        ids = _ids(r)
        assert p[3] in ids  # Sep 16 = Saturday
        assert p[5] in ids  # Nov 25 = Saturday
        assert p[0] not in ids  # Oct 16 = Monday

    def test_weekdays(self, client_tp):
        c, p, _ = client_tp
        r = c.get("/library?time_pattern=daytype:weekday")
        ids = _ids(r)
        assert p[0] in ids  # Oct 16 = Monday
        assert p[4] in ids  # Nov 23 = Thursday (Thanksgiving)
        assert p[3] not in ids  # Sep 16 = Saturday


class TestHolidayFilter:
    def test_thanksgiving_exact(self, client_tp):
        c, p, _ = client_tp
        r = c.get("/library?time_pattern=holiday:thanksgiving")
        ids = _ids(r)
        assert p[4] in ids  # Nov 23 = Thanksgiving 2023
        assert p[5] not in ids  # Nov 25 = 2 days after, not included without expand
        assert p[6] not in ids  # Nov 20 = 3 days before

    def test_thanksgiving_expand(self, client_tp):
        c, p, _ = client_tp
        r = c.get("/library?time_pattern=holiday:thanksgiving&expand=1")
        ids = _ids(r)
        assert p[4] in ids  # Nov 23 = Thanksgiving
        assert p[5] in ids  # Nov 25 = within ±2 days (Nov 21–25)
        assert p[6] not in ids  # Nov 20 = 3 days before = outside window

    def test_christmas_exact(self, client_tp):
        c, p, _ = client_tp
        r = c.get("/library?time_pattern=holiday:christmas")
        ids = _ids(r)
        assert p[7] in ids  # Dec 25
        assert p[4] not in ids  # Nov 23


class TestEdgeCases:
    def test_unknown_pattern_returns_all(self, client_tp):
        c, p, _ = client_tp
        r = c.get("/library?time_pattern=unknown:xyz")
        assert r.status_code == 200
        ids = _ids(r)
        assert len(ids) == 8  # all photos returned

    def test_empty_pattern_returns_all(self, client_tp):
        c, p, _ = client_tp
        r = c.get("/library?time_pattern=")
        assert r.status_code == 200
        ids = _ids(r)
        assert len(ids) == 8

    def test_time_pattern_and_combined_with_other_filters(self, client_tp):
        c, p, _ = client_tp
        # Fall AND untitled_only — all photos are untitled in fixture, so same as fall
        r = c.get("/library?time_pattern=season:fall&untitled=1")
        assert r.status_code == 200
        ids = _ids(r)
        assert p[0] in ids  # Oct 16 ∈ fall
        assert p[2] not in ids  # Jul — not in fall
