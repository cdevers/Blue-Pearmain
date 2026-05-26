"""
Photo filter helpers — pure functions returning SQLite WHERE clause fragments.
No Flask or DB dependencies.

All fragments reference the 'p' alias (photos p) to match the _library_where
convention in db/db.py. Unknown or no-op inputs return ("1=1", []).

Usage:
    from db.photo_filters import build_text_clause, build_location_clause
    sql, params = build_text_clause("sunset")
    sql, params = build_location_clause("United States", "MA", "Boston", None)
"""

from __future__ import annotations


def build_text_clause(q: str) -> tuple[str, list]:
    """LIKE search across all text fields including Apple AI caption.

    Semantics:
    - Case-insensitive for ASCII, case-sensitive for non-ASCII (SQLite LIKE behaviour).
    - Substring match only: '%q%'. Searching 'birthday cake' matches the
      whole phrase, not photos containing 'birthday' and 'cake' separately.
    - Tags are searched via json_each — 'bird' matches the tag 'birding'.
    """
    term = f"%{q}%"
    sql = (
        "(p.photos_title LIKE ? OR p.flickr_title LIKE ?"
        " OR p.photos_description LIKE ? OR p.flickr_description LIKE ?"
        " OR p.apple_ai_caption LIKE ?"
        " OR EXISTS (SELECT 1 FROM json_each(p.flickr_tags) WHERE value LIKE ?)"
        " OR EXISTS (SELECT 1 FROM json_each(p.photos_tags) WHERE value LIKE ?))"
    )
    return sql, [term] * 7


def build_location_clause(
    country: str | None,
    state: str | None,
    city: str | None,
    neighborhood: str | None,
) -> tuple[str, list]:
    """Exact match on place columns. Only non-None values generate clauses.
    All active levels are AND-combined, which disambiguates same-name cities
    (Springfield MA vs Springfield VT) and neighborhoods (Union Square in
    Somerville vs Boston). Photos with NULL place_country are never returned
    when country is set, because NULL != any string in SQL equality.

    Lower levels without parent levels return all matches across all parents —
    e.g. ?neighborhood=Union+Square alone matches every Union Square in the DB.
    The cascade UI prevents this in normal use by always sending the full path.
    """
    clauses: list[str] = []
    params: list = []
    if country:
        clauses.append("p.place_country = ?")
        params.append(country)
    if state:
        clauses.append("p.place_state = ?")
        params.append(state)
    if city:
        clauses.append("p.place_city = ?")
        params.append(city)
    if neighborhood:
        clauses.append("p.place_neighborhood = ?")
        params.append(neighborhood)
    if not clauses:
        return "1=1", []
    return " AND ".join(clauses), params


def build_person_clause(person: str) -> tuple[str, list]:
    """Match any photo whose apple_persons JSON array contains the exact name.
    '_UNKNOWN_' is a valid query value (returns photos with unidentified faces)
    even though it is filtered from the datalist autocomplete in the UI."""
    return (
        "EXISTS (SELECT 1 FROM json_each(p.apple_persons) WHERE value = ?)",
        [person],
    )


def build_date_alias_clause(date: str) -> tuple[str, list]:
    """Single-day filter. Used by the map popup 'Show this day' link via the
    ?date=YYYY-MM-DD alias. The alias is resolved in app.py before DB calls;
    this function is reserved for future use by other endpoints."""
    return "DATE(p.date_taken) = ?", [date]


def build_bbox_clause(
    lat_min: float, lat_max: float, lon_min: float, lon_max: float
) -> tuple[str, list]:
    """Spatial bounding-box filter. Returns photos whose GPS coordinates fall
    inside the given rectangle (BETWEEN is inclusive on both ends).
    Caller must ensure all four params are non-None and that lat_min <= lat_max
    and lon_min <= lon_max (app.py normalises these before calling).

    Antimeridian note: boxes that cross ±180° longitude are not supported.
    app.py swaps inverted lon values rather than splitting into two ranges.
    This is intentional — BP photos are in the Americas/Europe where this
    never occurs in practice."""
    sql = (
        "p.latitude IS NOT NULL AND p.longitude IS NOT NULL"
        " AND p.latitude BETWEEN ? AND ?"
        " AND p.longitude BETWEEN ? AND ?"
    )
    return sql, [lat_min, lat_max, lon_min, lon_max]
