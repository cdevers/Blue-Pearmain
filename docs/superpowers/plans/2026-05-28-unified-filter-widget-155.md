# Unified Filter Widget Implementation Plan (#155)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> ⚠️ **Implementation note (2026-05-28):** Tasks 1 and 2 were implemented before the plan received external review — an error in the workflow. Tasks 1 (`_STATUS_STATES` refinement, commit `4844f06`) and 2 (`api_map_photos()` status filter, commit `a7c2cd8`) are **already done and tested** (1514 tests passing). External review of the full plan is happening now, before Tasks 3–8 are executed. If the reviewer identifies issues with Tasks 1–2, they will need to be addressed retroactively.

**Goal:** Extract the five shared filter dimensions (time pattern, year range, album, person, privacy) into a Jinja macro used by both `/library` and `/map`; give both pages identical instant-apply behaviour and cross-page navigation that preserves filter state.

**Architecture:** A new `_filter_bar.html` macro owns the canonical HTML for the five shared controls. `db.py`'s `_STATUS_STATES` dict is refined to give friends/family their own buckets. Both pages use `name=`-based form field access; `library.html` gains instant-apply JS (no Apply button); `map.html` collapses to a compact bar + collapsible panel; cross-page links carry all five shared params.

**Tech Stack:** Python/Flask, SQLite, Jinja2 macros, vanilla JS (`URLSearchParams`, `debounce`, `blur`/`keydown Enter` for year inputs).

**Future scope (not in this PR):** tag filtering is the obvious next dimension to add to the shared macro once this ships. When that work is planned, `normalize_shared_filters()` gains a `tag` field and the macro gets a tag input — no other routes need changing.

---

## Files

| Action | Path | What changes |
|--------|------|--------------|
| Create | `reviewer/templates/_filter_bar.html` | New shared Jinja macro |
| Modify | `db/db.py` | Narrow `_STATUS_STATES`; add `friends`, `family`, `friends_family` |
| Modify | `reviewer/app.py` | Library route: year params + person_names rename. `map_view()`: initial_filters. `api_map_photos()`: status filter |
| Modify | `reviewer/templates/library.html` | Use macro; instant-apply JS; chip row; View on map link; remove Apply button |
| Modify | `reviewer/templates/map.html` | Collapse bar; collapsible panel; macro; name-based JS; status; blur+Enter for year |
| Create | `tests/test_unified_filter.py` | New tests for all new behaviour |

---

## Filter lifecycle (state flow)

Each request follows this sequence. Implementors should be able to point to where their code sits in this chain.

1. **URL / request args** — all filter state lives in URL params (library) or JS reads from named form fields (map). No localStorage.
2. **Route normalization** — `normalize_shared_filters()` in `app.py` is the single normalization entry point for both routes. It parses ints, swaps year bounds if `year_from > year_to`, validates album_id, and strips whitespace. Both `library()` and `map_view()` call it. Implemented in Task 3.
3. **DB query** — `library_photos()` / `library_photo_ids()` receive cleaned `date_from`/`date_to`, `status`, `album_id`, `person`, `time_pattern`. SQL WHERE clauses built from these.
4. **Template render** — route passes `filters` dict (library) or `initial_filters` dict (map) to Jinja; macro uses these to pre-populate `selected`/`value` attributes. Rendered HTML always reflects canonical (normalised) state.
5. **JS hydration** — map JS reads field values via `document.querySelector('[name=X]')`; library JS reads from `libFilters` (JSON-serialised `filters`). Event listeners attached once.
6. **User interaction** — selects trigger immediately; text fields debounce (details below); year inputs fire on `blur`/`Enter` only.
7. **URL sync** — library: `buildLibraryUrl()` serializes all form fields → `location.href` (full reload; browser history push). Map: `buildMapUrl()` passes params to `fetch()`; URL bar updated via `history.replaceState()` (no extra history entries during exploration).

### Canonical URL invariant

Two semantically equivalent filter states must serialize to the same URL:
- **Year bounds** always stored in ascending order (`year_from ≤ year_to`). `normalize_shared_filters()` enforces this; rendered URLs always carry canonical bounds.
- **Empty params omitted.** `buildLibraryUrl()` skips blank values; `buildMapUrl()` only sets params when non-empty. Deep-links are minimal.
- **Unknown status values dropped.** `normalize_shared_filters()` validates against `_STATUS_STATES` keys; invalid values become `""` (no-filter).
- **Year→date conversion is one-way.** `year_from`/`year_to` appear in URLs; `date_from`/`date_to` (ISO strings) are internal route variables that do not re-appear in generated URLs.

### Normalization authority

`normalize_shared_filters()` (module-level, `app.py`) is the single source of truth for parsing, validation, and canonical order. Both `library()` and `map_view()` call it; neither duplicates parsing logic inline. `_MAP_STATUS_CLAUSES` in `api_map_photos()` still mirrors `_STATUS_STATES` — that is tracked technical debt and a candidate for the follow-up refactor.

---

## Interaction model invariants

| Control type | Library behaviour | Map behaviour |
|---|---|---|
| `<select>` (time, album, status) | navigate immediately | fetch immediately |
| `<input type=text>` (person) | debounce 500 ms | debounce 300 ms |
| `<input type=number>` (year) | fire on `blur` or `Enter` only | fire on `blur` or `Enter` only |
| `<input type=checkbox>` (untitled, no_location, confirmed_none) | navigate immediately | n/a |

**Year swap:** if `year_from > year_to`, the route swaps them before generating `date_from`/`date_to`. Because library instant-apply does a full page reload, the form re-renders with the canonical (swapped) URL values — the inputs self-correct visually. The map does the same: year values reach `buildMapUrl()` after the route has canonicalized them via `initial_filters`.

**Pagination reset:** every library filter mutation calls `params.delete('page')` before navigating. This is implemented in `buildLibraryUrl()` and is non-negotiable — applying a restrictive filter while on page 14 must not produce a 404.

**History:** library uses `location.href` (adds a browser history entry per filter application — acceptable for explicit choices; year blur+Enter prevents per-keystroke entries). Map uses `history.replaceState()` so exploration doesn't pollute history.

**In-flight requests:** library full-page reload means no cancellation is needed (browser aborts previous request automatically). Map's `reloadMarkers()` issues `fetch()` calls; concurrent responses may arrive out of order but the last one wins (acceptable given small response size; explicit AbortController cancellation is a future enhancement).

