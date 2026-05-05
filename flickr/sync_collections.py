"""
flickr/sync_collections.py — sync Apple Photos folder hierarchy → Flickr Collections

Usage:
    python flickr/sync_collections.py --config config/config.yml [--dry-run]
    python flickr/sync_collections.py --config config/config.yml --remove [--force]

Or via bp CLI:
    bp sync-album-collections [--dry-run] [--remove [--force]]

Requires a Flickr Pro account. Albums without a folder remain as standalone
photosets and are not affected by this command.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

log = logging.getLogger("blue-pearmain.sync_collections")


def _topological_order(folders: list[dict]) -> list[dict]:
    """Return folders sorted parent-before-child (BFS from roots)."""
    children: dict[int | None, list[dict]] = {}
    for f in folders:
        children.setdefault(f["parent_id"], []).append(f)

    result: list[dict] = []
    queue = list(children.get(None, []))
    while queue:
        node = queue.pop(0)
        result.append(node)
        queue.extend(children.get(node["id"], []))
    return result


def sync_collections(db, flickr, dry_run: bool = False) -> dict:
    """
    Sync folder tree from DB → Flickr Collections.
    Returns totals dict: {"created": N, "updated": N, "skipped": N}.
    """
    from flickr.flickr_client import FlickrError

    folders = db.get_all_folders()
    if not folders:
        log.info("sync-album-collections: no folders found — nothing to sync")
        return {"created": 0, "updated": 0, "skipped": 0}

    ordered = _topological_order(folders)
    totals = {"created": 0, "updated": 0, "skipped": 0}

    for folder in ordered:
        folder_id     = folder["id"]
        name          = folder["name"]
        collection_id = folder["flickr_collection_id"]

        if dry_run:
            action = "would create" if not collection_id else "would update"
            log.info("[dry-run] %s collection for folder %r", action, name)
            totals["updated" if collection_id else "created"] += 1
            continue

        # Ensure this folder has a Flickr Collection
        if not collection_id:
            collection_id = flickr.create_collection(name, description="")
            db.set_folder_flickr_collection_id(folder_id, collection_id)
            log.info("created collection %r (id=%s)", name, collection_id)
            totals["created"] += 1
        else:
            totals["updated"] += 1

        # Collect direct child photosets (albums in this folder with a pushed set)
        photoset_rows = db.conn.execute(
            "SELECT flickr_set_id FROM albums WHERE folder_id = ? AND flickr_set_id IS NOT NULL",
            (folder_id,),
        ).fetchall()
        photoset_ids = [r["flickr_set_id"] for r in photoset_rows]

        # Collect direct child sub-collections (child folders with a collection ID)
        sub_col_rows = db.conn.execute(
            "SELECT flickr_collection_id FROM folders WHERE parent_id = ? AND flickr_collection_id IS NOT NULL",
            (folder_id,),
        ).fetchall()
        sub_collection_ids = [r["flickr_collection_id"] for r in sub_col_rows]

        try:
            flickr.edit_collection_sets(collection_id, photoset_ids, sub_collection_ids)
            log.debug(
                "updated collection %r — %d photosets, %d sub-collections",
                name, len(photoset_ids), len(sub_collection_ids),
            )
        except FlickrError as e:
            if "not found" in str(e).lower() or e.code == 2:
                log.warning(
                    "collection %r (id=%s) not found on Flickr — recreating",
                    name, collection_id,
                )
                db.clear_folder_flickr_collection_id(folder_id)
                collection_id = flickr.create_collection(name, description="")
                db.set_folder_flickr_collection_id(folder_id, collection_id)
                flickr.edit_collection_sets(collection_id, photoset_ids, sub_collection_ids)
            else:
                log.error("edit_collection_sets failed for %r: %s", name, e)

    log.info(
        "sync-album-collections done — created=%d  updated=%d  skipped=%d",
        totals["created"], totals["updated"], totals["skipped"],
    )
    return totals


def remove_orphaned_collections(
    db, flickr, library_path: str, force: bool = False
) -> dict:
    """
    Find DB folders whose apple_uuid no longer exists in the live Photos library,
    delete their Flickr Collections, and remove the DB rows.

    Returns {"removed": N, "skipped": N}.
    """
    import osxphotos

    photo_lib = osxphotos.PhotosDB(dbfile=library_path)

    # Collect all live folder UUIDs from the Photos library
    live_uuids: set[str] = set()
    for album in photo_lib.album_info:
        node = getattr(album, "parent", None)
        while node is not None:
            live_uuids.add(node.uuid)
            node = getattr(node, "parent", None)

    folders = db.get_all_folders()
    orphans = [f for f in folders if f["apple_uuid"] not in live_uuids and f["flickr_collection_id"]]

    if not orphans:
        log.info("sync-album-collections --remove: no orphaned collections found")
        return {"removed": 0, "skipped": 0}

    removed = 0
    skipped = 0
    for folder in orphans:
        if not force:
            answer = input(
                f"Delete Flickr Collection {folder['flickr_collection_id']!r} "
                f"for removed folder {folder['name']!r}? [y/N] "
            ).strip().lower()
            if answer != "y":
                log.info("skipped removal of folder %r", folder["name"])
                skipped += 1
                continue

        try:
            flickr.delete_collection(folder["flickr_collection_id"])
            db.conn.execute("DELETE FROM folders WHERE id = ?", (folder["id"],))
            db.conn.commit()
            log.info("removed collection for folder %r", folder["name"])
            removed += 1
        except Exception as e:
            log.error("failed to remove collection for %r: %s", folder["name"], e)
            skipped += 1

    return {"removed": removed, "skipped": skipped}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sync Apple Photos folder hierarchy → Flickr Collections"
    )
    parser.add_argument("--config",  default="config/config.yml")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be synced, don't write")
    parser.add_argument("--remove",  action="store_true", help="Remove Flickr Collections for deleted Photos folders")
    parser.add_argument("--force",   action="store_true", help="Skip confirmation prompts with --remove")
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

    try:
        totals = sync_collections(db, flickr, dry_run=args.dry_run)
    except Exception as e:
        from flickr.flickr_client import FlickrError
        if isinstance(e, FlickrError) and "pro" in str(e).lower():
            log.error("Flickr Collections require a Pro account — skipping")
            return 0
        log.error("sync_collections failed: %s", e)
        return 1

    if args.remove:
        library_path = str(Path(config.get("photos_library", {}).get("path", "")).expanduser())
        remove_orphaned_collections(db, flickr, library_path, force=args.force)

    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
