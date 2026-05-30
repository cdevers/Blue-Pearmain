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
    def test_date_swap_produces_canonical_order(self):
        from reviewer.app import app, normalize_shared_filters

        with app.test_request_context("/?date_from=2025-06-01&date_to=2010-01-01"):
            f = normalize_shared_filters()
        assert f["date_from"] == "2010-01-01"
        assert f["date_to"] == "2025-06-01"

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
        assert f["date_from"] is None
        assert f["date_to"] is None
        assert f["album_id"] is None
        assert f["person"] == ""
        assert f["status"] == ""
        assert f["expand"] == ""
        assert f["tag"] == ""

    def test_unknown_status_becomes_empty(self):
        from reviewer.app import app, normalize_shared_filters

        with app.test_request_context("/?status=bogus"):
            f = normalize_shared_filters()
        assert f["status"] == ""

    def test_single_date_from_preserved(self):
        from reviewer.app import app, normalize_shared_filters

        with app.test_request_context("/?date_from=2018-03-01"):
            f = normalize_shared_filters()
        assert f["date_from"] == "2018-03-01"
        assert f["date_to"] is None


# ── _safe_date() + new normalize_shared_filters() ──────────────────────────


class TestSafeDate:
    def test_valid_date_returned_as_string(self):
        from reviewer.app import app, _safe_date

        with app.test_request_context("/?date_from=2019-06-15"):
            result = _safe_date("date_from")
        assert result == "2019-06-15"

    def test_empty_param_returns_none(self):
        from reviewer.app import app, _safe_date

        with app.test_request_context("/"):
            result = _safe_date("date_from")
        assert result is None

    def test_invalid_format_returns_none(self):
        from reviewer.app import app, _safe_date

        with app.test_request_context("/?date_from=not-a-date"):
            result = _safe_date("date_from")
        assert result is None

    def test_impossible_date_returns_none(self):
        from reviewer.app import app, _safe_date

        with app.test_request_context("/?date_from=2019-13-01"):
            result = _safe_date("date_from")
        assert result is None

    def test_partial_date_returns_none(self):
        from reviewer.app import app, _safe_date

        with app.test_request_context("/?date_from=2019-06"):
            result = _safe_date("date_from")
        assert result is None

    def test_week_date_normalized_to_canonical_form(self):
        """ISO week dates (2019-W26-4) are normalized to YYYY-MM-DD, not passed through raw."""
        from reviewer.app import app, _safe_date

        with app.test_request_context("/?date_from=2019-W26-4"):
            result = _safe_date("date_from")
        # Either returns canonical YYYY-MM-DD or None — never the raw "2019-W26-4" string
        assert result != "2019-W26-4"
        if result is not None:
            assert len(result) == 10 and result[4] == "-" and result[7] == "-"


