"""
tests/test_map_filter.py — map filter: year range, album, person, privacy (#154)

Run from repo root:
    python -m pytest tests/test_map_filter.py -v
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

import reviewer.app as app_module
from db.db import Database


def _photo(i: int, **kwargs) -> dict:
    base: dict = {
        "uuid": f"mf-u{i}",
        "original_filename": f"IMG_{i:04d}.JPG",
        "privacy_state": "needs_review",
        "apple_persons": [],
        "apple_labels": [],
        "apple_unknown_faces": 0,
        "apple_named_faces": 0,
    }
    base.update(kwargs)
    return base


def _ids(resp) -> set[int]:
    return {p["id"] for p in resp.get_json()}


# ── Year range ─────────────────────────────────────────────────────────────


@pytest.fixture()
def client_years():
    """DB with photos in 2016, 2019, and 2023 — all geotagged."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "test.db")
        p16 = db.upsert_photo(
            _photo(
                1,
                latitude=48.8,
                longitude=2.3,
                date_taken="2016-08-15T10:00:00",
                privacy_state="approved_public",
            )
        )
        p19 = db.upsert_photo(
            _photo(
                2,
                latitude=40.7,
                longitude=-74.0,
                date_taken="2019-12-20T10:00:00",
                privacy_state="needs_review",
            )
        )
        p23 = db.upsert_photo(
            _photo(
                3,
                latitude=51.5,
                longitude=-0.1,
                date_taken="2023-07-04T10:00:00",
                privacy_state="keep_private",
            )
        )
        app_module._db = db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test"
        with app_module.app.test_client() as c:
            yield c, p16, p19, p23, db
        app_module._db = None


class TestYearRangeFilter:
    def test_year_from_excludes_earlier(self, client_years):
        c, p16, p19, p23, _ = client_years
        resp = c.get("/api/map-photos?year_from=2019")
        assert resp.status_code == 200
        ids = _ids(resp)
        assert p16 not in ids
        assert p19 in ids
        assert p23 in ids

    def test_year_to_excludes_later(self, client_years):
        c, p16, p19, p23, _ = client_years
        resp = c.get("/api/map-photos?year_to=2019")
        assert resp.status_code == 200
        ids = _ids(resp)
        assert p16 in ids
        assert p19 in ids
        assert p23 not in ids

    def test_year_range_both_bounds(self, client_years):
        c, p16, p19, p23, _ = client_years
        resp = c.get("/api/map-photos?year_from=2019&year_to=2019")
        assert resp.status_code == 200
        ids = _ids(resp)
        assert p16 not in ids
        assert p19 in ids
        assert p23 not in ids

    def test_year_from_greater_than_to_is_swapped(self, client_years):
        c, p16, p19, p23, _ = client_years
        # year_from=2023, year_to=2016 should silently swap to 2016–2023
        resp = c.get("/api/map-photos?year_from=2023&year_to=2016")
        assert resp.status_code == 200
        ids = _ids(resp)
        assert p16 in ids
        assert p19 in ids
        assert p23 in ids

    def test_response_ordered_by_date(self, client_years):
        c, p16, p19, p23, _ = client_years
        resp = c.get("/api/map-photos")
        photos = resp.get_json()
        dates = [p["date"] for p in photos if p["date"]]
        assert dates == sorted(dates), "API must return photos in date_taken order"

    def test_null_date_photos_present_as_dots(self, client_years):
        # Photos with NULL date_taken must appear in the response (valid map dots)
        c, p16, p19, p23, db = client_years
        p_nodate = db.upsert_photo(_photo(99, latitude=35.7, longitude=139.7))
        resp = c.get("/api/map-photos")
        ids = _ids(resp)
        assert p_nodate in ids, "NULL date_taken photo must appear as a map dot"

    def test_non_numeric_year_ignored(self, client_years):
        c, p16, p19, p23, _ = client_years
        resp = c.get("/api/map-photos?year_from=abc&year_to=xyz")
        assert resp.status_code == 200
        assert len(resp.get_json()) == 3  # no filter applied

    def test_out_of_range_year_ignored(self, client_years):
        c, p16, p19, p23, _ = client_years
        resp = c.get("/api/map-photos?year_from=1700&year_to=3000")
        assert resp.status_code == 200
        assert len(resp.get_json()) == 3  # no filter

    def test_privacy_state_in_response(self, client_years):
        c, *_ = client_years
        resp = c.get("/api/map-photos")
        assert resp.status_code == 200
        photos = resp.get_json()
        assert len(photos) > 0
        for p in photos:
            assert "privacy_state" in p, f"Missing privacy_state in {p}"


