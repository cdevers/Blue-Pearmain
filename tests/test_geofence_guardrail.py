"""
tests/test_geofence_guardrail.py — tests for the geofence/person-policy guardrail

Run from repo root:
    python -m pytest tests/test_geofence_guardrail.py -v
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import reviewer.app as app_module
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


@pytest.fixture()
def flask_client(tmp_path):
    """Flask test client with a seeded DB."""
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

    # Person policy: Jane Smith is always_private
    db.set_person_policy("Jane Smith", "always_private")

    # Photo 1: geofenced
    db.upsert_photo(
        {
            "uuid": "uuid-p1",
            "original_filename": "IMG_001.JPG",
            "privacy_state": "candidate_public",
            "geofence_zone": "work",
            "apple_persons": [],
            "proposed_tags": [],
        }
    )
    # Photo 2: private person (no zone)
    db.upsert_photo(
        {
            "uuid": "uuid-p2",
            "original_filename": "IMG_002.JPG",
            "privacy_state": "candidate_public",
            "geofence_zone": None,
            "apple_persons": ["Jane Smith"],
            "proposed_tags": [],
        }
    )
    # Photo 3: normal (not protected)
    db.upsert_photo(
        {
            "uuid": "uuid-p3",
            "original_filename": "IMG_003.JPG",
            "privacy_state": "candidate_public",
            "geofence_zone": None,
            "apple_persons": ["Bob Jones"],
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


class TestRouteAnnotation:
    def test_protected_badge_present_for_geofenced_photo(self, flask_client):
        r = flask_client.get("/review?state=candidate_public")
        html = r.data.decode()
        assert "protected-badge" in html
        assert "Geofence: work" in html

    def test_protected_badge_present_for_private_person_photo(self, flask_client):
        r = flask_client.get("/review?state=candidate_public")
        html = r.data.decode()
        assert "Private person: Jane Smith" in html

    def test_override_button_present_for_protected_photo(self, flask_client):
        r = flask_client.get("/review?state=candidate_public")
        html = r.data.decode()
        assert "btn-override" in html
        assert "Override" in html

    def test_normal_button_absent_for_protected_photo(self, flask_client):
        """2 protected photos + 1 normal = exactly 1 btn-pub button element.
        Count is 4: 2 from .btn-public in base.html CSS (substring), 1 from
        .btn-pub CSS rule in extra_style, 1 from the actual button element."""
        r = flask_client.get("/review?state=candidate_public")
        html = r.data.decode()
        # btn-pub appears in: .btn-public (×2 in base CSS), .btn-pub CSS rule (×1),
        # and the approve button element (×1 for the single non-protected photo)
        assert html.count("btn-pub") == 4

    def test_normal_photo_has_no_protected_badge(self, flask_client):
        r = flask_client.get("/review?state=candidate_public")
        html = r.data.decode()
        assert "Private person: Bob Jones" not in html

    def test_private_persons_js_set_embedded(self, flask_client):
        r = flask_client.get("/review?state=candidate_public")
        html = r.data.decode()
        assert "PRIVATE_PERSONS" in html
        assert "Jane Smith" in html
