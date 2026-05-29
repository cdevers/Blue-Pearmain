# Date Range Filter (#159) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `year_from`/`year_to` integer filter inputs with `date_from`/`date_to` native date-picker inputs across the shared filter bar, library, and map — enabling day-granularity date range filtering.

**Architecture:** `normalize_shared_filters()` becomes the single entry point for all date parsing — reading `date_from`/`date_to` directly and converting legacy `year_from`/`year_to` params for backward compat. Routes use `SharedFilters["date_from"]`/`["date_to"]` directly; no per-route year-conversion logic remains. The `_filter_bar.html` macro renders date pickers; `library.html` and `map.html` JS is updated to reference `[name=date_from]`/`[name=date_to]` throughout.

**Tech Stack:** Flask/Jinja2, SQLite (string comparison on ISO date columns), vanilla JS, `<input type="date">`

---

## File map

| File | Change |
|---|---|
| `reviewer/app.py` | Add `_safe_date()`, `timedelta` import, update `SharedFilters`, `normalize_shared_filters()`, `library()`, `api_map_photos()`, `map_view()`, add `format_date` Jinja filter |
| `reviewer/templates/_filter_bar.html` | Replace `year_from`/`year_to` number inputs with `date_from`/`date_to` date inputs |
| `reviewer/templates/library.html` | Remove Row-1 date inputs (moved to macro), update filter_count, map link, chip JS, event listeners, bulk-ops JS |
| `reviewer/templates/map.html` | Update all `year_from`/`year_to` JS references to `date_from`/`date_to` |
| `tests/test_unified_filter.py` | Update existing tests; add new `_safe_date`, normalization, library date, and integration tests |
| `tests/test_map_filter.py` | Update year-range tests to use date params |

---

## Task 1: `_safe_date()` + `SharedFilters` + `normalize_shared_filters()`

**Files:**
- Modify: `reviewer/app.py`
- Modify: `tests/test_unified_filter.py`

- [ ] **Step 1: Write failing tests for `_safe_date()` and new normalization**

Add to `tests/test_unified_filter.py` (insert after the existing `TestNormalizeSharedFilters` class):

```python
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
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python -m pytest tests/test_unified_filter.py::TestSafeDate tests/test_unified_filter.py::TestNormalizeSharedFiltersNew -v
```

Expected: FAIL — `_safe_date` not found, `date_from` not a key in SharedFilters.

- [ ] **Step 3: Update `SharedFilters` TypedDict and add `_safe_date()` + `timedelta` import**

In `reviewer/app.py`:

Change the import at line 30 from:
```python
from datetime import date as _date
```
to:
```python
from datetime import date as _date, timedelta
```

Replace `_safe_year()` and `SharedFilters` (lines 840–860) with:

```python
def _safe_year(key: str) -> int | None:
    """Parse a year from request.args[key]; kept for any callers outside normalize_shared_filters."""
    raw = request.args.get(key)
    if not raw:
        return None
    try:
        y = int(raw)
    except ValueError:
        return None
    return y if 1800 <= y <= 2099 else None


def _safe_date(key: str) -> str | None:
    """Parse a YYYY-MM-DD date string from request.args[key]; return None if missing/invalid."""
    val = (request.args.get(key) or "").strip()
    if not val:
        return None
    try:
        _date.fromisoformat(val)   # validates YYYY-MM-DD
        return val
    except ValueError:
        return None


class SharedFilters(TypedDict):
    time_pattern: str
    date_from: str | None   # YYYY-MM-DD or None
    date_to: str | None     # YYYY-MM-DD inclusive end, or None
    album_id: int | None
    person: str
    status: str
    expand: str
    tag: str  # "" when absent; whitespace-stripped
```

- [ ] **Step 4: Update `normalize_shared_filters()`**

Replace the body of `normalize_shared_filters()` (lines 869–902) with:

```python
def normalize_shared_filters() -> SharedFilters:
    """Parse and normalize the shared filter params from request.args.

    Single normalization entry point for both library() and map_view().
    Reads date_from/date_to directly; falls back to legacy year_from/year_to
    params (converting them to ISO date strings) when date params are absent.
    """
    # Primary: explicit date params
    date_from = _safe_date("date_from")
    date_to   = _safe_date("date_to")

    # Legacy compat: year_from / year_to → ISO date strings
    if date_from is None:
        y = _safe_year("year_from")
        if y is not None:
            date_from = f"{y:04d}-01-01"
    if date_to is None:
        y = _safe_year("year_to")
        if y is not None:
            date_to = f"{y:04d}-12-31"

    # Swap if inverted
    if date_from is not None and date_to is not None and date_from > date_to:
        date_from, date_to = date_to, date_from

    album_id: int | None = None
    raw_album = (request.args.get("album_id") or "").strip()
    if raw_album:
        try:
            album_id = int(raw_album)
        except ValueError:
            pass

    raw_status = (request.args.get("status") or "").strip()
    status = raw_status if raw_status in _VALID_STATUSES else ""

    return SharedFilters(
        time_pattern=(request.args.get("time_pattern") or "").strip(),
        date_from=date_from,
        date_to=date_to,
        album_id=album_id,
        person=(request.args.get("person") or "").strip(),
        status=status,
        expand=(request.args.get("expand") or "").strip(),
        tag=(request.args.get("tag") or "").strip(),
    )
```

- [ ] **Step 5: Update the existing normalization tests in `test_unified_filter.py`**

The old `TestNormalizeSharedFilters` class tests `year_from`/`year_to` keys that no longer exist. Replace the class with:

```python
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
```

- [ ] **Step 6: Run the new and updated tests**

```bash
python -m pytest tests/test_unified_filter.py::TestSafeDate tests/test_unified_filter.py::TestNormalizeSharedFiltersNew tests/test_unified_filter.py::TestNormalizeSharedFilters -v
```

Expected: All pass.

- [ ] **Step 7: Commit**

```bash
git add reviewer/app.py tests/test_unified_filter.py
git commit -m "feat(#159): _safe_date(), update SharedFilters + normalize_shared_filters()

Replace year_from/year_to int fields with date_from/date_to str fields in
SharedFilters TypedDict. _safe_date() validates YYYY-MM-DD strings.
normalize_shared_filters() reads date params directly and converts legacy
year_from/year_to params for backward compat.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 2: Library route — use SharedFilters date fields

**Files:**
- Modify: `reviewer/app.py`
- Modify: `tests/test_unified_filter.py`

The library route currently reads `date_from`/`date_to` from `request.args` separately (lines 1140–1141) and falls back to year conversion (lines 1155–1158). Replace all of this with the SharedFilters values.

**Background:** `db().library_photos()` uses `p.date_taken <= date_to` (inclusive). To make a user-selected `date_to = '2019-08-30'` include all photos taken on Aug 30, pass `date_to_db = '2019-08-31'` (next day). SQLite: `'2019-08-30T18:00:00' <= '2019-08-31'` → TRUE (included); `'2019-08-31T00:00:00' <= '2019-08-31'` → FALSE (excluded).

- [ ] **Step 1: Write failing library date-filter tests**

Add to `tests/test_unified_filter.py` (replace the existing `TestLibraryYearFilter` class):

```python
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
        assert p19 in ids   # taken on the boundary day → included
        assert p23 not in ids

    def test_date_to_excludes_next_day(self, client_lib_years):
        """Photo taken the day after date_to is excluded."""
        c, p16, p19, p23 = client_lib_years
        # p19 has date_taken="2019-12-20T10:00:00"; p23 has "2023-07-04T10:00:00"
        resp = c.get("/library?date_from=2019-12-21&date_to=2023-07-03")
        ids = _lib_ids(resp)
        assert p19 not in ids   # 2019-12-20 is before date_from
        assert p23 not in ids   # 2023-07-04 is after date_to

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
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python -m pytest tests/test_unified_filter.py::TestLibraryDateFilter -v
```

Expected: Most fail because the library route still reads `date_from`/`date_to` from request.args directly (not SharedFilters) and the `year_from`/`year_to` key no longer exists in SharedFilters.

- [ ] **Step 3: Update the library route in `reviewer/app.py`**

Find the `library()` function. Replace lines 1140–1158 (the old separate reading + year conversion) with:

```python
    sf = normalize_shared_filters()
    album_id = sf["album_id"]
    person: str | None = sf["person"] or None
    tag: str | None = sf["tag"] or None
    status: str | None = sf["status"] or None
    time_pattern: str | None = sf["time_pattern"] or None
    time_expand = 2 if sf["expand"] == "1" else 0

    # Use date_from/date_to from SharedFilters (handles legacy year params too)
    date_from: str | None = sf["date_from"] or None
    # date_to is the inclusive end the user selected (YYYY-MM-DD).
    # db._library_where() uses <=, so pass next-day for correct day-level inclusion.
    date_to_display: str | None = sf["date_to"] or None
    date_to: str | None = None
    if date_to_display:
        date_to = str(_date.fromisoformat(date_to_display) + timedelta(days=1))
