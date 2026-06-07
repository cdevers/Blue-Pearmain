"""Nominatim reverse geocoding for place enrichment (#217).

Provides:
  PlaceData           — six place fields extracted from a Nominatim response
  LookupResult        — wraps PlaceData | None with a cache_hit flag
  _parse_nominatim_response — parse a raw Nominatim JSON dict into PlaceData
  fetch_from_nominatim — HTTP call to Nominatim (injectable for testing)
  reverse_geocode     — cache-first lookup; calls fetcher on miss
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("blue-pearmain.geocoder")

_USER_AGENT = (
    "BluePearmain/1.0 "
    "(https://github.com/cdevers/Blue-Pearmain; "
    "contact: 1642218+cdevers@users.noreply.github.com)"
)
_NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
_last_call_time: float = 0.0  # module-level rate limiter (single-threaded)


@dataclass
class PlaceData:
    city: str | None
    state: str | None
    country: str | None
    country_code: str | None
    neighborhood: str | None
    address: str | None


@dataclass
class LookupResult:
    """Wraps a geocoder result with cache provenance.

    place=None means a network/HTTP error occurred — not cached.
    place=PlaceData(all None) + cache_hit=True means coordinates are known
        to have no Nominatim result — no API call will be retried.
    cache_hit=True  → result came from nominatim_cache; no API call was made.
    cache_hit=False → a live API call was made (or attempted).
    """

    place: PlaceData | None
    cache_hit: bool


def _parse_nominatim_response(data: dict[str, Any]) -> PlaceData:
    """Parse a raw Nominatim JSON response dict into a PlaceData.

    Returns a PlaceData with all-None fields if address data is absent.
    Address field mapping:
      neighbourhood or suburb (first non-null) → neighborhood
      city, town, or village (first non-null)  → city
      state                                    → state
      country                                  → country
      country_code                             → country_code
      display_name (top-level)                 → address
    """
    addr = data.get("address") or {}
    neighborhood = addr.get("neighbourhood") or addr.get("suburb")
    city = addr.get("city") or addr.get("town") or addr.get("village")
    return PlaceData(
        city=city or None,
        state=addr.get("state") or None,
        country=addr.get("country") or None,
        country_code=addr.get("country_code") or None,
        neighborhood=neighborhood or None,
        address=data.get("display_name") or None,
    )


def fetch_from_nominatim(lat: float, lon: float) -> "PlaceData | None":
    raise NotImplementedError("fetch_from_nominatim is implemented in Task 4")


def reverse_geocode(lat: float, lon: float, db: Any, fetcher: Any = None) -> "LookupResult":
    raise NotImplementedError("reverse_geocode is implemented in Task 4")
