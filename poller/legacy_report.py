# poller/legacy_report.py
"""Report on legacy assets with no matching Flickr photo (#229).

Walks legacy_assets for a library and compares wall-clock timestamps against
ALL rows in the photos table (regardless of privacy state or uuid presence).
An asset is 'unmatched' if no photo row shares its wall-clock timestamp —
these are candidates for direct Flickr upload (#230).

Wall-clock matching is intentional: Flickr date_taken and legacy Apple dates
share identical wall-clock digits for the same shot. See legacy_match.py for
the full rationale.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from legacy_match import normalise_wall_clock  # noqa: E402


def report_unmatched(db, library_uuid: str) -> dict:
    """Return inventory of legacy assets with no Flickr counterpart.

    Returns:
        library_uuid  str           the queried library
        total         int           all legacy assets for this library
        matched       int           timestamp found in photos table
        unmatched     int           no timestamp match — potential uploads
        no_date       int           date_taken is NULL — cannot match
        by_year       dict[str,int] year string → unmatched count
        assets        list[dict]    the unmatched asset rows
    """
    flickr_timestamps: set[str] = set()
    for row in db.conn.execute("SELECT date_taken FROM photos").fetchall():
        norm = normalise_wall_clock(row["date_taken"])
        if norm:
            flickr_timestamps.add(norm)

    total = 0
    matched = 0
    no_date = 0
    unmatched: list[dict] = []

    for asset in db.iter_legacy_assets(library_uuid):
        total += 1
        norm = normalise_wall_clock(asset.get("date_taken"))
        if norm is None:
            no_date += 1
        elif norm in flickr_timestamps:
            matched += 1
        else:
            unmatched.append(asset)

    by_year: dict[str, int] = {}
    for asset in unmatched:
        dt = asset.get("date_taken") or ""
        year = dt[:4] if len(dt) >= 4 else "unknown"
        by_year[year] = by_year.get(year, 0) + 1

    return {
        "library_uuid": library_uuid,
        "total": total,
        "matched": matched,
        "unmatched": len(unmatched),
        "no_date": no_date,
        "by_year": by_year,
        "assets": unmatched,
    }
