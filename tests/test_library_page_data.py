"""Unit tests for db.location_data() and db.person_names()."""

import tempfile
import pytest
from pathlib import Path
from db.db import Database


def _photo(i: int, **kwargs) -> dict:
    base: dict = {
        "uuid": f"lpd-u{i}",
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
def db_lpd():
    """
    Fixture with photos covering location and person edge cases:

    p1  — United States > MA > Springfield (no neighborhood)
    p2  — United States > VT > Springfield (same city, different state)
    p3  — United States > MA > Somerville, neighborhood="Union Square"
    p4  — United States > MA > Boston, neighborhood="Union Square"
           (same neighborhood as p3 but different city)
    p5  — United States > MA > Boston, neighborhood=""
           (empty neighborhood — excluded from neighborhood list)
    p6  — United States > MA > Boston, neighborhood="Back Bay"
    p7  — no place_country (NULL) — excluded from location_data
    p8  — apple_persons=["Alice"]
    p9  — apple_persons=["Bob"]
    p10 — apple_persons=["Alice", "Charlie"]  (Alice appears twice — deduplicated)
    p11 — apple_persons=["_UNKNOWN_"]         (excluded from person_names)
    p12 — apple_persons=[]                    (no persons)
    """
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "test.db")
        db.upsert_photo(
            _photo(1, place_country="United States", place_state="MA", place_city="Springfield")
        )
        db.upsert_photo(
            _photo(2, place_country="United States", place_state="VT", place_city="Springfield")
        )
        db.upsert_photo(
            _photo(
                3,
                place_country="United States",
                place_state="MA",
                place_city="Somerville",
                place_neighborhood="Union Square",
            )
        )
        db.upsert_photo(
            _photo(
                4,
                place_country="United States",
                place_state="MA",
                place_city="Boston",
                place_neighborhood="Union Square",
            )
        )
        db.upsert_photo(
            _photo(
                5,
                place_country="United States",
                place_state="MA",
                place_city="Boston",
                place_neighborhood="",
            )
        )
        db.upsert_photo(
            _photo(
                6,
                place_country="United States",
                place_state="MA",
                place_city="Boston",
                place_neighborhood="Back Bay",
            )
        )
        db.upsert_photo(_photo(7))  # no location
        db.upsert_photo(_photo(8, apple_persons=["Alice"]))
        db.upsert_photo(_photo(9, apple_persons=["Bob"]))
        db.upsert_photo(_photo(10, apple_persons=["Alice", "Charlie"]))
        db.upsert_photo(_photo(11, apple_persons=["_UNKNOWN_"]))
        db.upsert_photo(_photo(12, apple_persons=[]))
        yield db


class TestLocationData:
    def test_returns_nested_dict(self, db_lpd):
        tree = db_lpd.location_data()
        assert isinstance(tree, dict)
        assert "United States" in tree

    def test_null_country_excluded(self, db_lpd):
        tree = db_lpd.location_data()
        for country, states in tree.items():
            assert country  # no empty-string or None key
        assert len(tree) == 1  # only "United States"

    def test_state_level_correct(self, db_lpd):
        states = db_lpd.location_data()["United States"]
        assert "MA" in states
        assert "VT" in states

    def test_same_city_different_states(self, db_lpd):
        tree = db_lpd.location_data()
        assert "Springfield" in tree["United States"]["MA"]
        assert "Springfield" in tree["United States"]["VT"]

    def test_neighborhoods_correct(self, db_lpd):
        tree = db_lpd.location_data()
        boston_nbhds = tree["United States"]["MA"]["Boston"]
        assert "Back Bay" in boston_nbhds
        assert "Union Square" in boston_nbhds

    def test_empty_neighborhood_excluded(self, db_lpd):
        tree = db_lpd.location_data()
        boston_nbhds = tree["United States"]["MA"]["Boston"]
        assert "" not in boston_nbhds

    def test_same_neighborhood_different_cities(self, db_lpd):
        tree = db_lpd.location_data()
        assert "Union Square" in tree["United States"]["MA"]["Somerville"]
        assert "Union Square" in tree["United States"]["MA"]["Boston"]

    def test_neighborhoods_sorted(self, db_lpd):
        tree = db_lpd.location_data()
        boston_nbhds = tree["United States"]["MA"]["Boston"]
        assert boston_nbhds == sorted(boston_nbhds)

    def test_cities_sorted(self, db_lpd):
        tree = db_lpd.location_data()
        cities = list(tree["United States"]["MA"].keys())
        assert cities == sorted(cities)


class TestPersonNames:
    def test_returns_sorted_list(self, db_lpd):
        names = db_lpd.person_names()
        assert names == sorted(names)

    def test_excludes_unknown(self, db_lpd):
        names = db_lpd.person_names()
        assert "_UNKNOWN_" not in names

    def test_no_duplicates(self, db_lpd):
        names = db_lpd.person_names()
        assert len(names) == len(set(names))

    def test_all_three_named_persons_present(self, db_lpd):
        names = db_lpd.person_names()
        assert "Alice" in names
        assert "Bob" in names
        assert "Charlie" in names
        assert len(names) == 3
