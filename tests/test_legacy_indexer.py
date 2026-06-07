# tests/test_legacy_indexer.py
from __future__ import annotations

import json
import logging
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "poller"))

from db.db import Database  # noqa: E402
import legacy_indexer  # noqa: E402
from legacy_normalize import thumbnail_cache_key, thumbnail_path  # noqa: E402


class FakeFace:
    def __init__(self, name):
        self.name = name


class FakePhoto:
    def __init__(self, uuid, *, persons=(), faces=(), path=None, derivatives=(), **kw):
        self.uuid = uuid
        self.original_filename = kw.get("original_filename", f"{uuid}.jpg")
        self.fingerprint = kw.get("fingerprint", "fp")
        self.date = kw.get("date")
        self.width = kw.get("width", 4000)
        self.height = kw.get("height", 3000)
        self.location = kw.get("location", (None, None))
        self.title = kw.get("title")
        self.description = kw.get("description")
        self.keywords = list(kw.get("keywords", []))
        self.labels = list(kw.get("labels", []))
        self.persons = list(persons)
        self.face_info = [FakeFace(n) for n in faces]
        self.path = path
        self.path_derivatives = list(derivatives)


class FakePhotosDB:
    def __init__(self, photos, db_version="5002", uuid="LIB-UUID"):
        self._photos = photos
        self.db_version = db_version
        self.library_path = "/fake/Old.photoslibrary"
        self._uuid = uuid

    def photos(self):
        return list(self._photos)


def _factory(photos, **kw):
    def make(_path):
        return FakePhotosDB(photos, **kw)

    return make


def _db(tmp_path) -> Database:
    db = Database(str(tmp_path / "curator.db"))
    from db.migrations.migrate_026_legacy_index import run_on_conn

    run_on_conn(db.conn)
    return db


def test_indexes_persons_and_face_counts(tmp_path):
    db = _db(tmp_path)
    photos = [FakePhoto("A", persons=["Isaac", "May"], faces=["Isaac", "May", "_UNKNOWN_"])]
    stats = legacy_indexer.index_library(
        "/fake/Old.photoslibrary",
        db,
        curator_db_path=str(tmp_path / "curator.db"),
        thumb_root=tmp_path / "thumbs",
        copy_thumbnails=False,
        use_cache=False,
        photosdb_factory=_factory(photos),
    )
    assert stats["indexed"] == 1
    row = next(db.iter_legacy_assets("LIB-UUID"))
    assert json.loads(row["persons"]) == ["Isaac", "May"]
    assert row["named_face_count"] == 2
    assert row["unknown_face_count"] == 1


def test_persons_sorted_unique_deterministic(tmp_path):
    db = _db(tmp_path)
    photos = [FakePhoto("A", persons=["May", "Isaac", "May"])]
    legacy_indexer.index_library(
        "/fake/Old.photoslibrary",
        db,
        curator_db_path=str(tmp_path / "curator.db"),
        thumb_root=tmp_path / "thumbs",
        copy_thumbnails=False,
        use_cache=False,
        photosdb_factory=_factory(photos),
    )
    row = next(db.iter_legacy_assets("LIB-UUID"))
    assert json.loads(row["persons"]) == ["Isaac", "May"]


def test_master_rel_path_is_bundle_relative_posix(tmp_path):
    db = _db(tmp_path)
    photos = [FakePhoto("A", path="/fake/Old.photoslibrary/Masters/2010/A.jpg")]
    legacy_indexer.index_library(
        "/fake/Old.photoslibrary",
        db,
        curator_db_path=str(tmp_path / "curator.db"),
        thumb_root=tmp_path / "thumbs",
        copy_thumbnails=False,
        use_cache=False,
        photosdb_factory=_factory(photos),
    )
    row = next(db.iter_legacy_assets("LIB-UUID"))
    assert row["master_rel_path"] == "Masters/2010/A.jpg"


def test_thumbnail_copied_when_derivative_exists(tmp_path):
    db = _db(tmp_path)
    deriv = tmp_path / "deriv.jpg"
    deriv.write_bytes(b"JPEGDATA")
    photos = [FakePhoto("A", derivatives=[str(deriv)])]
    legacy_indexer.index_library(
        "/fake/Old.photoslibrary",
        db,
        curator_db_path=str(tmp_path / "curator.db"),
        thumb_root=tmp_path / "thumbs",
        copy_thumbnails=True,
        use_cache=False,
        photosdb_factory=_factory(photos),
    )
    row = next(db.iter_legacy_assets("LIB-UUID"))
    assert row["thumbnail_status"] == "ok"
    key = thumbnail_cache_key("LIB-UUID", "A")
    assert thumbnail_path(tmp_path / "thumbs", "LIB-UUID", key).exists()


