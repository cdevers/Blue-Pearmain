# Library Search and Filter Expansion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add text search, 4-level location cascade, person filter, and a `?date=` single-day alias to the library, and restructure the filter UI into a persistent search bar + collapsible Filters panel.

**Architecture:** A new pure module `db/photo_filters.py` provides composable `(sql, params)` fragment builders; `_library_where` in `db/db.py` calls into it alongside the existing time_pattern block (added by #142); the library route passes location and person lookup data to the template; the template is restructured from a flat filter bar into a search box + collapsible panel.

**Tech Stack:** Python (stdlib only), SQLite `LIKE` + `json_each`, Jinja2, vanilla JS (no new libraries).

**Spec:** `docs/superpowers/specs/2026-05-25-library-search-141.md`

**Dependency:** #142 must ship before this plan is executed. When this plan runs, `_library_where` already has `time_pattern` and `time_expand` params, the library filter bar already has the Time of year select + ±2 days checkbox, and `_buildPayload` already includes those fields. This plan layers on top of that state.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `db/photo_filters.py` | Create | Pure fragment builders: text, location, person, date alias |
| `db/db.py` | Modify | `location_data()`, `person_names()`, extend `_library_where` + 3 library methods |
| `reviewer/app.py` | Modify | Library route: read new params, resolve `?date=` alias, pass page-load data; bulk-edit: pass new filter params |
| `reviewer/templates/library.html` | Modify | Restructure to search bar + collapsible Filters panel; add location cascade + person input; update `_buildPayload` |
| `tests/test_photo_filters.py` | Create | Unit tests for the pure module |
| `tests/test_library_page_data.py` | Create | Unit tests for `location_data()` and `person_names()` |
| `tests/test_library_search.py` | Create | Integration tests via Flask test client |

---

## Task 1: `db/photo_filters.py` — pure photo filter module

**Files:**
- Create: `db/photo_filters.py`
- Create: `tests/test_photo_filters.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_photo_filters.py`:

```python
"""Unit tests for db/photo_filters.py — pure photo filter module."""
import pytest
from db.photo_filters import (
    build_text_clause,
    build_location_clause,
    build_person_clause,
    build_date_alias_clause,
)


class TestBuildTextClause:
    def test_seven_params_all_equal_to_term(self):
        sql, params = build_text_clause("sunset")
        assert len(params) == 7
        assert all(p == "%sunset%" for p in params)

    def test_sql_covers_all_seven_fields(self):
        sql, _ = build_text_clause("x")
        assert "photos_title" in sql
        assert "flickr_title" in sql
        assert "photos_description" in sql
        assert "flickr_description" in sql
        assert "apple_ai_caption" in sql
        assert "flickr_tags" in sql
        assert "photos_tags" in sql

    def test_tag_fields_use_json_each(self):
        sql, _ = build_text_clause("x")
        assert "json_each" in sql
        assert "EXISTS" in sql

    def test_term_wrapped_with_percent(self):
        _, params = build_text_clause("birthday")
        assert params[0] == "%birthday%"

    def test_empty_string_still_returns_fragment(self):
        # Caller guards q != None/empty; but if called with empty it should not crash
        sql, params = build_text_clause("")
        assert "%" in params[0]


class TestBuildLocationClause:
    def test_all_four_levels(self):
        sql, params = build_location_clause("United States", "MA", "Boston", "Back Bay")
        assert sql.count("= ?") == 4
        assert params == ["United States", "MA", "Boston", "Back Bay"]

    def test_three_levels_no_neighborhood(self):
        sql, params = build_location_clause("United States", "MA", "Springfield", None)
        assert "neighborhood" not in sql
        assert params == ["United States", "MA", "Springfield"]

    def test_country_only(self):
        sql, params = build_location_clause("France", None, None, None)
        assert sql == "p.place_country = ?"
        assert params == ["France"]

    def test_all_none_returns_noop(self):
        sql, params = build_location_clause(None, None, None, None)
        assert sql == "1=1"
        assert params == []

    def test_clauses_and_combined(self):
        sql, _ = build_location_clause("United States", "MA", None, None)
        assert " AND " in sql

    def test_neighborhood_without_city(self):
        # AND-combination is correct; cascade prevents this in UI
        sql, params = build_location_clause(None, None, None, "Union Square")
        assert "place_neighborhood" in sql
        assert params == ["Union Square"]


class TestBuildPersonClause:
    def test_json_each_exists_fragment(self):
        sql, params = build_person_clause("Alice")
        assert "json_each" in sql
        assert "EXISTS" in sql
        assert params == ["Alice"]

    def test_exact_match_not_like(self):
        sql, _ = build_person_clause("Alice")
        assert "LIKE" not in sql
        assert "value = ?" in sql

    def test_underscore_unknown_works_as_value(self):
        # _UNKNOWN_ is filtered from datalist but valid as a query param
        sql, params = build_person_clause("_UNKNOWN_")
        assert params == ["_UNKNOWN_"]


class TestBuildDateAliasClause:
    def test_date_function_exact_match(self):
        sql, params = build_date_alias_clause("2023-10-15")
        assert sql == "DATE(p.date_taken) = ?"
        assert params == ["2023-10-15"]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_photo_filters.py -v 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'db.photo_filters'`

- [ ] **Step 3: Create `db/photo_filters.py`**

```python
"""
Photo filter helpers — pure functions returning SQLite WHERE clause fragments.
No Flask or DB dependencies.

All fragments reference the 'p' alias (photos p) to match the _library_where
convention in db/db.py. Unknown or no-op inputs return ("1=1", []).

Usage:
    from db.photo_filters import build_text_clause, build_location_clause
    sql, params = build_text_clause("sunset")
    sql, params = build_location_clause("United States", "MA", "Boston", None)
"""

from __future__ import annotations


def build_text_clause(q: str) -> tuple[str, list]:
    """LIKE search across all text fields including Apple AI caption.

    Semantics:
    - Case-insensitive for ASCII, case-sensitive for non-ASCII (SQLite LIKE behaviour).
    - Substring match only: '%q%'. Searching 'birthday cake' matches the
      whole phrase, not photos containing 'birthday' and 'cake' separately.
    - Tags are searched via json_each — 'bird' matches the tag 'birding'.
    """
    term = f"%{q}%"
    sql = (
        "(p.photos_title LIKE ? OR p.flickr_title LIKE ?"
        " OR p.photos_description LIKE ? OR p.flickr_description LIKE ?"
        " OR p.apple_ai_caption LIKE ?"
        " OR EXISTS (SELECT 1 FROM json_each(p.flickr_tags) WHERE value LIKE ?)"
        " OR EXISTS (SELECT 1 FROM json_each(p.photos_tags) WHERE value LIKE ?))"
    )
    return sql, [term] * 7


def build_location_clause(
    country: str | None,
    state: str | None,
    city: str | None,
    neighborhood: str | None,
) -> tuple[str, list]:
    """Exact match on place columns. Only non-None values generate clauses.
    All active levels are AND-combined, which disambiguates same-name cities
    (Springfield MA vs Springfield VT) and neighborhoods (Union Square in
    Somerville vs Boston). Photos with NULL place_country are never returned
    when country is set, because NULL != any string in SQL equality.

    Lower levels without parent levels return all matches across all parents —
    e.g. ?neighborhood=Union+Square alone matches every Union Square in the DB.
    The cascade UI prevents this in normal use by always sending the full path.
    """
    clauses: list[str] = []
    params: list = []
    if country:
        clauses.append("p.place_country = ?")
        params.append(country)
    if state:
        clauses.append("p.place_state = ?")
        params.append(state)
    if city:
        clauses.append("p.place_city = ?")
        params.append(city)
    if neighborhood:
        clauses.append("p.place_neighborhood = ?")
        params.append(neighborhood)
    if not clauses:
        return "1=1", []
    return " AND ".join(clauses), params


def build_person_clause(person: str) -> tuple[str, list]:
    """Match any photo whose apple_persons JSON array contains the exact name.
    '_UNKNOWN_' is a valid query value (returns photos with unidentified faces)
    even though it is filtered from the datalist autocomplete in the UI."""
    return (
        "EXISTS (SELECT 1 FROM json_each(p.apple_persons) WHERE value = ?)",
        [person],
    )


def build_date_alias_clause(date: str) -> tuple[str, list]:
    """Single-day filter. Used by the map popup 'Show this day' link via the
    ?date=YYYY-MM-DD alias. The alias is resolved in app.py before DB calls;
    this function is reserved for future use by other endpoints (e.g. /api/map-photos)."""
    return "DATE(p.date_taken) = ?", [date]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_photo_filters.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add db/photo_filters.py tests/test_photo_filters.py
git commit -m "feat(#141): db/photo_filters.py — pure text/location/person filter module"
```

---

## Task 2: DB methods — `location_data`, `person_names`, extend `_library_where`

**Files:**
- Modify: `db/db.py` — lines ~876–993 (`_library_where`, `library_photos`, `library_photo_count`, `library_photo_ids`) + new methods after line 993
- Create: `tests/test_library_page_data.py`

**Context:** When this task runs, `_library_where` (line ~876) already has `time_pattern: str | None = None` and `time_expand: int = 2` params added by #142. You are extending it further — do not remove or reorder those params.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_library_page_data.py`:

```python
"""Unit tests for db.location_data() and db.person_names()."""
import tempfile
import pytest
from pathlib import Path
from db.db import Database


def _photo(i: int, **kwargs) -> dict:
    base: dict = {
        "uuid": f"lpd-u{i}",
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
def db_lpd():
    """
    Fixture with photos covering location and person edge cases:

    p1  — United States > MA > Springfield (no neighborhood)
    p2  — United States > VT > Springfield (same city, different state)
    p3  — United States > MA > Somerville, neighborhood="Union Square"
    p4  — United States > MA > Boston, neighborhood="Union Square"
           (same neighborhood as p3 but different city)
    p5  — United States > MA > Boston, neighborhood=""
           (empty neighborhood — excluded from neighborhood list)
    p6  — United States > MA > Boston, neighborhood="Back Bay"
    p7  — no place_country (NULL) — excluded from location_data
    p8  — apple_persons=["Alice"]
    p9  — apple_persons=["Bob"]
    p10 — apple_persons=["Alice", "Charlie"]  (Alice appears twice — deduplicated)
    p11 — apple_persons=["_UNKNOWN_"]         (excluded from person_names)
    p12 — apple_persons=[]                    (no persons)
    """
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "test.db")
        db.upsert_photo(_photo(1, place_country="United States", place_state="MA",
                                place_city="Springfield"))
        db.upsert_photo(_photo(2, place_country="United States", place_state="VT",
                                place_city="Springfield"))
        db.upsert_photo(_photo(3, place_country="United States", place_state="MA",
                                place_city="Somerville", place_neighborhood="Union Square"))
        db.upsert_photo(_photo(4, place_country="United States", place_state="MA",
                                place_city="Boston", place_neighborhood="Union Square"))
        db.upsert_photo(_photo(5, place_country="United States", place_state="MA",
                                place_city="Boston", place_neighborhood=""))
        db.upsert_photo(_photo(6, place_country="United States", place_state="MA",
                                place_city="Boston", place_neighborhood="Back Bay"))
        db.upsert_photo(_photo(7))  # no location
        db.upsert_photo(_photo(8, apple_persons=["Alice"]))
        db.upsert_photo(_photo(9, apple_persons=["Bob"]))
        db.upsert_photo(_photo(10, apple_persons=["Alice", "Charlie"]))
        db.upsert_photo(_photo(11, apple_persons=["_UNKNOWN_"]))
        db.upsert_photo(_photo(12, apple_persons=[]))
        yield db


class TestLocationData:
    def test_returns_nested_dict(self, db_lpd):
        tree = db_lpd.location_data()
        assert isinstance(tree, dict)
        assert "United States" in tree

    def test_null_country_excluded(self, db_lpd):
        tree = db_lpd.location_data()
        for country, states in tree.items():
            assert country  # no empty-string or None key
        # p7 has no place_country; it should not cause any entry
        assert len(tree) == 1  # only "United States"

    def test_state_level_correct(self, db_lpd):
        states = db_lpd.location_data()["United States"]
        assert "MA" in states
        assert "VT" in states

    def test_same_city_different_states(self, db_lpd):
        tree = db_lpd.location_data()
        assert "Springfield" in tree["United States"]["MA"]
        assert "Springfield" in tree["United States"]["VT"]

    def test_neighborhoods_correct(self, db_lpd):
        tree = db_lpd.location_data()
        boston_nbhds = tree["United States"]["MA"]["Boston"]
        assert "Back Bay" in boston_nbhds
        assert "Union Square" in boston_nbhds

    def test_empty_neighborhood_excluded(self, db_lpd):
        tree = db_lpd.location_data()
        boston_nbhds = tree["United States"]["MA"]["Boston"]
        assert "" not in boston_nbhds

    def test_same_neighborhood_different_cities(self, db_lpd):
        tree = db_lpd.location_data()
        assert "Union Square" in tree["United States"]["MA"]["Somerville"]
        assert "Union Square" in tree["United States"]["MA"]["Boston"]

    def test_neighborhoods_sorted(self, db_lpd):
        tree = db_lpd.location_data()
        boston_nbhds = tree["United States"]["MA"]["Boston"]
        assert boston_nbhds == sorted(boston_nbhds)

    def test_cities_sorted(self, db_lpd):
        tree = db_lpd.location_data()
        cities = list(tree["United States"]["MA"].keys())
        assert cities == sorted(cities)


class TestPersonNames:
    def test_returns_sorted_list(self, db_lpd):
        names = db_lpd.person_names()
        assert names == sorted(names)

    def test_excludes_unknown(self, db_lpd):
        names = db_lpd.person_names()
        assert "_UNKNOWN_" not in names

    def test_no_duplicates(self, db_lpd):
        names = db_lpd.person_names()
        assert len(names) == len(set(names))

    def test_all_three_named_persons_present(self, db_lpd):
        names = db_lpd.person_names()
        assert "Alice" in names
        assert "Bob" in names
        assert "Charlie" in names
        assert len(names) == 3
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_library_page_data.py -v 2>&1 | head -20
```

Expected: `AttributeError: 'Database' object has no attribute 'location_data'`

- [ ] **Step 3: Add `location_data()` to `db/db.py`**

After `library_photo_ids` (line ~993), add:

```python
def location_data(self) -> dict:
    """Return nested dict {country: {state: {city: [neighborhoods]}}} for non-deleted photos.
    Photos where place_country is NULL or empty are excluded.
    Empty-string neighborhoods are excluded from neighborhood lists.
    All levels sorted alphabetically."""
    rows = self.conn.execute(
        "SELECT place_country, place_state, place_city, place_neighborhood "
        "FROM photos "
        "WHERE flickr_deleted = 0 "
        "  AND place_country IS NOT NULL AND place_country != ''"
    ).fetchall()

    tree: dict = {}
    for r in rows:
        country = (r["place_country"] or "").strip()
        state   = (r["place_state"]   or "").strip()
        city    = (r["place_city"]    or "").strip()
        nbhd    = (r["place_neighborhood"] or "").strip()
        if not country:
            continue
        tree.setdefault(country, {})
        tree[country].setdefault(state, {})
        tree[country][state].setdefault(city, set())
        if nbhd:
            tree[country][state][city].add(nbhd)

    return {
        c: {
            s: {ci: sorted(nbhds) for ci, nbhds in sorted(cities.items())}
            for s, cities in sorted(states.items())
        }
        for c, states in sorted(tree.items())
    }

def person_names(self) -> list[str]:
    """Return distinct person names from apple_persons JSON arrays,
    excluding '_UNKNOWN_', sorted alphabetically."""
    rows = self.conn.execute(
        "SELECT DISTINCT j.value "
        "FROM photos p, json_each(p.apple_persons) j "
        "WHERE j.value != '_UNKNOWN_' AND p.flickr_deleted = 0 "
        "ORDER BY j.value"
    ).fetchall()
    return [r["value"] for r in rows]
```

- [ ] **Step 4: Run DB method tests to verify they pass**

```bash
python -m pytest tests/test_library_page_data.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Extend `_library_where` in `db/db.py`**

`_library_where` currently ends at line ~917. After #142, its signature is:

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
```

Replace the entire method with the following (preserving all existing clauses; add the six new #141 params at the end of the signature and the three new clause blocks at the end of the method body, after the time_pattern block):

```python
def _library_where(
    self,
    date_from: str | None,
    date_to: str | None,
    album_id: int | None,
    tag: str | None,
    status: str | None,
    untitled_only: bool,
    time_pattern: str | None = None,   # added by #142
    time_expand: int = 2,              # added by #142
    q: str | None = None,              # #141 text search
    country: str | None = None,        # #141 location cascade
    state: str | None = None,          # #141
    city: str | None = None,           # #141
    neighborhood: str | None = None,   # #141
    person: str | None = None,         # #141 person filter
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

    # #141 — text, location, person
    if q or country or state or city or neighborhood or person:
        from db.photo_filters import (
            build_text_clause,
            build_location_clause,
            build_person_clause,
        )
        if q:
            frag, frag_params = build_text_clause(q)
            clauses.append(frag)
            params.extend(frag_params)
        loc_sql, loc_params = build_location_clause(country, state, city, neighborhood)
        if loc_sql != "1=1":
            clauses.append(loc_sql)
            params.extend(loc_params)
        if person:
            frag, frag_params = build_person_clause(person)
            clauses.append(frag)
            params.extend(frag_params)

    where = "WHERE " + " AND ".join(clauses)

    if album_id is not None:
        return where + " AND pa.album_id = ? AND pa.removed_at IS NULL", params + [album_id]

    return where, params
```

- [ ] **Step 6: Extend `library_photos`, `library_photo_count`, `library_photo_ids`**

Each method gains the same six new optional kwargs. Show the complete updated signatures and `_library_where` call lines. The rest of each method body is **unchanged**.

**`library_photos`** (line ~919 — find it by name; the exact line shifts after #142 changes):

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
    q: str | None = None,
    country: str | None = None,
    state: str | None = None,
    city: str | None = None,
    neighborhood: str | None = None,
    person: str | None = None,
    limit: int = 120,
    offset: int = 0,
) -> list[dict]:
    """Return photos for the library grid, newest first, with filters applied."""
    where, params = self._library_where(
        date_from, date_to, album_id, tag, status, untitled_only,
        time_pattern, time_expand,
        q, country, state, city, neighborhood, person,
    )
    # ... rest of method body unchanged (join, execute, return)
```

**`library_photo_count`** (line ~956):

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
    q: str | None = None,
    country: str | None = None,
    state: str | None = None,
    city: str | None = None,
    neighborhood: str | None = None,
    person: str | None = None,
) -> int:
    """Return total photo count for the given library filters."""
    where, params = self._library_where(
        date_from, date_to, album_id, tag, status, untitled_only,
        time_pattern, time_expand,
        q, country, state, city, neighborhood, person,
    )
    # ... rest unchanged
```

**`library_photo_ids`** (line ~975):

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
    q: str | None = None,
    country: str | None = None,
    state: str | None = None,
    city: str | None = None,
    neighborhood: str | None = None,
    person: str | None = None,
) -> list[int]:
    """Return all photo IDs matching the filters (no limit — used by bulk-edit)."""
    where, params = self._library_where(
        date_from, date_to, album_id, tag, status, untitled_only,
        time_pattern, time_expand,
        q, country, state, city, neighborhood, person,
    )
    # ... rest unchanged
```

- [ ] **Step 7: Run full test suite — verify no regressions**

```bash
python -m pytest tests/test_library_page_data.py tests/test_photo_filters.py -v
python -m pytest tests/ -q
```

Expected: both targeted files pass; full suite passes.

- [ ] **Step 8: Commit**

```bash
git add db/db.py tests/test_library_page_data.py
git commit -m "feat(#141): DB — location_data, person_names, extend _library_where + 3 methods"
```

---

## Task 3: `reviewer/app.py` — library route + bulk-edit + integration tests

**Files:**
- Modify: `reviewer/app.py` — lines ~828–882 (library route), ~1199–1208 (bulk-edit)
- Create: `tests/test_library_search.py`

**Context:** After #142, the library route (starting line ~828) already reads `time_pattern` and `time_expand` and passes them to `library_photos`, `library_photo_count`, and the `filters` dict. The bulk-edit route already reads them from `_filter`. This task adds the six new params on top.

- [ ] **Step 1: Write the failing integration tests**

Create `tests/test_library_search.py`:

```python
"""Integration tests for text search, location, person, and date-alias filters."""
import tempfile
import pytest
import re
from pathlib import Path
from db.db import Database
import reviewer.app as app_module


def _photo(i: int, **kwargs) -> dict:
    base: dict = {
        "uuid": f"ls-u{i}",
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
def client_ls():
    """
    5-photo fixture:

    p1 — photos_title="Sunset over the lake"
         United States > MA > Springfield

    p2 — flickr_description="Birthday at the lake"
         apple_ai_caption="birthday cake on the table"
         United States > VT > Springfield
         (same city name as p1, different state)

    p3 — flickr_tags=["birding", "wildlife"]
         apple_persons=["Alice"]
         United States > MA > Somerville, neighborhood="Union Square"

    p4 — apple_persons=["Alice", "Bob"]
         United States > MA > Boston, neighborhood="Union Square"
         (same neighborhood as p3, different city)
         date_taken="2023-10-15T10:00:00"

    p5 — apple_persons=["_UNKNOWN_"]
         date_taken="2023-10-15T18:00:00"
    """
    with tempfile.TemporaryDirectory() as tmp:
        test_db = Database(Path(tmp) / "test.db")
        p1 = test_db.upsert_photo(_photo(1,
            photos_title="Sunset over the lake",
            place_country="United States", place_state="MA", place_city="Springfield"))
        p2 = test_db.upsert_photo(_photo(2,
            flickr_description="Birthday at the lake",
            apple_ai_caption="birthday cake on the table",
            place_country="United States", place_state="VT", place_city="Springfield"))
        p3 = test_db.upsert_photo(_photo(3,
            flickr_tags=["birding", "wildlife"],
            apple_persons=["Alice"],
            place_country="United States", place_state="MA",
            place_city="Somerville", place_neighborhood="Union Square"))
        p4 = test_db.upsert_photo(_photo(4,
            apple_persons=["Alice", "Bob"],
            place_country="United States", place_state="MA",
            place_city="Boston", place_neighborhood="Union Square",
            date_taken="2023-10-15T10:00:00"))
        p5 = test_db.upsert_photo(_photo(5,
            apple_persons=["_UNKNOWN_"],
            date_taken="2023-10-15T18:00:00"))

        app_module._db = test_db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test-secret"
        with app_module.app.test_client() as c:
            yield c, p1, p2, p3, p4, p5, test_db
        app_module._db = None


def _ids(resp) -> set[int]:
    return {int(m) for m in re.findall(r'data-id="(\d+)"', resp.data.decode())}


class TestTextSearch:
    def test_photos_title_match(self, client_ls):
        c, p1, p2, p3, p4, p5, _ = client_ls
        r = c.get("/library?q=sunset")
        assert r.status_code == 200
        ids = _ids(r)
        assert p1 in ids
        assert p2 not in ids
        assert p3 not in ids

    def test_flickr_description_match(self, client_ls):
        c, p1, p2, p3, p4, p5, _ = client_ls
        r = c.get("/library?q=birthday")
        ids = _ids(r)
        assert p2 in ids        # flickr_description contains "birthday"
        assert p1 not in ids

    def test_apple_ai_caption_match(self, client_ls):
        c, p1, p2, p3, p4, p5, _ = client_ls
        # "cake" only appears in apple_ai_caption of p2
        r = c.get("/library?q=cake")
        ids = _ids(r)
        assert p2 in ids
        assert p1 not in ids

    def test_flickr_tags_match(self, client_ls):
        c, p1, p2, p3, p4, p5, _ = client_ls
        # "bird" is a substring of the tag "birding"
        r = c.get("/library?q=bird")
        ids = _ids(r)
        assert p3 in ids
        assert p1 not in ids

    def test_no_match_returns_empty(self, client_ls):
        c, *_ = client_ls
        r = c.get("/library?q=xyzzy_no_match")
        assert r.status_code == 200
        assert _ids(r) == set()

    def test_empty_q_returns_all(self, client_ls):
        c, p1, p2, p3, p4, p5, _ = client_ls
        r = c.get("/library?q=")
        ids = _ids(r)
        assert {p1, p2, p3, p4, p5} == ids


class TestLocationFilter:
    def test_disambiguates_springfield_by_state(self, client_ls):
        c, p1, p2, p3, p4, p5, _ = client_ls
        r = c.get("/library?country=United+States&state=MA&city=Springfield")
        ids = _ids(r)
        assert p1 in ids        # Springfield MA
        assert p2 not in ids    # Springfield VT

    def test_vt_springfield(self, client_ls):
        c, p1, p2, p3, p4, p5, _ = client_ls
        r = c.get("/library?country=United+States&state=VT&city=Springfield")
        ids = _ids(r)
        assert p2 in ids
        assert p1 not in ids

    def test_disambiguates_union_square_by_city(self, client_ls):
        c, p1, p2, p3, p4, p5, _ = client_ls
        r = c.get("/library?country=United+States&state=MA"
                  "&city=Somerville&neighborhood=Union+Square")
        ids = _ids(r)
        assert p3 in ids        # Somerville Union Square
        assert p4 not in ids    # Boston Union Square

    def test_neighborhood_without_city_returns_both(self, client_ls):
        c, p1, p2, p3, p4, p5, _ = client_ls
        # No city filter — both Union Squares match. Correct: cascade prevents this in UI.
        r = c.get("/library?neighborhood=Union+Square")
        ids = _ids(r)
        assert p3 in ids
        assert p4 in ids

    def test_country_only(self, client_ls):
        c, p1, p2, p3, p4, p5, _ = client_ls
        r = c.get("/library?country=United+States")
        ids = _ids(r)
        assert {p1, p2, p3, p4} <= ids  # all geotagged photos
        # p5 has no country — may or may not be in result; check it's not in the set
        assert p5 not in ids

    def test_unknown_country_returns_nothing(self, client_ls):
        c, *_ = client_ls
        r = c.get("/library?country=Freedonia")
        assert r.status_code == 200
        assert _ids(r) == set()


class TestPersonFilter:
    def test_alice(self, client_ls):
        c, p1, p2, p3, p4, p5, _ = client_ls
        r = c.get("/library?person=Alice")
        ids = _ids(r)
        assert p3 in ids    # ["Alice"]
        assert p4 in ids    # ["Alice", "Bob"]
        assert p1 not in ids
        assert p5 not in ids

    def test_bob(self, client_ls):
        c, p1, p2, p3, p4, p5, _ = client_ls
        r = c.get("/library?person=Bob")
        ids = _ids(r)
        assert p4 in ids    # only Bob
        assert p3 not in ids

    def test_unknown_person_marker(self, client_ls):
        c, p1, p2, p3, p4, p5, _ = client_ls
        r = c.get("/library?person=_UNKNOWN_")
        ids = _ids(r)
        assert p5 in ids
        assert p3 not in ids

    def test_no_match_person(self, client_ls):
        c, *_ = client_ls
        r = c.get("/library?person=Nobody")
        assert _ids(r) == set()


class TestDateAlias:
    def test_date_returns_photos_from_that_day(self, client_ls):
        c, p1, p2, p3, p4, p5, _ = client_ls
        r = c.get("/library?date=2023-10-15")
        ids = _ids(r)
        assert p4 in ids    # 2023-10-15T10:00:00
        assert p5 in ids    # 2023-10-15T18:00:00
        assert p1 not in ids
        assert p2 not in ids
        assert p3 not in ids

    def test_date_alias_does_not_crash_without_other_filters(self, client_ls):
        c, *_ = client_ls
        r = c.get("/library?date=2023-01-01")
        assert r.status_code == 200


class TestCombinedFilters:
    def test_q_and_country_combined(self, client_ls):
        c, p1, p2, p3, p4, p5, _ = client_ls
        # "sunset" appears in p1 (MA), not p2 (VT) — so with MA filter, only p1
        r = c.get("/library?q=sunset&country=United+States&state=MA")
        ids = _ids(r)
        assert p1 in ids
        assert p2 not in ids

    def test_person_and_city_combined(self, client_ls):
        c, p1, p2, p3, p4, p5, _ = client_ls
        # Alice in Boston → only p4
        r = c.get("/library?person=Alice&city=Boston")
        ids = _ids(r)
        assert p4 in ids
        assert p3 not in ids    # Alice but Somerville

    def test_empty_params_return_all(self, client_ls):
        c, p1, p2, p3, p4, p5, _ = client_ls
        r = c.get("/library?q=&country=&person=")
        assert r.status_code == 200
        ids = _ids(r)
        assert len(ids) == 5
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_library_search.py -v 2>&1 | head -20
```

Expected: FAIL — `library_photos` does not accept `q` kwarg yet (DB was updated but app route hasn't been wired up; the DB kwargs default to None so the route call fails if the route passes them as keyword args, or passes nothing and tests fail because results are unfiltered).

- [ ] **Step 3: Update the library route in `reviewer/app.py`**

In `library()` (starting line ~828), add after the existing `time_pattern`/`time_expand` lines (or after `untitled_only` if #142 hasn't run yet — but it must have, per plan dependency):

```python
    q            = request.args.get("q", "").strip() or None
    country      = request.args.get("country") or None
    state        = request.args.get("state") or None
    city         = request.args.get("city") or None
    neighborhood = request.args.get("neighborhood") or None
    person       = request.args.get("person") or None
    date_alias   = request.args.get("date") or None
    if date_alias:
        date_from = date_from or date_alias
        date_to   = date_to   or date_alias
```

Update the `db().library_photos(...)` call to add the new kwargs:

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
        limit=per_page,
        offset=offset,
    )
```

Update the `db().library_photo_count(...)` call:

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
    )
```

Add two page-load queries (before `albums = db().get_all_albums()`):

```python
    location_tree = db().location_data()
    person_list   = db().person_names()
```

Update the `filters` dict:

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
            "q": q or "",
            "country": country or "",
            "state": state or "",
            "city": city or "",
            "neighborhood": neighborhood or "",
            "person": person or "",
        },
```

Add the new template variables to `render_template(...)`:

```python
    return render_template(
        "library.html",
        photos=photos,
        albums=albums,
        total=total,
        page=page,
        per_page=per_page,
        total_pages=max(1, (total + per_page - 1) // per_page),
        current_album=current_album,
        location_tree=location_tree,
        person_list=person_list,
        filters={...},   # as above
    )
```

- [ ] **Step 4: Update the bulk-edit route in `reviewer/app.py`**

In `api_bulk_edit()` (line ~1199), extend the `library_photo_ids` call:

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
        )