---

### Task 1: db.py — refine `_STATUS_STATES`

> ✅ **ALREADY IMPLEMENTED** (commit `4844f06`) — do NOT re-run Steps 1–6. This section is kept for reference and retroactive review only. Verify the implementation looks correct; if you spot a problem, fix it and note it — don't revert and redo.

> ⚠️ **Behaviour change:** `status=public` currently includes `approved_friends/family`. After this task it means strictly public only. No existing test creates friends/family photos and expects them in `public`, so existing tests are unaffected.

**Files:**
- Modify: `db/db.py:902-912` (the `_STATUS_STATES` dict)
- Test: `tests/test_unified_filter.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_unified_filter.py`:

```python
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

import reviewer.app as app_module
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
```

- [ ] **Step 2: Run to confirm failures**

```
python -m pytest tests/test_unified_filter.py::TestStatusValues -v
```

Expected: `test_public_is_strictly_public` FAILS (friends/family currently in public bucket), `test_friends` / `test_family` / `test_friends_family` FAIL (keys not in dict yet).

- [ ] **Step 3: Update `_STATUS_STATES` in `db/db.py`**

Find the dict at line ~902. Replace it entirely:

```python
_STATUS_STATES: dict[str, tuple[str, ...]] = {
    "public":         ("already_public", "approved_public"),
    "friends":        ("approved_friends",),
    "family":         ("approved_family",),
    "friends_family": ("approved_friends_family",),
    "private":        ("auto_private", "keep_private"),
    "pending":        ("needs_review", "candidate_public"),
}
```

- [ ] **Step 4: Run tests**

```
python -m pytest tests/test_unified_filter.py::TestStatusValues -v
```

Expected: all 7 tests PASS.

- [ ] **Step 5: Run full suite to confirm no regressions**

```
python -m pytest tests/ -q
```

Expected: all passing (existing `test_library_photos_status_public` uses `already_public` which stays in the public bucket).

- [ ] **Step 6: Commit**

```bash
git add db/db.py tests/test_unified_filter.py
git commit -m "feat(#155): refine _STATUS_STATES — friends/family own buckets, public strictly public

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 2: `api_map_photos()` — add `status` dataset filter

> ✅ **ALREADY IMPLEMENTED** (commit `a7c2cd8`) — do NOT re-run Steps 1–6. This section is kept for reference and retroactive review only.

**Files:**
- Modify: `reviewer/app.py` — `api_map_photos()` function
- Test: `tests/test_unified_filter.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_unified_filter.py`:

```python
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
```

- [ ] **Step 2: Run to confirm failures**

```
python -m pytest tests/test_unified_filter.py::TestMapStatusFilter -v
```

Expected: all 5 FAIL (status param not yet handled).

- [ ] **Step 3: Add `status` filter to `api_map_photos()` in `reviewer/app.py`**

Find the section after `person` filter and before `extra_where =` (around line 1004). Add:

```python
    # Status (privacy scope) — dataset-level filter; same semantics as library
    _MAP_STATUS_CLAUSES: dict[str, str] = {
        "public":         "p.privacy_state IN ('already_public','approved_public')",
        "friends":        "p.privacy_state = 'approved_friends'",
        "family":         "p.privacy_state = 'approved_family'",
        "friends_family": "p.privacy_state = 'approved_friends_family'",
        "private":        "p.privacy_state IN ('keep_private','auto_private')",
        "pending":        "p.privacy_state IN ('needs_review','candidate_public')",
    }
    map_status = (request.args.get("status") or "").strip()
    if map_status and map_status in _MAP_STATUS_CLAUSES:
        where_frags.append(_MAP_STATUS_CLAUSES[map_status])
        # No bound parameter — SQL literals only (all values are hard-coded)
```

Place this dict definition inside the function (or at module level — inside the function avoids polluting the module namespace and keeps it near its usage).

- [ ] **Step 4: Run tests**

```
python -m pytest tests/test_unified_filter.py::TestMapStatusFilter -v
```

Expected: all 5 PASS.

- [ ] **Step 5: Run full suite**

```
python -m pytest tests/ -q
```

Expected: all passing.

- [ ] **Step 6: Commit**

```bash
git add reviewer/app.py tests/test_unified_filter.py
git commit -m "feat(#155): api_map_photos — status dataset filter (affects dots + trail)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 3: library route — `normalize_shared_filters()` + year_from/year_to + person_names rename

**Files:**
- Modify: `reviewer/app.py` — new module-level function + `library()` function (~lines 1047–1195)
- Test: `tests/test_unified_filter.py`

- [ ] **Step 1: Write failing normalization tests**

Add to `tests/test_unified_filter.py` (before the library year section):

```python
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
```

- [ ] **Step 2: Run to confirm failures**

```
python -m pytest tests/test_unified_filter.py::TestNormalizeSharedFilters -v
```

Expected: all 5 FAIL (`normalize_shared_filters` not defined yet).

- [ ] **Step 3: Implement `normalize_shared_filters()` in `reviewer/app.py`**

Add this function at module level, immediately after `_safe_year()`:

```python
from typing import TypedDict

class SharedFilters(TypedDict):
    time_pattern: str
    year_from: int | None
    year_to: int | None
    album_id: int | None
    person: str
    status: str
    expand: str


def normalize_shared_filters() -> SharedFilters:
    """Parse and normalize the five shared filter params from request.args.

    Single normalization entry point for both library() and map_view().
    Centralizes: int parsing, year-bound swap, status validation, empty-string
    normalization. Call within a Flask request context.
    """
    from db.db import _STATUS_STATES  # import here to keep at function level

    year_from = _safe_year("year_from")
    year_to   = _safe_year("year_to")
    if year_from is not None and year_to is not None and year_from > year_to:
        year_from, year_to = year_to, year_from

    album_id: int | None = None
    raw_album = (request.args.get("album_id") or "").strip()
    if raw_album:
        try:
            album_id = int(raw_album)
        except ValueError:
            pass

    raw_status = (request.args.get("status") or "").strip()
    status = raw_status if raw_status in _STATUS_STATES else ""

    return SharedFilters(
        time_pattern=(request.args.get("time_pattern") or "").strip(),
        year_from=year_from,
        year_to=year_to,
        album_id=album_id,
        person=(request.args.get("person") or "").strip(),
        status=status,
        expand=(request.args.get("expand") or "").strip(),
    )
```

