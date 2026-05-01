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

import hashlib
import json
import logging
import subprocess
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from db.db import Database
    from flickr.flickr_client import FlickrClient

log = logging.getLogger("blue-pearmain.metadata_puller")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _field_hash(value: str) -> str:
    return hashlib.sha256((value or "").strip().encode()).hexdigest()


def _classify_text_field(
    photo_id: int,
    field: str,
    flickr_val: str,
    photos_val: str,
    now: str,
) -> list[dict]:
    """Classify a title or description divergence and return proposal dicts."""
    fval = (flickr_val or "").strip()
    pval = (photos_val or "").strip()

    if not fval and not pval:
        return []
    if fval == pval:
        return []

    fhash = _field_hash(fval) if fval else None
    phash = _field_hash(pval) if pval else None

    def make(source, target, proposed_value, conflict_type):
        return {
            "photo_id":                photo_id,
            "field":                   field,
            "proposed_value":          proposed_value,
            "source":                  source,
            "target":                  target,
            "conflict_type":           conflict_type,
            "source_hash_at_creation": fhash if source == "flickr" else phash,
            "target_hash_at_creation": phash if target == "photos" else fhash,
            "created_at":              now,
        }

    if not fval:
        return [make("photos", "flickr", pval, "non_conflict")]
    if not pval:
        return [make("flickr", "photos", fval, "non_conflict")]

    # Both non-empty and different → collision
    return [
        make("flickr", "photos", fval, "collision"),
        make("photos", "flickr", pval, "collision"),
    ]


# ---------------------------------------------------------------------------
# Phase 4: sync engine (DB cache → proposals, no API calls)
# ---------------------------------------------------------------------------

def run_sync_engine(
    db: "Database",
    photo_ids: list[int],
    dry_run: bool = False,
    verbose: bool = False,
) -> dict:
    """
    Diff flickr_tags vs photos_tags for each photo, classify the divergence,
    and write proposals to metadata_proposals. Sets meta_last_harmonized_at.
    No Flickr API calls. No writes to Photos or Flickr.
    Returns aggregate counts.
    """
    COMMIT_EVERY = 500
    totals = {"proposals": 0, "hash_matches": 0, "skipped": 0, "failed": 0}
    total = len(photo_ids)
    now = _now_iso()

    harmonize_batch: list[tuple[str, int]] = []  # (now, photo_id) for bulk UPDATE

    for i, photo_id in enumerate(photo_ids, 1):
        try:
            proposals = _harmonise_one(db, photo_id, now)
        except Exception as e:
            log.warning("sync_engine: photo_id=%s error: %s", photo_id, e)
            totals["failed"] += 1
            continue

        if proposals is None:
            totals["failed"] += 1
            continue

        if proposals == "hash_match":
            totals["hash_matches"] += 1
        elif proposals == "skipped":
            totals["skipped"] += 1
        else:
            if not dry_run:
                # Always supersede stale proposals before inserting fresh ones.
                # Catches reclassifications (e.g. collision → divergence after a
                # normalization fix) that upsert_proposal would otherwise silently
                # drop because the source hash hasn't changed.
                db.conn.execute(
                    "UPDATE metadata_proposals SET status='superseded', resolved_at=?"
                    " WHERE photo_id=? AND status='pending'",
                    (now, photo_id),
                )
                for p in proposals:
                    db.upsert_proposal(p)
            totals["proposals"] += len(proposals)
            if not proposals:
                totals["skipped"] += 1

        if not dry_run:
            harmonize_batch.append((now, photo_id))

        if len(harmonize_batch) >= COMMIT_EVERY:
            db.conn.executemany(
                "UPDATE photos SET meta_last_harmonized_at = ? WHERE id = ?",
                harmonize_batch,
            )
            db.conn.commit()
            harmonize_batch = []

        if verbose or i % 1000 == 0 or i == total:
            log.info(
                "Progress %d/%d — proposals=%d  hash_matches=%d  skipped=%d  failed=%d",
                i, total,
                totals["proposals"], totals["hash_matches"],
                totals["skipped"], totals["failed"],
            )

    if not dry_run and harmonize_batch:
        db.conn.executemany(
            "UPDATE photos SET meta_last_harmonized_at = ? WHERE id = ?",
            harmonize_batch,
        )
        db.conn.commit()

    if not dry_run:
        # Supersede stale pending proposals where values now agree.
        # Tags: hash-equality check.
        cur = db.conn.execute(
            """UPDATE metadata_proposals
               SET status='superseded', resolved_at=?
               WHERE status='pending' AND field='tags'
                 AND photo_id IN (
                   SELECT id FROM photos
                   WHERE flickr_tags_hash IS NOT NULL
                     AND photos_tags_hash IS NOT NULL
                     AND flickr_tags_hash = photos_tags_hash
                 )""",
            (now,),
        )
        if cur.rowcount:
            log.info(
                "sync_engine: superseded %d stale tag proposals (hashes now match)",
                cur.rowcount,
            )
        # Title and description: string-equality check.
        for field, subquery in [
            ("title",
             "SELECT id FROM photos WHERE TRIM(COALESCE(flickr_title,''))"
             " = TRIM(COALESCE(photos_title,''))"),
            ("description",
             "SELECT id FROM photos WHERE TRIM(COALESCE(flickr_description,''))"
             " = TRIM(COALESCE(photos_description,''))"),
        ]:
            cur = db.conn.execute(
                f"UPDATE metadata_proposals SET status='superseded', resolved_at=?"
                f" WHERE status='pending' AND field=?"
                f" AND photo_id IN ({subquery})",
                (now, field),
            )
            if cur.rowcount:
                log.info(
                    "sync_engine: superseded %d stale %s proposals (values now match)",
                    cur.rowcount, field,
                )
        db.conn.commit()

    return totals


