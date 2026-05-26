"""Integration tests for text search, location, person, and date-alias filters."""

import tempfile
import pytest
import re
from pathlib import Path
from db.db import Database
import reviewer.app as app_module


def _photo(i: int, **kwargs) -> dict:
    base: dict = {
        "uuid": f"ls-u{i}",
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
def client_ls():
    """
    5-photo fixture:

    p1 — photos_title="Sunset over the lake"
         United States > MA > Springfield

    p2 — flickr_description="Birthday at the lake"
         apple_ai_caption="birthday cake on the table"
         United States > VT > Springfield
         (same city name as p1, different state)

    p3 — flickr_tags=["birding", "wildlife"]
         apple_persons=["Alice"]
         United States > MA > Somerville, neighborhood="Union Square"

    p4 — apple_persons=["Alice", "Bob"]
         United States > MA > Boston, neighborhood="Union Square"
         (same neighborhood as p3, different city)
         date_taken="2023-10-15T10:00:00"

    p5 — apple_persons=["_UNKNOWN_"]
         date_taken="2023-10-15T18:00:00"
    """
    with tempfile.TemporaryDirectory() as tmp:
        test_db = Database(Path(tmp) / "test.db")
        p1 = test_db.upsert_photo(
            _photo(
                1,
                photos_title="Sunset over the lake",
                place_country="United States",
                place_state="MA",
                place_city="Springfield",
            )
        )
        p2 = test_db.upsert_photo(
            _photo(
                2,
                flickr_description="Birthday at the lake",
                apple_ai_caption="birthday cake on the table",
                place_country="United States",
                place_state="VT",
                place_city="Springfield",
            )
        )
        p3 = test_db.upsert_photo(
            _photo(
                3,
                flickr_tags=["birding", "wildlife"],
                apple_persons=["Alice"],
                place_country="United States",
                place_state="MA",
                place_city="Somerville",
                place_neighborhood="Union Square",
            )
        )
        p4 = test_db.upsert_photo(
            _photo(
                4,
                apple_persons=["Alice", "Bob"],
                place_country="United States",
                place_state="MA",
                place_city="Boston",
                place_neighborhood="Union Square",
                date_taken="2023-10-15T10:00:00",
            )
        )
        p5 = test_db.upsert_photo(
            _photo(5, apple_persons=["_UNKNOWN_"], date_taken="2023-10-15T18:00:00")
        )

        app_module._db = test_db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test-secret"
        with app_module.app.test_client() as c:
            yield c, p1, p2, p3, p4, p5, test_db
        app_module._db = None


def _ids(resp) -> set[int]:
    return {int(m) for m in re.findall(r'data-id="(\d+)"', resp.data.decode())}


class TestTextSearch:
    def test_photos_title_match(self, client_ls):
        c, p1, p2, p3, p4, p5, _ = client_ls
        r = c.get("/library?q=sunset")
        assert r.status_code == 200
        ids = _ids(r)
        assert p1 in ids
        assert p2 not in ids
        assert p3 not in ids

    def test_flickr_description_match(self, client_ls):
        c, p1, p2, p3, p4, p5, _ = client_ls
        r = c.get("/library?q=birthday")
        ids = _ids(r)
        assert p2 in ids  # flickr_description contains "birthday"
        assert p1 not in ids

    def test_apple_ai_caption_match(self, client_ls):
        c, p1, p2, p3, p4, p5, _ = client_ls
        # "cake" only appears in apple_ai_caption of p2
        r = c.get("/library?q=cake")
        ids = _ids(r)
        assert p2 in ids
        assert p1 not in ids

    def test_flickr_tags_match(self, client_ls):
        c, p1, p2, p3, p4, p5, _ = client_ls
        # "bird" is a substring of the tag "birding"
        r = c.get("/library?q=bird")
        ids = _ids(r)
        assert p3 in ids
        assert p1 not in ids

    def test_no_match_returns_empty(self, client_ls):
        c, *_ = client_ls
        r = c.get("/library?q=xyzzy_no_match")
        assert r.status_code == 200
        assert _ids(r) == set()

    def test_empty_q_returns_all(self, client_ls):
        c, p1, p2, p3, p4, p5, _ = client_ls
        r = c.get("/library?q=")
        ids = _ids(r)
        assert {p1, p2, p3, p4, p5} == ids


class TestLocationFilter:
    def test_disambiguates_springfield_by_state(self, client_ls):
        c, p1, p2, p3, p4, p5, _ = client_ls
        r = c.get("/library?country=United+States&state=MA&city=Springfield")
        ids = _ids(r)
        assert p1 in ids  # Springfield MA
        assert p2 not in ids  # Springfield VT

    def test_vt_springfield(self, client_ls):
        c, p1, p2, p3, p4, p5, _ = client_ls
        r = c.get("/library?country=United+States&state=VT&city=Springfield")
        ids = _ids(r)
        assert p2 in ids
        assert p1 not in ids

    def test_disambiguates_union_square_by_city(self, client_ls):
        c, p1, p2, p3, p4, p5, _ = client_ls
        r = c.get(
            "/library?country=United+States&state=MA&city=Somerville&neighborhood=Union+Square"
        )
        ids = _ids(r)
        assert p3 in ids  # Somerville Union Square
        assert p4 not in ids  # Boston Union Square

    def test_neighborhood_without_city_returns_both(self, client_ls):
        c, p1, p2, p3, p4, p5, _ = client_ls
        r = c.get("/library?neighborhood=Union+Square")
        ids = _ids(r)
        assert p3 in ids
        assert p4 in ids

    def test_country_only(self, client_ls):
        c, p1, p2, p3, p4, p5, _ = client_ls
        r = c.get("/library?country=United+States")
        ids = _ids(r)
        assert {p1, p2, p3, p4} <= ids  # all geotagged photos
        assert p5 not in ids

    def test_unknown_country_returns_nothing(self, client_ls):
        c, *_ = client_ls
        r = c.get("/library?country=Freedonia")
        assert r.status_code == 200
        assert _ids(r) == set()


class TestPersonFilter:
    def test_alice(self, client_ls):
        c, p1, p2, p3, p4, p5, _ = client_ls
        r = c.get("/library?person=Alice")
        ids = _ids(r)
        assert p3 in ids  # ["Alice"]
        assert p4 in ids  # ["Alice", "Bob"]
        assert p1 not in ids
        assert p5 not in ids

    def test_bob(self, client_ls):
        c, p1, p2, p3, p4, p5, _ = client_ls
        r = c.get("/library?person=Bob")
        ids = _ids(r)
        assert p4 in ids  # only Bob
        assert p3 not in ids

    def test_unknown_person_marker(self, client_ls):
        c, p1, p2, p3, p4, p5, _ = client_ls
        r = c.get("/library?person=_UNKNOWN_")
        ids = _ids(r)
        assert p5 in ids
        assert p3 not in ids

    def test_no_match_person(self, client_ls):
        c, *_ = client_ls
        r = c.get("/library?person=Nobody")
        assert _ids(r) == set()


class TestDateAlias:
    def test_date_returns_photos_from_that_day(self, client_ls):
        c, p1, p2, p3, p4, p5, _ = client_ls
        r = c.get("/library?date=2023-10-15")
        ids = _ids(r)
        assert p4 in ids  # 2023-10-15T10:00:00
        assert p5 in ids  # 2023-10-15T18:00:00
        assert p1 not in ids
        assert p2 not in ids
        assert p3 not in ids

    def test_date_alias_does_not_crash_without_other_filters(self, client_ls):
        c, *_ = client_ls
        r = c.get("/library?date=2023-01-01")
        assert r.status_code == 200


class TestCombinedFilters:
    def test_q_and_country_combined(self, client_ls):
        c, p1, p2, p3, p4, p5, _ = client_ls
        r = c.get("/library?q=sunset&country=United+States&state=MA")
        ids = _ids(r)
        assert p1 in ids
        assert p2 not in ids

    def test_person_and_city_combined(self, client_ls):
        c, p1, p2, p3, p4, p5, _ = client_ls
        r = c.get("/library?person=Alice&city=Boston")
        ids = _ids(r)
        assert p4 in ids
        assert p3 not in ids  # Alice but Somerville

    def test_empty_params_return_all(self, client_ls):
        c, p1, p2, p3, p4, p5, _ = client_ls
        r = c.get("/library?q=&country=&person=")
        assert r.status_code == 200
        ids = _ids(r)
        assert len(ids) == 5
