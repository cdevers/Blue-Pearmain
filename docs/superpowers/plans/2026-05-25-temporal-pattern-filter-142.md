# Temporal Pattern Filter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `time_pattern` filter to both the library grid and the map view, letting users narrow to any October, any Labor Day weekend, weekends only, etc. — across all calendar years at once.

**Architecture:** A new pure module `db/time_patterns.py` returns composable `(sql_fragment, params)` tuples; `_library_where` in `db/db.py` calls into it; both the library route and the map API route read `time_pattern` + `expand` query params. The library gains a "Time of year" dropdown in its filter bar; the map gains a filter bar above the map that re-fetches JSON on change (preserving zoom).

**Tech Stack:** Python `datetime`, SQLite `strftime()`, Jinja2, vanilla JS (no new libraries).

**Spec:** `docs/superpowers/specs/2026-05-25-temporal-pattern-filter-142.md`

**Dependency:** This issue ships before #141. When #141 ships, it will move the `time_pattern` control from the filter bar into the collapsible Filters panel.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `db/time_patterns.py` | Create | Pure functions: SEASONS, HOLIDAYS, `_nth_weekday`, `holiday_date`, `parse_pattern` |
| `db/db.py` | Modify | Add `_distinct_years()`; extend `_library_where`, `library_photos`, `library_photo_count`, `library_photo_ids` |
| `reviewer/app.py` | Modify | Library route + map route + bulk-edit route: read and apply `time_pattern`/`expand` |
| `reviewer/templates/library.html` | Modify | Add "Time of year" dropdown + expand checkbox; update `_buildPayload`; update Clear condition |
| `reviewer/templates/map.html` | Modify | Add filter bar; refactor to `plotPhotos` + `reloadMarkers`; adjust map height |
| `tests/test_time_patterns.py` | Create | Unit tests for the pure module |
| `tests/test_library_time_filter.py` | Create | Integration tests via Flask test client — library route |
| `tests/test_map_time_filter.py` | Create | Integration tests via Flask test client — map API |

---

## Task 1: `db/time_patterns.py` — pure temporal pattern module

**Files:**
- Create: `db/time_patterns.py`
- Create: `tests/test_time_patterns.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_time_patterns.py`:

```python
"""Unit tests for db/time_patterns.py — pure temporal pattern module."""
import datetime
import pytest
from db.time_patterns import SEASONS, HOLIDAYS, _nth_weekday, holiday_date, parse_pattern


# ---------------------------------------------------------------------------
# _nth_weekday
# ---------------------------------------------------------------------------

class TestNthWeekday:
    def test_first_monday_september_2023(self):
        # Labor Day 2023: first Monday of September = Sep 4
        assert _nth_weekday(2023, 9, 0, 1) == datetime.date(2023, 9, 4)

    def test_last_monday_may_2023(self):
        # Memorial Day 2023: last Monday of May = May 29
        # May 1 2023 is a Monday — edge case worth exercising
        assert _nth_weekday(2023, 5, 0, -1) == datetime.date(2023, 5, 29)

    def test_fourth_thursday_november_2023(self):
        # Thanksgiving 2023 = Nov 23
        assert _nth_weekday(2023, 11, 3, 4) == datetime.date(2023, 11, 23)

    def test_third_monday_january_2023(self):
        # MLK Day 2023 = Jan 16
        assert _nth_weekday(2023, 1, 0, 3) == datetime.date(2023, 1, 16)

    def test_second_monday_october_2023(self):
        # Columbus Day 2023 = Oct 9
        assert _nth_weekday(2023, 10, 0, 2) == datetime.date(2023, 10, 9)


# ---------------------------------------------------------------------------
# holiday_date
# ---------------------------------------------------------------------------

class TestHolidayDate:
    def test_thanksgiving_2023(self):
        assert holiday_date(2023, "thanksgiving") == datetime.date(2023, 11, 23)

    def test_labor_day_2023(self):
        assert holiday_date(2023, "labor_day") == datetime.date(2023, 9, 4)

    def test_memorial_day_2023(self):
        assert holiday_date(2023, "memorial_day") == datetime.date(2023, 5, 29)

    def test_mlk_day_2023(self):
        assert holiday_date(2023, "mlk_day") == datetime.date(2023, 1, 16)

    def test_christmas_fixed(self):
        assert holiday_date(2023, "christmas") == datetime.date(2023, 12, 25)

    def test_new_years_fixed(self):
        assert holiday_date(2024, "new_years") == datetime.date(2024, 1, 1)

    def test_unknown_key_returns_none(self):
        assert holiday_date(2023, "easter") is None
        assert holiday_date(2023, "") is None


# ---------------------------------------------------------------------------
# parse_pattern
# ---------------------------------------------------------------------------

class TestParsePattern:
    # Month
    def test_month_october(self):
        sql, params = parse_pattern("month:10", 0, [])
        assert "strftime('%m'" in sql
        assert params == ["10"]

    def test_month_january_zero_padded(self):
        sql, params = parse_pattern("month:01", 0, [])
        assert params == ["01"]

    # Season
    def test_season_fall(self):
        sql, params = parse_pattern("season:fall", 0, [])
        assert set(params) == {"09", "10", "11", "12"}
        assert len(params) == 4

    def test_season_winter_includes_march(self):
        sql, params = parse_pattern("season:winter", 0, [])
        assert "03" in params   # overlaps with spring — intentional
        assert "12" in params

    def test_season_spring_includes_june(self):
        sql, params = parse_pattern("season:spring", 0, [])
        assert "06" in params   # overlaps with summer — intentional
        assert "03" in params

    def test_season_summer(self):
        sql, params = parse_pattern("season:summer", 0, [])
        assert set(params) == {"06", "07", "08", "09"}

    def test_season_unknown_key(self):
        sql, params = parse_pattern("season:monsoon", 0, [])
        assert sql == "1=1"
        assert params == []

    # Day type
    def test_daytype_weekend(self):
        sql, params = parse_pattern("daytype:weekend", 0, [])
        assert "NOT IN" not in sql
        assert set(params) == {"0", "6"}

    def test_daytype_weekday(self):
        sql, params = parse_pattern("daytype:weekday", 0, [])
        assert "NOT IN" in sql
        assert set(params) == {"0", "6"}

    # Holidays — no expansion
    def test_holiday_exact_thanksgiving_2023(self):
        sql, params = parse_pattern("holiday:thanksgiving", 0, [2023])
        assert "2023-11-23" in params
        assert "BETWEEN" not in sql

    def test_holiday_exact_two_years(self):
        sql, params = parse_pattern("holiday:thanksgiving", 0, [2022, 2023])
        assert "2022-11-24" in params  # Thanksgiving 2022 = Nov 24
        assert "2023-11-23" in params

    # Holidays — with expansion
    def test_holiday_expand_thanksgiving_2023(self):
        sql, params = parse_pattern("holiday:thanksgiving", 2, [2023])
        # ±2 from Nov 23 → Nov 21 to Nov 25
        assert "2023-11-21" in params
        assert "2023-11-25" in params
        assert "BETWEEN" in sql

    def test_holiday_expand_two_years(self):
        sql, params = parse_pattern("holiday:thanksgiving", 2, [2022, 2023])
        assert sql.count("BETWEEN") == 2

    def test_holiday_unknown_key_empty_years(self):
        sql, params = parse_pattern("holiday:unknown_key", 0, [2023])
        assert sql == "1=1"
        assert params == []

    def test_holiday_no_years(self):
        sql, params = parse_pattern("holiday:thanksgiving", 2, [])
        assert sql == "1=1"
        assert params == []

    # Fallbacks
    def test_empty_pattern(self):
        sql, params = parse_pattern("", 0, [])
        assert sql == "1=1"
        assert params == []

    def test_unknown_prefix(self):
        sql, params = parse_pattern("unknown:xyz", 0, [2023])
        assert sql == "1=1"
        assert params == []
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_time_patterns.py -v 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'db.time_patterns'`