def test_thumbnail_miss_records_status_does_not_fail(tmp_path):
    db = _db(tmp_path)
    photos = [FakePhoto("A", derivatives=[])]  # no derivative on disk
    stats = legacy_indexer.index_library(
        "/fake/Old.photoslibrary",
        db,
        curator_db_path=str(tmp_path / "curator.db"),
        thumb_root=tmp_path / "thumbs",
        copy_thumbnails=True,
        use_cache=False,
        photosdb_factory=_factory(photos),
    )
    assert stats["indexed"] == 1
    row = next(db.iter_legacy_assets("LIB-UUID"))
    assert row["thumbnail_status"] == "missing"


def test_full_run_reconciles_deleted_assets(tmp_path):
    db = _db(tmp_path)
    # First run: A and B present.
    legacy_indexer.index_library(
        "/fake/Old.photoslibrary",
        db,
        curator_db_path=str(tmp_path / "curator.db"),
        thumb_root=tmp_path / "thumbs",
        copy_thumbnails=False,
        use_cache=False,
        photosdb_factory=_factory([FakePhoto("A"), FakePhoto("B")]),
    )
    assert db.legacy_asset_count("LIB-UUID") == 2
    # Second full run: only A present -> B reconciled away.
    legacy_indexer.index_library(
        "/fake/Old.photoslibrary",
        db,
        curator_db_path=str(tmp_path / "curator.db"),
        thumb_root=tmp_path / "thumbs",
        copy_thumbnails=False,
        use_cache=False,
        photosdb_factory=_factory([FakePhoto("A")]),
    )
    uuids = {r["asset_uuid"] for r in db.iter_legacy_assets("LIB-UUID")}
    assert uuids == {"A"}


def test_limit_run_does_not_reconcile(tmp_path):
    db = _db(tmp_path)
    legacy_indexer.index_library(
        "/fake/Old.photoslibrary",
        db,
        curator_db_path=str(tmp_path / "curator.db"),
        thumb_root=tmp_path / "thumbs",
        copy_thumbnails=False,
        use_cache=False,
        photosdb_factory=_factory([FakePhoto("A"), FakePhoto("B")]),
    )
    # limit=1: non-authoritative, deletes nothing even though only 1 seen.
    legacy_indexer.index_library(
        "/fake/Old.photoslibrary",
        db,
        curator_db_path=str(tmp_path / "curator.db"),
        thumb_root=tmp_path / "thumbs",
        copy_thumbnails=False,
        use_cache=False,
        limit=1,
        photosdb_factory=_factory([FakePhoto("A"), FakePhoto("B")]),
    )
    assert db.legacy_asset_count("LIB-UUID") == 2


def test_logs_progress_every_interval(tmp_path, caplog, monkeypatch):
    db = _db(tmp_path)
    monkeypatch.setattr(legacy_indexer, "PROGRESS_INTERVAL", 2)
    photos = [FakePhoto(str(i)) for i in range(5)]
    with caplog.at_level(logging.INFO, logger="blue-pearmain.legacy-indexer"):
        legacy_indexer.index_library(
            "/fake/Old.photoslibrary",
            db,
            curator_db_path=str(tmp_path / "curator.db"),
            thumb_root=tmp_path / "thumbs",
            copy_thumbnails=False,
            use_cache=False,
            photosdb_factory=_factory(photos),
        )
    progress = [
        r.getMessage()
        for r in caplog.records
        if "indexed 2" in r.getMessage() or "indexed 4" in r.getMessage()
    ]
    # Progress at 2 and 4 (not at the final 5, which isn't a multiple of 2).
    assert any("indexed 2 " in m for m in progress)
    assert any("indexed 4 " in m for m in progress)


def test_interrupted_full_run_does_not_reconcile(tmp_path):
    db = _db(tmp_path)
    legacy_indexer.index_library(
        "/fake/Old.photoslibrary",
        db,
        curator_db_path=str(tmp_path / "curator.db"),
        thumb_root=tmp_path / "thumbs",
        copy_thumbnails=False,
        use_cache=False,
        photosdb_factory=_factory([FakePhoto("A"), FakePhoto("B")]),
    )

    class Boom(FakePhotosDB):
        def photos(self):
            yield FakePhoto("A")
            raise RuntimeError("share unmounted mid-iteration")

    def boom_factory(_path):
        return Boom([])

    import pytest

    with pytest.raises(RuntimeError):
        legacy_indexer.index_library(
            "/fake/Old.photoslibrary",
            db,
            curator_db_path=str(tmp_path / "curator.db"),
            thumb_root=tmp_path / "thumbs",
            copy_thumbnails=False,
            use_cache=False,
            photosdb_factory=boom_factory,
        )
    # B must survive: an interrupted full run reconciles nothing.
    assert db.legacy_asset_count("LIB-UUID") == 2


