"""
poller/exporter.py — export BP state to portable NDJSON files

All public functions are pure (no side effects except write_export).
Database queries use raw SQL so no changes to db.py are needed.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from db.db import Database


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _json_loads_safe(value: str | None) -> list:
    """Return parsed JSON list, or [] on None/error."""
    if not value:
        return []
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []


# ---------------------------------------------------------------------------
# Serialisers
# ---------------------------------------------------------------------------


def serialize_photo(row: dict, album_names: list[str]) -> dict:
    """Serialise one photo DB row to a portable export dict."""
    flickr_tags = _json_loads_safe(row.get("flickr_tags"))
    photos_tags = _json_loads_safe(row.get("photos_tags"))
    tags = flickr_tags if flickr_tags else photos_tags

    faces = [p for p in _json_loads_safe(row.get("apple_persons")) if p != "_UNKNOWN_"]

    location: dict | None = None
    if row.get("latitude") is not None:
        location = {
            "latitude": row["latitude"],
            "longitude": row["longitude"],
            "city": row.get("place_city"),
            "state": row.get("place_state"),
            "country": row.get("place_country"),
        }

    return {
        "id": row["id"],
        "flickr_id": row.get("flickr_id"),
        "apple_uuid": row.get("uuid"),
        "original_filename": row.get("original_filename"),
        "title": row.get("flickr_title") or "",
        "description": row.get("flickr_description") or "",
        "tags": tags,
        "privacy_state": row["privacy_state"],
        "review_decision": row.get("review_decision"),
        "reviewed_at": row.get("reviewed_at"),
        "date_taken": row.get("date_taken"),
        "location": location,
        "geofenced": bool(row.get("geofence_zone")),
        "faces": faces,
        "albums": album_names,
    }


def serialize_zone(row: dict) -> dict:
    """Serialise one geofence_zones row to a portable export dict."""
    return {
        "name": row["name"],
        "label": row.get("label"),
        "latitude": row["latitude"],
        "longitude": row["longitude"],
        "radius_m": row["radius_m"],
        "policy": row.get("policy", "auto_private"),
        "active": bool(row.get("active", 1)),
        "notes": row.get("notes"),
    }


# ---------------------------------------------------------------------------
# DB assembly
# ---------------------------------------------------------------------------


def collect_albums(db: "Database") -> dict[int, list[str]]:
    """Return a mapping from photo_id → list of album names (excluding deleted albums)."""
    rows = db.conn.execute(
        """SELECT pa.photo_id, a.name
           FROM photo_albums pa
           JOIN albums a ON pa.album_id = a.id
           WHERE a.deleted_at IS NULL"""
    ).fetchall()
    result: dict[int, list[str]] = {}
    for row in rows:
        result.setdefault(row["photo_id"], []).append(row["name"])
    return result


def collect_export_data(db: "Database") -> dict:
    """
    Assemble the full export payload from the DB.

    Returns:
        {
          "manifest": {"exported_at": ..., "photo_count": N, "zone_count": N,
                       "bp_version": ..., "export_format_version": "1"},
          "photos": [...],
          "zones": [...],
        }
    """
    albums_by_photo = collect_albums(db)

    photo_rows = db.conn.execute(
        """SELECT id, flickr_id, uuid, flickr_title, flickr_description,
                  flickr_tags, photos_tags, privacy_state, review_decision,
                  reviewed_at, date_taken, latitude, longitude, place_city,
                  place_state, place_country, geofence_zone, apple_persons,
                  original_filename
           FROM photos ORDER BY id"""
    ).fetchall()

    photos = [serialize_photo(dict(row), albums_by_photo.get(row["id"], [])) for row in photo_rows]

    zone_rows = db.conn.execute(
        """SELECT name, label, latitude, longitude, radius_m, policy, active, notes
           FROM geofence_zones ORDER BY name"""
    ).fetchall()
    zones = [serialize_zone(dict(row)) for row in zone_rows]

    now = datetime.now(timezone.utc).isoformat()

    # Read BP version from the bp script header
    try:
        bp_text = (Path(__file__).parent.parent / "bp").read_text()
        m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', bp_text)
        bp_version: str = m.group(1) if m else "unknown"
    except Exception:
        bp_version = "unknown"

    return {
        "manifest": {
            "exported_at": now,
            "photo_count": len(photos),
            "zone_count": len(zones),
            "bp_version": bp_version,
            "export_format_version": "1",
        },
        "photos": photos,
        "zones": zones,
    }


# ---------------------------------------------------------------------------
# File output
# ---------------------------------------------------------------------------


def write_export(data: dict, out_dir: Path) -> None:
    """
    Write export data to out_dir.

    Creates out_dir if it does not exist. Writes:
    - photos.ndjson  — one JSON object per line (streamable, grep-able)
    - zones.json     — JSON array (small count, plain array is fine)
    - manifest.json  — export metadata (always JSON)
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    # Photos: NDJSON — one object per line
    ndjson_lines = [json.dumps(p, ensure_ascii=False) for p in data["photos"]]
    (out_dir / "photos.ndjson").write_text(
        "\n".join(ndjson_lines) + ("\n" if ndjson_lines else ""),
        encoding="utf-8",
    )

    # Zones: plain JSON array (typically < 10 zones)
    (out_dir / "zones.json").write_text(
        json.dumps(data["zones"], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # Manifest: always pretty-printed JSON
    (out_dir / "manifest.json").write_text(
        json.dumps(data["manifest"], indent=2),
        encoding="utf-8",
    )
