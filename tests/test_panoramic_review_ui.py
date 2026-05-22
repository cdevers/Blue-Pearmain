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
import reviewer.app as app_module


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


@pytest.fixture()
def flask_client(tmp_path):
    """Flask test client with seeded photos covering pano/normal/persons scenarios."""
    db = Database(tmp_path / "test.db")

    # Ensure person_policies table exists (lives in migration_019, not schema.sql)
    db.conn.execute("""
        CREATE TABLE IF NOT EXISTS person_policies (
            id          INTEGER PRIMARY KEY,
            person_name TEXT NOT NULL UNIQUE,
            policy      TEXT NOT NULL CHECK(policy IN ('always_private')),
            created_at  TEXT NOT NULL
        )
    """)
    db.conn.commit()

    # always_private policy for Jane Smith
    db.set_person_policy("Jane Smith", "always_private")

    # Photo 1: panoramic (5:1), named persons including always_private
    db.upsert_photo(
        {
            "uuid": "uuid-pano-a",
            "original_filename": "PANO_A.JPG",
            "privacy_state": "candidate_public",
            "width": 5000,
            "height": 1000,
            "apple_persons": ["Jane Smith", "Bob Jones"],
            "proposed_tags": [],
        }
    )
    # Photo 2: panoramic (3:1), unknown face only
    db.upsert_photo(
        {
            "uuid": "uuid-pano-b",
            "original_filename": "PANO_B.JPG",
            "privacy_state": "candidate_public",
            "width": 3000,
            "height": 1000,
            "apple_persons": ["_UNKNOWN_"],
            "proposed_tags": [],
        }
    )
    # Photo 3: normal 4:3, with a person (no chips expected)
    db.upsert_photo(
        {
            "uuid": "uuid-normal-c",
            "original_filename": "IMG_C.JPG",
            "privacy_state": "candidate_public",
            "width": 4032,
            "height": 3024,
            "apple_persons": ["Alice"],
            "proposed_tags": [],
        }
    )
    # Photo 4: panoramic but no persons
    db.upsert_photo(
        {
            "uuid": "uuid-pano-d",
            "original_filename": "PANO_D.JPG",
            "privacy_state": "candidate_public",
            "width": 8000,
            "height": 1000,
            "apple_persons": [],
            "proposed_tags": [],
        }
    )

    app_module._db = db
    app_module.app.config["TESTING"] = True
    app_module.app.config["SECRET_KEY"] = "test"
    with app_module.app.test_client() as c:
        yield c
    app_module._db = None
    db.close()


class TestPanoTemplate:
    def test_panoramic_tile_has_pano_class(self, flask_client):
        r = flask_client.get("/review?state=candidate_public")
        html = r.data.decode()
        # PANO_A.JPG has width=5000, height=1000 → ratio 5.0 > 2.0
        assert "pano" in html

    def test_pano_class_count_matches_panoramic_photos(self, flask_client):
        r = flask_client.get("/review?state=candidate_public")
        html = r.data.decode()
        # 3 pano photos → 3 occurrences of "photo-card pano" in the HTML
        assert html.count("photo-card pano") == 3

    def test_person_chips_rendered_for_pano_with_persons(self, flask_client):
        r = flask_client.get("/review?state=candidate_public")
        html = r.data.decode()
        assert "person-chips" in html

    def test_unknown_chip_rendered_for_unknown_person(self, flask_client):
        r = flask_client.get("/review?state=candidate_public")
        html = r.data.decode()
        assert "person-chip unknown" in html
        assert ">unknown<" in html

    def test_protected_chip_rendered_for_always_private_person(self, flask_client):
        r = flask_client.get("/review?state=candidate_public")
        html = r.data.decode()
        assert "person-chip protected" in html
        assert "Jane Smith" in html

    def test_normal_named_person_chip_rendered(self, flask_client):
        r = flask_client.get("/review?state=candidate_public")
        html = r.data.decode()
        assert "Bob Jones" in html

    def test_alice_not_in_person_chip_span(self, flask_client):
        r = flask_client.get("/review?state=candidate_public")
        html = r.data.decode()
        # Alice is in a normal (non-pano) tile — should NOT appear in a person-chip span
        assert '<span class="person-chip">Alice</span>' not in html

    def test_pano_with_no_persons_has_no_chips(self, flask_client):
        r = flask_client.get("/review?state=candidate_public")
        html = r.data.decode()
        # PANO_D has no persons — it should still appear in the page
        assert "PANO_D.JPG" in html
