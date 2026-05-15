"""
flickr/sync_metadata.py — Phase 4 metadata sync engine

Reads flickr_tags / photos_tags from the DB cache (populated by `bp poll`
and `bp scan`), diffs them, classifies divergences, and writes proposals to
metadata_proposals. No Flickr API calls. No writes to Photos or Flickr.

Proposals are reviewed and applied in Phase 5 (`bp reconcile --fix`).

Usage:
    bp sync-metadata [OPTIONS]
    python flickr/sync_metadata.py --config config/config.yml [OPTIONS]

Options:
    --limit N          Process at most N photos from the drift filter (0 = all, default 0)
    --photo-id ID      Process only this DB photo_id
    --dry-run          Classify and log; do not write proposals or update harmonized_at
    --refresh-flickr   Refresh Flickr cache via API before running sync engine
                       (use when poller hasn't run recently)
    --verbose          Log every photo processed
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

log = logging.getLogger("blue-pearmain.sync_metadata")


def main() -> int:
    parser = argparse.ArgumentParser(description="Diff DB metadata caches and generate proposals")
    parser.add_argument("--config", default="config/config.yml")
    parser.add_argument(
        "--dry-run", action="store_true", help="Classify and log; do not write proposals"
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="Process at most N photos (0 = all drift-filtered)"
    )
    parser.add_argument("--photo-id", type=int, default=None, help="Process only this DB photo_id")
    parser.add_argument(
        "--refresh-flickr",
        action="store_true",
        help="Re-fetch Flickr metadata via API before syncing",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore drift filter; process all photos with warm caches",
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

    # --refresh-flickr: update the Flickr cache via live API before diffing
    if args.refresh_flickr:
        rc = _refresh_flickr_cache(db, config, args)
        if rc != 0:
            return rc

    # Build photo_id list via drift filter
    if args.photo_id:
        photo_ids = [args.photo_id]
    else:
        photo_ids = _select_drift_filtered(db, limit=args.limit, force=args.force)

    if not photo_ids:
        print("proposals=0  hash_matches=0  skipped=0  failed=0  (nothing in drift filter)")
        db.close()
        return 0

    log.info(
        "Running sync engine on %d photo%s%s",
        len(photo_ids),
        "s" if len(photo_ids) != 1 else "",
        " (dry-run)" if args.dry_run else "",
    )

    from flickr.metadata_puller import run_sync_engine

    totals = run_sync_engine(db, photo_ids, dry_run=args.dry_run, verbose=args.verbose)

    suffix = "  (dry-run)" if args.dry_run else ""
    print(
        f"proposals={totals['proposals']}  "
        f"hash_matches={totals['hash_matches']}  "
        f"skipped={totals['skipped']}  "
        f"failed={totals['failed']}"
        f"{suffix}"
    )

    db.close()
    return 1 if totals["failed"] else 0


def _select_drift_filtered(db, limit: int, force: bool = False) -> list[int]:
    """
    Return photo IDs where both caches are populated and harmonization is
    either never done or stale relative to the most recent cache update.
    When force=True, skip the harmonized_at staleness check and return all
    photos with warm caches.
    """
    drift_clause = (
        ""
        if force
        else """
          AND (
            meta_last_harmonized_at IS NULL
            OR meta_last_harmonized_at < MAX(
                COALESCE(flickr_last_updated, meta_synced_flickr_at),
                meta_synced_photos_at
            )
          )"""
    )
    query = f"""
        SELECT id FROM photos
        WHERE flickr_id IS NOT NULL
          AND uuid IS NOT NULL
          AND meta_synced_flickr_at IS NOT NULL
          AND meta_synced_photos_at IS NOT NULL
          AND (flickr_deleted IS NULL OR flickr_deleted = 0)
          {drift_clause}
        ORDER BY id
    """
    if limit and limit > 0:
        rows = db.conn.execute(query + " LIMIT ?", (limit,)).fetchall()
    else:
        rows = db.conn.execute(query).fetchall()
    return [r["id"] for r in rows]


def _refresh_flickr_cache(db, config: dict, args) -> int:
    """Re-fetch Flickr metadata for drift-filtered photos and update cache columns."""
    log.info("--refresh-flickr: fetching live Flickr metadata before sync engine")
    try:
        from flickr.flickr_client import FlickrClient

        flickr = FlickrClient.from_config(config)
    except Exception as e:
        log.error("Cannot initialise Flickr client: %s", e)
        return 2

    photo_ids = (
        _select_drift_filtered(db, limit=args.limit, force=args.force)
        if not args.photo_id
        else [args.photo_id]
    )
    if not photo_ids:
        return 0

    from flickr.metadata_puller import pull_batch

    library_path = str(Path(config.get("photos_library", {}).get("path", "")).expanduser())
    totals = pull_batch(db, flickr, photo_ids, library_path=library_path, dry_run=True)
    log.info(
        "--refresh-flickr done: cache_hits=%d/%d",
        totals.get("cache_hits", 0),
        len(photo_ids),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
