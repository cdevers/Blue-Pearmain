# Design: Confirmed-None Library Filter (#148)

**Status:** approved  
**Issue:** [#148](https://github.com/cdevers/Blue-Pearmain/issues/148)  
**Date:** 2026-05-27

---

## Summary

Add a `?confirmed_none=1` library filter so photos marked as "intentionally no location" (`geo_confirmed_none = 1`) are discoverable and bulk-undoable. Currently these photos are invisible in the library — correctly excluded from the "No location" filter, but with no way to surface them if the operator wants to reverse a bulk mark.

---

## Background

`geo_confirmed_none = 1` means "review complete: no location exists." It is not missing data — it suppresses future sync proposals and is excluded from data-quality counts. The `POST /api/geo_confirm_none` endpoint supports per-photo undo (`clear=true`) and already accepts arrays of photo IDs, but there is no library view to find these photos in bulk.

The "No location" filter (`?no_location=1`, added in #145) shows unreviewed missing-location photos (`latitude IS NULL AND geo_confirmed_none = 0`). The new filter is its complement: `geo_confirmed_none = 1`.

---

## Design

### DB layer (`db/db.py`)

Add `confirmed_none: bool = False` parameter to:
- `_library_where()` — appends `p.geo_confirmed_none = 1` to the WHERE clause
- `library_photos()` — passes through to `_library_where()`
- `library_photo_count()` — passes through to `_library_where()`
- `library_photo_ids()` — passes through to `_library_where()`

**Mutual exclusivity:** `confirmed_none` and `no_location` are opposite states; passing both is a programming error. If both are `True`, raise `ValueError("confirmed_none and no_location are mutually exclusive")` in `_library_where()`.

Add `confirmed_none_count() -> int` alongside `no_location_count()`:

```python
def confirmed_none_count(self) -> int:
    row = self.conn.execute(
        "SELECT COUNT(*) AS n FROM photos"
        " WHERE geo_confirmed_none = 1"
        "   AND (flickr_deleted IS NULL OR flickr_deleted = 0)"
    ).fetchone()
    return row["n"] if row else 0
```

### Route (`reviewer/app.py`)

In `library()`:
- Parse `confirmed_none = request.args.get("confirmed_none") == "1"`
- Pass `confirmed_none=confirmed_none` to `library_photos()`, `library_photo_count()`, `library_photo_ids()`
- Compute `confirmed_none_count = db().confirmed_none_count()`
- Include `confirmed_none_count` in `render_template()`
- Include `"confirmed_none": "1" if confirmed_none else ""` in the `filters` dict

### Template (`reviewer/templates/library.html`)

**Filter chip** — immediately after the "No location" checkbox:

```html
<label style="display:flex;align-items:center;gap:5px">
  <input type="checkbox" name="confirmed_none" value="1"
         {% if filters.confirmed_none %}checked{% endif %}>
  Reviewed: no location
  {% if confirmed_none_count %}
  <span style="background:var(--border);border-radius:10px;padding:1px 6px;font-size:10px;margin-left:4px">{{ confirmed_none_count }}</span>
  {% endif %}
</label>
```

**Bulk action button** — contextual, shown only when `confirmed_none` filter is active (same pattern as "Remove from album"):

```html
{% if filters.confirmed_none %}
<span class="sep">│</span>
<button onclick="clearNoLocation()">Undo: no location</button>
{% endif %}
```

**JS helper** — mirrors `markNoLocation()`, calls the existing endpoint with `clear=true`:

```javascript
function clearNoLocation() {
  const ids = selectedIds();
  const n = ids.length;
  if (!n) return alert("Select photos first.");
  if (!confirm(`Undo 'no location' for ${n} photo${n === 1 ? "" : "s"}? They will re-enter the unreviewed missing-location queue.`))
    return;
  fetch("/api/geo_confirm_none", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({photo_ids: ids, clear: true}),
  }).then(() => location.reload());
}
```

**Active filter count** — increment the active-filter counter for the confirmed_none filter (the JS expression near the top of the filter panel that counts active filters).

---

## What is NOT changed

- `POST /api/geo_confirm_none` — already supports `clear=true` and arrays; no changes needed
- Schema — `geo_confirmed_none` column already exists (migration 024)
- Per-photo undo button in `photo.html` — already exists; no changes needed

---

## Testing

New test file `tests/test_geo_confirmed_none_filter.py`:

1. `test_confirmed_none_filter_returns_only_confirmed_none_photos` — DB filter returns only `geo_confirmed_none=1` rows
2. `test_confirmed_none_filter_excludes_geotagged_and_unreviewed` — photos with coords or `geo_confirmed_none=0` are excluded
3. `test_confirmed_none_count` — `confirmed_none_count()` returns correct integer
4. `test_confirmed_none_and_no_location_mutually_exclusive` — passing both raises `ValueError`
5. `test_library_route_confirmed_none_param` — `GET /library?confirmed_none=1` returns 200, filters correctly
6. `test_confirmed_none_chip_visible_in_template` — template renders "Reviewed: no location" chip with count
7. `test_undo_button_visible_only_when_filter_active` — "Undo: no location" button present when `confirmed_none=1`, absent otherwise

---

## Scope

| Artifact | Change |
|---|---|
| `db/db.py` | `confirmed_none` param in 4 functions; `confirmed_none_count()` |
| `reviewer/app.py` | Parse param, compute count, pass to template |
| `reviewer/templates/library.html` | Chip + contextual undo button + JS helper + active-filter counter |
| `tests/test_geo_confirmed_none_filter.py` | New file, 7 tests |

No schema changes. No new endpoints.
