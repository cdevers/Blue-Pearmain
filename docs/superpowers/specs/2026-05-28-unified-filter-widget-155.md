# Spec: Unified filter widget — shared macro, instant-apply, cross-page persistence (#155)

_Status: spec — awaiting implementation plan_

---

## Problem

The map (`/map`) and library (`/library`) each have independent filter implementations covering overlapping dimensions — time pattern, year range, album, and person. Adding a new filter to one page doesn't benefit the other. The library requires an explicit "Apply" click; the map applies instantly. Neither page has a way to carry its current filter state to the other view, which makes the intended workflow of bouncing between map exploration and library review friction-heavy.

---

## Goals

1. **Code reuse** — one place to add a new shared filter dimension; both pages gain it automatically.
2. **Consistent UX** — both pages behave identically for the shared controls: same layout, same instant-apply interaction model.
3. **Cross-page persistence** — switching from map to library (or vice versa) carries the current filter context.

---

## Scope

**In:**
- `reviewer/templates/_filter_bar.html` — new Jinja macro for the five shared filter controls
- `/library` route — add `year_from` / `year_to` integer params; extend `status` handling to include `friends`/`family`/`friends_family`
- `/api/map-photos` — add `status` as a dataset-level filter (affects dots + trail)
- Library UI — instant-apply (drop "Apply" button), active filter chip row, shared macro in panel
- Map UI — collapse two-row bar to compact-bar + collapsible panel using shared macro
- `map_view()` route — accept shared params for deep-link support
- Cross-page nav — "View on map" link on library; update `openInLibrary()` on map

**Out:**
- `/review` filter expansion (separate issue)
- `localStorage` filter persistence (URL-first is sufficient)
- Multi-person OR selection
- Library-only controls in the macro: search query (`q`), tag, untitled, no-location, confirmed-none, location cascade, spatial bbox, review-workflow `status` values (screenshot states)
- Map-only control in the macro: animation privacy override select
- Animation privacy override granularity (stays All / Public / Private; separate from the shared `status` filter)

---

## Architecture

### Shared macro

**File:** `reviewer/templates/_filter_bar.html`

```jinja
{% macro filter_bar(albums, person_names, filters) %}
<div class="shared-filter-bar">
  <label>Time of year
    <select name="time_pattern">
      {# Full option list is identical to the <select id="map-time-select"> block in
         the current map.html — copy it verbatim. First option must be value="" with
         label "— any time —" so an empty value means "no time filter". #}
    </select>
  </label>

  <span class="filter-year-range">
    <label>Year <input type="number" name="year_from" min="1800" max="2099"
                        value="{{ filters.year_from or '' }}" placeholder="from"></label>
    <span>–</span>
    <label><input type="number" name="year_to" min="1800" max="2099"
                   value="{{ filters.year_to or '' }}" placeholder="to"></label>
  </span>

  <label>Album
    <select name="album_id">
      <option value="">— any album —</option>
      {% for a in albums %}
      <option value="{{ a.id }}" {% if filters.album_id == a.id %}selected{% endif %}>{{ a.name }}</option>
      {% endfor %}
    </select>
  </label>

  <label>Person
    <input type="text" name="person" value="{{ filters.person or '' }}"
           list="shared-person-datalist" placeholder="person name…">
    <datalist id="shared-person-datalist">
      {% for name in person_names %}
      <option value="{{ name }}">
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

Both `library.html` and `map.html` call this macro. Both pages already pass `albums` and a person name list to the template; the macro reuses those same variables (library uses `person_list`, map uses `person_names` — the route normalises the key to `person_names` on both pages).

**Separation from library-only `status` values:** the library's existing filter panel has a fuller `status` select that includes review-workflow states (`screenshot_unreviewed`, etc.). That select is **replaced** by the shared macro's Privacy dropdown. The macro only exposes the seven dataset-scope values above; screenshot-filtering moves to the library-specific row 2 as a dedicated checkbox or separate select if needed. (Current usage of screenshot filters is through the `/review` route, not `/library`, so in practice this dropdown consolidation is clean.)

---

## Backend changes

### `/library` route (`app.py`)

Add `year_from` / `year_to` integer parsing using the existing `_safe_year()` helper (already present from #154). Convert to ISO string bounds and merge with any explicit `date_from` / `date_to`:

```python
year_from = _safe_year("year_from")
year_to   = _safe_year("year_to")
if year_from is not None and year_to is not None and year_from > year_to:
    year_from, year_to = year_to, year_from
