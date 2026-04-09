"""
poller.py — Flickr → local DB sync for Blue Pearmain

Polls Flickr for new or recently updated photos, runs the privacy
classifier and tag proposer, downloads thumbnails, and writes
everything to the local database.

Usage:
    python poller/poller.py --config config/config.yml
    python poller/poller.py --config config/config.yml --backfill --days 90
    python poller/poller.py --config config/config.yml --no-thumbs
    python poller/poller.py --config config/config.yml --dry-run

Options:
    --config PATH       Path to config.yml (required)
    --backfill          Process older photos, not just recent ones
    --days N            With --backfill: how many days back to go (default: 30)
    --since TIMESTAMP   Override: poll from this Unix timestamp forward
    --no-thumbs         Skip thumbnail downloads
    --dry-run           Fetch and classify but don't write to DB or Flickr
    --verbose           Extra logging
"""

from __future__ import annotations

import argparse
import concurrent.futures
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

# Allow running from repo root or poller/ directory
sys.path.insert(0, str(Path(__file__).parent.parent))

from db.db import Database
from flickr.flickr_client import FlickrClient, FlickrError
from analyzer.privacy import classify
from analyzer.tagger import propose_tags

log = logging.getLogger("blue-pearmain.poller")


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
# Flickr photo record → flat DB dict
# ---------------------------------------------------------------------------

EXTRA_FIELDS = (
    "date_upload,date_taken,geo,tags,machine_tags,"
    "url_sq,url_t,url_s,url_m,url_l,url_o,"
    "original_format,media,description,license,owner_name"
)


def flickr_photo_to_db(photo: dict, info: dict | None = None) -> dict:
    """
    Convert a Flickr API photo record (from search/recentlyUpdated) plus
    optional getInfo response into a flat dict suitable for db.upsert_photo().

    The 'photo' arg is from the paginated list response (has url_* fields).
    The 'info' arg is from flickr.photos.getInfo (has full location, tags, etc.)
    """
    # Basic identity
    row: dict = {
        "flickr_id":     photo.get("id"),
        "flickr_secret": photo.get("secret"),
        "flickr_server": photo.get("server"),
        "flickr_farm":   photo.get("farm"),
    }

    # Timestamps
    date_upload = photo.get("dateupload")
    if date_upload:
        row["date_uploaded_flickr"] = datetime.fromtimestamp(
            int(date_upload), tz=timezone.utc
        ).isoformat()

    date_taken = photo.get("datetaken")
    if date_taken:
        row["date_taken"] = date_taken  # already a string from Flickr

    # Title / description
    row["title"] = photo.get("title", "")

    desc = photo.get("description", {})
    if isinstance(desc, dict):
        row["flickr_description"] = desc.get("_content", "")
    elif isinstance(desc, str):
        row["flickr_description"] = desc

    # Tags from the list response (space-separated string)
    raw_tags = photo.get("tags", "")
    if isinstance(raw_tags, str) and raw_tags:
        row["flickr_tags"] = raw_tags.split()
    else:
        row["flickr_tags"] = []

    # Location from list response (may be absent for private photos)
    lat = photo.get("latitude")
    lon = photo.get("longitude")
    if lat and lon:
        try:
            row["latitude"]  = float(lat)
            row["longitude"] = float(lon)
        except (TypeError, ValueError):
            pass

    # Thumbnail URLs — prefer url_l (Large, 1024px), fall back to url_m
    row["thumbnail_url_l"] = photo.get("url_l", "")
    row["thumbnail_url_m"] = photo.get("url_m", "")

    # Original filename and format from extras
    row["original_format"] = photo.get("originalformat", "")

    # Enrich with getInfo data if available
    if info:
        _enrich_from_info(row, info)

    return row


def _enrich_from_info(row: dict, info: dict):
    """Pull richer fields from flickr.photos.getInfo response."""
    photo = info.get("photo", {})

    # Owner
    owner = photo.get("owner", {})
    row["flickr_owner_nsid"] = owner.get("nsid", "")

    # Camera (from EXIF — not always present in getInfo)
    # We get this from the scanner side via osxphotos; skip here.

    # Location
    location = photo.get("location", {})
    if location:
        try:
            row["latitude"]  = float(location.get("latitude",  row.get("latitude",  0)))
            row["longitude"] = float(location.get("longitude", row.get("longitude", 0)))
        except (TypeError, ValueError):
            pass
        row["place_city"]    = (location.get("locality")   or {}).get("_content", "")
        row["place_state"]   = (location.get("region")     or {}).get("_content", "")
        row["place_country"] = (location.get("country")    or {}).get("_content", "")

    # Tags from getInfo are richer (have id, author, raw value)
    tags_container = photo.get("tags", {})
    if isinstance(tags_container, dict):
        tag_items = tags_container.get("tag", [])
        row["flickr_tags"] = [t.get("raw", t.get("_content", "")) for t in tag_items]

    # Dates
    dates = photo.get("dates", {})
    if dates.get("taken"):
        row["date_taken"] = dates["taken"]

    upload_ts = dates.get("posted")
    if upload_ts:
        row["date_uploaded_flickr"] = datetime.fromtimestamp(
            int(upload_ts), tz=timezone.utc
        ).isoformat()

    # Description
    desc = (photo.get("description") or {}).get("_content", "")
    if desc:
        row["flickr_description"] = desc

    # Visibility
    visibility = photo.get("visibility", {})
    row["flickr_is_public"] = int(visibility.get("ispublic", 0))


