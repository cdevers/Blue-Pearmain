# Map Spatial Filter — Spec

**Issue:** [#144](https://github.com/cdevers/Blue-Pearmain/issues/144)
**Status:** in progress

---

## Goal

Let a user draw a rectangle on the map (or use the current viewport), then open all matching photos in the Library UI — combined with the map's time filter — in a new tab. From the library, existing bulk operations (tag, add to album) apply to the spatial+temporal result set.

## Motivation

The map spans both the Apple Photos library (recent years) and the full Flickr archive (~20 years). This creates a unified way to find and organise photos by place and time — e.g. all Honk festival photos at Davis Square across every year going back to 2009. From the library view, those photos can be bulk-tagged or added to an album.

---

## Architecture

Two pieces connected only by a URL:

1. **Map** — gains a draw tool and an "Open in Library" button. No server changes needed on the map side beyond a bug fix.
2. **Library** — gains a bbox filter (`lat_min`, `lat_max`, `lon_min`, `lon_max`). Treated as one more composable filter in the existing `_library_where` pipeline.

The URL format:
```
/library?lat_min=42.35000&lat_max=42.41000&lon_min=-71.12000&lon_max=-71.08000&time_pattern=holiday:columbus_day
```

---

## Files

| File | Change |
|---|---|
| `db/photo_filters.py` | Add `build_bbox_clause` |
| `db/db.py` | `_library_where` — 4 new params; update 3 callers |
| `reviewer/app.py` | Library route: parse bbox; bulk-edit route: pass bbox; `/api/map-photos`: fix `flickr_deleted` bug |
| `reviewer/templates/library.html` | `filter_count`, bbox chip, hidden inputs, `_buildPayload` |
| `reviewer/templates/map.html` | Leaflet.draw CDN, draw/clear buttons, Open in Library button, JS |
| `tests/test_photo_filters.py` | `build_bbox_clause` unit tests |
| `tests/test_library_search.py` | bbox integration tests (route + `_library_where`) |

---

## Detailed Design

### 1. `db/photo_filters.py` — `build_bbox_clause`

```python
def build_bbox_clause(
    lat_min: float, lat_max: float, lon_min: float, lon_max: float
) -> tuple[str, list]:
    sql = (
        "p.latitude IS NOT NULL AND p.longitude IS NOT NULL"
        " AND p.latitude BETWEEN ? AND ?"
        " AND p.longitude BETWEEN ? AND ?"
    )
    return sql, [lat_min, lat_max, lon_min, lon_max]
```

All four params are required — a partial set is not a valid bbox. The caller (`_library_where`) only dispatches to this function when all four are present.

---

### 2. `db/db.py` — `_library_where`

Add four new optional params:

```python
def _library_where(
    self,
    ...
    lat_min: float | None = None,   # #144 bbox
    lat_max: float | None = None,
    lon_min: float | None = None,
    lon_max: float | None = None,
) -> tuple[str, list]:
```

Dispatch block (after the `#141` block):

```python
# #144 — spatial bounding box
if lat_min is not None and lat_max is not None \
        and lon_min is not None and lon_max is not None:
    from db.photo_filters import build_bbox_clause
    frag, frag_params = build_bbox_clause(lat_min, lat_max, lon_min, lon_max)
    clauses.append(frag)
    params.extend(frag_params)
```

All three callers (`library_photos`, `library_photo_count`, `library_photo_ids`) already use keyword args — add `lat_min=lat_min, lat_max=lat_max, lon_min=lon_min, lon_max=lon_max` to each.

---

### 3. `reviewer/app.py`

#### Library route — parse bbox

Add `_parse_float` as a **module-level** helper (above the route functions):

```python
def _parse_float(v: str | None) -> float | None:
    """Parse a query-string value to float; return None on missing or invalid input."""
    try:
        return float(v) if v is not None else None
    except ValueError:
        return None
```

Then in the library route, after the existing `person` / `date_alias` block:

```python
lat_min = _parse_float(request.args.get("lat_min"))
lat_max = _parse_float(request.args.get("lat_max"))
lon_min = _parse_float(request.args.get("lon_min"))
lon_max = _parse_float(request.args.get("lon_max"))
# Only treat as bbox if all four are present
if not all(v is not None for v in (lat_min, lat_max, lon_min, lon_max)):
    lat_min = lat_max = lon_min = lon_max = None
```

`_parse_float` can be a module-level helper (reusable, testable).

Add to `filters` dict (store as rounded strings so URL round-trip is clean):

```python
"lat_min": f"{lat_min:.5f}" if lat_min is not None else "",
"lat_max": f"{lat_max:.5f}" if lat_max is not None else "",
"lon_min": f"{lon_min:.5f}" if lon_min is not None else "",
"lon_max": f"{lon_max:.5f}" if lon_max is not None else "",
```

Pass to all three library DB calls with keyword args:
```python
lat_min=lat_min, lat_max=lat_max, lon_min=lon_min, lon_max=lon_max,
```

#### Bulk-edit route — pass bbox

In the `/api/bulk-edit` handler, the `filter` payload from the client already passes filter params through. Add to the extraction block:

```python
lat_min = _parse_float(f.get("lat_min"))
lat_max = _parse_float(f.get("lat_max"))
lon_min = _parse_float(f.get("lon_min"))
lon_max = _parse_float(f.get("lon_max"))
if not all(v is not None for v in (lat_min, lat_max, lon_min, lon_max)):
    lat_min = lat_max = lon_min = lon_max = None
```

Pass to `library_photo_ids` with keyword args.

#### `/api/map-photos` bug fix

Add `AND p.flickr_deleted = 0` to the WHERE clause:

```python
"FROM photos p "
f"WHERE p.latitude IS NOT NULL AND p.longitude IS NOT NULL"
f" AND p.flickr_deleted = 0{extra_where}",
```

---

### 4. `reviewer/templates/library.html`

#### `filter_count`

Add one term:

```jinja2
(1 if filters.lat_min else 0) +
```

(All four bbox params are set together or not at all, so checking one is sufficient.)

#### Hidden inputs (bbox round-trips through pagination)

Inside the `<form>`, before the filter panel:

```html
{% if filters.lat_min %}
<input type="hidden" name="lat_min" value="{{ filters.lat_min }}">
<input type="hidden" name="lat_max" value="{{ filters.lat_max }}">
<input type="hidden" name="lon_min" value="{{ filters.lon_min }}">
<input type="hidden" name="lon_max" value="{{ filters.lon_max }}">
{% endif %}
```

Hidden inputs are submitted with the form on pagination, preserving the bbox.

#### "Map area ✕" chip in filter panel

At the top of the filter panel body, before Row 1:

```html
{% if filters.lat_min %}
<div class="lib-filter-row">
  <span style="font-size:12px;color:var(--muted)">Map area</span>
  <a href="{{ url_for('library',
       q=filters.q or None,
       date_from=filters.date_from or None,
       date_to=filters.date_to or None,
       album_id=filters.album_id or None,
       tag=filters.tag or None,
       status=filters.status or None,
       untitled=filters.untitled or None,
       time_pattern=filters.time_pattern or None,
       expand=filters.expand or None,
       country=filters.country or None,
       state=filters.state or None,
       city=filters.city or None,
       neighborhood=filters.neighborhood or None,
       person=filters.person or None) }}"
     style="font-size:12px;color:var(--muted);text-decoration:none">✕</a>
</div>
{% endif %}
```

This link reconstructs the URL with every current filter *except* the bbox params.

#### `_buildPayload`

Add four fields inside the `if (_selectAllFilter)` block:

```javascript
lat_min: fd.get('lat_min') ? parseFloat(fd.get('lat_min')) : null,
lat_max: fd.get('lat_max') ? parseFloat(fd.get('lat_max')) : null,
lon_min: fd.get('lon_min') ? parseFloat(fd.get('lon_min')) : null,
lon_max: fd.get('lon_max') ? parseFloat(fd.get('lon_max')) : null,
```

---

### 5. `reviewer/templates/map.html`

#### CDN additions (`{% block extra_head %}`)

```html
<link rel="stylesheet" href="https://unpkg.com/leaflet-draw@1.0.4/dist/leaflet.draw.css" />
```

And in `{% block content %}` before the inline `<script>`:

```html
<script src="https://unpkg.com/leaflet-draw@1.0.4/dist/leaflet.draw.js"></script>
```

#### Filter bar additions

In `.map-filter-bar`, after the expand checkbox:

```html
<button type="button" id="map-draw-btn" onclick="toggleDraw()">Draw selection</button>
<button type="button" id="map-clear-btn" onclick="clearSelection()"
        style="display:none">✕ Clear selection</button>
<button type="button" id="map-open-lib-btn" onclick="openInLibrary()">Open in Library ↗</button>
```

#### JavaScript

```javascript
// ── Spatial selection ──────────────────────────────────────
let _drawnLayer = null;
let _drawnBounds = null;
let _drawControl = null;

const _drawnItems = new L.FeatureGroup();
map.addLayer(_drawnItems);

function _initDrawControl() {
  if (_drawControl) return;
  _drawControl = new L.Control.Draw({
    draw: {
      rectangle: true,
      polyline: false, polygon: false, circle: false,
      circlemarker: false, marker: false,
    },
    edit: { featureGroup: _drawnItems, edit: false, remove: false },
  });
  map.addControl(_drawControl);
}

function toggleDraw() {
  _initDrawControl();
  // Trigger the rectangle draw handler directly
  new L.Draw.Rectangle(map, _drawControl.options.draw.rectangle).enable();
}

map.on(L.Draw.Event.CREATED, function (e) {
  if (_drawnLayer) _drawnItems.removeLayer(_drawnLayer);
  _drawnLayer = e.layer;
  _drawnItems.addLayer(_drawnLayer);
  _drawnBounds = _drawnLayer.getBounds();
  document.getElementById('map-draw-btn').style.display = 'none';
  document.getElementById('map-clear-btn').style.display = '';
});

function clearSelection() {
  if (_drawnLayer) _drawnItems.removeLayer(_drawnLayer);
  _drawnLayer = null;
  _drawnBounds = null;
  document.getElementById('map-draw-btn').style.display = '';
  document.getElementById('map-clear-btn').style.display = 'none';
}

function openInLibrary() {
  const bounds = _drawnBounds || map.getBounds();
  const params = new URLSearchParams({
    lat_min: bounds.getSouth().toFixed(5),
    lat_max: bounds.getNorth().toFixed(5),
    lon_min: bounds.getWest().toFixed(5),
    lon_max: bounds.getEast().toFixed(5),
  });
  const tp = document.getElementById('map-time-select').value;
  if (tp) params.set('time_pattern', tp);
  if (document.getElementById('map-expand-cb').checked) params.set('expand', '1');
  window.open('/library?' + params.toString(), '_blank');
}
```

---

## Testing

### `tests/test_photo_filters.py` — `build_bbox_clause`

```python
def test_build_bbox_clause_inside(db_with_geotagged):
    # photo at (42.38, -71.10) — inside box (42.35–42.41, -71.12–-71.08)
    photos = db.library_photos(..., lat_min=42.35, lat_max=42.41,
                                    lon_min=-71.12, lon_max=-71.08)
    assert len(photos) == 1

def test_build_bbox_clause_outside(db_with_geotagged):
    # photo at (40.71, -74.00) — outside box
    photos = db.library_photos(..., lat_min=42.35, lat_max=42.41,
                                    lon_min=-71.12, lon_max=-71.08)
    assert len(photos) == 0

def test_build_bbox_clause_boundary(db_with_geotagged):
    # photo exactly on boundary lat=42.35 — BETWEEN is inclusive
    photos = db.library_photos(..., lat_min=42.35, lat_max=42.41,
                                    lon_min=-71.12, lon_max=-71.08)
    assert len(photos) == 1

def test_build_bbox_clause_partial_ignored(db_with_geotagged):
    # Only lat_min provided — bbox not applied, all photos returned
    photos = db.library_photos(..., lat_min=42.35)  # lat_max/lon_ = None
    assert len(photos) == total_count
```

### `tests/test_library_search.py` — bbox integration

```python
def test_library_bbox_filter(client_with_geo):
    r = client.get('/library?lat_min=42.35&lat_max=42.41&lon_min=-71.12&lon_max=-71.08')
    assert r.status_code == 200
    # only photos inside the box in response
    assert b'Paris' not in r.data   # photo at 48.8, 2.3 — outside

def test_library_bbox_plus_time_pattern(client_with_geo):
    # seed: one photo inside box in October, one inside box in July, one outside box in October
    r = client.get('/library?lat_min=42.35&lat_max=42.41&lon_min=-71.12&lon_max=-71.08'
                   '&time_pattern=month:10')
    data = r.data.decode()
    assert 'inside-oct' in data      # inside box + October ✓
    assert 'inside-jul' not in data  # inside box but wrong month
    assert 'outside-oct' not in data # right month but outside box

def test_library_bbox_partial_params_ignored(client_with_geo):
    # Only 3 of 4 bbox params — treated as no bbox filter
    r = client.get('/library?lat_min=42.35&lat_max=42.41&lon_min=-71.12')
    assert r.status_code == 200
    # all photos returned (no bbox applied)

def test_library_bbox_in_filter_count(client_with_geo):
    r = client.get('/library?lat_min=42.35&lat_max=42.41&lon_min=-71.12&lon_max=-71.08')
    assert b'Filters (1)' in r.data

def test_library_bbox_chip_shown(client_with_geo):
    r = client.get('/library?lat_min=42.35&lat_max=42.41&lon_min=-71.12&lon_max=-71.08')
    assert b'Map area' in r.data

def test_map_photos_excludes_deleted(client_with_deleted_geo):
    r = client.get('/api/map-photos')
    ids = [p['id'] for p in r.get_json()]
    assert deleted_photo_id not in ids
```

---

## Stretch Goal (future issue)

**Multiple drawn rectangles** — store an array of bounds, combine as OR in SQL:
```sql
(bbox1_clause) OR (bbox2_clause) OR …
```
URL encoding: JSON-encoded array or repeated `bbox[]` params. Separate issue when needed.

---

## Out of Scope

- Freehand polygon selection
- Editing a drawn rectangle after creation (clear and redraw)
- Map highlighting the active rectangle when navigating back from library
