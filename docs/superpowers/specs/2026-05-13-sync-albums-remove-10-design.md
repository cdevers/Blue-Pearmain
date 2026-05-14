# `bp sync-albums --remove` — Design Spec (GH #10)

**Goal:** Extend album sync to reconcile removals — when a photo is removed from an Apple Photos album, or an entire album is deleted, the corresponding Flickr photoset membership is cleaned up.

---

## Schema changes

Two new nullable timestamp columns, one migration each.

**`photo_albums`:**
```sql
ALTER TABLE photo_albums ADD COLUMN removed_at TEXT;
```
- `NULL` → photo still belongs to this album
- Non-NULL → scanner detected removal at this timestamp; row is pending Flickr action
- After successful `removePhoto`, the row is hard-deleted (membership no longer exists)
- If `flickr_pushed = 0` at removal time, the row is deleted immediately by the scanner — no Flickr action needed

**`albums`:**
```sql
ALTER TABLE albums ADD COLUMN deleted_at TEXT;
```
- `NULL` → album still exists in Apple Photos
- Non-NULL → scanner detected deletion at this timestamp; photoset deletion is pending
- After successful `photosets.delete`, the `albums` row is hard-deleted; CASCADE cleans up remaining `photo_albums` rows

---

## Scanner changes (detection only — no Flickr calls)

### `sync_photo_albums()` — per-photo, existing function

After upserting current album rows for a photo:
1. Fetch all stored `photo_albums` rows for the photo
2. Compare stored album UUIDs against the current osxphotos album UUID set
3. For each stored row whose album UUID is no longer present:
   - If `flickr_pushed = 1`: set `removed_at = now` (pending Flickr removal)
   - If `flickr_pushed = 0`: delete the row immediately (never pushed, nothing to undo)

### `sync_deleted_albums()` — new function, called once per scan

Runs independently of `--limit` (album enumeration is cheap, ~200 albums):
1. Fetch all current album UUIDs from `PhotosDB().album_info`
2. Query all `albums` rows where `deleted_at IS NULL`
3. For each stored album whose `apple_uuid` is not in the current set: set `deleted_at = now`
4. Albums already marked `deleted_at IS NOT NULL` are left unchanged

Both functions respect the scanner's `--dry-run` flag: log what would be marked, write nothing.

---

## `sync-albums --remove` (action phase)

A new `--remove` flag. When passed, a removal phase runs after the existing additive sync. Two steps, in order:

### Step 1 — Deleted albums (whole photoset)

Query: `albums WHERE deleted_at IS NOT NULL AND flickr_set_id IS NOT NULL`

For each row:
- Call `flickr.photosets.delete(flickr_set_id)`
- On success or `FLICKR_ERR_PHOTOSET_NOT_FOUND`: hard-delete the `albums` row (CASCADE removes its `photo_albums` rows)
- On other error: log and count as failed, leave row for retry

Step 1 runs before Step 2 so that photos in a deleted album aren't also individually processed via `removePhoto`.

### Step 2 — Individual photo removals (photo left a surviving album)

Query: `photo_albums WHERE removed_at IS NOT NULL AND flickr_pushed = 1`, joined to `albums` (for `flickr_set_id`) and `photos` (for `flickr_id`)

For each row:
- Call `flickr.photosets.removePhoto(flickr_set_id, flickr_id)`
- On success or `FLICKR_ERR_PHOTO_NOT_IN_SET`: hard-delete the `photo_albums` row
- On `FLICKR_ERR_NOT_FOUND` (photo deleted from Flickr): hard-delete the row (desired state achieved)
- On other error: log and count as failed, leave row for retry

### Output

```
albums created=0  photos added=0  skipped=0  failed=0  photosets deleted=1  photos removed=3
```

`--dry-run` suppresses all writes and Flickr calls; logs what would be removed.
`--album NAME` filter applies to the additive phase only; removal phase always processes all pending tombstones.

---

## Flickr client additions (`flickr_client.py`)

```python
def remove_photo_from_photoset(self, photoset_id: str, photo_id: str) -> None:
    self._call("flickr.photosets.removePhoto",
               photoset_id=photoset_id, photo_id=photo_id)

def delete_photoset(self, photoset_id: str) -> None:
    self._call("flickr.photosets.delete", photoset_id=photoset_id)
```

New error constants:
```python
FLICKR_ERR_PHOTO_NOT_IN_SET  = 2   # removePhoto: photo not in set
FLICKR_ERR_PHOTOSET_NOT_FOUND = 1  # photosets.delete: set not found
```

---

## DB additions (`db.py`)

| Method | Purpose |
|--------|---------|
| `mark_photo_album_removed(photo_id, album_id)` | Set `removed_at = now` on a `photo_albums` row |
| `get_pending_album_removals(limit)` | `photo_albums` where `removed_at IS NOT NULL AND flickr_pushed = 1`, joined to `albums` + `photos` |
| `get_deleted_albums()` | `albums` where `deleted_at IS NOT NULL AND flickr_set_id IS NOT NULL` |
| `delete_photo_album_row(photo_id, album_id)` | Hard-delete one `photo_albums` row |
| `delete_album(album_id)` | Hard-delete one `albums` row (CASCADE handles `photo_albums`) |

---

## Testing

All new logic is testable without Flickr API calls (Flickr client mocked).

**Scanner detection (`test_core.py`):**
- Photo removed from one album, still in another → only the departed album's row gets `removed_at`
- Photo removed from all albums → all rows get `removed_at`
- Photo never pushed (`flickr_pushed = 0`) removed from album → row deleted immediately, not tombstoned
- Album deleted from Apple Photos → `albums.deleted_at` set; `photo_albums` rows untouched
- Album still present → no `deleted_at` written

**Removal sync:**
- Dry-run: tombstoned rows present, no DB mutations, correct log output
- Individual removal: `removePhoto` called, `photo_albums` row deleted on success
- `FLICKR_ERR_PHOTO_NOT_IN_SET` → treated as success, row deleted
- Deleted album: `photosets.delete` called, `albums` row hard-deleted, CASCADE removes `photo_albums`
- `FLICKR_ERR_PHOTOSET_NOT_FOUND` → treated as success, `albums` row deleted
- Step 1 runs before Step 2: photos in a deleted album are not double-processed via `removePhoto`