# ---------------------------------------------------------------------------
# _load_model_ids
# ---------------------------------------------------------------------------


def _make_photos4_db(tmp_path, rows: list[tuple[str, int]]) -> str:
    """Minimal photos.db with just enough RKVersion rows for model_id lookup."""
    db_path = str(tmp_path / "photos.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE RKVersion (modelId INTEGER PRIMARY KEY, uuid VARCHAR)")
    conn.executemany("INSERT INTO RKVersion VALUES (?, ?)", [(mid, uid) for uid, mid in rows])
    conn.commit()
    conn.close()
    return db_path


def _make_photos5_db(tmp_path, rows: list[tuple[str, int]]) -> str:
    """Minimal Photos.sqlite with ZGENERICASSET for Photos 5+ model_id lookup."""
    db_path = str(tmp_path / "Photos.sqlite")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE ZGENERICASSET (Z_PK INTEGER PRIMARY KEY, ZUUID VARCHAR)")
    conn.executemany("INSERT INTO ZGENERICASSET VALUES (?, ?)", [(pk, uuid) for uuid, pk in rows])
    conn.commit()
    conn.close()
    return db_path


def test_load_model_ids_returns_uuid_to_modelid(tmp_path):
    db = _make_photos4_db(tmp_path, [("uuid-A", 10), ("uuid-B", 255)])
    result = legacy_indexer._load_model_ids(db)
    assert result == {"uuid-A": 10, "uuid-B": 255}


def test_load_model_ids_prefers_zgenericasset_over_rkversion(tmp_path):
    """When Photos.sqlite has ZGENERICASSET, UUID4 keys from that table are returned."""
    db = _make_photos5_db(tmp_path, [("UUID4-A", 100), ("UUID4-B", 200)])
    result = legacy_indexer._load_model_ids(db)
    assert result == {"UUID4-A": 100, "UUID4-B": 200}


def test_load_model_ids_returns_empty_on_bad_path():
    result = legacy_indexer._load_model_ids("/nonexistent/photos.db")
    assert result == {}


# ---------------------------------------------------------------------------
# _derivatives_dir_photos4
# ---------------------------------------------------------------------------


def test_derivatives_dir_photos4_small_model_id():
    # model_id=1 → hex="1" → folder_id="00", nn_id="00", file_id="1"
    result = legacy_indexer._derivatives_dir_photos4(1, "/fake/lib")
    assert str(result) == "/fake/lib/resources/proxies/derivatives/00/00/1"


def test_derivatives_dir_photos4_typical_model_id():
    # model_id=100000 → hex="186a0" → folder_id="86", nn_id="01", file_id="186a0"
    result = legacy_indexer._derivatives_dir_photos4(100000, "/fake/lib")
    assert str(result) == "/fake/lib/resources/proxies/derivatives/86/01/186a0"


def test_derivatives_dir_photos4_returns_path_object():
    result = legacy_indexer._derivatives_dir_photos4(42, "/fake/lib")
    assert isinstance(result, Path)


# ---------------------------------------------------------------------------
# _copy_thumbnail — Photos 4 fallback
# ---------------------------------------------------------------------------


def test_copy_thumbnail_photos4_fallback_uses_real_bundle(tmp_path):
    """When path_derivatives is empty, falls back to _derivatives_dir_photos4."""
    # model_id=1 → dir: <real_lib>/resources/proxies/derivatives/00/00/1/
    real_lib = tmp_path / "Real.photoslibrary"
    deriv_dir = real_lib / "resources" / "proxies" / "derivatives" / "00" / "00" / "1"
    deriv_dir.mkdir(parents=True)
    (deriv_dir / "preview.jpg").write_bytes(b"X" * 200)

    photo = FakePhoto("uuid-1", derivatives=[])
    status = legacy_indexer._copy_thumbnail(
        photo,
        "LIB-UUID",
        tmp_path / "thumbs",
        real_library_path=str(real_lib),
        model_id=1,
    )
    assert status == "ok"
    key = thumbnail_cache_key("LIB-UUID", "uuid-1")
    assert thumbnail_path(tmp_path / "thumbs", "LIB-UUID", key).exists()