> **Note on `_STATUS_STATES` import:** `db.py` is already imported in `app.py` as `from db.db import Database`. Check whether `_STATUS_STATES` is exported (it starts with `_` so it's technically private). If it isn't directly importable, use `db.db._STATUS_STATES` or expose a `STATUS_KEYS` constant. The simplest fix is a module-level `set` in `app.py`: `_VALID_STATUSES = frozenset(["public","friends","family","friends_family","private","pending"])`.

- [ ] **Step 4: Run normalization tests**

```
python -m pytest tests/test_unified_filter.py::TestNormalizeSharedFilters -v
```

Expected: all 5 PASS.

- [ ] **Step 5: Write failing library year tests**

Add to `tests/test_unified_filter.py`:

```python
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
    import json
    # Library returns HTML; check for photo IDs in data-id attributes
    body = resp.data.decode()
    import re
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
        # Explicit date_from=2020-01-01 takes priority over year_from=2016
        resp = c.get("/library?date_from=2020-01-01&year_from=2016")
        ids = _lib_ids(resp)
        assert p16 not in ids  # date_from wins
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
```

> **Note:** The JS `buildLibraryUrl()` always calls `params.delete('page')`, so pagination is reset on every filter change. There is no server-side test for this (it's a client-side invariant enforced in JS), but it is an explicit design invariant — any filter mutation must land on page 1.

- [ ] **Step 6: Run to confirm failures**

```
python -m pytest tests/test_unified_filter.py::TestLibraryYearFilter -v
```

Expected: all 7 FAIL (`year_from`/`year_to` not parsed by library route yet).

- [ ] **Step 7: Update `library()` route in `reviewer/app.py` to use `normalize_shared_filters()`**

Replace the inline year/album/person parsing in `library()` with a call to `normalize_shared_filters()`. In the `library()` function, after the `date_from`/`date_to` lines, add:

```python
    # Shared filter normalization — single canonical entry point
    sf = normalize_shared_filters()
    # Apply year→ISO date only if no explicit date_from/date_to was provided
    if sf["year_from"] is not None and not date_from:
        date_from = f"{sf['year_from']:04d}-01-01"
    if sf["year_to"] is not None and not date_to:
        date_to = f"{sf['year_to'] + 1:04d}-01-01T00:00:00"
```

Then use `sf["album_id"]`, `sf["person"]`, `sf["status"]`, `sf["time_pattern"]`, `sf["expand"]` where the route previously parsed them individually (search for `album_id = request.args.get(...)`, `person = request.args.get(...)`, etc.).

(`_safe_year` is already defined at module level in `app.py` from #154; `normalize_shared_filters()` calls it internally, so no direct calls to `_safe_year` are needed in `library()` after this change.)

Then in the `render_template` call, rename the template variable and add year fields. Find:

```python
        person_list=person_list,
```

Change to:

```python
        person_names=person_list,
```

And in the `filters` dict, add (using the `sf` dict from `normalize_shared_filters()`):

```python
            "year_from": sf["year_from"] if sf["year_from"] is not None else "",
            "year_to":   sf["year_to"]   if sf["year_to"]   is not None else "",
```

- [ ] **Step 8: Run tests**

```
python -m pytest tests/test_unified_filter.py::TestLibraryYearFilter -v
```

Expected: all 7 PASS.

- [ ] **Step 9: Run full suite**

```
python -m pytest tests/ -q
```

Expected: all passing. (The one `person_list` reference in `library.html` — `{% for name in person_list %}` at line ~426 — will generate a Jinja `UndefinedError` only when the template is rendered. Template tests in `test_library_page_data.py` will catch this if they render the page. Run and check.)

If template tests fail with `UndefinedError: person_list`, fix now by updating `library.html` line ~426: change `person_list` → `person_names`. Then re-run. (This reference will be removed entirely in Task 6 when the macro takes over, but patching it now keeps tests green.)

- [ ] **Step 10: Commit**

```bash
git add reviewer/app.py tests/test_unified_filter.py
git commit -m "feat(#155): normalize_shared_filters() + library route year params + person_names rename

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 4: `map_view()` route — `initial_filters` for deep-linking

**Files:**
- Modify: `reviewer/app.py` — `map_view()` function (~lines 840–900)
- Test: `tests/test_unified_filter.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_unified_filter.py`:

```python
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
        resp = c.get("/map?time_pattern=month:08&year_from=2015&year_to=2019"
                     "&person=Marcin&status=public")
        assert resp.status_code == 200
        body = resp.data.decode()
        # The macro pre-populates form fields from initial_filters —
        # check that the values appear as option/input values in the HTML
        assert 'value="2015"' in body
        assert 'value="2019"' in body
        assert 'value="Marcin"' in body
```

- [ ] **Step 2: Run to confirm failure**

```
python -m pytest tests/test_unified_filter.py::TestMapViewInitialFilters -v
```

Expected: FAIL (map_view doesn't pass initial_filters; year/person values not in HTML).

- [ ] **Step 3: Update `map_view()` in `reviewer/app.py`**

In `map_view()`, before the `return render_template(...)` call, add:

```python
    # Parse shared filter params via the single normalization entry point
    sf = normalize_shared_filters()
    initial_filters = {
        "time_pattern": sf["time_pattern"],
        "year_from":    sf["year_from"] if sf["year_from"] is not None else "",
        "year_to":      sf["year_to"]   if sf["year_to"]   is not None else "",
        "album_id":     sf["album_id"],
        "person":       sf["person"],
        "status":       sf["status"],
        "expand":       sf["expand"],
    }
```

Then add `initial_filters=initial_filters` to the `render_template("map.html", ...)` call.

> `normalize_shared_filters()` replaces the inline `album_id` parsing that was here in earlier plan drafts. Both routes now share the same normalization path.

- [ ] **Step 4: Run tests**

```
python -m pytest tests/test_unified_filter.py::TestMapViewInitialFilters -v
```

Expected: `xfail` — the test is marked `@pytest.mark.xfail` because the body content assertions require the shared macro (Task 7). The route itself works; the assertion checks HTML that isn't rendered until Task 7. `xfail` is a clean pass in the suite.

- [ ] **Step 5: Run full suite**

```
python -m pytest tests/ -q
```

Expected: all passing (`xfail` counts as passing).

- [ ] **Step 6: Commit**

```bash
git add reviewer/app.py tests/test_unified_filter.py
git commit -m "feat(#155): map_view — initial_filters for deep-link pre-population

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 5: Create `_filter_bar.html` shared Jinja macro

**Files:**
- Create: `reviewer/templates/_filter_bar.html`

- [ ] **Step 1: Create the macro file**

```bash
touch reviewer/templates/_filter_bar.html
```

- [ ] **Step 2: Write the macro**

Write `reviewer/templates/_filter_bar.html` with this exact content:

```jinja
{#
  _filter_bar.html — shared filter macro (#155)

  Usage:
    {% from "_filter_bar.html" import filter_bar %}
    {{ filter_bar(albums, person_names, filters, datalist_id="lib-persons") }}
    {{ filter_bar(albums, person_names, initial_filters, datalist_id="map-persons") }}

  Parameters:
    albums       — list of dicts with keys: id (int), name (str)
    person_names — sorted list of named persons (strings); _UNKNOWN_ excluded
    filters      — dict with keys: time_pattern, year_from, year_to,
                   album_id (int|None), person, status
    datalist_id  — unique ID for the <datalist> element; must differ per page
                   to avoid duplicate-ID collisions (default: "shared-persons")
#}
{% macro filter_bar(albums, person_names, filters, datalist_id="shared-persons") %}
<div class="shared-filter-bar">

  <label>Time of year
    <select name="time_pattern">
      <option value="">— any time —</option>
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

  <label>Album
    <select name="album_id">
      <option value="">— any album —</option>
      {% for a in albums %}
      <option value="{{ a.id }}"
              {% if filters.album_id == a.id or (filters.album_id | string) == (a.id | string) %}selected{% endif %}>
        {{ a.name | e }}
      </option>
      {% endfor %}
    </select>
  </label>

  <label>Person
    <input type="text" name="person" value="{{ filters.person or '' }}"
           list="{{ datalist_id }}" placeholder="person name…" style="width:160px">
    <datalist id="{{ datalist_id }}">
      {% for name in person_names %}
      <option value="{{ name | e }}">
      {% endfor %}
    </datalist>
  </label>

  <label>Privacy
    <select name="status">
      <option value="">— any —</option>
      <option value="public"         {% if filters.status == 'public'         %}selected{% endif %}>Public</option>
      <option value="friends"        {% if filters.status == 'friends'        %}selected{% endif %}>Friends</option>
      <option value="family"         {% if filters.status == 'family'         %}selected{% endif %}>Family</option>
      <option value="friends_family" {% if filters.status == 'friends_family' %}selected{% endif %}>Friends &amp; Family</option>
      <option value="private"        {% if filters.status == 'private'        %}selected{% endif %}>Private</option>
      <option value="pending"        {% if filters.status == 'pending'        %}selected{% endif %}>Pending review</option>
    </select>
  </label>

</div>
{% endmacro %}
```

- [ ] **Step 3: Confirm the file parses as valid Jinja**

```bash
python -c "
from jinja2 import Environment, FileSystemLoader
env = Environment(loader=FileSystemLoader('reviewer/templates'))
env.get_template('_filter_bar.html')
print('OK')
"
```

Expected output: `OK`

- [ ] **Step 4: Commit**

```bash
git add reviewer/templates/_filter_bar.html
git commit -m "feat(#155): add shared _filter_bar.html Jinja macro (5 shared filter controls)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 6: `library.html` — use macro, instant-apply, chip row, View on map

**Files:**
- Modify: `reviewer/templates/library.html`
- Test: `tests/test_unified_filter.py`

- [ ] **Step 1: Write failing template tests**

Add to `tests/test_unified_filter.py`:

```python
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
        # View on map link must include the active shared filters
        assert "/map" in body
        assert "time_pattern=month%3A08" in body or "time_pattern=month:08" in body
        assert "year_from=2015" in body
        assert "person=Alice" in body

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
        resp = c.get("/library?time_pattern=month:08&year_from=2015&year_to=2019"
                     "&person=Alice+W&status=public")
        assert resp.status_code == 200
        body = resp.data.decode()
        # The "View on map" link must include each shared filter param
        import re
        map_links = re.findall(r'href="(/map[^"]*)"', body)
        assert map_links, "No /map link found in library response"
        map_url = map_links[0]
        assert "time_pattern" in map_url
        assert "year_from=2015" in map_url
        assert "year_to=2019" in map_url
        assert "Alice" in map_url
        assert "status=public" in map_url
```

- [ ] **Step 2: Run to confirm failures**

```
python -m pytest tests/test_unified_filter.py::TestLibraryTemplateIntegration -v
```

Expected: most FAIL.

- [ ] **Step 3: Update `library.html` — filter_count, macro import, panel restructure**

Make the following changes to `reviewer/templates/library.html`:

**3a. Update `filter_count` (around line 233)** — add year fields:

```jinja
{% set filter_count = (
  (1 if filters.date_from or filters.date_to
        or filters.year_from or filters.year_to else 0) +
  (1 if filters.album_id else 0) +
  (1 if filters.tag else 0) +
  (1 if filters.status else 0) +
  (1 if filters.untitled else 0) +
  (1 if filters.no_location else 0) +
  (1 if filters.confirmed_none else 0) +
  (1 if filters.time_pattern else 0) +
  (1 if filters.country else 0) +
  (1 if filters.state else 0) +
  (1 if filters.city else 0) +
  (1 if filters.neighborhood else 0) +
  (1 if filters.person else 0) +
  (1 if filters.lat_min else 0)
) %}
```

> `date_from`/`date_to` and `year_from`/`year_to` are the same conceptual filter ("date range"); they're combined into one counter so the badge doesn't inflate when both are set by the route layer.

**3b. Add macro import** directly before the `<form id="lib-filter-form"...>` tag:

```jinja
{% from "_filter_bar.html" import filter_bar %}
```

**3c. Inside the collapsible panel (`<div id="lib-filter-panel"...>`), add the shared macro as the first row**, immediately after any existing spatial bbox remove link:

```jinja
  <!-- Row 0: shared filter macro (time · year · album · person · privacy) -->
  <div class="lib-filter-row">
    {{ filter_bar(albums, person_names, filters, datalist_id="lib-persons") }}
  </div>
```

**3d. From the existing Row 1** (around line 303: "dates, album, tag, status, untitled"), **remove**:
- The entire `<label>Album ...` block (the select with all album options)
- The entire `<label>Status ...` block (the select with public/private/pending)

Keep: `<label>From ...`, `<label>To ...`, `<label>Tag ...`, `<label style="...">Untitled only`, `<label ...>No location ...`, `<label ...>Reviewed: no location ...`

**3e. Remove the entire Row 2** (time pattern select + expand checkbox) — it is now in the shared macro.

**3f. Keep Row 3** (location cascade) — no changes needed.

**3g. Remove the entire Row 4** (person text input + datalist).

**3h. Update the filter panel footer** — remove the "Apply filters" button; keep only the "Clear filters" link:

```jinja
  <div class="lib-filter-footer">
    {% if filter_count > 0 or filters.q %}
    <a href="{{ url_for('library', q=filters.q) if filters.q else url_for('library') }}"
       style="font-size:12px;color:var(--muted)">Clear filters</a>
    {% endif %}
  </div>
```

**3i. Add "View on map" link** in the `.lib-filter-bar` div, after the "Filters (N) ▾" toggle button:

```jinja
  <a href="{{ url_for('map_view',
    time_pattern=filters.time_pattern or None,
    year_from=filters.year_from or None,
    year_to=filters.year_to or None,
    album_id=filters.album_id or None,
    person=filters.person or None,
    status=filters.status or None) }}"
     class="map-btn" style="font-size:12px;padding:4px 8px"
     title="View current filter on map">🗺 Map</a>