- [ ] **Step 3: Create `db/time_patterns.py`**

```python
"""
Temporal pattern filter — pure functions returning SQLite WHERE clause fragments.
No Flask or DB dependencies.

Usage:
    frag, params = parse_pattern("month:10", expand_days=0, years=[])
    frag, params = parse_pattern("holiday:thanksgiving", expand_days=2, years=[2022, 2023])

All generated fragments reference the column alias p.date_taken (photos p).
Unknown or empty patterns return ("1=1", []) — a safe no-op clause.
"""

from __future__ import annotations

import datetime
from typing import Optional

# Fuzzy, overlapping season ranges — intentionally human-oriented.
# A photo from March is plausibly "winter" or "spring" depending on the person's
# frame of reference, so both seasons include it.
SEASONS: dict[str, list[str]] = {
    "spring": ["03", "04", "05", "06"],   # Mar–Jun
    "summer": ["06", "07", "08", "09"],   # Jun–Sep
    "fall":   ["09", "10", "11", "12"],   # Sep–Dec
    "winter": ["12", "01", "02", "03"],   # Dec–Mar
}

# Holiday definitions.
# ("fixed",       month, day)
# ("nth_weekday", month, weekday, n)
#   weekday: Mon=0 … Sun=6  (Python datetime.weekday() convention)
#   n: positive = nth from start, -1 = last
HOLIDAYS: dict[str, tuple] = {
    "new_years":       ("fixed",       1,  1),
    "mlk_day":         ("nth_weekday", 1,  0,  3),   # 3rd Mon Jan
    "presidents_day":  ("nth_weekday", 2,  0,  3),   # 3rd Mon Feb
    "memorial_day":    ("nth_weekday", 5,  0, -1),   # last Mon May
    "july_4th":        ("fixed",       7,  4),
    "labor_day":       ("nth_weekday", 9,  0,  1),   # 1st Mon Sep
    "columbus_day":    ("nth_weekday", 10, 0,  2),   # 2nd Mon Oct
    "halloween":       ("fixed",       10, 31),
    "thanksgiving":    ("nth_weekday", 11, 3,  4),   # 4th Thu Nov
    "christmas":       ("fixed",       12, 25),
}


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> datetime.date:
    """
    Return the nth occurrence of weekday in (year, month).
    weekday: Mon=0 … Sun=6 (Python datetime.weekday() convention).
    n=1 → first occurrence, n=-1 → last occurrence.
    """
    if n > 0:
        first = datetime.date(year, month, 1)
        days_ahead = (weekday - first.weekday()) % 7
        first_occurrence = first + datetime.timedelta(days=days_ahead)
        return first_occurrence + datetime.timedelta(weeks=n - 1)
    else:
        # last occurrence: start from the last day of the month and walk back
        if month == 12:
            last = datetime.date(year + 1, 1, 1) - datetime.timedelta(days=1)
        else:
            last = datetime.date(year, month + 1, 1) - datetime.timedelta(days=1)
        days_back = (last.weekday() - weekday) % 7
        return last - datetime.timedelta(days=days_back)


def holiday_date(year: int, key: str) -> Optional[datetime.date]:
    """Return the date of the named holiday in the given year, or None if unknown."""
    defn = HOLIDAYS.get(key)
    if defn is None:
        return None
    if defn[0] == "fixed":
        _, month, day = defn
        return datetime.date(year, month, day)
    if defn[0] == "nth_weekday":
        _, month, weekday, n = defn
        return _nth_weekday(year, month, weekday, n)
    return None


def parse_pattern(
    pattern: str,
    expand_days: int,
    years: list[int],
) -> tuple[str, list]:
    """
    Return (sql_fragment, params) to append to a WHERE clause.
    The fragment references column alias 'p.date_taken'.
    Unknown or empty pattern returns ("1=1", []) — a safe no-op.

    Args:
        pattern:     Colon-separated type:key, e.g. "month:10", "holiday:thanksgiving".
        expand_days: For holiday patterns, expand the window by ±this many calendar days.
                     0 means exact day only. Fixed at 2 in v1 UI.
        years:       List of calendar years to compute holiday dates for. Obtained by
                     querying DISTINCT strftime('%Y', date_taken) from the photos table.
                     Ignored for non-holiday patterns.
    """
    if not pattern:
        return "1=1", []

    if pattern.startswith("month:"):
        month = pattern[6:]
        return "strftime('%m', p.date_taken) = ?", [month]

    if pattern.startswith("season:"):
        key = pattern[7:]
        months = SEASONS.get(key)
        if not months:
            return "1=1", []
        placeholders = ",".join("?" * len(months))
        return f"strftime('%m', p.date_taken) IN ({placeholders})", list(months)

    if pattern.startswith("daytype:"):
        key = pattern[8:]
        # SQLite strftime('%w', ...) → '0'=Sunday, '6'=Saturday
        if key == "weekend":
            return "strftime('%w', p.date_taken) IN (?,?)", ["0", "6"]
        if key == "weekday":
            return "strftime('%w', p.date_taken) NOT IN (?,?)", ["0", "6"]
        return "1=1", []

    if pattern.startswith("holiday:"):
        key = pattern[8:]
        ranges: list[tuple[str, str]] = []
        for year in years:
            d = holiday_date(year, key)
            if d is None:
                continue
            if expand_days == 0:
                ranges.append((str(d), str(d)))
            else:
                lo = d - datetime.timedelta(days=expand_days)
                hi = d + datetime.timedelta(days=expand_days)
                ranges.append((str(lo), str(hi)))
        if not ranges:
            return "1=1", []
        clauses = " OR ".join("(p.date_taken BETWEEN ? AND ?)" for _ in ranges)
        params: list = []
        for lo, hi in ranges:
            params.extend([lo, hi])
        return f"({clauses})", params

    return "1=1", []
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_time_patterns.py -v
```