# Apply year bounds only if no explicit date_from/date_to was provided
if year_from is not None and not date_from:
    date_from = f"{year_from:04d}-01-01"
if year_to is not None and not date_to:
    date_to = f"{year_to + 1:04d}-01-01T00:00:00"
```

Add `year_from` and `year_to` to the `filters` dict:

```python
filters={
    ...existing keys...,
    "year_from": year_from or "",
    "year_to":   year_to   or "",
}
```

Update `filter_count` in `library.html` to include `year_from`/`year_to`.

**`db.library_photos()` and `db.library_photo_count()` — no signature change.** The route converts year integers to ISO `date_from`/`date_to` before calling them.

**Rename `person_list` → `person_names` in `library()` route.** The route currently passes `person_list=person_list` to the template. Change to `person_names=person_list` so the template variable name matches both the macro's parameter name and the variable name used by `map_view()`. Update the one reference to `person_list` in `library.html` accordingly.

**Extend `status` handling in `db.library_photos()` and `db.library_photo_count()`** to recognise three new values:

| `status` value | `privacy_state` clause |
|---|---|
| `friends` | `privacy_state = 'approved_friends'` |
| `family` | `privacy_state = 'approved_family'` |
| `friends_family` | `privacy_state = 'approved_friends_family'` |

The existing `public`, `private`, `pending` mappings are unchanged.

Add `status` to the `filters` dict passed to the template:

```python
filters={
    ...existing keys...,
    "year_from": year_from or "",
    "year_to":   year_to   or "",
    "status":    status    or "",   # replaces the existing "status" key (already present)
}
```

(`status` is already in the `filters` dict; this just ensures it's always present and covers the new values.)

### `/api/map-photos` — new `status` dataset filter

Add `status` param parsing to `api_map_photos()`:

```python
status = (request.args.get("status") or "").strip() or None
```

Add a WHERE clause fragment (appended to `where_frags` / `where_params` alongside year, album, person):

```python
_STATUS_MAP = {
    "public":         "p.privacy_state IN ('approved_public','already_public')",
    "friends":        "p.privacy_state = 'approved_friends'",
    "family":         "p.privacy_state = 'approved_family'",
    "friends_family": "p.privacy_state = 'approved_friends_family'",
    "private":        "p.privacy_state IN ('keep_private','auto_private')",
    "pending":        "p.privacy_state IN ('needs_review','candidate_public')",
}
if status and status in _STATUS_MAP:
    where_frags.append(_STATUS_MAP[status])
    # no bound param — SQL literals only (values are hard-coded, not user input)
```

This affects which dots and trail segments appear. The animation privacy override remains a separate client-side filter on top of this.

### `map_view()` route (`app.py`)

Accept the four shared params as optional URL params so the library's "View on map" link can deep-link to a pre-filtered map:

```python
@app.route("/map")
def map_view() -> str:
    # Pass through shared filter params so JS can read them from the form on load
    initial_filters = {
        "time_pattern": request.args.get("time_pattern", ""),
        "year_from":    request.args.get("year_from", ""),
        "year_to":      request.args.get("year_to", ""),
        "album_id":     request.args.get("album_id", ""),
        "person":       request.args.get("person", ""),
        "status":       request.args.get("status", ""),
    }
    return render_template("map.html", ..., initial_filters=initial_filters)
