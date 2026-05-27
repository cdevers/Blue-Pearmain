# Geo-tag editing and sync — Design spec

**Issue:** [#145](https://github.com/cdevers/Blue-Pearmain/issues/145)  
**Date:** 2026-05-26  
**Status:** draft

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

### `proposed_value` encoding for `geo_location`

Stored as JSON in the same format for both conflict types:

```json
{"lat": 42.3601, "lon": -71.0589}
```

`lat`/`lon` are always the *source* side's coordinates.

For **non_conflict** (one side has coords, other is null): one proposal is created.

For **divergence** (both sides have coords but differ by >1 km): two proposals are created, mirroring the existing text collision pattern:
- `source=flickr, target=photos, proposed_value = {"lat": flickr_lat, "lon": flickr_lon}`
- `source=photos, target=flickr, proposed_value = {"lat": photos_lat, "lon": photos_lon}`

The proposals UI shows both side-by-side. Clicking "Use Flickr" applies the flickr→photos proposal and rejects the photos→flickr one; "Use Photos" does the reverse.

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

**1 km threshold:** Uses the existing `haversine_m()` from `db/db.py`. Values within 1 000 m are treated as equivalent (GPS jitter tolerance).

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

- POST to a new `/api/geo_confirm_none` endpoint with the selected photo IDs
- Sets `geo_confirmed_none = 1` for each
- Cancels any pending `geo_location` proposals for those photos (marked `rejected`)
- Pills disappear; photos drop out of the "No location" filter

---

## Photo detail page (`photo.html`)

A new "Location" section below the existing metadata fields:

**If the photo has coordinates:**
- Displays `42.3601°N, 71.0589°W` (formatted from `latitude`/`longitude`)
- "View on map" link → `/map?photo_id={id}`
- "Edit on Flickr" link → `https://www.flickr.com/photos/{username}/{flickr_id}/edit/`
- "Edit in Photos" link → `photos://uuid/{uuid}` (macOS deep link — **needs verification** during implementation; if the URL scheme doesn't reliably open a specific photo, fall back to a "Copy UUID" button so the user can locate the photo manually in Photos.app)

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
- **Value display:** `42.3601°N, 71.0589°W` formatted from the JSON `proposed_value`
- **"View on map" link:** `/map?photo_id={id}` (opens map centered on proposed coordinates)
- **Divergence display:** Two columns — "Flickr says" / "Photos says" — with "Use Flickr" and "Use Photos" buttons, matching the existing collision UI for text fields

---

## Map view — `?photo_id=` parameter

**Backend (`app.py`):**

`map_view()` accepts an optional `photo_id` query parameter. If present:

1. Looks up `latitude`/`longitude` for that photo
2. Passes them as `center_lat`/`center_lon` (overriding the average-of-all-photos default)
3. Passes `highlight_id = photo_id` to the template

**Frontend (`map.html`):**

After the marker layer finishes loading, JS checks `highlight_id`. If set:

1. Finds the marker whose `id` matches `highlight_id`
2. Calls `marker.openPopup()` so the photo popup opens immediately
3. If the photo is in a cluster, calls `cluster.zoomToShowLayer(marker, callback)` first

This makes `/map?photo_id=123` a reliable deep link to any geotagged photo.

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
- `sync_geo()`: unit tests for all five detection cases (flickr-only, photos-only, diverge, agree, both-absent); threshold boundary at 999 m and 1 001 m
- `apply_geo_proposal()`: mock flickr_client and photoscript; assert correct call for flickr target and photos target
- `set_location()`: mock API call; assert `FlickrApiError` on failure
- Library filter: route test with a mix of geotagged, ungeotagged, and confirmed-none photos
- `/api/geo_confirm_none`: set and clear; verify proposal cancellation on set
- Map `?photo_id=`: route returns correct `center_lat`/`center_lon` and `highlight_id`

---

## Out of scope (this phase)

- In-BP location picker (map-click or address search)
- Accuracy metadata beyond `accuracy=16` default
- Batch geo editing from the library (no location picker UI yet)
- iPhoto archive photos (not in Photos.app, no `uuid`)
- Geo history / audit log
