# Map Filter Scoping Implementation Plan (#154)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add year range, album, person, and animation-privacy filters to the map, all ANDing with the existing time-pattern dropdown, so the trail and animation can be scoped to a real trip.

**Architecture:** All four new filters are appended as SQL WHERE fragments inside the existing `api_map_photos()` route in `app.py` (no new DB abstraction needed — the route already builds the query inline). `map_view()` gains two new template vars (`albums`, `person_names`) for the dropdowns. The filter bar in `map.html` grows a second row; `buildMapUrl()` is extended to include all active params; `_updateAnimateBtn()` learns about the privacy dropdown; `animatePOC()` pre-filters `_lastPhotos` by privacy before animating.

**Tech Stack:** Python/Flask, SQLite (`json_each`, EXISTS subqueries, ISO-string range predicates), Jinja2, vanilla JS (`<datalist>`, `debounce`, `requestAnimationFrame`).

---

## File map

| File | What changes |
|------|-------------|
| `reviewer/app.py` | `api_map_photos()` — parse 4 new query params, build SQL fragments; `map_view()` — pass `albums` and `person_names` |
| `reviewer/templates/map.html` | CSS (remove fixed height, add row-2 styles, chip row styles); HTML (row-2 controls, datalist, chip row); JS (`buildMapUrl`, `_updateAnimateBtn`, `animatePOC`, chip updater, debounce, change listeners) |
| `tests/test_map_filter.py` | New: 20 tests covering all backend filter paths and two template vars |

No migrations needed — no schema changes.

### Dataset invariants (preserve during implementation)

1. **`_lastPhotos` replaced atomically** — `reloadMarkers()` increments `_currentRequest`; `plotPhotos()` discards stale responses via the `requestId` guard. `_lastPhotos` is only written once the full response arrives. Never mutate it incrementally.

2. **Privacy filter never mutates `_lastPhotos`** — `animatePOC(photos)` uses `let pts = photos.filter(...)` which returns a new array. `photos` (which is `_lastPhotos`) is never modified. A subsequent UI interaction always reads the unfiltered set.

3. **NULL `date_taken` photos appear as dots, never in trail/animation** — the SQL carries no global `date_taken IS NOT NULL` guard. `plotTrail` and `animatePOC` both filter `.date` client-side. Do not add a server-side date guard.

4. **Deleted album IDs in URL state** — if `?album_id=N` is loaded and album N is deleted, the `<select>` shows `— any album —` (no matching option), the backend receives `album_id=N`, and the EXISTS subquery returns 0 rows (album is deleted, no active memberships). The result is an empty map, not an error. This is acceptable; no special handling needed.

---

### Task 1: Year range filter — tests and `api_map_photos()` backend

**Files:**
- Create: `tests/test_map_filter.py`
- Modify: `reviewer/app.py` (lines around `api_map_photos`)

- [ ] **Step 1: Create test file with year-range tests**

```python
# tests/test_map_filter.py
"""
tests/test_map_filter.py — map filter: year range, album, person, privacy (#154)

Run from repo root:
    python -m pytest tests/test_map_filter.py -v
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

import reviewer.app as app_module
from db.db import Database


def _photo(i: int, **kwargs) -> dict:
    base: dict = {
        "uuid": f"mf-u{i}",
        "original_filename": f"IMG_{i:04d}.JPG",
        "privacy_state": "needs_review",
        "apple_persons": [],
        "apple_labels": [],
        "apple_unknown_faces": 0,
        "apple_named_faces": 0,
    }
    base.update(kwargs)
    return base


@pytest.fixture()
def client_years():
    """DB with photos in 2016, 2019, and 2023 — all geotagged."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "test.db")
        p16 = db.upsert_photo(_photo(1, latitude=48.8, longitude=2.3,
                                      date_taken="2016-08-15T10:00:00",
                                      privacy_state="approved_public"))
        p19 = db.upsert_photo(_photo(2, latitude=40.7, longitude=-74.0,
                                      date_taken="2019-12-20T10:00:00",
                                      privacy_state="needs_review"))
        p23 = db.upsert_photo(_photo(3, latitude=51.5, longitude=-0.1,
                                      date_taken="2023-07-04T10:00:00",
                                      privacy_state="keep_private"))
        app_module._db = db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test"
        with app_module.app.test_client() as c:
            yield c, p16, p19, p23, db
        app_module._db = None


def _ids(resp) -> set[int]:
    return {p["id"] for p in resp.get_json()}


class TestYearRangeFilter:
    def test_year_from_excludes_earlier(self, client_years):
        c, p16, p19, p23, _ = client_years
        resp = c.get("/api/map-photos?year_from=2019")
        assert resp.status_code == 200
        ids = _ids(resp)
        assert p16 not in ids
        assert p19 in ids
        assert p23 in ids

    def test_year_to_excludes_later(self, client_years):
        c, p16, p19, p23, _ = client_years
        resp = c.get("/api/map-photos?year_to=2019")
        assert resp.status_code == 200
        ids = _ids(resp)
        assert p16 in ids
        assert p19 in ids
        assert p23 not in ids

    def test_year_range_both_bounds(self, client_years):
        c, p16, p19, p23, _ = client_years
        resp = c.get("/api/map-photos?year_from=2019&year_to=2019")
        assert resp.status_code == 200
        ids = _ids(resp)
        assert p16 not in ids
        assert p19 in ids
        assert p23 not in ids

    def test_year_from_greater_than_to_is_swapped(self, client_years):
        c, p16, p19, p23, _ = client_years
        # year_from=2023, year_to=2016 should silently swap to 2016-2023
        resp = c.get("/api/map-photos?year_from=2023&year_to=2016")
        assert resp.status_code == 200
        ids = _ids(resp)
        assert p16 in ids
        assert p19 in ids
        assert p23 in ids

    def test_response_ordered_by_date(self, client_years):
        c, p16, p19, p23, _ = client_years
        resp = c.get("/api/map-photos")
        photos = resp.get_json()
        dates = [p["date"] for p in photos if p["date"]]
        assert dates == sorted(dates), "API must return photos in date_taken order"

    def test_null_date_photos_present_as_dots(self, client_years):
        # Photos with NULL date_taken must appear in the response (valid map dots)
        c, p16, p19, p23, db = client_years
        p_nodate = db.upsert_photo(_photo(99, latitude=35.7, longitude=139.7))
        resp = c.get("/api/map-photos")
        ids = _ids(resp)
        assert p_nodate in ids, "NULL date_taken photo must appear as a map dot"

    def test_non_numeric_year_ignored(self, client_years):
        c, p16, p19, p23, _ = client_years
        resp = c.get("/api/map-photos?year_from=abc&year_to=xyz")
        assert resp.status_code == 200
        assert len(resp.get_json()) == 3  # no filter applied

    def test_out_of_range_year_ignored(self, client_years):
        c, p16, p19, p23, _ = client_years
        resp = c.get("/api/map-photos?year_from=1700&year_to=3000")
        assert resp.status_code == 200
        assert len(resp.get_json()) == 3  # no filter

    def test_privacy_state_in_response(self, client_years):
        c, *_ = client_years
        resp = c.get("/api/map-photos")
        assert resp.status_code == 200
        photos = resp.get_json()
        assert len(photos) > 0
        for p in photos:
            assert "privacy_state" in p, f"Missing privacy_state in {p}"
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python -m pytest tests/test_map_filter.py::TestYearRangeFilter -v
```

