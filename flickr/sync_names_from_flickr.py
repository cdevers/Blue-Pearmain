"""
flickr/sync_names_from_flickr.py — sync Flickr photoset/Collection renames → Apple Photos

Usage:
    python flickr/sync_names_from_flickr.py --config config/config.yml [--dry-run]

Or via bp CLI:
    bp sync-names-from-flickr [--dry-run]

When a Flickr photoset or Collection is renamed directly on Flickr, this command detects
the change and renames the corresponding Apple Photos album or folder via AppleScript.
Requires Photos.app to be running. Photos wins on conflict (both sides renamed).
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

log = logging.getLogger("blue-pearmain.sync_names_from_flickr")


def _rename_photos_album(apple_uuid: str, new_name: str) -> bool:
    """Rename an Apple Photos album via AppleScript. Requires Photos.app to be running."""
    safe = new_name.replace("\\", "\\\\").replace('"', '\\"')
    script = f'tell application "Photos" to set name of album id "{apple_uuid}" to "{safe}"'
    r = subprocess.run(["osascript", "-e", script],
                       capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        log.warning("Photos rename failed for album %r: %s", new_name, r.stderr.strip())
    return r.returncode == 0


def _rename_photos_folder(apple_uuid: str, new_name: str) -> bool:
    """Rename an Apple Photos folder via AppleScript. Requires Photos.app to be running."""
    safe = new_name.replace("\\", "\\\\").replace('"', '\\"')
    script = f'tell application "Photos" to set name of folder id "{apple_uuid}" to "{safe}"'
    r = subprocess.run(["osascript", "-e", script],
                       capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        log.warning("Photos rename failed for folder %r: %s", new_name, r.stderr.strip())
    return r.returncode == 0


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def sync_names_from_flickr(db, flickr, dry_run: bool = False) -> dict:
    """
    Detect Flickr photoset/Collection renames and propagate them to Apple Photos.
    Returns {"albums_renamed": N, "albums_skipped": N, "folders_renamed": N, "folders_skipped": N}.
    """
    set_map = flickr.get_photosets_titled()

    try:
        col_map = flickr.get_collections_flat()
    except Exception as e:
        if "pro" in str(e).lower():
            log.info("sync-names-from-flickr: Flickr Collections require Pro — skipping folders")
        else:
            log.warning("sync-names-from-flickr: could not fetch collections: %s", e)
        col_map = {}

    albums_renamed = 0
    albums_skipped = 0

    album_rows = db.conn.execute(
        "SELECT id, apple_uuid, name, flickr_set_id, flickr_name "
        "FROM albums WHERE flickr_set_id IS NOT NULL"
    ).fetchall()

    for row in album_rows:
        flickr_title = set_map.get(row["flickr_set_id"])
        if flickr_title is None:
            log.debug("photoset %s not in Flickr list — skipping", row["flickr_set_id"])
            albums_skipped += 1
            continue

        baseline = row["flickr_name"]
        if baseline is None:
            log.debug("album %r has no flickr_name baseline — skipping", row["name"])
            albums_skipped += 1
            continue

        photos_name    = row["name"]
        photos_changed = photos_name != baseline
        flickr_changed = flickr_title != baseline

        if not flickr_changed:
            albums_skipped += 1
            continue

        if photos_changed:
            log.info(
                "conflict: album %r renamed on both sides (Photos=%r, Flickr=%r) — Photos wins",
                baseline, photos_name, flickr_title,
            )
            albums_skipped += 1
            continue

        # Only Flickr was renamed
        log.info(
            "%salbum %r → %r (Flickr-side rename)",
            "[dry-run] would rename " if dry_run else "renaming ",
            photos_name, flickr_title,
        )
        if dry_run:
            albums_renamed += 1
            continue

        if _rename_photos_album(row["apple_uuid"], flickr_title):
            db.conn.execute(
                "UPDATE albums SET name = ?, flickr_name = ?, updated_at = ? WHERE id = ?",
                (flickr_title, flickr_title, _now_iso(), row["id"]),
            )
            db.conn.commit()
            albums_renamed += 1
        else:
            albums_skipped += 1

    folders_renamed = 0
    folders_skipped = 0

    if col_map:
        folder_rows = db.conn.execute(
            "SELECT id, apple_uuid, name, flickr_collection_id, flickr_name "
            "FROM folders WHERE flickr_collection_id IS NOT NULL"
        ).fetchall()

        for row in folder_rows:
            flickr_title = col_map.get(row["flickr_collection_id"])
            if flickr_title is None:
                folders_skipped += 1
                continue

            baseline = row["flickr_name"]
            if baseline is None:
                folders_skipped += 1
                continue

            photos_name    = row["name"]
            photos_changed = photos_name != baseline
            flickr_changed = flickr_title != baseline

            if not flickr_changed:
                folders_skipped += 1
                continue

            if photos_changed:
                log.info(
                    "conflict: folder %r renamed on both sides (Photos=%r, Flickr=%r) — Photos wins",
                    baseline, photos_name, flickr_title,
                )
                folders_skipped += 1
                continue

            log.info(
                "%sfolder %r → %r (Flickr-side rename)",
                "[dry-run] would rename " if dry_run else "renaming ",
                photos_name, flickr_title,
            )
            if dry_run:
                folders_renamed += 1
                continue

            if _rename_photos_folder(row["apple_uuid"], flickr_title):
                db.conn.execute(
                    "UPDATE folders SET name = ?, flickr_name = ?, updated_at = ? WHERE id = ?",
                    (flickr_title, flickr_title, _now_iso(), row["id"]),
                )
                db.conn.commit()
                folders_renamed += 1
            else:
                folders_skipped += 1

    log.info(
        "sync-names-from-flickr done — albums renamed=%d skipped=%d  folders renamed=%d skipped=%d",
        albums_renamed, albums_skipped, folders_renamed, folders_skipped,
    )
    return {
        "albums_renamed":  albums_renamed,
        "albums_skipped":  albums_skipped,
        "folders_renamed": folders_renamed,
        "folders_skipped": folders_skipped,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sync Flickr photoset/Collection renames → Apple Photos"
    )
    parser.add_argument("--config",  default="config/config.yml")
    parser.add_argument("--dry-run", action="store_true")
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

    sync_names_from_flickr(db, flickr, dry_run=args.dry_run)
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
