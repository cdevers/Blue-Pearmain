"""
flickr/coalesce_sets.py — detect and merge duplicate Flickr photosets

A "coalesce candidate" is a group of two or more Flickr photosets that share
the same title and whose photo date ranges overlap — or where one set is a
tiny orphan (≤ TINY_ORPHAN_THRESHOLD photos) that is almost certainly a BP
artifact from the duplicate-creation bug.

Legitimately separate albums that happen to share a name (e.g. "Instagram"
spanning multiple years) have non-overlapping date ranges and are not flagged.
"""

from __future__ import annotations

import logging
from collections import defaultdict

log = logging.getLogger("blue-pearmain.coalesce_sets")

# Photosets with at most this many photos are treated as orphans if a larger
# same-named set exists, regardless of date overlap.
TINY_ORPHAN_THRESHOLD = 5


def find_coalesce_candidates(
    db,
    flickr,
    all_sets: list[dict],
) -> list[dict]:
    """
    Inspect all_sets (from flickr.list_photosets()) and return groups that
    should be coalesced.

    Each returned group dict:
        title:     str
        canonical: set-dict (the set to keep)
        orphans:   list of set-dicts (sets to be merged in and deleted)

    Set-dicts have keys: id, title, photos, videos, in_db, db_album_id,
    earliest (ISO date str or None), latest (ISO date str or None).
    """
    # Group by title
    by_title: dict[str, list[dict]] = defaultdict(list)
    for s in all_sets:
        by_title[str(s["title"])].append(s)

    multi = {t: sets for t, sets in by_title.items() if len(sets) > 1}
    if not multi:
        return []

    # Build DB knowledge: set_id → album row with date range
    db_info: dict[str, dict] = {}
    for row in db.conn.execute(
        """SELECT a.id AS album_id, a.flickr_set_id,
                  MIN(p.date_taken) AS earliest, MAX(p.date_taken) AS latest
           FROM albums a
           LEFT JOIN photo_albums pa ON pa.album_id = a.id
           LEFT JOIN photos p ON p.id = pa.photo_id AND p.date_taken IS NOT NULL
           WHERE a.flickr_set_id IS NOT NULL
           GROUP BY a.id"""
    ).fetchall():
        db_info[row["flickr_set_id"]] = dict(row)

    candidates = []

    for title, sets in multi.items():
        annotated = _annotate_sets(sets, db_info, flickr)

        # Decide whether this group is a coalesce candidate
        has_tiny_orphan = any(
            not s["in_db"] and int(s["photos"]) <= TINY_ORPHAN_THRESHOLD for s in annotated
        )
        overlapping = _date_ranges_overlap([s for s in annotated if s["earliest"] and s["latest"]])

        if not (has_tiny_orphan or overlapping):
            continue

        # Pick canonical: prefer in_db, then most photos
        canonical = max(annotated, key=lambda s: (s["in_db"], int(s["photos"])))
        orphans = [s for s in annotated if s["id"] != canonical["id"]]

        candidates.append({"title": title, "canonical": canonical, "orphans": orphans})

    return candidates


def coalesce_group(db, flickr, group: dict, dry_run: bool = True) -> dict:
    """
    Merge each orphan photoset into the canonical set, then delete the orphan.

    Returns {"photos_moved": int, "sets_deleted": int}.
    """
    from flickr.flickr_client import FlickrError, FLICKR_ERR_ALREADY_IN_SET

    canonical = group["canonical"]
    orphans = group["orphans"]
    photos_moved = 0
    sets_deleted = 0

    for orphan in orphans:
        n_photos = int(orphan["photos"])
        if dry_run:
            log.info(
                "[dry-run] would merge %d photo(s) from %r (id=%s) into canonical (id=%s)",
                n_photos,
                group["title"],
                orphan["id"],
                canonical["id"],
            )
            continue

        # Move every photo from orphan into canonical
        photos = flickr.get_photoset_photos(orphan["id"])
        for photo in photos:
            try:
                flickr.add_photo_to_photoset(canonical["id"], photo["id"])
                photos_moved += 1
            except FlickrError as e:
                if e.code == FLICKR_ERR_ALREADY_IN_SET:
                    photos_moved += 1  # Already there — desired state achieved
                else:
                    log.warning(
                        "could not move photo %s from set %s to %s: %s",
                        photo["id"],
                        orphan["id"],
                        canonical["id"],
                        e,
                    )

        # Delete the orphan set
        flickr.delete_photoset(orphan["id"])
        sets_deleted += 1

        # Update the DB album row (if any) that referenced the orphan set
        if orphan.get("db_album_id"):
            db.conn.execute(
                "UPDATE albums SET flickr_set_id = ? WHERE id = ?",
                (canonical["id"], orphan["db_album_id"]),
            )
            db.conn.commit()

        log.info(
            "merged %d photo(s) from %r (id=%s) into canonical (id=%s) — orphan deleted",
            len(photos),
            group["title"],
            orphan["id"],
            canonical["id"],
        )

    return {"photos_moved": photos_moved, "sets_deleted": sets_deleted}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _annotate_sets(
    sets: list[dict],
    db_info: dict[str, dict],
    flickr,
) -> list[dict]:
    """Enrich each set dict with in_db, db_album_id, earliest, latest."""
    annotated = []
    for s in sets:
        info = db_info.get(s["id"])
        entry: dict = {
            **s,
            "in_db": info is not None,
            "db_album_id": info["album_id"] if info else None,
            "earliest": _norm_date(info["earliest"]) if info else None,
            "latest": _norm_date(info["latest"]) if info else None,
        }
        # For tiny Flickr-only orphans, fetch photo dates via API
        if info is None and int(s["photos"]) <= TINY_ORPHAN_THRESHOLD:
            try:
                photos = flickr.get_photoset_photos(s["id"], extras="date_taken")
                dates = sorted(p["datetaken"][:10] for p in photos if p.get("datetaken"))
                entry["earliest"] = dates[0] if dates else None
                entry["latest"] = dates[-1] if dates else None
            except Exception as exc:
                log.debug("could not fetch dates for orphan set %s: %s", s["id"], exc)
        annotated.append(entry)
    return annotated


def _date_ranges_overlap(sets: list[dict]) -> bool:
    """Return True if any two sets in the list have overlapping date ranges."""
    ranges = [(s["earliest"][:10], s["latest"][:10]) for s in sets if s["earliest"] and s["latest"]]
    for i, (e1, l1) in enumerate(ranges):
        for e2, l2 in ranges[i + 1 :]:
            if e1 <= l2 and e2 <= l1:
                return True
    return False


def _norm_date(val: str | None) -> str | None:
    """Return the date portion (YYYY-MM-DD) of an ISO datetime string, or None."""
    if not val:
        return None
    return val[:10]