def test_copy_thumbnail_photos4_fallback_picks_smallest_file(tmp_path):
    """Heuristic: smallest file in derivatives dir is copied (genuine thumbnail, not preview)."""
    real_lib = tmp_path / "Real.photoslibrary"
    deriv_dir = real_lib / "resources" / "proxies" / "derivatives" / "00" / "00" / "1"
    deriv_dir.mkdir(parents=True)
    (deriv_dir / "thumb.jpg").write_bytes(b"X" * 10)
    (deriv_dir / "preview.jpg").write_bytes(b"X" * 500)

    photo = FakePhoto("uuid-1", derivatives=[])
    legacy_indexer._copy_thumbnail(
        photo,
        "LIB-UUID",
        tmp_path / "thumbs",
        real_library_path=str(real_lib),
        model_id=1,
    )
    key = thumbnail_cache_key("LIB-UUID", "uuid-1")
    dest = thumbnail_path(tmp_path / "thumbs", "LIB-UUID", key)
    assert dest.stat().st_size == 10  # smallest file was copied


def test_copy_thumbnail_photos4_fallback_missing_when_dir_absent(tmp_path):
    """Returns 'missing' when real bundle derivatives dir does not exist (NAS unmounted)."""
    photo = FakePhoto("uuid-1", derivatives=[])
    status = legacy_indexer._copy_thumbnail(
        photo,
        "LIB-UUID",
        tmp_path / "thumbs",
        real_library_path=str(tmp_path / "NotMounted.photoslibrary"),
        model_id=1,
    )
    assert status == "missing"


def test_copy_thumbnail_photos4_fallback_missing_when_no_model_id(tmp_path):
    """Returns 'missing' when model_id is None (UUID not in lookup map)."""
    real_lib = tmp_path / "Real.photoslibrary"
    deriv_dir = real_lib / "resources" / "proxies" / "derivatives" / "00" / "00" / "1"
    deriv_dir.mkdir(parents=True)
    (deriv_dir / "preview.jpg").write_bytes(b"DATA")

    photo = FakePhoto("uuid-1", derivatives=[])
    status = legacy_indexer._copy_thumbnail(
        photo,
        "LIB-UUID",
        tmp_path / "thumbs",
        real_library_path=str(real_lib),
        model_id=None,  # UUID not found in map
    )
    assert status == "missing"


def test_copy_thumbnail_fast_path_wins_when_derivatives_present(tmp_path):
    """Regression: when path_derivatives is already populated, it is used without fallback."""
    # Derivative provided by osxphotos (real-bundle open or --no-cache)
    deriv = tmp_path / "existing_deriv.jpg"
    deriv.write_bytes(b"FAST_PATH")

    # Fallback dir also exists, but should NOT be touched
    real_lib = tmp_path / "Real.photoslibrary"
    fallback_dir = real_lib / "resources" / "proxies" / "derivatives" / "00" / "00" / "1"
    fallback_dir.mkdir(parents=True)
    (fallback_dir / "fallback.jpg").write_bytes(b"FALLBACK_SHOULD_NOT_BE_USED" * 10)

    photo = FakePhoto("uuid-1", derivatives=[str(deriv)])
    legacy_indexer._copy_thumbnail(
        photo,
        "LIB-UUID",
        tmp_path / "thumbs",
        real_library_path=str(real_lib),
        model_id=1,
    )
    key = thumbnail_cache_key("LIB-UUID", "uuid-1")
    dest = thumbnail_path(tmp_path / "thumbs", "LIB-UUID", key)
    assert dest.read_bytes() == b"FAST_PATH"  # fast path, not fallback


def test_copy_thumbnail_photos4_fallback_handles_stat_failure(tmp_path):
    """stat() failure on one file in the derivatives dir is skipped; others still work."""
    from unittest.mock import patch

    real_lib = tmp_path / "Real.photoslibrary"
    deriv_dir = real_lib / "resources" / "proxies" / "derivatives" / "00" / "00" / "1"
    deriv_dir.mkdir(parents=True)
    good = deriv_dir / "good.jpg"
    good.write_bytes(b"GOOD" * 100)
    bad = deriv_dir / "bad.jpg"
    bad.write_bytes(b"BAD")

    original_stat = Path.stat

    def stat_raises_for_bad(self, **kwargs):
        if self.name == "bad.jpg":
            raise OSError("simulated stat failure")
        return original_stat(self, **kwargs)

    photo = FakePhoto("uuid-1", derivatives=[])
    with patch.object(Path, "stat", stat_raises_for_bad):
        status = legacy_indexer._copy_thumbnail(
            photo,
            "LIB-UUID",
            tmp_path / "thumbs",
            real_library_path=str(real_lib),
            model_id=1,
        )
    assert status == "ok"  # bad.jpg skipped; good.jpg copied


