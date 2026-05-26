# Map Spatial Filter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a draw-rectangle tool to the map and an "Open in Library" button that opens matching photos in the library UI, filtered by the drawn area (or current viewport) plus the active time filter.

**Architecture:** The map produces a URL with bbox params (`lat_min`, `lat_max`, `lon_min`, `lon_max`) and opens it in a new tab. The library gains a bbox filter in its existing `_library_where` pipeline — one more composable clause alongside dates, tags, and persons. Bulk operations work for free because they already derive IDs from `_library_where`.

**Tech Stack:** Python/Flask, SQLite, Jinja2, Leaflet 1.9.4, Leaflet.draw 1.0.4

---

## File map

| File | Change |
|---|---|
| `db/photo_filters.py` | Add `build_bbox_clause` |
| `db/db.py` | `_library_where` + 4 new params; update `library_photos`, `library_photo_count`, `library_photo_ids` |
| `reviewer/app.py` | Module-level `_parse_float`; library route bbox parsing/clamping; bulk-edit route bbox passthrough; `/api/map-photos` `flickr_deleted` fix |
| `reviewer/templates/library.html` | `filter_count`, hidden inputs, "Map area ✕" chip, `_buildPayload` |
| `reviewer/templates/map.html` | Leaflet.draw CDN, draw/clear/open-lib buttons, spatial selection JS |
| `tests/test_photo_filters.py` | `build_bbox_clause` unit tests |
| `tests/test_library_search.py` | bbox route + `_library_where` integration tests |
| `tests/test_map_routes.py` | `flickr_deleted` fix test; map page button presence |

---

## Task 1: `build_bbox_clause` — pure filter module

**Files:**
- Modify: `db/photo_filters.py` (append after line 87)
- Test: `tests/test_photo_filters.py`

Background: `db/photo_filters.py` contains pure functions that return `(sql_fragment, params)` tuples. They have no Flask or DB dependencies. All fragments reference the `p` alias (`FROM photos p`). This task adds `build_bbox_clause` following the same pattern as `build_location_clause`.

- [ ] **Step 1: Write failing tests**

Add to the bottom of `tests/test_photo_filters.py`:

```python
from db.photo_filters import build_bbox_clause  # add to existing import or add new import


class TestBuildBboxClause:
    def test_returns_four_params_in_order(self):
        _, params = build_bbox_clause(42.35, 42.41, -71.12, -71.08)
        assert params == [42.35, 42.41, -71.12, -71.08]

    def test_sql_uses_between_for_lat_and_lon(self):
        sql, _ = build_bbox_clause(42.35, 42.41, -71.12, -71.08)
        assert sql.count("BETWEEN") == 2

    def test_sql_guards_null_coordinates(self):
        sql, _ = build_bbox_clause(42.35, 42.41, -71.12, -71.08)
        assert "p.latitude IS NOT NULL" in sql
        assert "p.longitude IS NOT NULL" in sql

    def test_negative_longitudes_accepted(self):
        # West-of-prime-meridian coords should work fine
        _, params = build_bbox_clause(-10.0, 10.0, -180.0, -90.0)
        assert params == [-10.0, 10.0, -180.0, -90.0]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_photo_filters.py::TestBuildBboxClause -v
```

Expected: `ImportError: cannot import name 'build_bbox_clause'`

- [ ] **Step 3: Implement `build_bbox_clause`**

Append to `db/photo_filters.py` after the `build_date_alias_clause` function:

```python
def build_bbox_clause(
    lat_min: float, lat_max: float, lon_min: float, lon_max: float
) -> tuple[str, list]:
    """Spatial bounding-box filter. Returns photos whose GPS coordinates fall
    inside the given rectangle (BETWEEN is inclusive on both ends).
    Caller must ensure all four params are non-None and that lat_min <= lat_max
    and lon_min <= lon_max (app.py normalises these before calling)."""
    sql = (
        "p.latitude IS NOT NULL AND p.longitude IS NOT NULL"
        " AND p.latitude BETWEEN ? AND ?"
        " AND p.longitude BETWEEN ? AND ?"
    )
    return sql, [lat_min, lat_max, lon_min, lon_max]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_photo_filters.py::TestBuildBboxClause -v
```

Expected: 4 passed

- [ ] **Step 5: Run full suite to check for regressions**

```bash
python -m pytest tests/ -q
```

Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add db/photo_filters.py tests/test_photo_filters.py
git commit -m "feat(#144): build_bbox_clause — spatial bounding-box filter fragment

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 2: Extend `_library_where` and its three callers

**Files:**
- Modify: `db/db.py:876-1101`
- Test: `tests/test_library_search.py`

