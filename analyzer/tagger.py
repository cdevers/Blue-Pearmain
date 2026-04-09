"""
tagger.py — derives proposed Flickr tags from Apple Photos metadata

Produces a deduplicated, sorted list of tag strings. Does not push
anything to Flickr; that happens after human review.

Tag sources (in priority order):
  1. Existing Flickr tags (preserved as-is)
  2. Existing Apple Photos keywords (user-curated, high trust)
  3. Apple ML labels (machine-generated, needs review)
  4. Location-derived tags (city, state, country, neighbourhood)
  5. Camera/technical tags (optional, off by default)
"""

from __future__ import annotations
import json
from typing import Any


# Apple labels we don't want to surface as Flickr tags — too generic
# or privacy-sensitive (people-related ones handled separately)
LABEL_BLOCKLIST = {
    "people", "person", "crowd", "audience",
    "performer",          # too vague
    "art",                # too vague
    "document",           # not useful
    "machine",            # too vague
    "light",              # too vague
    "line",               # too vague
    "frame",              # too vague
    "path",               # too vague
    "wall",               # too vague
    "window",             # too vague
    "door",               # too vague
    "clothing",           # too vague — specific items OK
    "outdoor",            # redundant with other labels
    "land",               # too vague
}

# Labels to rename for cleaner Flickr tags (apple_label -> flickr_tag)
LABEL_REMAP = {
    "automobile": "car",
    "road bicycle": "bicycle",
    "water body": "water",
    "natural environment": "nature",
    "performing arts": "performance",
    "rock concert": "concert",
    "music venue": "venue",
    "urban area": "urban",
    "metropolitan area": "city",
    "string instrument": "instrument",
    "musical instrument": "instrument",
}


def propose_tags(photo: dict[str, Any], include_camera: bool = False) -> list[str]:
    """
    Return a deduplicated sorted list of proposed tag strings.

    Args:
        photo:          dict from osxphotos or flat db record
        include_camera: whether to add camera model as a tag
    """
    tags: set[str] = set()

    # 1. Existing Flickr tags (already there, keep them)
    for t in _get_list(photo, "flickr_tags"):
        if t and t.strip():
            tags.add(t.strip().lower())

    # 2. Apple Photos keywords (user-curated)
    for kw in _get_list(photo, "keywords"):
        if kw and kw.strip():
            tags.add(kw.strip().lower())

    # 3. Apple ML labels
    for label in _get_labels(photo):
        label_lower = label.lower()
        if label_lower in LABEL_BLOCKLIST:
            continue
        remapped = LABEL_REMAP.get(label_lower, label_lower)
        tags.add(remapped)

    # 4. Location tags
    place = photo.get("place") or {}
    if isinstance(place, str):
        try:
            place = json.loads(place)
        except Exception:
            place = {}

    # Flat db fields take precedence
    city = photo.get("place_city") or (place.get("address") or {}).get("city")
    state = photo.get("place_state") or (place.get("address") or {}).get("state_province")
    country = photo.get("place_country") or (place.get("address") or {}).get("country")
    neighborhood = photo.get("place_neighborhood")

    for loc in [city, state, country, neighborhood]:
        if loc and loc.strip():
            tags.add(loc.strip().lower())

    # 5. Camera tag (optional)
    if include_camera:
        camera = photo.get("camera_model") or (
            (photo.get("exif_info") or {}).get("camera_model")
        )
        if camera:
            tags.add(camera.lower().replace(" ", "-"))

    # Remove empty strings that may have snuck in
    tags.discard("")

    return sorted(tags)


def merge_tags(existing: list[str], proposed: list[str]) -> list[str]:
    """
    Merge existing Flickr tags with proposed new ones.
    Existing tags are always preserved. Proposed tags are added.
    Returns sorted deduplicated list.
    """
    merged = set(t.lower().strip() for t in existing if t.strip())
    merged.update(t.lower().strip() for t in proposed if t.strip())
    merged.discard("")
    return sorted(merged)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_list(photo: dict, field: str) -> list:
    val = photo.get(field) or []
    if isinstance(val, str):
        try:
            val = json.loads(val)
        except Exception:
            val = [val]  # treat as single tag string
    return val if isinstance(val, list) else []


def _get_labels(photo: dict) -> list[str]:
    """Get Apple ML labels from either osxphotos or flat db format."""
    labels = (
        photo.get("labels")
        or photo.get("labels_normalized")
        or photo.get("apple_labels")
        or []
    )
    if isinstance(labels, str):
        try:
            labels = json.loads(labels)
        except Exception:
            labels = []
    return [str(l) for l in labels if l]