# ---------------------------------------------------------------------------
# index_library — model_id_map integration (Change A + Change B)
# ---------------------------------------------------------------------------


def test_index_library_loads_model_id_map_from_cache_db(tmp_path):
    """index_library wires model_id_map → _copy_thumbnail when the cache DB exists.

    Exercises the current cache-path contract. Update if cache layout changes.
    Uses photos.db / RKVersion (Photos 4 fallback path).
    """
    # FakePhotosDB uses library_uuid = "LIB-UUID" (from _uuid attribute).
    library_uuid = "LIB-UUID"
    curator_db = str(tmp_path / "curator.db")

    # Create minimal cache DB at the path index_library will look up.
    from legacy_cache import cache_dir as _cache_dir

    cache_db_dir = _cache_dir(curator_db, library_uuid) / "database"
    cache_db_dir.mkdir(parents=True)
    cache_conn = sqlite3.connect(str(cache_db_dir / "photos.db"))
    cache_conn.execute("CREATE TABLE RKVersion (modelId INTEGER PRIMARY KEY, uuid VARCHAR)")
    cache_conn.execute("INSERT INTO RKVersion VALUES (1, 'uuid-A')")
    cache_conn.commit()
    cache_conn.close()

    # Create derivative file at the path _derivatives_dir_photos4(1, real_lib) resolves to.
    real_lib = tmp_path / "Real.photoslibrary"
    deriv_dir = real_lib / "resources" / "proxies" / "derivatives" / "00" / "00" / "1"
    deriv_dir.mkdir(parents=True)
    (deriv_dir / "preview.jpg").write_bytes(b"PREVIEW_DATA")

    db = _db(tmp_path)
    photos = [FakePhoto("uuid-A", derivatives=[])]
    stats = legacy_indexer.index_library(
        str(real_lib),
        db,
        curator_db_path=curator_db,
        thumb_root=tmp_path / "thumbs",
        copy_thumbnails=True,
        use_cache=False,
        photosdb_factory=_factory(photos),
    )
    assert stats["thumb_ok"] == 1
    assert stats["thumb_missing"] == 0


def test_index_library_prefers_photos_sqlite_for_model_ids(tmp_path):
    """index_library picks Photos.sqlite (ZGENERICASSET) over photos.db when both exist.

    Mirrors osxphotos' DB selection: it opens Photos.sqlite first, returning
    ZGENERICASSET.ZUUID as photo.uuid. The model_id_map must use the same source.
    """
    library_uuid = "LIB-UUID"
    curator_db = str(tmp_path / "curator.db")

    from legacy_cache import cache_dir as _cache_dir

    cache_db_dir = _cache_dir(curator_db, library_uuid) / "database"
    cache_db_dir.mkdir(parents=True)

    # photos.db with RKVersion — has a *different* uuid ("wrong-uuid") so that if
    # the code incorrectly reads from it, model_id_map.get("uuid-A") returns None.
    wrong_conn = sqlite3.connect(str(cache_db_dir / "photos.db"))
    wrong_conn.execute("CREATE TABLE RKVersion (modelId INTEGER PRIMARY KEY, uuid VARCHAR)")
    wrong_conn.execute("INSERT INTO RKVersion VALUES (1, 'wrong-uuid')")
    wrong_conn.commit()
    wrong_conn.close()

    # Photos.sqlite with ZGENERICASSET — has the correct uuid ("uuid-A").
    right_conn = sqlite3.connect(str(cache_db_dir / "Photos.sqlite"))
    right_conn.execute("CREATE TABLE ZGENERICASSET (Z_PK INTEGER PRIMARY KEY, ZUUID VARCHAR)")
    right_conn.execute("INSERT INTO ZGENERICASSET VALUES (1, 'uuid-A')")
    right_conn.commit()
    right_conn.close()

    real_lib = tmp_path / "Real.photoslibrary"
    deriv_dir = real_lib / "resources" / "proxies" / "derivatives" / "00" / "00" / "1"
    deriv_dir.mkdir(parents=True)
    (deriv_dir / "preview.jpg").write_bytes(b"PREVIEW_DATA")

    db = _db(tmp_path)
    photos = [FakePhoto("uuid-A", derivatives=[])]
    stats = legacy_indexer.index_library(
        str(real_lib),
        db,
        curator_db_path=curator_db,
        thumb_root=tmp_path / "thumbs",
        copy_thumbnails=True,
        use_cache=False,
        photosdb_factory=_factory(photos),
    )
    assert stats["thumb_ok"] == 1
    assert stats["thumb_missing"] == 0
