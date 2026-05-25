"""
tests/test_album_management_api.py — integration tests for album rename and delete (#136, #137)

Run from repo root:
    python -m pytest tests/test_album_management_api.py -v
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

import reviewer.app as app_module
from db.db import Database


@pytest.fixture()
def client_and_albums():
    """Fresh DB + test client per test function — both operations mutate state."""
    with tempfile.TemporaryDirectory() as tmp:
        test_db = Database(Path(tmp) / "test.db")
        a1 = test_db.upsert_album("album-uuid-1", "Summer 2024")
        a2 = test_db.upsert_album("album-uuid-2", "Trips")
        app_module._db = test_db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test-secret"
        with app_module.app.test_client() as c:
            yield c, a1, a2, test_db
        app_module._db = None


class TestAlbumRename:
    def test_rename_valid(self, client_and_albums):
        c, a1, _, db = client_and_albums
        resp = c.patch(
            f"/api/albums/{a1}",
            data=json.dumps({"name": "Winter 2024"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["name"] == "Winter 2024"
        # Verify DB
        row = db.conn.execute("SELECT name FROM albums WHERE id = ?", (a1,)).fetchone()
        assert row["name"] == "Winter 2024"

    def test_rename_strips_whitespace(self, client_and_albums):
        c, a1, _, db = client_and_albums
        resp = c.patch(
            f"/api/albums/{a1}",
            data=json.dumps({"name": "  Padded Name  "}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert resp.get_json()["name"] == "Padded Name"

    def test_rename_to_same_name_succeeds(self, client_and_albums):
        """Renaming to the current name is a valid no-op — returns 200."""
        c, a1, _, db = client_and_albums
        resp = c.patch(
            f"/api/albums/{a1}",
            data=json.dumps({"name": "Summer 2024"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True
        assert resp.get_json()["name"] == "Summer 2024"

    def test_rename_empty_name_returns_400(self, client_and_albums):
        c, a1, _, _ = client_and_albums
        resp = c.patch(
            f"/api/albums/{a1}",
            data=json.dumps({"name": ""}),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert resp.get_json()["ok"] is False

    def test_rename_whitespace_only_returns_400(self, client_and_albums):
        c, a1, _, _ = client_and_albums
        resp = c.patch(
            f"/api/albums/{a1}",
            data=json.dumps({"name": "   "}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_rename_unknown_album_returns_404(self, client_and_albums):
        c, _, _, _ = client_and_albums
        resp = c.patch(
            "/api/albums/99999",
            data=json.dumps({"name": "Ghost"}),
            content_type="application/json",
        )
        assert resp.status_code == 404
        assert resp.get_json()["ok"] is False

    def test_rename_deleted_album_returns_404(self, client_and_albums):
        c, a1, _, db = client_and_albums
        db.mark_album_deleted(a1)
        resp = c.patch(
            f"/api/albums/{a1}",
            data=json.dumps({"name": "New Name"}),
            content_type="application/json",
        )
        assert resp.status_code == 404
        assert resp.get_json()["ok"] is False

    def test_rename_twice_before_sync(self, client_and_albums):
        """Rename a second time before sync runs — DB holds the latest name."""
        c, a1, _, db = client_and_albums
        c.patch(
            f"/api/albums/{a1}",
            data=json.dumps({"name": "First Rename"}),
            content_type="application/json",
        )
        c.patch(
            f"/api/albums/{a1}",
            data=json.dumps({"name": "Second Rename"}),
            content_type="application/json",
        )
        row = db.conn.execute("SELECT name, flickr_name FROM albums WHERE id = ?", (a1,)).fetchone()
        assert row["name"] == "Second Rename"
        # flickr_name is NULL in a fresh test DB (upsert_album never sets it).
        # rename_album must not overwrite it — if it did, sync tooling could not
        # detect a pending rename by comparing name vs flickr_name.
        assert row["flickr_name"] is None


class TestAlbumDelete:
    def test_delete_valid(self, client_and_albums):
        c, a1, _, db = client_and_albums
        resp = c.delete(f"/api/albums/{a1}")
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True
        # Verify deleted_at is set
        row = db.conn.execute("SELECT deleted_at FROM albums WHERE id = ?", (a1,)).fetchone()
        assert row["deleted_at"] is not None

    def test_delete_removes_from_albums_page(self, client_and_albums):
        c, a1, _, _ = client_and_albums
        c.delete(f"/api/albums/{a1}")
        resp = c.get("/albums")
        assert resp.status_code == 200
        assert "Summer 2024" not in resp.data.decode()

    def test_delete_unknown_album_returns_404(self, client_and_albums):
        c, _, _, _ = client_and_albums
        resp = c.delete("/api/albums/99999")
        assert resp.status_code == 404
        assert resp.get_json()["ok"] is False

    def test_delete_already_deleted_is_idempotent(self, client_and_albums):
        """DELETE on an already-deleted album is a no-op, not an error."""
        c, a1, _, db = client_and_albums
        db.mark_album_deleted(a1)
        resp = c.delete(f"/api/albums/{a1}")
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True