Expected: all tests PASS. Verify Thanksgiving 2022 = Nov 24 if the two-year test fails — check `_nth_weekday(2022, 11, 3, 4)`: Nov 1 2022 = Tuesday (weekday 1); days_ahead = (3-1)%7 = 2; first Thu = Nov 3; 4th = Nov 3 + 21 = Nov 24. ✓

- [ ] **Step 5: Commit**

```bash
git add db/time_patterns.py tests/test_time_patterns.py
git commit -m "feat(#142): db/time_patterns.py — pure temporal pattern module"
```

---

## Task 2: DB + library route + integration tests

**Files:**
- Modify: `db/db.py` — lines ~876–993 (`_library_where`, `library_photos`, `library_photo_count`, `library_photo_ids`)
- Modify: `reviewer/app.py` — lines ~828–882 (library route), ~1199–1208 (bulk-edit filter)
- Create: `tests/test_library_time_filter.py`

- [ ] **Step 1: Write the failing integration tests**

Create `tests/test_library_time_filter.py`:

```python
"""Integration tests for time_pattern filter in the library route."""
import tempfile
import pytest
from pathlib import Path
from db.db import Database
import reviewer.app as app_module


def _photo(i: int, **kwargs) -> dict:
    base: dict = {
        "uuid": f"tp-u{i}",
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
def client_tp():
    """
    Fixture with 8 photos covering different months, seasons, weekdays/weekends, holidays.
    All dates verified against Python calendar for 2023:

    Photo 1 — Oct 16 2023 (Monday, fall)
    Photo 2 — Mar 20 2023 (Monday, spring AND winter overlap)
    Photo 3 — Jul  4 2023 (Tuesday, summer)
    Photo 4 — Sep 16 2023 (Saturday, fall/summer overlap)
    Photo 5 — Nov 23 2023 (Thursday, Thanksgiving 2023, fall)
    Photo 6 — Nov 25 2023 (Saturday, within ±2 of Thanksgiving, fall)
    Photo 7 — Nov 20 2023 (Monday, outside ±2 of Thanksgiving, fall)
    Photo 8 — Dec 25 2023 (Monday, Christmas, winter/fall overlap)
    """
    with tempfile.TemporaryDirectory() as tmp:
        test_db = Database(Path(tmp) / "test.db")
        p = [
            test_db.upsert_photo(_photo(1, date_taken="2023-10-16T12:00:00")),
            test_db.upsert_photo(_photo(2, date_taken="2023-03-20T12:00:00")),
            test_db.upsert_photo(_photo(3, date_taken="2023-07-04T12:00:00")),
            test_db.upsert_photo(_photo(4, date_taken="2023-09-16T12:00:00")),
            test_db.upsert_photo(_photo(5, date_taken="2023-11-23T12:00:00")),
            test_db.upsert_photo(_photo(6, date_taken="2023-11-25T12:00:00")),
            test_db.upsert_photo(_photo(7, date_taken="2023-11-20T12:00:00")),
            test_db.upsert_photo(_photo(8, date_taken="2023-12-25T12:00:00")),
        ]
        app_module._db = test_db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test-secret"
        with app_module.app.test_client() as c:
            yield c, p, test_db
        app_module._db = None


def _ids(resp) -> set[int]:
    """Extract photo IDs from library HTML response."""
    import re
    return {int(m) for m in re.findall(r'data-id="(\d+)"', resp.data.decode())}


class TestMonthFilter:
    def test_october_only(self, client_tp):
        c, p, _ = client_tp
        r = c.get("/library?time_pattern=month:10")
        assert r.status_code == 200
        ids = _ids(r)
        assert p[0] in ids          # Oct 16
        assert p[1] not in ids      # Mar
        assert p[2] not in ids      # Jul

    def test_november_includes_all_november_photos(self, client_tp):
        c, p, _ = client_tp
        r = c.get("/library?time_pattern=month:11")
        ids = _ids(r)
        assert {p[4], p[5], p[6]} <= ids   # Nov 23, Nov 25, Nov 20
        assert p[0] not in ids              # Oct


class TestSeasonFilter:
    def test_fall_includes_sep_oct_nov_dec(self, client_tp):
        c, p, _ = client_tp
        r = c.get("/library?time_pattern=season:fall")
        ids = _ids(r)
        # Sep(4), Oct(1), Nov(5,6,7), Dec(8) all in fall
        assert {p[0], p[3], p[4], p[5], p[6], p[7]} <= ids
        assert p[1] not in ids   # Mar — not in fall
        assert p[2] not in ids   # Jul — not in fall

    def test_spring_includes_march(self, client_tp):
        c, p, _ = client_tp
        r = c.get("/library?time_pattern=season:spring")
        ids = _ids(r)
        assert p[1] in ids   # Mar 20 ∈ spring (Mar–Jun)
        assert p[7] not in ids  # Dec — not in spring

    def test_winter_includes_march_overlap(self, client_tp):
        c, p, _ = client_tp
        r = c.get("/library?time_pattern=season:winter")
        ids = _ids(r)
        assert p[1] in ids   # Mar 20 ∈ winter (Dec–Mar) — intentional overlap
        assert p[7] in ids   # Dec 25 ∈ winter

    def test_summer(self, client_tp):
        c, p, _ = client_tp
        r = c.get("/library?time_pattern=season:summer")
        ids = _ids(r)
        assert p[2] in ids   # Jul 4 ∈ summer
        assert p[3] in ids   # Sep 16 ∈ summer (Jun–Sep includes Sep)
        assert p[0] not in ids  # Oct — not in summer


class TestDayTypeFilter:
    def test_weekends(self, client_tp):
        c, p, _ = client_tp
        r = c.get("/library?time_pattern=daytype:weekend")
        ids = _ids(r)
        assert p[3] in ids   # Sep 16 = Saturday
        assert p[5] in ids   # Nov 25 = Saturday
        assert p[0] not in ids  # Oct 16 = Monday

    def test_weekdays(self, client_tp):
        c, p, _ = client_tp
        r = c.get("/library?time_pattern=daytype:weekday")
        ids = _ids(r)
        assert p[0] in ids   # Oct 16 = Monday
        assert p[4] in ids   # Nov 23 = Thursday (Thanksgiving)
        assert p[3] not in ids  # Sep 16 = Saturday


class TestHolidayFilter:
    def test_thanksgiving_exact(self, client_tp):
        c, p, _ = client_tp
        r = c.get("/library?time_pattern=holiday:thanksgiving")
        ids = _ids(r)
        assert p[4] in ids   # Nov 23 = Thanksgiving 2023
        assert p[5] not in ids  # Nov 25 = 2 days after, not included without expand
        assert p[6] not in ids  # Nov 20 = 3 days before

    def test_thanksgiving_expand(self, client_tp):
        c, p, _ = client_tp
        r = c.get("/library?time_pattern=holiday:thanksgiving&expand=1")
        ids = _ids(r)
        assert p[4] in ids   # Nov 23 = Thanksgiving
        assert p[5] in ids   # Nov 25 = within ±2 days (Nov 21–25)
        assert p[6] not in ids  # Nov 20 = 3 days before = outside window

    def test_christmas_exact(self, client_tp):
        c, p, _ = client_tp
        r = c.get("/library?time_pattern=holiday:christmas")
        ids = _ids(r)
        assert p[7] in ids   # Dec 25
        assert p[4] not in ids  # Nov 23


class TestEdgeCases:
    def test_unknown_pattern_returns_all(self, client_tp):
        c, p, _ = client_tp
        r = c.get("/library?time_pattern=unknown:xyz")
        assert r.status_code == 200
        ids = _ids(r)
        assert len(ids) == 8   # all photos returned

    def test_empty_pattern_returns_all(self, client_tp):
        c, p, _ = client_tp
        r = c.get("/library?time_pattern=")
        assert r.status_code == 200
        ids = _ids(r)
        assert len(ids) == 8

    def test_time_pattern_and_combined_with_other_filters(self, client_tp):
        c, p, _ = client_tp
        # Fall AND untitled_only — all photos are untitled in fixture, so same as fall
        r = c.get("/library?time_pattern=season:fall&untitled=1")
        assert r.status_code == 200
        ids = _ids(r)
        assert p[0] in ids   # Oct 16 ∈ fall
        assert p[2] not in ids  # Jul — not in fall
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_library_time_filter.py -v 2>&1 | head -20
```