```

Also remove the old `sf = normalize_shared_filters()` line that was at line 1146 and the year conversion block (lines 1155–1158). The new block above replaces both.

Then find and update the `date_alias` block (currently around line 1165) to use `date_to_display`:

```python
    date_alias = request.args.get("date") or None
    if date_alias:
        date_from = date_from or date_alias
        if date_to_display is None:
            date_to_display = date_alias
            date_to = date_alias + "T23:59:59"
```

In the `render_template` call at the end of `library()`, update the `filters` dict:
- Remove the `"year_from": ...` and `"year_to": ...` lines (they no longer exist in SharedFilters)
- Change `"date_from": date_from or ""` to stay as-is (correct)
- Change `"date_to": date_to or ""` to `"date_to": date_to_display or ""`

The full updated filters dict section:

```python
        filters={
            "date_from": date_from or "",
            "date_to": date_to_display or "",
            "album_id": album_id,
            "tag": tag or "",
            "status": status or "",
            "untitled": "1" if untitled_only else "",
            "no_location": "1" if no_location else "",
            "confirmed_none": "1" if confirmed_none else "",
            "time_pattern": time_pattern or "",
            "expand": "1" if time_expand > 0 else "",
            "q": q or "",
            "country": country or "",
            "state": state or "",
            "city": city or "",
            "neighborhood": neighborhood or "",
            "person": person or "",
            "lat_min": f"{lat_min:.5f}" if lat_min is not None else "",
            "lat_max": f"{lat_max:.5f}" if lat_max is not None else "",
            "lon_min": f"{lon_min:.5f}" if lon_min is not None else "",
            "lon_max": f"{lon_max:.5f}" if lon_max is not None else "",
        },
```

Note: `year_from` and `year_to` entries are removed.

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/test_unified_filter.py::TestLibraryDateFilter -v
```

Expected: All pass.

- [ ] **Step 5: Run full test suite to check for regressions**

```bash
python -m pytest tests/ -q
```

Expected: Pass (some existing tests that check for `year_from`/`year_to` in the filters dict or template will fail — these are updated in Task 7).

- [ ] **Step 6: Commit**

```bash
git add reviewer/app.py tests/test_unified_filter.py
git commit -m "feat(#159): library() route uses SharedFilters date_from/date_to

Remove separate request.args date reading and year→date conversion.
date_to_db computed as next-day for correct inclusive-end SQL boundary.
Remove year_from/year_to from filters template dict.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 3: `api_map_photos` — date-based SQL

**Files:**
- Modify: `reviewer/app.py`
- Modify: `tests/test_map_filter.py`

- [ ] **Step 1: Write failing map date-filter tests**

In `tests/test_map_filter.py`, find the existing `TestYearRange` class (or equivalent). Add a new class:

```python
class TestMapDateFilter:
    def test_date_from_excludes_earlier(self, client_years):
        c, p16, p19, p23 = client_years
        resp = c.get("/api/map-photos?date_from=2019-01-01")
        assert resp.status_code == 200
        ids = _ids(resp)
        assert p16 not in ids
        assert p19 in ids
        assert p23 in ids

    def test_date_to_excludes_later(self, client_years):
        c, p16, p19, p23 = client_years
        resp = c.get("/api/map-photos?date_to=2019-12-31")
        ids = _ids(resp)
        assert p16 in ids
        assert p19 in ids
        assert p23 not in ids

    def test_date_to_inclusive_boundary(self, client_years):
        """Photo taken on the boundary day is included."""
        c, p16, p19, p23 = client_years
        # p19 has date_taken="2019-12-20T10:00:00"
        resp = c.get("/api/map-photos?date_to=2019-12-20")
        ids = _ids(resp)
        assert p19 in ids
        assert p23 not in ids

    def test_legacy_year_from_still_works(self, client_years):
        c, p16, p19, p23 = client_years
        resp = c.get("/api/map-photos?year_from=2019")
        ids = _ids(resp)
        assert p16 not in ids
        assert p19 in ids
        assert p23 in ids

    def test_legacy_year_to_still_works(self, client_years):
        c, p16, p19, p23 = client_years
        resp = c.get("/api/map-photos?year_to=2019")
        ids = _ids(resp)
        assert p16 in ids
        assert p19 in ids
        assert p23 not in ids
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python -m pytest tests/test_map_filter.py::TestMapDateFilter -v
```

Expected: FAIL — `date_from`/`date_to` not wired in `api_map_photos`.

- [ ] **Step 3: Update `api_map_photos()` in `reviewer/app.py`**

Find the "Year range" SQL block in `api_map_photos()` (around line 1045):

```python
    # Year range — ISO string range predicates (index-friendly)
    if year_from is not None:
        where_frags.append("p.date_taken >= ?")
        where_params.append(f"{year_from:04d}-01-01")
    if year_to is not None:
        where_frags.append("p.date_taken < ?")
        where_params.append(f"{year_to + 1:04d}-01-01")
