"""
flickr/sync_albums.py — batch sync Apple Photos album membership → Flickr photosets

Usage:
    python flickr/sync_albums.py --config config/config.yml [--dry-run] [--album NAME] [--limit N]

Or via bp CLI:
    bp sync-albums [--dry-run] [--album NAME] [--limit N]
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

log = logging.getLogger("blue-pearmain.sync_albums")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sync Apple Photos album membership → Flickr photosets"
    )
    parser.add_argument("--config",  default="config/config.yml")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be pushed, don't write")
    parser.add_argument("--album",   default=None,        help="Sync only this album name")
    parser.add_argument("--limit",   type=int, default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    try:
        with open(args.config) as f:
            config = yaml.safe_load(f)
    except Exception as e:
        log.error("Cannot read config: %s", e)
        return 2

    try:
        from db.db import Database
        db = Database(Path(config["database"]["path"]).expanduser())
    except Exception as e:
        log.error("Cannot open database: %s", e)
        return 2

    try:
        from flickr.flickr_client import FlickrClient
        flickr = FlickrClient.from_config(config)
    except Exception as e:
        log.error("Cannot initialise Flickr client: %s", e)
        return 2

    limit = args.limit or 500
    pending = db.get_pending_album_pushes(limit=limit)

    if args.album:
        pending = [r for r in pending if r["album_name"] == args.album]

    # Deduplicate by photo_id so we call push_photo_to_albums once per photo
    seen_photo_ids: set[int] = set()
    unique_photos: list[int] = []
    for row in pending:
        pid = row["photo_id"]
        if pid not in seen_photo_ids:
            seen_photo_ids.add(pid)
            unique_photos.append(pid)

    from flickr.album_pusher import push_photo_to_albums

    albums_before = _count_created_sets(db)
    added   = 0
    skipped = 0
    failed  = 0

    for photo_id in unique_photos:
        if args.dry_run:
            photo = db.get_photo(photo_id)
            flickr_id = photo.get("flickr_id") if photo else None
            if flickr_id:
                log.info("[dry-run] would push photo_id=%s flickr_id=%s to albums", photo_id, flickr_id)
                skipped += 1
            else:
                skipped += 1
            continue

        try:
            n = push_photo_to_albums(db, flickr, photo_id)
            added += n
            if n == 0:
                skipped += 1
        except Exception as e:
            log.error("sync-albums: unexpected error photo_id=%s: %s", photo_id, e)
            failed += 1

    albums_created = _count_created_sets(db) - albums_before
    print(
        f"albums created={albums_created}  "
        f"photos added={added}  "
        f"skipped={skipped}  "
        f"failed={failed}"
    )

    sync_album_titles(db, flickr, dry_run=args.dry_run)

    db.close()
    return 1 if failed else 0


def _count_created_sets(db) -> int:
    row = db.conn.execute(
        "SELECT COUNT(*) AS n FROM albums WHERE flickr_set_id IS NOT NULL"
    ).fetchone()
    return row["n"] if row else 0


def sync_album_titles(db, flickr, dry_run: bool = False) -> dict:
    """Push current album names to Flickr photoset titles for all pushed albums."""
    rows = db.conn.execute(
        "SELECT name, flickr_set_id FROM albums WHERE flickr_set_id IS NOT NULL"
    ).fetchall()

    updated = 0
    for row in rows:
        if dry_run:
            log.info("[dry-run] would update photoset title %r → %r", row["flickr_set_id"], row["name"])
            updated += 1
            continue
        try:
            flickr.edit_photoset_meta(row["flickr_set_id"], row["name"])
            updated += 1
        except Exception as e:
            log.warning("failed to update photoset title for album %r: %s", row["name"], e)

    log.info("sync-album-titles: updated=%d", updated)
    return {"updated": updated}


if __name__ == "__main__":
    sys.exit(main())