class TestNormalizeSharedFiltersNew:
    def test_date_from_only(self):
        from reviewer.app import app, normalize_shared_filters

        with app.test_request_context("/?date_from=2019-06-15"):
            f = normalize_shared_filters()
        assert f["date_from"] == "2019-06-15"
        assert f["date_to"] is None

    def test_date_to_only(self):
        from reviewer.app import app, normalize_shared_filters

        with app.test_request_context("/?date_to=2019-08-30"):
            f = normalize_shared_filters()
        assert f["date_from"] is None
        assert f["date_to"] == "2019-08-30"

    def test_both_dates_set(self):
        from reviewer.app import app, normalize_shared_filters

        with app.test_request_context("/?date_from=2019-06-15&date_to=2019-08-30"):
            f = normalize_shared_filters()
        assert f["date_from"] == "2019-06-15"
        assert f["date_to"] == "2019-08-30"

    def test_date_swap_when_from_after_to(self):
        from reviewer.app import app, normalize_shared_filters

        with app.test_request_context("/?date_from=2019-12-31&date_to=2019-01-01"):
            f = normalize_shared_filters()
        assert f["date_from"] == "2019-01-01"
        assert f["date_to"] == "2019-12-31"

    def test_neither_date_set_returns_none(self):
        from reviewer.app import app, normalize_shared_filters

        with app.test_request_context("/"):
            f = normalize_shared_filters()
        assert f["date_from"] is None
        assert f["date_to"] is None

    def test_legacy_year_from_converts_to_date(self):
        from reviewer.app import app, normalize_shared_filters

        with app.test_request_context("/?year_from=2019"):
            f = normalize_shared_filters()
        assert f["date_from"] == "2019-01-01"
        assert f["date_to"] is None

    def test_legacy_year_to_converts_to_date(self):
        from reviewer.app import app, normalize_shared_filters

        with app.test_request_context("/?year_to=2020"):
            f = normalize_shared_filters()
        assert f["date_from"] is None
        assert f["date_to"] == "2020-12-31"

    def test_date_param_wins_over_legacy_year(self):
        from reviewer.app import app, normalize_shared_filters

        with app.test_request_context("/?date_from=2019-06-15&year_from=2016"):
            f = normalize_shared_filters()
        assert f["date_from"] == "2019-06-15"

    def test_invalid_date_ignored(self):
        from reviewer.app import app, normalize_shared_filters

        with app.test_request_context("/?date_from=not-a-date"):
            f = normalize_shared_filters()
        assert f["date_from"] is None

    def test_other_fields_unaffected(self):
        from reviewer.app import app, normalize_shared_filters

        with app.test_request_context("/?date_from=2019-06-15&status=public&person=Alice"):
            f = normalize_shared_filters()
        assert f["status"] == "public"
        assert f["person"] == "Alice"


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


class TestLibraryDateFilter:
    def test_date_from_excludes_earlier(self, client_lib_years):
        c, p16, p19, p23 = client_lib_years
        resp = c.get("/library?date_from=2019-01-01")
        assert resp.status_code == 200
        ids = _lib_ids(resp)
        assert p16 not in ids
        assert p19 in ids
        assert p23 in ids

    def test_date_to_excludes_later(self, client_lib_years):
        c, p16, p19, p23 = client_lib_years
        resp = c.get("/library?date_to=2019-12-31")
        ids = _lib_ids(resp)
        assert p16 in ids
        assert p19 in ids
        assert p23 not in ids

    def test_date_range_both_bounds(self, client_lib_years):
        c, p16, p19, p23 = client_lib_years
        resp = c.get("/library?date_from=2019-01-01&date_to=2019-12-31")
        ids = _lib_ids(resp)
        assert p16 not in ids
        assert p19 in ids
        assert p23 not in ids

    def test_date_to_inclusive_boundary(self, client_lib_years):
        """Photo taken on the boundary day is included."""
        c, p16, p19, p23 = client_lib_years
        # p19 has date_taken="2019-12-20T10:00:00"
        resp = c.get("/library?date_to=2019-12-20")
        ids = _lib_ids(resp)
        assert p19 in ids  # taken on the boundary day → included
        assert p23 not in ids

    def test_date_to_excludes_next_day(self, client_lib_years):
        """Photo taken the day after date_to is excluded."""
        c, p16, p19, p23 = client_lib_years
        # p19 has date_taken="2019-12-20T10:00:00"; p23 has "2023-07-04T10:00:00"
        resp = c.get("/library?date_from=2019-12-21&date_to=2023-07-03")
        ids = _lib_ids(resp)
        assert p19 not in ids  # 2019-12-20 is before date_from
        assert p23 not in ids  # 2023-07-04 is after date_to

    def test_date_swap_integration(self, client_lib_years):
        """Reversed date_from/date_to produces same results as correct order."""
        c, p16, p19, p23 = client_lib_years
        normal = _lib_ids(c.get("/library?date_from=2016-01-01&date_to=2022-12-31"))
        swapped = _lib_ids(c.get("/library?date_from=2022-12-31&date_to=2016-01-01"))
        assert normal == swapped

    def test_legacy_year_from_still_works(self, client_lib_years):
        """Old year_from URL param is auto-converted."""
        c, p16, p19, p23 = client_lib_years
        resp = c.get("/library?year_from=2019")
        ids = _lib_ids(resp)
        assert p16 not in ids
        assert p19 in ids
        assert p23 in ids

    def test_legacy_year_to_still_works(self, client_lib_years):
        """Old year_to URL param is auto-converted."""
        c, p16, p19, p23 = client_lib_years
        resp = c.get("/library?year_to=2019")
        ids = _lib_ids(resp)
        assert p16 in ids
        assert p19 in ids
        assert p23 not in ids

    def test_invalid_date_ignored(self, client_lib_years):
        c, p16, p19, p23 = client_lib_years
        resp = c.get("/library?date_from=not-a-date&date_to=xyz")
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
    def test_map_view_renders_with_date_params(self, client_map_view):
        """map_view() does not crash with date_from/date_to params."""
        c = client_map_view
        resp = c.get(
            "/map?time_pattern=month:08&date_from=2015-01-01&date_to=2019-12-31"
            "&person=Marcin&status=public"
        )
        assert resp.status_code == 200
        # Value assertions (value="2015-01-01") confirmed after Task 6 updates
        # the filter bar template to render <input type="date"> inputs.
        assert "Marcin" in resp.data.decode()

    def test_map_view_renders_with_legacy_year_params(self, client_map_view):
        """map_view() does not crash with legacy year_from/year_to params."""
        c = client_map_view
        resp = c.get("/map?year_from=2015&year_to=2019")
        assert resp.status_code == 200


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
        assert 'name="date_from"' in body  # was name="year_from"
        assert 'name="date_to"' in body  # was name="year_to"
        assert 'name="album_id"' in body
        assert 'name="person"' in body
        assert 'name="status"' in body
        # Old year inputs must be gone
        assert 'name="year_from"' not in body
        assert 'name="year_to"' not in body

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