def _harmonise_one(db: "Database", photo_id: int, now: str):
    """
    Return one of:
      "hash_match"  — all fields already in sync (tags confirmed via hash)
      "skipped"     — all fields empty or equal, tags confirmed via slow path
      list[dict]    — proposals to upsert (may be empty list)
      None          — photo row not found (error)
    """
    row = db.conn.execute(
        """SELECT flickr_tags, flickr_tags_hash, photos_tags, photos_tags_hash,
                  flickr_title, photos_title,
                  flickr_description, photos_description
           FROM photos WHERE id = ?""",
        (photo_id,),
    ).fetchone()
    if not row:
        return None

    proposals = []

    # Tags: fast-path hash check
    fhash = row["flickr_tags_hash"]
    phash = row["photos_tags_hash"]
    tags_hash_match = fhash and phash and fhash == phash
    if not tags_hash_match:
        proposals.extend(
            _classify_tags(photo_id, row["flickr_tags"], row["photos_tags"], fhash, phash, now)
        )

    # Title and description: always compare (short strings, no hash fast-path)
    for field in ("title", "description"):
        proposals.extend(
            _classify_text_field(
                photo_id, field,
                row[f"flickr_{field}"], row[f"photos_{field}"],
                now,
            )
        )

    if not proposals:
        if tags_hash_match:
            return "hash_match"
        # Slow path produced no proposals (e.g. punctuation-only tag difference,
        # text fields equal). Return an empty list so the per-photo supersede still
        # fires and clears any stale proposals for this photo.
        return []

    return proposals


def _classify_tags(
    photo_id: int,
    flickr_tags_json: str | None,
    photos_tags_json: str | None,
    flickr_hash: str | None,
    photos_hash: str | None,
    now: str,
) -> list[dict]:
    """
    Classify the tag divergence and return proposal dicts.
    Proposals are returned but NOT written to DB here.
    """
    ftags_raw = json.loads(flickr_tags_json) if flickr_tags_json else []
    ptags_raw = json.loads(photos_tags_json) if photos_tags_json else []

    def norm(tag: str) -> str:
        # Flickr normalizes tags to alphanumeric-only, silently stripping spaces,
        # hyphens, and other punctuation ("close-up" → "closeup", "new york" →
        # "newyork"). Keep only isalnum() chars so comparisons match Flickr's view.
        return "".join(
            c for c in unicodedata.normalize("NFC", tag.strip().casefold())
            if c.isalnum()
        )

    ftags_norm = {norm(t) for t in ftags_raw if t.strip()}
    ptags_norm = {norm(t) for t in ptags_raw if t.strip()}

    if not ftags_norm and not ptags_norm:
        return []
    if ftags_norm == ptags_norm:
        return []

    def make(source, target, proposed_value, conflict_type):
        return {
            "photo_id":                photo_id,
            "field":                   "tags",
            "proposed_value":          proposed_value,
            "source":                  source,
            "target":                  target,
            "conflict_type":           conflict_type,
            "source_hash_at_creation": flickr_hash if source == "flickr" else photos_hash,
            "target_hash_at_creation": photos_hash if target == "photos" else flickr_hash,
            "created_at":              now,
        }

    if not ftags_norm:
        return [make("photos", "flickr", photos_tags_json, "non_conflict")]

    if not ptags_norm:
        return [make("flickr", "photos", flickr_tags_json, "non_conflict")]

    if ftags_norm > ptags_norm:
        return [make("flickr", "photos", flickr_tags_json, "divergence")]

    if ptags_norm > ftags_norm:
        return [make("photos", "flickr", photos_tags_json, "divergence")]

    # Collision: neither is a superset
    return [
        make("flickr", "photos", flickr_tags_json, "collision"),
        make("photos", "flickr", photos_tags_json, "collision"),
    ]


# ---------------------------------------------------------------------------
# Public API (legacy: Phase 2/3 pull-and-write behaviour)
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
