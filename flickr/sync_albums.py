"""
flickr/sync_albums.py — batch sync Apple Photos album membership → Flickr photosets

Usage:
    python flickr/sync_albums.py --config config/config.yml [--dry-run] [--album NAME] [--limit N]
    python flickr/sync_albums.py --config config/config.yml --coalesce [--dry-run]

Or via bp CLI:
    bp sync-albums [--dry-run] [--album NAME] [--limit N]
    bp sync-albums --coalesce [--dry-run]
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
    parser.add_argument("--config", default="config/config.yml")
    parser.add_argument(
        "--dry-run", action="store_true", help="Show what would be pushed, don't write"
    )
    parser.add_argument("--album", default=None, help="Sync only this album name")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--coalesce",
        action="store_true",
        help=(
            "Detect duplicate photosets (same title, overlapping photo dates) and merge "
            "them into one. Use --dry-run to preview without making changes."
        ),
    )
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

    # Fetch all Flickr photosets once — used for both "adopt before create" prevention
    # and the optional coalesce step. Failure is non-fatal: prevention is skipped but
    # the rest of the sync continues normally.
    log.debug("fetching all Flickr photosets…")
    try:
        all_flickr_sets = flickr.list_photosets()
    except Exception as e:
        log.warning("could not fetch Flickr photoset list (adopt-before-create disabled): %s", e)
        all_flickr_sets = []

    # --coalesce: detect and merge duplicate photosets before pushing new photos
    if args.coalesce:
        from flickr.coalesce_sets import find_coalesce_candidates, coalesce_group

        candidates = find_coalesce_candidates(db, flickr, all_flickr_sets)
        if not candidates:
            log.info("coalesce: no duplicate photosets found")
        else:
            total_photos_moved = 0
            total_sets_deleted = 0
            for group in candidates:
                orphan_summary = ", ".join(
                    f"{o['id']} ({o['photos']} photos)" for o in group["orphans"]
                )
                log.info(
                    "coalesce candidate %r — canonical=%s (%s photos), orphans=[%s]%s",
                    group["title"],
                    group["canonical"]["id"],
                    group["canonical"]["photos"],
                    orphan_summary,
                    "  [dry-run]" if args.dry_run else "",
                )
                result = coalesce_group(db, flickr, group, dry_run=args.dry_run)
                total_photos_moved += result["photos_moved"]
                total_sets_deleted += result["sets_deleted"]

            if args.dry_run:
                log.info(
                    "coalesce: [dry-run] %d candidate group(s) found — "
                    "re-run without --dry-run to execute",
                    len(candidates),
                )
            else:
                log.info(
                    "coalesce: merged %d photo(s) across %d deleted orphan set(s)",
                    total_photos_moved,
                    total_sets_deleted,
                )
                # Refresh the set list after coalescing (canonical IDs may have changed)
                all_flickr_sets = flickr.list_photosets()

    # Build name→id map for "adopt before create" — picks the set with most photos
    # when duplicates remain (e.g. after a partial coalesce run).
    known_sets: dict[str, str] = {}
    known_sets_photos: dict[str, int] = {}
    for s in all_flickr_sets:
        title = str(s["title"])
        n = int(s["photos"])
        if title not in known_sets or n > known_sets_photos[title]:
            known_sets[title] = str(s["id"])
            known_sets_photos[title] = n

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
    added = 0
    skipped = 0
    failed = 0

    for photo_id in unique_photos:
        if args.dry_run:
            photo = db.get_photo(photo_id)
            flickr_id = photo.get("flickr_id") if photo else None
            if flickr_id:
                log.info(
                    "[dry-run] would push photo_id=%s flickr_id=%s to albums", photo_id, flickr_id
                )
                skipped += 1
            else:
                skipped += 1
            continue

        try:
            n = push_photo_to_albums(db, flickr, photo_id, known_sets=known_sets)
            added += n
            if n == 0:
                skipped += 1
        except Exception as e:
            log.error("sync-albums: unexpected error photo_id=%s: %s", photo_id, e)
            failed += 1

    albums_created = _count_created_sets(db) - albums_before
    print(
        f"albums created={albums_created}  photos added={added}  skipped={skipped}  failed={failed}"
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
    from flickr.flickr_client import FlickrError, FLICKR_ERR_NOT_FOUND

    rows = db.conn.execute(
        "SELECT id, name, flickr_set_id FROM albums WHERE flickr_set_id IS NOT NULL"
    ).fetchall()

    updated = 0
    cleared = 0
    for row in rows:
        if dry_run:
            log.info(
                "[dry-run] would update photoset title %r → %r", row["flickr_set_id"], row["name"]
            )
            updated += 1
            continue
        try:
            flickr.edit_photoset_meta(row["flickr_set_id"], row["name"])
            db.set_album_flickr_name(row["id"], row["name"])
            updated += 1
        except FlickrError as e:
            if e.code == FLICKR_ERR_NOT_FOUND:
                # Photoset was deleted on Flickr — clear the stale ID and reset
                # photo_albums so sync-albums recreates the photoset on the next run.
                n = db.conn.execute(
                    "UPDATE photo_albums SET flickr_pushed = 0 WHERE album_id = ?", (row["id"],)
                ).rowcount
                db.conn.execute(
                    "UPDATE albums SET flickr_set_id = NULL, flickr_name = NULL WHERE id = ?",
                    (row["id"],),
                )
                db.conn.commit()
                log.warning(
                    "photoset for album %r (id=%s) not found on Flickr — "
                    "cleared stale ID, reset %d photo push(es); will recreate on next sync-albums",
                    row["name"],
                    row["flickr_set_id"],
                    n,
                )
                cleared += 1
            else:
                log.warning("failed to update photoset title for album %r: %s", row["name"], e)
        except Exception as e:
            log.warning("failed to update photoset title for album %r: %s", row["name"], e)

    if dry_run:
        log.info("sync-album-titles: [dry-run] would-update=%d", updated)
    else:
        log.info("sync-album-titles: updated=%d  cleared-stale=%d", updated, cleared)
    return {"updated": updated, "cleared": cleared}


if __name__ == "__main__":
    sys.exit(main())
