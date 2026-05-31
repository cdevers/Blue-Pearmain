# poller/legacy_indexer.py
"""Legacy (migrated iPhoto/Photos 4) library indexer (#162).

Opens the library via osxphotos, builds one normalized row per asset in
legacy_assets, and copies an existing thumbnail derivative into BP's thumb
cache (path-independent identity). Full runs are an authoritative mirror:
after a successful full iteration they reconcile (delete unseen rows) and GC
orphaned thumbnails. --limit runs are non-authoritative (no deletions).
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Callable

from legacy_cache import ensure_cache
from legacy_normalize import (
    canonical_rel_path,
    normalize_json_list,
    thumbnail_cache_key,
    thumbnail_path,
)

log = logging.getLogger("blue-pearmain.legacy-indexer")

_UNKNOWN = {"", "_UNKNOWN_", None}


def _library_uuid(photosdb) -> str:
    """Path-independent identity of the source bundle."""
    for attr in ("library_uuid", "_uuid"):
        val = getattr(photosdb, attr, None)
        if val:
            return str(val)
    raise ValueError("PhotosDB exposed no library UUID")


def _face_counts(photo) -> tuple[int, int]:
    faces = getattr(photo, "face_info", None) or []
    named = sum(1 for f in faces if getattr(f, "name", None) not in _UNKNOWN)
    unknown = sum(1 for f in faces if getattr(f, "name", None) in _UNKNOWN)
    return named, unknown


def _persons(photo) -> list[str]:
    return [p for p in (getattr(photo, "persons", None) or []) if p not in _UNKNOWN]


def _rel_master(photo, library_path: str) -> str | None:
    p = getattr(photo, "path", None)
    if not p:
        return None
    try:
        rel = str(Path(p).relative_to(Path(library_path)))
    except ValueError:
        rel = p
    return canonical_rel_path(rel)


def _copy_thumbnail(photo, library_uuid: str, thumb_root: Path) -> str:
    """Copy the first existing derivative into the cache. Returns ok/missing/error."""
    derivs = getattr(photo, "path_derivatives", None) or []
    src = next((d for d in derivs if d and Path(d).exists()), None)
    if src is None:
        return "missing"
    try:
        key = thumbnail_cache_key(library_uuid, photo.uuid)
        dest = thumbnail_path(thumb_root, library_uuid, key)
        if dest.exists():
            return "ok"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        return "ok"
    except OSError as exc:
        log.warning("thumbnail copy failed for %s: %s", photo.uuid, exc)
        return "error"


def _build_row(photo, library_uuid: str, library_path: str) -> dict:
    lat, lon = getattr(photo, "location", (None, None)) or (None, None)
    date = getattr(photo, "date", None)
    named, unknown = _face_counts(photo)
    return {
        "library_uuid": library_uuid,
        "asset_uuid": photo.uuid,
        "original_filename": getattr(photo, "original_filename", None),
        "fingerprint": getattr(photo, "fingerprint", None),
        "date_taken": date.isoformat() if date else None,
        "width": getattr(photo, "width", None),
        "height": getattr(photo, "height", None),
        "latitude": lat,
        "longitude": lon,
        "title": getattr(photo, "title", None),
        "description": getattr(photo, "description", None),
        "keywords": normalize_json_list(getattr(photo, "keywords", [])),
        "labels": normalize_json_list(getattr(photo, "labels", [])),
        "persons": normalize_json_list(_persons(photo)),
        "named_face_count": named,
        "unknown_face_count": unknown,
        "master_rel_path": _rel_master(photo, library_path),
    }


def _open_photosdb(
    library_path, db, library_uuid_hint, curator_db_path, use_cache, refresh_cache, photosdb_factory
):
    import osxphotos

    factory: Callable = photosdb_factory or (lambda p: osxphotos.PhotosDB(p))
    if not use_cache:
        return factory(library_path)
    # Open once to learn the library_uuid, then cache by it.
    probe = factory(library_path)
    uuid = _library_uuid(probe)
    bundle = ensure_cache(db, library_path, uuid, curator_db_path, force=refresh_cache)
    return factory(bundle)


def index_library(
    library_path: str,
    db,
    *,
    curator_db_path: str,
    thumb_root: Path | str,
    copy_thumbnails: bool = True,
    limit: int | None = None,
    use_cache: bool = True,
    refresh_cache: bool = False,
    photosdb_factory: Callable | None = None,
) -> dict:
    """Index the legacy library into curator.db. Returns a stats dict."""
    thumb_root = Path(thumb_root)
    photosdb = _open_photosdb(
        library_path,
        db,
        None,
        curator_db_path,
        use_cache,
        refresh_cache,
        photosdb_factory,
    )
    library_uuid = _library_uuid(photosdb)
    schema_version = getattr(photosdb, "db_version", None)

    # Ensure the library row exists before inserting assets (FK requires it).
    db.set_legacy_library({"library_uuid": library_uuid, "source_path_last_seen": library_path})

    seen: set[str] = set()
    indexed = thumb_ok = thumb_missing = thumb_error = 0

    for photo in photosdb.photos():
        if limit is not None and indexed >= limit:
            break
        row = _build_row(photo, library_uuid, library_path)
        if copy_thumbnails:
            status = _copy_thumbnail(photo, library_uuid, thumb_root)
        else:
            status = "skipped"
        row["thumbnail_cache_key"] = thumbnail_cache_key(library_uuid, photo.uuid)
        row["thumbnail_status"] = status
        db.upsert_legacy_asset(row)
        seen.add(photo.uuid)
        indexed += 1
        thumb_ok += status == "ok"
        thumb_missing += status == "missing"
        thumb_error += status == "error"

    # Authoritative reconciliation — only after a successful FULL iteration.
    # An exception above propagates and skips this block (interrupted == no delete).
    reconciled = 0
    if limit is None:
        removed_keys = db.delete_legacy_assets_not_in(library_uuid, seen)
        reconciled = len(removed_keys)
        for key in removed_keys:
            stale = thumbnail_path(thumb_root, library_uuid, key)
            try:
                stale.unlink(missing_ok=True)
            except OSError:
                pass

    try:
        schema_int = int(schema_version) if schema_version is not None else None
    except (TypeError, ValueError):
        schema_int = None

    from legacy_cache import locate_source_db, source_db_stats

    lib_rec = {
        "library_uuid": library_uuid,
        "source_path_last_seen": library_path,
        "schema_version": schema_int,
        "asset_count": db.legacy_asset_count(library_uuid),
    }
    src = locate_source_db(library_path)
    if src:
        try:
            lib_rec.update(source_db_stats(src))
        except OSError:
            pass
    db.set_legacy_library(lib_rec)

    stats = {
        "library_uuid": library_uuid,
        "indexed": indexed,
        "reconciled": reconciled,
        "thumb_ok": thumb_ok,
        "thumb_missing": thumb_missing,
        "thumb_error": thumb_error,
        "authoritative": limit is None,
    }
    log.info("legacy index complete: %s", stats)
    return stats