# ---------------------------------------------------------------------------
# Privacy classification for Flickr-only records
# ---------------------------------------------------------------------------

def classify_flickr_record(row: dict, zones: list[dict]) -> tuple[str, str]:
    """
    Run the privacy classifier on a Flickr-sourced record.

    For Flickr-only records (not yet matched to Photos), we have:
    - GPS coordinates (if geotagged)
    - Flickr tags
    - No Apple ML labels, persons, or face data yet

    The classifier will mostly fall through to 'candidate_public'
    (no people signals) or geofence matches. Face/people signals
    come later once the scanner matches to Apple Photos.
    """
    return classify(row, zones)


# ---------------------------------------------------------------------------
# Thumbnail download (optionally threaded)
# ---------------------------------------------------------------------------

def download_thumb(client: FlickrClient, row: dict, thumb_root: Path) -> str | None:
    """
    Download the best available thumbnail for a photo.
    Returns the local path on success, None on failure.
    """
    flickr_id = row.get("flickr_id")
    if not flickr_id:
        return None

    url = row.get("thumbnail_url_l") or row.get("thumbnail_url_m")
    if not url:
        return None

    # Shard by first two chars of flickr_id to avoid huge flat directories
    shard = flickr_id[:2]
    dest = thumb_root / shard / f"{flickr_id}.jpg"

    if dest.exists():
        return str(dest)

    success = client.download_thumbnail(url, str(dest))
    return str(dest) if success else None


# ---------------------------------------------------------------------------
# Main poll loop
# ---------------------------------------------------------------------------

