# tests/test_sync_geo.py
"""sync_geo() — geo proposal detection (#145)."""

from __future__ import annotations
import json
import tempfile
from pathlib import Path
import pytest
from db.db import Database
from flickr.geo_sync import sync_geo, GEO_CREATE_THRESHOLD_M, GEO_SUPPRESS_THRESHOLD_M


def _photo(i: int, **kwargs) -> dict:
    return {
        "uuid": f"geo-sync-u{i}",
        "flickr_id": f"geo-sync-f{i}",
        "original_filename": f"IMG_{i:04d}.JPG",
        "privacy_state": "needs_review",
        "apple_persons": [],
        "apple_labels": [],
        **kwargs,
    }


@pytest.fixture()
def db():
    with tempfile.TemporaryDirectory() as tmp:
        yield Database(Path(tmp) / "t.db")


class TestSyncGeo:
    def test_flickr_only_creates_non_conflict_proposal(self, db):
        pid = db.upsert_photo(_photo(1, flickr_latitude=42.3601, flickr_longitude=-71.0589))
        sync_geo(db, dry_run=False, photo_ids=[pid])
        row = db.conn.execute(
            "SELECT field, source, target, conflict_type FROM metadata_proposals WHERE photo_id=?",
            (pid,),
        ).fetchone()
        assert row["field"] == "geo_location"
        assert row["source"] == "flickr"
        assert row["target"] == "photos"
        assert row["conflict_type"] == "non_conflict"

    def test_photos_only_creates_non_conflict_proposal(self, db):
        pid = db.upsert_photo(_photo(2, photos_latitude=42.3601, photos_longitude=-71.0589))
        sync_geo(db, dry_run=False, photo_ids=[pid])
        row = db.conn.execute(
            "SELECT source, target FROM metadata_proposals WHERE photo_id=?",
            (pid,),
        ).fetchone()
        assert row["source"] == "photos"
        assert row["target"] == "flickr"

    def test_both_absent_creates_no_proposal(self, db):
        pid = db.upsert_photo(_photo(3))
        sync_geo(db, dry_run=False, photo_ids=[pid])
        count = db.conn.execute(
            "SELECT COUNT(*) AS n FROM metadata_proposals WHERE photo_id=?", (pid,)
        ).fetchone()["n"]
        assert count == 0

    def test_coords_agree_within_threshold_creates_no_proposal(self, db):
        # 500m apart — under the 1km threshold
        pid = db.upsert_photo(
            _photo(
                4,
                flickr_latitude=42.3601,
                flickr_longitude=-71.0589,
                photos_latitude=42.3646,
                photos_longitude=-71.0589,  # ~500m north
            )
        )
        sync_geo(db, dry_run=False, photo_ids=[pid])
        count = db.conn.execute(
            "SELECT COUNT(*) AS n FROM metadata_proposals WHERE photo_id=?", (pid,)
        ).fetchone()["n"]
        assert count == 0

    def test_divergence_creates_two_proposals(self, db):
        # Fenway vs Seoul — ~10,900 km apart
        pid = db.upsert_photo(
            _photo(
                5,
                flickr_latitude=42.3601,
                flickr_longitude=-71.0589,
                photos_latitude=37.5665,
                photos_longitude=126.9780,
            )
        )
        sync_geo(db, dry_run=False, photo_ids=[pid])
        rows = db.conn.execute(
            "SELECT source, target, conflict_type FROM metadata_proposals WHERE photo_id=?",
            (pid,),
        ).fetchall()
        assert len(rows) == 2
        directions = {(r["source"], r["target"]) for r in rows}
        assert ("flickr", "photos") in directions
        assert ("photos", "flickr") in directions
        assert all(r["conflict_type"] == "divergence" for r in rows)

    def test_divergence_stores_distance_m_in_proposed_value(self, db):
        pid = db.upsert_photo(
            _photo(
                6,
                flickr_latitude=42.3601,
                flickr_longitude=-71.0589,
                photos_latitude=37.5665,
                photos_longitude=126.9780,
            )
        )
        sync_geo(db, dry_run=False, photo_ids=[pid])
        row = db.conn.execute(
            "SELECT proposed_value FROM metadata_proposals WHERE photo_id=? AND source='flickr'",
            (pid,),
        ).fetchone()
        payload = json.loads(row["proposed_value"])
        assert "distance_m" in payload
        assert payload["distance_m"] > 1_000_000  # Seoul–Boston is ~10,900 km

    def test_threshold_boundary_below_no_proposal(self, db):
        lat1, lon1 = 42.3601, -71.0589
        dlat = (GEO_CREATE_THRESHOLD_M - 1) / 111_319.9
        lat2 = lat1 + dlat
        pid = db.upsert_photo(
            _photo(
                7,
                flickr_latitude=lat1,
                flickr_longitude=lon1,
                photos_latitude=lat2,
                photos_longitude=lon1,
            )
        )
        sync_geo(db, dry_run=False, photo_ids=[pid])
        count = db.conn.execute(
            "SELECT COUNT(*) AS n FROM metadata_proposals WHERE photo_id=?", (pid,)
        ).fetchone()["n"]
        assert count == 0

    def test_threshold_boundary_above_creates_proposal(self, db):
        lat1, lon1 = 42.3601, -71.0589
        dlat = (GEO_CREATE_THRESHOLD_M + 1) / 111_319.9
        lat2 = lat1 + dlat
        pid = db.upsert_photo(
            _photo(
                8,
                flickr_latitude=lat1,
                flickr_longitude=lon1,
                photos_latitude=lat2,
                photos_longitude=lon1,
            )
        )
        sync_geo(db, dry_run=False, photo_ids=[pid])
        count = db.conn.execute(
            "SELECT COUNT(*) AS n FROM metadata_proposals WHERE photo_id=?", (pid,)
        ).fetchone()["n"]
        assert count > 0

    def test_geo_confirmed_none_photos_skipped(self, db):
        pid = db.upsert_photo(
            _photo(
                9,
                flickr_latitude=42.3601,
                flickr_longitude=-71.0589,
                geo_confirmed_none=1,
            )
        )
        sync_geo(db, dry_run=False, photo_ids=[pid])
        count = db.conn.execute(
            "SELECT COUNT(*) AS n FROM metadata_proposals WHERE photo_id=?", (pid,)
        ).fetchone()["n"]
        assert count == 0

    def test_dry_run_creates_no_proposals(self, db):
        pid = db.upsert_photo(_photo(10, flickr_latitude=42.3601, flickr_longitude=-71.0589))
        totals = sync_geo(db, dry_run=True, photo_ids=[pid])
        count = db.conn.execute(
            "SELECT COUNT(*) AS n FROM metadata_proposals WHERE photo_id=?", (pid,)
        ).fetchone()["n"]
        assert count == 0
        assert totals["proposals_created"] == 0

    def test_return_dict_has_granular_counters(self, db):
        """sync_geo() return dict must include all observability counters."""
        totals = sync_geo(db, dry_run=False, photo_ids=[])
        assert "proposals_created" in totals
        assert "suppressed_confirmed_none" in totals
        assert "suppressed_in_band" in totals
        assert "suppressed_under_threshold" in totals
        assert "suppressed_both_absent" in totals
        assert "suppressed_not_linked" in totals
        assert "failed" in totals

    def test_confirmed_none_increments_suppressed_counter(self, db):
        pid = db.upsert_photo(
            _photo(
                15,
                flickr_latitude=42.3601,
                flickr_longitude=-71.0589,
                geo_confirmed_none=1,
            )
        )
        totals = sync_geo(db, dry_run=False, photo_ids=[pid])
        assert totals["suppressed_confirmed_none"] == 1
        assert totals["proposals_created"] == 0

    def test_in_band_increments_suppressed_in_band_counter(self, db):
        lat1, lon1 = 42.3601, -71.0589
        # 999m — just below the create threshold, squarely in the hysteresis band
        dlat = (GEO_CREATE_THRESHOLD_M - 1) / 111_319.9
        pid = db.upsert_photo(
            _photo(
                16,
                flickr_latitude=lat1,
                flickr_longitude=lon1,
                photos_latitude=lat1 + dlat,
                photos_longitude=lon1,
            )
        )
        totals = sync_geo(db, dry_run=False, photo_ids=[pid])
        assert totals["suppressed_in_band"] == 1
        assert totals["proposals_created"] == 0

    def test_supersede_uses_directional_key(self, db):
        """A re-sync of flickr→photos does NOT supersede an existing photos→flickr proposal."""
        pid = db.upsert_photo(
            _photo(
                11,
                flickr_latitude=42.3601,
                flickr_longitude=-71.0589,
                photos_latitude=37.5665,
                photos_longitude=126.9780,
            )
        )
        sync_geo(db, dry_run=False, photo_ids=[pid])
        db.conn.execute(
            "UPDATE metadata_proposals SET status='rejected' WHERE photo_id=? AND source='photos'",
            (pid,),
        )
        db.conn.commit()
        sync_geo(db, dry_run=False, photo_ids=[pid])
        rejected = db.conn.execute(
            "SELECT status FROM metadata_proposals WHERE photo_id=? AND source='photos' ORDER BY id DESC LIMIT 1",
            (pid,),
        ).fetchone()
        assert rejected["status"] == "rejected"

    def test_photo_missing_flickr_id_skipped(self, db):
        """Photos without flickr_id are skipped (can't sync to Flickr)."""
        pid = db.upsert_photo(
            {
                "uuid": "no-flickr-uuid",
                "original_filename": "IMG_NF.JPG",
                "privacy_state": "needs_review",
                "apple_persons": [],
                "apple_labels": [],
                "photos_latitude": 42.3601,
                "photos_longitude": -71.0589,
            }
        )
        totals = sync_geo(db, dry_run=False, photo_ids=[pid])
        count = db.conn.execute(
            "SELECT COUNT(*) AS n FROM metadata_proposals WHERE photo_id=?", (pid,)
        ).fetchone()["n"]
        assert count == 0
        assert totals["suppressed_not_linked"] == 1

    def test_band_creates_no_proposal(self, db):
        """Distance in hysteresis band (800m < dist <= 1000m): no proposal created."""
        lat1, lon1 = 42.3601, -71.0589
        # midpoint of the hysteresis band — stays correct if thresholds change
        mid_m = (GEO_SUPPRESS_THRESHOLD_M + GEO_CREATE_THRESHOLD_M) // 2
        dlat = mid_m / 111_319.9
        pid = db.upsert_photo(
            _photo(
                17,
                flickr_latitude=lat1,
                flickr_longitude=lon1,
                photos_latitude=lat1 + dlat,
                photos_longitude=lon1,
            )
        )
        totals = sync_geo(db, dry_run=False, photo_ids=[pid])
        count = db.conn.execute(
            "SELECT COUNT(*) AS n FROM metadata_proposals WHERE photo_id=?", (pid,)
        ).fetchone()["n"]
        assert count == 0
        assert totals["suppressed_in_band"] == 1

    def test_below_suppress_threshold_increments_suppressed_under_threshold(self, db):
        lat1, lon1 = 42.3601, -71.0589
        # 1m below the suppress threshold — clearly in the under-threshold zone
        dlat = (GEO_SUPPRESS_THRESHOLD_M - 1) / 111_319.9
        pid = db.upsert_photo(
            _photo(
                18,
                flickr_latitude=lat1,
                flickr_longitude=lon1,
                photos_latitude=lat1 + dlat,
                photos_longitude=lon1,
            )
        )
        totals = sync_geo(db, dry_run=False, photo_ids=[pid])
        assert totals["suppressed_under_threshold"] == 1
        assert totals["suppressed_in_band"] == 0


