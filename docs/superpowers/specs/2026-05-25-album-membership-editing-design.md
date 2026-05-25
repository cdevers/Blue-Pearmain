# Album Membership Editing — Design Spec

**Issue:** Subset of #124 (Album / photoset management from BP UI)  
**Date:** 2026-05-25  
**Status:** Approved, awaiting implementation plan

---

## Problem

Photos can be browsed by album in the library view (read-only filter), but there is no way to change album membership from within BP. Adding or removing photos from albums requires switching to Photos.app or Flickr's web UI.

## Scope (this issue)

- Add photos to existing albums (bulk, from library view)
- Remove photos from existing albums (bulk, from library view, when filtered to that album)
- Lightweight `/albums` index page listing albums with photo counts
- Flickr sync via existing background pipeline — no immediate API calls from the web request

**Deferred to later issues:**
- Creating new albums from BP UI
- Deleting albums
- Renaming albums
- A full album detail/management page (`/albums/<id>`)
- Reconciling Photos.app vs Flickr membership discrepancies

---

## Architecture & Data Flow

```
User selects photos in library view
    │
    ├─ clicks "Add to album ▾"
    │     → panel expands with album checkboxes
    │     → POST /api/album-membership  { photo_ids: [...], add: [album_id, ...] }
    │     → db.upsert_photo_album() per photo/album pair  (flickr_pushed=0)
    │     → queued for next  bp sync-albums  run
    │
    └─ clicks "Remove from [Album]"  (only visible when album_id filter is active)
          → inline confirm appears
          → POST /api/album-membership  { photo_ids: [...], remove: [album_id] }
          → db.mark_photo_album_removed() per photo  (removed_at=now)
          → queued for next  bp sync-albums --remove --apply  run
```

No new async machinery. Both add and remove use the existing sync pipeline (`bp sync-albums`). The web request only writes to the DB.

---

## Backend

### New DB method — `db/db.py`

```python
def get_album_membership_for_photos(self, photo_ids: list[int]) -> dict[int, set[int]]:
    """
    Returns {album_id: {photo_id, ...}} for all active (non-removed)
    memberships among the given photo_ids.
    Used to show current membership state in the Add-to-album panel.
    """
```

No other new DB methods needed. `upsert_photo_album` and `mark_photo_album_removed` already exist and handle the write side correctly (idempotent add, tombstone remove).

### New routes — `reviewer/app.py`

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/albums` | Album index page |
| `POST` | `/api/album-membership` | Add and/or remove memberships |
| `GET` | `/api/album-membership` | Current membership for selected photos |

**`POST /api/album-membership`**
```
Body:  { "photo_ids": [int, ...], "add": [int, ...], "remove": [int, ...] }
Returns: { "added": N, "removed": N }
Errors: 400 on empty photo_ids, unknown album_id, or unknown photo_id
```
Both `add` and `remove` are optional (one or both may be present in a single request).  
`added` and `removed` counts reflect new rows created / tombstoned — already-member photos that are re-added are not counted (idempotent, no-op).

**`GET /api/album-membership`**
```
Query: ?photo_ids=1,2,3
Returns: { "membership": { album_id: [photo_id, ...], ... } }
```
Called client-side when the Add-to-album panel opens, to show which albums already contain the selected photos.

### Minor change — `GET /library`

Pass `current_album` (id + name) to the template when `album_id` filter is active. This is needed to label the "Remove from [Album]" button with the album name.

---

## Frontend

### `reviewer/templates/library.html`

**Action bar additions** (when photos are selected):

- `Add to album ▾` button — always visible; opens the add panel
- `Remove from [album name]` button — only rendered when `current_album` is set (album filter active); styled in red (destructive)

**Add-to-album panel** (new `lib-edit-panel` sibling to existing edit panel):

- Checkbox list of all albums from existing `albums` template context variable
- On open: fetches `GET /api/album-membership?photo_ids=...` to grey out albums the selected photos already belong to (all selected photos already members → greyed; partial membership → shown normally with a note)
- Apply button → `POST /api/album-membership` with `add: [checked album ids]`
- On success: flash "Added to N album(s)", close panel, re-fetch and update the album-count badge on each affected photo card in the grid (no full page reload)

**Remove confirmation** (inline, not a modal):

- Clicking "Remove from [Album]" reveals a `"Remove 5 photos from Summer 2024? [Confirm]"` inline prompt within the action bar
- Confirm → `POST /api/album-membership` with `remove: [album_id]`
- On success: flash "Removed from [Album]", refresh grid (removed photos disappear from filtered view)

### `reviewer/templates/albums.html` *(new)*

Extends `base.html`. Contains:
- Page heading "Albums"
- Table or card list: album name | photo count | "View in library →" link to `/library?album_id=<id>`
- Empty state when no albums exist
- No editing controls on this page (stub only — editing lives in library view)

### `reviewer/templates/base.html`

- Add "Albums" nav entry at key `9`, linking to `/albums`

---

## Testing

### `tests/test_album_membership_api.py`

- `POST` add: valid payload → rows inserted, `flickr_pushed=0`
- `POST` remove: valid payload → `removed_at` set
- `POST` add + remove in same request
- `POST` with already-member photo (idempotent)
- `POST` with invalid `photo_id` → 400
- `POST` with invalid `album_id` → 400
- `POST` with empty `photo_ids` → 400
- `GET` with `?photo_ids=1,2,3` → correct membership dict
- `GET /albums` → 200, album names present in response

### `tests/test_db_album_membership.py`

- `get_album_membership_for_photos` returns correct `{album_id: {photo_id}}` mapping
- `get_album_membership_for_photos` with empty list → empty dict
- Verify `upsert_photo_album` idempotency (add existing membership → no duplicate row, `removed_at` cleared if previously tombstoned)

### Manual verification

Run `bp ui` and confirm:
- Album filter dropdown still works
- Selecting photos shows "Add to album ▾" in action bar
- Add panel opens, checkboxes render, apply writes to DB
- Filtering to an album then selecting photos shows "Remove from [Album]"
- Remove confirm flow works, photos disappear from grid after removal
- `/albums` page loads, lists albums with counts, links work

---

## Extension Points

This design deliberately leaves room for the full #124 vision:

- `/albums` stub becomes a full album management page by adding controls to the template
- `POST /api/album-membership` can be extended with a `create_album` field when new-album creation is added
- The add panel's checkbox list can gain a "New album…" inline input row without restructuring the panel
- `GET /api/album-membership` already returns structured data suitable for a richer membership editor
