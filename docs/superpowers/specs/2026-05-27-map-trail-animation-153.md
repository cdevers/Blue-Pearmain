# Spec: Animated map trail — Indiana Jones-style route animation (#153, Phase 1 POC)

_Status: spec — not yet implemented_

---

## Problem

The photo trail (#151) shows a static polyline connecting geotagged photos chronologically. It tells you *where* you went, but not the feeling of *travelling there*. An animated version — a plane tracing the route as the trail grows behind it — makes the journey legible and satisfying to watch.

---

## Scope (Phase 1 — POC)

**In:**
- "Animate" / "Stop" toggle button in the map filter bar
- `requestAnimationFrame` animation loop drawing a growing polyline
- ✈ plane marker (Leaflet `divIcon`) rotating to face direction of travel
- Map pans smoothly to follow the plane
- Distance-proportional speed (12 s default total duration)
- All changes in `map.html` only — no backend, no API, no Python

**Out (later phases):**
- Time-proportional speed mode (A)
- Speed slider / Slow / Normal / Fast selector
- Video/MP4 export (Phase 2: Playwright + ffmpeg)
- Cinematic effects: vignette, title cards, great-circle arcs
- Photo thumbnail overlays at stops
- Privacy filter (public-only mode)

---

## UI

### Filter bar button

A new **Animate** button appears immediately to the right of the "Show trail" checkbox. It is:
- **Hidden** when the trail checkbox is unchecked
- **Hidden** when `_lastPhotos.length < 2`
- **Shown** (label: `Animate`) when the trail is visible and there are ≥2 points
- **Shown** (label: `Stop`) while animation is running

The button uses the existing `.map-btn` class for consistency.

```html
<button type="button" id="map-animate-btn"
        class="map-btn" style="display:none"
        onclick="toggleAnimation()">Animate</button>
```

Show/hide logic is wired into:
1. The trail checkbox `change` listener — show/hide based on `_lastPhotos.length ≥ 2`
2. The `map-time-select` change handler — hide (and stop if running) when filter is cleared
3. `plotTrail()` — after updating `_lastPhotos`, evaluate visibility

---

## New module-level variables

```js
let _animFrame   = null;  // rAF handle while animating, null otherwise
let _animTrail   = null;  // L.polyline growing trail, null when not animating
let _planeMarker = null;  // L.marker with divIcon plane, null when not animating
```

---

## Helper functions

### `bearing(lat1, lon1, lat2, lon2) → degrees`

Standard spherical bearing, 0–360:

```js
function bearing(lat1, lon1, lat2, lon2) {
  const toRad = d => d * Math.PI / 180;
  const dLon = toRad(lon2 - lon1);
  const φ1 = toRad(lat1), φ2 = toRad(lat2);
  const y = Math.sin(dLon) * Math.cos(φ2);
  const x = Math.cos(φ1) * Math.sin(φ2) - Math.sin(φ1) * Math.cos(φ2) * Math.cos(dLon);
  return (Math.atan2(y, x) * 180 / Math.PI + 360) % 360;
}
```

### `planeIcon(deg) → L.divIcon`

Creates a `divIcon` with the ✈ glyph rotated to face `deg`. The ✈ character points east (90°) by default, so CSS rotation is `deg - 90`:

```js
function planeIcon(deg) {
  return L.divIcon({
    className: '',
    html: `<div style="font-size:20px;transform:rotate(${deg - 90}deg);
                       transform-origin:center;line-height:1">✈</div>`,
    iconSize: [24, 24],
    iconAnchor: [12, 12],
  });
}
```

### `lerp(a, b, t) → number`

Linear interpolation, `t` in [0, 1]:

```js
const lerp = (a, b, t) => a + (b - a) * t;
```

---

## `animatePOC(photos)`

Builds the segment list and starts the `requestAnimationFrame` loop.

```
Input: photos — same array as _lastPhotos (filtered, sorted by date)
```

1. Filter to points with lat/lon; if < 2, return early.
2. Build `segments` array: `[{lat1,lon1,lat2,lon2,dist}, ...]` where `dist = map.distance([lat1,lon1],[lat2,lon2])`.
3. Compute `totalDist = sum of segment dists`. If 0, return early.
4. `DURATION_MS = 12000` (constant; tune later).
5. Hide `_trailLayer` (`_trailLayer.setStyle({opacity:0, fillOpacity:0})`).
6. Create `_animTrail = L.polyline([], {color:'#e06820', weight:3, opacity:0.8}).addTo(map)`.
7. Place `_planeMarker` at `[segments[0].lat1, segments[0].lon1]` using `planeIcon(bearing(...))`.
8. `map.setView([segments[0].lat1, segments[0].lon1], map.getZoom(), {animate:false})`.
9. Record `startTime = performance.now()`, `segIdx = 0`, `segStart = 0` (fraction of total time at which current segment begins).
10. Start rAF loop: `_animFrame = requestAnimationFrame(step)`.

**`step(now)`:**

```
elapsed = now - startTime
t_global = Math.min(elapsed / DURATION_MS, 1)   // 0→1 over full duration