class TestSupersedeIsolation:
    def test_text_sync_does_not_supersede_geo_proposals(self, db):
        """run_sync_engine() text-field supersede must not wipe geo proposals."""
        from flickr.metadata_puller import run_sync_engine

        pid = db.upsert_photo(
            {
                "uuid": "supersede-test-u1",
                "flickr_id": "supersede-test-f1",
                "original_filename": "IMG_ST.JPG",
                "privacy_state": "needs_review",
                "apple_persons": [],
                "apple_labels": [],
                "flickr_latitude": 42.3601,
                "flickr_longitude": -71.0589,
            }
        )
        db.conn.execute(
            "INSERT INTO metadata_proposals"
            " (photo_id, field, source, target, conflict_type, status, created_at)"
            " VALUES (?, 'geo_location', 'flickr', 'photos', 'non_conflict', 'pending', datetime('now'))",
            (pid,),
        )
        db.conn.commit()

        run_sync_engine(
            db,
            [
                {
                    "id": pid,
                    "flickr_id": "supersede-test-f1",
                    "flickr_title": "New title",
                    "photos_title": "Old title",
                }
            ],
            dry_run=False,
        )

        row = db.conn.execute(
            "SELECT status FROM metadata_proposals WHERE photo_id=? AND field='geo_location'",
            (pid,),
        ).fetchone()
        assert row is not None
        assert row["status"] == "pending", (
            "geo proposal was superseded by a text sync run — "
            "run_sync_engine() must scope supersede to field IN ('title','description','tags')"
        )
