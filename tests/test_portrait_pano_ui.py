"""
tests/test_portrait_pano_ui.py — tests for portrait panoramic tile support (#128)

Run from repo root:
    python -m pytest tests/test_portrait_pano_ui.py -v
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


@pytest.fixture()
def flask_client(tmp_path):
    """Flask test client seeded with landscape pano, portrait pano, and normal photos."""
    db = Database(tmp_path / "test.db")

    # Ensure person_policies table exists (migration_019, not in schema.sql)
    db.conn.execute("""
        CREATE TABLE IF NOT EXISTS person_policies (
            id          INTEGER PRIMARY KEY,
            person_name TEXT NOT NULL UNIQUE,
            policy      TEXT NOT NULL CHECK(policy IN ('always_private')),
            created_at  TEXT NOT NULL
        )
    """)
    db.conn.commit()

    # Photo 1: landscape pano (3:1 ratio, no rotation) → expects class "pano"
    db.upsert_photo(
        {
            "uuid": "uuid-land-pano",
            "original_filename": "PANO_LAND.JPG",
            "privacy_state": "candidate_public",
            "width": 3000,
            "height": 1000,
            "apple_persons": [],
            "proposed_tags": [],
        }
    )

    # Photo 2: portrait pano (1:3 ratio, no rotation) → expects class "pano-portrait"
    db.upsert_photo(
        {
            "uuid": "uuid-port-pano",
            "original_filename": "PANO_PORT.JPG",
            "privacy_state": "candidate_public",
            "width": 1000,
            "height": 3000,
            "apple_persons": [],
            "proposed_tags": [],
        }
    )

    # Photo 3: normal 4:3 → expects neither class
    db.upsert_photo(
        {
            "uuid": "uuid-normal",
            "original_filename": "IMG_NORM.JPG",
            "privacy_state": "candidate_public",
            "width": 4032,
            "height": 3024,
            "apple_persons": [],
            "proposed_tags": [],
        }
    )

    # Photo 4: stored sideways — raw dims are 3000×1000 (landscape) but display_rotation=90
    #   eff_w = height = 1000, eff_h = width = 3000 → portrait pano after rotation
    db.upsert_photo(
        {
            "uuid": "uuid-rotated-port",
            "original_filename": "PANO_ROT_PORT.JPG",
            "privacy_state": "candidate_public",
            "width": 3000,
            "height": 1000,
            "display_rotation": 90,
            "apple_persons": [],
            "proposed_tags": [],
        }
    )

    app_module._db = db
    app_module.app.config["TESTING"] = True
    app_module.app.config["SECRET_KEY"] = "test"
    with app_module.app.test_client() as client:
        yield client
    app_module._db = None

    db.close()


# ---------------------------------------------------------------------------
# review_queue: display_rotation is returned
# ---------------------------------------------------------------------------


class TestReviewQueueRotation:
    def test_display_rotation_returned_in_review_queue(self, db):
        """review_queue must include display_rotation so the template can correct dimensions."""
        db.upsert_photo(
            {
                "uuid": "uuid-rot-check",
                "original_filename": "IMG_ROT.JPG",
                "privacy_state": "candidate_public",
                "width": 3000,
                "height": 1000,
                "display_rotation": 90,
                "apple_persons": [],
                "proposed_tags": [],
            }
        )
        photos = db.review_queue(states=["candidate_public"])
        assert len(photos) == 1
        assert photos[0]["display_rotation"] == 90


# ---------------------------------------------------------------------------
# Template rendering: CSS classes
# ---------------------------------------------------------------------------


class TestPortraitPanoTemplate:
    def test_landscape_pano_gets_pano_class(self, flask_client):
        r = flask_client.get("/review?state=candidate_public")
        html = r.data.decode()
        # Landscape pano: card element must have class="... pano ..."
        assert 'class="photo-card pano"' in html or "photo-card pano " in html or ' pano"' in html

    def test_portrait_pano_gets_pano_portrait_class(self, flask_client):
        r = flask_client.get("/review?state=candidate_public")
        html = r.data.decode()
        assert "pano-portrait" in html

    def test_normal_photo_gets_no_pano_class(self, flask_client):
        r = flask_client.get("/review?state=candidate_public")
        html = r.data.decode()
        # At least one card should have neither pano class — IMG_NORM.JPG
        assert 'class="photo-card "' in html or 'class="photo-card"' in html

    def test_rotated_landscape_stored_sideways_is_portrait_pano(self, flask_client):
        """Photo with raw width=3000,height=1000,rotation=90 has eff dims 1000×3000 → portrait pano."""
        r = flask_client.get("/review?state=candidate_public")
        html = r.data.decode()
        # Two photos should now be portrait panos: uuid-port-pano and uuid-rotated-port
        assert html.count("pano-portrait") >= 2

    def test_pano_portrait_css_present(self, flask_client):
        """The .pano-portrait CSS rule must be present in the page."""
        r = flask_client.get("/review?state=candidate_public")
        html = r.data.decode()
        assert "pano-portrait" in html
        assert "grid-row: span 2" in html

    def test_landscape_pano_css_still_present(self, flask_client):
        """Regression: .pano (landscape) CSS must not be removed."""
        r = flask_client.get("/review?state=candidate_public")
        html = r.data.decode()
        assert "grid-column: span 2" in html

    def test_portrait_pano_aspect_ratio_css(self, flask_client):
        """The portrait pano thumb should use aspect-ratio: 1/3."""
        r = flask_client.get("/review?state=candidate_public")
        html = r.data.decode()
        assert "aspect-ratio: 1/3" in html

    def test_no_is_pano_variable_in_template(self, flask_client):
        """Regression guard: the old single is_pano variable must be replaced."""
        template_path = Path(__file__).parent.parent / "reviewer" / "templates" / "review.html"
        source = template_path.read_text()
        # The variable name 'is_pano' (without _landscape or _portrait suffix) must not appear
        assert "is_pano " not in source
        assert "is_pano}" not in source
        assert "{% set is_pano " not in source