```

The map's filter form fields are populated from `initial_filters` in the template (Jinja `value=` attributes). Since the map already reads these fields via JS to build the fetch URL, pre-populating them is sufficient — no JS change required for initial load.

---

## Library UI changes (`library.html`)

### Structure

The collapsible filter panel is restructured into three rows:

**Row 1 — shared macro:**
```jinja
{% from "_filter_bar.html" import filter_bar %}
{{ filter_bar(albums, person_names, filters) }}
```

**Row 2 — library-specific:**
`date_from` / `date_to` calendar pickers · tag · status · untitled · no-location · confirmed-none checkboxes

**Row 3 — library-specific:**
Location cascade (country / state / city / neighborhood)

**Panel footer:** "Clear filters" link only — no "Apply filters" button. "Clear filters" navigates to `/library` (preserving `q` if a search query is active, discarding all other params). It is hidden when no filter is active (same condition as the existing implementation).

### Instant-apply JS

```js
function buildLibraryUrl() {
  const form = document.getElementById('lib-filter-form');
  const params = new URLSearchParams();
  for (const el of form.elements) {
    if (!el.name) continue;
    if (el.type === 'checkbox') { if (el.checked) params.set(el.name, el.value); }
    else if (el.value) params.set(el.name, el.value);
  }
  params.delete('page');   // reset to page 1 on filter change
  return '/library?' + params.toString();
}

function applyLibraryFilter() {
  location.href = buildLibraryUrl();
}

const _debounced = debounce(applyLibraryFilter, 500);

// Shared macro fields
// Selects: immediate on change
document.querySelector('[name=time_pattern]').addEventListener('change', applyLibraryFilter);
document.querySelector('[name=album_id]').addEventListener('change', applyLibraryFilter);
document.querySelector('[name=status]').addEventListener('change', applyLibraryFilter);
// Person text: debounced on input (500ms) — avoids per-keystroke reloads
document.querySelector('[name=person]').addEventListener('input', _debounced);
// Year inputs: fire on blur or Enter only — avoids intermediate states like "201" mid-type
for (const el of document.querySelectorAll('[name=year_from],[name=year_to]')) {
  el.addEventListener('blur', applyLibraryFilter);
  el.addEventListener('keydown', e => { if (e.key === 'Enter') applyLibraryFilter(); });
}

// Library-specific fields
document.querySelector('[name=status]').addEventListener('change', applyLibraryFilter);
document.querySelector('[name=tag]').addEventListener('input', _debounced);
document.querySelector('[name=country]').addEventListener('change', applyLibraryFilter);
// ... state, city, neighborhood selects (already have JS cascade handlers)
for (const cb of document.querySelectorAll('[name=untitled],[name=no_location],[name=confirmed_none]'))
  cb.addEventListener('change', applyLibraryFilter);
```

The `debounce()` helper is defined inline in `library.html` (copied verbatim from `map.html`). Both templates carry their own copy; no shared JS file is introduced in this issue.

### Active filter chip row

Added between the search bar and the photo grid:

```html
<div id="lib-filter-chips" class="lib-filter-chips"></div>
```

Populated by `_updateLibraryChips()` on load (reads the `filters` object passed from the server via `data-filters` attribute or inline Jinja). Chip format mirrors the map: album name, `YYYY–YYYY` for year range, person name, time pattern label, privacy label (e.g. "Public", "Friends & Family"). Chip row is hidden via CSS `:empty` when no filters are active.

### Panel auto-open

```js
// Auto-open panel if any filter (other than q) is active
const filterCount = {{ filter_count }};
if (filterCount > 0) {
  document.getElementById('lib-filter-panel').style.display = 'block';
}
```

(Already partially implemented — just needs to include `year_from`/`year_to` in the count.)

---

## Map UI changes (`map.html`)

### Compact bar

The current two-row always-visible bar is replaced with a single line:

```html
<div class="map-filter-bar">
  <button type="button" id="map-filter-toggle" onclick="toggleMapPanel()">
    Filters<span id="map-filter-badge"></span> ▾
  </button>
  <span style="flex:1"></span>
  <label><input type="checkbox" id="map-trail-cb"> Trail</label>
  <button id="map-animate-btn">▶ Animate</button>