```

**3j. Add chip row** between the `.lib-filter-bar` div and the photo grid (before `<div class="lib-select-bar">`):

```html
<div id="lib-filter-chips" class="lib-filter-chips"></div>
```

Add CSS (in the `<style>` block at the top of the template):

```css
.lib-filter-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
  padding: 4px 12px;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
}
.lib-filter-chips:empty { display: none; }
.lib-chip {
  font-size: 11px;
  padding: 2px 8px;
  background: var(--border);
  color: var(--text);
  border-radius: 10px;
  max-width: 180px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
```

**3k. Before adding the new JS block: search `library.html` for any pre-existing event listeners on `time_pattern`, `album_id`, `status`, `tag`, `untitled`, `no_location`, `confirmed_none`.** If any exist (from before this refactor), remove them now. Adding new listeners on top of old ones causes duplicate navigation and racey URL mutations. Full-page reloads mask the symptom — check the source, not the behaviour.

Then add instant-apply JS + chip row JS in the `<script>` block near the bottom of the file (before `</script>` of the second script block):

```js
// ── Instant-apply filter navigation (#155) ───────────────────────────────

function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

function buildLibraryUrl() {
  // Serializes all named form fields generically. Intentional: any new filter
  // field added to lib-filter-form (shared macro or library-specific) automatically
  // participates in URL persistence and navigation without touching this function.
  const form = document.getElementById('lib-filter-form');
  const params = new URLSearchParams();
  for (const el of form.elements) {
    if (!el.name) continue;
    if (el.type === 'checkbox') { if (el.checked) params.set(el.name, el.value); }
    else if (el.value) params.set(el.name, el.value);
  }
  params.delete('page');  // reset to page 1 on filter change
  return '/library?' + params.toString();
}

function applyLibraryFilter() {
  location.href = buildLibraryUrl();
}

const _libDebounced = debounce(applyLibraryFilter, 500);

// Shared macro fields: selects immediate, person debounced
const _tmSel = document.querySelector('[name=time_pattern]');
if (_tmSel) _tmSel.addEventListener('change', applyLibraryFilter);
const _alSel = document.querySelector('[name=album_id]');
if (_alSel) _alSel.addEventListener('change', applyLibraryFilter);
const _stSel = document.querySelector('[name=status]');
if (_stSel) _stSel.addEventListener('change', applyLibraryFilter);
const _perIn = document.querySelector('[name=person]');
if (_perIn) _perIn.addEventListener('input', _libDebounced);

// Year inputs: fire on blur or Enter only (avoids intermediate-state reloads)
for (const el of document.querySelectorAll('[name=year_from],[name=year_to]')) {
  el.addEventListener('blur', applyLibraryFilter);
  el.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); applyLibraryFilter(); } });
}