```

Replace with:

```python
    # Date range — from SharedFilters (handles legacy year params via normalization)
    if sf["date_from"]:
        where_frags.append("p.date_taken >= ?")
        where_params.append(sf["date_from"])
    if sf["date_to"]:
        exclusive_end = str(_date.fromisoformat(sf["date_to"]) + timedelta(days=1))
        where_frags.append("p.date_taken < ?")
        where_params.append(exclusive_end)
```

Also remove the local variable assignments near the top of `api_map_photos()`:
```python
    year_from = sf["year_from"]   # DELETE this line
    year_to = sf["year_to"]       # DELETE this line
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/test_map_filter.py::TestMapDateFilter -v
```

Expected: All pass.

- [ ] **Step 5: Commit**

```bash
git add reviewer/app.py tests/test_map_filter.py
git commit -m "feat(#159): api_map_photos() uses date_from/date_to from SharedFilters

Replace year_from/year_to SQL with date-based predicates.
Legacy year params still work via normalize_shared_filters() conversion.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 4: `map_view()` initial_filters

**Files:**
- Modify: `reviewer/app.py`
- Modify: `tests/test_unified_filter.py`

- [ ] **Step 1: Update the existing `TestMapViewInitialFilters` test**

In `tests/test_unified_filter.py`, replace `TestMapViewInitialFilters`:

```python
class TestMapViewInitialFilters:
    def test_map_view_passes_date_filters_to_template(self, client_map_view):
        c = client_map_view
        resp = c.get(
            "/map?time_pattern=month:08&date_from=2015-01-01&date_to=2019-12-31"
            "&person=Marcin&status=public"
        )
        assert resp.status_code == 200
        body = resp.data.decode()
        assert 'value="2015-01-01"' in body
        assert 'value="2019-12-31"' in body
        assert 'value="Marcin"' in body

    def test_map_view_legacy_year_params_converted(self, client_map_view):
        c = client_map_view
        resp = c.get("/map?year_from=2015&year_to=2019")
        assert resp.status_code == 200
        body = resp.data.decode()
        assert 'value="2015-01-01"' in body
        assert 'value="2019-12-31"' in body
```

- [ ] **Step 2: Run to confirm failure**

```bash
python -m pytest tests/test_unified_filter.py::TestMapViewInitialFilters -v
```

Expected: FAIL — `initial_filters` still has `year_from`/`year_to` keys.

- [ ] **Step 3: Update `map_view()` in `reviewer/app.py`**

Find the `initial_filters` dict in `map_view()` (around line 961). Replace:

```python
    initial_filters = {
        "time_pattern": sf["time_pattern"],
        "year_from": sf["year_from"] if sf["year_from"] is not None else "",
        "year_to": sf["year_to"] if sf["year_to"] is not None else "",
        "album_id": sf["album_id"],
        "person": sf["person"],
        "status": sf["status"],
        "expand": sf["expand"],
        "tag": sf["tag"],
    }
```

With:

```python
    initial_filters = {
        "time_pattern": sf["time_pattern"],
        "date_from": sf["date_from"] or "",
        "date_to": sf["date_to"] or "",
        "album_id": sf["album_id"],
        "person": sf["person"],
        "status": sf["status"],
        "expand": sf["expand"],
        "tag": sf["tag"],
    }
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/test_unified_filter.py::TestMapViewInitialFilters -v
```

Expected: Pass.

- [ ] **Step 5: Commit**