```

- [ ] **Step 5: Run integration tests to verify they pass**

```bash
python -m pytest tests/test_library_search.py -v
```

Expected: all tests PASS.

```bash
python -m pytest tests/ -q
```

Expected: all existing tests still pass.

- [ ] **Step 6: Commit**

```bash
git add reviewer/app.py tests/test_library_search.py
git commit -m "feat(#141): library route — text/location/person search + date alias"
```

---

## Task 4: `reviewer/templates/library.html` — restructured filter UI

**Files:**
- Modify: `reviewer/templates/library.html`

No automated tests for the template — manual verification at end of task. The integration tests from Task 3 confirm the backend works; the template is UI only.

**Context:** After #142, the filter bar (lines ~187–218) contains a Time of year select and a ±2 days checkbox (with supporting JS at the bottom of `{% block content %}`). This task moves those controls into the new Filters panel and adds the search bar, location cascade, and person input. The untitled checkbox no longer has `onchange="this.form.submit()"`.

- [ ] **Step 1: Add CSS for the Filters panel**

In `{% block extra_style %}`, after the `.lib-filter-bar` block (around line 29), add:

```css
/* ── Filters panel (collapsible) ─────────────────── */
.lib-filter-panel {
  padding: 10px 16px 12px;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
}
.lib-filter-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}
.lib-filter-row label { font-size: 11px; color: var(--muted); }
.lib-filter-row input[type=text],
.lib-filter-row input[type=date],
.lib-filter-row select {
  background: var(--bg);
  border: 1px solid var(--border);
  color: var(--text);
  border-radius: var(--radius);
  padding: 4px 8px;
  font-size: 12px;
}
.lib-filter-footer {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px solid var(--border);
}
```

- [ ] **Step 2: Replace the `<form>` filter block with the new two-part layout**

Find and replace the entire `<form>` block (from `<form id="lib-filter-form"` to the closing `</form>` tag at line ~218). Replace with the following complete block:

```html
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
  (1 if filters.person else 0)
) %}
<form id="lib-filter-form" method="get" action="{{ url_for('library') }}">
<!-- Search bar (always visible, sticky) -->
<div class="lib-filter-bar">
  <input type="search" name="q" value="{{ filters.q }}"
         placeholder="Search photos…"
         style="flex:1;min-width:180px;background:var(--bg);border:1px solid var(--border);
                color:var(--text);border-radius:var(--radius);padding:4px 10px;font-size:12px">
  <button type="button" id="lib-filter-toggle"
          onclick="toggleFilterPanel()"
          style="background:var(--surface);border:1px solid var(--border);color:var(--text);
                 padding:4px 12px;border-radius:var(--radius);font-size:12px;cursor:pointer">
    Filters{% if filter_count %} ({{ filter_count }}){% endif %} ▾
  </button>
  {% if filters.q or filter_count > 0 %}
  <a href="{{ url_for('library') }}" style="font-size:12px;color:var(--muted)">Clear all</a>
  {% endif %}
  <span style="color:var(--muted);font-size:12px;margin-left:auto;white-space:nowrap">
    {{ total }} photo{{ 's' if total != 1 }}
  </span>
