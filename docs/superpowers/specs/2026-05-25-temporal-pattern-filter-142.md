# Temporal Pattern Filter — Design Spec

**Issue:** #142  
**Date:** 2026-05-25  
**Status:** in progress

---

## Problem

Standard date filters ask "between date A and date B." That doesn't help when you remember *when in the year* something happened but not *which year*. Annual festivals, holiday gatherings, seasonal trips — these are naturally retrieved by month, season, or named holiday, not by a date range.

The map view makes this especially compelling: you can locate the place, but still need to narrow to "October" or "Labor Day weekend" to find the right cluster.

## Scope

A `time_pattern` filter that works across all calendar years at once, on both the map and the library grid:

- **Month** — "any October", "any March"
- **Season** — Spring (Mar–May), Summer (Jun–Aug), Fall (Sep–Nov), Winter (Dec–Feb)
- **Day type** — weekends, weekdays
- **Named US holiday presets** — fixed-date and floating, with optional ±2-day expansion window

The ±2-day expansion is fixed at 2 in v1. The internal API accepts a numeric `expand_days` argument so a future UI slider (1–10 days) requires no backend change.

**Out of scope (v1):**
- Configurable expansion window in the UI (reserved for future iteration; mitigated by geographic search)
- Moveable feasts (Easter, Mardi Gras, Lunar New Year) — their calendars require astronomical or religious computation beyond standard date math. The `holiday_date()` function's type system already accommodates a future `lookup` type (pre-computed `{year → date}` table); adding new holidays later is a data change, not an algorithm change.
- Weather-based filtering
- Combining multiple pattern dimensions simultaneously (e.g., "October AND weekends") — use one pattern at a time

---

## Architecture & Data Flow

```
time_pattern query param
    → db/time_patterns.py: parse_pattern(pattern, expand_days, years)
    → returns (sql_fragment, params) to append to any WHERE clause

Library:
    GET /library?time_pattern=season:fall&expand=1
        → app.py reads params
        → db().library_photos(time_pattern=..., time_expand=2)
        → _library_where() calls time_patterns.parse_pattern()
        → SQL with strftime() or OR-of-BETWEEN clauses
        → page reload (existing library pattern)

Map:
    JS: select.onchange → fetch('/api/map-photos?time_pattern=month:10')
        → app.py reads params, queries distinct years
        → time_patterns.parse_pattern() → clause
        → appends to map WHERE, returns filtered JSON
        → JS: markers.clearLayers() → re-plot (preserves map zoom/position)
```

**Pattern encoding** — single `time_pattern` query param, colon-separated:

| Prefix | Example values |
|--------|----------------|
| `month:` | `01` … `12` |
| `season:` | `spring`, `summer`, `fall`, `winter` |
| `daytype:` | `weekend`, `weekday` |
| `holiday:` | see holiday table below |

`expand=1` (integer, default 0) expands holiday presets by ±`expand_days` calendar days. Ignored for month/season/daytype patterns.

---

## Backend

### `db/time_patterns.py` *(new)*

Pure functions — no Flask or DB dependencies.

```python
SEASONS: dict[str, list[str]] = {
    "spring": ["03", "04", "05"],
    "summer": ["06", "07", "08"],
    "fall":   ["09", "10", "11"],
    "winter": ["12", "01", "02"],
}

# (type, *args)
# fixed:       (month, day)
# nth_weekday: (month, weekday[Mon=1], n)   n=-1 → last
HOLIDAYS: dict[str, tuple] = {
    "new_years":      ("fixed",       1,  1),
    "mlk_day":        ("nth_weekday", 1,  1,  3),   # 3rd Mon Jan
    "presidents_day": ("nth_weekday", 2,  1,  3),   # 3rd Mon Feb
    "memorial_day":   ("nth_weekday", 5,  1, -1),   # last Mon May
    "july_4th":       ("fixed",       7,  4),
    "labor_day":      ("nth_weekday", 9,  1,  1),   # 1st Mon Sep
    "columbus_day":   ("nth_weekday", 10, 1,  2),   # 2nd Mon Oct
    "halloween":      ("fixed",       10, 31),
    "thanksgiving":   ("nth_weekday", 11, 3,  4),   # 4th Thu Nov (weekday 3 = Thursday, Mon=0)
    "christmas":      ("fixed",       12, 25),
}
```

> **Weekday convention:** Python's `datetime.weekday()` — Monday = 0, Sunday = 6. `_nth_weekday` uses this convention throughout.

**Public API:**

```python
def holiday_date(year: int, key: str) -> datetime.date | None:
    """Return the date of the named holiday in the given year, or None if unknown."""

def parse_pattern(
    pattern: str,
    expand_days: int,
    years: list[int],
) -> tuple[str, list]:
    """
    Return (sql_fragment, params) to append to a WHERE clause.
    The fragment references column alias 'p.date_taken'.
    Unknown or empty pattern returns ("1=1", []).
    """
```

**Internal helpers:**