Background: `_library_where` (line 876) builds a composable WHERE clause. It is called by three public methods: `library_photos` (line 964), `library_photo_count` (line 1022), `library_photo_ids` (line 1062). All three callers already use keyword args — adding new params is safe. This task adds four new optional params (`lat_min`, `lat_max`, `lon_min`, `lon_max`) and a dispatch block after the existing `#141` block.

- [ ] **Step 1: Write failing DB integration tests**

Add a new fixture and tests at the bottom of `tests/test_library_search.py`:

```python
@pytest.fixture()
def client_geo():
    """
    3-photo fixture for bbox tests:

    p_inside — lat=42.38, lon=-71.10 — inside the test box (42.35–42.41, -71.12–-71.08)
               date_taken="2023-10-15T10:00:00"  (October)
    p_outside — lat=48.86, lon=2.35 — Paris, outside the test box
                date_taken="2023-10-20T10:00:00"  (October)
    p_boundary — lat=42.35, lon=-71.12 — exactly on the boundary (BETWEEN is inclusive)
    """
    with tempfile.TemporaryDirectory() as tmp:
        test_db = Database(Path(tmp) / "test.db")
        p_inside = test_db.upsert_photo(
            _photo(10, latitude=42.38, longitude=-71.10,
                   photos_title="Inside", date_taken="2023-10-15T10:00:00")
        )
        p_outside = test_db.upsert_photo(
            _photo(11, latitude=48.86, longitude=2.35,
                   photos_title="Outside", date_taken="2023-10-20T10:00:00")
        )
        p_boundary = test_db.upsert_photo(
            _photo(12, latitude=42.35, longitude=-71.12,
                   photos_title="Boundary")
        )
        app_module._db = test_db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test-secret"
        with app_module.app.test_client() as c:
            yield c, p_inside, p_outside, p_boundary, test_db
        app_module._db = None


class TestLibraryBbox:
    def test_bbox_returns_only_inside_photos(self, client_geo):
        c, p_inside, p_outside, p_boundary, _ = client_geo
        r = c.get("/library?lat_min=42.35&lat_max=42.41&lon_min=-71.12&lon_max=-71.08")
        assert r.status_code == 200
        data = r.data.decode()
        assert "Inside" in data
        assert "Boundary" in data
        assert "Outside" not in data

    def test_bbox_boundary_inclusive(self, client_geo):
        c, _, _, p_boundary, db = client_geo
        count = db.library_photo_count(lat_min=42.35, lat_max=42.41,
                                        lon_min=-71.12, lon_max=-71.08)
        # p_inside + p_boundary = 2
        assert count == 2

    def test_bbox_partial_params_ignored(self, client_geo):
        c, _, _, _, db = client_geo
        # Only 3 of 4 params — no bbox applied, all 3 photos returned
        count = db.library_photo_count(lat_min=42.35, lat_max=42.41, lon_min=-71.12)
        assert count == 3

    def test_bbox_plus_time_pattern(self, client_geo):
        c, p_inside, p_outside, p_boundary, db = client_geo
        # inside box + October
        count = db.library_photo_count(lat_min=42.35, lat_max=42.41,
                                        lon_min=-71.12, lon_max=-71.08,
                                        time_pattern="month:10")
        assert count == 1  # only p_inside has date_taken in October

    def test_bbox_filter_count_shows_1(self, client_geo):
        c, *_ = client_geo
        r = c.get("/library?lat_min=42.35&lat_max=42.41&lon_min=-71.12&lon_max=-71.08")
        assert b"Filters (1)" in r.data

    def test_bbox_chip_shown_in_panel(self, client_geo):
        c, *_ = client_geo
        r = c.get("/library?lat_min=42.35&lat_max=42.41&lon_min=-71.12&lon_max=-71.08")
        assert b"Map area" in r.data

    def test_bbox_hidden_inputs_for_pagination(self, client_geo):
        c, *_ = client_geo
        r = c.get("/library?lat_min=42.35&lat_max=42.41&lon_min=-71.12&lon_max=-71.08")
        data = r.data.decode()
        assert 'name="lat_min"' in data
        assert 'name="lat_max"' in data
        assert 'name="lon_min"' in data
        assert 'name="lon_max"' in data

    def test_bbox_inverted_coords_still_finds_photos(self, client_geo):
        c, *_ = client_geo
        # lat_min > lat_max — app.py normalises before DB call
        r = c.get("/library?lat_min=42.41&lat_max=42.35&lon_min=-71.08&lon_max=-71.12")
        assert b"Map area" in r.data

    def test_bbox_out_of_range_clamped(self, client_geo):
        c, *_ = client_geo
        # Absurd values don't crash
        r = c.get("/library?lat_min=-999&lat_max=999&lon_min=-999&lon_max=999")
        assert r.status_code == 200
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_library_search.py::TestLibraryBbox -v
```

