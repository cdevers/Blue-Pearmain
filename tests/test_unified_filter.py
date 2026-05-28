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
