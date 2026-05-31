# poller/legacy_cache.py
"""Local DB cache for the legacy library indexer (#162).

Mirrors the library's database/ directory to data/legacy-cache/<library_uuid>/
so osxphotos can open a local copy instead of the slow AFP mount. Validity is
judged against legacy_libraries metadata (mtime + size + 16 MiB head-hash);
there is no filesystem sidecar. Build is atomic (temp dir + rename).
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from legacy_normalize import head_hash  # noqa: E402

# Candidate locations of the primary asset DB. This tool targets Photos-4
# (migrated iPhoto) libraries, so prefer photos.db: a migrated bundle can carry
# a leftover Photos.sqlite, but photos.db is the DB osxphotos actually reads and
# the only one holding RKAdminData.databaseUuid.
_DB_CANDIDATES = (
    "database/photos.db",  # Photos 4 (our target)
    "database/Photos.sqlite",  # Photos 5+ fallback
)


def locate_source_db(library_path: str) -> str | None:
    lib = Path(library_path)
    for rel in _DB_CANDIDATES:
        p = lib / rel
        if p.exists():
            return str(p)
    return None


def read_library_uuid(source_db_path: str) -> str | None:
    """Path-independent identity for a Photos-4 library.

    osxphotos exposes no library_uuid for Photos-4 bundles, so read Apple's
    intrinsic databaseUuid from RKAdminData and hash it into a filesystem-safe
    id (the raw value contains chars like % and + that are poor for paths).
    Returns None when the value cannot be read.
    """
    # immutable=1 reads only the main DB file, bypassing the WAL. The source
    # lives on a read-only AFP mount where a plain mode=ro connection can't open
    # the -wal/-shm sidecars; databaseUuid is write-once so this is safe.
    try:
        conn = sqlite3.connect(f"file:{source_db_path}?immutable=1", uri=True)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute(
            "SELECT propertyValue, blobPropertyValue FROM RKAdminData "
            "WHERE propertyArea='database' AND propertyName='databaseUuid'"
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if not row:
        return None
    value = row[0] if row[0] is not None else row[1]
    if value is None:
        return None
    raw = value.encode("utf-8") if isinstance(value, str) else bytes(value)
    return "p4-" + hashlib.sha256(raw).hexdigest()[:24]


def source_db_stats(db_path: str) -> dict:
    st = Path(db_path).stat()
    return {
        "db_mtime": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
        "db_size": st.st_size,
        "db_head_hash": head_hash(db_path),
    }


def cache_root(curator_db_path: str) -> Path:
    """legacy-cache dir beside curator.db (under data/, which is git-ignored)."""
    return Path(curator_db_path).expanduser().resolve().parent / "legacy-cache"


def cache_dir(curator_db_path: str, library_uuid: str) -> Path:
    return cache_root(curator_db_path) / library_uuid


def is_cache_valid(library_rec: dict | None, source_db_path: str) -> bool:
    """True iff the recorded mtime + size + head-hash all match the live source."""
    if not library_rec:
        return False
    try:
        stats = source_db_stats(source_db_path)
    except OSError:
        return False
    return (
        library_rec.get("db_mtime") == stats["db_mtime"]
        and library_rec.get("db_size") == stats["db_size"]
        and library_rec.get("db_head_hash") == stats["db_head_hash"]
    )


def build_cache(library_path: str, library_uuid: str, curator_db_path: str) -> str:
    """Copy the library's database/ dir + top-level plists into a local skeleton
    bundle, atomically. Returns the path to the cached bundle (suitable for
    osxphotos.PhotosDB). Replaces any existing/partial cache."""
    lib = Path(library_path)
    dest = cache_dir(curator_db_path, library_uuid)
    tmp = dest.parent / f".{library_uuid}.tmp"
    if tmp.exists():
        shutil.rmtree(tmp)
    (tmp / "database").parent.mkdir(parents=True, exist_ok=True)

    shutil.copytree(lib / "database", tmp / "database")
    # osxphotos needs the bundle plists to recognize the library version.
    for plist in lib.glob("*.plist"):
        shutil.copy2(plist, tmp / plist.name)

    if dest.exists():
        shutil.rmtree(dest)
    tmp.rename(dest)  # atomic: a partial copy never lands at dest
    return str(dest)


def ensure_cache(
    db, library_path: str, library_uuid: str, curator_db_path: str, force: bool = False
) -> str:
    """Return a local bundle path osxphotos can open, building/refreshing the
    cache when invalid or forced. db is the Database (for legacy_libraries)."""
    source_db = locate_source_db(library_path)
    if source_db is None:
        raise FileNotFoundError(f"No Photos DB found under {library_path}")
    dest = cache_dir(curator_db_path, library_uuid)
    rec = db.get_legacy_library(library_uuid)
    if not force and dest.exists() and is_cache_valid(rec, source_db):
        return str(dest)
    return build_cache(library_path, library_uuid, curator_db_path)
