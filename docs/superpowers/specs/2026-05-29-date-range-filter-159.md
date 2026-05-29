# Spec: Day-granularity date range filter (#159)

**Status:** ready for implementation planning  
**Issue:** https://github.com/cdevers/Blue-Pearmain/issues/159  
**Date:** 2026-05-29

---

## Problem

The shared filter bar exposes `year_from` / `year_to` as integer year fields. The smallest expressible range is a full calendar year. There is no way to say "photos from this trip" or "photos taken on this day."

---

## Solution overview

Replace the two year-number inputs in `_filter_bar.html` with native `<input type="date">` fields (`date_from` / `date_to`). Either field is optional — open-ended ranges are valid ("from this date onwards", "up to this date"). The `time_pattern` filter continues to AND with the date range.

---

## Filter bar changes (`_filter_bar.html`)

The macro currently renders:

```html
<span class="shared-year-range">
  <label>Year
    <input type="number" name="year_from" …>
  </label>
  <span>–</span>
  <label>
    <input type="number" name="year_to" …>
  </label>
</span>
```

Replace with:

```html
<label>From
  <input type="date" name="date_from" value="{{ filters.date_from or '' }}">
</label>
<span class="dash">–</span>
<label>To
  <input type="date" name="date_to" value="{{ filters.date_to or '' }}">
</label>
```

- Labels: "From" / "To" (no "Year" heading — the date picker makes the meaning self-evident)
- Native `<input type="date">` — browser provides calendar popup; also accepts typed `YYYY-MM-DD`
- `color-scheme: dark` CSS so the picker matches the dark theme on macOS/iOS
- `year_from` / `year_to` hidden inputs are **not** emitted — old form submissions use the new names

No macro signature change — `filters` is a dict and the macro just reads `filters.date_from` and `filters.date_to` directly. The routes must pass `date_from` and `date_to` in the template context (replacing `year_from` / `year_to`).

---

## Data model (`app.py`)

### `SharedFilters` TypedDict

```python
# Before
class SharedFilters(TypedDict):
    time_pattern: str
    year_from: int | None
    year_to: int | None
    album_id: int | None
    person: str | None
    status: str | None
    tag: str | None
    expand: bool

# After
class SharedFilters(TypedDict):
    time_pattern: str
    date_from: str | None   # YYYY-MM-DD or None
    date_to: str | None     # YYYY-MM-DD or None (inclusive end)
    album_id: int | None
    person: str | None
    status: str | None
    tag: str | None
    expand: bool
```

### `_safe_date(key)` helper

Replaces `_safe_year()`. Reads `request.args.get(key)`, validates the string is a valid ISO date (`YYYY-MM-DD`), returns the string on success or `None` on failure (missing, wrong format, impossible date).

```python
def _safe_date(key: str) -> str | None:
    val = (request.args.get(key) or "").strip()
    if not val:
        return None
    try:
        date.fromisoformat(val)   # validates YYYY-MM-DD
        return val
    except ValueError:
        return None
```

### `normalize_shared_filters()`

1. Try `_safe_date("date_from")` and `_safe_date("date_to")` first.
2. **Backward compat:** if either date param is absent, check for legacy `year_from` / `year_to` integer params and convert:
   - `year_from` → `f"{year}-01-01"`
   - `year_to`   → `f"{year}-12-31"`
3. If `date_from > date_to` (both set), swap them.
4. Return `SharedFilters` with `date_from` / `date_to` (no `year_from` / `year_to`).

---

## Route changes

### Library route (`/library`)

Current SQL filtering (year-level):

```python
if year_from is not None:
    where_frags.append("p.date_taken >= ?")
    where_params.append(f"{year_from:04d}-01-01")
if year_to is not None:
    where_frags.append("p.date_taken < ?")
    where_params.append(f"{year_to + 1:04d}-01-01")
```

New SQL filtering (day-level):

```python
if sf["date_from"]:
    where_frags.append("p.date_taken >= ?")
    where_params.append(sf["date_from"])
if sf["date_to"]:
    # inclusive end: include full final day
    exclusive_end = str(date.fromisoformat(sf["date_to"]) + timedelta(days=1))
    where_frags.append("p.date_taken < ?")
    where_params.append(exclusive_end)
```

The `date_to` boundary is **inclusive** for the user but stored as an exclusive SQL boundary (day+1). A photo with `date_taken = '2019-08-30T18:00:00'` is included when `date_to = '2019-08-30'`.

**`date_taken` format note:** The DB column contains mixed formats — `'YYYY-MM-DD HH:MM:SS'` and `'YYYY-MM-DDTHH:MM:SS±HH:MM'` both appear. Both sort correctly under SQLite lexicographic `>=` / `<` comparison against a plain `YYYY-MM-DD` boundary string, because the date portion is always the first 10 characters. The map endpoint already relies on this property successfully.

Template context: replace `year_from` / `year_to` keys with `date_from` / `date_to`:

