"""POST /api/geo_confirm_none — set and clear geo_confirmed_none (#145)."""

from __future__ import annotations
import json
import tempfile
from pathlib import Path
import pytest
import reviewer.app as app_module
from db.db import Database


def _photo(i: int, **kwargs) -> dict:
    return {
        "uuid": f"gcn-u{i}",
        "original_filename": f"IMG_{i:04d}.JPG",
        "privacy_state": "needs_review",
        "apple_persons": [],
        "apple_labels": [],
        **kwargs,
    }


@pytest.fixture()
def client_gcn():
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "t.db")
        p1 = db.upsert_photo(_photo(1))
        p2 = db.upsert_photo(_photo(2))
        # Insert a pending geo proposal for p1
        db.conn.execute(
            "INSERT INTO metadata_proposals"
            " (photo_id, field, source, target, conflict_type, status, created_at)"
            " VALUES (?, 'geo_location', 'flickr', 'photos', 'non_conflict', 'pending', datetime('now'))",
            (p1,),
        )
        db.conn.commit()
        app_module._db = db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test"
        with app_module.app.test_client() as c:
            yield c, p1, p2
        app_module._db = None


def _post(c, **body):
    return c.post(
        "/api/geo_confirm_none",
        data=json.dumps(body),
        content_type="application/json",
        headers={"X-Requested-With": "XMLHttpRequest"},
    )


class TestGeoConfirmNoneApi:
    def test_set_geo_confirmed_none(self, client_gcn):
        c, p1, p2 = client_gcn
        r = _post(c, photo_ids=[p1], clear=False)
        assert r.status_code == 200
        data = r.get_json()
        assert data["ok"] is True

    def test_set_updates_db_column(self, client_gcn):
        c, p1, p2 = client_gcn
        _post(c, photo_ids=[p1])
        from reviewer.app import _db

        row = _db.conn.execute("SELECT geo_confirmed_none FROM photos WHERE id=?", (p1,)).fetchone()
        assert row["geo_confirmed_none"] == 1

    def test_set_cancels_pending_geo_proposals(self, client_gcn):
        c, p1, p2 = client_gcn
        _post(c, photo_ids=[p1])
        from reviewer.app import _db

        rows = _db.conn.execute(
            "SELECT status FROM metadata_proposals WHERE photo_id=? AND field='geo_location'",
            (p1,),
        ).fetchall()
        assert all(r["status"] == "rejected" for r in rows)

    def test_clear_restores_column(self, client_gcn):
        c, p1, p2 = client_gcn
        _post(c, photo_ids=[p1])
        _post(c, photo_ids=[p1], clear=True)
        from reviewer.app import _db

        row = _db.conn.execute("SELECT geo_confirmed_none FROM photos WHERE id=?", (p1,)).fetchone()
        assert row["geo_confirmed_none"] == 0

    def test_clear_does_not_cancel_proposals(self, client_gcn):
        c, p1, p2 = client_gcn
        _post(c, photo_ids=[p1])
        _post(c, photo_ids=[p1], clear=True)
        from reviewer.app import _db

        rows = _db.conn.execute(
            "SELECT status FROM metadata_proposals WHERE photo_id=? AND field='geo_location'",
            (p1,),
        ).fetchall()
        assert all(r["status"] == "rejected" for r in rows)

    def test_bulk_set(self, client_gcn):
        c, p1, p2 = client_gcn
        r = _post(c, photo_ids=[p1, p2])
        assert r.status_code == 200
        from reviewer.app import _db

        rows = _db.conn.execute(
            "SELECT geo_confirmed_none FROM photos WHERE id IN (?,?)", (p1, p2)
        ).fetchall()
        assert all(r["geo_confirmed_none"] == 1 for r in rows)

    def test_state_transition_set_clear_set(self, client_gcn):
        c, p1, _ = client_gcn
        _post(c, photo_ids=[p1])
        _post(c, photo_ids=[p1], clear=True)
        _post(c, photo_ids=[p1])
        from reviewer.app import _db

        row = _db.conn.execute("SELECT geo_confirmed_none FROM photos WHERE id=?", (p1,)).fetchone()
        assert row["geo_confirmed_none"] == 1

    def test_missing_photo_ids_returns_400(self, client_gcn):
        c, _, _ = client_gcn
        r = _post(c, clear=False)
        assert r.status_code == 400
