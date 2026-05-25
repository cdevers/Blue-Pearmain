"""
tests/test_album_membership_api.py — integration tests for album membership routes (#135)

Run from repo root:
    python -m pytest tests/test_album_membership_api.py -v
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

import reviewer.app as app_module
from db.db import Database


def _photo_payload(i: int) -> dict:
    return {
        "uuid": f"u{i}",
        "original_filename": f"IMG_{i:04d}.JPG",
        "privacy_state": "needs_review",
        "apple_persons": [],
        "apple_labels": [],
        "apple_unknown_faces": 0,
        "apple_named_faces": 0,
    }


@pytest.fixture(scope="module")
def client_with_albums():
    with tempfile.TemporaryDirectory() as tmp:
        test_db = Database(Path(tmp) / "test.db")
        p1 = test_db.upsert_photo(_photo_payload(1))
        p2 = test_db.upsert_photo(_photo_payload(2))
        p3 = test_db.upsert_photo(_photo_payload(3))
        a1 = test_db.upsert_album("album-uuid-1", "Summer 2024")
        a2 = test_db.upsert_album("album-uuid-2", "Trips")
        app_module._db = test_db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test-secret"
        with app_module.app.test_client() as c:
            yield c, p1, p2, p3, a1, a2, test_db
        app_module._db = None


class TestAlbumsIndexPage:
    def test_albums_page_200(self, client_with_albums):
        c, *_ = client_with_albums
        resp = c.get("/albums")
        assert resp.status_code == 200

    def test_albums_page_shows_album_names(self, client_with_albums):
        c, _, _, _, a1, a2, db = client_with_albums
        resp = c.get("/albums")
        html = resp.data.decode()
        assert "Summer 2024" in html
        assert "Trips" in html

    def test_albums_page_links_to_library(self, client_with_albums):
        c, _, _, _, a1, _, _ = client_with_albums
        resp = c.get("/albums")
        html = resp.data.decode()
        assert f"/library?album_id={a1}" in html


class TestAlbumMembershipWrite:
    def test_add_valid(self, client_with_albums):
        c, p1, p2, _, a1, _, db = client_with_albums
        resp = c.post(
            "/api/album-membership",
            data=json.dumps({"photo_ids": [p1, p2], "add": [a1]}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["added"] == 2
        # Verify DB state
        row = db.conn.execute(
            "SELECT removed_at, flickr_pushed FROM photo_albums WHERE photo_id=? AND album_id=?",
            (p1, a1),
        ).fetchone()
        assert row is not None
        assert row["removed_at"] is None
        assert row["flickr_pushed"] == 0

    def test_remove_valid(self, client_with_albums):
        c, p1, p2, _, a1, _, db = client_with_albums
        # Ensure photos are members first
        db.upsert_photo_album(p1, a1)
        db.upsert_photo_album(p2, a1)
        resp = c.post(
            "/api/album-membership",
            data=json.dumps({"photo_ids": [p1, p2], "remove": [a1]}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["removed"] == 2
        row = db.conn.execute(
            "SELECT removed_at FROM photo_albums WHERE photo_id=? AND album_id=?",
            (p1, a1),
        ).fetchone()
        assert row["removed_at"] is not None

    def test_add_and_remove_in_same_request(self, client_with_albums):
        c, p1, p2, p3, a1, a2, db = client_with_albums
        db.upsert_photo_album(p3, a1)
        resp = c.post(
            "/api/album-membership",
            data=json.dumps({"photo_ids": [p1, p3], "add": [a2], "remove": [a1]}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["added"] >= 1
        assert data["removed"] >= 1

    def test_add_idempotent(self, client_with_albums):
        c, p1, _, _, a1, _, db = client_with_albums
        db.upsert_photo_album(p1, a1)  # already a member
        resp = c.post(
            "/api/album-membership",
            data=json.dumps({"photo_ids": [p1], "add": [a1]}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        # No duplicate rows
        count = db.conn.execute(
            "SELECT COUNT(*) FROM photo_albums WHERE photo_id=? AND album_id=?",
            (p1, a1),
        ).fetchone()[0]
        assert count == 1

    def test_empty_photo_ids_returns_400(self, client_with_albums):
        c, *_ = client_with_albums
        resp = c.post(
            "/api/album-membership",
            data=json.dumps({"photo_ids": [], "add": [1]}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_invalid_album_id_returns_400(self, client_with_albums):
        c, p1, _, _, _, _, _ = client_with_albums
        resp = c.post(
            "/api/album-membership",
            data=json.dumps({"photo_ids": [p1], "add": [99999]}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_missing_photo_ids_returns_400(self, client_with_albums):
        c, *_ = client_with_albums
        resp = c.post(
            "/api/album-membership",
            data=json.dumps({"add": [1]}),
            content_type="application/json",
        )
        assert resp.status_code == 400


class TestAlbumMembershipRead:
    def test_get_membership_returns_200(self, client_with_albums):
        c, p1, p2, _, a1, _, db = client_with_albums
        db.upsert_photo_album(p1, a1)
        resp = c.get(f"/api/album-membership?photo_ids={p1},{p2}")
        assert resp.status_code == 200

    def test_get_membership_includes_active_albums(self, client_with_albums):
        c, p1, _, _, a1, _, db = client_with_albums
        db.upsert_photo_album(p1, a1)
        resp = c.get(f"/api/album-membership?photo_ids={p1}")
        data = resp.get_json()
        # JSON keys are strings
        assert str(a1) in data["membership"]
        assert p1 in data["membership"][str(a1)]

    def test_get_membership_excludes_tombstoned(self, client_with_albums):
        c, p1, _, _, a1, _, db = client_with_albums
        db.upsert_photo_album(p1, a1)
        db.mark_photo_album_removed(p1, a1)
        resp = c.get(f"/api/album-membership?photo_ids={p1}")
        data = resp.get_json()
        assert str(a1) not in data["membership"]
        # Restore state: reactivate so later tests are not affected
        db.upsert_photo_album(p1, a1)

    def test_get_membership_empty_photo_ids_returns_empty(self, client_with_albums):
        c, *_ = client_with_albums
        resp = c.get("/api/album-membership?photo_ids=")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["membership"] == {}


class TestLibraryCurrentAlbum:
    def test_library_filtered_by_album_passes_album_name(self, client_with_albums):
        c, _, _, _, a1, _, _ = client_with_albums
        resp = c.get(f"/library?album_id={a1}")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "Summer 2024" in html