</div>
```

`map-filter-badge` shows `(N)` when any filter is active (same logic as current `_hasAnyFilter()`).

### Collapsible panel

```html
<div id="map-filter-panel" style="display:none">
  {% from "_filter_bar.html" import filter_bar %}
  {{ filter_bar(albums, person_names, initial_filters) }}

  <!-- Map-specific -->
  <label>▶ Animate:
    <select id="map-privacy-select">
      <option value="all">All photos</option>
      <option value="public">Public only</option>
      <option value="private">Private only</option>
    </select>
  </label>
</div>
```

### JS changes

- `toggleMapPanel()` — shows/hides `#map-filter-panel`; auto-opens on load if `_hasAnyFilter()`.
- `buildMapUrl()` — unchanged (reads named form fields).
- `_updateFilterBadge()` — replaces separate `_hasAnyFilter()` calls; counts active shared filters and updates `#map-filter-badge`.
- All existing `change` listeners on the shared filter controls remain; they now fire from inside the panel instead of the top bar.
- Year inputs on the map: replace existing debounced `input` handler with `blur` + `keydown Enter` (same as library). This also removes intermediate-state fetches on the map.
- `_updateAnimateBtn()` — unchanged logic; triggered by privacy select change and on data load.
- Chip row — unchanged.

---

## Cross-page navigation

### Library → Map

A "View on map" link in the library toolbar, Jinja-built:

```jinja
<a href="{{ url_for('map_view',
  time_pattern=filters.time_pattern or None,
  year_from=filters.year_from or None,
  year_to=filters.year_to or None,
  album_id=filters.album_id or None,
  person=filters.person or None,
  status=filters.status or None) }}"
   title="View these photos on the map">🗺 Map</a>
```

`None` values are omitted from the URL by Flask's `url_for`. The map's form fields are pre-populated from `initial_filters` (passed by `map_view()`), and the map's JS reads them on load to fire the initial fetch.

### Map → Library

`openInLibrary()` updated to include all five shared params:

```js
function openInLibrary() {
  const p = new URLSearchParams();
  const tm = document.querySelector('[name=time_pattern]')?.value;
  const yf = document.querySelector('[name=year_from]')?.value;
  const yt = document.querySelector('[name=year_to]')?.value;
  const ai = document.querySelector('[name=album_id]')?.value;
  const pe = document.querySelector('[name=person]')?.value;
  if (tm) p.set('time_pattern', tm);
  if (yf) p.set('year_from', yf);
  if (yt) p.set('year_to', yt);
  if (ai) p.set('album_id', ai);
  if (pe) p.set('person', pe);
  const st = document.querySelector('[name=status]')?.value;
  if (st) p.set('status', st);
  // Preserve spatial bbox if a map region is selected (existing behaviour)
  if (_regionBounds) {
    p.set('lat_min', _regionBounds.getSouth().toFixed(5));
    p.set('lat_max', _regionBounds.getNorth().toFixed(5));
    p.set('lon_min', _regionBounds.getWest().toFixed(5));
    p.set('lon_max', _regionBounds.getEast().toFixed(5));
  }
  window.open('/library?' + p.toString(), '_blank');
}
```

---

## Invariants

**Privacy filtering is server-side on both pages.** The `status` filter is applied in the SQL WHERE clause — never as a client-side pass over results. On `/library` this ensures pagination counts are correct (a page of 120 "Public" photos is 120 photos that actually match, not 120 photos minus client-filtered ones). On `/api/map-photos` the WHERE clause excludes non-matching dots before the response is sent.

**Privacy semantics are identical across routes.** The `_STATUS_MAP` dict (or equivalent logic) must produce the same `privacy_state` comparisons in both `db.library_photos()` and `api_map_photos()`. If "private" means `keep_private + auto_private` in the library, it must mean exactly that on the map too. Do not let one route silently interpret "private" as "not public" while the other uses exact enum values.

