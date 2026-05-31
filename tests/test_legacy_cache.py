# tests/test_legacy_cache.py
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "poller"))

from legacy_cache import (  # noqa: E402
    cache_dir,
    cache_root,
    locate_source_db,
    read_library_uuid,
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


def _photos4_db(tmp_path, *, uuid_value="MY%X00uFQayV48ecM+9I2A", with_table=True) -> str:
    tmp_path = Path(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = tmp_path / "photos.db"
    conn = sqlite3.connect(str(db))
    if with_table:
        conn.execute(
            "CREATE TABLE RKAdminData ("
            "propertyArea TEXT, propertyName TEXT, "
            "propertyValue TEXT, blobPropertyValue BLOB)"
        )
        if uuid_value is not None:
            conn.execute(
                "INSERT INTO RKAdminData(propertyArea, propertyName, propertyValue) "
                "VALUES('database', 'databaseUuid', ?)",
                (uuid_value,),
            )
        conn.commit()
    conn.close()
    return str(db)


class TestReadLibraryUuid:
    def test_reads_and_hashes_database_uuid(self, tmp_path):
        src = _photos4_db(tmp_path)
        uuid = read_library_uuid(src)
        assert uuid is not None
        assert uuid.startswith("p4-")
        # filesystem-safe: no chars from the raw value
        assert all(c not in uuid for c in "%+/ ")

    def test_deterministic_for_same_value(self, tmp_path):
        a = read_library_uuid(_photos4_db(tmp_path / "a", uuid_value="ABC"))
        b = read_library_uuid(_photos4_db(tmp_path / "b", uuid_value="ABC"))
        assert a == b

    def test_distinct_values_differ(self, tmp_path):
        a = read_library_uuid(_photos4_db(tmp_path / "a", uuid_value="ABC"))
        b = read_library_uuid(_photos4_db(tmp_path / "b", uuid_value="XYZ"))
        assert a != b

    def test_missing_table_returns_none(self, tmp_path):
        assert read_library_uuid(_photos4_db(tmp_path, with_table=False)) is None

    def test_missing_row_returns_none(self, tmp_path):
        assert read_library_uuid(_photos4_db(tmp_path, uuid_value=None)) is None

    def test_unreadable_path_returns_none(self, tmp_path):
        assert read_library_uuid(str(tmp_path / "nope.db")) is None


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
