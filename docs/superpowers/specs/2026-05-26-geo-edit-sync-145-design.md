# Geo-tag editing and sync — Design spec

**Issue:** [#145](https://github.com/cdevers/Blue-Pearmain/issues/145)  
**Date:** 2026-05-26  
**Status:** draft — revised after external review 2026-05-26

---

## Location state model

Before diving into mechanics, it helps to name the states a photo can be in. Absence of coordinates is no longer passive missing data — it becomes an explicit three-state machine:

| State | `latitude` | `geo_confirmed_none` | Meaning |
|---|---|---|---|
| **has-coords** | non-null | 0 | Photo has a geotag (may be correct or incorrect) |
| **missing-unreviewed** | null | 0 | No geotag; not yet assessed |
| **intentionally-none** | null | 1 | No geotag; explicitly confirmed correct |

Every geo-related feature (filter, pill, sync, proposals) branches on this state machine. The `intentionally-none` state suppresses all future geo proposals for that photo.

---

## Problem

Photos can carry incorrect or missing geotags — e.g. a photo of Fenway Park that Flickr places in Korea due to a historical data error. Flickr and Apple Photos both have native geo editors, but BP has no way to surface these discrepancies, and no mechanism to propagate a location correction from one side to the other.

This spec covers:

1. Surfacing ungeotagged photos in the library
2. Letting the user dismiss photos that legitimately have no location
3. Deep links to native geo editors (Flickr, Photos.app)
4. Geo sync proposals — detecting and queuing lat/lon divergences for bidirectional resolution
5. A `?photo_id=` centering parameter on the BP map view

It does **not** include an in-BP location picker (no map-click editor in this phase). That is a natural follow-on once the proposal machinery exists.

---

## Scope and trust model

Single-user, localhost. No authentication or authorization changes required.

---

## Database changes (migration)

### 1. `photos.geo_confirmed_none` column

```sql
ALTER TABLE photos ADD COLUMN geo_confirmed_none INTEGER NOT NULL DEFAULT 0;
```

When `1`, the photo has been explicitly confirmed as having no location (screenshot, downloaded image, Photoshop export, etc.). It is excluded from the "no location" library filter and skipped by the geo sync engine.

### 2. `metadata_proposals.field` CHECK constraint

The existing CHECK must be extended to allow `'geo_location'`:

```sql
CHECK(field IN ('title', 'description', 'tags', 'geo_location'))
```

SQLite does not support ALTER TABLE to modify CHECK constraints. The migration must recreate the `metadata_proposals` table with the updated constraint, copying existing rows. The migration must be idempotent.

### Proposal directionality

Every `geo_location` proposal has explicit directionality:

| Field | Meaning |
|---|---|
| `source` | System that *has* the coordinates being proposed (`flickr` or `photos`) |
| `target` | System that will *receive* the coordinates if applied (`flickr` or `photos`) |
| `proposed_value` | JSON object — always contains `lat`/`lon` (source coords); divergence cases also carry `current_lat`/`current_lon`/`distance_m` |

This makes each proposal self-contained: the UI never needs to find a counter-proposal to display the full picture.

### `proposed_value` encoding for `geo_location`

**non_conflict** (one side has coords, other is null) — one proposal created:
```json
{"lat": 42.3601, "lon": -71.0589}
```
`lat`/`lon` are the source side's coordinates. The destination has no current value.

**divergence** (both sides have coords but differ by > `GEO_DIVERGENCE_THRESHOLD_M`) — two proposals created:

```json
{
  "lat": 42.3601,
  "lon": -71.0589,
  "current_lat": 37.5665,
  "current_lon": 126.9780,
  "distance_m": 10923456
}
```

- `lat`/`lon` — what will be written to the *target* system (the source side's value)
- `current_lat`/`current_lon` — what is currently on the *target* system (pre-computed at proposal creation time)
- `distance_m` — geodesic distance between the two points, computed via `haversine_m()` at proposal creation time; displayed as "~10,923 km apart" in the UI

The two opposing proposals are:
- `source=flickr, target=photos` — proposes writing Flickr's coords to Photos; `current_*` holds Photos' current coords
- `source=photos, target=flickr` — proposes writing Photos' coords to Flickr; `current_*` holds Flickr's current coords

The proposals UI shows both side-by-side with the distance delta. Clicking "Use Flickr" applies the flickr→photos proposal and rejects photos→flickr; "Use Photos" does the reverse.

---

## Geo sync detection (`sync_metadata.py`)

A new `sync_geo(db, dry_run, photo_ids)` function runs after the existing title/description/tags diff pass. It operates on photos that:

- Have both a `uuid` and a `flickr_id` (matched to both systems)
- Have `geo_confirmed_none = 0`
- Have `flickr_deleted = 0`

**Case detection:**

| Flickr coords | Photos coords | Action |
|---|---|---|
| present | absent | `non_conflict` proposal: flickr → photos |
| absent | present | `non_conflict` proposal: photos → flickr |
| present | present, differ >1 km | `divergence` proposal |
| both absent | — | no proposal (use library filter to surface) |
| both present, agree within 1 km | — | no proposal |

**Distance comparison only — never compare raw floats.** Flickr and Apple Photos may store coordinates at different precision or with different rounding. Raw `latitude == latitude` comparisons must never be used. All comparisons go through `haversine_m()`.

**Threshold constant:** Define `GEO_DIVERGENCE_THRESHOLD_M: int = 1_000` as a module-level constant in `sync_metadata.py`. Do not hardcode `1000` inline.

**1 km threshold rationale:** Values within 1 000 m are treated as equivalent — covers GPS jitter, network triangulation noise, and Flickr rounding to ~4 decimal places (~11 m precision). Anything farther is a meaningful discrepancy.

**Missing-on-one-side asymmetry:** The two non-conflict cases are operationally different and should be treated as such:
- **Flickr has coords, Photos has none** — Photos may simply not have imported the location from EXIF or Flickr. Safe to auto-apply (proposal is created but can be auto-applied in a future phase).
- **Photos has coords, Flickr has none** — Flickr may be intentionally missing a location (user removed it for privacy). Do not auto-apply; always queue for review.

In this phase both cases create `non_conflict` proposals and require user confirmation. The asymmetry is documented for a future auto-apply phase.

**Superseding:** Before creating a new proposal, any existing `pending` geo proposal for the same `(photo_id, field='geo_location')` is marked `superseded` — same pattern as the existing text field logic.

**Invocation:** `bp sync-metadata` (no new CLI flags needed). The `--photo-id` and `--dry-run` flags apply to `sync_geo` as well.

---

## Flickr client (`flickr_client.py`)

New method:

```python
def set_location(self, flickr_id: str, lat: float, lon: float) -> None:
    """Set the geotag on a Flickr photo via flickr.photos.geo.setLocation."""
```

- Calls `flickr.photos.geo.setLocation` with `photo_id`, `lat`, `lon`
- Uses default accuracy (`accuracy=16` — street level) unless a stored accuracy value exists
- Raises `FlickrApiError` on failure (same pattern as `set_meta`)

---

## Proposal application (`proposal_applier.py`)

New function `apply_geo_proposal(db, proposal, flickr_client, config)`:

- **target=flickr:** calls `flickr_client.set_location(flickr_id, lat, lon)`
- **target=photos:** calls `photoscript.Photo(uuid).location = (lat, lon)` inside a `_with_timeout(_PHOTOS_WRITE_TIMEOUT)` guard (same 45 s timeout as existing photoscript writes)
- On success: marks proposal `applied`, stamps `resolved_at`
- On failure: marks proposal `failed`, stores error in `resolution_note`

Divergence proposals (both sides have differing coords): two proposals are created (flickr→photos and photos→flickr). The user chooses which side wins via the proposals UI ("Use Flickr" / "Use Photos"), matching the existing text collision interaction. The chosen direction's proposal is applied; the opposing proposal is marked `rejected`.

---

## Library filter — "No location"

### Filter chip

A new "No location" chip in the library filter bar, alongside the existing privacy-state, album, tag, and map-area filters.

- Query condition: `latitude IS NULL AND longitude IS NULL AND geo_confirmed_none = 0`
- Shows a count badge (same style as other chips)
- Mutually exclusive with the map-area bounding-box filter (spatial filter requires coordinates to exist)

### "no loc" thumbnail pill

A small pill rendered bottom-right on every ungeotagged, unconfirmed thumbnail, in all library views (not just the filtered view):

```html
<div class="no-loc-pill">no loc</div>
```

CSS: dark semi-transparent background, 9px uppercase text, same visual register as the existing `.thumb-title` bar. Hidden when `geo_confirmed_none = 1`.

The pill is rendered in the Jinja template based on `photo.latitude is none and not photo.geo_confirmed_none`.

### Bulk action

When photos are selected in the library, the existing bulk-action bar gains a **"Mark: no location (correct)"** button. On click:

- If more than 10 photos are selected, show a JS confirmation dialog: "Mark N photos as having no location? This will suppress future location sync proposals for all of them." Proceed only on confirmation.
- POST to a new `/api/geo_confirm_none` endpoint with the selected photo IDs
- Sets `geo_confirmed_none = 1` for each
- Cancels any pending `geo_location` proposals for those photos (marked `rejected`)
- Pills disappear; photos drop out of the "No location" filter

The 10-photo threshold is a safeguard because `geo_confirmed_none = 1` suppresses future geo proposals permanently. Accidental mass application would be silent and hard to detect later.

---

## Photo detail page (`photo.html`)

A new "Location" section below the existing metadata fields:

**Deep link tiers — implementation must respect this distinction:**

| Link | Reliability | Notes |
|---|---|---|
| "View on map" → `/map?photo_id=N` | **Guaranteed** — BP internal route |
| "Edit on Flickr" → `flickr.com/.../edit/` | **Guaranteed** — stable Flickr URL |
| "Edit in Photos" → `photos://` scheme | **Best-effort** — verify during implementation |

The `photos://uuid/{uuid}` deep link must be tested before shipping. If it doesn't reliably open the correct photo, fall back to a "Copy UUID" button. Do not silently ship a broken link.

**If the photo has coordinates:**
- Displays `42.3601°N, 71.0589°W` (formatted from `latitude`/`longitude`)
- "View on map" link → `/map?photo_id={id}` (guaranteed)
- "Edit on Flickr" link → `https://www.flickr.com/photos/{username}/{flickr_id}/edit/` (guaranteed)
- "Edit in Photos" link → `photos://uuid/{uuid}` (best-effort — see table above)

**If the photo has no coordinates and `geo_confirmed_none = 0`:**
- Displays "No location"
- "Edit on Flickr" and "Edit in Photos" links (same as above)
- "Mark as correct (no location needed)" link → POST to `/api/geo_confirm_none` for this photo

**If `geo_confirmed_none = 1`:**
- Displays "No location (confirmed)"
- "Edit on Flickr" and "Edit in Photos" links still shown (user can change their mind)
- "Undo: clear confirmation" link → POST to `/api/geo_confirm_none` with `clear=1`

---

## Proposals UI (`/proposals` page)

The `geo_location` field renders with:

- **Label:** "Location" (same pattern as "Title", "Description", "Tags")
- **Source attribution:** "Flickr → Photos" or "Photos → Flickr" shown explicitly on each proposal row
- **Value display:** `42.3601°N, 71.0589°W` formatted from `lat`/`lon` in `proposed_value`
- **"View on map" link:** `/map?photo_id={id}` (opens map centred on the *proposed* coordinates)
- **Divergence display:** For proposals with `distance_m` in `proposed_value`, show the distance delta prominently: e.g. "~10,923 km apart" (format: `< 1 km` / `1.2 km` / `~10,923 km`). This immediately communicates the severity of the discrepancy — "Fenway Park vs Seoul (~10,923 km apart)" tells the full story.
- **Use Flickr / Use Photos buttons:** Same interaction as existing text collision UI. The button labels are derived from `source` and `target` fields.

---

## Map view — `?photo_id=` parameter

> ✓ Implemented in #146.

**Backend (`app.py`):** `map_view()` accepts an optional `photo_id` query parameter. If present, looks up that photo's coordinates and uses them as `center_lat`/`center_lon`; passes `highlight_id = photo_id` to the template.

**Frontend (`map.html`):** After markers load, `tryHighlight()` finds the matching marker and calls `markers.zoomToShowLayer(marker, () => marker.openPopup())`. A `_highlightDone` flag ensures the highlight fires only once per page load (not on every time-filter reload).

**Edge cases (all handled silently):**

| Situation | Behaviour |
|---|---|
| `photo_id` exists, has coordinates | Map centred there, popup opened |
| `photo_id` exists, no coordinates | Falls back to average centre; `highlight_id = null` |
| `photo_id` not found | Falls back to average centre; `highlight_id = null` |
| Photo is filtered out by active time filter | Map centres correctly; marker not visible in current filter; `tryHighlight()` no-ops silently (marker not in `_markerById`) |

The time-filter no-op is intentional. Overriding the active filter to show a filtered-out photo would create surprising behaviour.

---

## New API endpoints

| Method | Path | Description |
|---|---|---|
| POST | `/api/geo_confirm_none` | Set or clear `geo_confirmed_none` for one or more photos |

Request body:
```json
{
  "photo_ids": [1, 2, 3],
  "clear": false
}
```

`clear: true` sets `geo_confirmed_none = 0`; omit or `false` sets it to `1`.

---

## Error handling

- `flickr.photos.geo.setLocation` failures: proposal marked `failed`; user can retry from the proposals UI
- `photoscript.Photo(uuid).location` write failures: same — `failed` status, retry available
- Photos that have moved out of the library (UUID no longer resolves): log warning, mark proposal `failed` with note
- `photo_id` not found in `?photo_id=` map param: falls back to average-center silently

---

## Testing

- Migration: idempotency test for the `metadata_proposals` table recreation; `geo_confirmed_none` column exists after migration
- `sync_geo()`: unit tests for all five detection cases (flickr-only, photos-only, diverge, agree, both-absent); threshold boundary at `GEO_DIVERGENCE_THRESHOLD_M - 1` m and `GEO_DIVERGENCE_THRESHOLD_M + 1` m; verify `distance_m` is stored in divergence proposals; verify `geo_confirmed_none = 1` photos are skipped
- `apply_geo_proposal()`: mock flickr_client and photoscript; assert correct call for flickr target and photos target; assert `distance_m` not required for application (apply uses `lat`/`lon` only)
- `set_location()`: mock API call; assert `FlickrApiError` on failure
- Library filter: route test with a mix of geotagged, ungeotagged, and confirmed-none photos
- `/api/geo_confirm_none`: set and clear; verify proposal cancellation on set; verify bulk confirmation prompt threshold (>10 photos) is enforced client-side
- Map `?photo_id=`: ✓ tested in #146

---

## Out of scope (this phase)

- In-BP location picker (map-click or address search)
- Accuracy metadata beyond `accuracy=16` default
- Batch geo editing from the library (no location picker UI yet)
- iPhoto archive photos (not in Photos.app, no `uuid`)
- Geo history / audit log
