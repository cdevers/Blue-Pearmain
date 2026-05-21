"""
tag_writeback.py — write pushed_tags back to Photos.app as explicit keywords

Reads pushed_tags from the DB for Photos-linked records and merges them
into the photo's keyword list in Photos.app via photoscript. This makes
ML-derived tags visible in Smart Albums.

Photos.app must be running. Keywords are merged additively — existing
keywords are never removed.

Usage:
    python poller/tag_writeback.py --config config/config.yml
    python poller/tag_writeback.py --config config/config.yml --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    import photoscript
except ImportError:
    photoscript = None  # type: ignore[assignment]

from db.db import Database

log = logging.getLogger("blue-pearmain.tag-writeback")


def writeback(
    db: Database,
    dry_run: bool = False,
    limit: int = 500,
    verbose: bool = False,
    source: str = "pushed-tags",
) -> dict[str, int]:
    """
    Merge tag candidates into Photos.app keywords for all Photos-linked records.

    source: "pushed-tags" reads pushed_tags column; "proposed-tags" reads proposed_tags.
    Returns: {ok, updated, not_found, errors}
    """
    tag_col = "pushed_tags" if source == "pushed-tags" else "proposed_tags"
    rows = db.conn.execute(
        f"""SELECT id, uuid, {tag_col} AS tag_source
           FROM photos
           WHERE {tag_col} IS NOT NULL
             AND uuid IS NOT NULL
           LIMIT ?""",
        (limit,),
    ).fetchall()

    totals: dict[str, int] = {"ok": 0, "updated": 0, "not_found": 0, "errors": 0}

    if not rows:
        return totals

    lib = photoscript.PhotosLibrary()

    for row in rows:
        uuid = row["uuid"]
        pushed = json.loads(row["tag_source"] or "[]")
        if not pushed:
            continue

        try:
            photos = list(lib.photos(uuid=[uuid]))
        except Exception as e:
            log.error(f"  {uuid}: lookup error — {e}")
            totals["errors"] += 1
            continue

        if not photos:
            log.debug(f"  {uuid}: not found in Photos.app")
            totals["not_found"] += 1
            continue

        photo = photos[0]
        try:
            current = sorted(photo.keywords)
            merged = sorted(set(current) | set(pushed))
            if merged == current:
                if verbose:
                    log.debug(f"  {uuid}: ok (no new keywords)")
                totals["ok"] += 1
            else:
                if not dry_run:
                    photo.keywords = merged
                    db.log_operation(
                        photo_id=int(row["id"]),
                        operation="tag_writeback",
                        target="photos_keywords",
                        old_value=None,
                        new_value=json.dumps(sorted(set(merged) - set(current))),
                        trigger="tag_writeback",
                        actor="bp",
                    )
                totals["updated"] += 1
                log.info(
                    f"  {uuid}: {'would add' if dry_run else 'added'} "
                    f"{sorted(set(merged) - set(current))}"
                )
        except Exception as e:
            log.error(f"  {uuid}: keyword write error — {e}")
            totals["errors"] += 1

    return totals


def setup_logging(verbose: bool) -> None:
    from poller.bp_logging import configure

    configure("tag-writeback", verbose)


def main() -> int:
    parser = argparse.ArgumentParser(description="Blue Pearmain tag write-back to Photos.app")
    parser.add_argument("--config", default="config/config.yml")
    parser.add_argument("--dry-run", action="store_true", help="Report without writing")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--source",
        choices=["pushed-tags", "proposed-tags"],
        default="pushed-tags",
    )
    args = parser.parse_args()

    setup_logging(args.verbose)

    if photoscript is None:
        log.error("photoscript is not installed. Run: pip install photoscript")
        return 1

    with open(args.config) as f:
        config = yaml.safe_load(f)

    db_path = Path(config["database"]["path"]).expanduser()
    db = Database(db_path)

    log.info("Blue Pearmain tag write-back starting")
    totals = writeback(
        db,
        dry_run=args.dry_run,
        limit=args.limit,
        verbose=args.verbose,
        source=args.source,
    )

    print(
        f"\n  ok={totals['ok']}"
        f"  updated={totals['updated']}"
        f"  not_found={totals['not_found']}"
        f"  errors={totals['errors']}"
    )
    if args.dry_run:
        print("  (dry-run — no keywords were written)")

    db.close()
    return 1 if totals["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
