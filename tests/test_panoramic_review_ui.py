"""
tests/test_panoramic_review_ui.py — tests for panoramic photo handling in the review UI

Run from repo root:
    python -m pytest tests/test_panoramic_review_ui.py -v
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


class TestReviewQueueDimensions:
    def test_review_queue_returns_width_and_height(self, db):
        db.upsert_photo(
            {
                "uuid": "uuid-pano-1",
                "original_filename": "PANO_001.JPG",
                "privacy_state": "candidate_public",
                "width": 5000,
                "height": 1000,
                "apple_persons": [],
                "proposed_tags": [],
            }
        )
        photos = db.review_queue(states=["candidate_public"])
        assert len(photos) == 1
        assert photos[0]["width"] == 5000
        assert photos[0]["height"] == 1000

    def test_review_queue_returns_none_when_dimensions_absent(self, db):
        db.upsert_photo(
            {
                "uuid": "uuid-pano-2",
                "original_filename": "IMG_002.JPG",
                "privacy_state": "candidate_public",
                "apple_persons": [],
                "proposed_tags": [],
            }
        )
        photos = db.review_queue(states=["candidate_public"])
        assert len(photos) == 1
        # width/height may be None or 0 when not set; either is acceptable
        assert photos[0].get("width") in (None, 0)
        assert photos[0].get("height") in (None, 0)

    def test_review_queue_returns_non_panoramic_dimensions(self, db):
        db.upsert_photo(
            {
                "uuid": "uuid-pano-3",
                "original_filename": "IMG_003.JPG",
                "privacy_state": "candidate_public",
                "width": 4032,
                "height": 3024,
                "apple_persons": [],
                "proposed_tags": [],
            }
        )
        photos = db.review_queue(states=["candidate_public"])
        assert len(photos) == 1
        assert photos[0]["width"] == 4032
        assert photos[0]["height"] == 3024
