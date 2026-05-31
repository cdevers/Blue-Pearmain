# tests/test_legacy_indexer.py
from __future__ import annotations

import json
import logging
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