</div>

<!-- Collapsible filter panel (auto-opens when any non-q filter is active) -->
<div id="lib-filter-panel" class="lib-filter-panel"
     style="display:{% if filter_count > 0 %}block{% else %}none{% endif %}">

  <!-- Row 1: dates, album, tag, status, untitled -->
  <div class="lib-filter-row">
    <label>From <input type="date" name="date_from" value="{{ filters.date_from }}"></label>
    <label>To <input type="date" name="date_to" value="{{ filters.date_to }}"></label>
    <label>Album
      <select name="album_id">
        <option value="">All albums</option>
        {% for a in albums %}
          <option value="{{ a.id }}" {% if filters.album_id == a.id %}selected{% endif %}>{{ a.name }}</option>
        {% endfor %}
      </select>
    </label>
    <label>Tag <input type="text" name="tag" value="{{ filters.tag }}" placeholder="filter by tag…" style="width:120px"></label>
    <label>Status
      <select name="status">
        <option value="">All</option>
        <option value="public"  {% if filters.status == 'public'  %}selected{% endif %}>Public</option>
        <option value="private" {% if filters.status == 'private' %}selected{% endif %}>Private</option>
        <option value="pending" {% if filters.status == 'pending' %}selected{% endif %}>Pending</option>
      </select>
    </label>
    <label style="display:flex;align-items:center;gap:5px">
      <input type="checkbox" name="untitled" value="1" {% if filters.untitled %}checked{% endif %}>
      Untitled only
    </label>
  </div>

  <!-- Row 2: time pattern (moved here from #142's filter bar) -->
  <div class="lib-filter-row">
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
  </div>

  <!-- Row 3: location cascade -->
  <div class="lib-filter-row">
    <label>Country
      <select name="country" id="sel-country">
        <option value="">Any</option>
        {% for c in location_tree.keys()|sort %}
        <option value="{{ c }}" {% if filters.country == c %}selected{% endif %}>{{ c }}</option>
        {% endfor %}
      </select>
    </label>
    <label>State/Region
      <select name="state" id="sel-state">
        <option value="">Any</option>
      </select>
    </label>
    <label>City
      <select name="city" id="sel-city">
        <option value="">Any</option>
      </select>
    </label>
    <label>Neighborhood
      <select name="neighborhood" id="sel-neighborhood">
        <option value="">Any</option>
      </select>
    </label>
  </div>

  <!-- Row 4: person -->
  <div class="lib-filter-row">
    <label>Person
      <input type="text" name="person" value="{{ filters.person }}"
             list="person-datalist" placeholder="person name…" style="width:200px">
      <datalist id="person-datalist">
        {% for name in person_list %}
        <option value="{{ name }}">
        {% endfor %}
      </datalist>
    </label>
  </div>

  <!-- Panel footer -->
  <div class="lib-filter-footer">
    <button type="submit"
            style="background:var(--accent);border:none;color:white;
                   padding:4px 12px;border-radius:var(--radius);font-size:12px;cursor:pointer">
      Apply filters
    </button>
    {% if filter_count > 0 %}
    <a href="{{ url_for('library', q=filters.q) if filters.q else url_for('library') }}"
       style="font-size:12px;color:var(--muted)">Clear filters</a>
    {% endif %}
  </div>
</div>
</form>
```

- [ ] **Step 3: Update `_buildPayload` to include new filter fields**

Find the `_buildPayload` function (around line 600). Replace the `payload.filter` block:

```javascript
  if (_selectAllFilter) {
    const form = document.getElementById('lib-filter-form');
    const fd = new FormData(form);
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
    };
  }