Expected: FAIL — `library_photos` does not accept `time_pattern` kwarg yet.

- [ ] **Step 3: Extend `_library_where` in `db/db.py`**

The method currently starts at line ~876. Add two new params and the time_pattern clause. The full updated method:

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
    time_expand: int = 2,
) -> tuple[str, list]:
    """Return (WHERE clause fragment, params list) for library queries."""
    clauses: list[str] = ["p.flickr_deleted = 0"]
    params: list = []

    if date_from:
        clauses.append("p.date_taken >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("p.date_taken <= ?")
        params.append(date_to)
    if status and status in self._STATUS_STATES:
        states = self._STATUS_STATES[status]
        placeholders = ",".join("?" * len(states))
        clauses.append(f"p.privacy_state IN ({placeholders})")
        params.extend(states)
    if untitled_only:
        clauses.append(
            "(p.flickr_title IS NULL OR p.flickr_title = '') "
            "AND (p.photos_title IS NULL OR p.photos_title = '')"
        )
    if tag:
        clauses.append(
            "(EXISTS (SELECT 1 FROM json_each(p.flickr_tags) WHERE value = ?) "
            "OR EXISTS (SELECT 1 FROM json_each(p.photos_tags) WHERE value = ?))"
        )
        params.extend([tag, tag])
    if time_pattern:
        from db.time_patterns import parse_pattern
        frag, frag_params = parse_pattern(time_pattern, time_expand, self._distinct_years())
        if frag != "1=1":
            clauses.append(frag)
            params.extend(frag_params)

    where = "WHERE " + " AND ".join(clauses)

    if album_id is not None:
        return where + " AND pa.album_id = ? AND pa.removed_at IS NULL", params + [album_id]

    return where, params
```

Also add `_distinct_years` after `_library_where`:

```python
def _distinct_years(self) -> list[int]:
    """Return all distinct calendar years present in photos.date_taken, sorted ascending."""
    rows = self.conn.execute(
        "SELECT DISTINCT CAST(strftime('%Y', date_taken) AS INTEGER) AS y "
        "FROM photos WHERE date_taken IS NOT NULL ORDER BY y"
    ).fetchall()
    return [r["y"] for r in rows if r["y"] is not None]
```

- [ ] **Step 4: Add `time_pattern`/`time_expand` kwargs to `library_photos`, `library_photo_count`, `library_photo_ids`**

In `library_photos` (line ~919), add `time_pattern: str | None = None, time_expand: int = 2` to the signature and pass through to `_library_where`:

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
    time_expand: int = 2,
    limit: int = 120,
    offset: int = 0,
) -> list[dict]:
    """Return photos for the library grid, newest first, with filters applied."""
    where, params = self._library_where(
        date_from, date_to, album_id, tag, status, untitled_only,
        time_pattern, time_expand,
    )
    # ... rest unchanged
```

Same change for `library_photo_count` (line ~956):

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
    time_expand: int = 2,
) -> int:
    """Return total photo count for the given library filters."""
    where, params = self._library_where(
        date_from, date_to, album_id, tag, status, untitled_only,
        time_pattern, time_expand,
    )
    # ... rest unchanged
