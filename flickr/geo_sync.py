# flickr/geo_sync.py
"""
flickr/geo_sync.py — geo-location sync engine (#145)

Compares flickr_latitude/flickr_longitude vs photos_latitude/photos_longitude
cached in the DB and writes geo_location proposals to metadata_proposals.

No Flickr API calls. No writes to Photos or Flickr. Cache-based, offline.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from db.db import Database

log = logging.getLogger("blue-pearmain.geo_sync")

GEO_CREATE_THRESHOLD_M: int = 1_000  # create a proposal when divergence exceeds this
GEO_SUPPRESS_THRESHOLD_M: int = (
    800  # hysteresis band lower edge; below this → suppressed_under_threshold
)

# WGS84 equatorial radius — matches the 111_319.9 m/deg approximation used in
# threshold boundary tests. (db.db.haversine_m uses the mean spherical radius
# 6_371_000, which gives ~111_195 m/deg and would cause boundary tests to fail.)
_EARTH_RADIUS_M: float = 6_378_137.0


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return distance in metres between two lat/lon points (WGS84 equatorial radius)."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return _EARTH_RADIUS_M * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coord_hash(lat: float, lon: float) -> str:
    return f"{lat:.4f},{lon:.4f}"


def sync_geo(
    db: "Database",
    dry_run: bool = False,
    photo_ids: list[int] | None = None,
) -> dict[str, int]:
    """
    Detect geo discrepancies between Flickr and Photos caches, write proposals.

    Returns granular counters for observability.
    """
    totals: dict[str, int] = {
        "proposals_created": 0,
        "suppressed_confirmed_none": 0,
        "suppressed_in_band": 0,
        "suppressed_under_threshold": 0,
        "suppressed_both_absent": 0,
        "suppressed_not_linked": 0,
        "failed": 0,
    }

    if photo_ids is None:
        rows = db.conn.execute(
            """SELECT id FROM photos
               WHERE uuid IS NOT NULL
                 AND flickr_id IS NOT NULL
                 AND geo_confirmed_none = 0
                 AND (flickr_deleted IS NULL OR flickr_deleted = 0)"""
        ).fetchall()
        photo_ids = [r["id"] for r in rows]

    now = _now_iso()

    for photo_id in photo_ids:
        try:
            row = db.conn.execute(
                """SELECT id, uuid, flickr_id, geo_confirmed_none,
                          flickr_latitude, flickr_longitude,
                          photos_latitude, photos_longitude
                   FROM photos WHERE id = ?""",
                (photo_id,),
            ).fetchone()
        except Exception as e:
            log.warning("sync_geo: photo_id=%s fetch error: %s", photo_id, e)
            totals["failed"] += 1
            continue

        if not row:
            totals["failed"] += 1
            continue

        if row["geo_confirmed_none"]:
            totals["suppressed_confirmed_none"] += 1
            continue

        if not row["uuid"] or not row["flickr_id"]:
            totals["suppressed_not_linked"] += 1
            continue

        flk_lat = row["flickr_latitude"]
        flk_lon = row["flickr_longitude"]
        pho_lat = row["photos_latitude"]
        pho_lon = row["photos_longitude"]

        has_flickr = flk_lat is not None and flk_lon is not None
        has_photos = pho_lat is not None and pho_lon is not None

        proposals: list[dict] = []

        if has_flickr and not has_photos:
            proposals.append(
                _make_non_conflict(
                    photo_id,
                    source="flickr",
                    target="photos",
                    src_lat=flk_lat,
                    src_lon=flk_lon,
                    now=now,
                )
            )
        elif has_photos and not has_flickr:
            proposals.append(
                _make_non_conflict(
                    photo_id,
                    source="photos",
                    target="flickr",
                    src_lat=pho_lat,
                    src_lon=pho_lon,
                    now=now,
                )
            )
        elif has_flickr and has_photos:
            dist = _haversine_m(flk_lat, flk_lon, pho_lat, pho_lon)
            if dist > GEO_CREATE_THRESHOLD_M:
                proposals.extend(
                    _make_divergence_pair(
                        photo_id,
                        flk_lat=flk_lat,
                        flk_lon=flk_lon,
                        pho_lat=pho_lat,
                        pho_lon=pho_lon,
                        dist=dist,
                        now=now,
                    )
                )
            elif dist > GEO_SUPPRESS_THRESHOLD_M:
                # Hysteresis band: leave existing pending proposals untouched
                totals["suppressed_in_band"] += 1
                continue
            else:
                totals["suppressed_under_threshold"] += 1
                continue
        else:
            totals["suppressed_both_absent"] += 1
            continue

        if not dry_run:
            for p in proposals:
                db.upsert_proposal(p)
            db.conn.commit()
            totals["proposals_created"] += len(proposals)

    log.debug(
        "sync_geo done: created=%d  confirmed_none=%d  in_band=%d"
        "  under_threshold=%d  both_absent=%d  not_linked=%d  failed=%d",
        totals["proposals_created"],
        totals["suppressed_confirmed_none"],
        totals["suppressed_in_band"],
        totals["suppressed_under_threshold"],
        totals["suppressed_both_absent"],
        totals["suppressed_not_linked"],
        totals["failed"],
    )
    return totals


def _make_non_conflict(
    photo_id: int,
    source: str,
    target: str,
    src_lat: float,
    src_lon: float,
    now: str,
) -> dict:
    return {
        "photo_id": photo_id,
        "field": "geo_location",
        "proposed_value": json.dumps({"lat": src_lat, "lon": src_lon}),
        "source": source,
        "target": target,
        "conflict_type": "non_conflict",
        "source_hash_at_creation": _coord_hash(src_lat, src_lon),
        "target_hash_at_creation": None,
        "created_at": now,
    }


def _make_divergence_pair(
    photo_id: int,
    flk_lat: float,
    flk_lon: float,
    pho_lat: float,
    pho_lon: float,
    dist: float,
    now: str,
) -> list[dict]:
    dist_m = round(dist)
    flickr_to_photos = {
        "photo_id": photo_id,
        "field": "geo_location",
        "proposed_value": json.dumps(
            {
                "lat": flk_lat,
                "lon": flk_lon,
                "current_lat": pho_lat,
                "current_lon": pho_lon,
                "distance_m": dist_m,
            }
        ),
        "source": "flickr",
        "target": "photos",
        "conflict_type": "divergence",
        "source_hash_at_creation": _coord_hash(flk_lat, flk_lon),
        "target_hash_at_creation": _coord_hash(pho_lat, pho_lon),
        "created_at": now,
    }
    photos_to_flickr = {
        "photo_id": photo_id,
        "field": "geo_location",
        "proposed_value": json.dumps(
            {
                "lat": pho_lat,
                "lon": pho_lon,
                "current_lat": flk_lat,
                "current_lon": flk_lon,
                "distance_m": dist_m,
            }
        ),
        "source": "photos",
        "target": "flickr",
        "conflict_type": "divergence",
        "source_hash_at_creation": _coord_hash(pho_lat, pho_lon),
        "target_hash_at_creation": _coord_hash(flk_lat, flk_lon),
        "created_at": now,
    }
    return [flickr_to_photos, photos_to_flickr]
