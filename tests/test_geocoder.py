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
    LookupResult,  # noqa: F401 — used in Task 4 tests
    PlaceData,
    _parse_nominatim_response,
    reverse_geocode,  # noqa: F401 — used in Task 4 tests
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
