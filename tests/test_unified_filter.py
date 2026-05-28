"""
tests/test_unified_filter.py — shared filter widget: status values, library year
range, map status filter, cross-page nav (#155)

Run from repo root:
    python -m pytest tests/test_unified_filter.py -v
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from db.db import Database
import reviewer.app as app_module


def _photo(i: int, **kwargs) -> dict:
    base: dict = {
        "uuid": f"uf-u{i}",
        "original_filename": f"IMG_{i:04d}.JPG",
        "privacy_state": "needs_review",
        "apple_persons": [],
        "apple_labels": [],
        "apple_unknown_faces": 0,
        "apple_named_faces": 0,
    }
    base.update(kwargs)
    return base


# ── Status values in db.library_photos() ─────────────────────────────────


@pytest.fixture()
def db_privacy():
    """DB with one photo for every privacy_state bucket."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "test.db")
        ids = {}
        for state in (
            "already_public",
            "approved_public",
            "approved_friends",
            "approved_family",
            "approved_friends_family",
            "keep_private",
            "auto_private",
            "needs_review",
            "candidate_public",
        ):
            ids[state] = db.upsert_photo(_photo(len(ids), privacy_state=state))
        yield db, ids


class TestStatusValues:
    def test_public_is_strictly_public(self, db_privacy):
        db, ids = db_privacy
        rows = db.library_photos(status="public")
        result_ids = {r["id"] for r in rows}
        assert ids["already_public"] in result_ids
        assert ids["approved_public"] in result_ids
        # friends/family are NOT in public
        assert ids["approved_friends"] not in result_ids
        assert ids["approved_family"] not in result_ids
        assert ids["approved_friends_family"] not in result_ids

    def test_friends_returns_only_approved_friends(self, db_privacy):
        db, ids = db_privacy
        rows = db.library_photos(status="friends")
        result_ids = {r["id"] for r in rows}
        assert ids["approved_friends"] in result_ids
        assert ids["approved_public"] not in result_ids
        assert ids["approved_family"] not in result_ids

    def test_family_returns_only_approved_family(self, db_privacy):
        db, ids = db_privacy
        rows = db.library_photos(status="family")
        result_ids = {r["id"] for r in rows}
        assert ids["approved_family"] in result_ids
        assert ids["approved_friends"] not in result_ids

    def test_friends_family_returns_approved_friends_family(self, db_privacy):
        db, ids = db_privacy
        rows = db.library_photos(status="friends_family")
        result_ids = {r["id"] for r in rows}
        assert ids["approved_friends_family"] in result_ids
        assert ids["approved_friends"] not in result_ids
        assert ids["approved_family"] not in result_ids

    def test_private_returns_keep_and_auto_private(self, db_privacy):
        db, ids = db_privacy
        rows = db.library_photos(status="private")
        result_ids = {r["id"] for r in rows}
        assert ids["keep_private"] in result_ids
        assert ids["auto_private"] in result_ids
        assert ids["approved_public"] not in result_ids

    def test_pending_returns_needs_review_and_candidate(self, db_privacy):
        db, ids = db_privacy
        rows = db.library_photos(status="pending")
        result_ids = {r["id"] for r in rows}
        assert ids["needs_review"] in result_ids
        assert ids["candidate_public"] in result_ids
        assert ids["approved_public"] not in result_ids

    def test_unknown_status_returns_all(self, db_privacy):
        db, ids = db_privacy
        rows = db.library_photos(status="bogus")
        # unknown status ignored → no filter applied
        assert len(rows) == len(ids)


# ── /api/map-photos status filter ────────────────────────────────────────


@pytest.fixture()
def client_map_status():
    """DB with geotagged photos of varying privacy states."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "test.db")
        p_pub = db.upsert_photo(
            _photo(50, latitude=48.8, longitude=2.3, privacy_state="approved_public")
        )
        p_friend = db.upsert_photo(
            _photo(51, latitude=40.7, longitude=-74.0, privacy_state="approved_friends")
        )
        p_priv = db.upsert_photo(
            _photo(52, latitude=51.5, longitude=-0.1, privacy_state="keep_private")
        )
        p_pend = db.upsert_photo(
            _photo(53, latitude=35.7, longitude=139.7, privacy_state="needs_review")
        )
        app_module._db = db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test"
        with app_module.app.test_client() as c:
            yield c, p_pub, p_friend, p_priv, p_pend
        app_module._db = None


def _map_ids(resp) -> set[int]:
    return {p["id"] for p in resp.get_json()}


class TestMapStatusFilter:
    def test_status_public_returns_only_public(self, client_map_status):
        c, p_pub, p_friend, p_priv, p_pend = client_map_status
        resp = c.get("/api/map-photos?status=public")
        assert resp.status_code == 200
        ids = _map_ids(resp)
        assert p_pub in ids
        assert p_friend not in ids
        assert p_priv not in ids

    def test_status_friends_returns_only_friends(self, client_map_status):
        c, p_pub, p_friend, p_priv, p_pend = client_map_status
        resp = c.get("/api/map-photos?status=friends")
        ids = _map_ids(resp)
        assert p_friend in ids
        assert p_pub not in ids

    def test_status_private_returns_only_private(self, client_map_status):
        c, p_pub, p_friend, p_priv, p_pend = client_map_status
        resp = c.get("/api/map-photos?status=private")
        ids = _map_ids(resp)
        assert p_priv in ids
        assert p_pub not in ids

    def test_status_unknown_returns_all(self, client_map_status):
        c, p_pub, p_friend, p_priv, p_pend = client_map_status
        resp = c.get("/api/map-photos?status=bogus")
        assert resp.status_code == 200
        # All 4 geotagged photos returned when status is unknown
        assert len(resp.get_json()) == 4

    def test_no_status_param_returns_all(self, client_map_status):
        c, p_pub, p_friend, p_priv, p_pend = client_map_status
        resp = c.get("/api/map-photos")
        assert len(resp.get_json()) == 4
