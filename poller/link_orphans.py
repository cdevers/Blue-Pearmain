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
from collections import defaultdict
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from db.db import Database
from datetime import datetime, timedelta

from scanner import normalise_dt, normalise_dt_localise

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
    the Photos record has no flickr_id yet.

    Matching is done in Python (not SQL) to avoid a full cross-product join
    across hundreds of thousands of rows.  Both sides are loaded into memory,
    normalised to second-precision, and matched via a hash dict — O(n + m)
    rather than O(n * m).

    Where a Photos record matches multiple Flickr records at the same second,
    the Flickr record with the smallest id is used (same tie-breaking as the
    scanner).  Each Flickr record is matched at most once.
    """
    log.info("Loading Flickr-only records …")
    flickr_rows = db.conn.execute(
        "SELECT id, date_taken FROM photos WHERE uuid IS NULL AND flickr_id IS NOT NULL"
    ).fetchall()
    log.info("  %d Flickr-only records loaded.", len(flickr_rows))

    # Build dict: normalised_dt → sorted list of Flickr row ids
    flickr_by_dt: dict[str, list[int]] = defaultdict(list)
    for r in flickr_rows:
        dt = normalise_dt(r["date_taken"])
        if dt:
            flickr_by_dt[dt].append(r["id"])
    for lst in flickr_by_dt.values():
        lst.sort()

    log.info("Loading Photos-only records …")
    photos_rows = db.conn.execute(
        "SELECT id, date_taken FROM photos WHERE uuid IS NOT NULL AND flickr_id IS NULL ORDER BY id"
    ).fetchall()
    log.info("  %d Photos-only records loaded.", len(photos_rows))

    pairs: list[tuple[int, int]] = []
    claimed: set[int] = set()   # Flickr ids already assigned to a pair

    total = len(photos_rows)
    for i, row in enumerate(photos_rows):
        if len(pairs) >= limit:
            break
        if i > 0 and i % 10_000 == 0:
            log.info("  Matching: %d / %d scanned, %d pairs found …", i, total, len(pairs))

        # Build candidate keys from BOTH the stripped form (for Photos records
        # already stored in local time with a tz offset) AND the localised form
        # (for Photos records stored in UTC by a daemon running in UTC).
        # Deduplication means no extra false positives when both forms agree.
        # Each base is also tried at +1s and +2s (Flickr sub-second rounding).
        raw_bases: set[str] = set()
        for base_fn in (normalise_dt, normalise_dt_localise):
            b = base_fn(row["date_taken"])
            if b:
                raw_bases.add(b)

        if not raw_bases:
            continue

        candidate_keys: list[str] = []
        seen_keys: set[str] = set()
        for base in sorted(raw_bases):   # deterministic order
            for delta in (0, 1, 2):
                try:
                    key = (datetime.fromisoformat(base) + timedelta(seconds=delta)).strftime("%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue
                if key not in seen_keys:
                    candidate_keys.append(key)
                    seen_keys.add(key)

        candidates = []
        seen_fids: set[int] = set()
        for key in candidate_keys:
            for fid in flickr_by_dt.get(key, []):
                if fid not in seen_fids:
                    candidates.append(fid)
                    seen_fids.add(fid)
        for flickr_id in candidates:
            if flickr_id not in claimed:
                pairs.append((flickr_id, row["id"]))
                claimed.add(flickr_id)
                break

    return pairs


def link_orphans(db: Database, dry_run: bool, limit: int) -> tuple[int, int]:
    """Find and merge orphaned pairs.  Returns (linked, failed) counts."""
    pairs = find_orphan_pairs(db, limit)
    if not pairs:
        log.info("No linkable orphan pairs found.")
        return 0, 0

    log.info(
        "Found %d orphan pair(s) to link%s.",
        len(pairs),
        " (dry-run — no writes)" if dry_run else "",
    )

    if dry_run:
        return len(pairs), 0

    linked = 0
    failed = 0
    total  = len(pairs)
    for i, (flickr_rec_id, photos_rec_id) in enumerate(pairs, 1):
        if i % 1_000 == 0 or i == total:
            log.info("  Merging: %d / %d …", i, total)

        log.debug("  pair photos_id=%d → flickr_rec_id=%d", photos_rec_id, flickr_rec_id)

        try:
            ok = db.merge_flickr_into_photos(flickr_rec_id, photos_rec_id)
            if ok:
                linked += 1
            else:
                log.warning(
                    "  Skipped pair (photos=%d, flickr=%d): preconditions not met",
                    photos_rec_id, flickr_rec_id,
                )
                failed += 1
        except Exception as exc:
            log.error(
                "  Failed to link pair (photos=%d, flickr=%d): %s",
                photos_rec_id, flickr_rec_id, exc,
            )
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