```python
def _nth_weekday(year: int, month: int, weekday: int, n: int) -> datetime.date:
    """
    Return the nth occurrence of weekday in (year, month).
    n=1 → first, n=-1 → last.
    weekday: Mon=0 … Sun=6 (Python convention).
    """
```

**SQL generation by pattern type:**

| Pattern type | SQL fragment |
|---|---|
| `month:MM` | `strftime('%m', p.date_taken) = ?` |
| `season:X` | `strftime('%m', p.date_taken) IN (?,?,?)` |
| `daytype:weekend` | `strftime('%w', p.date_taken) IN (?,?)` — values `'0','6'` |
| `daytype:weekday` | `strftime('%w', p.date_taken) NOT IN (?,?)` — values `'0','6'` |
| `holiday:X`, expand_days=0 | `DATE(p.date_taken) = ?` for each year → OR-joined |
| `holiday:X`, expand_days>0 | `(p.date_taken BETWEEN ? AND ?)` for each year → OR-joined |

Note: `strftime('%w', ...)` in SQLite returns `'0'` = Sunday, `'6'` = Saturday.

For holiday patterns, `parse_pattern` iterates `years`, calls `holiday_date(year, key)` for each, skips `None` results, and builds the OR chain. If no valid dates are found (e.g. unknown holiday key, empty years list), returns `("1=1", [])`.

---

### `db/db.py`

**`_library_where`** gains two new optional parameters:

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

When `time_pattern` is set:
1. Query distinct years: `SELECT DISTINCT CAST(strftime('%Y', date_taken) AS INTEGER) AS y FROM photos WHERE date_taken IS NOT NULL ORDER BY y`
2. Call `time_patterns.parse_pattern(time_pattern, time_expand, years)` → `(frag, frag_params)`
3. Append `frag` to `clauses`, extend `params` with `frag_params`

`library_photos`, `library_photo_count`, and `library_photo_ids` all gain matching passthrough kwargs:
```python
time_pattern: str | None = None,
time_expand: int = 2,
```

---

### `reviewer/app.py`

**Library route (`GET /library`)**

```python
time_pattern = request.args.get("time_pattern") or None
time_expand  = int(request.args.get("expand", 2))
```

Pass through to `library_photos()` and `library_photo_count()`. Add to the `filters` dict so pagination links and "Clear filters" preserve them:

```python
filters={
    ...
    "time_pattern": time_pattern or "",
    "expand": 1 if time_expand else 0,
}
```

**`GET /api/map-photos`**

```python
time_pattern = request.args.get("time_pattern") or None
time_expand  = int(request.args.get("expand", 2))
```

When `time_pattern` is set, query distinct years, call `time_patterns.parse_pattern()`, append `AND {frag}` to the map query's WHERE clause before executing. Return filtered JSON as before.

---

## Frontend

### `reviewer/templates/library.html`

A **"Time"** control added to the existing filter bar, between the date range inputs and the Album dropdown:

```
From [__] To [__]  |  Time [dropdown▾] [☐ ±2 days]  |  Album [▾]  Tag [__]  ...
```

```html
<label>Time
  <select name="time_pattern" onchange="this.form.submit()">
    <option value="">Any time</option>
    <optgroup label="Month">
      <option value="month:01" …>January</option>
      … (February … December)
    </optgroup>
    <optgroup label="Season">
      <option value="season:spring">Spring (Mar–May)</option>
      <option value="season:summer">Summer (Jun–Aug)</option>
      <option value="season:fall">Fall (Sep–Nov)</option>
      <option value="season:winter">Winter (Dec–Feb)</option>
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
<label id="lib-expand-label" style="display:none">
  <input type="checkbox" name="expand" value="1"
    {% if filters.expand %}checked{% endif %}
    onchange="this.form.submit()">
  ±2 days
</label>
```

JS (inline, below form): show/hide `#lib-expand-label` based on whether the selected value starts with `holiday:`.

```js
(function() {
  const sel = document.querySelector('[name="time_pattern"]');
  const lbl = document.getElementById('lib-expand-label');
  function sync() { lbl.style.display = sel.value.startsWith('holiday:') ? '' : 'none'; }
  sel.addEventListener('change', sync);
  sync();
})();
```

**"Clear filters"** link condition extended to include `time_pattern`.

**Pagination links** already pass `**filters` — no change needed once `time_pattern` and `expand` are in the `filters` dict.

---

### `reviewer/templates/map.html`

A compact filter bar inserted above the `<div id="map">`, always visible:

```html
<div class="map-filter-bar">
  <label>Time of year
    <select id="map-time-select">
      … (same optgroups as library) …
    </select>
  </label>
  <label id="map-expand-label" style="display:none">
    <input type="checkbox" id="map-expand-cb"> ±2 days
  </label>
  <span id="map-photo-count" style="font-size:12px;color:var(--muted)"></span>
</div>
<div id="map"></div>
```

Map height adjusts to account for the filter bar:

