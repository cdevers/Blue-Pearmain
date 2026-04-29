"""
link_orphans.py — batch-link Photos-only records to their Flickr counterparts

Fixes split photo records that were created when a photo was scanned from
Apple Photos before its corresponding Flickr upload was polled.  The scanner
normally merges matching records on the fly, but any photo inserted as
Photos-only before the Flickr record existed stays orphaned forever unless
this tool (or a subsequent full scan) runs.

Usage:
    python poller/link_orphans.py --config config/config.yml
    python poller/link_orphans.py --config config/config.yml --dry-run
    python poller/link_orphans.py --config config/config.yml --limit 5000
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.db import Database

log = logging.getLogger("blue-pearmain.link-orphans")


def setup_logging(config: dict, verbose: bool) -> None:
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


def find_orphan_pairs(db: Database, limit: int) -> list[tuple[int, int]]:
    """
    Return (flickr_rec_id, photos_rec_id) pairs where timestamps match and
    the Photos record has no flickr_id yet.  Ordered by Photos record id so
    repeated runs are deterministic.

    Where a single Photos record matches multiple Flickr records (timestamp
    collision), the Flickr record with the smallest id is preferred — the
    same tie-breaking the scanner uses (candidates are returned in DB order
    and the first is taken as primary).
    """
    rows = db.conn.execute(
        """SELECT p1.id AS photos_id, MIN(p2.id) AS flickr_id_row
           FROM photos p1
           JOIN photos p2 ON
             replace(substr(p1.date_taken, 1, 19), 'T', ' ') = substr(p2.date_taken, 1, 19)
             AND p1.uuid IS NOT NULL AND p1.flickr_id IS NULL
             AND p2.uuid IS NULL     AND p2.flickr_id IS NOT NULL
           GROUP BY p1.id
           ORDER BY p1.id
           LIMIT ?""",
        (limit,),
    ).fetchall()
    return [(row["flickr_id_row"], row["photos_id"]) for row in rows]


def link_orphans(db: Database, dry_run: bool, limit: int) -> tuple[int, int]:
    """
    Find and merge orphaned pairs.  Returns (linked, failed) counts.
    """
    pairs = find_orphan_pairs(db, limit)
    if not pairs:
        log.info("No linkable orphan pairs found.")
        return 0, 0

    log.info("Found %d orphan pair(s) to link%s.", len(pairs), " (dry-run)" if dry_run else "")

    linked = 0
    failed = 0
    for flickr_rec_id, photos_rec_id in pairs:
        flickr_row  = db.conn.execute("SELECT flickr_id, date_taken FROM photos WHERE id = ?", (flickr_rec_id,)).fetchone()
        photos_row  = db.conn.execute("SELECT original_filename, date_taken FROM photos WHERE id = ?", (photos_rec_id,)).fetchone()

        log.debug(
            "  Linking photos_id=%d (%s %s) → flickr_id=%d (%s)",
            photos_rec_id,
            photos_row["original_filename"] if photos_row else "?",
            photos_row["date_taken"][:19] if photos_row else "",
            flickr_rec_id,
            flickr_row["flickr_id"] if flickr_row else "?",
        )

        if dry_run:
            linked += 1
            continue

        try:
            ok = db.merge_flickr_into_photos(flickr_rec_id, photos_rec_id)
            if ok:
                linked += 1
            else:
                log.warning("  merge_flickr_into_photos returned False for pair (%d, %d)", flickr_rec_id, photos_rec_id)
                failed += 1
        except Exception as exc:
            log.error("  Failed to link pair (%d, %d): %s", flickr_rec_id, photos_rec_id, exc)
            failed += 1

    return linked, failed


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Blue Pearmain link-orphans — merge split Photos/Flickr records"
    )
    parser.add_argument("--config",  default="config/config.yml", help="Path to config.yml")
    parser.add_argument("--dry-run", action="store_true", help="Identify pairs but don't write")
    parser.add_argument("--limit",   type=int, default=100_000, help="Max pairs to process (default 100000)")
    parser.add_argument("--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"Config not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    with open(config_path) as f:
        config = yaml.safe_load(f)

    setup_logging(config, args.verbose)
    log.info("Blue Pearmain link-orphans starting%s", " (dry-run)" if args.dry_run else "")

    db_path = Path(config["database"]["path"]).expanduser()
    db = Database(db_path)

    try:
        linked, failed = link_orphans(db, dry_run=args.dry_run, limit=args.limit)
        verb = "Would link" if args.dry_run else "Linked"
        log.info("%s %d pair(s);  failed=%d", verb, linked, failed)
        sys.exit(1 if failed else 0)
    finally:
        db.close()


if __name__ == "__main__":
    main()
