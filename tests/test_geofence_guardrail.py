"""
tests/test_geofence_guardrail.py — tests for the geofence/person-policy guardrail

Run from repo root:
    python -m pytest tests/test_geofence_guardrail.py -v
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.db import Database


@pytest.fixture()
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    yield d
    d.close()


class TestReviewQueueGeofenceFields:
    def test_review_queue_returns_geofence_zone(self, db):
        db.upsert_photo(
            {
                "uuid": "uuid-geo-1",
                "original_filename": "IMG_001.JPG",
                "privacy_state": "candidate_public",
                "geofence_zone": "work",
                "apple_persons": [],
                "proposed_tags": [],
            }
        )
        photos = db.review_queue(states=["candidate_public"])
        assert len(photos) == 1
        assert photos[0]["geofence_zone"] == "work"

    def test_review_queue_returns_none_geofence_zone_when_unset(self, db):
        db.upsert_photo(
            {
                "uuid": "uuid-geo-2",
                "original_filename": "IMG_002.JPG",
                "privacy_state": "candidate_public",
                "apple_persons": [],
                "proposed_tags": [],
            }
        )
        photos = db.review_queue(states=["candidate_public"])
        assert len(photos) == 1
        assert photos[0]["geofence_zone"] is None

    def test_review_queue_returns_apple_persons_as_list(self, db):
        db.upsert_photo(
            {
                "uuid": "uuid-geo-3",
                "original_filename": "IMG_003.JPG",
                "privacy_state": "candidate_public",
                "apple_persons": ["Jane Smith", "Bob Jones"],
                "proposed_tags": [],
            }
        )
        photos = db.review_queue(states=["candidate_public"])
        assert len(photos) == 1
        assert isinstance(photos[0]["apple_persons"], list)
        assert "Jane Smith" in photos[0]["apple_persons"]

    def test_review_queue_returns_empty_list_when_no_persons(self, db):
        db.upsert_photo(
            {
                "uuid": "uuid-geo-4",
                "original_filename": "IMG_004.JPG",
                "privacy_state": "candidate_public",
                "apple_persons": [],
                "proposed_tags": [],
            }
        )
        photos = db.review_queue(states=["candidate_public"])
        assert len(photos) == 1
        assert photos[0]["apple_persons"] == []

    def test_review_queue_returns_privacy_reason(self, db):
        db.upsert_photo(
            {
                "uuid": "uuid-geo-5",
                "original_filename": "IMG_005.JPG",
                "privacy_state": "candidate_public",
                "privacy_reason": "no people detected",
                "apple_persons": [],
                "proposed_tags": [],
            }
        )
        photos = db.review_queue(states=["candidate_public"])
        assert len(photos) == 1
        assert photos[0]["privacy_reason"] == "no people detected"