# ── Album filter ───────────────────────────────────────────────────────────


@pytest.fixture()
def client_albums():
    """DB with two photos in album A, one in album B, one in neither."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "test.db")
        p1 = db.upsert_photo(
            _photo(10, latitude=48.8, longitude=2.3, date_taken="2018-06-01T10:00:00")
        )
        p2 = db.upsert_photo(
            _photo(11, latitude=40.7, longitude=-74.0, date_taken="2018-06-05T10:00:00")
        )
        p3 = db.upsert_photo(
            _photo(12, latitude=51.5, longitude=-0.1, date_taken="2020-03-01T10:00:00")
        )
        p4 = db.upsert_photo(
            _photo(13, latitude=35.7, longitude=139.7, date_taken="2021-01-01T10:00:00")
        )  # no album

        album_a = db.upsert_album("uuid-a", "Spain 2018")
        album_b = db.upsert_album("uuid-b", "UK 2020")
        db.upsert_photo_album(p1, album_a)
        db.upsert_photo_album(p2, album_a)
        db.upsert_photo_album(p3, album_b)

        app_module._db = db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test"
        with app_module.app.test_client() as c:
            yield c, p1, p2, p3, p4, album_a, album_b, db
        app_module._db = None


class TestAlbumFilter:
    def test_album_filter_returns_only_member_photos(self, client_albums):
        c, p1, p2, p3, p4, album_a, album_b, _ = client_albums
        resp = c.get(f"/api/map-photos?album_id={album_a}")
        assert resp.status_code == 200
        ids = _ids(resp)
        assert ids == {p1, p2}

    def test_album_filter_respects_removed_at(self, client_albums):
        c, p1, p2, p3, p4, album_a, album_b, db = client_albums
        # Tombstone p2 from album_a
        db.conn.execute(
            "UPDATE photo_albums SET removed_at = '2024-01-01T00:00:00' "
            "WHERE photo_id = ? AND album_id = ?",
            (p2, album_a),
        )
        db.conn.commit()
        resp = c.get(f"/api/map-photos?album_id={album_a}")
        ids = _ids(resp)
        assert p2 not in ids
        assert p1 in ids

    def test_album_filter_different_album(self, client_albums):
        c, p1, p2, p3, p4, album_a, album_b, _ = client_albums
        resp = c.get(f"/api/map-photos?album_id={album_b}")
        ids = _ids(resp)
        assert ids == {p3}

    def test_invalid_album_id_ignored(self, client_albums):
        c, p1, p2, p3, p4, *_ = client_albums
        resp = c.get("/api/map-photos?album_id=notanumber")
        assert resp.status_code == 200
        assert len(resp.get_json()) == 4  # all photos returned


# ── Person filter ──────────────────────────────────────────────────────────


@pytest.fixture()
def client_persons():
    """DB with photos tagged with different people."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "test.db")
        p1 = db.upsert_photo(
            _photo(
                20,
                latitude=48.8,
                longitude=2.3,
                date_taken="2014-11-01T10:00:00",
                apple_persons=["Marcin Sulikowski", "Chris Devers"],
            )
        )
        p2 = db.upsert_photo(
            _photo(
                21,
                latitude=21.0,
                longitude=105.8,
                date_taken="2016-05-15T10:00:00",
                apple_persons=["Marcin Sulikowski", "_UNKNOWN_"],
            )
        )
        p3 = db.upsert_photo(
            _photo(
                22,
                latitude=51.5,
                longitude=-0.1,
                date_taken="2018-09-20T10:00:00",
                apple_persons=["Chris Devers"],
            )
        )
        p4 = db.upsert_photo(
            _photo(
                23,
                latitude=40.7,
                longitude=-74.0,
                date_taken="2022-03-01T10:00:00",
                apple_persons=["_UNKNOWN_"],
            )
        )

        app_module._db = db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test"
        with app_module.app.test_client() as c:
            yield c, p1, p2, p3, p4, db
        app_module._db = None


