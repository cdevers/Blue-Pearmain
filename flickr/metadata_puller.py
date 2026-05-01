"""
flickr/metadata_puller.py — compare Flickr metadata against Apple Photos, write non-conflicts

For each photo with a linked flickr_id and uuid:
  1. Fetch title / description / tags from Flickr.
  2. Read title / description / keywords from the Photos library via osxphotos.
  3. For each field:
       - Flickr non-empty, Photos empty  → write to Photos (clear Flickr win)
       - Values equal                    → no-op
       - Both non-empty and different    → record metadata_conflict, skip write
       - Flickr empty, Photos non-empty → preserve Photos value (no-op)
  4. Return a result dict per photo.

Usage:
    from flickr.metadata_puller import pull_photo_metadata, pull_batch
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from db.db import Database
    from flickr.flickr_client import FlickrClient

log = logging.getLogger("blue-pearmain.metadata_puller")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def pull_photo_metadata(
    db: "Database",
    flickr: "FlickrClient",
    photo_id: int,
    library_path: str,
    dry_run: bool = False,
    photos_db: object = None,
) -> dict:
    """
    Process one photo row. Returns:
        {
            "photo_id":  int,
            "flickr_id": str,
            "status":    "ok" | "partial" | "no_uuid" | "flickr_error" | "write_error",
            "written":   list[str],   # fields written to Photos
            "conflicts": list[str],   # fields recorded as conflicts
            "skipped":   list[str],   # fields needing no action
            "errors":    list[str],
            "cache_hit": bool,        # True if Flickr metadata read from DB cache
        }
    """
    from flickr.flickr_client import FlickrError

    row = db.conn.execute(
        "SELECT id, flickr_id, uuid, original_filename FROM photos WHERE id = ?",
        (photo_id,),
    ).fetchone()

    result: dict = {
        "photo_id":  photo_id,
        "flickr_id": row["flickr_id"] if row else None,
        "status":    "ok",
        "written":   [],
        "conflicts": [],
        "skipped":   [],
        "errors":    [],
        "cache_hit": False,
    }

    if not row or not row["uuid"]:
        result["status"] = "no_uuid"
        return result

    flickr_id = row["flickr_id"]
    uuid      = row["uuid"]

    # 1. Fetch from DB cache; fall back to live Flickr API on cache miss
    flickr_meta = _read_flickr_cache(db, photo_id)
    if flickr_meta is not None:
        result["cache_hit"] = True
    else:
        try:
            flickr_meta = _fetch_flickr_metadata(flickr, flickr_id)
        except FlickrError as e:
            if e.code == 1:
                # Photo was deleted from Flickr; record it so future runs skip it
                result["status"] = "flickr_deleted"
                if not dry_run:
                    db.mark_flickr_deleted(photo_id)
                log.info(
                    "flickr_deleted photo_id=%s flickr_id=%s — marked, will skip in future",
                    photo_id, flickr_id,
                )
            else:
                result["status"] = "flickr_error"
                result["errors"].append(str(e))
            return result

    # 2. Read from Photos
    try:
        photos_meta = _read_photos_metadata(uuid, library_path, photos_db=photos_db)
    except Exception as e:
        result["status"] = "write_error"
        result["errors"].append(f"osxphotos read failed: {e}")
        return result

    # 3. Compare and act per field
    fields_to_write: dict[str, str] = {}

    for field in ("title", "description"):
        fval = (flickr_meta.get(field) or "").strip()
        pval = (photos_meta.get(field) or "").strip()

        if not fval:
            result["skipped"].append(field)   # nothing on Flickr, keep Photos
        elif fval == pval:
            result["skipped"].append(field)   # already in sync
        elif not pval:
            fields_to_write[field] = fval     # Flickr has it, Photos doesn't
        else:
            # Both non-empty and different → conflict
            result["conflicts"].append(field)
            if not dry_run:
                db.upsert_metadata_conflict(photo_id, field, fval, pval)
            log.debug("conflict %s photo_id=%s field=%s", row["original_filename"], photo_id, field)

    # Tags: set-based, case-insensitive comparison
    ftags = _normalise_tags(flickr_meta.get("tags") or [])
    ptags = _normalise_tags(photos_meta.get("tags") or [])

    if not ftags:
        result["skipped"].append("tags")
    elif ftags == ptags:
        result["skipped"].append("tags")
    elif not ptags:
        fields_to_write["tags"] = json.dumps(sorted(flickr_meta.get("tags") or []))
    else:
        result["conflicts"].append("tags")
        if not dry_run:
            db.upsert_metadata_conflict(
                photo_id, "tags",
                json.dumps(sorted(flickr_meta.get("tags") or [])),
                json.dumps(sorted(photos_meta.get("tags") or [])),
            )

    # 4. Write to Photos
    if fields_to_write and not dry_run:
        try:
            _write_photos_metadata(uuid, library_path, fields_to_write, photos_db=photos_db)
            result["written"].extend(fields_to_write.keys())
            log.debug(
                "wrote %s to Photos photo_id=%s fields=%s",
                row["original_filename"], photo_id, list(fields_to_write.keys()),
            )
        except RuntimeError as e:
            result["status"] = "write_error"
            result["errors"].append(str(e))
            return result
    elif fields_to_write and dry_run:
        result["written"].extend(fields_to_write.keys())  # count as written in dry-run output

    if result["conflicts"] and not result["written"]:
        result["status"] = "partial"
    elif result["errors"]:
        result["status"] = "write_error"

    return result


def pull_batch(
    db: "Database",
    flickr: "FlickrClient",
    photo_ids: list[int],
    library_path: str,
    dry_run: bool = False,
    verbose: bool = False,
) -> dict:
    """
    Run pull_photo_metadata for a list of photo_ids.
    Returns aggregate counts: {"written": N, "conflicts": N, "skipped": N, "failed": N}
    """
    totals = {"written": 0, "conflicts": 0, "skipped": 0, "failed": 0,
              "cache_hits": 0, "cache_misses": 0}
    total = len(photo_ids)

    try:
        import osxphotos
        photos_db = osxphotos.PhotosDB(dbfile=library_path)
    except ImportError:
        raise RuntimeError("osxphotos is not installed — cannot read Photos metadata")
    except Exception as e:
        raise RuntimeError(f"Cannot open Photos library: {e}")

    log.info("Processing %d photos%s", total, " (dry-run)" if dry_run else "")

    for i, photo_id in enumerate(photo_ids, 1):
        result = pull_photo_metadata(
            db, flickr, photo_id, library_path, dry_run=dry_run, photos_db=photos_db
        )
        totals["written"]   += len(result["written"])
        totals["conflicts"] += len(result["conflicts"])
        totals["skipped"]   += len(result["skipped"])

        if result.get("cache_hit"):
            totals["cache_hits"] += 1
        else:
            totals["cache_misses"] += 1

        if result["status"] == "flickr_deleted":
            totals["skipped"] += 1
        elif result["status"] in ("flickr_error", "write_error", "no_uuid"):
            totals["failed"] += 1
            if result["status"] != "no_uuid":
                log.warning(
                    "pull_batch: photo_id=%s status=%s errors=%s",
                    photo_id, result["status"], result["errors"],
                )
        elif verbose:
            log.debug(
                "pull_batch: photo_id=%s written=%s conflicts=%s skipped=%s cache_hit=%s",
                photo_id, result["written"], result["conflicts"], result["skipped"],
                result.get("cache_hit"),
            )

        if i % 50 == 0 or i == total:
            log.info(
                "Progress: %d / %d — written=%d  conflicts=%d  skipped=%d  failed=%d  "
                "cache=%d/%d",
                i, total,
                totals["written"], totals["conflicts"], totals["skipped"], totals["failed"],
                totals["cache_hits"], total,
            )

    return totals


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _read_flickr_cache(db: "Database", photo_id: int) -> dict | None:
    """
    Read Flickr metadata from the DB cache columns written by the poller.
    Returns None if the cache is unpopulated (meta_synced_flickr_at is NULL),
    meaning this photo predates the Phase 2 poller run and needs a live API call.
    """
    row = db.conn.execute(
        """SELECT flickr_title, flickr_description, flickr_tags, meta_synced_flickr_at
           FROM photos WHERE id = ?""",
        (photo_id,),
    ).fetchone()
    if not row or not row["meta_synced_flickr_at"]:
        return None
    tags = json.loads(row["flickr_tags"]) if row["flickr_tags"] else []
    return {
        "title":       row["flickr_title"]       or "",
        "description": row["flickr_description"] or "",
        "tags":        tags,
    }


def _fetch_flickr_metadata(flickr: "FlickrClient", flickr_id: str) -> dict:
    """Fetch title, description, tags from Flickr. Raises FlickrError on failure."""
    info   = flickr.get_photo_info(flickr_id)
    photo  = info.get("photo", {})
    title  = photo.get("title", {}).get("_content", "") or ""
    desc   = photo.get("description", {}).get("_content", "") or ""
    tags_raw = photo.get("tags", {})
    if isinstance(tags_raw, dict):
        tags = [t.get("raw", "") for t in tags_raw.get("tag", []) if t.get("raw")]
    else:
        tags = []
    return {"title": title, "description": desc, "tags": tags}


def _read_photos_metadata(uuid: str, library_path: str, photos_db: object = None) -> dict:
    """
    Read title, description (caption), and keywords from Apple Photos via osxphotos.
    Returns {"title": str, "description": str, "tags": list[str]}.
    Raises RuntimeError if osxphotos is unavailable or the photo is not found.
    """
    if photos_db is None:
        try:
            import osxphotos
        except ImportError:
            raise RuntimeError("osxphotos is not installed — cannot read Photos metadata")
        photos_db = osxphotos.PhotosDB(dbfile=library_path)

    results = photos_db.photos(uuid=[uuid])
    if not results:
        return {"title": "", "description": "", "tags": []}

    photo = results[0]
    return {
        "title":       photo.title       or "",
        "description": photo.description or "",
        "tags":        list(photo.keywords or []),
    }


def _write_photos_metadata(uuid: str, library_path: str, fields: dict, photos_db: object = None) -> None:
    """
    Write title/description/keywords to Apple Photos via photoscript (AppleScript bridge).
    Raises RuntimeError if Photos.app is not running or photoscript is unavailable.
    """
    if not _photos_is_running():
        raise RuntimeError(
            "Photos.app is not running — open Photos.app and re-run sync-metadata"
        )

    try:
        import photoscript
    except ImportError:
        raise RuntimeError("photoscript is not installed — pip install photoscript")

    try:
        photo = photoscript.Photo(uuid)
    except Exception as e:
        raise RuntimeError(f"photoscript could not find photo {uuid}: {e}") from e

    if "title" in fields:
        photo.title = fields["title"]
    if "description" in fields:
        photo.description = fields["description"]
    if "tags" in fields:
        photo.keywords = json.loads(fields["tags"])


def _photos_is_running() -> bool:
    """Return True if Photos.app is currently running."""
    try:
        result = subprocess.run(
            ["osascript", "-e", 'tell application "System Events" to (name of processes) contains "Photos"'],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip().lower() == "true"
    except Exception:
        return False


def _normalise_tags(tags: list[str]) -> set[str]:
    """Normalise a tag list for comparison: lowercase, strip, dedupe."""
    return {t.strip().lower() for t in tags if t.strip()}