Expected: FAIL — `_library_where` does not accept `lat_min` etc.

- [ ] **Step 3: Add 4 params to `_library_where` signature**

In `db/db.py`, change the `_library_where` signature (line 876). Add after `person: str | None = None,  # #141 person filter`:

```python
    def _library_where(
        self,
        date_from: str | None,
        date_to: str | None,
        album_id: int | None,
        tag: str | None,
        status: str | None,
        untitled_only: bool,
        time_pattern: str | None = None,
        time_expand: int = 0,
        q: str | None = None,
        country: str | None = None,
        state: str | None = None,
        city: str | None = None,
        neighborhood: str | None = None,
        person: str | None = None,
        lat_min: float | None = None,   # #144 bbox
        lat_max: float | None = None,   # #144
        lon_min: float | None = None,   # #144
        lon_max: float | None = None,   # #144
    ) -> tuple[str, list]:
```

- [ ] **Step 4: Add bbox dispatch block in `_library_where`**

After the existing `#141` block (after line 947, before `where = "WHERE " + ...`), add:

```python
        # #144 — spatial bounding box
        if (lat_min is not None and lat_max is not None
                and lon_min is not None and lon_max is not None):
            from db.photo_filters import build_bbox_clause
            frag, frag_params = build_bbox_clause(lat_min, lat_max, lon_min, lon_max)
            clauses.append(frag)
            params.extend(frag_params)
```

- [ ] **Step 5: Update `library_photos` signature and caller (line 964)**

Add 4 new params to the method signature (after `person`):

```python
    def library_photos(
        self,
        date_from: str | None = None,
        date_to: str | None = None,
        album_id: int | None = None,
        tag: str | None = None,
        status: str | None = None,
        untitled_only: bool = False,
        time_pattern: str | None = None,
        time_expand: int = 0,
        q: str | None = None,
        country: str | None = None,
        state: str | None = None,
        city: str | None = None,
        neighborhood: str | None = None,
        person: str | None = None,
        lat_min: float | None = None,
        lat_max: float | None = None,
        lon_min: float | None = None,
        lon_max: float | None = None,
        limit: int = 120,
        offset: int = 0,
    ) -> list[dict]:
```

And add to the `_library_where` call inside `library_photos`:

```python
        where, params = self._library_where(
            date_from=date_from,
            date_to=date_to,
            album_id=album_id,
            tag=tag,
            status=status,
            untitled_only=untitled_only,
            time_pattern=time_pattern,
            time_expand=time_expand,
            q=q,
            country=country,
            state=state,
            city=city,
            neighborhood=neighborhood,
            person=person,
            lat_min=lat_min,
            lat_max=lat_max,
            lon_min=lon_min,
            lon_max=lon_max,
        )
```

- [ ] **Step 6: Update `library_photo_count` signature and caller (line 1022)**

Same pattern — add 4 new params after `person`:

```python
    def library_photo_count(
        self,
        date_from: str | None = None,
        date_to: str | None = None,
        album_id: int | None = None,
        tag: str | None = None,
        status: str | None = None,
        untitled_only: bool = False,
        time_pattern: str | None = None,
        time_expand: int = 0,
        q: str | None = None,
        country: str | None = None,
        state: str | None = None,
        city: str | None = None,
        neighborhood: str | None = None,
        person: str | None = None,
        lat_min: float | None = None,
        lat_max: float | None = None,
        lon_min: float | None = None,
        lon_max: float | None = None,
    ) -> int:
```

Add `lat_min=lat_min, lat_max=lat_max, lon_min=lon_min, lon_max=lon_max,` to its `_library_where` call.

- [ ] **Step 7: Update `library_photo_ids` signature and caller (line 1062)**

Same pattern — add 4 new params after `person`:

```python
    def library_photo_ids(
        self,
        date_from: str | None = None,
        date_to: str | None = None,
        album_id: int | None = None,
        tag: str | None = None,
        status: str | None = None,
        untitled_only: bool = False,
        time_pattern: str | None = None,
        time_expand: int = 0,
        q: str | None = None,
        country: str | None = None,
        state: str | None = None,
        city: str | None = None,
        neighborhood: str | None = None,
        person: str | None = None,
        lat_min: float | None = None,
        lat_max: float | None = None,
        lon_min: float | None = None,
        lon_max: float | None = None,
    ) -> list[int]:
```

