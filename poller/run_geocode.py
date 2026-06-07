"""bp geocode — retroactive place data backfill via Nominatim (#217).

Provides run_geocode(), injectable for testing and called by cmd_geocode in bp.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

log = logging.getLogger("blue-pearmain.geocoder")

_PLACE_FIELDS = ("place_city", "place_state", "place_country", "place_neighborhood")


def run_geocode(
    db: Any,
    *,
    dry_run: bool,
    overwrite: bool,
    limit: int | None,
    fetcher: "Callable[[float, float], Any] | None" = None,
) -> dict[str, int]:
    """Backfill place data for photos with GPS coordinates but missing place fields.

    Returns counts:
      geocoded  — API calls that returned a result and were/would be written
      cached    — lookups from nominatim_cache (no API call made)
      no_result — Nominatim returned no address data
      skipped   — already had complete place data and --overwrite not passed
      errors    — API calls that returned None (network/HTTP errors)

    --limit N counts API call attempts. Cache hits do not count.
    Failed network calls DO count (prevents spinning on persistent errors).
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent))
    from geocoder import fetch_from_nominatim, reverse_geocode

    if fetcher is None:
        fetcher = fetch_from_nominatim

    if overwrite:
        query = (
            "SELECT id, latitude, longitude, place_city, place_state, place_country, "
            "place_neighborhood FROM photos WHERE latitude IS NOT NULL"
        )
        skip_count = 0
    else:
        query = (
            "SELECT id, latitude, longitude, place_city, place_state, place_country, "
            "place_neighborhood FROM photos WHERE latitude IS NOT NULL "
            "AND (place_city IS NULL OR place_state IS NULL "
            "OR place_country IS NULL OR place_neighborhood IS NULL)"
        )
        # Count photos already fully geocoded that will be skipped
        skip_count = db.conn.execute(
            "SELECT COUNT(*) FROM photos WHERE latitude IS NOT NULL "
            "AND place_city IS NOT NULL AND place_state IS NOT NULL "
            "AND place_country IS NOT NULL AND place_neighborhood IS NOT NULL"
        ).fetchone()[0]

    rows = db.conn.execute(query).fetchall()

    counts: dict[str, int] = {
        "geocoded": 0,
        "cached": 0,
        "no_result": 0,
        "skipped": skip_count,
        "errors": 0,
    }
    api_calls = 0

    for row in rows:
        photo_id = row["id"]
        lat = row["latitude"]
        lon = row["longitude"]

        if limit is not None and api_calls >= limit:
            break

        result = reverse_geocode(lat, lon, db, fetcher=fetcher)

        if result.cache_hit:
            if result.place and any(
                getattr(result.place, f.replace("place_", ""), None) for f in _PLACE_FIELDS
            ):
                if not dry_run:
                    db.update_place_data(
                        photo_id,
                        {
                            "place_city": result.place.city,
                            "place_state": result.place.state,
                            "place_country": result.place.country,
                            "place_country_code": result.place.country_code,
                            "place_neighborhood": result.place.neighborhood,
                            "place_address": result.place.address,
                        },
                        overwrite=overwrite,
                    )
                counts["cached"] += 1
            else:
                counts["no_result"] += 1
        else:
            api_calls += 1
            if result.place is None:
                counts["errors"] += 1
            elif any(getattr(result.place, f.replace("place_", ""), None) for f in _PLACE_FIELDS):
                if not dry_run:
                    db.update_place_data(
                        photo_id,
                        {
                            "place_city": result.place.city,
                            "place_state": result.place.state,
                            "place_country": result.place.country,
                            "place_country_code": result.place.country_code,
                            "place_neighborhood": result.place.neighborhood,
                            "place_address": result.place.address,
                        },
                        overwrite=overwrite,
                    )
                counts["geocoded"] += 1
            else:
                counts["no_result"] += 1

    return counts