**Map animation privacy override is client-side only.** After `/api/map-photos` returns a `status`-filtered dataset, the animation privacy select may further narrow which photos animate. This client-side narrowing operates on `_lastPhotos` (never mutates it) and does not re-fetch. The two layers are independent and composable: `status=public` + `animate=private` would yield zero animation candidates — which is correct and expected.

---

## Filter parameter contract

The shared params and their meaning are identical on both pages:

| Param | Type | Meaning |
|-------|------|---------|
| `time_pattern` | str (optional) | Semantic time pattern (e.g. `month:08`, `holiday:thanksgiving`) |
| `year_from` | int (optional) | Earliest year inclusive; 1800–2099 |
| `year_to` | int (optional) | Latest year inclusive; silently swapped if > year_from |
| `album_id` | int (optional) | Album membership filter |
| `person` | str (optional) | Case-insensitive exact match against `apple_persons` |
| `status` | str (optional) | Privacy scope: `public` / `friends` / `family` / `friends_family` / `private` / `pending` |

**Map-only (not in shared contract):** animation privacy override (`all`/`public`/`private`), trail toggle, animate state. These must not appear in library URLs generated by `openInLibrary()`.

**Library-only:** `q` (search query), `date_from`/`date_to` (calendar date pickers), `tag`, `untitled`, `no_location`, `confirmed_none`, location cascade, spatial bbox.

---

## Testing

New tests in `tests/test_library_filter.py` (or extend existing `test_map_filter.py`):

- `test_library_year_from_excludes_earlier` — `year_from=2019` excludes 2016 photos
- `test_library_year_to_excludes_later` — `year_to=2019` excludes 2023 photos
- `test_library_year_range_both_bounds` — only photos in range returned
- `test_library_year_from_greater_than_to_is_swapped` — silent swap
- `test_library_year_does_not_override_explicit_date_from` — explicit `date_from` takes precedence over `year_from`
- `test_library_year_nonnumeric_ignored` — `year_from=abc` ignored gracefully
- `test_library_year_out_of_range_ignored` — `year_from=1700` ignored
- `test_library_status_public_filter` — `status=public` returns only approved_public + already_public photos
- `test_library_status_friends_filter` — `status=friends` returns only approved_friends photos
- `test_library_status_family_filter` — `status=family` returns only approved_family photos
- `test_library_status_friends_family_filter` — `status=friends_family` returns only approved_friends_family photos
- `test_library_status_private_filter` — `status=private` returns only keep_private + auto_private photos
- `test_library_status_pending_filter` — `status=pending` returns only needs_review + candidate_public photos
- `test_map_api_status_public_filter` — `/api/map-photos?status=public` returns only public-state geotagged photos
- `test_map_api_status_unknown_ignored` — `/api/map-photos?status=bogus` returns all geotagged photos (unknown values ignored)
- `test_map_view_accepts_initial_filter_params` — `map_view()` passes `initial_filters` (including `status`) to template
- `test_library_view_on_map_link_has_filter_params` — "View on map" link in library HTML includes active shared filters including `status` (template test)

Template tests (extend `tests/test_map_filter.py`):
- `test_shared_macro_present_in_library` — `<select name="time_pattern">` and `<select name="status">` appear in `/library` response
- `test_shared_macro_present_in_map` — `<select name="time_pattern">` and `<select name="status">` appear in `/map` response

---

## Future extensions

- **`/review` filter expansion** — add shared macro to the review queue so a review session can be scoped to a year/album/person. Separate issue.
- **`localStorage` filter memory** — remember last-used filter state across browser sessions. Useful once the filter system is more heavily used in daily workflow.
- **Saved filter presets** — named combinations stored in the DB (e.g. "Marcin trips"). Depends on the filter system being proven in practice.
- **`q` search param in cross-page nav** — currently library-only; could be passed to the map as a person/tag hint.