```bash
git add reviewer/app.py tests/test_unified_filter.py
git commit -m "feat(#159): map_view() initial_filters uses date_from/date_to

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 5: `format_date` Jinja filter

**Files:**
- Modify: `reviewer/app.py`
- Modify: `tests/test_unified_filter.py`

- [ ] **Step 1: Write a failing test**

Add to `tests/test_unified_filter.py`:

```python
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
```

- [ ] **Step 2: Run to confirm failure**

```bash
python -m pytest tests/test_unified_filter.py::TestFormatDateFilter -v
```

Expected: FAIL — `format_date` not in `jinja_env.filters`.

- [ ] **Step 3: Add the Jinja filter to `reviewer/app.py`**

Add after the `_VALID_STATUSES` definition (around line 866):

```python
@app.template_filter("format_date")
def _format_date_filter(s: str) -> str:
    """Format a YYYY-MM-DD string as 'Jun 15, 2018'."""
    try:
        return _date.fromisoformat(s).strftime("%b %-d, %Y")
    except (ValueError, AttributeError):
        return s
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/test_unified_filter.py::TestFormatDateFilter -v
```

Expected: Pass.

- [ ] **Step 5: Commit**

```bash
git add reviewer/app.py tests/test_unified_filter.py
git commit -m "feat(#159): add format_date Jinja filter (YYYY-MM-DD → 'Jun 15, 2018')

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 6: `_filter_bar.html` — replace year inputs with date inputs

**Files:**
- Modify: `reviewer/templates/_filter_bar.html`
- Modify: `tests/test_unified_filter.py`

- [ ] **Step 1: Update the template test**

In `tests/test_unified_filter.py`, update `TestLibraryTemplateIntegration.test_shared_macro_controls_in_library`:

```python
    def test_shared_macro_controls_in_library(self, client_template):
        c = client_template
        resp = c.get("/library")
        assert resp.status_code == 200
        body = resp.data.decode()
        assert 'name="time_pattern"' in body
        assert 'name="date_from"' in body    # was name="year_from"
        assert 'name="date_to"' in body      # was name="year_to"
        assert 'name="album_id"' in body
        assert 'name="person"' in body
        assert 'name="status"' in body
        # Old year inputs must be gone from the macro
        assert 'type="number"' not in body or 'name="year_from"' not in body
```

- [ ] **Step 2: Run to confirm failure**

```bash
python -m pytest "tests/test_unified_filter.py::TestLibraryTemplateIntegration::test_shared_macro_controls_in_library" -v
```

Expected: FAIL — `name="date_from"` not found (macro still has year inputs).

- [ ] **Step 3: Update `_filter_bar.html`**

Replace the `<span class="shared-year-range">` block:

```html
  <span class="shared-year-range">
    <label>Year
      <input type="number" name="year_from" min="1800" max="2099"
             value="{{ filters.year_from or '' }}" placeholder="from" style="width:62px">
    </label>
    <span style="padding:0 2px">–</span>
    <label>
      <input type="number" name="year_to" min="1800" max="2099"
             value="{{ filters.year_to or '' }}" placeholder="to" style="width:62px">
    </label>
  </span>
```

With:

```html
  <label>From
    <input type="date" name="date_from"
           value="{{ filters.date_from or '' }}"
           style="color-scheme:dark;width:140px">
  </label>
  <span style="padding:0 2px;align-self:flex-end;padding-bottom:6px">–</span>
  <label>To
    <input type="date" name="date_to"
           value="{{ filters.date_to or '' }}"
           style="color-scheme:dark;width:140px">
  </label>
```

Also update the docblock comment at the top of `_filter_bar.html` to reflect the new `filters` keys:

```jinja
{#
  ...
  Parameters:
    ...
    filters  — dict with keys: time_pattern, date_from, date_to,
               album_id (int|None), person, status, tag, expand
    ...
#}
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest "tests/test_unified_filter.py::TestLibraryTemplateIntegration::test_shared_macro_controls_in_library" -v
```

Expected: Pass.

- [ ] **Step 5: Commit**

```bash
git add reviewer/templates/_filter_bar.html tests/test_unified_filter.py
git commit -m "feat(#159): _filter_bar.html — date pickers replace year number inputs

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 7: `library.html` JS + template updates

**Files:**
- Modify: `reviewer/templates/library.html`
- Modify: `tests/test_unified_filter.py`

This task has several sub-changes. Do them all together in one commit.

- [ ] **Step 1: Update template integration tests**

Update `TestLibraryTemplateIntegration` in `tests/test_unified_filter.py`:

```python
    def test_library_has_view_on_map_link(self, client_template):
        c = client_template
        resp = c.get("/library?time_pattern=month:08&date_from=2015-01-01&person=Alice+W")
        body = resp.data.decode()
        assert "/map" in body
        assert "time_pattern=month%3A08" in body or "time_pattern=month:08" in body
        assert "date_from=2015-01-01" in body
        assert "Alice" in body

    def test_library_to_map_roundtrip_preserves_filters(self, client_template):
        """View-on-map link from library carries all shared filter params."""
        c = client_template
        resp = c.get(
            "/library?time_pattern=month:08&date_from=2015-01-01&date_to=2019-12-31"
            "&person=Alice+W&status=public"
        )
        assert resp.status_code == 200
        body = resp.data.decode()
        import re
        map_links = re.findall(r'href="(/map[^"]*)"', body)
        assert map_links, "No /map link found in library response"
        map_url = next((u for u in map_links if "time_pattern" in u), None)
        assert map_url is not None, "No /map link with filter params found"
        assert "date_from=2015-01-01" in map_url
        assert "date_to=2019-12-31" in map_url
        assert "Alice" in map_url
        assert "status=public" in map_url