# ── db.tag_names() ────────────────────────────────────────────────────────────


@pytest.fixture()
def db_tags():
    """DB with two photos sharing a tag and one unique tag each."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "test.db")
        db.upsert_photo(_photo(80, photos_tags=["boston", "travel"]))
        db.upsert_photo(_photo(81, photos_tags=["concert", "boston"]))
        yield db


@pytest.fixture()
def db_tags_with_deleted():
    """DB with one live photo and one flickr_deleted photo."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "test.db")
        db.upsert_photo(_photo(82, photos_tags=["boston"]))
        p2 = db.upsert_photo(_photo(83, photos_tags=["deleted-tag"]))
        db.conn.execute("UPDATE photos SET flickr_deleted = 1 WHERE id = ?", (p2,))
        db.conn.commit()
        yield db


@pytest.fixture()
def db_blank_tags():
    """DB with a photo whose photos_tags contains blank entries."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "test.db")
        db.upsert_photo(_photo(84, photos_tags=["", "   ", "boston"]))
        yield db


class TestTagNames:
    def test_returns_sorted_deduplicated_list(self, db_tags):
        result = db_tags.tag_names()
        assert result == ["boston", "concert", "travel"]

    def test_excludes_flickr_deleted_photos(self, db_tags_with_deleted):
        result = db_tags_with_deleted.tag_names()
        assert "deleted-tag" not in result
        assert "boston" in result

    def test_returns_empty_when_no_tags(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.db")
            db.upsert_photo(_photo(85))
            assert db.tag_names() == []

    def test_excludes_blank_values(self, db_blank_tags):
        result = db_blank_tags.tag_names()
        assert "" not in result
        assert "   " not in result
        assert result == ["boston"]


# ── normalize_shared_filters() — tag field ────────────────────────────────────


class TestNormalizeSharedFiltersTag:
    def test_tag_present(self):
        from reviewer.app import app, normalize_shared_filters

        with app.test_request_context("/?tag=boston"):
            f = normalize_shared_filters()
        assert f["tag"] == "boston"

    def test_tag_whitespace_stripped(self):
        from reviewer.app import app, normalize_shared_filters

        with app.test_request_context("/?tag=+boston+"):
            f = normalize_shared_filters()
        assert f["tag"] == "boston"

    def test_tag_absent_gives_empty_string(self):
        from reviewer.app import app, normalize_shared_filters

        with app.test_request_context("/"):
            f = normalize_shared_filters()
        assert f["tag"] == ""

    def test_tag_empty_string_stays_empty(self):
        from reviewer.app import app, normalize_shared_filters

        with app.test_request_context("/?tag="):
            f = normalize_shared_filters()
        assert f["tag"] == ""


# ── /library tag filter ───────────────────────────────────────────────────────


@pytest.fixture()
def client_lib_tags():
    """DB with photos tagged boston, concert, and one untagged."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "test.db")
        p1 = db.upsert_photo(
            _photo(90, photos_tags=["boston", "travel"], privacy_state="approved_public")
        )
        p2 = db.upsert_photo(_photo(91, photos_tags=["concert"], privacy_state="approved_public"))
        p3 = db.upsert_photo(_photo(92, photos_tags=[], privacy_state="approved_public"))
        app_module._db = db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test"
        with app_module.app.test_client() as c:
            yield c, p1, p2, p3
        app_module._db = None