Add `lat_min=lat_min, lat_max=lat_max, lon_min=lon_min, lon_max=lon_max,` to its `_library_where` call.

- [ ] **Step 8: Run tests to verify they pass**

```bash
python -m pytest tests/test_library_search.py::TestLibraryBbox -v
```

Expected: All bbox DB/route tests that touch db.py should now pass. Some may still fail if they test library.html output (chip, filter_count, hidden inputs) — those are fixed in Task 4.

- [ ] **Step 9: Run full suite**

```bash
python -m pytest tests/ -q
```

Expected: all existing tests pass; some new bbox tests may still fail (template tests — fixed in Task 4)

- [ ] **Step 10: Commit**

```bash
git add db/db.py tests/test_library_search.py
git commit -m "feat(#144): extend _library_where + 3 callers with bbox params

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 3: `app.py` — parse bbox, fix map-photos, bulk-edit passthrough

**Files:**
- Modify: `reviewer/app.py`
- Test: `tests/test_library_search.py` (route tests), `tests/test_map_routes.py`

Background: Three changes to app.py:
1. Module-level `_parse_float` helper (reused by library route and bulk-edit route)
2. Library route: parse + clamp + normalise bbox params, add to `filters` dict, pass to DB calls
3. `/api/map-photos`: add `AND p.flickr_deleted = 0` to WHERE clause
4. `/api/bulk-edit`: extract bbox from `_filter` dict, pass to `library_photo_ids`

- [ ] **Step 1: Write failing test for `/api/map-photos` flickr_deleted bug**

Add to `tests/test_map_routes.py`:

```python
@pytest.fixture()
def client_with_deleted():
    """One live geotagged photo and one deleted geotagged photo."""
    with tempfile.TemporaryDirectory() as tmp:
        test_db = Database(Path(tmp) / "test.db")
        live = test_db.upsert_photo(
            _photo(20, latitude=42.38, longitude=-71.10,
                   photos_title="Live photo")
        )
        deleted = test_db.upsert_photo(
            _photo(21, latitude=42.39, longitude=-71.09,
                   photos_title="Deleted photo", flickr_deleted=1)
        )
        app_module._db = test_db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test-secret"
        with app_module.app.test_client() as c:
            yield c, live, deleted
        app_module._db = None


class TestMapPhotosDeletedFilter:
    def test_deleted_photos_excluded_from_map(self, client_with_deleted):
        c, live, deleted = client_with_deleted
        r = c.get("/api/map-photos")
        assert r.status_code == 200
        data = r.get_json()
        ids = [p["id"] for p in data]
        assert live in ids
        assert deleted not in ids
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_map_routes.py::TestMapPhotosDeletedFilter -v
```

Expected: FAIL — deleted photo currently appears in map results

- [ ] **Step 3: Fix `/api/map-photos` — add `flickr_deleted = 0`**

In `reviewer/app.py`, find the `api_map_photos` function (line 794). Change the SQL WHERE clause from:

```python
f"WHERE p.latitude IS NOT NULL AND p.longitude IS NOT NULL{extra_where}",
```

to:

```python
f"WHERE p.latitude IS NOT NULL AND p.longitude IS NOT NULL"
f" AND p.flickr_deleted = 0{extra_where}",
```

- [ ] **Step 4: Run test to verify fix works**

```bash
python -m pytest tests/test_map_routes.py::TestMapPhotosDeletedFilter -v
```

Expected: PASS

- [ ] **Step 5: Add `_parse_float` module-level helper**

In `reviewer/app.py`, after the `truncate_tags` function (line 109), before the `# Routes — pages` comment, add:

```python
def _parse_float(v: str | None) -> float | None:
    """Parse a query-string value to float. Returns None on missing or non-numeric input."""
    try:
        return float(v) if v is not None else None
    except ValueError:
        return None
```

- [ ] **Step 6: Add bbox parsing to the library route**

In the `library` function (line 856), after the `date_alias` block (after line 880), add:

```python
    lat_min = _parse_float(request.args.get("lat_min"))
    lat_max = _parse_float(request.args.get("lat_max"))
    lon_min = _parse_float(request.args.get("lon_min"))
    lon_max = _parse_float(request.args.get("lon_max"))
    # Require all four; ignore a partial set
    if not all(v is not None for v in (lat_min, lat_max, lon_min, lon_max)):
        lat_min = lat_max = lon_min = lon_max = None
    else:
        # Clamp to valid geographic bounds
        lat_min = max(-90.0, min(90.0, lat_min))
        lat_max = max(-90.0, min(90.0, lat_max))
        lon_min = max(-180.0, min(180.0, lon_min))
        lon_max = max(-180.0, min(180.0, lon_max))
        # Normalise ordering — silently swap inverted values
        if lat_min > lat_max:
            lat_min, lat_max = lat_max, lat_min
        if lon_min > lon_max:
            lon_min, lon_max = lon_max, lon_min
```