```

- [ ] **Step 2: Run to confirm failure**

```bash
python -m pytest tests/test_unified_filter.py::TestLibraryTemplateIntegration -v
```

Expected: `test_library_has_view_on_map_link` and `test_library_to_map_roundtrip_preserves_filters` fail.

- [ ] **Step 3: Update `library.html` — filter_count**

Find the `filter_count` block (around line 262). Remove the `filters.year_from or filters.year_to` reference.

**Design note:** The date range counts as **one** dimension — `(1 if either date is set else 0)`. This matches the existing `year_from`/`year_to` behavior and is the authoritative spec choice. Do NOT change it to `(1 if date_from else 0) + (1 if date_to else 0)`.

```jinja
{# Before #}
{% set filter_count = (
  (1 if filters.date_from or filters.date_to
        or filters.year_from or filters.year_to else 0) +
  ...

{# After #}
{% set filter_count = (
  (1 if filters.date_from or filters.date_to else 0) +
  ...
```

- [ ] **Step 4: Update `library.html` — "View on map" link**

Find the `url_for('map_view', ...)` call (around line 298). Replace `year_from` and `year_to` with `date_from` and `date_to`:

```jinja
  <a href="{{ url_for('map_view',
    time_pattern=filters.time_pattern or None,
    date_from=filters.date_from or None,
    date_to=filters.date_to or None,
    album_id=filters.album_id or None,
    person=filters.person or None,
    status=filters.status or None,
    tag=filters.tag or None) }}"
     class="map-btn" style="font-size:12px;padding:4px 8px"
     title="View current filter on map">🗺 Map</a>
```

- [ ] **Step 5: Update `library.html` — Row 1 (remove date inputs)**

Find Row 1 (around line 351). It currently has:

```html
  <!-- Row 1: dates, tag, untitled, no location -->
  <div class="lib-filter-row">
    <label>From <input type="date" name="date_from" value="{{ filters.date_from }}"></label>
    <label>To <input type="date" name="date_to" value="{{ (filters.date_to or '')[:10] }}"></label>
    <label>Tag <input ...></label>
    ...
```

Remove the two date inputs (they now live in the shared macro). Update the comment:

```html
  <!-- Row 1: tag, untitled, no location, confirmed none -->
  <div class="lib-filter-row">
    <label>Tag <input type="text" name="tag" value="{{ filters.tag }}" placeholder="filter by tag…" style="width:120px"></label>
    ...
```

(Keep the Tag, Untitled, No location, and Confirmed none inputs; remove only the two date inputs.)

- [ ] **Step 6: Update `library.html` — JS event listeners**

Find the "Year inputs" listener block (around line 1189):

```javascript
// Year inputs: fire on blur or Enter only (avoids intermediate-state reloads)
for (const el of document.querySelectorAll('[name=year_from],[name=year_to]')) {
  el.addEventListener('blur', applyLibraryFilter);
  el.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); applyLibraryFilter(); } });
}
```

Replace with:

```javascript
// Date inputs: fire on change (calendar pick) or blur (keyboard entry)
for (const el of document.querySelectorAll('[name=date_from],[name=date_to]')) {
  el.addEventListener('change', applyLibraryFilter);
  el.addEventListener('blur', applyLibraryFilter);
}
```

- [ ] **Step 7: Update `library.html` — chip JS**

Find the chip-building JS block (around line 1215):

```javascript
  if (f.year_from && f.year_to) chips.push(`${f.year_from}–${f.year_to}`);
  else if (f.year_from) chips.push(`from ${f.year_from}`);
  else if (f.year_to)   chips.push(`to ${f.year_to}`);
  ...
  if (f.date_from || f.date_to) {
    chips.push(`${f.date_from || '…'} → ${f.date_to ? f.date_to.slice(0,10) : '…'}`);
  }