// Library-specific fields: immediate
const _tagIn = document.querySelector('[name=tag]');
if (_tagIn) _tagIn.addEventListener('input', _libDebounced);
for (const cb of document.querySelectorAll('[name=untitled],[name=no_location],[name=confirmed_none]'))
  cb.addEventListener('change', applyLibraryFilter);
// status select in library-specific row is now in the macro — already handled above

// ── Filter chip row (#155) ────────────────────────────────────────────────
(function () {
  const STATUS_LABELS = {
    public: 'Public', friends: 'Friends', family: 'Family',
    friends_family: 'Friends & Family', private: 'Private', pending: 'Pending',
  };
  const chips = [];
  const f = libFilters;  // populated earlier: const libFilters = {{ filters | tojson }};

  if (f.time_pattern) {
    const sel = document.querySelector('[name=time_pattern]');
    const label = sel ? sel.options[sel.selectedIndex]?.text : f.time_pattern;
    chips.push(label || f.time_pattern);
  }
  if (f.year_from && f.year_to) chips.push(`${f.year_from}–${f.year_to}`);
  else if (f.year_from) chips.push(`from ${f.year_from}`);
  else if (f.year_to)   chips.push(`to ${f.year_to}`);
  if (f.album_id) {
    const sel = document.querySelector('[name=album_id]');
    const label = sel ? sel.options[sel.selectedIndex]?.text : String(f.album_id);
    chips.push(label || String(f.album_id));
  }
  if (f.person)  chips.push(f.person);
  if (f.status)  chips.push(STATUS_LABELS[f.status] || f.status);
  if (f.date_from || f.date_to) {
    chips.push(`${f.date_from || '…'} → ${f.date_to ? f.date_to.slice(0,10) : '…'}`);
  }
  if (f.tag)     chips.push(`#${f.tag}`);
  if (f.country || f.state || f.city) {
    chips.push([f.city, f.state, f.country].filter(Boolean).join(', '));
  }

  const container = document.getElementById('lib-filter-chips');
  if (container && chips.length) {
    container.innerHTML = chips
      .map(t => `<span class="lib-chip">${t.replace(/&/g,'&amp;').replace(/</g,'&lt;')}</span>`)
      .join('');
  }
})();
```

> **Note:** `libFilters` is already set earlier in the template as `const libFilters = {{ filters | tojson }};`. The chip JS relies on that existing variable.

- [ ] **Step 4: Update `_buildPayload` in `library.html`** — convert year params to ISO dates for the bulk endpoint (search for `_buildPayload` around line 837).

> The `/api/bulk-edit` endpoint calls `library_photo_ids()` which accepts `date_from`/`date_to` but has no `year_from`/`year_to` parameter. Sending raw year ints would silently be ignored, leaving bulk ops to apply to the wrong photo set. We convert year→ISO date client-side here, mirroring the same logic the library route already does server-side.

```js
    // Convert year_from/year_to → date_from/date_to for the bulk endpoint,
    // which uses library_photo_ids() and only understands ISO date strings.
    // Explicit date_from/date_to (if set) take priority.
    let _yearFrom = fd.get('year_from') ? parseInt(fd.get('year_from'), 10) : null;
    let _yearTo   = fd.get('year_to')   ? parseInt(fd.get('year_to'),   10) : null;
    if (_yearFrom !== null && _yearTo !== null && _yearFrom > _yearTo) {
      [_yearFrom, _yearTo] = [_yearTo, _yearFrom];
    }
    const _dateFrom = fd.get('date_from') ||
                      (_yearFrom !== null ? String(_yearFrom).padStart(4,'0') + '-01-01' : null);
    const _dateTo   = fd.get('date_to') ||
                      (_yearTo   !== null ? String(_yearTo + 1).padStart(4,'0') + '-01-01T00:00:00' : null);

    payload.filter = {
      date_from:    _dateFrom,
      date_to:      _dateTo,
      album_id:     fd.get('album_id') ? parseInt(fd.get('album_id'), 10) : null,
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

- [ ] **Step 5: Run template tests**

```
python -m pytest tests/test_unified_filter.py::TestLibraryTemplateIntegration -v
python -m pytest tests/ -q
```

Expected: `TestLibraryTemplateIntegration` tests pass (except `test_shared_macro_in_map` which depends on Task 7). Full suite passes.

- [ ] **Step 6: Commit**

```bash
git add reviewer/templates/library.html
git commit -m "feat(#155): library.html — shared macro, instant-apply JS, chip row, View on map

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 7: `map.html` — collapse bar, collapsible panel, macro, JS updates

**Files:**
- Modify: `reviewer/templates/map.html`

- [ ] **Step 1: Replace the HTML filter bar**

In `map.html`, replace the entire `<div class="map-filter-bar">` block (from the opening div through `<div id="map-filter-chips" ...></div>` and the closing `</div>`) with:

```html
<div class="map-filter-bar">
  <!-- Compact always-visible bar -->
  <div class="map-filter-row" id="map-compact-bar">
    <button type="button" id="map-filter-toggle" class="map-btn" onclick="toggleMapPanel()">
      Filters<span id="map-filter-badge"></span> ▾
    </button>
    <span style="flex:1"></span>
    <label style="display:flex;align-items:center;gap:5px">
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

  <!-- Collapsible filter panel (hidden via CSS; .is-open shows it) -->
  {% from "_filter_bar.html" import filter_bar %}
  <div id="map-filter-panel" class="map-filter-panel">
    {{ filter_bar(albums, person_names, initial_filters, datalist_id="map-persons") }}

    <!-- Map-specific: expand toggle + animation privacy -->
    <div style="display:flex;align-items:center;gap:12px;padding:4px 0">
      <label id="map-expand-label" style="display:none;align-items:center;gap:5px">
        <input type="checkbox" name="expand" id="map-expand-cb"
               {% if initial_filters.expand %}checked{% endif %}> ±2 days
      </label>
      <label style="margin-left:auto;align-items:center;gap:5px">▶ Animate:
        <select id="map-privacy-select">
          <option value="all">All photos</option>
          <option value="public">Public only</option>
          <option value="private">Private only</option>
        </select>
      </label>
    </div>
  </div>

  <!-- Active filter chips (hidden when empty via :empty selector) -->
  <div class="map-filter-chips" id="map-filter-chips"></div>
</div>
```

Add CSS for the new panel (in the `<style>` block at the top of `map.html`):

```css
.map-filter-panel {
  display: none;  /* hidden by default; JS adds .is-open to show */
  padding: 8px 12px;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  flex-direction: column;
  gap: 8px;
}
.map-filter-panel.is-open {
  display: flex;
}
.shared-filter-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  gap: 8px;
}
```

- [ ] **Step 2: Update `_hasAnyFilter()` — name-based access + status**

Replace the existing `_hasAnyFilter()` function:

```js
function _hasAnyFilter() {
  if ((document.querySelector('[name=time_pattern]')?.value || '')) return true;
  if ((document.querySelector('[name=year_from]')?.value || '').trim()) return true;
  if ((document.querySelector('[name=year_to]')?.value || '').trim()) return true;
  if ((document.querySelector('[name=album_id]')?.value || '')) return true;
  if ((document.querySelector('[name=person]')?.value || '').trim()) return true;
  if ((document.querySelector('[name=status]')?.value || '')) return true;
  return false;
}
```

- [ ] **Step 3: Update `_updateFilterChips()` — name-based access + status chip**

Replace the existing `_updateFilterChips()` function:

```js
function _updateFilterChips() {
  const container = document.getElementById('map-filter-chips');
  if (!container) return;
  const chips = [];

  const patternEl = document.querySelector('[name=time_pattern]');
  if (patternEl?.value) {
    chips.push({ text: patternEl.options[patternEl.selectedIndex].text, cls: 'map-chip' });
  }

  const yf = (document.querySelector('[name=year_from]')?.value || '').trim();
  const yt = (document.querySelector('[name=year_to]')?.value || '').trim();
  if (yf && yt)     chips.push({ text: `${yf}–${yt}`, cls: 'map-chip' });
  else if (yf)      chips.push({ text: `from ${yf}`, cls: 'map-chip' });
  else if (yt)      chips.push({ text: `to ${yt}`, cls: 'map-chip' });

  const person = (document.querySelector('[name=person]')?.value || '').trim();
  if (person) chips.push({ text: person, cls: 'map-chip' });

  const albumEl = document.querySelector('[name=album_id]');
  if (albumEl?.value) {
    chips.push({ text: albumEl.options[albumEl.selectedIndex].text, cls: 'map-chip' });
  }

  const statusEl = document.querySelector('[name=status]');
  if (statusEl?.value) {
    chips.push({ text: statusEl.options[statusEl.selectedIndex].text, cls: 'map-chip' });
  }

  const privacyEl = document.getElementById('map-privacy-select');
  if (privacyEl?.value !== 'all') {
    chips.push({ text: `▶ ${privacyEl.options[privacyEl.selectedIndex].text}`, cls: 'map-chip map-chip-anim' });
  }

  container.innerHTML = chips
    .map(c => `<span class="${c.cls}">${esc(c.text)}</span>`)
    .join('');
}
```

- [ ] **Step 4: Update `buildMapUrl()` — name-based access + status**

Replace the existing `buildMapUrl()` function:

```js
function buildMapUrl() {
  const params = new URLSearchParams();
  const pattern = document.querySelector('[name=time_pattern]')?.value || '';
  if (pattern) params.set('time_pattern', pattern);
  if (document.getElementById('map-expand-cb')?.checked) params.set('expand', '1');
  const yf = (document.querySelector('[name=year_from]')?.value || '').trim();
  const yt = (document.querySelector('[name=year_to]')?.value || '').trim();
  if (yf) params.set('year_from', yf);
  if (yt) params.set('year_to', yt);
  const album = document.querySelector('[name=album_id]')?.value || '';
  if (album) params.set('album_id', album);
  const person = (document.querySelector('[name=person]')?.value || '').trim();
  if (person) params.set('person', person);
  const status = document.querySelector('[name=status]')?.value || '';
  if (status) params.set('status', status);
  // Sync URL bar without adding a browser history entry (exploration ≠ navigation)
  const s = params.toString();
  history.replaceState(null, '', s ? `/map?${s}` : '/map');
  return s ? `/api/map-photos?${s}` : '/api/map-photos';
}
```

- [ ] **Step 5: Update `openInLibrary()` — name-based access + status + _regionBounds**

Replace the existing `openInLibrary()` function:

```js
function openInLibrary() {
  const bounds = _regionBounds || map.getBounds();
  const params = new URLSearchParams({
    lat_min: bounds.getSouth().toFixed(5),
    lat_max: bounds.getNorth().toFixed(5),
    lon_min: bounds.getWest().toFixed(5),
    lon_max: bounds.getEast().toFixed(5),
  });
  const tp = document.querySelector('[name=time_pattern]')?.value || '';
  if (tp) params.set('time_pattern', tp);
  if (document.getElementById('map-expand-cb')?.checked) params.set('expand', '1');
  const yf = (document.querySelector('[name=year_from]')?.value || '').trim();
  const yt = (document.querySelector('[name=year_to]')?.value || '').trim();
  if (yf) params.set('year_from', yf);
  if (yt) params.set('year_to', yt);
  const album = document.querySelector('[name=album_id]')?.value || '';
  if (album) params.set('album_id', album);
  const person = (document.querySelector('[name=person]')?.value || '').trim();
  if (person) params.set('person', person);
  const status = document.querySelector('[name=status]')?.value || '';
  if (status) params.set('status', status);
  window.open('/library?' + params.toString(), '_blank');
}
```

- [ ] **Step 6: Update event listeners — name-based access, year blur+Enter, toggleMapPanel, badge update**

Replace the entire "Event listeners" block (from `document.getElementById('map-time-select').addEventListener` through `reloadMarkers(); // initial load`):