```

Same change for `library_photo_ids` (line ~975):

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
    time_expand: int = 2,
) -> list[int]:
    """Return all photo IDs matching the filters (no limit — used by bulk-edit)."""
    where, params = self._library_where(
        date_from, date_to, album_id, tag, status, untitled_only,
        time_pattern, time_expand,
    )
    # ... rest unchanged
```

- [ ] **Step 5: Update the library route in `reviewer/app.py`**

In `library()` (line ~828), add after `untitled_only = ...`:

```python
    time_pattern = request.args.get("time_pattern") or None
    time_expand  = 2 if request.args.get("expand") == "1" else 0
```

Add to `db().library_photos(...)` call:

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
        limit=per_page,
        offset=offset,
    )
```

Add to `db().library_photo_count(...)` call:

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
    )
```

Add to the `filters` dict:

```python
        filters={
            "date_from": date_from or "",
            "date_to": date_to or "",
            "album_id": album_id,
            "tag": tag or "",
            "status": status or "",
            "untitled": untitled_only,
            "time_pattern": time_pattern or "",
            "expand": "1" if time_expand > 0 else "",
        },
```

- [ ] **Step 6: Update the bulk-edit route in `reviewer/app.py`**

In `api_bulk_edit()` (line ~1201), update the `library_photo_ids` call to pass `time_pattern` and `time_expand` from the filter dict:

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
        )
```

- [ ] **Step 7: Run full test suite to verify**

```bash
python -m pytest tests/test_library_time_filter.py tests/test_time_patterns.py -v
```

Expected: all tests in both files PASS.

```bash
python -m pytest tests/ -q
```

Expected: all existing tests still pass (no regressions).

- [ ] **Step 8: Commit**

```bash
git add db/db.py reviewer/app.py tests/test_library_time_filter.py
git commit -m "feat(#142): extend library route + DB for time_pattern filter"
```

---

## Task 3: Map route integration

**Files:**
- Modify: `reviewer/app.py` — lines ~778–826 (`api_map_photos`)
- Create: `tests/test_map_time_filter.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_map_time_filter.py`:

```python
"""Integration tests for time_pattern filter on GET /api/map-photos."""
import tempfile
import pytest
from pathlib import Path
from db.db import Database
import reviewer.app as app_module


