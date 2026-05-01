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
import json
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
    return unicodedata.normalize("NFC", tag.strip().casefold())


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


def setup_logging(config: dict, verbose: bool):
    level = logging.DEBUG if verbose else getattr(
        logging, config.get("logging", {}).get("level", "INFO").upper(), logging.INFO
    )
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    log_file = config.get("logging", {}).get("file")
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file))
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        handlers=handlers,
    )


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
        row["camera_make"]  = exif.camera_make  or ""
        row["camera_model"] = exif.camera_model or ""
        row["lens_model"]   = exif.lens_model   or ""

    # Location
    if photo.latitude is not None:
        row["latitude"]  = photo.latitude
        row["longitude"] = photo.longitude

    place = photo.place
    if place:
        addr = place.address or {}
        row["place_city"]         = getattr(addr, "city",           None) or ""
        row["place_state"]        = getattr(addr, "state_province", None) or ""
        row["place_country"]      = getattr(addr, "country",        None) or ""
        row["place_country_code"] = getattr(addr, "iso_country_code", None) or ""
        row["place_address"]      = place.address_str or ""
        row["place_ishome"]       = 1 if place.ishome else 0

        # Neighbourhood — first entry of additional_city_info if present
        names = place.names or {}
        extra_city = getattr(names, "additional_city_info", None) or []
        if extra_city:
            row["place_neighborhood"] = extra_city[0]

    # Photos metadata cache (title, description, keywords)
    photos_title       = getattr(photo, "title",       None) or ""
    photos_description = getattr(photo, "description", None) or ""
    photos_tags        = list(getattr(photo, "keywords", None) or [])
    row["photos_title"]          = photos_title
    row["photos_description"]    = photos_description
    row["photos_tags"]           = photos_tags  # auto-serialised to JSON by upsert_photo
    row["photos_tags_hash"]      = _compute_tags_hash(photos_tags)
    row["meta_synced_photos_at"] = datetime.now(timezone.utc).isoformat()

    # Apple ML labels
    labels = list(photo.labels or [])
    row["apple_labels"] = labels

    # Apple ML persons
    persons = list(photo.persons or [])
    row["apple_persons"]      = persons
    row["apple_named_faces"]  = sum(1 for p in persons if p and p != "_UNKNOWN_")
    row["apple_unknown_faces"] = sum(1 for p in persons if p == "_UNKNOWN_")

    # Human count from media_analysis
    if isinstance(ma, dict):
        humans = ma.get("humans") or []
        row["apple_human_count"] = len(humans)

        # Apple AI caption
        caption_data = ma.get("image_caption") or {}
        if isinstance(caption_data, dict):
            row["apple_ai_caption"]      = caption_data.get("imageCaptionText", "")
            row["apple_ai_caption_conf"] = caption_data.get("imageCaptionConfidence", 0.0)

    # Apple aesthetic score
    score = photo.score
    if score:
        row["apple_aesthetic_score"] = getattr(score, "overall", None)

    # Special media type flags — store in privacy_reason if screenshot
    row["_is_screenshot"] = bool(getattr(photo, "screenshot", False))
    row["_is_selfie"]     = bool(getattr(photo, "selfie", False))
    row["_is_live"]       = bool(getattr(photo, "live_photo", False))

    # Fingerprint for matching
    row["fingerprint"] = getattr(photo, "fingerprint", None) or ""

    # Dimensions
    row["width"]  = getattr(photo, "width",  None)
    row["height"] = getattr(photo, "height", None)

    return row


# ---------------------------------------------------------------------------
# Album sync helper
# ---------------------------------------------------------------------------

