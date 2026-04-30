"""
flickr/sync_metadata.py — batch sync Flickr metadata → Apple Photos

Compares Flickr title/description/tags against Apple Photos, writes
non-conflicting Flickr values into Photos, and records conflicts for
review in the UI.

Usage:
    python flickr/sync_metadata.py --config config/config.yml [OPTIONS]

Or via bp CLI:
    bp sync-metadata [OPTIONS]
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
    parser = argparse.ArgumentParser(
        description="Sync Flickr metadata → Apple Photos"
    )
    parser.add_argument("--config",         default="config/config.yml")
    parser.add_argument("--dry-run",        action="store_true",
                        help="Compare and log; do not write to Photos or DB")
    parser.add_argument("--limit",          type=int, default=500,
                        help="Process at most N photos (default 500)")
    parser.add_argument("--photo-id",       type=int, default=None,
                        help="Process only this DB photo_id")
    parser.add_argument("--conflicts-only", action="store_true",
                        help="Detect and record conflicts only; skip Photos writes")
    parser.add_argument("--verbose",        action="store_true")
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

    library_path = config.get("photos_library", {}).get("path", "")
    if not library_path:
        log.error("photos_library.path not set in config")
        return 2

    try:
        from flickr.flickr_client import FlickrClient
        flickr = FlickrClient.from_config(config)
    except Exception as e:
        log.error("Cannot initialise Flickr client: %s", e)
        return 2

    # Build photo_id list
    if args.photo_id:
        photo_ids = [args.photo_id]
    else:
        rows = db.conn.execute(
            """SELECT id FROM photos
               WHERE flickr_id IS NOT NULL AND uuid IS NOT NULL
               ORDER BY id
               LIMIT ?""",
            (args.limit,),
        ).fetchall()
        photo_ids = [r["id"] for r in rows]

    if not photo_ids:
        print("written=0  conflicts=0  skipped=0  failed=0")
        db.close()
        return 0

    # --conflicts-only: treat as dry_run for the write step but still record conflicts
    effective_dry_run = args.dry_run or args.conflicts_only

    from flickr.metadata_puller import pull_batch
    totals = pull_batch(
        db, flickr, photo_ids,
        library_path=library_path,
        dry_run=effective_dry_run,
        verbose=args.verbose,
    )

    print(
        f"written={totals['written']}  "
        f"conflicts={totals['conflicts']}  "
        f"skipped={totals['skipped']}  "
        f"failed={totals['failed']}"
    )

    db.close()
    return 1 if totals["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
