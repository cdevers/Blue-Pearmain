# Spec: Map filter — year range, album, person, and animation privacy (#154)

_Status: spec — awaiting implementation plan_

---

## Problem

The map animation POC (#153) works, but the existing time-pattern dropdown makes it hard to scope meaningfully. Selecting "August" pulls in all Augusts across the entire archive; "December" shows decades of holiday photos at once. The animation is comical rather than useful.

More fundamentally, the map filter bar is a **place + time + person explorer** — not just an animation scoper. Tightening it enables a workflow that neither Apple Photos nor Flickr supports: "show me every place I've met Marcin," or "find which August trip included Spain," or "animate our December 1996 Europe trip with only public photos."

---

## Scope

**In:**
- Year range (from/to) filter alongside the existing pattern dropdown
- Album filter (dropdown of all non-deleted albums)
- Person filter (type-ahead input against `apple_persons` JSON)
- Animation privacy toggle (All / Public only / Private only)
- All four new filters AND with the existing time pattern
- All filters affect map dot display, trail polyline, and animation
- Privacy toggle affects animation only (not map dots)
- `datalist`-based type-ahead for person (no JS library)
- Backend: new optional query params on `/api/map-photos`

**Out:**
- Multi-person OR selection (future extension; see below)
- Person filter on library/review views
- Fuzzy date editing (separate issue)
- Album-membership-based trail coloring
- Saved filter presets

---

## UI

### Filter bar layout (two rows)

```
┌─────────────────────────────────────────────────────────────────────┐
│ Pattern [▾ August          ] | Year [      ] – [      ] | ☑ Trail  Animate │
│ Person  [Search name…      ] | Album [▾ any album     ] | 🔒 Animate: [▾ All photos] │
└─────────────────────────────────────────────────────────────────────┘
```

Row 1 contains time scope and trail/animate controls — the most commonly used filters.  
Row 2 contains who/where/privacy — used when narrowing a specific exploration.

All controls live inside the existing `.map-filter-bar` div. The second row is separated by a thin border and uses the same `.map-btn` / form element styles already in `map.html`.

### Filter controls

**Pattern dropdown** — the existing `<select id="map-time-select">`. A new first option `— any time —` (value `""`) is added so the filter can be cleared; all other options and optgroups are unchanged. When value is `""`, no time pattern clause is applied.

**Year from / Year to** — two `<input type="number" min="1800" max="2099">` fields. Either or both may be left blank (= unbounded). Label: "Year" with a dash between them.

**Person** — `<input type="text" list="person-datalist">` paired with a `<datalist id="person-datalist">` populated server-side from all distinct named persons in `apple_persons` (excluding `_UNKNOWN_` and blank). When the field is blank, no person filter is applied. Matching is **case-insensitive exact**: `LOWER(je.value) = LOWER(?)`. This is more robust than strict case-sensitivity given that Apple Photos name data can vary in capitalisation.

**Album** — `<select>` populated server-side with all non-deleted albums ordered by name. First option is `— any album —` (value `""`). When blank, no album filter. With 177+ albums, the dropdown can get long; a `size="1"` select with the browser's native search (typing characters to jump to a match) is sufficient for now. A searchable replacement is a natural upgrade once the filter system is proven useful.

**Animation privacy** — a `<select>` with a visible label "▶ Animate:" immediately before it (using the ▶ play symbol to signal it is animation-specific, not a global privacy mode). Three options:
- `all` (default, label "All photos")
- `public` (label "Public only") → filters to `privacy_state IN ('approved_public', 'already_public')`
- `private` (label "Private only") → filters to `privacy_state NOT IN ('approved_public', 'already_public')`

This control is always visible in row 2. It only takes effect when `toggleAnimation()` is called. If the privacy filter reduces the eligible geotagged photos below 2, the Animate button is **disabled** (grayed, `disabled` attribute set). No toast or error message — the disabled state is sufficient signal. The button re-enables when the privacy filter is relaxed or the filtered photo set grows.

### Active filter summary

A chip row sits **below** the two-row filter bar, visible only when at least one filter is active. It renders the current filter set as readable tokens, for example:

```
August  •  2019  •  Marcin Sulikowski  •  Japan Trip  •  ▶ Public only
```

Each chip reflects the human-readable label, not the raw value (album name not ID; year range "2015–2019" not two separate fields; pattern label not key). This row is purely display — no interactive removal in this issue.

**Why it matters:** animation screenshots and screen-recordings will otherwise lose context. The chip row makes the filter state legible in any capture.

### Show/hide and interaction rules

- All row-2 controls are always visible when the filter bar is visible (no hide/show toggling).
- The Animate button is **shown** when trail checkbox is checked; it is **disabled** (grayed) when the privacy-filtered geotagged photo count is < 2, and **enabled** otherwise.
- Changing any filter (pattern, year, album, person) re-fires the `/api/map-photos` request and re-renders the map. The active filter chip row updates at the same time.
- The privacy dropdown does **not** re-fire the map request on change — it only takes effect at animation start. `_updateAnimateBtn()` re-evaluates the enabled/disabled state on every privacy change.

### Invariant

All of dots, trail polyline, and animation draw from the **same filtered dataset** returned by a single `/api/map-photos` call. The trail and animation must never recompute from a different query path than the dots. Privacy filtering is the only divergence — and it is applied client-side to the same `_lastPhotos` array, not via a separate fetch.

---

## Filter semantics

All active filters compound with AND:

```
photos WHERE
  [time pattern clause]          -- existing logic, unchanged
  AND [year range clause]        -- range predicate on date_taken (index-friendly; see below)
  AND [album clause]             -- EXISTS in photo_albums
  AND [person clause]            -- EXISTS in json_each(apple_persons)
```

| Filter | Map dots | Trail polyline | Animation |
|--------|----------|----------------|-----------|
| Pattern | ✓ | ✓ | ✓ |
| Year range | ✓ | ✓ | ✓ |
| Person | ✓ | ✓ | ✓ |
| Album | ✓ | ✓ | ✓ |
| Privacy | — | — | ✓ only |

When a filter is blank/unset, its clause is omitted entirely (no `1=1` padding needed; the query builder skips it).

---

## Backend

### `/api/map-photos` — new query parameters

| Param | Type | Meaning |
|-------|------|---------|
| `year_from` | int (optional) | Earliest year inclusive; omit = no lower bound |
| `year_to` | int (optional) | Latest year inclusive; omit = no upper bound |
| `album_id` | int (optional) | Filter to this album's membership |
| `person` | str (optional) | Exact match against any element of `apple_persons` JSON array |

Privacy is **not** a backend param — it is applied client-side in `animatePOC()` by filtering `_lastPhotos` before building segments.

### SQL clauses

**Year range** — use ISO-string range predicates rather than `strftime('%Y', ...)`. Wrapping a column in a function prevents SQLite from using an index on `date_taken`. Since `date_taken` is stored as `YYYY-MM-DD HH:MM:SS` (ISO text), year boundaries map cleanly to string comparisons:

```sql
-- year_from=2019 contributes:
p.date_taken >= '2019-01-01'

-- year_to=2019 contributes:
p.date_taken < '2020-01-01'      -- exclusive upper bound = start of next year

-- both bounds:
p.date_taken >= '2019-01-01' AND p.date_taken < '2020-01-01'
```

The Python helper that builds these strings:
```python
def _year_bounds(year_from: int | None, year_to: int | None
                ) -> tuple[list[str], list[str]]:
    clauses, params = [], []
    if year_from is not None:
        clauses.append("p.date_taken >= ?")
        params.append(f"{year_from:04d}-01-01")
    if year_to is not None:
        clauses.append("p.date_taken < ?")
        params.append(f"{year_to + 1:04d}-01-01")
    return clauses, params
```

**Input validation — year_from > year_to:** silently swap before constructing bounds. No error message; the intent is unambiguous and a validation error would just be friction.

**Album:**
```sql
EXISTS (
  SELECT 1 FROM photo_albums pa2
  WHERE pa2.photo_id = p.id
    AND pa2.album_id = ?
    AND pa2.removed_at IS NULL
)
```
(Uses a correlated subquery to avoid duplicating rows when `photo_albums` has multiple entries.)

**Person:**
```sql
EXISTS (
  SELECT 1 FROM json_each(p.apple_persons) je
  WHERE LOWER(je.value) = LOWER(?)
)
```

**Photos with NULL `date_taken`:** included in map dots and trail if they have coordinates and pass the album/person filters; excluded from the trail polyline and animation (which require temporal ordering). Year range filter naturally excludes them (NULL comparisons are false in SQL). No special handling needed.

### `db.py` — `get_map_photos()` signature change

Current signature (inferred):
```python
def get_map_photos(self, pattern: str | None) -> list[dict]: ...
```

New signature:
```python
def get_map_photos(
    self,
    pattern: str | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    album_id: int | None = None,
    person: str | None = None,
) -> list[dict]: ...
```

The method builds the WHERE clause incrementally, appending only the active filter fragments. Existing `parse_pattern()` and `birthday_clause()` logic is unchanged.

### `app.py` — `api_map_photos()` route

Parse four new optional query params before calling `db().get_map_photos()`. Validate:
- `year_from` / `year_to`: coerce to `int`; ignore if non-numeric or out of range 1800–2099. If both are valid and `year_from > year_to`, silently swap them.
- `album_id`: coerce to `int`; ignore if non-numeric.
- `person`: strip whitespace; ignore if empty string after strip.

### `map_view()` route — new template vars

Pass to `map.html`:
- `albums` — `db().get_all_albums()` (already exists; used to populate album dropdown)
- `person_names` — sorted list of distinct named persons from `apple_persons`, excluding `_UNKNOWN_` and blank; used to populate `<datalist>`

The `person_names` query:
```sql
SELECT DISTINCT je.value
FROM photos p, json_each(p.apple_persons) je
WHERE je.value != '_UNKNOWN_'
  AND je.value != ''
  AND p.apple_persons IS NOT NULL
ORDER BY je.value
```

---

## Client-side privacy filtering

`animatePOC(photos)` gains a privacy pre-filter step before building segments:

```js
function animatePOC(photos) {
  const privacySel = document.getElementById('map-privacy-select').value;
  const PUBLIC_STATES = new Set(['approved_public', 'already_public']);
  let pts = photos.filter(p => p.lat != null && p.lon != null);
  if (privacySel === 'public') {
    pts = pts.filter(p => PUBLIC_STATES.has(p.privacy_state));
  } else if (privacySel === 'private') {
    pts = pts.filter(p => !PUBLIC_STATES.has(p.privacy_state));
  }
  if (pts.length < 2) return;   // button was already disabled; this is a safety guard
  // ... existing segment/rAF logic ...
}
```

`_updateAnimateBtn()` is extended to evaluate privacy-filtered count:

```js
function _updateAnimateBtn() {
  const trailOn = document.getElementById('map-trail-cb').checked;
  const privacySel = document.getElementById('map-privacy-select').value;
  const PUBLIC_STATES = new Set(['approved_public', 'already_public']);
  let eligible = _lastPhotos.filter(p => p.lat != null && p.lon != null);
  if (privacySel === 'public') eligible = eligible.filter(p => PUBLIC_STATES.has(p.privacy_state));
  else if (privacySel === 'private') eligible = eligible.filter(p => !PUBLIC_STATES.has(p.privacy_state));
  const btn = document.getElementById('map-animate-btn');
  btn.style.display = trailOn ? '' : 'none';
  btn.disabled = eligible.length < 2;
}
```

The `/api/map-photos` response must include `privacy_state` per photo (add to the SELECT if not already present).

---

## Template changes (`map.html`)

1. Add year-from/year-to inputs to row 1 of filter bar.
2. Add row 2 with person input + datalist, album select, privacy select.
3. Update the `map-time-select` change handler to also read new filter values and include them in the `/api/map-photos` fetch URL.
4. Add separate `change` listeners for year inputs, album select, and person input (debounced 300 ms for person text input to avoid firing on every keystroke).
5. Populate `<datalist id="person-datalist">` from `{{ person_names | tojson }}` Jinja variable.
6. Populate album `<select>` from `{{ albums | tojson }}`.

### Debounce helper (new, small)

```js
function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}
```

Person input uses `input` event with 300 ms debounce. All other controls use `change` event with no debounce.

---

## Testing

New tests in `tests/test_map_filter.py`:

- `test_year_from_filters_correctly` — photos before year excluded
- `test_year_to_filters_correctly` — photos after year excluded
- `test_year_range_both_bounds` — only photos within range included
- `test_year_range_empty_fields` — no year params = no filtering
- `test_album_filter` — only photos in specified album returned
- `test_album_filter_respects_removed_at` — removed memberships excluded
- `test_person_filter` — only photos with named person returned
- `test_person_filter_unknown_excluded` — `_UNKNOWN_` does not match person search
- `test_person_filter_blank` — blank person param = no filter
- `test_combined_filters` — pattern + year + person all AND together
- `test_map_view_passes_albums_to_template` — `map_view()` includes `albums`
- `test_map_view_passes_person_names` — `map_view()` includes `person_names`, no `_UNKNOWN_`
- `test_api_ignores_invalid_year` — non-numeric year_from/to ignored gracefully
- `test_year_swap_when_from_greater_than_to` — year_from=2020, year_to=2015 treated as 2015–2020
- `test_privacy_state_in_api_response` — `privacy_state` field present in each photo dict
- `test_person_filter_case_insensitive` — "marcin sulikowski" matches "Marcin Sulikowski"
- `test_null_date_taken_excluded_from_trail_ordering` — photos without dates not in temporal sequence
- `test_year_bounds_sql_uses_range_not_strftime` — year filter builds `>= 'YYYY-01-01'` clause (not strftime)

---

## Future extensions

**Multi-person OR selection** — a natural next step once single-person works. The `person` param becomes `persons` (multi-value), and the SQL uses `EXISTS ... WHERE je.value IN (?, ?, ?)`. The UI would need a tag/chip input or a multi-select; a separate issue when the use case is clear.

**Person filter on library/review views** — the `/api/library` endpoint could accept the same `person` param for person-scoped review sessions. Out of scope here.

**Saved filter presets** — named combinations (e.g., "Marcin trips") stored in localStorage or DB. Worthwhile once the filter system is proven useful in practice.

---

## Worked scenarios

| Scenario | Pattern | Year from | Year to | Person | Album |
|----------|---------|-----------|---------|--------|-------|
| Every place I've met Marcin | — | — | — | Marcin Sulikowski | — |
| Indigenous Peoples Day, pre-2020 | Indigenous Peoples Day weekend | — | 2019 | — | — |
| Find which August we were in Spain | August | — | — | — | — |
| December 1996 Europe trip | December | 1996 | 1996 | — | — |
| Vietnam trip with Marcin | — | — | — | Marcin Sulikowski | Vietnam 2014 |