class TestPersonFilter:
    def test_person_filter_returns_matching_photos(self, client_persons):
        c, p1, p2, p3, p4, _ = client_persons
        resp = c.get("/api/map-photos?person=Marcin+Sulikowski")
        assert resp.status_code == 200
        ids = _ids(resp)
        assert ids == {p1, p2}

    def test_person_filter_case_insensitive(self, client_persons):
        c, p1, p2, p3, p4, _ = client_persons
        resp = c.get("/api/map-photos?person=marcin+sulikowski")
        ids = _ids(resp)
        assert ids == {p1, p2}

    def test_person_filter_unknown_string_matches_unknown_entries(self, client_persons):
        # Searching for "_UNKNOWN_" finds photos with _UNKNOWN_ entries
        c, p1, p2, p3, p4, _ = client_persons
        resp = c.get("/api/map-photos?person=_UNKNOWN_")
        ids = _ids(resp)
        assert p4 in ids  # has _UNKNOWN_
        assert p3 not in ids  # Chris only, no _UNKNOWN_

    def test_person_filter_blank_returns_all(self, client_persons):
        c, p1, p2, p3, p4, _ = client_persons
        resp = c.get("/api/map-photos?person=")
        ids = _ids(resp)
        assert ids == {p1, p2, p3, p4}

    def test_combined_year_and_person(self, client_persons):
        c, p1, p2, p3, p4, _ = client_persons
        # Marcin + year_from=2016 + year_to=2016 → only p2 (2016-05-15)
        resp = c.get("/api/map-photos?person=Marcin+Sulikowski&year_from=2016&year_to=2016")
        ids = _ids(resp)
        assert ids == {p2}


# ── Template vars ──────────────────────────────────────────────────────────


@pytest.fixture()
def client_template_vars():
    """DB with one album and named persons."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "test.db")
        db.upsert_photo(_photo(30, apple_persons=["Alice Wonderland", "_UNKNOWN_"]))
        db.upsert_photo(_photo(31, apple_persons=["Bob Builder"]))
        db.upsert_album("uuid-tv1", "Japan 2019")
        db.upsert_album("uuid-tv2", "Scotland 2022")

        app_module._db = db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test"
        with app_module.app.test_client() as c:
            yield c, db
        app_module._db = None


class TestMapViewTemplateVars:
    def test_albums_passed_to_template(self, client_template_vars):
        c, _ = client_template_vars
        resp = c.get("/map")
        assert resp.status_code == 200
        body = resp.data.decode()
        assert "Japan 2019" in body
        assert "Scotland 2022" in body

    def test_person_names_passed_to_template(self, client_template_vars):
        c, _ = client_template_vars
        resp = c.get("/map")
        assert resp.status_code == 200
        body = resp.data.decode()
        assert "Alice Wonderland" in body
        assert "Bob Builder" in body

    def test_unknown_excluded_from_person_names(self, client_template_vars):
        c, _ = client_template_vars
        resp = c.get("/map")
        body = resp.data.decode()
        assert "_UNKNOWN_" not in body