def _photo(i: int, **kwargs) -> dict:
    base: dict = {
        "uuid": f"mtp-u{i}",
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
def client_mtp():
    """
    Fixture with 3 photos:
      p_oct — geotagged, October (month 10, fall)
      p_jul — geotagged, July (month 07, summer)
      p_none — no location (never appears in map results)
    """
    with tempfile.TemporaryDirectory() as tmp:
        test_db = Database(Path(tmp) / "test.db")
        p_oct = test_db.upsert_photo(
            _photo(10, latitude=48.8566, longitude=2.3522, date_taken="2023-10-16T12:00:00")
        )
        p_jul = test_db.upsert_photo(
            _photo(11, latitude=40.7128, longitude=-74.0060, date_taken="2023-07-04T12:00:00")
        )
        p_none = test_db.upsert_photo(
            _photo(12, date_taken="2023-10-16T12:00:00")  # no lat/lon
        )
        app_module._db = test_db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test-secret"
        with app_module.app.test_client() as c:
            yield c, p_oct, p_jul, p_none, test_db
        app_module._db = None


def _ids(resp) -> set[int]:
    return {item["id"] for item in resp.get_json()}


class TestMapTimeFilter:
    def test_no_filter_returns_all_geotagged(self, client_mtp):
        c, p_oct, p_jul, p_none, _ = client_mtp
        r = c.get("/api/map-photos")
        assert r.status_code == 200
        ids = _ids(r)
        assert p_oct in ids
        assert p_jul in ids
        assert p_none not in ids   # no location

    def test_month_filter(self, client_mtp):
        c, p_oct, p_jul, p_none, _ = client_mtp
        r = c.get("/api/map-photos?time_pattern=month:10")
        assert r.status_code == 200
        ids = _ids(r)
        assert p_oct in ids
        assert p_jul not in ids
        assert p_none not in ids

    def test_season_summer(self, client_mtp):
        c, p_oct, p_jul, p_none, _ = client_mtp
        r = c.get("/api/map-photos?time_pattern=season:summer")
        ids = _ids(r)
        assert p_jul in ids    # July ∈ summer
        assert p_oct not in ids  # October not in summer

    def test_daytype_weekend(self, client_mtp):
        c, p_oct, p_jul, p_none, _ = client_mtp
        # Oct 16 = Monday, Jul 4 = Tuesday — neither is weekend
        r = c.get("/api/map-photos?time_pattern=daytype:weekend")
        ids = _ids(r)
        assert p_oct not in ids
        assert p_jul not in ids

    def test_daytype_weekday(self, client_mtp):
        c, p_oct, p_jul, p_none, _ = client_mtp
        r = c.get("/api/map-photos?time_pattern=daytype:weekday")
        ids = _ids(r)
        assert p_oct in ids
        assert p_jul in ids

    def test_holiday_thanksgiving_not_in_fixture(self, client_mtp):
        c, p_oct, p_jul, p_none, _ = client_mtp
        # Neither photo is near Thanksgiving (Nov 21–25 2023)
        r = c.get("/api/map-photos?time_pattern=holiday:thanksgiving&expand=1")
        assert r.status_code == 200
        assert r.get_json() == []

    def test_unknown_pattern_returns_all_geotagged(self, client_mtp):
        c, p_oct, p_jul, p_none, _ = client_mtp
        r = c.get("/api/map-photos?time_pattern=unknown:xyz")
        assert r.status_code == 200
        ids = _ids(r)
        assert p_oct in ids
        assert p_jul in ids
        assert p_none not in ids

    def test_json_structure_unchanged(self, client_mtp):
        c, p_oct, p_jul, p_none, _ = client_mtp
        r = c.get("/api/map-photos?time_pattern=month:10")
        data = r.get_json()
        assert len(data) == 1
        item = data[0]
        assert set(item.keys()) >= {"id", "lat", "lon", "title", "date", "flickr_url"}
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_map_time_filter.py -v 2>&1 | head -20
```

Expected: FAIL — `/api/map-photos` does not yet accept `time_pattern`.

- [ ] **Step 3: Update `api_map_photos` in `reviewer/app.py`**

The current route (line ~778) is:

```python
@app.route("/api/map-photos")
def api_map_photos() -> Response:
    flickr_username = _config.get("flickr", {}).get("username", "")
    rows = db().conn.execute(
        "SELECT id, latitude, longitude, photos_title, flickr_title, "
        "       date_taken, flickr_id "
        "FROM photos "
        "WHERE latitude IS NOT NULL AND longitude IS NOT NULL"
    ).fetchall()
```

Replace with:

```python
@app.route("/api/map-photos")
def api_map_photos() -> Response:
    flickr_username = _config.get("flickr", {}).get("username", "")
    time_pattern = request.args.get("time_pattern") or None
    time_expand  = 2 if request.args.get("expand") == "1" else 0

    extra_where = ""
    extra_params: list = []
    if time_pattern:
        from db.time_patterns import parse_pattern
        years = [
            r[0]
            for r in db().conn.execute(
                "SELECT DISTINCT CAST(strftime('%Y', date_taken) AS INTEGER) AS y "
                "FROM photos WHERE date_taken IS NOT NULL ORDER BY y"
            ).fetchall()
            if r[0] is not None
        ]
        frag, frag_params = parse_pattern(time_pattern, time_expand, years)
        if frag != "1=1":
            extra_where = f" AND {frag}"
            extra_params = frag_params

    rows = db().conn.execute(
        "SELECT id, latitude, longitude, photos_title, flickr_title, "
        "       date_taken, flickr_id "
        "FROM photos "
        f"WHERE latitude IS NOT NULL AND longitude IS NOT NULL{extra_where}",
        extra_params,
    ).fetchall()
```

The rest of the function (building `result` list and `return jsonify(result)`) is unchanged.

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_map_time_filter.py tests/test_time_patterns.py tests/test_library_time_filter.py -v
```

Expected: all PASS.

```bash
python -m pytest tests/ -q
```

Expected: all existing tests still pass.

- [ ] **Step 5: Commit**

```bash
git add reviewer/app.py tests/test_map_time_filter.py
git commit -m "feat(#142): extend /api/map-photos for time_pattern filter"
```

---

## Task 4: Library HTML — time pattern filter control

**Files:**
- Modify: `reviewer/templates/library.html`

No automated tests for this task — manual verification described at the end.

- [ ] **Step 1: Add the time pattern control to the filter bar**

In `library.html`, locate the filter bar (the `<div class="lib-filter-bar">` block around line 188). Add the following after the `<label>` block for "Untitled only" and before the `<button type="submit">`:

```html
  <label>Time of year
    <select name="time_pattern">
      <option value="">Any time</option>
      <optgroup label="Month">
        <option value="month:01" {% if filters.time_pattern == 'month:01' %}selected{% endif %}>January</option>
        <option value="month:02" {% if filters.time_pattern == 'month:02' %}selected{% endif %}>February</option>
        <option value="month:03" {% if filters.time_pattern == 'month:03' %}selected{% endif %}>March</option>
        <option value="month:04" {% if filters.time_pattern == 'month:04' %}selected{% endif %}>April</option>
        <option value="month:05" {% if filters.time_pattern == 'month:05' %}selected{% endif %}>May</option>
        <option value="month:06" {% if filters.time_pattern == 'month:06' %}selected{% endif %}>June</option>
        <option value="month:07" {% if filters.time_pattern == 'month:07' %}selected{% endif %}>July</option>
        <option value="month:08" {% if filters.time_pattern == 'month:08' %}selected{% endif %}>August</option>
        <option value="month:09" {% if filters.time_pattern == 'month:09' %}selected{% endif %}>September</option>
        <option value="month:10" {% if filters.time_pattern == 'month:10' %}selected{% endif %}>October</option>
        <option value="month:11" {% if filters.time_pattern == 'month:11' %}selected{% endif %}>November</option>
        <option value="month:12" {% if filters.time_pattern == 'month:12' %}selected{% endif %}>December</option>
      </optgroup>
      <optgroup label="Season">
        <option value="season:spring" {% if filters.time_pattern == 'season:spring' %}selected{% endif %}>Spring (Mar–Jun)</option>
        <option value="season:summer" {% if filters.time_pattern == 'season:summer' %}selected{% endif %}>Summer (Jun–Sep)</option>
        <option value="season:fall"   {% if filters.time_pattern == 'season:fall'   %}selected{% endif %}>Fall (Sep–Dec)</option>
        <option value="season:winter" {% if filters.time_pattern == 'season:winter' %}selected{% endif %}>Winter (Dec–Mar)</option>
      </optgroup>
      <optgroup label="Day type">
        <option value="daytype:weekend" {% if filters.time_pattern == 'daytype:weekend' %}selected{% endif %}>Weekends</option>
        <option value="daytype:weekday" {% if filters.time_pattern == 'daytype:weekday' %}selected{% endif %}>Weekdays</option>
      </optgroup>
      <optgroup label="Holidays">
        <option value="holiday:new_years"      {% if filters.time_pattern == 'holiday:new_years'      %}selected{% endif %}>New Year's Day (Jan 1)</option>
        <option value="holiday:mlk_day"        {% if filters.time_pattern == 'holiday:mlk_day'        %}selected{% endif %}>MLK Day (3rd Mon Jan)</option>
        <option value="holiday:presidents_day" {% if filters.time_pattern == 'holiday:presidents_day' %}selected{% endif %}>Presidents' Day (3rd Mon Feb)</option>
        <option value="holiday:memorial_day"   {% if filters.time_pattern == 'holiday:memorial_day'   %}selected{% endif %}>Memorial Day (last Mon May)</option>
        <option value="holiday:july_4th"       {% if filters.time_pattern == 'holiday:july_4th'       %}selected{% endif %}>July 4th</option>
        <option value="holiday:labor_day"      {% if filters.time_pattern == 'holiday:labor_day'      %}selected{% endif %}>Labor Day (1st Mon Sep)</option>
        <option value="holiday:columbus_day"   {% if filters.time_pattern == 'holiday:columbus_day'   %}selected{% endif %}>Columbus Day (2nd Mon Oct)</option>
        <option value="holiday:halloween"      {% if filters.time_pattern == 'holiday:halloween'      %}selected{% endif %}>Halloween (Oct 31)</option>
        <option value="holiday:thanksgiving"   {% if filters.time_pattern == 'holiday:thanksgiving'   %}selected{% endif %}>Thanksgiving (4th Thu Nov)</option>
        <option value="holiday:christmas"      {% if filters.time_pattern == 'holiday:christmas'      %}selected{% endif %}>Christmas (Dec 25)</option>
      </optgroup>
    </select>
  </label>
  <label id="lib-expand-label" style="display:none;align-items:center;gap:5px">
    <input type="checkbox" name="expand" value="1" {% if filters.expand == '1' %}checked{% endif %}> ±2 days
  </label>
```

- [ ] **Step 2: Update the "Clear filters" condition**

Find the line (around line 213):
```html
  {% if filters.date_from or filters.date_to or filters.album_id or filters.tag or filters.status or filters.untitled %}
```

Replace with:
```html
  {% if filters.date_from or filters.date_to or filters.album_id or filters.tag or filters.status or filters.untitled or filters.time_pattern %}
```

- [ ] **Step 3: Update `_buildPayload` in the library JS**

Find the `_buildPayload` function (around line 600) and update the `payload.filter` block:

```javascript
  if (_selectAllFilter) {
    const form = document.getElementById('lib-filter-form');
    const fd = new FormData(form);
    payload.filter = {
      date_from: fd.get('date_from') || null,
      date_to: fd.get('date_to') || null,
      album_id: fd.get('album_id') ? parseInt(fd.get('album_id')) : null,
      tag: fd.get('tag') || null,
      status: fd.get('status') || null,
      untitled: fd.get('untitled') === '1',
      time_pattern: fd.get('time_pattern') || null,
      expand: fd.get('expand') || null,
    };
  }
```

- [ ] **Step 4: Add the expand label show/hide JS**

At the bottom of `{% block content %}`, before `{% endblock %}`, add:

```html
<script>
(function () {
  const sel = document.querySelector('[name="time_pattern"]');
  const lbl = document.getElementById('lib-expand-label');
  function syncExpand() {
    lbl.style.display = sel.value.startsWith('holiday:') ? 'flex' : 'none';
  }
  sel.addEventListener('change', syncExpand);
  syncExpand();  // run on page load to restore state when returning with expand=1
})();
</script>
```

- [ ] **Step 5: Run the full test suite (no regressions)**

```bash
python -m pytest tests/ -q
```

Expected: all tests pass.

- [ ] **Step 6: Manual verification**

```bash
bp ui
```

- Navigate to `/library`. Confirm "Time of year" dropdown appears in the filter bar.
- Select "October" — Apply — grid narrows to October photos.
- Select "Thanksgiving" — the "±2 days" checkbox appears. Check it — Apply — grid includes Nov 21–25 photos.
- Deselect the holiday — "±2 days" checkbox disappears.
- "Clear filters" link appears while a filter is active; clicking it removes all filters.
- Select-all while a time filter is active — bulk action should correctly reflect the filtered count.

- [ ] **Step 7: Commit**

```bash
git add reviewer/templates/library.html
git commit -m "feat(#142): library.html — time pattern filter control + expand checkbox"
```

---

## Task 5: Map HTML — filter bar and JS reload

**Files:**
- Modify: `reviewer/templates/map.html`

- [ ] **Step 1: Add CSS for the filter bar and update map height**

In `map.html`, inside `{% block extra_style %}`, find:

```css
#map { height: calc(100vh - 48px); width: 100%; }
```

Replace with:

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

- [ ] **Step 2: Add the filter bar HTML above the map div**

In `{% block content %}`, replace:

```html
<div id="map"></div>
```

with:

```html
<div class="map-filter-bar">
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
    </select>
  </label>
  <label id="map-expand-label" style="display:none;align-items:center;gap:5px">
    <input type="checkbox" id="map-expand-cb"> ±2 days
  </label>
  <span id="map-photo-count" style="font-size:12px;color:var(--muted)"></span>
</div>
<div id="map"></div>
```

- [ ] **Step 3: Refactor the map JS to use `plotPhotos` + `reloadMarkers`**

Replace the entire `<script>` block (from `const map = L.map(...)` through `</script>`) with:

```html
<script>
const map = L.map('map').setView([{{ center_lat }}, {{ center_lon }}], 5);

L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  maxZoom: 19,
}).addTo(map);

const markers = L.markerClusterGroup();
map.addLayer(markers);   // added once; reloadMarkers only calls clearLayers()

function esc(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function buildMapUrl() {
  const p = document.getElementById('map-time-select').value;
  const e = document.getElementById('map-expand-cb').checked ? '&expand=1' : '';
  return p ? `/api/map-photos?time_pattern=${encodeURIComponent(p)}${e}` : '/api/map-photos';
}

function plotPhotos(photos) {
  photos.forEach(p => {
    const marker = L.marker([p.lat, p.lon]);
    const shortTitle = p.title.length > 60 ? p.title.slice(0, 60) + '…' : p.title;
    let links = `<a href="/photo/${p.id}">Open photo</a>`;
    if (p.flickr_url) links += `<a href="${esc(p.flickr_url)}" target="_blank" rel="noopener">Flickr &#x2197;</a>`;
    if (p.date)       links += `<a href="/library?date=${p.date}">Show this day</a>`;
    marker.bindPopup(`
      <div class="map-popup">
        <img src="/thumb/${p.id}" alt="">
        <div class="pop-title">${esc(shortTitle)}</div>
        <div class="pop-date">${esc(p.date || '')}</div>
        <div class="pop-links">${links}</div>
      </div>
    `, { maxWidth: 200 });
    markers.addLayer(marker);
  });
  document.getElementById('map-photo-count').textContent =
    photos.length === 1 ? '1 photo' : `${photos.length} photos`;
}