- [ ] **Step 7: Add bbox to library route DB calls**

In the `library` function, add `lat_min=lat_min, lat_max=lat_max, lon_min=lon_min, lon_max=lon_max,` to all three DB calls:

`library_photos(...)`:
```python
    photos = db().library_photos(
        date_from=date_from,
        date_to=date_to,
        album_id=album_id,
        tag=tag,
        status=status,
        untitled_only=untitled_only,
        time_pattern=time_pattern,
        time_expand=time_expand,
        q=q,
        country=country,
        state=state,
        city=city,
        neighborhood=neighborhood,
        person=person,
        lat_min=lat_min,
        lat_max=lat_max,
        lon_min=lon_min,
        lon_max=lon_max,
        limit=per_page,
        offset=offset,
    )
```

`library_photo_count(...)`:
```python
    total = db().library_photo_count(
        date_from=date_from,
        date_to=date_to,
        album_id=album_id,
        tag=tag,
        status=status,
        untitled_only=untitled_only,
        time_pattern=time_pattern,
        time_expand=time_expand,
        q=q,
        country=country,
        state=state,
        city=city,
        neighborhood=neighborhood,
        person=person,
        lat_min=lat_min,
        lat_max=lat_max,
        lon_min=lon_min,
        lon_max=lon_max,
    )
```

- [ ] **Step 8: Add bbox to `filters` dict in library route**

In the `render_template` call, add to the `filters` dict (after `"person": person or ""`):

```python
            "lat_min": f"{lat_min:.5f}" if lat_min is not None else "",
            "lat_max": f"{lat_max:.5f}" if lat_max is not None else "",
            "lon_min": f"{lon_min:.5f}" if lon_min is not None else "",
            "lon_max": f"{lon_max:.5f}" if lon_max is not None else "",
```

- [ ] **Step 9: Add bbox passthrough to `/api/bulk-edit`**

In `api_bulk_edit` (line 1227), in the `if _filter is not None:` block, add bbox extraction after the existing `person` line (line 1284):

```python
        lat_min_f = _parse_float(_filter.get("lat_min"))
        lat_max_f = _parse_float(_filter.get("lat_max"))
        lon_min_f = _parse_float(_filter.get("lon_min"))
        lon_max_f = _parse_float(_filter.get("lon_max"))
        if not all(v is not None for v in (lat_min_f, lat_max_f, lon_min_f, lon_max_f)):
            lat_min_f = lat_max_f = lon_min_f = lon_max_f = None
```

And add to the `library_photo_ids` call:

```python
        photo_ids = db().library_photo_ids(
            date_from=_filter.get("date_from"),
            date_to=_filter.get("date_to"),
            album_id=_filter.get("album_id"),
            tag=_filter.get("tag"),
            status=_filter.get("status"),
            untitled_only=bool(_filter.get("untitled")),
            time_pattern=_filter.get("time_pattern") or None,
            time_expand=2 if _filter.get("expand") == "1" else 0,
            q=_filter.get("q") or None,
            country=_filter.get("country") or None,
            state=_filter.get("state") or None,
            city=_filter.get("city") or None,
            neighborhood=_filter.get("neighborhood") or None,
            person=_filter.get("person") or None,
            lat_min=lat_min_f,
            lat_max=lat_max_f,
            lon_min=lon_min_f,
            lon_max=lon_max_f,
        )
```

- [ ] **Step 10: Run the full bbox test suite**

```bash
python -m pytest tests/test_library_search.py::TestLibraryBbox tests/test_map_routes.py::TestMapPhotosDeletedFilter -v
```

Expected: map-photos test passes; most bbox route tests pass; template-dependent tests (chip, hidden inputs, filter_count) may still fail — those are fixed in Task 4.

- [ ] **Step 11: Run full suite**

```bash
python -m pytest tests/ -q
```

Expected: all existing tests pass

- [ ] **Step 12: Commit**

