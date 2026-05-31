# poller/legacy_match.py
"""Non-destructive match-preview tiers for the legacy indexer (#162).

Pure logic: given a Flickr-only photo dict and candidate legacy_assets rows,
classify into confident / ambiguous / no-match and emit deterministically
ordered rows for the report/CSV.

Timestamp matching is done on local wall-clock time, NOT UTC. Flickr
date_taken is naive local capture time (EXIF DateTimeOriginal) and legacy
Apple dates are tz-aware in the photo's local zone; the same shot therefore
has identical wall-clock digits on both sides. Converting to UTC (as the
reupload/orphan matcher does, where both sides are Flickr-naive) would inject
the local offset as a false ~4-5h skew and miss nearly every match.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))
from deduplicator import _parse_dt  # noqa: E402
from legacy_normalize import normalize_title  # noqa: E402

from analyzer.privacy import PEOPLE_LABELS  # noqa: E402


def normalise_wall_clock(value) -> str | None:
    """Normalise a timestamp to local wall-clock 'YYYY-MM-DD HH:MM:SS'.

    Strips any timezone offset (keeping the local wall-clock digits) rather
    than converting to UTC, so naive Flickr times compare correctly against
    tz-aware legacy times. Returns None on empty/unparseable input.
    """
    if not value:
        return None
    dt = _parse_dt(value)
    if dt is None:
        return None
    return dt.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")


CONFIDENT = "confident"
AMBIGUOUS = "ambiguous"
NO_MATCH = "no-match"

_TIER_ORDER = {CONFIDENT: 0, AMBIGUOUS: 1, NO_MATCH: 2}


def _norm_dt(value) -> str | None:
    return normalise_wall_clock(value)


def _dims_match(photo: dict, cand: dict) -> bool:
    return photo.get("width") == cand.get("width") and photo.get("height") == cand.get("height")


def _title_conflict(photo: dict, cand: dict) -> bool:
    """Conflict only when BOTH titles are non-empty after normalization and differ."""
    a = normalize_title(photo.get("flickr_title"))
    b = normalize_title(cand.get("title"))
    if a is None or b is None:
        return False
    return a != b


def classify_match(photo: dict, candidates: list[dict]) -> tuple[str, list[dict]]:
    """Return (tier, matched_candidates). matched_candidates are the timestamp
    matches (the rows the report should show); empty for no-match."""
    pd = _norm_dt(photo.get("date_taken"))
    if pd is None:
        return NO_MATCH, []
    time_matches = [c for c in candidates if _norm_dt(c.get("date_taken")) == pd]
    if not time_matches:
        return NO_MATCH, []
    if len(time_matches) == 1:
        c = time_matches[0]
        if _dims_match(photo, c) and not _title_conflict(photo, c):
            return CONFIDENT, time_matches
        return AMBIGUOUS, time_matches
    return AMBIGUOUS, time_matches


def preview_rows(photo_candidate_pairs) -> list[dict]:
    """Build (unordered) report rows from (photo, candidates) pairs.

    confident -> one row (the match); ambiguous -> one row per timestamp
    candidate; no-match -> one row with empty asset_uuid.
    """
    rows: list[dict] = []
    for photo, candidates in photo_candidate_pairs:
        tier, matches = classify_match(photo, candidates)
        date_norm = _norm_dt(photo.get("date_taken")) or ""
        flickr_id = str(photo.get("flickr_id", ""))
        if tier == NO_MATCH:
            rows.append(
                {
                    "tier": tier,
                    "date_norm": date_norm,
                    "flickr_id": flickr_id,
                    "asset_uuid": "",
                    "width": photo.get("width"),
                    "height": photo.get("height"),
                    "flickr_title": photo.get("flickr_title") or "",
                }
            )
            continue
        for c in matches:
            rows.append(
                {
                    "tier": tier,
                    "date_norm": date_norm,
                    "flickr_id": flickr_id,
                    "asset_uuid": c.get("asset_uuid", ""),
                    "width": photo.get("width"),
                    "height": photo.get("height"),
                    "flickr_title": photo.get("flickr_title") or "",
                    "legacy_persons": c.get("persons", "[]"),
                    "legacy_title": c.get("title") or "",
                }
            )
    return rows


def order_rows(rows: list[dict]) -> list[dict]:
    """Deterministic order: tier -> date_norm -> flickr_id -> asset_uuid."""
    return sorted(
        rows,
        key=lambda r: (
            _TIER_ORDER.get(r["tier"], 99),
            r.get("date_norm", ""),
            r.get("flickr_id", ""),
            r.get("asset_uuid", ""),
        ),
    )


def _json_list(value) -> list[str]:
    """Parse a stored JSON-list field (string or already-decoded list)."""
    if isinstance(value, list):
        return [str(x) for x in value if x]
    if isinstance(value, str) and value:
        try:
            data = json.loads(value)
        except (ValueError, TypeError):
            return []
        if isinstance(data, list):
            return [str(x) for x in data if x]
    return []


def shape_legacy_for_classify(asset: dict) -> dict:
    """Build a classify()-ready record from a legacy_assets row.

    Reconstructs `_UNKNOWN_` sentinels from unknown_face_count so the shared
    classifier counts unknown faces the same way it does for Apple records.
    """
    persons = _json_list(asset.get("persons"))
    unknown = int(asset.get("unknown_face_count") or 0)
    persons = persons + ["_UNKNOWN_"] * unknown
    return {
        "latitude": asset.get("latitude"),
        "longitude": asset.get("longitude"),
        "persons": persons,
        "labels": _json_list(asset.get("labels")),
    }


def is_people_positive(asset: dict) -> bool:
    """True if the legacy asset shows any people signal classify() would act on."""
    if int(asset.get("named_face_count") or 0) > 0:
        return True
    if int(asset.get("unknown_face_count") or 0) > 0:
        return True
    if _json_list(asset.get("persons")):
        return True
    labels = {lbl.lower() for lbl in _json_list(asset.get("labels"))}
    return bool(labels & PEOPLE_LABELS)
