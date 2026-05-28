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


# ── normalize_shared_filters() ─────────────────────────────────────────────


class TestNormalizeSharedFilters:
    def test_year_swap_produces_canonical_order(self):
        from reviewer.app import app, normalize_shared_filters

        with app.test_request_context("/?year_from=2025&year_to=2010"):
            f = normalize_shared_filters()
        assert f["year_from"] == 2010
        assert f["year_to"] == 2025

    def test_invalid_album_id_becomes_none(self):
        from reviewer.app import app, normalize_shared_filters

        with app.test_request_context("/?album_id=notanint"):
            f = normalize_shared_filters()
        assert f["album_id"] is None

    def test_empty_request_gives_clean_defaults(self):
        from reviewer.app import app, normalize_shared_filters

        with app.test_request_context("/"):
            f = normalize_shared_filters()
        assert f["time_pattern"] == ""
        assert f["year_from"] is None
        assert f["year_to"] is None
        assert f["album_id"] is None
        assert f["person"] == ""
        assert f["status"] == ""
        assert f["expand"] == ""

    def test_unknown_status_becomes_empty(self):
        from reviewer.app import app, normalize_shared_filters

        with app.test_request_context("/?status=bogus"):
            f = normalize_shared_filters()
        assert f["status"] == ""

    def test_single_year_bound_preserved(self):
        from reviewer.app import app, normalize_shared_filters

        with app.test_request_context("/?year_from=2018"):
            f = normalize_shared_filters()
        assert f["year_from"] == 2018
        assert f["year_to"] is None


# ── /library year_from / year_to ──────────────────────────────────────────