```bash
git add reviewer/app.py tests/test_map_routes.py
git commit -m "feat(#144): library route bbox parsing + bulk-edit passthrough; fix map-photos flickr_deleted

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 4: `library.html` — filter_count, hidden inputs, Map area chip, `_buildPayload`

**Files:**
- Modify: `reviewer/templates/library.html`

Background: Four additions to the template:
1. `filter_count` Jinja2 expression — add one term for bbox
2. Hidden `<input>` tags so bbox params survive form submission (pagination)
3. "Map area ✕" chip at top of filter panel — shown when bbox is active
4. `_buildPayload` JS — add bbox fields so bulk-edit includes the spatial filter

Important conventions in this file:
- `filter_count` is a Jinja2 `{% set %}` at line 217 — it's a sum of 0/1 terms
- `filters.lat_min` is a non-empty string (e.g. `"42.35000"`) when bbox is active, `""` when not — truthy check works
- The filter panel `<div>` auto-opens when `filter_count > 0` — bbox counts, so the panel opens automatically when arriving from the map

- [ ] **Step 1: Add bbox term to `filter_count`**

Find the `{% set filter_count = (` block (line 217). Add one line after the `(1 if filters.person else 0)` line:

```jinja2
{% set filter_count = (
  (1 if filters.date_from or filters.date_to else 0) +
  (1 if filters.album_id else 0) +
  (1 if filters.tag else 0) +
  (1 if filters.status else 0) +
  (1 if filters.untitled else 0) +
  (1 if filters.time_pattern else 0) +
  (1 if filters.country else 0) +
  (1 if filters.state else 0) +
  (1 if filters.city else 0) +
  (1 if filters.neighborhood else 0) +
  (1 if filters.person else 0) +
  (1 if filters.lat_min else 0)
) %}
```

- [ ] **Step 2: Add hidden inputs for bbox pagination round-trip**

Immediately after `<form id="lib-filter-form" method="get" action="{{ url_for('library') }}">` (line 230), add:

```html
{% if filters.lat_min %}
<input type="hidden" name="lat_min" value="{{ filters.lat_min }}">
<input type="hidden" name="lat_max" value="{{ filters.lat_max }}">
<input type="hidden" name="lon_min" value="{{ filters.lon_min }}">
<input type="hidden" name="lon_max" value="{{ filters.lon_max }}">
{% endif %}
```

- [ ] **Step 3: Add "Map area ✕" chip in filter panel**

Immediately after `<div id="lib-filter-panel" class="lib-filter-panel" ...>` (line 252) and the blank line after it, before the `<!-- Row 1 -->` comment, add:

```html
  {% if filters.lat_min %}
  <div class="lib-filter-row">
    <span style="font-size:12px;color:var(--muted)">📍 Map area</span>
    <a href="{{ url_for('library',
         q=filters.q or None,
         date_from=filters.date_from or None,
         date_to=filters.date_to or None,
         album_id=filters.album_id or None,
         tag=filters.tag or None,
         status=filters.status or None,
         untitled=filters.untitled or None,
         time_pattern=filters.time_pattern or None,
         expand=filters.expand or None,
         country=filters.country or None,
         state=filters.state or None,
         city=filters.city or None,
         neighborhood=filters.neighborhood or None,
         person=filters.person or None) }}"
       style="font-size:12px;color:var(--muted);text-decoration:none"
       title="Remove map area filter">✕</a>
  </div>
  {% endif %}