```

- [ ] **Step 4: Add panel toggle + location cascade + expand JS**

Find the existing expand show/hide `<script>` block that #142 added at the bottom of `{% block content %}`. Replace it entirely with the following unified script (it merges the expand logic from #142 with the new toggle and cascade):

```html
<script>
const locationTree = {{ location_tree | tojson }};
const filters = {{ filters | tojson }};

// ── Filter panel toggle ──────────────────────────────────────────────────
function toggleFilterPanel() {
  const panel = document.getElementById('lib-filter-panel');
  panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
}

// ── Expand checkbox show/hide (time_pattern — from #142) ─────────────────
(function () {
  const sel = document.querySelector('[name="time_pattern"]');
  const lbl = document.getElementById('lib-expand-label');
  function syncExpand() {
    lbl.style.display = sel.value.startsWith('holiday:') ? 'flex' : 'none';
  }
  sel.addEventListener('change', syncExpand);
  syncExpand();
})();

// ── Location cascade ─────────────────────────────────────────────────────
const selCountry      = document.getElementById('sel-country');
const selState        = document.getElementById('sel-state');
const selCity         = document.getElementById('sel-city');
const selNeighborhood = document.getElementById('sel-neighborhood');

function rebuildSelect(sel, options, current) {
  sel.innerHTML = '<option value="">Any</option>';
  options.sort().forEach(opt => {
    const o = document.createElement('option');
    o.value = o.textContent = opt;
    if (opt === current) o.selected = true;
    sel.appendChild(o);
  });
}