```css
.map-filter-bar {
  height: 40px;
  display: flex; align-items: center; gap: 12px;
  padding: 0 16px;
  border-bottom: 1px solid var(--border);
  background: var(--surface);
  font-size: 13px;
}
#map { height: calc(100vh - 48px - 40px); width: 100%; }
```

JS behaviour:

```js
function buildMapUrl() {
  const p = document.getElementById('map-time-select').value;
  const e = document.getElementById('map-expand-cb').checked ? '&expand=1' : '';
  return p ? `/api/map-photos?time_pattern=${encodeURIComponent(p)}${e}` : '/api/map-photos';
}

function reloadMarkers() {
  markers.clearLayers();
  fetch(buildMapUrl())
    .then(r => r.json())
    .then(photos => {
      plotPhotos(photos);   // extracted helper from existing forEach block
      document.getElementById('map-photo-count').textContent =
        photos.length === 1 ? '1 photo' : `${photos.length} photos`;
    })
    .catch(() => {});
}

document.getElementById('map-time-select').addEventListener('change', function() {
  const lbl = document.getElementById('map-expand-label');
  lbl.style.display = this.value.startsWith('holiday:') ? '' : 'none';
  if (!this.value.startsWith('holiday:')) document.getElementById('map-expand-cb').checked = false;
  reloadMarkers();
});
document.getElementById('map-expand-cb').addEventListener('change', reloadMarkers);
```

The existing `fetch('/api/map-photos')` on page load is replaced by `reloadMarkers()`.

---

## Extension Points

- **Configurable expansion window** — change `expand=1` (boolean) to `expand=N` (integer days, 1–10) in both the UI and the internal API. The `time_expand` parameter already accepts any integer.
- **Moveable feasts** (Easter, Mardi Gras, Lunar New Year) — add a `lookup` holiday type backed by a pre-computed `{year: date}` dict. `holiday_date()` dispatches to it the same way it dispatches to `fixed` and `nth_weekday`. No algorithm change needed.
- **Combining patterns** — e.g. "October AND weekends" would require passing multiple pattern params and ANDing the fragments. Not in scope; the current single-param design doesn't preclude adding a second param later.
- **Per-locale holiday sets** — the `HOLIDAYS` dict could be split into `HOLIDAYS_US`, `HOLIDAYS_CA`, etc., with the active set driven by a config flag.

---

## Testing

### `tests/test_time_patterns.py` *(new — pure unit tests)*

- `holiday_date(2023, "thanksgiving")` → `date(2023, 11, 23)` (4th Thursday)
- `holiday_date(2023, "labor_day")` → `date(2023, 9, 4)` (1st Monday)
- `holiday_date(2023, "memorial_day")` → `date(2023, 5, 29)` (last Monday)
- `holiday_date(2023, "christmas")` → `date(2023, 12, 25)`
- `_nth_weekday` edge case: last Monday of May when May 1 is Monday → last is May 29
- `parse_pattern("month:10", 0, [])` → `("strftime('%m', p.date_taken) = ?", ["10"])`
- `parse_pattern("season:fall", 0, [])` → months `09`, `10`, `11`
- `parse_pattern("daytype:weekend", 0, [])` → weekday IN `'0'`, `'6'`
- `parse_pattern("daytype:weekday", 0, [])` → weekday NOT IN `'0'`, `'6'`
- `parse_pattern("holiday:thanksgiving", 0, [2023])` → exact date `2023-11-23`
- `parse_pattern("holiday:thanksgiving", 2, [2023])` → range `2023-11-21` to `2023-11-25`
- `parse_pattern("holiday:thanksgiving", 2, [2022, 2023])` → two BETWEEN clauses OR-joined
- `parse_pattern("", 0, [])` → `("1=1", [])`
- `parse_pattern("unknown:xyz", 0, [])` → `("1=1", [])`
- `parse_pattern("holiday:unknown_key", 0, [2023])` → `("1=1", [])`

### `tests/test_library_time_filter.py` *(new — integration)*

Setup: insert photos with known dates spanning multiple months/years and weekday/weekend mix.

- `GET /library?time_pattern=month:10` → only October photos
- `GET /library?time_pattern=season:fall` → Sep/Oct/Nov photos only
- `GET /library?time_pattern=daytype:weekend` → only photos on Saturday/Sunday
- `GET /library?time_pattern=holiday:christmas&expand=1` → photos Dec 23–27 (for each year present)
- `GET /library?time_pattern=holiday:thanksgiving` → only exact Thanksgiving dates
- Unknown pattern → all photos returned (no 400, no crash)
- Pagination link contains `time_pattern` and `expand` params when active

### `tests/test_map_time_filter.py` *(new — map API)*

- `GET /api/map-photos?time_pattern=month:10` → only October geotagged photos
- `GET /api/map-photos?time_pattern=season:summer` → Jun/Jul/Aug geotagged photos
- `GET /api/map-photos?time_pattern=holiday:labor_day&expand=1` → correct ±2-day ranges
- `GET /api/map-photos` (no param) → full unfiltered result unchanged
- Unknown pattern → full unfiltered result, no crash
