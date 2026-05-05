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
import hashlib
import html
import json
import logging
import sys
import time
import unicodedata
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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalise_tag(tag: str) -> str:
    # Flickr normalizes tags to alphanumeric-only ("close-up" → "closeup",
    # "new york" → "newyork"). Keep only isalnum() chars so hashes align.
    return "".join(
        c for c in unicodedata.normalize("NFC", tag.strip().casefold())
        if c.isalnum()
    )


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
# Flickr photo record → flat DB dict
# ---------------------------------------------------------------------------

EXTRA_FIELDS = (
    "date_upload,date_taken,geo,tags,machine_tags,"
    "url_sq,url_t,url_s,url_m,url_l,url_o,"
    "original_format,media,description,license,owner_name,last_update"
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

    # Title / description — HTML-decode: Flickr API returns entities like &amp; &quot;
    row["flickr_title"] = html.unescape(photo.get("title", "") or "")

    desc = photo.get("description", {})
    if isinstance(desc, dict):
        row["flickr_description"] = html.unescape(desc.get("_content", "") or "")
    elif isinstance(desc, str):
        row["flickr_description"] = html.unescape(desc)

    # Last-update timestamp (present when last_update is in extras)
    last_update = photo.get("lastupdate")
    if last_update:
        row["flickr_last_updated"] = datetime.fromtimestamp(
            int(last_update), tz=timezone.utc
        ).isoformat()

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

    # Title from getInfo is authoritative over the list response
    title = html.unescape((photo.get("title") or {}).get("_content", "") or "")
    if title:
        row["flickr_title"] = title

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

    last_update = dates.get("lastupdate")
    if last_update:
        row["flickr_last_updated"] = datetime.fromtimestamp(
            int(last_update), tz=timezone.utc
        ).isoformat()

    # Description
    desc = html.unescape((photo.get("description") or {}).get("_content", "") or "")
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
    return classify(row, zones, self_name="")


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
# Auto-push helpers
# ---------------------------------------------------------------------------

def _find_approved_photos_record(db, flickr_row: dict):
    """
    Look for a Photos-only DB record (no flickr_id) with privacy_state
    'approved_public' that matches this incoming Flickr upload by date_taken.

    Both sides are normalized to UTC seconds precision before comparison,
    handling the different formats Apple Photos and Flickr use:
      Apple Photos: 2026-04-07T16:35:09.679000-04:00  (ISO8601 + ms + tz)
      Flickr:       2023-05-06T16:34:28-04:00          (ISO8601, no ms)
                    2023-05-07 13:03:19                 (space format, no tz)

    Returns the DB record dict if found, else None.
    """
    import sys as _sys, os as _os
    _sys.path.insert(0, _os.path.dirname(__file__))
    from scanner import normalise_dt

    # Normalise the incoming Flickr date to UTC, truncated to seconds
    flickr_dt = normalise_dt(flickr_row.get("date_taken"))
    if not flickr_dt:
        return None
    flickr_dt_prefix = flickr_dt[:19]  # "YYYY-MM-DDTHH:MM:SS"

    # Fetch candidate approved Photos-only records within a 1-second window.
    # We can't do UTC normalisation in SQLite directly, so we pull candidates
    # by the date portion and then filter in Python.
    date_prefix = flickr_dt_prefix[:10]  # "YYYY-MM-DD"
    candidates = db.conn.execute(
        """SELECT * FROM photos
           WHERE flickr_id IS NULL
             AND privacy_state = 'approved_public'
             AND date_taken LIKE ?""",
        (f"{date_prefix}%",),
    ).fetchall()

    import json as _json
    for row in candidates:
        row_dt = normalise_dt(row["date_taken"])
        if row_dt and row_dt[:19] == flickr_dt_prefix:
            d = dict(row)
            for field in ("apple_labels", "apple_persons", "proposed_tags"):
                if isinstance(d.get(field), str):
                    try:    d[field] = _json.loads(d[field])
                    except (json.JSONDecodeError, TypeError, ValueError): d[field] = []
            return d
    return None


def _push_to_flickr(client, flickr_id: str, db_record: dict, db, dry_run: bool) -> int:
    """
    Push permissions and tags to Flickr for a newly matched approved photo.
    Records push state in the DB only for operations that succeed.
    Returns the number of failed operations (0 = all ok).
    """
    from analyzer.tagger import merge_tags
    errors = []

    try:
        client.set_permissions(flickr_id, is_public=1)
        db.conn.execute(
            "UPDATE photos SET perms_pushed_flickr = 1 WHERE flickr_id = ?",
            (flickr_id,)
        )
        log.info(f"  set_permissions OK for {flickr_id}")
    except FlickrError as e:
        log.error(f"  set_permissions failed for {flickr_id}: {e}")
        errors.append(str(e))

    tags = db_record.get("proposed_tags") or []
    if tags:
        try:
            client.add_tags(flickr_id, tags)
            db.conn.execute(
                "UPDATE photos SET tags_pushed_flickr = 1 WHERE flickr_id = ?",
                (flickr_id,)
            )
            log.info(f"  add_tags OK for {flickr_id} ({len(tags)} tags)")
        except FlickrError as e:
            from flickr.flickr_client import FLICKR_ERR_MAX_TAGS
            if e.code == FLICKR_ERR_MAX_TAGS:
                log.warning(
                    f"  add_tags skipped for {flickr_id}: "
                    f"Flickr 75-tag limit reached — tags not pushed"
                )
                # Not counted as an error — perms were still set correctly
            else:
                log.error(f"  add_tags failed for {flickr_id}: {e}")
                errors.append(str(e))

    if not errors:
        db.conn.commit()
    return len(errors)


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
    seen = new = updated = push_errors = 0
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

                # Privacy classification — skip if a human has already decided
                existing_for_review = db.get_photo_by_flickr_id(flickr_id)
                if existing_for_review and existing_for_review.get("review_decision"):
                    # Preserve the human decision; only update sync metadata
                    row["privacy_state"]  = existing_for_review["privacy_state"]
                    row["privacy_reason"] = existing_for_review["privacy_reason"]
                elif photo.get("ispublic") == 1 or row.get("flickr_is_public") == 1:
                    row["privacy_state"]  = "already_public"
                    row["privacy_reason"] = "public on Flickr"
                else:
                    state, reason = classify_flickr_record(row, zones)
                    row["privacy_state"]  = state
                    row["privacy_reason"] = reason

                # Tag proposals
                proposed = propose_tags(row)
                row["proposed_tags"] = proposed

                # Cache Flickr metadata for the sync engine
                tags = row.get("flickr_tags") or []
                row["flickr_tags"]           = json.dumps(tags)
                row["flickr_tags_hash"]      = _compute_tags_hash(tags)
                row["meta_synced_flickr_at"] = _now_iso()

                # Drop transient fields that have no DB column
                for _key in ("thumbnail_url_l", "thumbnail_url_m",
                             "flickr_is_public", "flickr_owner_nsid", "original_format"):
                    row.pop(_key, None)

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
                    # New Flickr upload — check if there's a matching Photos-only
                    # record that was already approved. If so, auto-push to Flickr.
                    matched = _find_approved_photos_record(db, row)
                    if matched:
                        log.info(
                            f"  Auto-push: {flickr_id} matched approved Photos record "
                            f"(uuid={matched.get('uuid')}) — pushing to Flickr"
                        )
                        # Merge Flickr identity into the Photos record.
                        # Pass uuid so upsert_photo finds the existing row via
                        # the uuid unique constraint rather than inserting a new one.
                        row["privacy_state"]  = "approved_public"
                        row["privacy_reason"] = "matched approved Photos record"
                        row["uuid"]           = matched["uuid"]
                        db.upsert_photo(row)
                        push_errors += _push_to_flickr(client, flickr_id, matched, db, dry_run=False)
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

    return seen, new, updated, push_errors


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------

def _validate_config(config: dict, config_path: str):
    """Fail fast with a readable message if required keys are missing."""
    required = {
        "flickr.api_key":            "Flickr API key",
        "flickr.api_secret":         "Flickr API secret",
        "flickr.oauth_token":        "Flickr OAuth token (run flickr/flickr_auth.py)",
        "flickr.oauth_token_secret": "Flickr OAuth token secret (run flickr/flickr_auth.py)",
        "database.path":             "SQLite database path",
    }
    errors = []
    for dotted_key, description in required.items():
        parts = dotted_key.split(".")
        val = config
        try:
            for part in parts:
                val = val[part]
        except (KeyError, TypeError):
            val = None
        if not val:
            errors.append(f"  {dotted_key}: {description}")
    if errors:
        print(f"\nConfiguration errors in {config_path}:")
        for e in errors:
            print(e)
        print("\nCopy config/config.example.yml to config/config.yml and fill in missing values.")
        sys.exit(1)


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

    # Validate config before doing anything else
    _validate_config(config, str(config_path))

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
        seen, new, updated, push_errors = poll(
            client=client,
            db=db,
            thumb_root=thumb_root,
            min_ts=min_ts,
            dry_run=args.dry_run,
            fetch_info=args.fetch_info,
        )
        summary = f"Poll complete: {seen} seen, {new} new, {updated} updated"
        if push_errors:
            summary += f", {push_errors} push error(s)"
        log.info(summary)

        if run_id:
            db.finish_sync_run(
                run_id,
                status="complete" if not push_errors else "complete_with_errors",
                photos_seen=seen,
                photos_new=new,
                photos_updated=updated,
            )

        if push_errors:
            sys.exit(1)

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