selCountry.addEventListener('change', () => {
  rebuildSelect(selState, Object.keys(locationTree[selCountry.value] || {}), '');
  rebuildSelect(selCity, [], '');
  rebuildSelect(selNeighborhood, [], '');
});
selState.addEventListener('change', () => {
  const cities = (locationTree[selCountry.value] || {})[selState.value] || {};
  rebuildSelect(selCity, Object.keys(cities), '');
  rebuildSelect(selNeighborhood, [], '');
});
selCity.addEventListener('change', () => {
  const nbhds = ((locationTree[selCountry.value] || {})[selState.value] || {})[selCity.value] || [];
  rebuildSelect(selNeighborhood, nbhds, '');
});

// Initialise cascade from current filter values on page load
if (filters.country) {
  rebuildSelect(selState, Object.keys(locationTree[filters.country] || {}), filters.state);
}
if (filters.state) {
  const cities = (locationTree[filters.country] || {})[filters.state] || {};
  rebuildSelect(selCity, Object.keys(cities), filters.city);
}
if (filters.city) {
  const nbhds = ((locationTree[filters.country] || {})[filters.state] || {})[filters.city] || [];
  rebuildSelect(selNeighborhood, nbhds, filters.neighborhood);
}
</script>
```

- [ ] **Step 5: Run full test suite (no regressions)**

```bash
python -m pytest tests/ -q
```

Expected: all tests pass.

- [ ] **Step 6: Manual verification**

```bash
bp ui
```

- Navigate to `/library`. Confirm the search bar is visible at the top.
- Type "sunset" in the search box and press Enter — grid narrows to matching photos.
- Click "Filters ▾" — the panel opens below the search bar.
- Set Country → State → City and click Apply — cascade selects populate correctly as you pick each level.
- Select a holiday in Time of year — "±2 days" checkbox appears. Works as before.
- Set Person to a name from the datalist — grid narrows.
- Navigate to `/library?date=2023-10-15` — both date pickers populate to that date, grid shows only photos from that day.
- "Clear all" link clears all filters including q. "Clear filters" inside the panel preserves q.
- "Filters (3)" badge shows the correct count of active filters.
- "Select all" with active search filter — bulk action correctly reflects the filtered count (server-side via the updated bulk-edit route).

- [ ] **Step 7: Commit**

```bash
git add reviewer/templates/library.html
git commit -m "feat(#141): library.html — search bar, collapsible Filters panel, location cascade, person input"
```

---

## Task 5: README, docs, and issue close

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-05-25-library-search-141.md`