```js
// ── Panel toggle ──────────────────────────────────────────────────────────
function toggleMapPanel() {
  document.getElementById('map-filter-panel').classList.toggle('is-open');
}

function _updateFilterBadge() {
  const badge = document.getElementById('map-filter-badge');
  if (!badge) return;
  let n = 0;
  if (document.querySelector('[name=time_pattern]')?.value) n++;
  if ((document.querySelector('[name=year_from]')?.value || '').trim()) n++;
  if ((document.querySelector('[name=year_to]')?.value || '').trim()) n++;
  if (document.querySelector('[name=album_id]')?.value) n++;
  if ((document.querySelector('[name=person]')?.value || '').trim()) n++;
  if (document.querySelector('[name=status]')?.value) n++;
  badge.textContent = n ? ` (${n})` : '';
}

// ── Shared filter control listeners ──────────────────────────────────────
document.querySelector('[name=time_pattern]')?.addEventListener('change', function () {
  const lbl = document.getElementById('map-expand-label');
  const cb  = document.getElementById('map-expand-cb');
  const isHoliday = this.value.startsWith('holiday:');
  lbl.style.display = isHoliday ? 'flex' : 'none';
  if (!isHoliday && cb) cb.checked = false;

  if (!_hasAnyFilter()) {
    document.getElementById('map-trail-cb').checked = false;
    stopAnimation();
    if (_trailLayer) { map.removeLayer(_trailLayer); _trailLayer = null; }
  }
  reloadMarkers();
  _updateFilterChips();
  _updateFilterBadge();
});

document.querySelector('[name=album_id]')?.addEventListener('change', () => {
  reloadMarkers(); _updateFilterChips(); _updateFilterBadge();
});

document.querySelector('[name=status]')?.addEventListener('change', () => {
  reloadMarkers(); _updateFilterChips(); _updateFilterBadge();
});

// Person: debounced on input
const _debouncedReload = debounce(() => {
  reloadMarkers(); _updateFilterChips(); _updateFilterBadge();
}, 300);
document.querySelector('[name=person]')?.addEventListener('input', _debouncedReload);

// Year: blur + Enter only
for (const el of document.querySelectorAll('[name=year_from],[name=year_to]')) {
  el.addEventListener('blur', () => { reloadMarkers(); _updateFilterChips(); _updateFilterBadge(); });
  el.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); reloadMarkers(); _updateFilterChips(); _updateFilterBadge(); }
  });
}

document.getElementById('map-expand-cb')?.addEventListener('change', reloadMarkers);

document.getElementById('map-trail-cb').addEventListener('change', () => {
  plotTrail(_lastPhotos);
  _updateAnimateBtn();
});

document.getElementById('map-privacy-select').addEventListener('change', () => {
  _updateAnimateBtn();
  _updateFilterChips();
});

// Auto-open panel if any filter was set via initial_filters (deep-link)
if (_hasAnyFilter()) {
  document.getElementById('map-filter-panel').classList.add('is-open');
}

reloadMarkers();
_updateFilterChips();
_updateFilterBadge();
```

