# tests/test_geo_proposals_ui.py
"""Proposals UI renders geo_location field (#145)."""

from __future__ import annotations
import json
import tempfile
from pathlib import Path
import pytest
import reviewer.app as app_module
from db.db import Database


def _photo(i: int, **kwargs) -> dict:
    return {
        "uuid": f"gpu-u{i}",
        "flickr_id": f"gpu-f{i}",
        "original_filename": f"IMG_{i:04d}.JPG",
        "privacy_state": "needs_review",
        "apple_persons": [],
        "apple_labels": [],
        **kwargs,
    }


def _insert_geo_proposal(db, photo_id, source, target, conflict_type, payload):
    db.conn.execute(
        "INSERT INTO metadata_proposals"
        " (photo_id, field, proposed_value, source, target, conflict_type, status, created_at)"
        " VALUES (?, 'geo_location', ?, ?, ?, ?, 'pending', datetime('now'))",
        (photo_id, json.dumps(payload), source, target, conflict_type),
    )
    db.conn.commit()


@pytest.fixture()
def client_proposals():
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "t.db")
        pid_nc = db.upsert_photo(_photo(1, latitude=42.3601, longitude=-71.0589))
        _insert_geo_proposal(
            db, pid_nc, "flickr", "photos", "non_conflict", {"lat": 42.3601, "lon": -71.0589}
        )
        pid_div = db.upsert_photo(
            _photo(
                2,
                flickr_latitude=42.3601,
                flickr_longitude=-71.0589,
                photos_latitude=37.5665,
                photos_longitude=126.9780,
            )
        )
        _insert_geo_proposal(
            db,
            pid_div,
            "flickr",
            "photos",
            "divergence",
            {
                "lat": 42.3601,
                "lon": -71.0589,
                "current_lat": 37.5665,
                "current_lon": 126.9780,
                "distance_m": 10_923_456,
            },
        )
        app_module._db = db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test"
        with app_module.app.test_client() as c:
            yield c, pid_nc, pid_div
        app_module._db = None


class TestGeoProposalsUI:
    def test_non_conflict_proposal_rendered(self, client_proposals):
        c, pid_nc, _ = client_proposals
        html = c.get("/proposals").data.decode()
        assert "42.3601" in html

    def test_non_conflict_shows_source_target(self, client_proposals):
        c, pid_nc, _ = client_proposals
        html = c.get("/proposals").data.decode()
        assert "flickr" in html.lower()
        assert "photos" in html.lower()

    def test_non_conflict_shows_approve_button(self, client_proposals):
        c, pid_nc, _ = client_proposals
        html = c.get("/proposals").data.decode()
        assert "Approve" in html

    def test_divergence_shows_distance_delta(self, client_proposals):
        c, _, pid_div = client_proposals
        html = c.get("/proposals").data.decode()
        # Distance ~10,923 km should appear
        assert "10,923" in html or "10923" in html

    def test_divergence_shows_use_flickr_use_photos_buttons(self, client_proposals):
        c, _, pid_div = client_proposals
        html = c.get("/proposals").data.decode()
        assert "Use Flickr" in html
        assert "Use Photos" in html

    def test_divergence_shows_view_on_map_link(self, client_proposals):
        c, _, pid_div = client_proposals
        html = c.get("/proposals").data.decode()
        assert f"/map?photo_id={pid_div}" in html

    def test_geo_location_field_not_shown_as_raw_label(self, client_proposals):
        c, _, _ = client_proposals
        html = c.get("/proposals").data.decode()
        assert "GEO_LOCATION" not in html
