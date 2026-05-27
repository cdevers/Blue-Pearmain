# Photo Detail Mini-Map (#146) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Embed a small Leaflet map on the photo detail page for geotagged photos, and add `?photo_id=` centering support to `/map` so the mini-map's "View full map" link opens the full map centred on that photo with its popup open.

**Architecture:** Two coordinated changes — (1) `map_view()` in `app.py` gains an optional `photo_id` query param that overrides the default average-centre and passes a `highlight_id` to the template; `map.html` JS stores marker references and calls `markers.zoomToShowLayer` after load. (2) `photo.html` conditionally loads Leaflet, renders a 160 px map div in the detail panel, and links to `/map?photo_id={id}`.

**Tech Stack:** Flask/Jinja2, Leaflet 1.9.4 (CDN, already used by `map.html`), pytest (existing test patterns in `tests/test_map_routes.py`)

---

## File map

| File | Change |
|---|---|
| `reviewer/app.py` | `map_view()` — accept `photo_id` query param |
| `reviewer/templates/map.html` | Store marker refs; add `highlightId` var; add `tryHighlight()` |
| `reviewer/templates/photo.html` | Conditional Leaflet CSS (`extra_head`), mini-map section + coords display, Leaflet JS init (`scripts`) |
| `tests/test_map_routes.py` | Extend with `?photo_id=` tests |
| `tests/test_photo_minimap.py` | New — photo detail mini-map rendering tests |

---

## Task 1: `?photo_id=` — backend route + tests

**Files:**
- Modify: `reviewer/app.py` (around line 786, the `map_view` function)
- Modify: `tests/test_map_routes.py` (append new test class)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_map_routes.py`:

```python
class TestMapPhotoIdParam:
    def test_centers_on_photo_when_photo_id_given(self, client_geo):
        c, p1, *_ = client_geo
        # p1 has latitude=48.8566, longitude=2.3522 (set in client_geo fixture)
        html = c.get(f"/map?photo_id={p1}").data.decode()
        assert "48.8566" in html
        assert "2.3522" in html

    def test_highlight_id_equals_photo_id_in_template(self, client_geo):
        c, p1, *_ = client_geo
        html = c.get(f"/map?photo_id={p1}").data.decode()
        # Template renders: const highlightId = <p1>;
        assert "highlightId" in html
        assert str(p1) in html

    def test_highlight_id_is_null_when_no_photo_id(self, client_geo):
        c, *_ = client_geo
        html = c.get("/map").data.decode()
        assert "highlightId" in html
        assert "null" in html

    def test_falls_back_to_average_when_photo_has_no_coords(self, client_geo):
        c, p1, p2, p3, p4, _ = client_geo
        # p4 has no latitude/longitude
        html = c.get(f"/map?photo_id={p4}").data.decode()
        assert "null" in html  # highlight_id falls back to None → null

    def test_falls_back_gracefully_when_photo_id_missing(self, client_geo):
        c, *_ = client_geo
        resp = c.get("/map?photo_id=99999")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "null" in html  # highlight_id = None → null
```

- [ ] **Step 2: Run to confirm failure**

```
python -m pytest tests/test_map_routes.py::TestMapPhotoIdParam -v
```

Expected: 5 failures — `highlightId` not in template, wrong centre coords.

- [ ] **Step 3: Implement `map_view()` in `app.py`**

Replace the existing `map_view` function (currently lines ~786–799):

```python
@app.route("/map")
def map_view() -> str:
    photo_id_param = request.args.get("photo_id", type=int)
    highlight_id: int | None = None
    center_lat: float
    center_lon: float

    if photo_id_param is not None:
        row = (
            db()
            .conn.execute(
                "SELECT latitude, longitude FROM photos "
                "WHERE id = ? AND latitude IS NOT NULL",
                (photo_id_param,),
            )
            .fetchone()
        )
        if row:
            center_lat = row["latitude"]
            center_lon = row["longitude"]
            highlight_id = photo_id_param
        else:
            photo_id_param = None  # fall through to average below

    if photo_id_param is None:
        row = (
            db()
            .conn.execute(
                "SELECT AVG(latitude) AS lat, AVG(longitude) AS lon "
                "FROM photos WHERE latitude IS NOT NULL AND longitude IS NOT NULL"
            )
            .fetchone()
        )
        center_lat = row["lat"] if row["lat"] is not None else 20.0
        center_lon = row["lon"] if row["lon"] is not None else 0.0

    return render_template(
        "map.html",
        center_lat=center_lat,
        center_lon=center_lon,
        highlight_id=highlight_id,
    )