class TestLibraryTagFilter:
    def test_tag_filters_to_matching_photos(self, client_lib_tags):
        c, p1, p2, p3 = client_lib_tags
        resp = c.get("/library?tag=boston")
        assert resp.status_code == 200
        ids = _lib_ids(resp)
        assert p1 in ids
        assert p2 not in ids
        assert p3 not in ids

    def test_no_tag_returns_all(self, client_lib_tags):
        c, p1, p2, p3 = client_lib_tags
        resp = c.get("/library")
        ids = _lib_ids(resp)
        assert {p1, p2, p3}.issubset(ids)

    def test_nonexistent_tag_returns_zero(self, client_lib_tags):
        c, p1, p2, p3 = client_lib_tags
        resp = c.get("/library?tag=no-such-tag")
        ids = _lib_ids(resp)
        assert len(ids) == 0

    def test_tag_datalist_rendered_in_library(self, client_lib_tags):
        c, p1, p2, p3 = client_lib_tags
        resp = c.get("/library")
        body = resp.data.decode()
        assert 'id="lib-tags"' in body

    def test_view_on_map_link_carries_tag(self, client_lib_tags):
        c, p1, p2, p3 = client_lib_tags
        resp = c.get("/library?tag=boston")
        body = resp.data.decode()
        import re

        map_links = re.findall(r'href="(/map[^"]*)"', body)
        tag_link = next((u for u in map_links if "tag=" in u), None)
        assert tag_link is not None, "No /map link with tag= found"
        assert "boston" in tag_link


# ── /api/map-photos tag filter ────────────────────────────────────────────────