def poll(
    client: FlickrClient,
    db: Database,
    thumb_root: Path | None,
    min_ts: int,
    dry_run: bool = False,
    fetch_info: bool = False,
    max_thumbs_workers: int = 4,
) -> tuple[int, int, int]:
    """
    Poll Flickr from min_ts forward, paginating through all results.
    Returns (seen, new, updated) counts.
    """
    zones = db.active_zones()
    seen = new = updated = 0
    page = 1

    while True:
        log.info(f"Fetching page {page} (from {datetime.fromtimestamp(min_ts, tz=timezone.utc).date()})")
        try:
            resp = client.get_recent_uploads(
                min_upload_date=min_ts,
                page=page,
                per_page=500,
                extras=EXTRA_FIELDS,
            )
        except FlickrError as e:
            log.error(f"Flickr API error on page {page}: {e}")
            break

        photos_page = resp.get("photos", {})
        items = photos_page.get("photo", [])
        total_pages = int(photos_page.get("pages", 1))

        log.info(f"  Page {page}/{total_pages}: {len(items)} photos")

        thumb_futures: list[concurrent.futures.Future] = []

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_thumbs_workers) as executor:
            for photo in items:
                seen += 1
                flickr_id = photo["id"]

                # Optionally fetch full info (slower but richer)
                info = None
                if fetch_info:
                    try:
                        info = client.get_photo_info(flickr_id, photo.get("secret"))
                    except FlickrError as e:
                        log.warning(f"  getInfo failed for {flickr_id}: {e}")

                row = flickr_photo_to_db(photo, info)

                # Privacy classification
                state, reason = classify_flickr_record(row, zones)
                row["privacy_state"]  = state
                row["privacy_reason"] = reason

                # Already public on Flickr? Mark it.
                if photo.get("ispublic") == 1 or row.get("flickr_is_public") == 1:
                    row["privacy_state"]  = "already_public"
                    row["privacy_reason"] = "public on Flickr"

                # Tag proposals
                proposed = propose_tags(row)
                row["proposed_tags"] = proposed

                # Serialise flickr_tags for storage (not a schema column — fold into proposed)
                row.pop("flickr_tags", None)
                row.pop("thumbnail_url_l", None)
                row.pop("thumbnail_url_m", None)
                row.pop("flickr_description", None)
                row.pop("flickr_is_public", None)
                row.pop("flickr_owner_nsid", None)
                row.pop("title", None)
                row.pop("original_format", None)

                if dry_run:
                    log.debug(f"  [dry-run] {flickr_id}: {state} — {reason} — tags: {proposed[:5]}")
                    continue

                # Check if already in DB
                existing = db.get_photo_by_flickr_id(flickr_id)
                if existing:
                    # Update metadata but preserve any review decisions
                    db.upsert_photo(row)
                    updated += 1
                else:
                    db.upsert_photo(row)
                    new += 1

                # Queue thumbnail download
                if thumb_root:
                    thumb_url = photo.get("url_l") or photo.get("url_m")
                    if thumb_url:
                        fut = executor.submit(
                            download_thumb, client,
                            {"flickr_id": flickr_id,
                             "thumbnail_url_l": photo.get("url_l", ""),
                             "thumbnail_url_m": photo.get("url_m", "")},
                            thumb_root,
                        )
                        thumb_futures.append((flickr_id, fut))

            # Collect thumbnail results
            for flickr_id, fut in thumb_futures:
                try:
                    path = fut.result(timeout=30)
                    if path and not dry_run:
                        db.conn.execute(
                            "UPDATE photos SET thumbnail_path = ? WHERE flickr_id = ?",
                            (path, flickr_id),
                        )
                        db.conn.commit()
                except Exception as e:
                    log.warning(f"  Thumbnail failed for {flickr_id}: {e}")

        if page >= total_pages:
            break
        page += 1

    return seen, new, updated


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Blue Pearmain poller — sync Flickr → local DB"
    )
    parser.add_argument("--config",   default="config/config.yml", help="Path to config.yml")
    parser.add_argument("--backfill", action="store_true",          help="Poll historical photos, not just recent")
    parser.add_argument("--days",     type=int, default=30,         help="With --backfill: days to look back (default 30)")
    parser.add_argument("--since",    type=int, default=None,       help="Override: start from this Unix timestamp")
    parser.add_argument("--no-thumbs",action="store_true",          help="Skip thumbnail downloads")
    parser.add_argument("--dry-run",  action="store_true",          help="Classify but don't write to DB")
    parser.add_argument("--verbose",  action="store_true",          help="Debug logging")
    parser.add_argument("--fetch-info", action="store_true",        help="Fetch full getInfo for each photo (slower, richer)")
    args = parser.parse_args()

    config_path = Path(args.config)
    config = load_config(config_path)
    setup_logging(config, args.verbose)

    log.info("Blue Pearmain poller starting")

    # Database
    db_path = Path(config["database"]["path"]).expanduser()
    db = Database(db_path)

    # Flickr client
    try:
        client = FlickrClient.from_config(config)
        client.test_login()
        log.info(f"Flickr auth OK (user: {config['flickr'].get('user_nsid', '?')})")
    except Exception as e:
        log.error(f"Flickr auth failed: {e}")
        log.error("Run flickr/flickr_auth.py first to authorise.")
        sys.exit(1)

    # Thumbnail root
    thumb_root: Path | None = None
    if not args.no_thumbs:
        thumb_root = Path(config["thumbnails"]["path"]).expanduser()
        thumb_root.mkdir(parents=True, exist_ok=True)

    # Determine start timestamp
    if args.since:
        min_ts = args.since
        log.info(f"Polling from specified timestamp: {datetime.fromtimestamp(min_ts, tz=timezone.utc)}")
    elif args.backfill:
        cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
        min_ts = int(cutoff.timestamp())
        log.info(f"Backfill mode: polling last {args.days} days (from {cutoff.date()})")
    else:
        # Use timestamp of last successful sync run, or default to 24h ago
        last_run = db.conn.execute(
            "SELECT started_at FROM sync_runs WHERE source = 'flickr_poll' "
            "AND status = 'complete' ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        if last_run:
            last_dt = datetime.fromisoformat(last_run["started_at"])
            # Subtract 5 minutes to avoid missing photos at the boundary
            min_ts = int((last_dt - timedelta(minutes=5)).timestamp())
            log.info(f"Resuming from last sync: {last_dt.isoformat()}")
        else:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
            min_ts = int(cutoff.timestamp())
            log.info(f"No previous sync found — defaulting to last 24 hours")

    # Run
    run_id = None if args.dry_run else db.start_sync_run("flickr_poll")

    try:
        seen, new, updated = poll(
            client=client,
            db=db,
            thumb_root=thumb_root,
            min_ts=min_ts,
            dry_run=args.dry_run,
            fetch_info=args.fetch_info,
        )
        log.info(f"Poll complete: {seen} seen, {new} new, {updated} updated")

        if run_id:
            db.finish_sync_run(
                run_id,
                status="complete",
                photos_seen=seen,
                photos_new=new,
                photos_updated=updated,
            )

    except KeyboardInterrupt:
        log.info("Interrupted.")
        if run_id:
            db.finish_sync_run(run_id, status="error", error_message="interrupted")
    except Exception as e:
        log.exception(f"Poller error: {e}")
        if run_id:
            db.finish_sync_run(run_id, status="error", error_message=str(e))
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