- [ ] **Step 1: Update README**

Run `python -m pytest tests/ -q` to get the exact test count (1221 + ~30 new tests from this issue). Update the test count line in README.

Add to the feature list near the library entry:

```markdown
- **Library search** — full-text search across titles, descriptions, tags, and Apple AI captions (`?q=`); 4-level location cascade (country → state → city → neighborhood); person filter with datalist autocomplete; `?date=YYYY-MM-DD` single-day alias (used by map "Show this day" link). All filters AND-combined and composable. Filter bar restructured to persistent search box + collapsible Filters panel.
```

- [ ] **Step 2: Mark spec done**

In `docs/superpowers/specs/2026-05-25-library-search-141.md`, change:

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

Expected: no new mypy errors. Fix any that appear (they'll be in the modified files — typically missing type annotations on new function params or return types).

- [ ] **Step 4: Commit**

```bash
git add README.md docs/superpowers/specs/2026-05-25-library-search-141.md
git commit -m "docs(#141): README + mark spec done"
```

- [ ] **Step 5: Close GH issue with retrospective**

```bash
gh issue close 141 --comment "Shipped.

**What was built:**
- \`db/photo_filters.py\` — pure module: \`build_text_clause\` (7 fields, LIKE + json_each), \`build_location_clause\` (4-level exact AND-cascade), \`build_person_clause\` (json_each exact), \`build_date_alias_clause\`
- \`db/db.py\` — \`location_data()\` (nested dict for cascade UI), \`person_names()\` (datalist), \`_library_where\` extended with 6 new params; all 3 library methods updated
- Library route: reads q/country/state/city/neighborhood/person/?date=; resolves date alias; passes location_tree and person_list to template; bulk-edit route updated
- Library template restructured: persistent search bar + collapsible Filters panel; location cascade JS; person datalist; time_pattern controls moved from bar into panel; ±2 days expand JS merged into unified script
- ~30 new tests

**Size estimate:** M (was labelled M — accurate)"
```

- [ ] **Step 6: Push**

```bash
git push origin main
```