def sync_photo_albums(photo, photo_db_id: int, db: Database, dry_run: bool) -> None:
    """
    Upsert album membership rows for one osxphotos PhotoInfo object.

    Uses photo.album_info (list of AlbumInfo objects with .title and .uuid).
    photo.albums returns plain strings and must not be used here.

    Filters to user-created albums only when album_type is available (osxphotos
    >= some future version); when the attribute is absent (osxphotos 0.75.x),
    album_info already excludes smart/system albums so all entries are accepted.
    """
    album_infos = getattr(photo, "album_info", []) or []
    for album in album_infos:
        # album_type is only present in newer osxphotos versions.
        # Default to "Album" (pass through) when missing.
        album_type = getattr(album, "album_type", "Album")
        if album_type != "Album":
            continue
        if dry_run:
            log.debug("  [dry-run] album: %r (%s)", album.title, album.uuid)
            continue
        album_id = db.upsert_album(album.uuid, album.title)
        db.upsert_photo_album(photo_db_id, album_id)


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
            dt_str = dt_str[:10 + dt_str[10:].index(sep)]
    return dt_str[:19].replace("T", " ")  # always YYYY-MM-DD HH:MM:SS


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
) -> dict:
    """
    Merge Photos metadata into an existing Flickr DB record.
    Re-runs privacy classification with the fuller data.
    Returns the updated row dict (does not write to DB).
    """
    merged = {k: v for k, v in existing.items() if k != "id"}

    # Fields we always take from Photos (more authoritative than Flickr)
    for field in (
        "uuid", "original_filename", "date_taken", "date_added_photos",
        "date_analyzed", "camera_make", "camera_model", "lens_model",
        "apple_labels", "apple_persons", "apple_named_faces",
        "apple_unknown_faces", "apple_human_count",
        "apple_ai_caption", "apple_ai_caption_conf",
        "apple_aesthetic_score", "fingerprint",
        "width", "height",
        "photos_title", "photos_description", "photos_tags",
        "photos_tags_hash", "meta_synced_photos_at",
    ):
        if photo_row.get(field) is not None:
            merged[field] = photo_row[field]

    # Location: Photos GPS is usually more precise than Flickr's
    if photo_row.get("latitude"):
        merged["latitude"]  = photo_row["latitude"]
        merged["longitude"] = photo_row["longitude"]

    # Place fields
    for field in (
        "place_city", "place_state", "place_country",
        "place_country_code", "place_address",
        "place_neighborhood", "place_ishome",
    ):
        if photo_row.get(field) is not None:
            merged[field] = photo_row[field]

    # Screenshot / selfie → auto_private unless already reviewed
    is_screenshot = photo_row.get("_is_screenshot", False)
    if is_screenshot and existing.get("privacy_state") not in (
        "approved_public", "keep_private", "already_public"
    ):
        merged["privacy_state"]  = "auto_private"
        merged["privacy_reason"] = "screenshot"
        merged["proposed_tags"]  = []
        return merged

    # Re-run privacy classifier with enriched data
    # Only update state if not already human-reviewed
    if existing.get("privacy_state") not in (
        "approved_public", "keep_private", "already_public", "skipped"
    ):
        state, reason = classify(merged, zones, self_name=self_name)
        merged["privacy_state"]  = state
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
) -> tuple[int, int, int, int]:
    """
    Scan the Photos library and sync to DB.
    Returns (scanned, matched, enriched, inserted) counts.
    """
    try:
        import osxphotos
    except ImportError:
        log.error("osxphotos is not installed. Run: uv tool install osxphotos")
        sys.exit(1)

    log.info(f"Opening Photos library: {library_path}")
    photosdb = osxphotos.PhotosDB(dbfile=library_path)

    zones     = db.active_zones()
    scanned   = 0
    matched   = 0
    enriched  = 0
    inserted  = 0
    linked    = 0  # Photos-only records late-linked to a Flickr record

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
            analysis_unchanged = (
                existing_by_uuid.get("date_analyzed") == photo_row.get("date_analyzed")
            )
            photos_cache_fresh = (
                existing_by_uuid.get("meta_synced_photos_at") is not None
                and existing_by_uuid.get("photos_tags_hash") == photo_row.get("photos_tags_hash")
                and existing_by_uuid.get("photos_title") == photo_row.get("photos_title")
            )
            if analysis_unchanged and photos_cache_fresh:
                continue
            enriched_row = build_enriched_row(
                photo_row, existing_by_uuid, zones, self_name
            )
            if not dry_run:
                db.upsert_photo(enriched_row)
            enriched += 1
            continue

        # Try to match against Flickr records
        candidates = find_flickr_match(photo_row, db)

        if candidates:
            matched += 1
            # Handle duplicates: link first candidate, flag others
            primary = candidates[0]
            enriched_row = build_enriched_row(photo_row, primary, zones, self_name)

            if not dry_run:
                row_id = db.upsert_photo(enriched_row)
                sync_photo_albums(photo, row_id, db, dry_run)

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

            state  = enriched_row.get("privacy_state", "?")
            reason = enriched_row.get("privacy_reason", "")
            tags   = enriched_row.get("proposed_tags", [])
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

            if is_screenshot:
                photo_row["privacy_state"]  = "auto_private"
                photo_row["privacy_reason"] = "screenshot"
                photo_row["proposed_tags"]  = []
            else:
                state, reason = classify(photo_row, zones, self_name=self_name)
                photo_row["privacy_state"]  = state
                photo_row["privacy_reason"] = reason
                photo_row["proposed_tags"]  = propose_tags(photo_row)

            if not dry_run:
                row_id = db.upsert_photo(photo_row)
                sync_photo_albums(photo, row_id, db, dry_run)
            inserted += 1

    return scanned, matched, enriched, inserted, linked


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


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
                "UPDATE photos SET width = ?, height = ? WHERE id = ?",
                (w, h, row["id"])
            )
            updated += 1

    db.conn.commit()
    log.info("Backfill complete: %d rows updated.", updated)
    return updated


def main():
    parser = argparse.ArgumentParser(
        description="Blue Pearmain scanner — sync Apple Photos → local DB"
    )
    parser.add_argument("--config",  default="config/config.yml", help="Path to config.yml")
    parser.add_argument("--all",     action="store_true",         help="Scan entire library")
    parser.add_argument("--days",    type=int, default=7,         help="Days to look back (default 7)")
    parser.add_argument("--dry-run", action="store_true",         help="Don't write to DB")
    parser.add_argument("--verbose", action="store_true",         help="Debug logging")
    parser.add_argument("--library", default=None,                help="Override Photos library path")
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
        scanned, matched, enriched, inserted, linked = scan(
            library_path=library_path,
            db=db,
            since=since,
            dry_run=args.dry_run,
            self_name=self_name,
        )

        log.info(
            f"Scan complete: {scanned} scanned, {matched} matched to Flickr, "
            f"{linked} late-linked, {enriched} re-enriched, {inserted} Photos-only inserted"
        )

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
