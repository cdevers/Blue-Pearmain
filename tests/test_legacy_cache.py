# tests/test_legacy_cache.py
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "poller"))

from legacy_cache import (  # noqa: E402
    cache_dir,
    cache_root,
    locate_source_db,
    source_db_stats,
    is_cache_valid,
)


def _fake_library(tmp_path) -> Path:
    """Build a minimal Photos-4-shaped bundle with a database/ dir."""
    lib = tmp_path / "Old.photoslibrary"
    (lib / "database").mkdir(parents=True)
    db = lib / "database" / "photos.db"
    db.write_bytes(b"SQLITEHEADER" + b"\x00" * 500)
    return lib


class TestLocateSourceDb:
    def test_finds_photos4_db(self, tmp_path):
        lib = _fake_library(tmp_path)
        assert locate_source_db(str(lib)) == str(lib / "database" / "photos.db")

    def test_missing_returns_none(self, tmp_path):
        assert locate_source_db(str(tmp_path / "nope.photoslibrary")) is None


class TestCacheRoot:
    def test_root_is_beside_curator_db(self, tmp_path):
        db_path = tmp_path / "data" / "curator.db"
        assert cache_root(str(db_path)) == tmp_path / "data" / "legacy-cache"

    def test_dir_keyed_by_library_uuid(self, tmp_path):
        db_path = tmp_path / "data" / "curator.db"
        assert cache_dir(str(db_path), "LIB") == tmp_path / "data" / "legacy-cache" / "LIB"


class TestValidity:
    def test_valid_when_all_match(self, tmp_path):
        lib = _fake_library(tmp_path)
        src = locate_source_db(str(lib))
        stats = source_db_stats(src)
        lib_rec = {
            "db_mtime": stats["db_mtime"],
            "db_size": stats["db_size"],
            "db_head_hash": stats["db_head_hash"],
        }
        assert is_cache_valid(lib_rec, src) is True

    def test_invalid_when_hash_differs_same_mtime_size(self, tmp_path):
        lib = _fake_library(tmp_path)
        src = locate_source_db(str(lib))
        stats = source_db_stats(src)
        lib_rec = {
            "db_mtime": stats["db_mtime"],
            "db_size": stats["db_size"],
            "db_head_hash": "different",
        }
        assert is_cache_valid(lib_rec, src) is False

    def test_invalid_when_record_missing(self, tmp_path):
        lib = _fake_library(tmp_path)
        src = locate_source_db(str(lib))
        assert is_cache_valid(None, src) is False