```

- [ ] **Step 4: Run tests**

```
python -m pytest tests/test_map_routes.py::TestMapPhotoIdParam -v
```

Expected: still failing — `highlightId` not in template yet. That's correct; the template change is Task 2.

- [ ] **Step 5: Add `highlight_id` default to existing template render calls**

The existing `map.html` template doesn't reference `highlight_id` yet (Task 2 adds it), but since Jinja2 raises `UndefinedError` only when the variable is actually used in the template, the existing tests won't break until Task 2 adds the `highlightId` JS line. No action needed here.

---

## Task 2: `?photo_id=` — frontend JS in `map.html` + tests pass

**Files:**
- Modify: `reviewer/templates/map.html`

- [ ] **Step 1: Add `_markerById` dict, `highlightId` constant, and `tryHighlight()` to `map.html`**

In `map.html`, the JS currently has:

```javascript
const markers = L.markerClusterGroup();
map.addLayer(markers);   // added once; reloadMarkers only calls clearLayers()
```

After that line, add:

```javascript
const _markerById = {};   // photo id (number) → Leaflet marker; rebuilt on each reload
const highlightId = {{ highlight_id | tojson }};   // null or integer from backend

function tryHighlight() {
  if (!highlightId) return;
  const marker = _markerById[highlightId];
  if (!marker) return;
  markers.zoomToShowLayer(marker, () => marker.openPopup());
}
```

- [ ] **Step 2: Store each marker in `_markerById` inside `plotPhotos`**

The existing `plotPhotos` function body has:

```javascript
  photos.forEach(p => {
    const marker = L.marker([p.lat, p.lon]);
```

After the `markers.addLayer(marker)` line inside the `forEach`, add:

```javascript
    _markerById[p.id] = marker;
```

And at the end of `plotPhotos`, after the count display line, add a call to `tryHighlight()`:

```javascript
  tryHighlight();
```

The full updated `plotPhotos` looks like this:

```javascript
function plotPhotos(photos, requestId) {
  if (requestId !== _currentRequest) return;   // stale response — discard
  photos.forEach(p => {
    const marker = L.marker([p.lat, p.lon]);
    const title = p.title || '';
    const shortTitle = title.length > 60 ? title.slice(0, 60) + '…' : title;
    let links = `<a href="/photo/${p.id}">Open photo</a>`;
    if (p.flickr_url) links += `<a href="${esc(p.flickr_url)}" target="_blank" rel="noopener">Flickr &#x2197;</a>`;
    if (p.date)       links += `<a href="/library?date=${esc(p.date)}">Show this day</a>`;
    marker.bindPopup(`
      <div class="map-popup">
        <img src="/thumb/${p.id}" alt="">
        <div class="pop-title">${esc(shortTitle)}</div>
        <div class="pop-date">${esc(p.date || '')}</div>
        <div class="pop-links">${links}</div>
      </div>
    `, { maxWidth: 200 });
    markers.addLayer(marker);
    _markerById[p.id] = marker;
  });
  document.getElementById('map-photo-count').textContent =
    photos.length === 1 ? '1 photo' : `${photos.length} photos`;
  tryHighlight();
}
```

- [ ] **Step 3: Clear `_markerById` at the start of `reloadMarkers`**

The existing `reloadMarkers`:

```javascript
function reloadMarkers() {
  const requestId = ++_currentRequest;
  markers.clearLayers();
```

Add one line after `markers.clearLayers()`:

```javascript
  Object.keys(_markerById).forEach(k => delete _markerById[k]);
```

- [ ] **Step 4: Run tests**

```
python -m pytest tests/test_map_routes.py::TestMapPhotoIdParam -v
```

Expected: all 5 tests pass.

- [ ] **Step 5: Run full map test suite to confirm no regressions**

```
python -m pytest tests/test_map_routes.py tests/test_map_time_filter.py -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add reviewer/app.py reviewer/templates/map.html tests/test_map_routes.py
git commit -m "feat(#146): map view ?photo_id= param — centre + auto-open popup

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 3: Mini-map on photo detail page + tests

**Files:**
- Modify: `reviewer/templates/photo.html`
- Create: `tests/test_photo_minimap.py`

- [ ] **Step 1: Write the failing tests in `tests/test_photo_minimap.py`**

```python
"""
tests/test_photo_minimap.py — photo detail page mini-map rendering (#146)

Run from repo root:
    python -m pytest tests/test_photo_minimap.py -v
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

import reviewer.app as app_module
from db.db import Database


def _photo(i: int, **kwargs) -> dict:
    base: dict = {
        "uuid": f"minimap-u{i}",
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
def client_detail():
    """DB with one geotagged photo (Boston) and one ungeotagged photo."""
    with tempfile.TemporaryDirectory() as tmp:
        test_db = Database(Path(tmp) / "test.db")
        geo_id = test_db.upsert_photo(
            _photo(
                1,
                latitude=42.3601,
                longitude=-71.0589,
                photos_title="Fenway Park",
            )
        )
        no_geo_id = test_db.upsert_photo(
            _photo(2, photos_title="Screenshot")
        )
        app_module._db = test_db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test-secret"
        with app_module.app.test_client() as c:
            yield c, geo_id, no_geo_id
        app_module._db = None


class TestPhotoDetailMinimap:
    def test_minimap_div_present_for_geotagged_photo(self, client_detail):
        c, geo_id, _ = client_detail
        html = c.get(f"/photo/{geo_id}").data.decode()
        assert 'id="mini-map"' in html

    def test_leaflet_css_loaded_for_geotagged_photo(self, client_detail):
        c, geo_id, _ = client_detail
        html = c.get(f"/photo/{geo_id}").data.decode()
        assert "leaflet@1.9.4/dist/leaflet.css" in html

    def test_leaflet_js_loaded_for_geotagged_photo(self, client_detail):
        c, geo_id, _ = client_detail
        html = c.get(f"/photo/{geo_id}").data.decode()
        assert "leaflet@1.9.4/dist/leaflet.js" in html

    def test_coordinates_displayed_for_geotagged_photo(self, client_detail):
        c, geo_id, _ = client_detail
        html = c.get(f"/photo/{geo_id}").data.decode()
        assert "42.3601" in html  # latitude
        assert "71.0589" in html  # longitude abs value

    def test_view_full_map_link_uses_photo_id_param(self, client_detail):
        c, geo_id, _ = client_detail
        html = c.get(f"/photo/{geo_id}").data.decode()
        assert f"/map?photo_id={geo_id}" in html

    def test_minimap_absent_for_ungeotagged_photo(self, client_detail):
        c, _, no_geo_id = client_detail
        html = c.get(f"/photo/{no_geo_id}").data.decode()
        assert 'id="mini-map"' not in html

    def test_leaflet_not_loaded_for_ungeotagged_photo(self, client_detail):
        c, _, no_geo_id = client_detail
        html = c.get(f"/photo/{no_geo_id}").data.decode()
        assert "leaflet@1.9.4/dist/leaflet.css" not in html
```

- [ ] **Step 2: Run to confirm failure**

```
python -m pytest tests/test_photo_minimap.py -v
```

Expected: all 7 fail — `id="mini-map"` not in template, no Leaflet CSS, etc.

- [ ] **Step 3: Add Leaflet CSS to `photo.html` `extra_head` block**

`photo.html` has no `{% block extra_head %}` yet. Add it immediately before `{% block extra_style %}` (currently line 4):

```html
{% block extra_head %}
{% if photo.latitude %}
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
{% endif %}
{% endblock %}

{% block extra_style %}
```

- [ ] **Step 4: Add the mini-map section to the detail panel in `photo.html`**

Find the end of the "Details" meta-section. It currently ends with the `{% endif %}` for the `photo.uuid` block (around line 396). After that `</div>` (closing the Details `meta-section`), add:

```html
    </div>

    <!-- Mini-map (geotagged photos only) -->
    {% if photo.latitude %}
    <div class="meta-section">
      <h3>Map</h3>
      <div id="mini-map" style="height:160px; border-radius:4px; overflow:hidden; margin-bottom:6px"></div>
      <div style="font-size:11px; color:var(--muted)">
        {{ "%.4f"|format(photo.latitude|abs) }}°{{ 'N' if photo.latitude >= 0 else 'S' }},
        {{ "%.4f"|format(photo.longitude|abs) }}°{{ 'E' if photo.longitude >= 0 else 'W' }}
        &nbsp;·&nbsp;
        <a href="{{ url_for('map_view', photo_id=photo.id) }}">Full map →</a>
      </div>
    </div>
    {% endif %}
```

The insertion point is the line that reads `</div>` closing the Details `meta-section`, which is line ~396 in the original file. The existing structure around that point:

```html
      {% elif photo.flickr_id %}
      <div class="meta-row">
        <span class="label">Photos</span>
        <span class="value" style="color:var(--muted)" title="...">not in library</span>
      </div>
      {% endif %}
    </div>                          ← insert AFTER this closing div

    <!-- Flickr rotation — only shown when photo is on Flickr -->
```

- [ ] **Step 5: Add Leaflet JS init to `photo.html` `scripts` block**

The `{% block scripts %}` in `photo.html` starts with `<script>` (currently line 498). Add the Leaflet JS load and init **before** the opening `<script>` tag of the existing block:

```html
{% block scripts %}
{% if photo.latitude %}
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
(function () {
  const miniMap = L.map('mini-map', {
    zoomControl: false,
    dragging: false,
    scrollWheelZoom: false,
    doubleClickZoom: false,
    attributionControl: false,
  }).setView([{{ photo.latitude }}, {{ photo.longitude }}], 15);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
  }).addTo(miniMap);
  L.marker([{{ photo.latitude }}, {{ photo.longitude }}]).addTo(miniMap);
  requestAnimationFrame(() => miniMap.invalidateSize());
}());
</script>
{% endif %}
<script>
const PHOTO_ID = {{ photo.id }};
// ... rest of existing scripts block unchanged
```

Only the `{% block scripts %}` opening line and the new Leaflet block are added before the existing `<script>` tag — everything inside the existing `<script>` tag stays exactly as-is.

- [ ] **Step 6: Run tests**

```
python -m pytest tests/test_photo_minimap.py -v
```

Expected: all 7 pass.

- [ ] **Step 7: Run full test suite**

```
python -m pytest tests/ -q
```

Expected: all pass, no regressions.

- [ ] **Step 8: Run lint**

```
make lint
```

Expected: no mypy or ruff errors on touched files.

- [ ] **Step 9: Commit**

```bash
git add reviewer/templates/photo.html tests/test_photo_minimap.py
git commit -m "feat(#146): mini-map on photo detail page for geotagged photos

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 4: README, docs, issue close

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-05-26-geo-edit-sync-145-design.md` (mark map param done)

- [ ] **Step 1: Update README**

In the feature list section of `README.md`, find the `/map` page entry:

> `/map` page (key `0`) plots all geotagged photos on an interactive OpenStreetMap map…

Append to that sentence:

> ; clicking "Full map →" on any photo's detail page opens `/map` centred on that photo with its popup open

- [ ] **Step 2: Mark the `?photo_id=` section done in the #145 spec**

In `docs/superpowers/specs/2026-05-26-geo-edit-sync-145-design.md`, under the "Map view — `?photo_id=` parameter" heading, add a status note:

```markdown
> ✓ Implemented in #146.
```

- [ ] **Step 3: Close issue #146 with a retrospective comment**

```bash
gh issue comment 146 --body "Implemented in two commits:

- \`feat(#146): map view ?photo_id= param — centre + auto-open popup\`
- \`feat(#146): mini-map on photo detail page for geotagged photos\`

**Retrospective:** size:S estimate ✓. ~3 files changed, ~60 lines net. No scope changes."

gh issue close 146
```

- [ ] **Step 4: Version bump + push**

```bash
# Edit pyproject.toml — bump patch version (e.g. 1.2.4 → 1.2.5)
# Then:
git add README.md docs/superpowers/specs/2026-05-26-geo-edit-sync-145-design.md pyproject.toml
git commit -m "docs(#146): README update + mark map param done; bump version to 1.2.5

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
git push origin main
```