- [ ] **Step 7: Remove the transitional `xfail` marker from `TestMapViewInitialFilters`**

In `tests/test_unified_filter.py`, remove the `@pytest.mark.xfail(...)` decorator from `test_map_view_passes_initial_filters_to_template`. The macro is now in place and the test should pass unconditionally.

- [ ] **Step 8: Run template integration tests**

```
python -m pytest tests/test_unified_filter.py::TestLibraryTemplateIntegration::test_shared_macro_in_map -v
python -m pytest tests/test_unified_filter.py::TestMapViewInitialFilters -v
```

Expected: both PASS now (no xfail, straight pass).

- [ ] **Step 9: Run full suite**

```
python -m pytest tests/ -q
```

Expected: all passing.

- [ ] **Step 10: Commit**

```bash
git add reviewer/templates/map.html tests/test_unified_filter.py
git commit -m "feat(#155): map.html — compact bar + collapsible panel, shared macro, name-based JS

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 8: Final verification — lint, README, issue, push, version bump

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-05-28-unified-filter-widget-155.md`
- Modify: `docs/superpowers/plans/2026-05-28-unified-filter-widget-155.md`

- [ ] **Step 1: Run full test suite**

```
python -m pytest tests/ -q
```

Expected: all tests passing. Note the count — should be 1502 + new tests.