function reloadMarkers() {
  markers.clearLayers();
  fetch(buildMapUrl())
    .then(r => r.json())
    .then(plotPhotos)
    .catch(() => { /* silently fail — map usable, just empty */ });
}

document.getElementById('map-time-select').addEventListener('change', function () {
  const lbl = document.getElementById('map-expand-label');
  const cb  = document.getElementById('map-expand-cb');
  lbl.style.display = this.value.startsWith('holiday:') ? 'flex' : 'none';
  if (!this.value.startsWith('holiday:')) cb.checked = false;
  reloadMarkers();
});
document.getElementById('map-expand-cb').addEventListener('change', reloadMarkers);

reloadMarkers();   // initial load (replaces the former bare fetch call)
</script>
```

- [ ] **Step 4: Run the full test suite (no regressions)**

```bash
python -m pytest tests/ -q
```

Expected: all tests pass (map.html is a template — no test coverage for the JS, but backend tests confirm the API works).

- [ ] **Step 5: Manual verification**

```bash
bp ui
```

Press `0` to open the map.

- Map loads with photo count shown (e.g. "12,345 photos").
- Select "October" from "Time of year" — markers reload instantly, count updates.
- Select "Thanksgiving" — "±2 days" checkbox appears. Check it — markers reload to show Nov 21–25 photos.
- Deselect holiday — "±2 days" disappears; markers reload to full set.
- Map zoom/position is preserved across filter changes.
- Clicking a marker popup still shows thumbnail, title, date, and links correctly.

- [ ] **Step 6: Commit**

```bash
git add reviewer/templates/map.html
git commit -m "feat(#142): map.html — filter bar, time pattern select, JS reload"
```

---

## Task 6: README, docs, and issue close

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-05-25-temporal-pattern-filter-142.md`

