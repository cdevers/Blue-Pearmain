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
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlparse

log = logging.getLogger("blue-pearmain.geocoder")

_USER_AGENT = (
    "BluePearmain/1.0 "
    "(https://github.com/cdevers/Blue-Pearmain; "
    "contact: 1642218+cdevers@users.noreply.github.com)"
)
_PUBLIC_NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
_last_call_time: float = 0.0  # module-level rate limiter (single-threaded)
_RETRY_DELAYS = (5, 15)  # minimum back-off (seconds) before each retry attempt on 429


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


def _fetch_nominatim(url: str, lat: float, lon: float) -> "PlaceData | None":
    """Execute one Nominatim request (with 429 retry) and return PlaceData or None.

    Handles request construction, retry back-off, response parsing, and error
    logging. Does NOT apply rate limiting — callers enforce delays before calling
    and update their own last_call_time timestamp afterward.
    """
    import requests  # deferred import — not needed if geocoder isn't used

    _params = {"lat": lat, "lon": lon, "zoom": 14, "addressdetails": 1, "format": "json"}
    _headers = {"User-Agent": _USER_AGENT}

    try:
        resp = requests.get(url, params=_params, headers=_headers, timeout=10)
        for delay in _RETRY_DELAYS:
            if resp.status_code != 429:
                break
            try:
                retry_after = int(resp.headers.get("Retry-After", delay))
            except ValueError:
                retry_after = delay  # HTTP-date format or unexpected value — fall back to floor
            wait = max(retry_after, delay)
            log.warning(
                "Nominatim rate-limited (429) for (%.6f, %.6f); backing off %ds",
                lat,
                lon,
                wait,
            )
            time.sleep(wait)
            resp = requests.get(url, params=_params, headers=_headers, timeout=10)
        if resp.status_code != 200:
            log.warning("Nominatim returned HTTP %s for (%.6f, %.6f)", resp.status_code, lat, lon)
            return None
        return _parse_nominatim_response(resp.json())
    except Exception as exc:
        log.warning("Nominatim request failed for (%.6f, %.6f): %s", lat, lon, exc)
        return None


def fetch_from_nominatim(lat: float, lon: float) -> "PlaceData | None":
    """Make a live HTTP GET to Nominatim and return parsed PlaceData, or None on error.

    Rate-limited to 1 request/second per Nominatim usage policy. On a 429 response,
    retries up to twice with incremental back-off: 5 seconds before the first retry,
    15 seconds before the second. The actual wait is max(Retry-After, floor) so we
    respect longer server-requested delays while ignoring Retry-After: 0.
    """
    global _last_call_time
    elapsed = time.monotonic() - _last_call_time
    if elapsed < 1.0:
        time.sleep(1.0 - elapsed)
    try:
        return _fetch_nominatim(_PUBLIC_NOMINATIM_URL, lat, lon)
    finally:
        _last_call_time = time.monotonic()


def make_fetcher(
    url: str | None = None,
    *,
    min_delay: float | None = None,
) -> Callable[[float, float], "PlaceData | None"]:
    """Return a Nominatim fetcher closure bound to url and an auto-detected delay.

    url=None uses the public endpoint. min_delay=None auto-detects: 1.0s for the
    public endpoint (Nominatim usage policy), 0.0s for any other host.
    The closure tracks its own last_call_time, independent of the module-level
    _last_call_time used by fetch_from_nominatim.
    """
    effective_url = url or _PUBLIC_NOMINATIM_URL
    if min_delay is None:
        # Compare hostname (not netloc) so explicit port variants like :443 still match.
        hostname = urlparse(effective_url).hostname
        min_delay = 1.0 if hostname == "nominatim.openstreetmap.org" else 0.0

    # 0.0 guarantees the first call is never rate-limited: time.monotonic() returns
    # the system uptime in seconds, so elapsed >> min_delay on any first real call.
    last_call_time = 0.0

    def fetcher(lat: float, lon: float) -> "PlaceData | None":
        nonlocal last_call_time
        elapsed = time.monotonic() - last_call_time
        if elapsed < min_delay:
            time.sleep(min_delay - elapsed)
        try:
            return _fetch_nominatim(effective_url, lat, lon)
        finally:
            last_call_time = time.monotonic()

    return fetcher


def reverse_geocode(
    lat: float,
    lon: float,
    db: Any,
    fetcher: Callable[[float, float], "PlaceData | None"] = fetch_from_nominatim,
) -> LookupResult:
    """Cache-first reverse geocode for (lat, lon).

    1. Round lat/lon to 3 decimal places.
    2. Check nominatim_cache via db.get_nominatim_cache(lat_r, lon_r).
    3. Cache hit  → return LookupResult(place=PlaceData(...), cache_hit=True).
       (place may have all-None fields if coordinates are known to return nothing)
    4. Cache miss → call fetcher(lat, lon).
       - fetcher returns None (error) → LookupResult(place=None, cache_hit=False),
         not cached; next scan will retry.
       - fetcher returns PlaceData → store in cache, return
         LookupResult(place=result, cache_hit=False).
    """
    lat_r = round(lat, 3)
    lon_r = round(lon, 3)

    cached = db.get_nominatim_cache(lat_r, lon_r)
    if cached is not None:
        # Cache hit — convert raw dict back to PlaceData
        place = PlaceData(
            city=cached.get("place_city"),
            state=cached.get("place_state"),
            country=cached.get("place_country"),
            country_code=cached.get("place_country_code"),
            neighborhood=cached.get("place_neighborhood"),
            address=cached.get("place_address"),
        )
        return LookupResult(place=place, cache_hit=True)

    # Cache miss — call the fetcher
    result = fetcher(lat, lon)
    if result is None:
        # Network/HTTP error — do not cache; allow retry on next scan
        return LookupResult(place=None, cache_hit=False)

    # Store result (including all-None PlaceData, which suppresses future retries)
    db.set_nominatim_cache(
        lat_r,
        lon_r,
        {
            "place_city": result.city,
            "place_state": result.state,
            "place_country": result.country,
            "place_country_code": result.country_code,
            "place_neighborhood": result.neighborhood,
            "place_address": result.address,
        },
    )
    return LookupResult(place=result, cache_hit=False)