- [ ] **Step 2: Run lint**

```
make lint
```

Expected: `Success: no issues found` from mypy; `All checks passed!` from ruff.

If mypy errors appear in `reviewer/app.py` for the new `_MAP_STATUS_CLAUSES` dict (e.g. type annotation), add:

```python
_MAP_STATUS_CLAUSES: dict[str, str] = { ... }
```

- [ ] **Step 3: Update README — library description**

Find the `/library` line in README.md (around line 26) and update to mention year range and instant-apply:

Current:
```
- `/library` page ...
```

Add to the description: `; filter by year range, album, person, privacy, time pattern, tag, and location with instant-apply (no submit button required); an active filter chip row shows the current scope at a glance`

- [ ] **Step 4: Update spec status**

In `docs/superpowers/specs/2026-05-28-unified-filter-widget-155.md`, change line 3:

```markdown
_Status: ✓ done — shipped 2026-05-28_
```

- [ ] **Step 5: Commit docs**

```bash
git add README.md docs/superpowers/specs/2026-05-28-unified-filter-widget-155.md
git commit -m "docs(#155): update README + mark spec done

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

- [ ] **Step 6: Post retrospective comment and close issue**

```bash
gh issue comment 155 --body "## Done ✓

Shipped in v1.3.3 (to be tagged). 7 commits across 6 files.

**What shipped:**
- \`_STATUS_STATES\` refined: \`public\` now means strictly public; \`friends\`, \`family\`, \`friends_family\` are new independent status buckets
- \`/api/map-photos\` gains \`status\` as a server-side dataset filter (affects dots + trail)
- \`/library\` route: \`year_from\`/\`year_to\` integer params; \`person_names\` rename
- \`map_view()\` route: \`initial_filters\` for deep-link pre-population
- \`reviewer/templates/_filter_bar.html\`: new shared Jinja macro (time_pattern · year · album · person · privacy)
- \`library.html\`: shared macro in panel row 0; Apply button removed; instant-apply JS; filter chip row; View on map link; \`_buildPayload\` updated
- \`map.html\`: two-row bar → compact bar + collapsible panel; all JS converted to name-based access; status added to all filter functions; year inputs use blur/Enter; auto-open panel if filter active on load; \`openInLibrary()\` passes status

**Retrospective:**
- Size estimate: L ✓
- Files: db.py, app.py, _filter_bar.html (new), library.html, map.html, test_unified_filter.py (new), README.md, spec
- No scope creep — review queue deferred as planned"

gh issue close 155 --reason completed
```

- [ ] **Step 7: Push and bump version**

```bash
git push origin main
make bump
git push
git push --tags
```

---

## Self-review against spec

| Spec requirement | Task |
|---|---|
| `_filter_bar.html` macro: 5 shared controls | Task 5 |
| `_STATUS_STATES` refined + friends/family added | Task 1 |
| `api_map_photos()` status dataset filter | Task 2 |
| Library route: year_from/year_to + person_names rename | Task 3 |
| `map_view()` initial_filters | Task 4 |
| Library UI: instant-apply (no Apply button) | Task 6 |
| Library UI: active filter chip row | Task 6 |
| Library UI: shared macro in panel | Task 6 |
| Library UI: View on map link with shared params | Task 6 |
| Map UI: compact bar + collapsible panel | Task 7 |
| Map UI: macro used in panel | Task 7 |
| Map UI: name-based JS throughout | Task 7 |
| Map UI: status in all filter functions | Task 7 |
| Map UI: year inputs → blur+Enter | Task 7 |
| Map UI: auto-open panel on deep-link | Task 7 |
| `openInLibrary()` passes status | Task 7 |
| Privacy server-side before pagination invariant | Task 1–2 (db+API enforce this) |
| Identical privacy semantics across routes | Tasks 1–2 (`_STATUS_STATES` shared by both) |
| Tests: all new status values | Task 1, 2 |
| Tests: library year range | Task 3 |
| Tests: template integration (macro on both pages) | Task 6 |
| Tests: View on map link + roundtrip preserves filters | Task 6 |
| `expand` in `initial_filters` (holiday deep-link) | Task 4 |
| Bulk ops respect year range (year→date in `_buildPayload`) | Task 6 |
| `normalize_shared_filters()` + `SharedFilters` TypedDict | Task 3 |
| Both routes use `normalize_shared_filters()` | Tasks 3, 4 |
| Canonical URL invariant + normalization authority | Plan header |
| Map URL bar syncs via `history.replaceState` | Task 7 |
| Pagination reset invariant (`params.delete('page')`) | Task 6 (JS) |
| State flow + interaction model documented | Plan header |
| Tags filter noted as planned next addition | Plan header |
| README update | Task 8 |
| Issue closed with retrospective | Task 8 |
