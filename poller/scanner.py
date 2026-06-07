"""
scanner.py — Apple Photos → local DB sync for Blue Pearmain

Reads the local Photos library via osxphotos, cross-references records
against the Flickr-sourced DB entries, enriches matched records with
Apple's ML data (labels, faces, captions, GPS), and re-runs the privacy
classifier with the fuller picture.

Usage:
    python poller/scanner.py --config config/config.yml
    python poller/scanner.py --config config/config.yml --all
    python poller/scanner.py --config config/config.yml --dry-run --verbose

Options:
    --config PATH    Path to config.yml (required)
    --all            Scan entire Photos library, not just recently added/modified
    --days N         How many days back to scan for recent photos (default: 7)
    --dry-run        Classify and match but don't write to DB
    --verbose        Extra logging
    --library PATH   Override Photos library path from config
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.db import Database
from analyzer.privacy import classify
from analyzer.tagger import propose_tags

log = logging.getLogger("blue-pearmain.scanner")


def _normalise_tag(tag: str) -> str:
    # Flickr normalizes tags to alphanumeric-only ("close-up" → "closeup",
    # "new york" → "newyork"). Keep only isalnum() chars so hashes align.
    return "".join(c for c in unicodedata.normalize("NFC", tag.strip().casefold()) if c.isalnum())


def _compute_tags_hash(tags: list[str]) -> str:
    normed = sorted({_normalise_tag(t) for t in tags if t.strip()})
    return hashlib.sha256(" ".join(normed).encode()).hexdigest()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def load_config(path: Path) -> dict:
    if not path.exists():
        log.error(f"Config file not found: {path}")
        sys.exit(1)
    with open(path) as f:
        return yaml.safe_load(f)


def setup_logging(config: dict, verbose: bool) -> None:
    from poller.bp_logging import configure

    configure("scanner", verbose)


# ---------------------------------------------------------------------------
# osxphotos → flat DB dict
# ---------------------------------------------------------------------------


def photos_record_to_db(photo) -> dict:
    """
    Convert an osxphotos PhotoInfo object to a flat dict for db.upsert_photo().
    Handles missing fields gracefully — osxphotos may return None for many.
    """
    row: dict = {}

    # Identity
    row["uuid"] = photo.uuid
    row["original_filename"] = photo.original_filename

    # Timestamps
    if photo.date:
        row["date_taken"] = photo.date.isoformat()
    if photo.date_added:
        row["date_added_photos"] = photo.date_added.isoformat()

    # Analysis date from media_analysis
    ma = getattr(photo, "media_analysis", None) or {}
    if isinstance(ma, dict) and ma.get("date_analyzed"):
        row["date_analyzed"] = ma["date_analyzed"]

    # Camera
    exif = photo.exif_info
    if exif:
        row["camera_make"] = exif.camera_make or ""
        row["camera_model"] = exif.camera_model or ""
        row["lens_model"] = exif.lens_model or ""

    # Location
    if photo.latitude is not None:
        row["latitude"] = photo.latitude
        row["longitude"] = photo.longitude
        row["photos_latitude"] = photo.latitude  # geo cache — #145
        row["photos_longitude"] = photo.longitude  # geo cache — #145

    place = photo.place
    if place:
        addr = place.address or {}
        row["place_city"] = getattr(addr, "city", None) or ""
        row["place_state"] = getattr(addr, "state_province", None) or ""
        row["place_country"] = getattr(addr, "country", None) or ""
        row["place_country_code"] = getattr(addr, "iso_country_code", None) or ""
        row["place_address"] = place.address_str or ""
        row["place_ishome"] = 1 if place.ishome else 0

        # Neighbourhood — first entry of additional_city_info if present
        names = place.names or {}
        extra_city = getattr(names, "additional_city_info", None) or []
        if extra_city:
            row["place_neighborhood"] = extra_city[0]

    # Photos metadata cache (title, description, keywords)
    photos_title = getattr(photo, "title", None) or ""
    photos_description = getattr(photo, "description", None) or ""
    photos_tags = list(getattr(photo, "keywords", None) or [])
    row["photos_title"] = photos_title
    row["photos_description"] = photos_description
    row["photos_tags"] = photos_tags  # auto-serialised to JSON by upsert_photo
    row["photos_tags_hash"] = _compute_tags_hash(photos_tags)
    row["meta_synced_photos_at"] = datetime.now(timezone.utc).isoformat()

    # Apple ML labels
    labels = list(photo.labels or [])
    row["apple_labels"] = labels

    # Apple ML persons
    persons = list(photo.persons or [])
    row["apple_persons"] = persons
    row["apple_named_faces"] = sum(1 for p in persons if p and p != "_UNKNOWN_")
    row["apple_unknown_faces"] = sum(1 for p in persons if p == "_UNKNOWN_")

    # Human count from media_analysis
    if isinstance(ma, dict):
        humans = ma.get("humans") or []
        row["apple_human_count"] = len(humans)

        # Apple AI caption
        caption_data = ma.get("image_caption") or {}
        if isinstance(caption_data, dict):
            row["apple_ai_caption"] = caption_data.get("imageCaptionText", "")
            row["apple_ai_caption_conf"] = caption_data.get("imageCaptionConfidence", 0.0)

    # Apple aesthetic score
    score = photo.score
    if score:
        row["apple_aesthetic_score"] = getattr(score, "overall", None)

    # Special media type flags — store in privacy_reason if screenshot
    row["_is_screenshot"] = bool(getattr(photo, "screenshot", False))
    row["_is_selfie"] = bool(getattr(photo, "selfie", False))
    row["_is_live"] = bool(getattr(photo, "live_photo", False))
    row["is_video"] = 1 if getattr(photo, "ismovie", False) else 0

    # Fingerprint for matching
    row["fingerprint"] = getattr(photo, "fingerprint", None) or ""

    # Dimensions
    row["width"] = getattr(photo, "width", None)
    row["height"] = getattr(photo, "height", None)

    # Apple Photos heart/Favorites flag
    row["apple_favorite"] = 1 if getattr(photo, "favorite", False) else 0

    return row


# ---------------------------------------------------------------------------
# Album sync helper
# ---------------------------------------------------------------------------


def sync_photo_albums(photo, photo_db_id: int, db: Database, dry_run: bool) -> None:
    """
    Upsert album membership rows for one osxphotos PhotoInfo object, and
    tombstone any rows for albums the photo is no longer in.

    Uses photo.album_info (list of AlbumInfo objects with .title and .uuid).
    photo.albums returns plain strings and must not be used here.

    Filters to user-created albums only when album_type is available (osxphotos
    >= some future version); when the attribute is absent (osxphotos 0.75.x),
    album_info already excludes smart/system albums so all entries are accepted.
    """
    album_infos = getattr(photo, "album_info", []) or []
    seen_folder_uuids: set[str] = set()
    seen_album_uuids: set[str] = set()  # track all accepted albums for removal detection

    for album in album_infos:
        album_type = getattr(album, "album_type", "Album")
        if album_type != "Album":
            continue

        seen_album_uuids.add(album.uuid)  # collect before dry_run check

        if dry_run:
            log.debug("  [dry-run] album: %r (%s)", album.title, album.uuid)
            continue

        # Walk folder ancestry from root to immediate parent.
        ancestors: list = []
        node = getattr(album, "parent", None)
        while node is not None:
            ancestors.append(node)
            node = getattr(node, "parent", None)
        ancestors.reverse()  # root first

        parent_db_id: int | None = None
        for folder in ancestors:
            if folder.uuid not in seen_folder_uuids:
                db.upsert_folder(folder.uuid, folder.title, parent_id=parent_db_id)
                seen_folder_uuids.add(folder.uuid)
            row = db.conn.execute(
                "SELECT id FROM folders WHERE apple_uuid = ?", (folder.uuid,)
            ).fetchone()
            parent_db_id = row["id"]

        album_id = db.upsert_album(album.uuid, album.title, folder_id=parent_db_id)
        db.upsert_photo_album(photo_db_id, album_id)  # also clears any removed_at tombstone

    # Removal detection: tombstone rows for albums this photo is no longer in.
    # Only compare against non-tombstoned rows (already-tombstoned rows are pending sync).
    stored_rows = db.conn.execute(
        """SELECT pa.album_id, a.apple_uuid, pa.flickr_pushed
           FROM photo_albums pa
           JOIN albums a ON a.id = pa.album_id
           WHERE pa.photo_id = ? AND pa.removed_at IS NULL""",
        (photo_db_id,),
    ).fetchall()

    for row in stored_rows:
        if row["apple_uuid"] not in seen_album_uuids:
            if dry_run:
                log.debug(
                    "  [dry-run] photo_id=%s would be removed from album %s",
                    photo_db_id,
                    row["apple_uuid"],
                )
            elif row["flickr_pushed"]:
                db.mark_photo_album_removed(photo_db_id, row["album_id"])
                log.debug(
                    "photo_id=%s removed from album_id=%s — tombstoned (was pushed to Flickr)",
                    photo_db_id,
                    row["album_id"],
                )
            else:
                db.delete_photo_album_row(photo_db_id, row["album_id"])
                log.debug(
                    "photo_id=%s removed from album_id=%s — deleted (never pushed)",
                    photo_db_id,
                    row["album_id"],
                )


def sync_deleted_albums(photosdb, db: Database, dry_run: bool) -> int:
    """
    Detect albums deleted from Apple Photos and mark them for Flickr photoset cleanup.

    Compares all album UUIDs from osxphotos against stored album rows and tombstones
    any that have disappeared. Includes a plausibility guard: if osxphotos returns
    fewer than 50% of the stored baseline, aborts to prevent mass false-positives
    from transient osxphotos failures.

    Note: the 50% threshold may abort legitimately for very small libraries (e.g.,
    deleting 1 of 2 albums). Blue Pearmain users have large libraries in practice;
    if this ever matters, add an absolute minimum-difference floor alongside the %.

    Returns the count of albums newly marked deleted.
    """
    try:
        current_album_infos = photosdb.album_info
    except Exception as e:
        log.warning("sync_deleted_albums: could not fetch album list from osxphotos: %s", e)
        return 0

    current_uuids = {a.uuid for a in current_album_infos}

    stored_count = db.conn.execute(
        "SELECT COUNT(*) AS n FROM albums WHERE deleted_at IS NULL"
    ).fetchone()["n"]

    if stored_count > 0 and len(current_uuids) < stored_count * 0.5:
        log.warning(
            "sync_deleted_albums: plausibility guard triggered — "
            "osxphotos returned %d albums but DB has %d non-deleted; "
            "aborting to prevent false deletions",
            len(current_uuids),
            stored_count,
        )
        return 0

    stored_albums = db.conn.execute(
        "SELECT id, apple_uuid, name FROM albums WHERE deleted_at IS NULL"
    ).fetchall()

    marked = 0
    for row in stored_albums:
        if row["apple_uuid"] not in current_uuids:
            if dry_run:
                log.info(
                    "  [dry-run] album %r (%s) would be marked deleted",
                    row["name"],
                    row["apple_uuid"],
                )
            else:
                db.mark_album_deleted(row["id"])
                log.info("album %r (%s) marked deleted", row["name"], row["apple_uuid"])
            marked += 1

    return marked


# ---------------------------------------------------------------------------
# Matching logic
# ---------------------------------------------------------------------------


def normalise_dt(dt_str: str | None) -> str | None:
    """
    Strip timezone info and sub-second precision from a datetime string,
    returning 'YYYY-MM-DD HH:MM:SS' for comparison.
    Handles both ISO8601 and Flickr-style 'YYYY-MM-DD HH:MM:SS' strings.
    """
    if not dt_str:
        return None
    # Truncate at the dot (sub-seconds) or +/- (timezone)
    for sep in (".", "+", "-"):
        if sep in dt_str[10:]:  # only look after the date part
            dt_str = dt_str[: 10 + dt_str[10:].index(sep)]
    return dt_str[:19].replace("T", " ")  # always YYYY-MM-DD HH:MM:SS


def normalise_dt_plus1(dt_str: str | None) -> str | None:
    """
    Return the normalised timestamp incremented by one second, or None.

    Flickr rounds sub-second EXIF times to the nearest second while Apple
    Photos truncates them.  A photo with date_taken 20:14:50.941 therefore
    normalises to 20:14:50 on the Photos side but appears as 20:14:51 on
    Flickr.  Checking dt+1 catches this systematic off-by-one.
    """
    dt = normalise_dt(dt_str)
    if not dt:
        return None
    try:
        return (datetime.fromisoformat(dt) + timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def normalise_dt_plus2(dt_str: str | None) -> str | None:
    """
    Return the normalised timestamp incremented by two seconds, or None.

    Some Flickr uploads exhibit a 2-second offset rather than 1-second:
    observed consistently for HEIC photos where sub-second rounding produces
    an extra second of drift through Flickr's processing pipeline.
    """
    dt = normalise_dt(dt_str)
    if not dt:
        return None
    try:
        return (datetime.fromisoformat(dt) + timedelta(seconds=2)).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def normalise_dt_localise(dt_str: str | None, tz=None) -> str | None:
    """
    Like normalise_dt, but converts timezone-aware strings to the target timezone
    before stripping the offset.

    Flickr stores date_taken as EXIF local time (no timezone). Photos records
    scanned while the daemon ran in UTC have +00:00 offsets; their wall-clock
    hours differ from Flickr's by the UTC offset. Converting to the machine's
    local timezone (tz=None) before comparison makes them match.

    Pass an explicit timezone for deterministic tests.
    """
    if not dt_str:
        return None
    try:
        dt = datetime.fromisoformat(dt_str)
        if dt.tzinfo is not None:
            dt = dt.astimezone(tz).replace(tzinfo=None)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return normalise_dt(dt_str)


def find_flickr_match(photo_row: dict, db: Database) -> list[dict]:
    """
    Find Flickr DB records that match a Photos record.
    Returns a list of candidates (may be >1 for duplicate uploads).

    Matching strategy:
      1. Exact date_taken match (to the second, timezone-normalised)
      2. Among those, prefer records with matching GPS (within ~100m)
    """
    dt = normalise_dt(photo_row.get("date_taken"))
    if not dt:
        return []

    dt1 = normalise_dt_plus1(photo_row.get("date_taken"))
    dt2 = normalise_dt_plus2(photo_row.get("date_taken"))
    patterns = [f"{dt}%"] + ([f"{dt1}%"] if dt1 else []) + ([f"{dt2}%"] if dt2 else [])
    if len(patterns) > 1:
        placeholders = " OR ".join("date_taken LIKE ?" for _ in patterns)
        rows = db.conn.execute(
            f"SELECT * FROM photos WHERE ({placeholders}) AND uuid IS NULL",
            patterns,
        ).fetchall()
    else:
        rows = db.conn.execute(
            "SELECT * FROM photos WHERE date_taken LIKE ? AND uuid IS NULL",
            (f"{dt}%",),
        ).fetchall()

    if not rows:
        return []

    candidates = [dict(r) for r in rows]

    # If GPS available on both sides, prefer closest match
    lat = photo_row.get("latitude")
    lon = photo_row.get("longitude")
    if lat and lon and len(candidates) > 1:
        from db.db import haversine_m

        def dist(c):
            if c.get("latitude") and c.get("longitude"):
                return haversine_m(lat, lon, c["latitude"], c["longitude"])
            return float("inf")

        candidates.sort(key=dist)

    return candidates


# ---------------------------------------------------------------------------
# Enrichment: merge Photos data into a DB record
# ---------------------------------------------------------------------------


def build_enriched_row(
    photo_row: dict,
    existing: dict,
    zones: list[dict],
    self_name: str,
    person_policies: dict[str, str] | None = None,
    db: "Database | None" = None,
) -> dict:
    """
    Merge Photos metadata into an existing Flickr DB record.
    Re-runs privacy classification with the fuller data.
    Returns the updated row dict (does not write to DB).
    """
    merged = {k: v for k, v in existing.items() if k != "id"}

    # Fields we always take from Photos (more authoritative than Flickr)
    for field in (
        "uuid",
        "original_filename",
        "date_taken",
        "date_added_photos",
        "date_analyzed",
        "camera_make",
        "camera_model",
        "lens_model",
        "apple_labels",
        "apple_persons",
        "apple_named_faces",
        "apple_unknown_faces",
        "apple_human_count",
        "apple_ai_caption",
        "apple_ai_caption_conf",
        "apple_aesthetic_score",
        "fingerprint",
        "width",
        "height",
        "photos_title",
        "photos_description",
        "photos_tags",
        "photos_tags_hash",
        "meta_synced_photos_at",
    ):
        if photo_row.get(field) is not None:
            merged[field] = photo_row[field]

    # Location: Photos GPS is usually more precise than Flickr's
    if photo_row.get("latitude") is not None:
        merged["latitude"] = photo_row["latitude"]
        merged["longitude"] = photo_row["longitude"]

    # Place fields
    for field in (
        "place_city",
        "place_state",
        "place_country",
        "place_country_code",
        "place_address",
        "place_neighborhood",
        "place_ishome",
    ):
        if photo_row.get(field) is not None:
            merged[field] = photo_row[field]

    # Geocoder fill-in: use Nominatim to fill any missing place fields from GPS coordinates
    _PLACE_FIELDS = ("place_city", "place_state", "place_country", "place_neighborhood")
    if (
        db is not None
        and merged.get("latitude") is not None
        and merged.get("longitude") is not None
        and any(merged.get(f) is None for f in _PLACE_FIELDS)
    ):
        from geocoder import reverse_geocode  # deferred import — poller path

        result = reverse_geocode(merged["latitude"], merged["longitude"], db)
        if result.place:
            merged["place_city"] = merged.get("place_city") or result.place.city
            merged["place_state"] = merged.get("place_state") or result.place.state
            merged["place_country"] = merged.get("place_country") or result.place.country
            merged["place_country_code"] = (
                merged.get("place_country_code") or result.place.country_code
            )
            merged["place_neighborhood"] = (
                merged.get("place_neighborhood") or result.place.neighborhood
            )
            merged["place_address"] = merged.get("place_address") or result.place.address

    # Screenshot / selfie → auto_private unless already reviewed
    is_screenshot = photo_row.get("_is_screenshot", False)
    merged["is_screenshot"] = 1 if is_screenshot else 0
    if is_screenshot and existing.get("privacy_state") not in (
        "approved_public",
        "keep_private",
        "already_public",
        "approved_friends",
        "approved_family",
        "approved_friends_family",
    ):
        merged["privacy_state"] = "auto_private"
        merged["privacy_reason"] = "screenshot"
        merged["proposed_tags"] = []
        return merged

    # Re-run privacy classifier with enriched data
    # Only update state if not already human-reviewed
    if existing.get("privacy_state") not in (
        "approved_public",
        "keep_private",
        "already_public",
        "skipped",
        "approved_friends",
        "approved_family",
        "approved_friends_family",
    ):
        state, reason = classify(
            merged, zones, self_name=self_name, person_policies=person_policies
        )
        merged["privacy_state"] = state
        merged["privacy_reason"] = reason

    # Re-propose tags with enriched data
    merged["proposed_tags"] = propose_tags(merged)

    return merged


# ---------------------------------------------------------------------------
# Main scan loop
# ---------------------------------------------------------------------------


def scan(
    library_path: str,
    db: Database,
    since: datetime | None,
    dry_run: bool,
    self_name: str,
) -> tuple[int, int, int, int, int, int]:
    """
    Scan the Photos library and sync to DB.

    Returns (scanned, matched, enriched, inserted, linked, deleted) counts.
    `deleted` is always 0 for incremental scans (only runs during --all).
    """
    try:
        import osxphotos
    except ImportError:
        log.error("osxphotos is not installed. Run: uv tool install osxphotos")
        sys.exit(1)

    log.info(f"Opening Photos library: {library_path}")
    photosdb = osxphotos.PhotosDB(dbfile=library_path)

    zones = db.active_zones()
    person_policies = db.get_person_policies()
    scanned = 0
    matched = 0
    enriched = 0
    inserted = 0
    linked = 0  # Photos-only records late-linked to a Flickr record
    deleted = 0

    # Build a query — osxphotos supports filtering by date
    if since:
        log.info(f"Scanning photos added/modified since {since.date()}")
        photos = photosdb.photos(from_date=since)
    else:
        log.info("Scanning all photos in library")
        photos = photosdb.photos()

    total = len(photos)
    log.info(f"Found {total} photos to process")

    for i, photo in enumerate(photos, 1):
        if i % 500 == 0:
            log.info(f"  Progress: {i}/{total}")

        scanned += 1
        photo_row = photos_record_to_db(photo)

        # Check if already in DB by UUID
        existing_by_uuid = db.get_photo_by_uuid(photo.uuid)

        if existing_by_uuid:
            # Always sync album membership — it can change independently of ML analysis
            sync_photo_albums(photo, existing_by_uuid["id"], db, dry_run)

            # If this record has no Flickr link yet, try to find one now.
            # This handles the common case where the photo was scanned into
            # Apple Photos before the Flickr iOS app uploaded it — at scan
            # time there was no Flickr record to match against, so the record
            # was stored as Photos-only.  On subsequent scans we retry here.
            if not existing_by_uuid.get("flickr_id"):
                candidates = find_flickr_match(photo_row, db)
                if candidates:
                    primary = candidates[0]
                    log.debug(
                        "Late-linking %s (id=%s) → flickr:%s (id=%s)",
                        photo.original_filename,
                        existing_by_uuid["id"],
                        primary["flickr_id"],
                        primary["id"],
                    )
                    if not dry_run:
                        db.merge_flickr_into_photos(primary["id"], existing_by_uuid["id"])
                        # Refresh so the re-enrichment step below sees the flickr_id
                        existing_by_uuid = db.get_photo(existing_by_uuid["id"]) or existing_by_uuid
                    linked += 1

            # Skip full re-enrichment only when Apple's ML analysis AND the
            # Photos metadata cache are both unchanged since last scan.
            analysis_unchanged = existing_by_uuid.get("date_analyzed") == photo_row.get(
                "date_analyzed"
            )
            photos_cache_fresh = (
                existing_by_uuid.get("meta_synced_photos_at") is not None
                and existing_by_uuid.get("photos_tags_hash") == photo_row.get("photos_tags_hash")
                and existing_by_uuid.get("photos_title") == photo_row.get("photos_title")
            )
            if analysis_unchanged and photos_cache_fresh:
                if not dry_run:
                    db.apply_scanner_rating(
                        existing_by_uuid["id"], photo_row.get("apple_favorite", 0)
                    )
                continue
            enriched_row = build_enriched_row(
                photo_row,
                existing_by_uuid,
                zones,
                self_name,
                person_policies=person_policies,
                db=db,
            )
            if not dry_run:
                db.upsert_photo(enriched_row)
                db.apply_scanner_rating(existing_by_uuid["id"], photo_row.get("apple_favorite", 0))
            enriched += 1
            continue

        # Try to match against Flickr records
        candidates = find_flickr_match(photo_row, db)

        if candidates:
            matched += 1
            # Handle duplicates: link first candidate, flag others
            primary = candidates[0]
            enriched_row = build_enriched_row(
                photo_row, primary, zones, self_name, person_policies=person_policies, db=db
            )

            if not dry_run:
                row_id = db.upsert_photo(enriched_row)
                sync_photo_albums(photo, row_id, db, dry_run)
                db.apply_scanner_rating(row_id, photo_row.get("apple_favorite", 0))

            # Flag additional duplicate Flickr records
            for dup in candidates[1:]:
                log.debug(
                    f"  Duplicate Flickr upload: {dup['flickr_id']} "
                    f"(same date as {primary['flickr_id']})"
                )
                if not dry_run:
                    db.set_privacy_state(
                        dup["id"],
                        "auto_private",
                        f"duplicate of flickr:{primary['flickr_id']}",
                    )

            state = enriched_row.get("privacy_state", "?")
            reason = enriched_row.get("privacy_reason", "")
            tags = enriched_row.get("proposed_tags", [])
            log.debug(
                f"  Matched {photo.original_filename} → "
                f"flickr:{primary['flickr_id']} | {state} | tags: {tags[:5]}"
            )

        else:
            # No Flickr match yet — insert as Photos-only record
            # Privacy classify with what we have
            is_screenshot = photo_row.pop("_is_screenshot", False)
            photo_row.pop("_is_selfie", None)
            photo_row.pop("_is_live", None)
            apple_favorite_photos_only = photo_row.pop("apple_favorite", 0)
            photo_row["is_screenshot"] = 1 if is_screenshot else 0

            if is_screenshot:
                photo_row["privacy_state"] = "auto_private"
                photo_row["privacy_reason"] = "screenshot"
                photo_row["proposed_tags"] = []
            else:
                state, reason = classify(
                    photo_row, zones, self_name=self_name, person_policies=person_policies
                )
                photo_row["privacy_state"] = state
                photo_row["privacy_reason"] = reason
                photo_row["proposed_tags"] = propose_tags(photo_row)

            if not dry_run:
                row_id = db.upsert_photo(photo_row)
                sync_photo_albums(photo, row_id, db, dry_run)
                db.apply_scanner_rating(row_id, apple_favorite_photos_only)
            inserted += 1

    if since is None:
        deleted = sync_deleted_photos(photosdb, db, dry_run)

    # Detect albums deleted from Apple Photos. Unlike sync_deleted_photos, this runs
    # on every scan (including incremental). photosdb.album_info always returns the
    # full album list regardless of the since= filter, so incremental scans are safe.
    sync_deleted_albums(photosdb, db, dry_run)
    return scanned, matched, enriched, inserted, linked, deleted


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def sync_deleted_photos(photosdb, db: Database, dry_run: bool) -> int:
    """Delete Photos-only DB records whose UUID is no longer in the Photos library.

    Safe to call only during --all scans: photosdb.photos() must return the full
    library (no date filter) so that absence of a UUID is meaningful.

    Returns the count of records deleted (or would-be deleted in dry-run).
    """
    all_photos = photosdb.photos()

    if len(all_photos) == 0:
        log.error(
            "sync_deleted_photos: osxphotos returned 0 photos — "
            "aborting (plausibility guard: empty result indicates library read failure)"
        )
        return 0

    current_uuids: set[str] = {p.uuid for p in all_photos}

    rows = db.conn.execute(
        "SELECT id, uuid FROM photos WHERE uuid IS NOT NULL AND flickr_id IS NULL"
    ).fetchall()

    if not rows:
        log.info("sync_deleted_photos: no Photos-only records to check")
        return 0

    to_delete = [r for r in rows if r["uuid"] not in current_uuids]

    if not to_delete:
        log.info("sync_deleted_photos: all %d Photos-only records still present", len(rows))
        return 0

    deletion_ratio = len(to_delete) / len(rows)
    if len(rows) >= 10 and deletion_ratio > 0.10:
        log.warning(
            "sync_deleted_photos: would delete %d/%d Photos-only records (%.0f%%) — "
            "exceeds 10%% threshold, aborting. Investigate and re-run if intentional.",
            len(to_delete),
            len(rows),
            deletion_ratio * 100,
        )
        return 0

    for row in to_delete:
        log.info(
            "sync_deleted_photos: %s uuid=%s id=%d",
            "would delete" if dry_run else "deleting",
            row["uuid"],
            row["id"],
        )
        if not dry_run:
            db.delete_photo(row["id"])

    if not dry_run:
        db.conn.commit()

    log.info(
        "sync_deleted_photos: %s %d record(s)",
        "dry-run, would delete" if dry_run else "deleted",
        len(to_delete),
    )
    return len(to_delete)


def backfill_dimensions(db, library) -> int:
    """
    Update width/height for all Apple-Photos-matched rows that are missing
    dimensions. Useful after migrate_002 adds the columns to an existing DB.

    Returns the number of rows updated.
    """
    import logging

    log = logging.getLogger(__name__)

    rows = db.conn.execute("""
        SELECT id, uuid FROM photos
        WHERE uuid IS NOT NULL
          AND (width IS NULL OR height IS NULL)
    """).fetchall()

    if not rows:
        log.info("No rows need dimension backfill.")
        return 0

    log.info("Backfilling dimensions for %d photos …", len(rows))
    updated = 0

    # Build uuid→photo map from the library
    uuid_map = {p.uuid: p for p in library}

    for row in rows:
        photo = uuid_map.get(row["uuid"])
        if photo is None:
            continue
        w = getattr(photo, "width", None)
        h = getattr(photo, "height", None)
        if w and h:
            db.conn.execute(
                "UPDATE photos SET width = ?, height = ? WHERE id = ?", (w, h, row["id"])
            )
            updated += 1

    db.conn.commit()
    log.info("Backfill complete: %d rows updated.", updated)
    return updated


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Blue Pearmain scanner — sync Apple Photos → local DB"
    )
    parser.add_argument("--config", default="config/config.yml", help="Path to config.yml")
    parser.add_argument("--all", action="store_true", help="Scan entire library")
    parser.add_argument("--days", type=int, default=7, help="Days to look back (default 7)")
    parser.add_argument("--dry-run", action="store_true", help="Don't write to DB")
    parser.add_argument("--verbose", action="store_true", help="Debug logging")
    parser.add_argument("--library", default=None, help="Override Photos library path")
    args = parser.parse_args()

    config_path = Path(args.config)
    config = load_config(config_path)
    setup_logging(config, args.verbose)

    log.info("Blue Pearmain scanner starting")

    # Database
    db_path = Path(config["database"]["path"]).expanduser()
    db = Database(db_path)

    # Photos library path
    library_path = args.library or config.get("photos_library", {}).get("path", "")
    library_path = str(Path(library_path).expanduser())
    if not Path(library_path).exists():
        log.error(f"Photos library not found: {library_path}")
        log.error("Set photos_library.path in config.yml")
        sys.exit(1)

    self_name = config.get("photos_library", {}).get("self_name", "")

    # Determine scan window
    since: datetime | None = None
    if not args.all:
        since = datetime.now(timezone.utc) - timedelta(days=args.days)

        # Check last scanner run and use that if more recent
        last_run = db.conn.execute(
            "SELECT started_at FROM sync_runs "
            "WHERE source = 'photos_scan' AND status = 'complete' "
            "ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        if last_run:
            last_dt = datetime.fromisoformat(last_run["started_at"])
            if last_dt > since:
                since = last_dt - timedelta(minutes=5)
                log.info(f"Resuming from last scanner run: {last_dt.date()}")

    run_id = None if args.dry_run else db.start_sync_run("photos_scan")

    try:
        scanned, matched, enriched, inserted, linked, deleted = scan(
            library_path=library_path,
            db=db,
            since=since,
            dry_run=args.dry_run,
            self_name=self_name,
        )

        base_msg = (
            f"Scan complete: {scanned} scanned, {matched} matched to Flickr, "
            f"{linked} late-linked, {enriched} re-enriched, {inserted} Photos-only inserted"
        )
        if since is None:
            base_msg += f", {deleted} deleted (Photos removed)"
        log.info(base_msg)

        if run_id:
            db.finish_sync_run(
                run_id,
                status="complete",
                photos_seen=scanned,
                photos_new=inserted,
                photos_updated=matched + enriched + linked,
            )

    except KeyboardInterrupt:
        log.info("Interrupted.")
        if run_id:
            db.finish_sync_run(run_id, status="error", error_message="interrupted")
    except Exception as e:
        log.exception(f"Scanner error: {e}")
        if run_id:
            db.finish_sync_run(run_id, status="error", error_message=str(e))
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