```python
# Before
"year_from": sf["year_from"] if sf["year_from"] is not None else "",
"year_to":   sf["year_to"]   if sf["year_to"]   is not None else "",

# After
"date_from": sf["date_from"] or "",
"date_to":   sf["date_to"]   or "",
```

**Chip row:** currently renders year chips. Update to render date chips:

```jinja
{% if filters.date_from %}
  <span class="filter-chip">
    from {{ filters.date_from | format_date }}
    <a href="…?date_from=&…">×</a>
  </span>
{% endif %}
{% if filters.date_to %}
  <span class="filter-chip">
    to {{ filters.date_to | format_date }}
    <a href="…?date_to=&…">×</a>
  </span>
{% endif %}
```

`format_date` is a Jinja filter registered in `app.py`:

```python
@app.template_filter("format_date")
def _format_date_filter(s: str) -> str:
    """Format a YYYY-MM-DD string as 'Jun 15, 2018'."""
    try:
        return date.fromisoformat(s).strftime("%b %-d, %Y")
    except (ValueError, AttributeError):
        return s
```

**Filter count / chip independence:** `date_from` and `date_to` each have their own chip with their own dismiss link. Dismissing `date_from` clears only `date_from`; `date_to` remains active (and vice versa). However, together they count as **one** active filter dimension in the `filter_count` tally — matching the existing behavior for `year_from`/`year_to`. The library template already has this expression: `(1 if filters.date_from or filters.date_to ... else 0)` — no change needed there.

**Library → Map deep link ("View on map"):** replace `year_from`/`year_to` query params with `date_from`/`date_to`.

### Map route (`/map` + `/api/map-photos`)

The map already accepts `date_from`/`date_to` query params and has SQL wiring. The current fallback that derives them from `year_from`/`year_to`:

```python
if sf["year_from"] is not None and not date_from:
    date_from = f"{sf['year_from']:04d}-01-01"
if sf["year_to"] is not None and not date_to:
    date_to = f"{sf['year_to'] + 1:04d}-01-01T00:00:00"
```

Replace with direct use of SharedFilters:

```python
date_from = sf["date_from"] or None
date_to_raw = sf["date_to"] or None
if date_to_raw:
    date_to = str(date.fromisoformat(date_to_raw) + timedelta(days=1))
else:
    date_to = None
```

The existing `WHERE p.date_taken >= ?` / `p.date_taken < ?` SQL clauses are unchanged.

Template context for the map: update to pass `date_from`/`date_to` instead of `year_from`/`year_to`.

---

## `time_pattern` interaction

Unchanged. `time_pattern` and `date_from`/`date_to` AND together:
- `time_pattern = month:07` + `date_from = 2010-01-01` + `date_to = 2020-12-31` → all Julys between 2010 and 2020

No special-casing required; the WHERE clauses are independent and naturally compose.

---

## Backward compatibility

| Old URL param | Behaviour |
|---|---|
| `?year_from=2019` | Converted to `date_from=2019-01-01` in normalization |
| `?year_to=2020` | Converted to `date_to=2020-12-31` in normalization |
| `?date_from=2019-06-15` | Used directly |
| `?date_from=bad-value` | Silently ignored (treated as None) |
| Both `date_from` and `year_from` present | `date_from` wins |

Saved bookmarks, shared links, and the library→map deep link from before this change continue to work without redirects.

---

## Tests

| Test | What it checks |
|---|---|
| `test_safe_date_valid` | `_safe_date` returns valid YYYY-MM-DD strings unchanged |
| `test_safe_date_invalid` | Bad format, impossible date, empty string → None |
| `test_normalize_date_from_only` | Only `date_from` set → `date_to` is None |
| `test_normalize_date_to_only` | Only `date_to` set → `date_from` is None |
| `test_normalize_date_swap` | `date_from > date_to` → swapped |
| `test_normalize_legacy_year_from` | `year_from=2019` → `date_from='2019-01-01'` |
| `test_normalize_legacy_year_to` | `year_to=2020` → `date_to='2020-12-31'` |
| `test_normalize_date_wins_over_year` | Both present → date params win |
| `test_library_date_from_filter` | Photos before cutoff excluded |
| `test_library_date_to_filter` | Photos after cutoff excluded |
| `test_library_date_to_inclusive` | Photo on boundary day included; photo next day excluded |
| `test_map_photos_date_from_filter` | Map API respects `date_from` |
| `test_map_photos_date_to_inclusive` | Map API boundary day included |
| `test_library_date_swap_integration` | Route-level: reversed range (`date_from > date_to`) returns same records as correct order |

---

## What is explicitly out of scope

- Month-precision partial dates (e.g. `2019-06` without a day) — `<input type="date">` always provides a full date; the server validates YYYY-MM-DD and rejects anything else
- Multi-range selection (e.g. two separate date spans) — not in scope
- Any changes to `time_pattern` options or logic
- Any changes to album, person, tag, or privacy filters
