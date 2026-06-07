"""Tests for poller/geocoder.py — Nominatim reverse geocoding (#217).

All tests use an injectable fetcher — no real HTTP calls are made.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

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


# ---------------------------------------------------------------------------
# Scanner integration
# ---------------------------------------------------------------------------


class TestScannerIntegration:
    """Test that build_enriched_row calls reverse_geocode correctly."""

    EXISTING: dict = {
        "uuid": "test-uuid",
        "flickr_id": "12345",
        "privacy_state": "candidate_public",
        "privacy_reason": "",
        "proposed_tags": [],
        "place_city": None,
        "place_state": None,
        "place_country": None,
        "place_country_code": None,
        "place_neighborhood": None,
        "place_address": None,
        "place_ishome": 0,
        "apple_persons": [],
        "apple_named_faces": 0,
        "apple_unknown_faces": 0,
        "apple_labels": [],
        "apple_human_count": 0,
        "apple_ai_caption": "",
        "apple_ai_caption_conf": 0.0,
        "geofenced": 0,
    }

    def _photo_row_with_coords(
        self,
        lat: float,
        lon: float,
        place_city: str | None = None,
    ) -> dict:
        return {
            "uuid": "test-uuid",
            "latitude": lat,
            "longitude": lon,
            "place_city": place_city,
            "place_state": None,
            "place_country": None,
            "place_country_code": None,
            "place_neighborhood": None,
            "place_address": None,
            "place_ishome": 0,
            "apple_persons": [],
            "apple_named_faces": 0,
            "apple_unknown_faces": 0,
            "apple_labels": [],
            "apple_human_count": 0,
            "apple_ai_caption": "",
            "apple_ai_caption_conf": 0.0,
            "date_analyzed": None,
            "meta_synced_photos_at": None,
            "photos_tags_hash": None,
            "photos_title": None,
            "photos_description": None,
            "photos_tags": [],
            "_is_screenshot": False,
            "_is_selfie": False,
            "_is_live": False,
            "is_video": 0,
        }

    def test_scanner_fills_place_from_geocoder(self, tmp_path: Path):
        from scanner import build_enriched_row

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

        photo_row = self._photo_row_with_coords(42.3614, -71.0572)
        result = build_enriched_row(photo_row, self.EXISTING, [], "Chris Devers", db=db)
        assert result["place_city"] == "Somerville"
        assert result["place_state"] == "Massachusetts"

    def test_scanner_skips_geocoder_when_all_place_set(self, tmp_path: Path):
        from scanner import build_enriched_row

        db = _db(tmp_path)
        # Photo row already has all four key place fields populated
        photo_row = self._photo_row_with_coords(42.361, -71.057)
        photo_row["place_city"] = "Somerville"
        photo_row["place_state"] = "Massachusetts"
        photo_row["place_country"] = "United States"
        photo_row["place_neighborhood"] = "Winter Hill"

        # No entry in nominatim_cache — if geocoder is called, it would find nothing
        result = build_enriched_row(photo_row, self.EXISTING, [], "Chris Devers", db=db)
        # place_city should still be Somerville (from photo_row), not overwritten
        assert result["place_city"] == "Somerville"
        # Verify cache was NOT written (geocoder skipped)
        cached = db.get_nominatim_cache(42.361, -71.057)
        assert cached is None

    def test_scanner_zero_zero_coordinates_not_skipped(self, tmp_path: Path):
        # (lat=0.0, lon=0.0) is a valid coordinate pair (null island).
        # Neither value should be treated as falsy — geocoder must be called.
        from scanner import build_enriched_row

        db = _db(tmp_path)
        db.set_nominatim_cache(
            0.0,
            0.0,
            {
                "place_city": "Gulf of Guinea",
                "place_state": None,
                "place_country": None,
                "place_country_code": None,
                "place_neighborhood": None,
                "place_address": None,
            },
        )

        photo_row = self._photo_row_with_coords(0.0, 0.0)
        result = build_enriched_row(photo_row, self.EXISTING, [], "Chris Devers", db=db)
        assert result["place_city"] == "Gulf of Guinea"


# ---------------------------------------------------------------------------
# bp geocode command
# ---------------------------------------------------------------------------


class TestBpGeocode:
    """Tests for run_geocode() (injectable; called by cmd_geocode in bp)."""

    def _make_db_with_photo(
        self,
        tmp_path: Path,
        *,
        lat: float = 42.361,
        lon: float = -71.057,
        place_city: str | None = None,
        place_state: str | None = None,
        place_country: str | None = None,
        place_neighborhood: str | None = None,
    ) -> tuple["Database", int]:
        """Insert a test photo with the given place fields; return (db, photo_id)."""
        db = _db(tmp_path)
        row_id = db.upsert_photo(
            {
                "uuid": "test-geocode-uuid",
                "flickr_id": None,
                "latitude": lat,
                "longitude": lon,
                "place_city": place_city,
                "place_state": place_state,
                "place_country": place_country,
                "place_neighborhood": place_neighborhood,
                "privacy_state": "candidate_public",
                "privacy_reason": "",
                "proposed_tags": [],
            }
        )
        return db, row_id

    def test_bp_geocode_fills_gaps(self, tmp_path: Path):
        from run_geocode import run_geocode

        db, photo_id = self._make_db_with_photo(tmp_path)

        def fake_fetcher(lat: float, lon: float) -> PlaceData | None:
            return _place(
                city="Somerville", state="Massachusetts", country="United States", country_code="us"
            )

        counts = run_geocode(db, dry_run=False, overwrite=False, limit=None, fetcher=fake_fetcher)
        assert counts["geocoded"] == 1
        row = db.get_photo(photo_id)
        assert row["place_city"] == "Somerville"

    def test_bp_geocode_skips_existing(self, tmp_path: Path):
        from run_geocode import run_geocode

        db, photo_id = self._make_db_with_photo(
            tmp_path,
            place_city="Cambridge",
            place_state="Massachusetts",
            place_country="United States",
            place_neighborhood="Harvard Square",
        )

        fetcher_calls = []

        def fake_fetcher(lat: float, lon: float) -> PlaceData | None:
            fetcher_calls.append((lat, lon))
            return _place(city="Somerville")

        counts = run_geocode(db, dry_run=False, overwrite=False, limit=None, fetcher=fake_fetcher)
        assert counts["skipped"] == 1
        assert counts["geocoded"] == 0
        assert fetcher_calls == []  # no API call needed
        row = db.get_photo(photo_id)
        assert row["place_city"] == "Cambridge"  # unchanged

    def test_bp_geocode_overwrite_flag(self, tmp_path: Path):
        from run_geocode import run_geocode

        db, photo_id = self._make_db_with_photo(
            tmp_path,
            place_city="Old City",
            place_state="Old State",
            place_country="Old Country",
            place_neighborhood="Old Neighborhood",
        )

        def fake_fetcher(lat: float, lon: float) -> PlaceData | None:
            return _place(
                city="New City", state="New State", country="New Country", country_code="nc"
            )

        counts = run_geocode(db, dry_run=False, overwrite=True, limit=None, fetcher=fake_fetcher)
        assert counts["geocoded"] >= 1
        row = db.get_photo(photo_id)
        assert row["place_city"] == "New City"

    def test_bp_geocode_dry_run(self, tmp_path: Path):
        from run_geocode import run_geocode

        db, photo_id = self._make_db_with_photo(tmp_path)

        def fake_fetcher(lat: float, lon: float) -> PlaceData | None:
            return _place(city="Somerville")

        counts = run_geocode(db, dry_run=True, overwrite=False, limit=None, fetcher=fake_fetcher)
        assert counts["geocoded"] == 1  # counted
        row = db.get_photo(photo_id)
        assert row["place_city"] is None  # DB unchanged

    def test_bp_geocode_limit(self, tmp_path: Path):
        from run_geocode import run_geocode

        # Insert two photos with missing place data
        db = _db(tmp_path)
        db.upsert_photo(
            {
                "uuid": "uuid-a",
                "flickr_id": None,
                "latitude": 42.0,
                "longitude": -71.0,
                "place_city": None,
                "place_state": None,
                "place_country": None,
                "place_neighborhood": None,
                "privacy_state": "candidate_public",
                "privacy_reason": "",
                "proposed_tags": [],
            }
        )
        db.upsert_photo(
            {
                "uuid": "uuid-b",
                "flickr_id": None,
                "latitude": 43.0,
                "longitude": -72.0,
                "place_city": None,
                "place_state": None,
                "place_country": None,
                "place_neighborhood": None,
                "privacy_state": "candidate_public",
                "privacy_reason": "",
                "proposed_tags": [],
            }
        )

        fetcher_calls = []

        def fake_fetcher(lat: float, lon: float) -> PlaceData | None:
            fetcher_calls.append((lat, lon))
            return _place(city="Somewhere")

        run_geocode(db, dry_run=False, overwrite=False, limit=1, fetcher=fake_fetcher)
        assert len(fetcher_calls) == 1  # stopped after limit

    def test_bp_geocode_limit_counts_failed_calls(self, tmp_path: Path):
        # Network errors count toward --limit to prevent spinning on persistent failures
        from run_geocode import run_geocode

        db = _db(tmp_path)
        for i in range(3):
            db.upsert_photo(
                {
                    "uuid": f"uuid-{i}",
                    "flickr_id": None,
                    "latitude": float(40 + i),
                    "longitude": -71.0,
                    "place_city": None,
                    "place_state": None,
                    "place_country": None,
                    "place_neighborhood": None,
                    "privacy_state": "candidate_public",
                    "privacy_reason": "",
                    "proposed_tags": [],
                }
            )

        fetcher_calls = []

        def fake_fetcher(lat: float, lon: float) -> PlaceData | None:
            fetcher_calls.append((lat, lon))
            return None  # persistent network error

        run_geocode(db, dry_run=False, overwrite=False, limit=2, fetcher=fake_fetcher)
        assert len(fetcher_calls) == 2  # limited even with errors

    def test_bp_geocode_stops_early_on_consecutive_errors(self, tmp_path: Path):
        # After 3 consecutive API errors, run_geocode aborts and sets stopped_early.
        from run_geocode import run_geocode

        db = _db(tmp_path)
        for i in range(5):
            db.upsert_photo(
                {
                    "uuid": f"uuid-err-{i}",
                    "flickr_id": None,
                    "latitude": float(40 + i),
                    "longitude": -71.0,
                    "place_city": None,
                    "place_state": None,
                    "place_country": None,
                    "place_neighborhood": None,
                    "privacy_state": "candidate_public",
                    "privacy_reason": "",
                    "proposed_tags": [],
                }
            )

        fetcher_calls = []

        def fake_fetcher(lat: float, lon: float) -> PlaceData | None:
            fetcher_calls.append((lat, lon))
            return None  # all calls fail

        counts = run_geocode(db, dry_run=False, overwrite=False, limit=None, fetcher=fake_fetcher)
        # Should stop after 3 consecutive errors, not process all 5 photos
        assert len(fetcher_calls) == 3
        assert counts["errors"] == 3
        assert counts["stopped_early"] == 1


# ---------------------------------------------------------------------------
# fetch_from_nominatim — 429 backoff (#219)
# ---------------------------------------------------------------------------


class TestFetchFromNominatim:
    def test_429_backs_off_and_retries_on_success(self):
        """On 429 + Retry-After, sleeps the right amount and returns PlaceData on retry."""
        from unittest.mock import MagicMock, patch

        from geocoder import fetch_from_nominatim

        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.headers = {"Retry-After": "2"}  # below the 5-second first-retry floor

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json.return_value = {
            "display_name": "Somerville, MA",
            "address": {
                "city": "Somerville",
                "state": "Massachusetts",
                "country": "United States",
                "country_code": "us",
            },
        }

        with (
            patch("requests.get", side_effect=[resp_429, resp_200]) as mock_get,
            patch("time.sleep") as mock_sleep,
        ):
            result = fetch_from_nominatim(42.361, -71.057)

        assert result is not None
        assert result.city == "Somerville"
        assert mock_get.call_count == 2
        mock_sleep.assert_any_call(5)  # first-retry floor: max(2, 5) == 5

    def test_429_default_backoff_when_no_retry_after(self):
        """On 429 without Retry-After header, uses the first-retry floor of 5 seconds."""
        from unittest.mock import MagicMock, patch

        from geocoder import fetch_from_nominatim

        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.headers = {}

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json.return_value = {
            "display_name": "Somewhere",
            "address": {"country": "United States", "country_code": "us"},
        }

        with (
            patch("requests.get", side_effect=[resp_429, resp_200]),
            patch("time.sleep") as mock_sleep,
        ):
            fetch_from_nominatim(42.0, -71.0)

        mock_sleep.assert_any_call(5)  # default floor for first retry: max(0, 5) == 5

    def test_429_retry_also_fails_returns_none(self):
        """On persistent 429, returns None after all retries (3 HTTP calls total)."""
        from unittest.mock import MagicMock, patch

        from geocoder import fetch_from_nominatim

        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.headers = {"Retry-After": "0"}

        with (
            patch("requests.get", return_value=resp_429) as mock_get,
            patch("time.sleep") as mock_sleep,
        ):
            result = fetch_from_nominatim(42.0, -71.0)

        assert result is None
        # original attempt + 2 retries (one per entry in _RETRY_DELAYS)
        assert mock_get.call_count == 3
        # incremental back-off: 5s then 15s
        mock_sleep.assert_any_call(5)
        mock_sleep.assert_any_call(15)


# ---------------------------------------------------------------------------
# make_fetcher — URL-bound fetcher factory (#225)
# ---------------------------------------------------------------------------


class TestMakeFetcher:
    def _mock_resp_200(self):
        from unittest.mock import MagicMock

        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"display_name": "X", "address": {}}
        return resp

    def test_make_fetcher_default_url_uses_public_endpoint(self):
        """make_fetcher() with no URL argument hits the public Nominatim endpoint."""
        from unittest.mock import patch

        from geocoder import _PUBLIC_NOMINATIM_URL, make_fetcher

        fetcher = make_fetcher()
        with patch("requests.get", return_value=self._mock_resp_200()) as mock_get:
            fetcher(42.361, -71.057)

        assert mock_get.call_args[0][0] == _PUBLIC_NOMINATIM_URL

    def test_make_fetcher_public_url_delay_is_1s(self):
        """Subsequent calls are rate-limited to 1 request/second.

        Uses realistic uptime-scale mock values to mirror production behaviour:
          - last_call_time starts at 0.0 (closure init)
          - first call:  elapsed = 1000.0 - 0.0 = 1000.0 → no sleep; LCT = 1000.1
          - second call: elapsed = 1000.2 - 1000.1 = 0.1 < 1.0 → sleep(0.9); LCT = 1001.2
        """
        from unittest.mock import patch

        from geocoder import _PUBLIC_NOMINATIM_URL, make_fetcher

        fetcher = make_fetcher(_PUBLIC_NOMINATIM_URL)

        with (
            patch("requests.get", return_value=self._mock_resp_200()),
            patch("time.monotonic", side_effect=[1000.0, 1000.1, 1000.2, 1001.2]),
            patch("time.sleep") as mock_sleep,
        ):
            fetcher(42.0, -71.0)  # first call: goes through without sleeping
            fetcher(42.0, -71.0)  # second call: rate-limited

        mock_sleep.assert_called_once_with(pytest.approx(0.9))

    def test_make_fetcher_local_url_delay_is_0(self):
        """Auto-detected delay is 0.0s for any URL other than the public endpoint."""
        from unittest.mock import patch

        from geocoder import make_fetcher

        fetcher = make_fetcher("http://localhost:8080/reverse")

        with (
            patch("requests.get", return_value=self._mock_resp_200()),
            patch("time.monotonic", side_effect=[1000.0, 1000.1, 1000.1, 1000.2]),
            patch("time.sleep") as mock_sleep,
        ):
            fetcher(42.0, -71.0)
            fetcher(42.0, -71.0)  # elapsed = 0.0, but min_delay=0.0 → no sleep

        mock_sleep.assert_not_called()

    def test_make_fetcher_explicit_min_delay_overrides_auto(self):
        """Explicit min_delay=0.0 suppresses the delay even for the public URL."""
        from unittest.mock import patch

        from geocoder import _PUBLIC_NOMINATIM_URL, make_fetcher

        fetcher = make_fetcher(_PUBLIC_NOMINATIM_URL, min_delay=0.0)

        with (
            patch("requests.get", return_value=self._mock_resp_200()),
            patch("time.monotonic", side_effect=[1000.0, 1000.1, 1000.1, 1000.2]),
            patch("time.sleep") as mock_sleep,
        ):
            fetcher(42.0, -71.0)
            fetcher(42.0, -71.0)  # min_delay=0.0 overrides auto-detect → no sleep

        mock_sleep.assert_not_called()