@pytest.fixture()
def client_map_tags():
    """DB with geotagged photos: one tagged boston, one tagged concert, one untagged."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "test.db")
        p1 = db.upsert_photo(
            _photo(
                93,
                photos_tags=["boston"],
                latitude=42.36,
                longitude=-71.06,
                date_taken="2022-06-01T10:00:00",
                privacy_state="approved_public",
            )
        )
        p2 = db.upsert_photo(
            _photo(
                94,
                photos_tags=["concert"],
                latitude=42.37,
                longitude=-71.07,
                date_taken="2022-06-02T10:00:00",
                privacy_state="approved_public",
            )
        )
        p3 = db.upsert_photo(
            _photo(
                95,
                photos_tags=[],
                latitude=42.38,
                longitude=-71.08,
                date_taken="2022-06-03T10:00:00",
                privacy_state="approved_public",
            )
        )
        app_module._db = db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test"
        with app_module.app.test_client() as c:
            yield c, p1, p2, p3
        app_module._db = None


class TestMapTagFilter:
    def test_tag_scopes_map_results(self, client_map_tags):
        c, p1, p2, p3 = client_map_tags
        resp = c.get("/api/map-photos?tag=boston")
        assert resp.status_code == 200
        ids = _map_ids(resp)
        assert p1 in ids
        assert p2 not in ids
        assert p3 not in ids

    def test_no_tag_returns_all_geotagged(self, client_map_tags):
        c, p1, p2, p3 = client_map_tags
        resp = c.get("/api/map-photos")
        ids = _map_ids(resp)
        assert {p1, p2, p3}.issubset(ids)

    def test_nonexistent_tag_returns_empty(self, client_map_tags):
        c, p1, p2, p3 = client_map_tags
        resp = c.get("/api/map-photos?tag=no-such-tag")
        ids = _map_ids(resp)
        assert len(ids) == 0

    def test_tag_match_is_case_sensitive(self, client_map_tags):
        """Tag filter is intentionally case-sensitive — matches _library_where() behaviour."""
        c, p1, p2, p3 = client_map_tags
        resp = c.get("/api/map-photos?tag=Boston")  # capital B
        ids = _map_ids(resp)
        assert p1 not in ids

    def test_tag_in_flickr_tags_also_matches(self, client_map_tags):
        """Tag filter checks flickr_tags as well as photos_tags."""
        c, p1, p2, p3 = client_map_tags
        db = app_module._db
        p_flickr = db.upsert_photo(
            _photo(
                96,
                flickr_tags=["flickr-only-tag"],
                photos_tags=[],
                latitude=42.39,
                longitude=-71.09,
                date_taken="2022-06-04T10:00:00",
                privacy_state="approved_public",
            )
        )
        resp = c.get("/api/map-photos?tag=flickr-only-tag")
        ids = _map_ids(resp)
        assert p_flickr in ids

    def test_photo_with_tag_in_both_columns_appears_once(self, client_map_tags):
        """Photo with tag in both flickr_tags and photos_tags appears exactly once."""
        db = app_module._db
        p_both = db.upsert_photo(
            _photo(
                97,
                flickr_tags=["boston"],
                photos_tags=["boston"],
                latitude=42.40,
                longitude=-71.10,
                date_taken="2022-06-05T10:00:00",
                privacy_state="approved_public",
            )
        )
        c, p1, p2, p3 = client_map_tags
        resp = c.get("/api/map-photos?tag=boston")
        data = resp.get_json()
        matching = [p for p in data if p["id"] == p_both]
        assert len(matching) == 1


# ── /map tag deep-link ────────────────────────────────────────────────────────


@pytest.fixture()
def client_map_deep_link():
    """Minimal DB for map route deep-link tests."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "test.db")
        db.upsert_photo(
            _photo(
                98,
                latitude=42.36,
                longitude=-71.06,
                date_taken="2022-06-01T10:00:00",
                privacy_state="approved_public",
                photos_tags=["boston"],
            )
        )
        app_module._db = db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test"
        with app_module.app.test_client() as c:
            yield c
        app_module._db = None


class TestMapTagDeepLink:
    def test_map_view_passes_tag_to_initial_filters(self, client_map_deep_link):
        resp = client_map_deep_link.get("/map?tag=boston")
        assert resp.status_code == 200
        body = resp.data.decode()
        assert 'value="boston"' in body

    def test_map_view_renders_tag_datalist(self, client_map_deep_link):
        resp = client_map_deep_link.get("/map")
        body = resp.data.decode()
        assert 'id="map-tags"' in body

    def test_map_view_tag_datalist_contains_photo_tag(self, client_map_deep_link):
        resp = client_map_deep_link.get("/map")
        body = resp.data.decode()
        assert "boston" in body

    def test_map_to_library_roundtrip_carries_tag(self, client_map_deep_link):
        """openInLibrary() serialises tag — verify it appears in the JS."""
        resp = client_map_deep_link.get("/map?tag=boston")
        body = resp.data.decode()
        assert "params.set('tag'" in body or 'params.set("tag"' in body


# ── format_date Jinja filter ───────────────────────────────────────────────


class TestFormatDateFilter:
    def test_formats_iso_string_as_readable_date(self):
        from reviewer.app import app

        with app.app_context():
            env = app.jinja_env
            result = env.filters["format_date"]("2018-06-15")
        assert result == "Jun 15, 2018"

    def test_formats_single_digit_day(self):
        from reviewer.app import app

        with app.app_context():
            env = app.jinja_env
            result = env.filters["format_date"]("2018-06-05")
        assert result == "Jun 5, 2018"

    def test_invalid_input_returned_unchanged(self):
        from reviewer.app import app

        with app.app_context():
            env = app.jinja_env
            result = env.filters["format_date"]("not-a-date")
        assert result == "not-a-date"

    def test_empty_string_returned_unchanged(self):
        from reviewer.app import app

        with app.app_context():
            env = app.jinja_env
            result = env.filters["format_date"]("")
        assert result == ""
