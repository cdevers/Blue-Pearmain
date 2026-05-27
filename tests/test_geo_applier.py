# tests/test_geo_applier.py
"""apply_geo_proposal() — write geo to Photos or Flickr (#145)."""

from __future__ import annotations
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest
from db.db import Database
from flickr.proposal_applier import apply_proposal, apply_geo_reverse


def _photo(i: int, **kwargs) -> dict:
    return {
        "uuid": f"geo-apply-u{i}",
        "flickr_id": f"geo-apply-f{i}",
        "original_filename": f"IMG_{i:04d}.JPG",
        "privacy_state": "needs_review",
        "apple_persons": [],
        "apple_labels": [],
        **kwargs,
    }


def _insert_proposal(
    db: Database,
    photo_id: int,
    source: str,
    target: str,
    conflict_type: str = "non_conflict",
    lat: float = 42.3601,
    lon: float = -71.0589,
    current_lat: float | None = None,
    current_lon: float | None = None,
    distance_m: int | None = None,
) -> int:
    payload: dict = {"lat": lat, "lon": lon}
    if distance_m is not None:
        assert current_lat is not None
        payload.update(
            {"current_lat": current_lat, "current_lon": current_lon, "distance_m": distance_m}
        )
    db.conn.execute(
        """INSERT INTO metadata_proposals
           (photo_id, field, proposed_value, source, target, conflict_type, status, created_at)
           VALUES (?, 'geo_location', ?, ?, ?, ?, 'pending', datetime('now'))""",
        (photo_id, json.dumps(payload), source, target, conflict_type),
    )
    db.conn.commit()
    return db.conn.execute("SELECT last_insert_rowid()").fetchone()[0]


@pytest.fixture()
def db():
    with tempfile.TemporaryDirectory() as tmp:
        yield Database(Path(tmp) / "t.db")


class TestApplyGeoProposal:
    def test_apply_flickr_to_photos_calls_photoscript(self, db):
        pid = db.upsert_photo(_photo(1))
        prop_id = _insert_proposal(db, pid, source="flickr", target="photos")
        mock_photo = MagicMock()
        with (
            patch("flickr.proposal_applier._photos_is_responsive", return_value=True),
            patch("photoscript.Photo", return_value=mock_photo),
        ):
            result = apply_proposal(db, prop_id, library_path="/tmp")
        assert result["ok"] is True

    def test_apply_flickr_to_photos_marks_applied(self, db):
        pid = db.upsert_photo(_photo(2))
        prop_id = _insert_proposal(db, pid, source="flickr", target="photos")
        mock_photo = MagicMock()
        with (
            patch("flickr.proposal_applier._photos_is_responsive", return_value=True),
            patch("photoscript.Photo", return_value=mock_photo),
        ):
            apply_proposal(db, prop_id, library_path="/tmp")
        row = db.conn.execute(
            "SELECT status FROM metadata_proposals WHERE id=?", (prop_id,)
        ).fetchone()
        assert row["status"] == "applied"

    def test_apply_photos_to_flickr_calls_set_location(self, db):
        pid = db.upsert_photo(_photo(3))
        prop_id = _insert_proposal(db, pid, source="photos", target="flickr")
        mock_client = MagicMock()
        result = apply_proposal(db, prop_id, library_path="/tmp", flickr_client=mock_client)
        assert result["ok"] is True
        mock_client.set_location.assert_called_once_with("geo-apply-f3", 42.3601, -71.0589)

    def test_apply_photos_to_flickr_marks_applied(self, db):
        pid = db.upsert_photo(_photo(4))
        prop_id = _insert_proposal(db, pid, source="photos", target="flickr")
        mock_client = MagicMock()
        apply_proposal(db, prop_id, library_path="/tmp", flickr_client=mock_client)
        row = db.conn.execute(
            "SELECT status FROM metadata_proposals WHERE id=?", (prop_id,)
        ).fetchone()
        assert row["status"] == "applied"

    def test_apply_geo_does_not_require_distance_m(self, db):
        """non_conflict proposals have no distance_m — must still apply."""
        pid = db.upsert_photo(_photo(5))
        prop_id = _insert_proposal(
            db, pid, source="flickr", target="photos", conflict_type="non_conflict"
        )
        mock_photo = MagicMock()
        with (
            patch("flickr.proposal_applier._photos_is_responsive", return_value=True),
            patch("photoscript.Photo", return_value=mock_photo),
        ):
            result = apply_proposal(db, prop_id, library_path="/tmp")
        assert result["ok"] is True

    def test_apply_flickr_to_photos_updates_db_cache(self, db):
        pid = db.upsert_photo(_photo(6))
        prop_id = _insert_proposal(
            db, pid, source="flickr", target="photos", lat=42.3601, lon=-71.0589
        )
        mock_photo = MagicMock()
        with (
            patch("flickr.proposal_applier._photos_is_responsive", return_value=True),
            patch("photoscript.Photo", return_value=mock_photo),
        ):
            apply_proposal(db, prop_id, library_path="/tmp")
        row = db.conn.execute(
            "SELECT photos_latitude, photos_longitude, latitude, longitude FROM photos WHERE id=?",
            (pid,),
        ).fetchone()
        assert row["photos_latitude"] == pytest.approx(42.3601)
        assert row["latitude"] == pytest.approx(42.3601)

    def test_apply_geo_reverse_for_divergence(self, db):
        """apply_geo_reverse() applies Photos coords to Flickr."""
        pid = db.upsert_photo(
            _photo(
                7,
                photos_latitude=37.5665,
                photos_longitude=126.9780,
                flickr_latitude=42.3601,
                flickr_longitude=-71.0589,
            )
        )
        prop_id = _insert_proposal(
            db,
            pid,
            source="flickr",
            target="photos",
            conflict_type="divergence",
            lat=42.3601,
            lon=-71.0589,
            current_lat=37.5665,
            current_lon=126.9780,
            distance_m=10_900_000,
        )
        mock_client = MagicMock()
        result = apply_geo_reverse(db, prop_id, flickr_client=mock_client)
        assert result["ok"] is True
        mock_client.set_location.assert_called_once_with(
            "geo-apply-f7", pytest.approx(37.5665), pytest.approx(126.9780)
        )