```

Replace both blocks with a single formatted date chip:

```javascript
  function _fmtDate(iso) {
    if (!iso) return '…';
    try {
      const d = new Date(iso + 'T12:00:00');  // noon avoids DST shifts
      return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
    } catch (e) { return iso; }
  }
  if (f.date_from && f.date_to) chips.push(`${_fmtDate(f.date_from)} – ${_fmtDate(f.date_to)}`);
  else if (f.date_from) chips.push(`from ${_fmtDate(f.date_from)}`);
  else if (f.date_to)   chips.push(`to ${_fmtDate(f.date_to)}`);
```

- [ ] **Step 8: Update `library.html` — bulk-ops JS**

Find the bulk-ops `year_from`/`year_to` conversion block (around line 817):

```javascript
    // Convert year_from/year_to → date_from/date_to for the bulk endpoint,
    // which uses library_photo_ids() and only understands ISO date strings.
    let _yearFrom = fd.get('year_from') ? parseInt(fd.get('year_from'), 10) : null;
    let _yearTo   = fd.get('year_to')   ? parseInt(fd.get('year_to'),   10) : null;
    if (_yearFrom !== null && _yearTo !== null && _yearFrom > _yearTo) {
      [_yearFrom, _yearTo] = [_yearTo, _yearFrom];
    }
    const _dateFrom = fd.get('date_from') ||
                      (_yearFrom !== null ? String(_yearFrom).padStart(4,'0') + '-01-01' : null);
    const _dateTo   = fd.get('date_to') ||
                      (_yearTo   !== null ? String(_yearTo + 1).padStart(4,'0') + '-01-01T00:00:00' : null);
```

Replace with:

```javascript
    // date_from/date_to come directly from the form (now in shared macro, not year fields)
    const _dateFrom = fd.get('date_from') || null;
    const _dateTo   = fd.get('date_to')   || null;
```

- [ ] **Step 9: Run tests**

```bash
python -m pytest tests/test_unified_filter.py::TestLibraryTemplateIntegration -v
```

Expected: All pass.

- [ ] **Step 10: Commit**

```bash
git add reviewer/templates/library.html tests/test_unified_filter.py
git commit -m "feat(#159): library.html — date chip, map link, remove Row-1 date inputs

Update filter_count, View-on-map link, JS event listeners, and chip display
to use date_from/date_to. Remove Row-1 date inputs (now in shared macro).
Simplify bulk-ops JS (no year→date conversion needed).

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 8: `map.html` JS updates

**Files:**
- Modify: `reviewer/templates/map.html`

All `[name=year_from]` / `[name=year_to]` references become `[name=date_from]` / `[name=date_to]`. There are four locations.

- [ ] **Step 1: Update `_hasAnyFilter()` (around line 193)**

```javascript
// Before
  if ((document.querySelector('[name=year_from]')?.value || '').trim()) return true;
  if ((document.querySelector('[name=year_to]')?.value || '').trim()) return true;

// After
  if ((document.querySelector('[name=date_from]')?.value || '').trim()) return true;
  if ((document.querySelector('[name=date_to]')?.value || '').trim()) return true;
```

- [ ] **Step 2: Update `_updateFilterChips()` (around line 212)**

```javascript
// Before
  const yf = (document.querySelector('[name=year_from]')?.value || '').trim();
  const yt = (document.querySelector('[name=year_to]')?.value || '').trim();
  if (yf && yt)     chips.push({ text: `${yf}–${yt}`, cls: 'map-chip' });
  else if (yf)      chips.push({ text: `from ${yf}`, cls: 'map-chip' });
  else if (yt)      chips.push({ text: `to ${yt}`, cls: 'map-chip' });

// After
  function _fmtMapDate(iso) {
    if (!iso) return '…';
    try {
      const d = new Date(iso + 'T12:00:00');
      return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
    } catch (e) { return iso; }
  }
  const df = (document.querySelector('[name=date_from]')?.value || '').trim();
  const dt = (document.querySelector('[name=date_to]')?.value || '').trim();
  if (df && dt)   chips.push({ text: `${_fmtMapDate(df)} – ${_fmtMapDate(dt)}`, cls: 'map-chip' });
  else if (df)    chips.push({ text: `from ${_fmtMapDate(df)}`, cls: 'map-chip' });
  else if (dt)    chips.push({ text: `to ${_fmtMapDate(dt)}`, cls: 'map-chip' });
```

- [ ] **Step 3: Update `buildMapUrl()` (around line 418)**

