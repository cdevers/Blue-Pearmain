# tests/test_legacy_report.py
"""Tests for bp legacy-report (#229): legacy assets with no Flickr counterpart."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "poller"))


def _make_db(tmp_path):
    from db.db import Database
    from db.migrations.migrate_026_legacy_index import run_on_conn

    db = Database(str(tmp_path / "curator.db"))
    run_on_conn(db.conn)
    return db


def _lib(db, library_uuid="LIB"):
    db.set_legacy_library({"library_uuid": library_uuid, "display_name": "Test"})


def _asset(db, asset_uuid, date_taken, library_uuid="LIB", **kw):
    db.upsert_legacy_asset(
        {
            "library_uuid": library_uuid,
            "asset_uuid": asset_uuid,
            "original_filename": f"{asset_uuid}.jpg",
            "date_taken": date_taken,
            "named_face_count": 0,
            "unknown_face_count": 0,
            **kw,
        }
    )


def _photo(db, date_taken, *, uuid=None, privacy_state="candidate_public"):
    db.conn.execute(
        "INSERT INTO photos (uuid, flickr_id, date_taken, privacy_state) VALUES (?, ?, ?, ?)",
        (uuid, f"F_{date_taken}", date_taken, privacy_state),
    )
    db.conn.commit()


# ── Core counts ───────────────────────────────────────────────────────────────


def test_empty_library(tmp_path):
    db = _make_db(tmp_path)
    _lib(db)
    from legacy_report import report_unmatched

    r = report_unmatched(db, "LIB")
    assert r["total"] == 0
    assert r["matched"] == 0
    assert r["unmatched"] == 0
    assert r["no_date"] == 0
    assert r["assets"] == []
    assert r["by_year"] == {}


def test_all_matched(tmp_path):
    db = _make_db(tmp_path)
    _lib(db)
    _asset(db, "A", "2010-06-01 12:00:00")
    _photo(db, "2010-06-01 12:00:00")
    from legacy_report import report_unmatched

    r = report_unmatched(db, "LIB")
    assert r["total"] == 1
    assert r["matched"] == 1
    assert r["unmatched"] == 0
    assert r["assets"] == []


def test_none_matched(tmp_path):
    db = _make_db(tmp_path)
    _lib(db)
    _asset(db, "A", "2010-06-01 12:00:00")
    _asset(db, "B", "2011-03-15 08:30:00")
    from legacy_report import report_unmatched

    r = report_unmatched(db, "LIB")
    assert r["total"] == 2
    assert r["matched"] == 0
    assert r["unmatched"] == 2
    assert len(r["assets"]) == 2


def test_partial_match(tmp_path):
    db = _make_db(tmp_path)
    _lib(db)
    _asset(db, "A", "2010-06-01 12:00:00")  # will match
    _asset(db, "B", "2011-03-15 08:30:00")  # won't match
    _photo(db, "2010-06-01 12:00:00")
    from legacy_report import report_unmatched

    r = report_unmatched(db, "LIB")
    assert r["matched"] == 1
    assert r["unmatched"] == 1
    assert r["assets"][0]["asset_uuid"] == "B"


def test_no_date_asset_counted_separately(tmp_path):
    db = _make_db(tmp_path)
    _lib(db)
    _asset(db, "A", None)
    from legacy_report import report_unmatched

    r = report_unmatched(db, "LIB")
    assert r["total"] == 1
    assert r["no_date"] == 1
    assert r["unmatched"] == 0
    assert r["matched"] == 0
    assert r["assets"] == []


# ── by_year breakdown ─────────────────────────────────────────────────────────


def test_by_year_breakdown(tmp_path):
    db = _make_db(tmp_path)
    _lib(db)
    _asset(db, "A", "2005-01-01 10:00:00")
    _asset(db, "B", "2005-07-04 12:00:00")
    _asset(db, "C", "2009-12-31 23:59:59")
    from legacy_report import report_unmatched

    r = report_unmatched(db, "LIB")
    assert r["by_year"]["2005"] == 2
    assert r["by_year"]["2009"] == 1


def test_no_date_excluded_from_by_year(tmp_path):
    db = _make_db(tmp_path)
    _lib(db)
    _asset(db, "A", None)
    _asset(db, "B", "2005-01-01 10:00:00")
    from legacy_report import report_unmatched

    r = report_unmatched(db, "LIB")
    assert "unknown" not in r["by_year"]
    assert r["no_date"] == 1


def test_matched_assets_excluded_from_by_year(tmp_path):
    db = _make_db(tmp_path)
    _lib(db)
    _asset(db, "A", "2010-06-01 12:00:00")  # matched
    _asset(db, "B", "2005-07-04 12:00:00")  # unmatched
    _photo(db, "2010-06-01 12:00:00")
    from legacy_report import report_unmatched

    r = report_unmatched(db, "LIB")
    assert "2010" not in r["by_year"]
    assert r["by_year"]["2005"] == 1


# ── Matching semantics ────────────────────────────────────────────────────────


def test_auto_private_photo_counts_as_on_flickr(tmp_path):
    """Privacy state is irrelevant — if a Flickr photo has this timestamp it's on Flickr."""
    db = _make_db(tmp_path)
    _lib(db)
    _asset(db, "A", "2010-06-01 12:00:00")
    _photo(db, "2010-06-01 12:00:00", privacy_state="auto_private")
    from legacy_report import report_unmatched

    r = report_unmatched(db, "LIB")
    assert r["matched"] == 1
    assert r["unmatched"] == 0


def test_uuid_photo_counts_as_matched(tmp_path):
    """A photo already in the active Photos library (uuid set) still means it's on Flickr."""
    db = _make_db(tmp_path)
    _lib(db)
    _asset(db, "A", "2010-06-01 12:00:00")
    _photo(db, "2010-06-01 12:00:00", uuid="APPLE-UUID", privacy_state="approved_public")
    from legacy_report import report_unmatched

    r = report_unmatched(db, "LIB")
    assert r["matched"] == 1
    assert r["unmatched"] == 0


def test_multiple_assets_same_timestamp_all_matched(tmp_path):
    """Burst shots — two legacy assets at the same second, one Flickr photo → both matched."""
    db = _make_db(tmp_path)
    _lib(db)
    _asset(db, "A", "2010-06-01 12:00:00")
    _asset(db, "B", "2010-06-01 12:00:00")
    _photo(db, "2010-06-01 12:00:00")
    from legacy_report import report_unmatched

    r = report_unmatched(db, "LIB")
    assert r["matched"] == 2
    assert r["unmatched"] == 0


def test_library_uuid_scoping(tmp_path):
    """Assets from a different library are not included in the report."""
    db = _make_db(tmp_path)
    _lib(db, "LIB1")
    _lib(db, "LIB2")
    _asset(db, "A", "2010-06-01 12:00:00", library_uuid="LIB1")
    _asset(db, "B", "2011-01-01 10:00:00", library_uuid="LIB2")
    from legacy_report import report_unmatched

    r = report_unmatched(db, "LIB1")
    assert r["total"] == 1
    assert r["assets"][0]["asset_uuid"] == "A"