```

This link passes every current filter **except** the four bbox params, which are intentionally omitted so clicking ✕ removes the spatial filter while keeping everything else.

- [ ] **Step 4: Add bbox fields to `_buildPayload`**

Find `_buildPayload` in the JS block (line 765). Inside the `if (_selectAllFilter) {` block, after the `person` line, add:

```javascript
      lat_min: fd.get('lat_min') ? parseFloat(fd.get('lat_min')) : null,
      lat_max: fd.get('lat_max') ? parseFloat(fd.get('lat_max')) : null,
      lon_min: fd.get('lon_min') ? parseFloat(fd.get('lon_min')) : null,
      lon_max: fd.get('lon_max') ? parseFloat(fd.get('lon_max')) : null,
```

The full updated `payload.filter` block should look like:

```javascript
    payload.filter = {
      date_from:    fd.get('date_from') || null,
      date_to:      fd.get('date_to') || null,
      album_id:     fd.get('album_id') ? parseInt(fd.get('album_id')) : null,
      tag:          fd.get('tag') || null,
      status:       fd.get('status') || null,
      untitled:     fd.get('untitled') === '1',
      time_pattern: fd.get('time_pattern') || null,
      expand:       fd.get('expand') || null,
      q:            fd.get('q') || null,
      country:      fd.get('country') || null,
      state:        fd.get('state') || null,
      city:         fd.get('city') || null,
      neighborhood: fd.get('neighborhood') || null,
      person:       fd.get('person') || null,
      lat_min: fd.get('lat_min') ? parseFloat(fd.get('lat_min')) : null,
      lat_max: fd.get('lat_max') ? parseFloat(fd.get('lat_max')) : null,
      lon_min: fd.get('lon_min') ? parseFloat(fd.get('lon_min')) : null,
      lon_max: fd.get('lon_max') ? parseFloat(fd.get('lon_max')) : null,
    };
```

- [ ] **Step 5: Run the full bbox test suite**

```bash
python -m pytest tests/test_library_search.py::TestLibraryBbox -v
```

Expected: all 9 bbox tests pass

- [ ] **Step 6: Run full suite**

```bash
python -m pytest tests/ -q
```

Expected: all pass

- [ ] **Step 7: Run lint**

```bash
make lint
```

Expected: clean

- [ ] **Step 8: Commit**

```bash
git add reviewer/templates/library.html
git commit -m "feat(#144): library.html — bbox filter_count, hidden inputs, Map area chip, _buildPayload

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 5: `map.html` — Leaflet.draw, draw/clear/open-lib buttons, spatial selection JS

**Files:**
- Modify: `reviewer/templates/map.html`
- Test: `tests/test_map_routes.py`

Background: The map page gains Leaflet.draw (CDN) and three new buttons in the filter bar. All drawing is triggered programmatically via our custom buttons — the default Leaflet.draw toolbar is not added to the map, keeping the UI clean. `_drawnBounds` holds the active selection; `openInLibrary()` uses it if set, otherwise uses `map.getBounds()` (the current viewport).

`L.Draw.Rectangle` is used directly without `L.Control.Draw` being added to the map — this is intentional and supported.

- [ ] **Step 1: Write tests for map page UI additions**

Add to `tests/test_map_routes.py`:

```python
class TestMapPageSpatialUI:
    def test_draw_button_present(self, client_geo):
        c, *_ = client_geo
        html = c.get("/map").data.decode()
        assert "map-draw-btn" in html
        assert "Draw selection" in html

    def test_clear_button_present(self, client_geo):
        c, *_ = client_geo
        html = c.get("/map").data.decode()
        assert "map-clear-btn" in html
        assert "Clear selection" in html

    def test_open_library_button_present(self, client_geo):
        c, *_ = client_geo
        html = c.get("/map").data.decode()
        assert "map-open-lib-btn" in html
        assert "Open in Library" in html

    def test_leaflet_draw_script_loaded(self, client_geo):
        c, *_ = client_geo
        html = c.get("/map").data.decode()
        assert "leaflet-draw" in html

    def test_open_in_library_js_function_present(self, client_geo):
        c, *_ = client_geo
        html = c.get("/map").data.decode()
        assert "openInLibrary" in html
        assert "lat_min" in html
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_map_routes.py::TestMapPageSpatialUI -v
```

Expected: FAIL — buttons and scripts not present yet

- [ ] **Step 3: Add Leaflet.draw CSS to `{% block extra_head %}`**

In `reviewer/templates/map.html`, add to the `{% block extra_head %}` block (after the existing three CDN links, before `{% endblock %}`):

```html
<link rel="stylesheet" href="https://unpkg.com/leaflet-draw@1.0.4/dist/leaflet.draw.css" />
```

The full `{% block extra_head %}` becomes:

```html
{% block extra_head %}
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.css" />
<link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.Default.css" />
<link rel="stylesheet" href="https://unpkg.com/leaflet-draw@1.0.4/dist/leaflet.draw.css" />
{% endblock %}
```

- [ ] **Step 4: Add three buttons to the filter bar**

In `reviewer/templates/map.html`, find the `.map-filter-bar` div. After `<span id="map-photo-count" ...></span>` and before `</div>`, add:

```html
  <button type="button" id="map-draw-btn" onclick="toggleDraw()"
          style="margin-left:auto;background:var(--surface);border:1px solid var(--border);
                 color:var(--text);padding:4px 10px;border-radius:var(--radius);
                 font-size:12px;cursor:pointer">Draw selection</button>
  <button type="button" id="map-clear-btn" onclick="clearSelection()"
          style="display:none;background:var(--surface);border:1px solid var(--border);
                 color:var(--text);padding:4px 10px;border-radius:var(--radius);
                 font-size:12px;cursor:pointer">✕ Clear selection</button>
  <button type="button" id="map-open-lib-btn" onclick="openInLibrary()"
          style="background:var(--accent);border:1px solid var(--accent);
                 color:white;padding:4px 10px;border-radius:var(--radius);
                 font-size:12px;cursor:pointer">Open in Library ↗</button>
```

- [ ] **Step 5: Add Leaflet.draw JS script**

In `reviewer/templates/map.html`, add the Leaflet.draw script after the markercluster script and before the inline `<script>` block:

```html
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://unpkg.com/leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js"></script>
<script src="https://unpkg.com/leaflet-draw@1.0.4/dist/leaflet.draw.js"></script>
<script>
const map = L.map('map')...
```

- [ ] **Step 6: Add spatial selection JS**

In `reviewer/templates/map.html`, after the `reloadMarkers();   // initial load` line (before `</script>`), add:

```javascript
// ── Spatial selection ─────────────────────────────────────────────────────
const _drawnItems = new L.FeatureGroup();
map.addLayer(_drawnItems);

let _drawnLayer  = null;   // current rectangle layer, or null
let _drawnBounds = null;   // LatLngBounds of drawn rect, or null
let _drawHandler = null;   // active L.Draw.Rectangle handler, or null

function toggleDraw() {
  if (_drawHandler) {
    _drawHandler.disable();
    _drawHandler = null;
    return;
  }
  _drawHandler = new L.Draw.Rectangle(map, {
    shapeOptions: { color: '#0077cc', weight: 2, fillOpacity: 0.1 },
  });
  _drawHandler.enable();
}

map.on(L.Draw.Event.CREATED, function (e) {
  if (_drawnLayer) _drawnItems.removeLayer(_drawnLayer);
  _drawnLayer  = e.layer;
  _drawnBounds = _drawnLayer.getBounds();
  _drawnItems.addLayer(_drawnLayer);
  _drawHandler = null;
  document.getElementById('map-draw-btn').style.display  = 'none';
  document.getElementById('map-clear-btn').style.display = '';
});

function clearSelection() {
  if (_drawnLayer) _drawnItems.removeLayer(_drawnLayer);
  _drawnLayer  = null;
  _drawnBounds = null;
  document.getElementById('map-draw-btn').style.display  = '';
  document.getElementById('map-clear-btn').style.display = 'none';
}

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
  window.open('/library?' + params.toString(), '_blank');
}
```

- [ ] **Step 7: Run map UI tests**

```bash
python -m pytest tests/test_map_routes.py::TestMapPageSpatialUI -v
```

Expected: all 5 tests pass

- [ ] **Step 8: Run full suite + lint**

```bash
python -m pytest tests/ -q && make lint
```

Expected: all pass, lint clean

- [ ] **Step 9: Commit**

```bash
git add reviewer/templates/map.html tests/test_map_routes.py
git commit -m "feat(#144): map.html — Leaflet.draw, draw/clear/open-lib buttons, spatial selection JS

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 6: README, docs, close issue, push

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-05-26-map-spatial-filter-144.md`

- [ ] **Step 1: Update README test count and feature description**

In `README.md`, update the test count from `1331` to the new total (run `python -m pytest tests/ -q` to get the exact number).

Find the sentence starting `1331 tests covering` and append to its feature list (before `See [docs/testing.md]`):

```
, map spatial filter (draw-rectangle or viewport → open in Library as bbox filter with time pattern, "Map area ✕" chip, pagination-safe hidden inputs, bulk-edit passthrough)
```

Also update the count in `| \`tests/\` | Unit tests (1331 tests) |` to match.

- [ ] **Step 2: Mark spec done**

In `docs/superpowers/specs/2026-05-26-map-spatial-filter-144.md`, change:

```
**Status:** in progress
```

to:

```
**Status:** ✓ done
```

- [ ] **Step 3: Run full suite one final time**

```bash
python -m pytest tests/ -q
```

Expected: all pass

- [ ] **Step 4: Run lint**

```bash
make lint
```

Expected: clean

- [ ] **Step 5: Commit**

```bash
git add README.md docs/superpowers/specs/2026-05-26-map-spatial-filter-144.md
git commit -m "docs(#144): README + mark spec done

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

- [ ] **Step 6: Close GitHub issue with retrospective**

```bash
gh issue close 144 --comment "## Closed

Implemented map spatial filter per spec.

**What shipped (5 commits):**
- \`db/photo_filters.py\` — \`build_bbox_clause\`
- \`db/db.py\` — \`_library_where\` + 3 callers extended with bbox params
- \`reviewer/app.py\` — \`_parse_float\`, library route (clamp + normalise + pass), bulk-edit passthrough, \`/api/map-photos\` flickr_deleted fix
- \`reviewer/templates/library.html\` — filter_count, hidden inputs, Map area chip, _buildPayload
- \`reviewer/templates/map.html\` — Leaflet.draw CDN, draw/clear/open-lib buttons, spatial selection JS

**Retrospective:**
- Size estimate: M ✓
- Files: 8 | Plan tasks: 6
- No schema changes, no mutation semantics — pure filter addition"
```

- [ ] **Step 7: Bump version and push**

```bash
make bump
git push origin main
git push --tags
```