```javascript
// Before
  const yf = (document.querySelector('[name=year_from]')?.value || '').trim();
  const yt = (document.querySelector('[name=year_to]')?.value || '').trim();
  if (yf) params.set('year_from', yf);
  if (yt) params.set('year_to', yt);

// After
  const df = (document.querySelector('[name=date_from]')?.value || '').trim();
  const dt = (document.querySelector('[name=date_to]')?.value || '').trim();
  if (df) params.set('date_from', df);
  if (dt) params.set('date_to', dt);
```

- [ ] **Step 4: Update `_updateFilterBadge()` (around line 487)**

Date range = one dimension in the badge count (matches spec: `1 if either date set`).

```javascript
// Before
  if ((document.querySelector('[name=year_from]')?.value || '').trim()) n++;
  if ((document.querySelector('[name=year_to]')?.value || '').trim()) n++;

// After — date range counts as one dimension (not two)
  if ((document.querySelector('[name=date_from]')?.value || '').trim() ||
      (document.querySelector('[name=date_to]')?.value || '').trim()) n++;
```

- [ ] **Step 5: Update the year event listeners (around line 532)**

```javascript
// Before
// Year: blur + Enter only
for (const el of document.querySelectorAll('[name=year_from],[name=year_to]')) {
  el.addEventListener('blur', () => { reloadMarkers(); _updateFilterChips(); _updateFilterBadge(); });
  el.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); reloadMarkers(); _updateFilterChips(); _updateFilterBadge(); }
  });
}

// After
// Date inputs: fire on change (calendar pick) or blur (keyboard entry)
for (const el of document.querySelectorAll('[name=date_from],[name=date_to]')) {
  el.addEventListener('change', () => { reloadMarkers(); _updateFilterChips(); _updateFilterBadge(); });
  el.addEventListener('blur', () => { reloadMarkers(); _updateFilterChips(); _updateFilterBadge(); });
}
```

- [ ] **Step 6: Update `openInLibrary()` (around line 609)**

```javascript
// Before
  const yf = (document.querySelector('[name=year_from]')?.value || '').trim();
  const yt = (document.querySelector('[name=year_to]')?.value || '').trim();
  if (yf) params.set('year_from', yf);
  if (yt) params.set('year_to', yt);

// After
  const df = (document.querySelector('[name=date_from]')?.value || '').trim();
  const dt = (document.querySelector('[name=date_to]')?.value || '').trim();
  if (df) params.set('date_from', df);
  if (dt) params.set('date_to', dt);
```

- [ ] **Step 7: Run the full test suite**

```bash
python -m pytest tests/ -q
```

Expected: All pass.

- [ ] **Step 8: Commit**

```bash
git add reviewer/templates/map.html
git commit -m "feat(#159): map.html — update all JS year refs to date_from/date_to

Update _hasAnyFilter, _updateFilterChips, buildMapUrl, _updateFilterBadge,
event listeners, and openInLibrary to use date_from/date_to.
Date range counts as one filter dimension in the badge.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 9: Final verification

- [ ] **Step 1: Verify no stray `year_from`/`year_to` references remain**

```bash
git grep -nE 'year_from|year_to' reviewer/ tests/
```

Expected survivors — only these categories should remain; anything else is a missed update:
- `reviewer/app.py`: `_safe_year()` definition, calls inside `normalize_shared_filters()` for legacy conversion, and any inline comments explaining the backward-compat path
- `tests/test_unified_filter.py`: legacy-compat tests (`test_legacy_year_from_*`, `test_legacy_year_to_*`) and their URL strings (e.g. `?year_from=2019`)
- `tests/test_map_filter.py`: same legacy-compat tests

If any other file or line appears in the output (template files, JS, route code outside normalization, non-compat test assertions), fix it before proceeding.

- [ ] **Step 2: Run the full test suite**

```bash
python -m pytest tests/ -q
```

Expected: All tests pass. Note the total count.

- [ ] **Step 2: Update docs**

In `docs/future-directions.md`, mark #159 done:

```markdown
### Unified filter widget: date range ([#159](https://github.com/cdevers/Blue-Pearmain/issues/159)) `size:S` · ✓ done
```

Update `README.md` if there is a user-facing section about filtering — mention that the library and map now support day-granularity date range filters.

- [ ] **Step 3: Close the GitHub issue**

```bash
gh issue close 159 --repo cdevers/Blue-Pearmain \
  --comment "Implemented in this branch. day-granularity date_from/date_to replaces year_from/year_to across shared filter bar, library, and map. Legacy year params auto-converted for backward compat."
```

- [ ] **Step 4: Commit docs + bump version**

```bash
git add docs/future-directions.md README.md
git commit -m "docs(#159): mark done, update README

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

- [ ] **Step 5: Push**

```bash
git push origin main
```
