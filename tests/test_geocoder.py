"""Tests for poller/geocoder.py — Nominatim reverse geocoding (#217).

All tests use an injectable fetcher — no real HTTP calls are made.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "poller"))

from db.db import Database
from geocoder import (
    PlaceData,
    _parse_nominatim_response,
    reverse_geocode,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _db(tmp_path: Path) -> Database:
    return Database(str(tmp_path / "curator.db"))


def _place(**kwargs) -> PlaceData:
    defaults = dict(
        city=None, state=None, country=None, country_code=None, neighborhood=None, address=None
    )
    defaults.update(kwargs)
    return PlaceData(**defaults)


# ---------------------------------------------------------------------------
# _parse_nominatim_response
# ---------------------------------------------------------------------------


class TestParseNominatimResponse:
    def test_parse_nominatim_response_full(self):
        data = {
            "display_name": "14 High Street, Somerville, Massachusetts, United States",
            "address": {
                "neighbourhood": "Winter Hill",
                "city": "Somerville",
                "state": "Massachusetts",
                "country": "United States",
                "country_code": "us",
            },
        }
        result = _parse_nominatim_response(data)
        assert result.neighborhood == "Winter Hill"
        assert result.city == "Somerville"
        assert result.state == "Massachusetts"
        assert result.country == "United States"
        assert result.country_code == "us"
        assert result.address == "14 High Street, Somerville, Massachusetts, United States"

    def test_parse_nominatim_response_town_fallback(self):
        # No 'city' key — should fall back to 'town', then 'village'
        data = {
            "display_name": "Some Town, MA, US",
            "address": {
                "town": "Acton",
                "state": "Massachusetts",
                "country": "United States",
                "country_code": "us",
            },
        }
        result = _parse_nominatim_response(data)
        assert result.city == "Acton"

    def test_parse_nominatim_response_village_fallback(self):
        # No 'city' or 'town' — fall back to 'village'
        data = {
            "display_name": "Someplace, rural",
            "address": {
                "village": "Podunk",
                "state": "Maine",
                "country": "United States",
                "country_code": "us",
            },
        }
        result = _parse_nominatim_response(data)
        assert result.city == "Podunk"

    def test_parse_nominatim_response_suburb_fallback(self):
        # No 'neighbourhood' key — fall back to 'suburb'
        data = {
            "display_name": "Some area",
            "address": {
                "suburb": "Davis Square",
                "city": "Somerville",
                "state": "Massachusetts",
                "country": "United States",
                "country_code": "us",
            },
        }
        result = _parse_nominatim_response(data)
        assert result.neighborhood == "Davis Square"
        assert result.city == "Somerville"

    def test_parse_nominatim_response_missing_fields(self):
        # Sparse response — only country present
        data = {
            "display_name": "Somewhere",
            "address": {
                "country": "France",
                "country_code": "fr",
            },
        }
        result = _parse_nominatim_response(data)
        assert result.country == "France"
        assert result.country_code == "fr"
        assert result.city is None
        assert result.state is None
        assert result.neighborhood is None


# ---------------------------------------------------------------------------
# reverse_geocode — cache logic
# ---------------------------------------------------------------------------


class TestReverseGeocode:
    def test_reverse_geocode_cache_hit(self, tmp_path: Path):
        db = _db(tmp_path)
        db.set_nominatim_cache(
            42.361,
            -71.057,
            {
                "place_city": "Somerville",
                "place_state": "Massachusetts",
                "place_country": "United States",
                "place_country_code": "us",
                "place_neighborhood": "Winter Hill",
                "place_address": "Somerville, MA, US",
            },
        )
        fetcher_called = []

        def fake_fetcher(lat: float, lon: float) -> PlaceData | None:
            fetcher_called.append((lat, lon))
            return None

        result = reverse_geocode(42.3614, -71.0572, db, fetcher=fake_fetcher)
        assert result.cache_hit is True
        assert result.place is not None
        assert result.place.city == "Somerville"
        assert fetcher_called == []  # fetcher must NOT be called on cache hit

    def test_reverse_geocode_null_cache_hit_suppresses_api_call(self, tmp_path: Path):
        # All-null cached entry → cache hit, no API call, PlaceData with all None fields
        db = _db(tmp_path)
        db.set_nominatim_cache(
            10.0,
            20.0,
            {
                "place_city": None,
                "place_state": None,
                "place_country": None,
                "place_country_code": None,
                "place_neighborhood": None,
                "place_address": None,
            },
        )
        fetcher_called = []

        def fake_fetcher(lat: float, lon: float) -> PlaceData | None:
            fetcher_called.append((lat, lon))
            return None

        result = reverse_geocode(10.0, 20.0, db, fetcher=fake_fetcher)
        assert result.cache_hit is True
        assert result.place is not None  # PlaceData instance, not Python None
        assert result.place.city is None
        assert fetcher_called == []

    def test_reverse_geocode_cache_miss_stores_result(self, tmp_path: Path):
        db = _db(tmp_path)

        def fake_fetcher(lat: float, lon: float) -> PlaceData | None:
            return _place(
                city="Cambridge", state="Massachusetts", country="United States", country_code="us"
            )

        result = reverse_geocode(42.374, -71.106, db, fetcher=fake_fetcher)
        assert result.cache_hit is False
        assert result.place is not None
        assert result.place.city == "Cambridge"
        # Verify it was stored in cache
        cached = db.get_nominatim_cache(42.374, -71.106)
        assert cached is not None
        assert cached["place_city"] == "Cambridge"

    def test_reverse_geocode_cache_miss_null_result(self, tmp_path: Path):
        # Fetcher returns PlaceData(all None) — should be cached (prevents future retries)
        db = _db(tmp_path)

        def fake_fetcher(lat: float, lon: float) -> PlaceData | None:
            return _place()  # all None

        result = reverse_geocode(0.0, 0.0, db, fetcher=fake_fetcher)
        assert result.cache_hit is False
        assert result.place is not None
        assert result.place.city is None
        # Must be stored in cache so future calls skip the API
        cached = db.get_nominatim_cache(0.0, 0.0)
        assert cached is not None  # row exists (cache hit next time)

    def test_reverse_geocode_rounds_coordinates(self, tmp_path: Path):
        # Two coordinates within ~111 m should share the same cache entry
        db = _db(tmp_path)
        fetcher_calls = []

        def fake_fetcher(lat: float, lon: float) -> PlaceData | None:
            fetcher_calls.append((lat, lon))
            return _place(city="Somerville")

        reverse_geocode(42.3614, -71.0572, db, fetcher=fake_fetcher)
        result2 = reverse_geocode(42.3612, -71.0574, db, fetcher=fake_fetcher)
        assert len(fetcher_calls) == 1  # second call hits cache
        assert result2.cache_hit is True
        assert result2.place is not None
        assert result2.place.city == "Somerville"

    def test_reverse_geocode_network_error(self, tmp_path: Path):
        # Fetcher returns None (network/HTTP error) — not cached
        db = _db(tmp_path)

        def fake_fetcher(lat: float, lon: float) -> PlaceData | None:
            return None  # simulate network error

        result = reverse_geocode(42.0, -71.0, db, fetcher=fake_fetcher)
        assert result.cache_hit is False
        assert result.place is None
        # Must NOT be stored in cache — next scan should retry
        cached = db.get_nominatim_cache(42.0, -71.0)
        assert cached is None