@pytest.fixture()
def client_lib_years():
    """DB with library photos in 2016, 2019, 2023."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "test.db")
        p16 = db.upsert_photo(
            _photo(60, date_taken="2016-08-15T10:00:00", privacy_state="approved_public")
        )
        p19 = db.upsert_photo(
            _photo(61, date_taken="2019-12-20T10:00:00", privacy_state="needs_review")
        )
        p23 = db.upsert_photo(
            _photo(62, date_taken="2023-07-04T10:00:00", privacy_state="keep_private")
        )
        app_module._db = db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test"
        with app_module.app.test_client() as c:
            yield c, p16, p19, p23
        app_module._db = None


def _lib_ids(resp) -> set[int]:
    import re

    body = resp.data.decode()
    return {int(m) for m in re.findall(r'data-id="(\d+)"', body)}


class TestLibraryYearFilter:
    def test_year_from_excludes_earlier(self, client_lib_years):
        c, p16, p19, p23 = client_lib_years
        resp = c.get("/library?year_from=2019")
        assert resp.status_code == 200
        ids = _lib_ids(resp)
        assert p16 not in ids
        assert p19 in ids
        assert p23 in ids

    def test_year_to_excludes_later(self, client_lib_years):
        c, p16, p19, p23 = client_lib_years
        resp = c.get("/library?year_to=2019")
        ids = _lib_ids(resp)
        assert p16 in ids
        assert p19 in ids
        assert p23 not in ids

    def test_year_range_both_bounds(self, client_lib_years):
        c, p16, p19, p23 = client_lib_years
        resp = c.get("/library?year_from=2019&year_to=2019")
        ids = _lib_ids(resp)
        assert p16 not in ids
        assert p19 in ids
        assert p23 not in ids

    def test_year_swap_when_from_greater_than_to(self, client_lib_years):
        c, p16, p19, p23 = client_lib_years
        resp = c.get("/library?year_from=2023&year_to=2016")
        ids = _lib_ids(resp)
        assert p16 in ids
        assert p19 in ids
        assert p23 in ids

    def test_year_does_not_override_explicit_date_from(self, client_lib_years):
        c, p16, p19, p23 = client_lib_years
        resp = c.get("/library?date_from=2020-01-01&year_from=2016")
        ids = _lib_ids(resp)
        assert p16 not in ids
        assert p19 not in ids
        assert p23 in ids

    def test_nonnumeric_year_ignored(self, client_lib_years):
        c, p16, p19, p23 = client_lib_years
        resp = c.get("/library?year_from=abc&year_to=xyz")
        assert resp.status_code == 200
        assert len(_lib_ids(resp)) == 3

    def test_out_of_range_year_ignored(self, client_lib_years):
        c, p16, p19, p23 = client_lib_years
        resp = c.get("/library?year_from=1700&year_to=3000")
        assert resp.status_code == 200
        assert len(_lib_ids(resp)) == 3


# ── map_view() initial_filters ────────────────────────────────────────────


@pytest.fixture()
def client_map_view():
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "test.db")
        app_module._db = db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test"
        with app_module.app.test_client() as c:
            yield c
        app_module._db = None


class TestMapViewInitialFilters:
    @pytest.mark.xfail(strict=False, reason="pre-populates form via shared macro added in Task 7")
    def test_map_view_passes_initial_filters_to_template(self, client_map_view):
        c = client_map_view
        resp = c.get(
            "/map?time_pattern=month:08&year_from=2015&year_to=2019&person=Marcin&status=public"
        )
        assert resp.status_code == 200
        body = resp.data.decode()
        assert 'value="2015"' in body
        assert 'value="2019"' in body
        assert 'value="Marcin"' in body


# ── Template integration: shared macro + library UI ───────────────────────


@pytest.fixture()
def client_template():
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "test.db")
        db.upsert_photo(_photo(70, apple_persons=["Alice W"]))
        db.upsert_album("uuid-t1", "Japan 2019")
        app_module._db = db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test"
        with app_module.app.test_client() as c:
            yield c
        app_module._db = None


class TestLibraryTemplateIntegration:
    def test_shared_macro_controls_in_library(self, client_template):
        c = client_template
        resp = c.get("/library")
        assert resp.status_code == 200
        body = resp.data.decode()
        assert 'name="time_pattern"' in body
        assert 'name="year_from"' in body
        assert 'name="year_to"' in body
        assert 'name="album_id"' in body
        assert 'name="person"' in body
        assert 'name="status"' in body

    def test_library_has_no_apply_button(self, client_template):
        c = client_template
        resp = c.get("/library")
        body = resp.data.decode()
        assert "Apply filters" not in body

    def test_library_has_view_on_map_link(self, client_template):
        c = client_template
        resp = c.get("/library?time_pattern=month:08&year_from=2015&person=Alice+W")
        body = resp.data.decode()
        assert "/map" in body
        assert "time_pattern=month%3A08" in body or "time_pattern=month:08" in body
        assert "year_from=2015" in body
        assert "Alice" in body

    def test_library_chip_row_present(self, client_template):
        c = client_template
        resp = c.get("/library")
        body = resp.data.decode()
        assert "lib-filter-chips" in body

    def test_shared_macro_in_map(self, client_template):
        c = client_template
        resp = c.get("/map")
        assert resp.status_code == 200
        body = resp.data.decode()
        assert 'name="time_pattern"' in body
        assert 'name="status"' in body

    def test_library_to_map_roundtrip_preserves_filters(self, client_template):
        """View-on-map link from library carries all shared filter params."""
        c = client_template
        resp = c.get(
            "/library?time_pattern=month:08&year_from=2015&year_to=2019"
            "&person=Alice+W&status=public"
        )
        assert resp.status_code == 200
        body = resp.data.decode()
        import re

        map_links = re.findall(r'href="(/map[^"]*)"', body)
        assert map_links, "No /map link found in library response"
        # nav bar also has a bare /map link; find the View-on-map link with filter params
        map_url = next((u for u in map_links if "time_pattern" in u), None)
        assert map_url is not None, "No /map link with filter params found"
        assert "year_from=2015" in map_url
        assert "year_to=2019" in map_url
        assert "Alice" in map_url
        assert "status=public" in map_url
