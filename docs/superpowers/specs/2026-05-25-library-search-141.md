# Library Search and Filter Expansion — Design Spec

**Issue:** #141  
**Date:** 2026-05-25  
**Status:** in progress

---

## Problem

The library grid can filter by album, tag, status, and date range, but has no text search and no way to filter by location or person. Finding photos requires knowing which album they're in or scrolling the full grid. The map view's "Show this day" popup link also has nowhere to land — the library has no `?date=` single-day param.

## Scope

Three new filter dimensions, a `?date=` alias, and a restructured filter UI:

- **Text search** (`q`) — `LIKE '%q%'` across titles, descriptions, tags, and Apple AI caption
- **Location cascade** (`country` → `state` → `city` → `neighborhood`) — four-level exact-match cascade, always AND-combined; lower levels without parent levels return all matches across all parents (e.g. `?neighborhood=Union+Square` alone matches every Union Square in the DB — the cascade JS prevents this in normal use by always sending the full path)
- **Person** (`person`) — exact match in the `apple_persons` JSON array; datalist autocomplete in the UI
- **`?date=YYYY-MM-DD` alias** — convenience param for the map's "Show this day" link; translated to `date_from = date_to = date` in `app.py` before any DB call; appears in the From/To pickers so no special UI is needed
- **Restructured filter UI** — search box always visible; all other controls (including the `time_pattern` control added by #142) consolidated into a collapsible Filters panel with an Apply button

**Out of scope:**
- FTS5 full-text search index — plain `LIKE` is sufficient at personal-library scale
- Saved searches / search history
- Sorting controls
- Map-based bounding-box filter (#140 extension point)
- Server-side rejection of under-specified location queries (e.g. `?neighborhood=X` without `?city=Y`) — AND-combination is correct behaviour; the cascade prevents it in normal use

**Dependency:** #142 must ship first. When #141 ships, the `time_pattern` / `±2 days` controls added by #142 to the library filter bar are moved into the Filters panel.

---

## Architecture & Data Flow

```
db/photo_filters.py  (new — mirrors db/time_patterns.py from #142)
    build_text_clause(q)                                      → (sql, params)
    build_location_clause(country, state, city, neighborhood) → (sql, params)
    build_person_clause(person)                               → (sql, params)
    build_date_alias_clause(date)                             → (sql, params)

Library:
    GET /library?q=sunset&country=United+States&state=MA&city=Boston&person=Alice
        → app.py translates ?date= alias if present
        → db().library_photos(q=..., country=..., state=..., city=..., person=...)
        → _library_where() calls photo_filters.build_*() for each active param
        → all clauses AND-combined with existing album/tag/status/untitled clauses
        → page reload; all params preserved in form for pagination and badge count

Map (future, not in this issue):
    GET /api/map-photos?q=sunset&person=Alice
        → imports photo_filters directly — same composable functions, zero new abstraction
```

**Query params:**

| Param | Matches | SQL |
|---|---|---|
| `q` | photos_title, flickr_title, photos_description, flickr_description, apple_ai_caption, flickr_tags (json_each), photos_tags (json_each) | `LIKE '%q%'` on each |
| `country` | `place_country` | `= ?` |
| `state` | `place_state` | `= ?` |
| `city` | `place_city` | `= ?` |
| `neighborhood` | `place_neighborhood` | `= ?` |
| `person` | `apple_persons` JSON array | `EXISTS (SELECT 1 FROM json_each(apple_persons) WHERE value = ?)` |
| `date` | `date_taken` | alias → `date_from = date_to = date`; `DATE(p.date_taken) = ?` |

All params are optional and AND-combined. Setting none leaves the full grid unchanged.

**Page-load data** (queried once per library render, passed to template):

- `location_tree` — `{country: {state: {city: [neighborhoods]}}}` nested dict; excludes photos where `place_country IS NULL`; serialised as JSON `data-` attribute on the form for cascade JS
- `person_list` — distinct names from `apple_persons`, excluding `_UNKNOWN_`, sorted; rendered as `<datalist>` in the template

---

## Backend

### `db/photo_filters.py` *(new)*

Pure functions — no Flask or DB dependencies. All fragments reference the `p` alias (`p.date_taken`, `p.place_country`, etc.) to match the `photos p` alias used throughout `_library_where`.

```python
def build_text_clause(q: str) -> tuple[str, list]:
    """LIKE search across all text fields including Apple AI caption."""
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
    Lower levels without parent levels return all matches across all parents —
    the cascade UI prevents this in normal use by always sending the full path."""
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
    """Match any photo whose apple_persons JSON array contains the exact name."""
    return (
        "EXISTS (SELECT 1 FROM json_each(p.apple_persons) WHERE value = ?)",
        [person],
    )


def build_date_alias_clause(date: str) -> tuple[str, list]:
    """Single-day filter. Enables the map popup 'Show this day' link."""
    return "DATE(p.date_taken) = ?", [date]
```

---

### `db/db.py`

**`_library_where`** gains six new optional params. It remains a coordinator: existing clauses first, then `time_patterns` (#142), then `photo_filters` (#141).

```python
def _library_where(
    self,
    date_from: str | None,
    date_to: str | None,
    album_id: int | None,
    tag: str | None,
    status: str | None,
    untitled_only: bool,
    time_pattern: str | None = None,   # #142
    time_expand: int = 2,              # #142
    q: str | None = None,              # #141
    country: str | None = None,        # #141
    state: str | None = None,          # #141
    city: str | None = None,           # #141
    neighborhood: str | None = None,   # #141
    person: str | None = None,         # #141
) -> tuple[str, list]:
```

Note: `date` (single-day alias) is **not** a param of `_library_where`. It is resolved in `app.py` before the DB call — see below. `build_date_alias_clause` exists in `photo_filters.py` for future use by other endpoints (e.g. `/api/map-photos`).

When any `#141` param is set, calls the relevant `photo_filters.build_*()` and appends the fragment and params. Clause order: `flickr_deleted=0` → structural (date range, status, untitled, tag) → time_pattern → text → location → person.

**Two new DB methods** for page-load data:

```python
def location_data(self) -> dict:
    """Return nested dict {country: {state: {city: [neighborhoods]}}} for non-deleted photos.
    Photos where place_country IS NULL are excluded.
    Neighborhoods may be empty strings in the DB; those are excluded.
    Result is sorted at each level."""

def person_names(self) -> list[str]:
    """Return distinct person names from apple_persons JSON arrays,
    excluding '_UNKNOWN_', sorted alphabetically."""
```

`library_photos`, `library_photo_count`, and `library_photo_ids` all gain the seven new passthrough kwargs (defaulting to `None` / `False`).

---

### `reviewer/app.py`

**Library route** reads the new params:

```python
q            = request.args.get("q", "").strip() or None
country      = request.args.get("country") or None
state        = request.args.get("state") or None
city         = request.args.get("city") or None
neighborhood = request.args.get("neighborhood") or None
person       = request.args.get("person") or None
date_alias   = request.args.get("date") or None   # single-day alias
```

`date_alias` is resolved before DB calls:
```python
if date_alias:
    date_from = date_from or date_alias
    date_to   = date_to   or date_alias
```
This means `?date=2023-10-15` populates both date pickers and shows naturally in the panel.

Two page-load calls (results passed to template):
```python
location_tree = db().location_data()    # serialised as JSON in template
person_list   = db().person_names()     # rendered as <datalist>
```

All new params added to the `filters` dict for pagination links and badge count:
```python
filters={
    ...existing...,
    "q":            q or "",
    "country":      country or "",
    "state":        state or "",
    "city":         city or "",
    "neighborhood": neighborhood or "",
    "person":       person or "",
}
```

---

## Frontend

### `reviewer/templates/library.html`

The filter bar is replaced with a two-part layout: a persistent search bar and a collapsible Filters panel.

**Search bar (always visible):**
```
[🔍 Search photos…                              ] [Filters ▾ ③] [Clear all]
```
- Full-width `<input type="search" name="q">`, submits on Enter
- "Filters" button toggles the panel; badge (③) shows count of active non-search filter slots
- "Clear all" link appears only when any filter is active

**Badge count** — computed in the template from the `filters` dict. Each non-empty slot counts as 1; `date_from` and `date_to` together count as 1:
```
q, (date_from or date_to), album_id, tag, status, untitled,
time_pattern, country, state, city, neighborhood, person
```

**Filter panel** (collapses below the search bar; auto-opens if any non-q filter param is in the URL):

```
┌────────────────────────────────────────────────────────────┐
│ From [date] To [date] │ Album [▾] │ Tag [___] │ Status [▾] │
│ □ Untitled only │ Time of year [▾] □ ±2 days              │
│ ─────────────────────────────────────────────────────────  │
│ Country [▾] → State/Region [▾] → City [▾] → Nbhd [▾]     │
│ Person [___________________________↓]                       │
│                                    [Apply filters] [Clear] │
└────────────────────────────────────────────────────────────┘
```

Controls inside the panel do **not** auto-submit on change. The user configures multiple filters then clicks **Apply** (form submit). This replaces the `onchange="this.form.submit()"` behaviour currently on the album, status, and untitled controls.

The `time_pattern` and `±2 days` controls added by #142 are moved from the filter bar into the panel as part of this issue.

The search box and the panel are part of the same `<form method="get">`. Submitting via Enter in the search box includes all current panel values.

---

**Location cascade JS:**

The `location_tree` dict is serialised as a `data-location-tree` attribute on the form element:

```html
<form ... data-location-tree="{{ location_tree | tojson }}">
```

On page load, the cascade is initialised from active filter values in the URL (so returning to `/library?country=United+States&state=MA&city=Boston` pre-populates all three dependent selects with the correct option lists). On each select change, the next level's options are rebuilt and deeper levels are reset:

The `filters` dict is also emitted as a JS constant at the top of the script block so the cascade initialisation can reference active values:

```js
const filters = {{ filters | tojson }};   // Jinja → JS
```

```js
const tree = JSON.parse(form.dataset.locationTree);

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
  rebuildSelect(selState, Object.keys(tree[selCountry.value] || {}), filters.state);
  rebuildSelect(selCity, [], null);
  rebuildSelect(selNeighborhood, [], null);
});
selState.addEventListener('change', () => {
  const cities = (tree[selCountry.value] || {})[selState.value] || {};
  rebuildSelect(selCity, Object.keys(cities), filters.city);
  rebuildSelect(selNeighborhood, [], null);
});
selCity.addEventListener('change', () => {
  const nbhds = ((tree[selCountry.value] || {})[selState.value] || {})[selCity.value] || [];
  rebuildSelect(selNeighborhood, nbhds, filters.neighborhood);
});

// Initialise from current filter values on page load
if (filters.country) {
  rebuildSelect(selState, Object.keys(tree[filters.country] || {}), filters.state);
}
if (filters.state) {
  const cities = (tree[filters.country] || {})[filters.state] || {};
  rebuildSelect(selCity, Object.keys(cities), filters.city);
}
if (filters.city) {
  const nbhds = ((tree[filters.country] || {})[filters.state] || {})[filters.city] || [];
  rebuildSelect(selNeighborhood, nbhds, filters.neighborhood);
}
```

**Person datalist:**

```html
<input type="text" name="person" value="{{ filters.person }}"
       list="person-datalist" placeholder="person name…">
<datalist id="person-datalist">
  {% for name in person_list %}
  <option value="{{ name }}">
  {% endfor %}
</datalist>
```

`_UNKNOWN_` is excluded from `person_list` (it is an internal marker). Passing `?person=_UNKNOWN_` directly in the URL still works and returns photos with unidentified faces — it is just not offered as a datalist suggestion.

---

## Extension Points

- **Map search** — `db/photo_filters.py` functions can be imported directly by `/api/map-photos`. Adding `?q=sunset&person=Alice` to the map endpoint requires no new abstraction.
- **Configurable expansion window** — `time_expand` in `_library_where` already accepts any integer; a future UI slider (1–10 days) is a frontend-only change.
- **Moveable feasts** (#142 extension) — `db/time_patterns.py` lookup table path; no impact on `photo_filters.py`.
- **Sorting controls** — a `sort` param would extend `library_photos` independently of the filter chain.

---

## Testing

### `tests/test_photo_filters.py` *(new — unit tests for the pure module)*

- `build_text_clause("sunset")` → fragment has 7 params all equal to `"%sunset%"`
- `build_location_clause("United States", "MA", "Boston", None)` → 3 clauses, no neighborhood clause
- `build_location_clause("United States", "MA", "Somerville", "Union Square")` → 4 clauses
- `build_location_clause(None, None, None, None)` → `("1=1", [])`
- `build_person_clause("Alice")` → `json_each` EXISTS fragment with `"Alice"` param
- `build_date_alias_clause("2023-10-15")` → `DATE(p.date_taken) = ?` with `"2023-10-15"`

### `tests/test_library_search.py` *(new — integration)*

**Fixture design:** Include two photos with the same city name in different states (e.g. "Springfield, MA" and "Springfield, VT") and two photos in different cities sharing a neighborhood name (e.g. "Union Square, Somerville, MA" and "Union Square, Boston, MA") to verify AND-combination disambiguates correctly.

- `GET /library?q=sunset` → only photos matching "sunset" in any text field
- `GET /library?q=birthday` matches a photo whose `apple_ai_caption` contains "birthday cake"
- `GET /library?q=bird` matches a photo whose `flickr_tags` JSON array contains "birding"
- `GET /library?country=United+States&state=MA&city=Springfield` → only Springfield MA, not VT
- `GET /library?country=United+States&state=MA&city=Somerville&neighborhood=Union+Square` → only Somerville Union Square, not Boston Union Square
- `GET /library?neighborhood=Union+Square` alone → both Union Squares returned (correct; cascade prevents this in UI)
- `GET /library?person=Alice` → only photos with "Alice" in `apple_persons`
- `GET /library?person=_UNKNOWN_` → returns photos with unidentified persons (direct param works)
- `GET /library?date=2023-10-15` → only photos from that exact day; date_from and date_to both set to `2023-10-15`
- `GET /library?q=sunset&country=United+States&state=MA` → filters AND-combined
- Unknown/empty params → full grid, no crash
- Pagination links preserve all active filter params including `q`, location, `person`

### `tests/test_library_page_data.py` *(new — DB methods)*

- `db.location_data()` → nested dict with correct structure; no entry for `place_country IS NULL` photos
- `db.location_data()` → two cities named "Springfield" appear under their respective states
- `db.location_data()` → "Union Square" appears under both Somerville and Boston
- `db.location_data()` → photos with empty-string `place_neighborhood` are excluded from neighborhood lists
- `db.person_names()` → sorted list, excludes `_UNKNOWN_`, no duplicates