Expected: all 7 tests FAIL (year params not yet parsed; `privacy_state` not in response).

- [ ] **Step 3: Update `api_map_photos()` in `reviewer/app.py`**

Find the existing `api_map_photos()` function (around line 885). Replace it entirely:

```python
@app.route("/api/map-photos")
def api_map_photos() -> Response:
    flickr_username = _config.get("flickr", {}).get("username", "")
    time_pattern = request.args.get("time_pattern") or None
    time_expand = 2 if request.args.get("expand") == "1" else 0

    # ── New filter params ────────────────────────────────────────────────
    def _safe_year(key: str) -> int | None:
        raw = request.args.get(key)
        if not raw:
            return None
        try:
            y = int(raw)
        except ValueError:
            return None
        return y if 1800 <= y <= 2099 else None

    year_from = _safe_year("year_from")
    year_to   = _safe_year("year_to")
    if year_from is not None and year_to is not None and year_from > year_to:
        year_from, year_to = year_to, year_from

    album_id_raw = request.args.get("album_id")
    album_id: int | None = None
    if album_id_raw:
        try:
            album_id = int(album_id_raw)
        except ValueError:
            pass

    person = (request.args.get("person") or "").strip() or None

    # ── Build WHERE fragments ────────────────────────────────────────────
    where_frags: list[str] = []
    where_params: list = []

    # Time pattern (existing logic — unchanged)
    if time_pattern:
        from db.time_patterns import parse_pattern, birthday_clause

        if time_pattern.startswith("birthday:"):
            person_name = time_pattern[9:]
            bday = db().get_person_birthdays().get(person_name)
            if bday:
                all_years = [
                    r[0]
                    for r in db()
                    .conn.execute(
                        "SELECT DISTINCT CAST(strftime('%Y', date_taken) AS INTEGER) AS y "
                        "FROM photos WHERE date_taken IS NOT NULL ORDER BY y"
                    )
                    .fetchall()
                    if r[0] is not None
                ]
                month, day = (int(x) for x in bday[-5:].split("-"))
                frag, frag_params = birthday_clause(month, day, time_expand, all_years)
                if frag != "1=1":
                    where_frags.append(frag)
                    where_params.extend(frag_params)
        else:
            years = (
                [
                    r[0]
                    for r in db()
                    .conn.execute(
                        "SELECT DISTINCT CAST(strftime('%Y', date_taken) AS INTEGER) AS y "
                        "FROM photos WHERE date_taken IS NOT NULL ORDER BY y"
                    )
                    .fetchall()
                    if r[0] is not None
                ]
                if time_pattern.startswith("holiday:")
                else []
            )
            frag, frag_params = parse_pattern(time_pattern, time_expand, years)
            if frag != "1=1":
                where_frags.append(frag)
                where_params.extend(frag_params)

    # Year range — ISO string range predicates (index-friendly)
    if year_from is not None:
        where_frags.append("p.date_taken >= ?")
        where_params.append(f"{year_from:04d}-01-01")
    if year_to is not None:
        where_frags.append("p.date_taken < ?")
        where_params.append(f"{year_to + 1:04d}-01-01")

    # Album
    if album_id is not None:
        where_frags.append(
            "EXISTS (SELECT 1 FROM photo_albums pa2 "
            "WHERE pa2.photo_id = p.id AND pa2.album_id = ? AND pa2.removed_at IS NULL)"
        )
        where_params.append(album_id)

    # Person (case-insensitive)
    if person:
        where_frags.append(
            "EXISTS (SELECT 1 FROM json_each(p.apple_persons) je WHERE LOWER(je.value) = LOWER(?))"
        )
        where_params.append(person)

    extra_where = (" AND " + " AND ".join(where_frags)) if where_frags else ""

    rows = (
        db()
        .conn.execute(
            "SELECT p.id, p.latitude, p.longitude, p.photos_title, p.flickr_title, "
            "       p.date_taken, p.flickr_id, p.privacy_state "
            "FROM photos p "
            f"WHERE p.latitude IS NOT NULL AND p.longitude IS NOT NULL "
            f"AND p.flickr_deleted = 0{extra_where} "
            "ORDER BY p.date_taken, p.id",
            # NOTE: ORDER BY date_taken, id ensures deterministic ordering for trail/animation.
            # Do NOT add a date_taken IS NOT NULL filter here — photos with NULL dates are valid
            # map dots and must appear in the response; they are excluded from trail/animation
            # client-side (plotTrail and animatePOC both filter .date before using).
            # The secondary sort on p.id breaks ties so reloads never jitter.
            where_params,
        )
        .fetchall()
    )
    result = []
    for r in rows:
        title = (r["photos_title"] or r["flickr_title"] or "").strip() or "(untitled)"
        flickr_url = (
            f"https://www.flickr.com/photos/{flickr_username}/{r['flickr_id']}"
            if r["flickr_id"] and flickr_username
            else None
        )
        result.append(
            {
                "id": r["id"],
                "lat": r["latitude"],
                "lon": r["longitude"],
                "title": title,
                "date": (r["date_taken"] or "")[:10],
                "flickr_url": flickr_url,
                "privacy_state": r["privacy_state"],
            }
        )
    return jsonify(result)
```

