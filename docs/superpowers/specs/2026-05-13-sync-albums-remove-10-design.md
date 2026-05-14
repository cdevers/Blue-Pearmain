# `bp sync-albums --remove` — Design Spec (GH #10)

**Goal:** Extend album sync to reconcile removals — when a photo is removed from an Apple Photos album, or an entire album is deleted, the corresponding Flickr photoset membership is cleaned up.

**Authoritativeness:** Apple Photos is the source of truth. `bp sync-albums --remove` makes Flickr match the local album state, not the other way around. Manual edits made directly to Flickr photosets may be overwritten.

---

## Schema changes

Two new nullable timestamp columns, one migration each.

**`photo_albums`:**
```sql
ALTER TABLE photo_albums ADD COLUMN removed_at TEXT;
```
- `NULL` → photo still belongs to this album (or was re-added after a prior removal)
- Non-NULL → scanner detected removal at this timestamp; row is pending Flickr action
- After successful `removePhoto`, the row is hard-deleted (membership no longer exists)
- If `flickr_pushed = 0` at removal time, the row is deleted immediately by the scanner — no Flickr action needed
- **Re-add invariant:** if a photo-album pair is observed again after being tombstoned (photo removed then re-added before sync runs), `removed_at` is cleared back to `NULL` and no Flickr removal occurs. Churn (remove → re-add → re-remove) is handled by the same logic on subsequent scans.

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
4. For each stored row that IS present but has `removed_at IS NOT NULL` (photo was re-added): clear `removed_at = NULL`

The existing `upsert_photo_album()` must be updated to clear `removed_at` on re-observation (currently uses `INSERT OR IGNORE` which leaves a tombstoned row untouched).

### `sync_deleted_albums()` — new function, called once per scan

Runs independently of `--limit` (album enumeration is cheap, ~200 albums):
1. Fetch all current album UUIDs from `PhotosDB().album_info`
2. **Plausibility guard:** compare observed count against the count of non-deleted albums in the DB. If observed count is less than 50% of the stored baseline (and stored baseline > 0), abort without tombstoning and log a warning — this indicates a transient osxphotos failure, not genuine mass deletion. Zero observed albums always aborts regardless of threshold.
3. Query all `albums` rows where `deleted_at IS NULL`
4. For each stored album whose `apple_uuid` is not in the current set: set `deleted_at = now`
5. Albums already marked `deleted_at IS NOT NULL` are left unchanged

Both functions respect the scanner's `--dry-run` flag: log what would be marked, write nothing.

---

## `sync-albums --remove` (action phase)

Two new flags:
- `--remove` — enables the removal phase (shows what would be removed; dry-run by default)
- `--apply` — required alongside `--remove` to execute destructive Flickr calls

```bash
bp sync-albums --remove           # preview removals, no writes
bp sync-albums --remove --apply   # execute removals
bp sync-albums --remove --dry-run # same as --remove alone (explicit)
```

The removal phase runs after the existing additive sync, in two steps:

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
- On `FLICKR_ERR_PHOTOSET_NOT_FOUND` (photoset was manually deleted on Flickr): treat as non-fatal — desired state achieved — hard-delete the `photo_albums` row and log a warning
- On `FLICKR_ERR_NOT_FOUND` (photo deleted from Flickr): hard-delete the row (desired state achieved)
- On other error: log and count as failed, leave row for retry

Failed rows are retried on the next `--remove --apply` run. The tombstone (`removed_at IS NOT NULL`) is never cleared on failure — the row stays pending until Flickr confirms.

### Output

```
albums created=0  photos added=0  skipped=0  failed=0  photosets deleted=1  photos removed=3
```

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
FLICKR_ERR_PHOTO_NOT_IN_SET   = 2   # removePhoto: photo not in set
FLICKR_ERR_PHOTOSET_NOT_FOUND = 1   # photosets.delete: set not found
```

---

## DB additions (`db.py`)

| Method | Purpose |
|--------|---------|
| `mark_photo_album_removed(photo_id, album_id)` | Set `removed_at = now` on a `photo_albums` row |
| `clear_photo_album_removed(photo_id, album_id)` | Clear `removed_at = NULL` when photo is re-observed in album |
| `get_pending_album_removals(limit)` | `photo_albums` where `removed_at IS NOT NULL AND flickr_pushed = 1`, joined to `albums` + `photos` |
| `get_deleted_albums()` | `albums` where `deleted_at IS NOT NULL AND flickr_set_id IS NOT NULL` |
| `delete_photo_album_row(photo_id, album_id)` | Hard-delete one `photo_albums` row |
| `delete_album(album_id)` | Hard-delete one `albums` row (CASCADE handles `photo_albums`) |

`upsert_photo_album()` is updated to call `clear_photo_album_removed()` when the row already exists with `removed_at IS NOT NULL`.

---

## Testing

All new logic is testable without Flickr API calls (Flickr client mocked).

**Scanner detection (`test_core.py`):**
- Photo removed from one album, still in another → only the departed album's row gets `removed_at`
- Photo removed from all albums → all rows get `removed_at`
- Photo never pushed (`flickr_pushed = 0`) removed from album → row deleted immediately, not tombstoned
- Album deleted from Apple Photos → `albums.deleted_at` set; `photo_albums` rows untouched
- Album still present → no `deleted_at` written
- Photo removed then re-added before sync → `removed_at` is cleared, no Flickr removal occurs
- `sync_deleted_albums()` with osxphotos returning zero albums → no tombstones written (plausibility guard)
- `sync_deleted_albums()` with osxphotos returning < 50% of stored album count → no tombstones written, warning logged

**Removal sync:**
- `--remove` without `--apply`: shows preview, no DB mutations, no Flickr calls
- `--remove --apply`: executes removals
- Individual removal: `removePhoto` called, `photo_albums` row deleted on success
- `FLICKR_ERR_PHOTO_NOT_IN_SET` → treated as success, row deleted
- Deleted album: `photosets.delete` called, `albums` row hard-deleted, CASCADE removes `photo_albums`
- `FLICKR_ERR_PHOTOSET_NOT_FOUND` → treated as success, `albums` row deleted
- `FLICKR_ERR_PHOTOSET_NOT_FOUND` during `removePhoto` (photoset manually deleted on Flickr) → treated as non-fatal, row deleted, warning logged
- `removePhoto` failure → row remains tombstoned, counted as failed, retried next run
- Step 1 runs before Step 2: photos in a deleted album are not double-processed via `removePhoto`
