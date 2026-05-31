"""
privacy.py — classifies a photo record into a privacy state

This is intentionally pure logic with no I/O. Pass it an osxphotos
record (as a dict) plus the list of active geofence zones, and it
returns a (state, reason) tuple.

States:
  'auto_private'     geofence / home flag — skip review queue entirely
  'needs_review'     people detected — human must decide
  'candidate_public' no people signals — propose tags, confirm before pushing
"""

from __future__ import annotations
from db.db import haversine_m

# Apple label strings that indicate people are present in the frame
PEOPLE_LABELS = {
    "people",
    "person",
    "crowd",
    "audience",
    "performer",
    "singer",
    "entertainer",
    # add more as you observe false-negatives in practice
}

# Minimum human detection confidence to treat as a person signal
HUMAN_CONFIDENCE_THRESHOLD = 0.35

# Minimum face quality to count a face (filters ghost detections)
FACE_QUALITY_THRESHOLD = 0.0

# Ruleset version for classify(). Bump by hand whenever the rules in
# classify() change, so audit rows can be correlated to the logic in force.
CLASSIFIER_VERSION = 1


def classify(
    photo: dict,
    zones: list[dict],
    self_name: str = "",
    person_policies: dict[str, str] | None = None,
) -> tuple[str, str]:
    """
    Classify a photo into a privacy state.

    Args:
        photo:            dict from osxphotos (or equivalent from Flickr poller)
        zones:            list of active geofence zone dicts from the database
        self_name:        the photographer's name as it appears in Apple's People
        person_policies:  dict mapping person names to policy strings (e.g. "always_private")

    Returns:
        (state, reason) tuple
    """

    lat = photo.get("latitude")
    lon = photo.get("longitude")

    # ------------------------------------------------------------------
    # 1. Apple's own home flag — highest priority
    # ------------------------------------------------------------------
    if photo.get("place_ishome") or (
        isinstance(photo.get("place"), dict) and photo["place"].get("ishome")
    ):
        return "auto_private", "home location (Apple Photos)"

    # ------------------------------------------------------------------
    # 2. Custom geofence zones
    # ------------------------------------------------------------------
    if lat is not None and lon is not None:
        for zone in zones:
            dist = haversine_m(lat, lon, zone["latitude"], zone["longitude"])
            if dist <= zone["radius_m"]:
                policy = zone.get("policy", "auto_private")
                label = zone.get("label") or zone.get("name", "unnamed zone")
                if policy == "auto_private":
                    return "auto_private", f"geofence: {label}"
                elif policy == "flag_review":
                    return "needs_review", f"geofence flag: {label}"
                # policy == 'auto_public' falls through to normal logic

    # ------------------------------------------------------------------
    # 2b. Person policies — before general person detection
    # Case-insensitive: normalise both stored keys and photo names to
    # lowercase so that Apple Photos naming drift doesn't break matches.
    # ------------------------------------------------------------------
    if person_policies:
        _lower_policies = {k.lower(): v for k, v in person_policies.items()}
        persons = _get_persons(photo)
        named_others = [p for p in persons if p and p != self_name and p != "_UNKNOWN_"]
        for name in named_others:
            if _lower_policies.get(name.lower()) == "always_private":
                return "auto_private", f"person policy: {name}"

    # ------------------------------------------------------------------
    # 3. Named persons other than self
    # ------------------------------------------------------------------
    persons = _get_persons(photo)
    named_others = [p for p in persons if p and p != self_name and p != "_UNKNOWN_"]
    if named_others:
        names = ", ".join(named_others)
        return "needs_review", f"named person(s): {names}"

    # ------------------------------------------------------------------
    # 4. Unknown faces detected
    # ------------------------------------------------------------------
    unknown_count = _count_unknown_faces(photo)
    if unknown_count > 0:
        return "needs_review", f"{unknown_count} unidentified face(s)"

    # ------------------------------------------------------------------
    # 5. Apple's people/crowd labels
    # ------------------------------------------------------------------
    labels = _get_labels(photo)
    labels_lower = {lbl.lower() for lbl in labels}
    matched = labels_lower & PEOPLE_LABELS
    if matched:
        return "needs_review", f"people label(s): {', '.join(sorted(matched))}"

    # ------------------------------------------------------------------
    # 6. Human body detection in media_analysis
    # ------------------------------------------------------------------
    human_count = _confident_human_count(photo)
    if human_count > 0:
        return "needs_review", f"{human_count} human(s) detected (body detection)"

    # ------------------------------------------------------------------
    # 7. No people signals — candidate for public
    # ------------------------------------------------------------------
    return "candidate_public", "no people detected"


# ---------------------------------------------------------------------------
# Helpers to normalise the two record shapes we handle:
#   (a) raw osxphotos dict (nested structure)
#   (b) flat db dict (JSON-serialised lists, flattened fields)
# ---------------------------------------------------------------------------


def _get_persons(photo: dict) -> list[str]:
    persons = photo.get("persons") or photo.get("apple_persons") or []
    if isinstance(persons, str):
        import json

        try:
            persons = json.loads(persons)
        except Exception:
            persons = []
    return [str(p) for p in persons if p]


def _get_labels(photo: dict) -> list[str]:
    labels = photo.get("labels") or photo.get("apple_labels") or []
    if isinstance(labels, str):
        import json

        try:
            labels = json.loads(labels)
        except Exception:
            labels = []
    return [str(lbl) for lbl in labels if lbl]


def _count_unknown_faces(photo: dict) -> int:
    """Count _UNKNOWN_ persons, and also count quality-filtered face_info entries."""
    count = 0

    # From flat db record
    persons = _get_persons(photo)
    count += sum(1 for p in persons if p == "_UNKNOWN_")

    # From raw osxphotos face_info (if present)
    face_info = photo.get("face_info") or []
    for face in face_info:
        if (
            face.get("name") == ""
            and face.get("quality", -1) >= FACE_QUALITY_THRESHOLD
            and face.get("size", 0) > 0
        ):
            count += 1

    return count


def _confident_human_count(photo: dict) -> int:
    """Count humans[] entries in media_analysis above confidence threshold."""
    media = photo.get("media_analysis") or {}
    humans = media.get("humans") or []
    return sum(1 for h in humans if h.get("humanConfidence", 0) >= HUMAN_CONFIDENCE_THRESHOLD)