- [ ] **Step 1: Update README**

Update the test count (currently 1221 — add the count of new tests: `test_time_patterns.py` ~20, `test_library_time_filter.py` ~13, `test_map_time_filter.py` ~8 = ~41 new tests). Run `python -m pytest tests/ -q` to get the exact count, then update the line in README.

Add to the feature list (near the `/library` and `/map` entries):

```markdown
- **Temporal pattern filter** — "Time of year" dropdown in the library and map: any month, fuzzy season, weekends/weekdays, named US holidays (Labor Day, Thanksgiving, Christmas, and 7 others) with optional ±2-day expansion window. Computed in pure SQLite `strftime()` + Python `datetime`; no external dependencies.
```

- [ ] **Step 2: Mark spec done**

In `docs/superpowers/specs/2026-05-25-temporal-pattern-filter-142.md`, change:

```
**Status:** in progress
```

to:

```
**Status:** ✓ done
```

- [ ] **Step 3: Run lint**

```bash
make lint
```

Expected: no new mypy errors. Fix any that appear (they'll be in the modified files).

- [ ] **Step 4: Commit**

```bash
git add README.md docs/superpowers/specs/2026-05-25-temporal-pattern-filter-142.md
git commit -m "docs(#142): README + mark spec done"
```

- [ ] **Step 5: Close GH issue with retrospective**

```bash
gh issue close 142 --comment "Shipped in this branch.

**What was built:**
- \`db/time_patterns.py\` — pure module: SEASONS (fuzzy overlapping ranges), HOLIDAYS (10 US federal holidays), \`_nth_weekday()\`, \`holiday_date()\`, \`parse_pattern()\`
- \`_library_where\` extended with \`time_pattern\` / \`time_expand\` params; \`library_photos\`, \`library_photo_count\`, \`library_photo_ids\` all updated
- Library filter bar: 'Time of year' dropdown (months → seasons → day type → holidays in calendar order) + '±2 days' expand checkbox
- Map filter bar: same dropdown + expand; JS re-fetches and re-plots on change (preserves zoom)
- Bulk-edit route updated to pass \`time_pattern\` / \`expand\` through filter dict so 'select all filtered' respects the time filter
- ~41 new tests

**Size estimate:** S (was labelled S — accurate)"
```

- [ ] **Step 6: Push**

```bash
git push origin main
```