// Which segment are we in?
Each segment's time share = segDist / totalDist
Walk segments until cumulative share > t_global → current segIdx, local t within segment

pos = lerp(seg.lat1, seg.lat2, t_local), lerp(seg.lon1, seg.lon2, t_local)

// Grow trail: collect all completed segment latlngs + interpolated current pos
_animTrail.setLatLngs([...completedPoints, [pos.lat, pos.lon]])

// Move + rotate plane
brg = bearing(seg.lat1, seg.lon1, seg.lat2, seg.lon2)
_planeMarker.setLatLng([pos.lat, pos.lon])
_planeMarker.setIcon(planeIcon(brg))

// Pan map
map.panTo([pos.lat, pos.lon], {animate:true, duration:0.3})

if (t_global < 1) {
  _animFrame = requestAnimationFrame(step)
} else {
  stopAnimation()
}
```

---

## `stopAnimation()`

Called on Stop button click, on natural completion, and when the time filter changes mid-animation.

```js
function stopAnimation() {
  if (_animFrame) { cancelAnimationFrame(_animFrame); _animFrame = null; }
  if (_planeMarker) { map.removeLayer(_planeMarker); _planeMarker = null; }
  if (_animTrail)  { map.removeLayer(_animTrail);   _animTrail  = null; }
  if (_trailLayer) { _trailLayer.setStyle({opacity: 0.65}); }
  const btn = document.getElementById('map-animate-btn');
  btn.textContent = 'Animate';
}
```

---

## `toggleAnimation()`

```js
function toggleAnimation() {
  if (_animFrame !== null) {
    stopAnimation();
  } else {
    animatePOC(_lastPhotos);
    document.getElementById('map-animate-btn').textContent = 'Stop';
  }
}
```

---

## Wiring into existing handlers

### Trail checkbox `change` listener

After `plotTrail(_lastPhotos)`:
```js
const animBtn = document.getElementById('map-animate-btn');
animBtn.style.display = (document.getElementById('map-trail-cb').checked && _lastPhotos.length >= 2)
  ? '' : 'none';
```

### `map-time-select` change handler

When filter is cleared (`!isAnyPattern`):
```js
stopAnimation();
document.getElementById('map-animate-btn').style.display = 'none';
```

### `plotTrail(photos)` (end of function)

After updating `_lastPhotos`:
```js
const animBtn = document.getElementById('map-animate-btn');
if (animBtn) {
  animBtn.style.display = (document.getElementById('map-trail-cb').checked && _lastPhotos.length >= 2)
    ? '' : 'none';
}
```

---

## Manual verification checklist

1. No time filter → Animate button hidden.
2. Select filter, uncheck trail → Animate button hidden.
3. Select filter, check trail, < 2 geotagged photos → Animate button hidden.
4. Select filter, check trail, ≥ 2 photos → Animate button visible.
5. Click Animate → plane appears at start, trail grows, map follows, button shows "Stop".
6. Animation completes → static trail restored, plane gone, button shows "Animate".
7. Click Stop mid-animation → same cleanup as completion.
8. Change filter mid-animation → animation stops, cleans up, filter reloads.
9. Plane rotates correctly: heading east on an eastward segment, north on a northward one.
10. Map pans smoothly — no jarring jumps.

---

## Implementation checklist

- [ ] Add `bearing()`, `planeIcon()`, `lerp()` helpers
- [ ] Add `_animFrame`, `_animTrail`, `_planeMarker` module-level variables
- [ ] Add `animatePOC(photos)` with rAF loop
- [ ] Add `stopAnimation()` cleanup function
- [ ] Add `toggleAnimation()` dispatcher
- [ ] Add Animate button HTML to filter bar
- [ ] Wire button show/hide into trail checkbox listener
- [ ] Wire `stopAnimation()` into time-select clear handler
- [ ] Wire button show/hide into `plotTrail()`
- [ ] Manual verification of all 10 scenarios above
- [ ] Commit referencing #153