- [ ] **Step 4: Run year-range tests**

```bash
python -m pytest tests/test_map_filter.py::TestYearRangeFilter -v
```

Expected: all 7 PASS.

- [ ] **Step 5: Run full suite to confirm no regressions**

```bash
python -m pytest tests/ -q
```

Expected: all existing tests pass.

- [ ] **Step 6: Commit**

```bash
git add reviewer/app.py tests/test_map_filter.py
git commit -m "feat(#154): year range filter + privacy_state in /api/map-photos

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 2: Album filter — tests and backend

**Files:**
- Modify: `tests/test_map_filter.py` (add album fixture and tests)
- Modify: `reviewer/app.py` (already done in Task 1 — album_id parsing is in place)

- [ ] **Step 1: Add album fixture and tests to `tests/test_map_filter.py`**

Append this to the file (after `TestYearRangeFilter`):

```python
@pytest.fixture()
def client_albums():
    """DB with two photos in album A, one in album B, one in neither."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "test.db")
        p1 = db.upsert_photo(_photo(10, latitude=48.8, longitude=2.3,
                                     date_taken="2018-06-01T10:00:00"))
        p2 = db.upsert_photo(_photo(11, latitude=40.7, longitude=-74.0,
                                     date_taken="2018-06-05T10:00:00"))
        p3 = db.upsert_photo(_photo(12, latitude=51.5, longitude=-0.1,
                                     date_taken="2020-03-01T10:00:00"))
        p4 = db.upsert_photo(_photo(13, latitude=35.7, longitude=139.7,
                                     date_taken="2021-01-01T10:00:00"))  # no album

        album_a = db.upsert_album("uuid-a", "Spain 2018")
        album_b = db.upsert_album("uuid-b", "UK 2020")
        db.upsert_photo_album(p1, album_a)
        db.upsert_photo_album(p2, album_a)
        db.upsert_photo_album(p3, album_b)

        app_module._db = db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test"
        with app_module.app.test_client() as c:
            yield c, p1, p2, p3, p4, album_a, album_b, db
        app_module._db = None


class TestAlbumFilter:
    def test_album_filter_returns_only_member_photos(self, client_albums):
        c, p1, p2, p3, p4, album_a, album_b, _ = client_albums
        resp = c.get(f"/api/map-photos?album_id={album_a}")
        assert resp.status_code == 200
        ids = _ids(resp)
        assert ids == {p1, p2}

    def test_album_filter_respects_removed_at(self, client_albums):
        c, p1, p2, p3, p4, album_a, album_b, db = client_albums
        # Tombstone p2 from album_a
        db.conn.execute(
            "UPDATE photo_albums SET removed_at = '2024-01-01T00:00:00' "
            "WHERE photo_id = ? AND album_id = ?", (p2, album_a)
        )
        db.conn.commit()
        resp = c.get(f"/api/map-photos?album_id={album_a}")
        ids = _ids(resp)
        assert p2 not in ids
        assert p1 in ids

    def test_album_filter_different_album(self, client_albums):
        c, p1, p2, p3, p4, album_a, album_b, _ = client_albums
        resp = c.get(f"/api/map-photos?album_id={album_b}")
        ids = _ids(resp)
        assert ids == {p3}

    def test_invalid_album_id_ignored(self, client_albums):
        c, p1, p2, p3, p4, *_ = client_albums
        resp = c.get("/api/map-photos?album_id=notanumber")
        assert resp.status_code == 200
        assert len(resp.get_json()) == 4  # all photos returned
```

- [ ] **Step 2: Run album tests**

```bash
python -m pytest tests/test_map_filter.py::TestAlbumFilter -v
```

Expected: all 4 PASS (album_id parsing is already in `api_map_photos()` from Task 1).

- [ ] **Step 3: Commit**

```bash
git add tests/test_map_filter.py
git commit -m "test(#154): album filter tests

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 3: Person filter — tests

**Files:**
- Modify: `tests/test_map_filter.py` (add person fixture and tests)

- [ ] **Step 1: Add person fixture and tests**

Append to `tests/test_map_filter.py`:

```python
@pytest.fixture()
def client_persons():
    """DB with photos tagged with different people."""
    import json as _json
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "test.db")
        p1 = db.upsert_photo(_photo(20, latitude=48.8, longitude=2.3,
                                     date_taken="2014-11-01T10:00:00",
                                     apple_persons=["Marcin Sulikowski", "Chris Devers"]))
        p2 = db.upsert_photo(_photo(21, latitude=21.0, longitude=105.8,
                                     date_taken="2016-05-15T10:00:00",
                                     apple_persons=["Marcin Sulikowski", "_UNKNOWN_"]))
        p3 = db.upsert_photo(_photo(22, latitude=51.5, longitude=-0.1,
                                     date_taken="2018-09-20T10:00:00",
                                     apple_persons=["Chris Devers"]))
        p4 = db.upsert_photo(_photo(23, latitude=40.7, longitude=-74.0,
                                     date_taken="2022-03-01T10:00:00",
                                     apple_persons=["_UNKNOWN_"]))

        app_module._db = db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test"
        with app_module.app.test_client() as c:
            yield c, p1, p2, p3, p4, db
        app_module._db = None


class TestPersonFilter:
    def test_person_filter_returns_matching_photos(self, client_persons):
        c, p1, p2, p3, p4, _ = client_persons
        resp = c.get("/api/map-photos?person=Marcin+Sulikowski")
        assert resp.status_code == 200
        ids = _ids(resp)
        assert ids == {p1, p2}

    def test_person_filter_case_insensitive(self, client_persons):
        c, p1, p2, p3, p4, _ = client_persons
        resp = c.get("/api/map-photos?person=marcin+sulikowski")
        ids = _ids(resp)
        assert ids == {p1, p2}

    def test_person_filter_unknown_not_matched_by_unknown_string(self, client_persons):
        # Searching for "_UNKNOWN_" finds photos with _UNKNOWN_ entries
        c, p1, p2, p3, p4, _ = client_persons
        resp = c.get("/api/map-photos?person=_UNKNOWN_")
        ids = _ids(resp)
        assert p4 in ids    # has _UNKNOWN_
        assert p3 not in ids  # Chris only, no _UNKNOWN_

    def test_person_filter_blank_returns_all(self, client_persons):
        c, p1, p2, p3, p4, _ = client_persons
        resp = c.get("/api/map-photos?person=")
        ids = _ids(resp)
        assert ids == {p1, p2, p3, p4}

    def test_combined_year_and_person(self, client_persons):
        c, p1, p2, p3, p4, _ = client_persons
        # Marcin + year_from=2016 → only p2 (2016-05-15)
        resp = c.get("/api/map-photos?person=Marcin+Sulikowski&year_from=2016&year_to=2016")
        ids = _ids(resp)
        assert ids == {p2}
```

- [ ] **Step 2: Run person tests**

```bash
python -m pytest tests/test_map_filter.py::TestPersonFilter -v
```

Expected: all 5 PASS (person filter already in `api_map_photos()` from Task 1).

- [ ] **Step 3: Commit**

```bash
git add tests/test_map_filter.py
git commit -m "test(#154): person filter tests (case-insensitive, combined)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 4: Template vars — `albums` and `person_names` in `map_view()`

**Files:**
- Modify: `reviewer/app.py` (`map_view()` function)
- Modify: `tests/test_map_filter.py` (add template var tests)

- [ ] **Step 1: Add template-var tests**

Append to `tests/test_map_filter.py`:

```python
@pytest.fixture()
def client_template_vars():
    """DB with one album and one named person."""
    import json as _json
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "test.db")
        db.upsert_photo(_photo(30, apple_persons=["Alice Wonderland", "_UNKNOWN_"]))
        db.upsert_photo(_photo(31, apple_persons=["Bob Builder"]))
        db.upsert_album("uuid-tv1", "Japan 2019")
        db.upsert_album("uuid-tv2", "Scotland 2022")

        app_module._db = db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test"
        with app_module.app.test_client() as c:
            yield c, db
        app_module._db = None


class TestMapViewTemplateVars:
    def test_albums_passed_to_template(self, client_template_vars):
        c, _ = client_template_vars
        resp = c.get("/map")
        assert resp.status_code == 200
        body = resp.data.decode()
        assert "Japan 2019" in body
        assert "Scotland 2022" in body

    def test_person_names_passed_to_template(self, client_template_vars):
        c, _ = client_template_vars
        resp = c.get("/map")
        assert resp.status_code == 200
        body = resp.data.decode()
        assert "Alice Wonderland" in body
        assert "Bob Builder" in body

    def test_unknown_excluded_from_person_names(self, client_template_vars):
        c, _ = client_template_vars
        resp = c.get("/map")
        body = resp.data.decode()
        # _UNKNOWN_ must not appear in person datalist (it may appear elsewhere in page)
        # Check the datalist specifically by looking for the datalist section
        assert "_UNKNOWN_" not in body
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python -m pytest tests/test_map_filter.py::TestMapViewTemplateVars -v
```

Expected: all 3 FAIL (`albums` and `person_names` not yet in `map_view()`).

- [ ] **Step 3: Update `map_view()` in `reviewer/app.py`**

Find `map_view()` (around line 841). Replace the `return render_template(...)` call at the end:

```python
    # Gather template vars for filter bar
    albums = db().get_all_albums()

    person_names_rows = db().conn.execute(
        """
        SELECT DISTINCT je.value
        FROM photos p, json_each(p.apple_persons) je
        WHERE je.value != '_UNKNOWN_'
          AND je.value != ''
          AND p.apple_persons IS NOT NULL
        ORDER BY je.value
        """
    ).fetchall()
    person_names = [r[0] for r in person_names_rows]

    return render_template(
        "map.html",
        center_lat=center_lat,
        center_lon=center_lon,
        highlight_id=highlight_id,
        birthday_people=db().get_person_birthdays(),
        albums=albums,
        person_names=person_names,
    )
```

- [ ] **Step 4: Run template-var tests**

```bash
python -m pytest tests/test_map_filter.py::TestMapViewTemplateVars -v
```

Expected: all 3 PASS.

- [ ] **Step 5: Run full suite**

```bash
python -m pytest tests/ -q
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add reviewer/app.py tests/test_map_filter.py
git commit -m "feat(#154): map_view passes albums and person_names to template

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 5: Template — CSS and row 1 (year inputs)

**Files:**
- Modify: `reviewer/templates/map.html`

The existing `.map-filter-bar` has `height: 40px` — this must be removed (replaced with `min-height`) to allow two rows. The `#map` height calculation also references `40px` and must be updated.

- [ ] **Step 1: Update CSS in `map.html`**

In `{% block extra_style %}`, find and replace the filter bar CSS:

Old:
```css
.map-filter-bar {
  height: 40px;
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 0 16px;
  border-bottom: 1px solid var(--border);
  background: var(--surface);
  font-size: 13px;
}
.map-filter-bar label { display: flex; align-items: center; gap: 6px; }
#map { height: calc(100vh - 48px - 40px); width: 100%; }
```

New:
```css
.map-filter-bar {
  display: flex;
  flex-direction: column;
  border-bottom: 1px solid var(--border);
  background: var(--surface);
  font-size: 13px;
}
.map-filter-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 0 16px;
  height: 40px;
  flex-wrap: wrap;
}
.map-filter-row + .map-filter-row {
  border-top: 1px solid var(--border);
}
.map-filter-bar label { display: flex; align-items: center; gap: 6px; }
.map-filter-chips {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 16px;
  flex-wrap: wrap;
  border-top: 1px solid var(--border);
  min-height: 0;
}
.map-filter-chips:empty { display: none; }
.map-chip {
  background: #e0eeff;
  border: 1px solid #a0c0ff;
  border-radius: 12px;
  padding: 1px 8px;
  font-size: 11px;
  color: #1a5fbf;
  white-space: nowrap;
  max-width: 180px;
  overflow: hidden;
  text-overflow: ellipsis;
}
.map-chip-anim {
  background: #fff0d0;
  border-color: #e0a040;
  color: #7a4800;
}
#map { height: calc(100vh - 48px - 80px); width: 100%; }
```

Note: `80px` = two 40px rows. The chip row adds its own height when non-empty (handled by flexbox); the `#map` height will shrink slightly when chips are shown, which is acceptable.

- [ ] **Step 2: Restructure the filter bar HTML**

Find the existing `<div class="map-filter-bar">` block. Replace the entire block with:

```html
<div class="map-filter-bar">
  <!-- Row 1: time scope + trail + animate -->
  <div class="map-filter-row">
    <label>Time of year
      <select id="map-time-select">
        <option value="">Any time</option>
        <optgroup label="Month">
          <option value="month:01">January</option>
          <option value="month:02">February</option>
          <option value="month:03">March</option>
          <option value="month:04">April</option>
          <option value="month:05">May</option>
          <option value="month:06">June</option>
          <option value="month:07">July</option>
          <option value="month:08">August</option>
          <option value="month:09">September</option>
          <option value="month:10">October</option>
          <option value="month:11">November</option>
          <option value="month:12">December</option>
        </optgroup>
        <optgroup label="Season">
          <option value="season:spring">Spring (Mar–Jun)</option>
          <option value="season:summer">Summer (Jun–Sep)</option>
          <option value="season:fall">Fall (Sep–Dec)</option>
          <option value="season:winter">Winter (Dec–Mar)</option>
        </optgroup>
        <optgroup label="Day type">
          <option value="daytype:weekend">Weekends</option>
          <option value="daytype:weekday">Weekdays</option>
        </optgroup>
        <optgroup label="Holidays">
          <option value="holiday:new_years">New Year's Day (Jan 1)</option>
          <option value="holiday:mlk_day">MLK Day (3rd Mon Jan)</option>
          <option value="holiday:presidents_day">Presidents' Day (3rd Mon Feb)</option>
          <option value="holiday:memorial_day">Memorial Day (last Mon May)</option>
          <option value="holiday:july_4th">July 4th</option>
          <option value="holiday:labor_day">Labor Day (1st Mon Sep)</option>
          <option value="holiday:columbus_day">Columbus Day (2nd Mon Oct)</option>
          <option value="holiday:halloween">Halloween (Oct 31)</option>
          <option value="holiday:thanksgiving">Thanksgiving (4th Thu Nov)</option>
          <option value="holiday:christmas">Christmas (Dec 25)</option>
        </optgroup>
        {% if birthday_people %}
        <optgroup label="Birthdays">
          {% for name in birthday_people | sort %}
          <option value="birthday:{{ name | e }}">{{ name }}'s birthday</option>
          {% endfor %}
        </optgroup>
        {% endif %}
      </select>
    </label>
    <label id="map-expand-label" style="display:none;align-items:center;gap:5px">
      <input type="checkbox" id="map-expand-cb"> ±2 days
    </label>
    <label style="display:flex;align-items:center;gap:5px">Year
      <input type="number" id="map-year-from" min="1800" max="2099"
             placeholder="from" style="width:62px">
      <span>–</span>
      <input type="number" id="map-year-to" min="1800" max="2099"
             placeholder="to" style="width:62px">
    </label>
    <label style="align-items:center;gap:5px">
      <input type="checkbox" id="map-trail-cb"> Trail
    </label>
    <button type="button" id="map-animate-btn" onclick="toggleAnimation()"
            class="map-btn" style="display:none">Animate</button>
    <span id="map-photo-count" style="font-size:12px;color:var(--muted)"></span>
    <button type="button" id="map-draw-btn" onclick="toggleDraw()"
            class="map-btn" style="margin-left:auto">Draw selection</button>
    <button type="button" id="map-clear-btn" onclick="clearSelection()"
            class="map-btn" style="display:none">✕ Clear selection</button>
    <button type="button" id="map-open-lib-btn" onclick="openInLibrary()"
            class="map-btn map-btn-primary">Open in Library ↗</button>
  </div>

  <!-- Row 2: person + album + animation privacy -->
  <div class="map-filter-row">
    <label>Person
      <input type="text" id="map-person" list="map-person-datalist"
             placeholder="Search name…" style="width:160px">
      <datalist id="map-person-datalist">
        {% for name in person_names %}
        <option value="{{ name | e }}">
        {% endfor %}
      </datalist>
    </label>
    <label>Album
      <select id="map-album-select">
        <option value="">— any album —</option>
        {% for album in albums %}
        <option value="{{ album.id }}">{{ album.name | e }}</option>
        {% endfor %}
      </select>
    </label>
    <label style="margin-left:auto;align-items:center;gap:5px">▶ Animate:
      <select id="map-privacy-select">
        <option value="all">All photos</option>
        <option value="public">Public only</option>
        <option value="private">Private only</option>
      </select>
    </label>
  </div>

  <!-- Active filter chips -->
  <div class="map-filter-chips" id="map-filter-chips"></div>
</div>
```

- [ ] **Step 3: Update `buildMapUrl()` to include all filter params**

Find the existing `buildMapUrl()` function and replace it:

```js
function buildMapUrl() {
  const params = new URLSearchParams();
  const pattern = document.getElementById('map-time-select').value;
  if (pattern) params.set('time_pattern', pattern);
  if (document.getElementById('map-expand-cb').checked) params.set('expand', '1');
  const yf = document.getElementById('map-year-from').value.trim();
  const yt = document.getElementById('map-year-to').value.trim();
  if (yf) params.set('year_from', yf);
  if (yt) params.set('year_to', yt);
  const album = document.getElementById('map-album-select').value;
  if (album) params.set('album_id', album);
  const person = document.getElementById('map-person').value.trim();
  if (person) params.set('person', person);
  const s = params.toString();
  return s ? `/api/map-photos?${s}` : '/api/map-photos';
}
```

- [ ] **Step 4: Update `openInLibrary()` to pass new params**

Find the `openInLibrary()` function and replace just the params-building section (after the existing geo bounds lines):

```js
function openInLibrary() {
  const bounds = _drawnBounds || map.getBounds();
  const params = new URLSearchParams({
    lat_min: bounds.getSouth().toFixed(5),
    lat_max: bounds.getNorth().toFixed(5),
    lon_min: bounds.getWest().toFixed(5),
    lon_max: bounds.getEast().toFixed(5),
  });
  const tp = document.getElementById('map-time-select').value;
  if (tp) params.set('time_pattern', tp);
  if (document.getElementById('map-expand-cb').checked) params.set('expand', '1');
  const yf = document.getElementById('map-year-from').value.trim();
  const yt = document.getElementById('map-year-to').value.trim();
  if (yf) params.set('year_from', yf);
  if (yt) params.set('year_to', yt);
  const album = document.getElementById('map-album-select').value;
  if (album) params.set('album_id', album);
  const person = document.getElementById('map-person').value.trim();
  if (person) params.set('person', person);
  window.open('/library?' + params.toString(), '_blank');
}
```

- [ ] **Step 5: Update the `map-time-select` change handler**

The existing handler shows/hides `#map-trail-label` based on `isAnyPattern`. With new filters, the trail label is always shown (row 2 is always visible). Replace the change handler:

```js
document.getElementById('map-time-select').addEventListener('change', function () {
  const lbl = document.getElementById('map-expand-label');
  const cb  = document.getElementById('map-expand-cb');
  const isHoliday = this.value.startsWith('holiday:');
  lbl.style.display = isHoliday ? 'flex' : 'none';
  if (!isHoliday) cb.checked = false;

  // If pattern cleared AND no other filter active, reset trail/animate
  if (!_hasAnyFilter()) {
    document.getElementById('map-trail-cb').checked = false;
    stopAnimation();
    if (_trailLayer) { map.removeLayer(_trailLayer); _trailLayer = null; }
  }
  reloadMarkers();
  _updateFilterChips();
});
```

Add the `_hasAnyFilter()` helper (place near top of the `<script>` block, before its first use):

```js
function _hasAnyFilter() {
  if (document.getElementById('map-time-select').value) return true;
  if (document.getElementById('map-year-from').value.trim()) return true;
  if (document.getElementById('map-year-to').value.trim()) return true;
  if (document.getElementById('map-album-select').value) return true;
  if (document.getElementById('map-person').value.trim()) return true;
  return false;
}
```

- [ ] **Step 6: Add `_updateFilterChips` stub** (full implementation in Task 8)

Inside the `<script>` block, add immediately before `_hasAnyFilter()`:

```js
// Stub — replaced by full implementation in Task 8
function _updateFilterChips() {}
```

- [ ] **Step 7: Verify the map still loads in the browser**

Start the dev server:
```bash
python reviewer/app.py --config config/config.yml
```

Open `http://localhost:5173/map` (or the port from your config). Verify:
1. Two-row filter bar renders without layout breakage
2. Year from/to inputs appear in row 1
3. Person, album, and "▶ Animate:" controls appear in row 2
4. Map fills the viewport (no gap at bottom)
5. Existing pattern dropdown still works (change to "August", dots update)

- [ ] **Step 8: Commit**

```bash
git add reviewer/templates/map.html
git commit -m "feat(#154): two-row filter bar with year range inputs

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 6: Event listeners for row-2 controls + debounce

**Files:**
- Modify: `reviewer/templates/map.html` (JS section)

- [ ] **Step 1: Add debounce helper and row-2 change listeners**

Add the `debounce` helper immediately before `reloadMarkers()` (the initial load call at the end of the event-wiring section):

```js
function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

// ── Row-2 filter listeners ─────────────────────────────────────────────────
const _debouncedReload = debounce(() => { reloadMarkers(); _updateFilterChips(); }, 300);

document.getElementById('map-person').addEventListener('input', _debouncedReload);

document.getElementById('map-album-select').addEventListener('change', () => {
  reloadMarkers();
  _updateFilterChips();
});

document.getElementById('map-year-from').addEventListener('change', () => {
  reloadMarkers();
  _updateFilterChips();
});

document.getElementById('map-year-to').addEventListener('change', () => {
  reloadMarkers();
  _updateFilterChips();
});

document.getElementById('map-privacy-select').addEventListener('change', () => {
  _updateAnimateBtn();
  _updateFilterChips();
});
```

- [ ] **Step 2: Verify year + person filters work in browser**

Open `http://localhost:5173/map`.
1. Type a name in the Person field — after 300 ms, dots should update.
2. Type `2019` in year-from field and press Tab — dots should narrow.
3. Select an album — dots narrow to that album.
4. Clear all — dots return to full set.

- [ ] **Step 3: Commit**

```bash
git add reviewer/templates/map.html
git commit -m "feat(#154): row-2 event listeners with debounce

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 7: Privacy-aware `_updateAnimateBtn()` and `animatePOC()` pre-filter

**Files:**
- Modify: `reviewer/templates/map.html` (JS: `_updateAnimateBtn`, `animatePOC`, `toggleAnimation`)

- [ ] **Step 1: Replace `_updateAnimateBtn()`**

Find the existing `_updateAnimateBtn()` function and replace it:

```js
function _updateAnimateBtn() {
  const btn = document.getElementById('map-animate-btn');
  if (!btn) return;
  const trailOn = document.getElementById('map-trail-cb').checked;
  if (!trailOn) {
    btn.style.display = 'none';
    btn.disabled = false;
    return;
  }
  btn.style.display = '';
  const privacySel = document.getElementById('map-privacy-select').value;
  const PUBLIC_STATES = new Set(['approved_public', 'already_public']);
  let eligible = _lastPhotos.filter(p => p.lat != null && p.lon != null);
  if (privacySel === 'public') {
    eligible = eligible.filter(p => PUBLIC_STATES.has(p.privacy_state));
  } else if (privacySel === 'private') {
    eligible = eligible.filter(p => !PUBLIC_STATES.has(p.privacy_state));
  }
  btn.disabled = eligible.length < 2;
}
```

- [ ] **Step 2: Update `animatePOC()` to add privacy pre-filter**

Find the existing `animatePOC(photos)` function. Replace just the opening lines (before `// Build segments`):

Old opening:
```js
function animatePOC(photos) {
  const pts = photos.filter(p => p.lat != null && p.lon != null);
  if (pts.length < 2) return;
```

New opening:
```js
function animatePOC(photos) {
  const privacySel = document.getElementById('map-privacy-select').value;
  const PUBLIC_STATES = new Set(['approved_public', 'already_public']);
  // Keep only geotagged photos with a date (same exclusion as plotTrail)
  let pts = photos.filter(p => p.lat != null && p.lon != null && p.date);
  if (privacySel === 'public') {
    pts = pts.filter(p => PUBLIC_STATES.has(p.privacy_state));
  } else if (privacySel === 'private') {
    pts = pts.filter(p => !PUBLIC_STATES.has(p.privacy_state));
  }
  // Sort deterministically by date then id — matches ORDER BY in the API and plotTrail sort
  pts.sort((a, b) => a.date < b.date ? -1 : a.date > b.date ? 1 : a.id - b.id);
  if (pts.length < 2) return;  // button was disabled; safety guard
```

- [ ] **Step 3: Verify privacy toggle in browser**

1. Load a time filter with ≥2 geotagged, mixed-privacy photos.
2. Check "Trail". Animate button appears.
3. Set "▶ Animate:" to "Public only". If < 2 public photos, Animate button goes gray/disabled.
4. Set back to "All photos". Button re-enables.
5. Click Animate. Animation plays using only the privacy-filtered set.

- [ ] **Step 4: Commit**

```bash
git add reviewer/templates/map.html
git commit -m "feat(#154): privacy-aware _updateAnimateBtn and animatePOC pre-filter

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 8: Active filter chip row

**Files:**
- Modify: `reviewer/templates/map.html` (JS: `_updateFilterChips`)

The chip row (`<div id="map-filter-chips">`) was added to the HTML in Task 5. Now replace the stub with the full implementation.

- [ ] **Step 1: Replace the `_updateFilterChips()` stub**

Find `function _updateFilterChips() {}` (the stub added in Task 5) and replace it with:

```js
function _updateFilterChips() {
  const container = document.getElementById('map-filter-chips');
  if (!container) return;
  const chips = [];

  const patternEl = document.getElementById('map-time-select');
  if (patternEl.value) {
    const label = patternEl.options[patternEl.selectedIndex].text;
    chips.push({ text: label, cls: 'map-chip' });
  }

  const yf = document.getElementById('map-year-from').value.trim();
  const yt = document.getElementById('map-year-to').value.trim();
  if (yf && yt) {
    chips.push({ text: `${yf}–${yt}`, cls: 'map-chip' });
  } else if (yf) {
    chips.push({ text: `from ${yf}`, cls: 'map-chip' });
  } else if (yt) {
    chips.push({ text: `to ${yt}`, cls: 'map-chip' });
  }

  const person = document.getElementById('map-person').value.trim();
  if (person) chips.push({ text: person, cls: 'map-chip' });

  const albumEl = document.getElementById('map-album-select');
  if (albumEl.value) {
    const albumLabel = albumEl.options[albumEl.selectedIndex].text;
    chips.push({ text: albumLabel, cls: 'map-chip' });
  }

  const privacyEl = document.getElementById('map-privacy-select');
  if (privacyEl.value !== 'all') {
    const privLabel = privacyEl.options[privacyEl.selectedIndex].text;
    chips.push({ text: `▶ ${privLabel}`, cls: 'map-chip map-chip-anim' });
  }

  container.innerHTML = chips
    .map(c => `<span class="${c.cls}">${esc(c.text)}</span>`)
    .join('');
}
```

- [ ] **Step 2: Call `_updateFilterChips()` on initial load**

Find the `reloadMarkers();   // initial load` line and add a call immediately after:

```js
reloadMarkers();   // initial load
_updateFilterChips();
```

- [ ] **Step 3: Verify chip row in browser**

1. Select "August" from the pattern dropdown → chip "August" appears.
2. Set year-from=2015, year-to=2019 → chip "2015–2019" appears.
3. Type a person name → chip appears (after 300 ms debounce).
4. Select an album → album chip appears.
5. Set "▶ Animate:" to "Public only" → amber chip "▶ Public only" appears.
6. Clear pattern → chip disappears.
7. With no active filters, chip row is empty and visually hidden.

- [ ] **Step 4: Commit**

```bash
git add reviewer/templates/map.html
git commit -m "feat(#154): active filter chip row

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 9: Run full test suite, lint, and push

**Files:** none new

- [ ] **Step 1: Run the full test suite**

```bash
python -m pytest tests/ -q
```

Expected: all tests pass. Fix any failures before proceeding.

- [ ] **Step 2: Run linter**

```bash
make lint
```

Fix any ruff or mypy errors before proceeding.

- [ ] **Step 3: Manual end-to-end verification checklist**

Start the dev server: `python reviewer/app.py --config config/config.yml`

| # | Scenario | Expected |
|---|----------|----------|
| 1 | No filters → map loads all geotagged photos | All dots visible |
| 2 | Pattern "August" → year from 2018, to 2018 | Only Aug 2018 dots |
| 3 | Person = "Marcin Sulikowski" | Only Marcin photos |
| 4 | Album = "Vietnam 2014" | Only Vietnam album photos |
| 5 | Year from > year to (e.g., from=2020, to=2015) | Treated as 2015–2020 |
| 6 | All filters combined | AND semantics; fewer dots |
| 7 | Enable trail; click Animate | Animation plays scoped set |
| 8 | Set privacy = "Public only"; Animate with 0 public photos | Button disabled |
| 9 | Privacy chip shows amber "▶ Public only" | Chip appears |
| 10 | Clear all filters | All dots return; chips empty |

- [ ] **Step 4: Update GH issue #154 with status comment**

```bash
gh issue comment 154 --body "Implementation complete. All tests pass. Manual verification done. Closing."
gh issue close 154
```

- [ ] **Step 5: Push**

```bash
git push
```

- [ ] **Step 6: Bump version**

```bash
make bump
git push && git push --tags
```
