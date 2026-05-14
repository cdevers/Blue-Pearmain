# Ghost Photo Cleanup — Design Spec (GH #62)

**Goal:** Detect Photos-only records whose Apple Photos UUID has been deleted from the library and hard-delete them from the DB, eliminating "ghost" entries that show no thumbnail, no Flickr link, and produce a no-op when "Open in Photos" is clicked.

---

## Problem

Some photos in the DB have `uuid IS NOT NULL` and `flickr_id IS NULL` (Photos-only records) but their UUID no longer exists in the Apple Photos library. The photo was deleted from Photos at some point after it was scanned. These records:

- Show a placeholder thumbnail in the review grid (no derivative, no Flickr CDN fallback)
- Have no Flickr link in the detail view
- Produce a silent no-op when "Open in Photos" is clicked (osascript finds no media item with that UUID)

`uuid_stale = 1` is a separate flag set when Flickr's API rejects a proposal with "invalid photo ID" — it does not cover Photos-library deletions, and it is not set for these records.

As of the last audit, 798 records have no thumbnail and no Flickr ID. A 20-record sample via osxphotos showed ~15% were deleted from Photos and ~85% were iCloud-only (still present in Photos but never downloaded locally). The iCloud-only case is tracked separately in issue #64. This spec covers only the deleted-photo case.

---

## Scope

**In scope:**
- Photos-only records (`uuid IS NOT NULL`, `flickr_id IS NULL`) whose UUID is absent from the current Photos library → hard-delete

**Out of scope:**
- Linked records (`flickr_id IS NOT NULL`) where the UUID disappears from Photos — these become Flickr-only records and retain their thumbnail and Flickr presence. Not "ghost" photos.
- iCloud-only photos (still in Photos, no local derivative) — tracked in #64.
- UI changes — the review grid already shows a placeholder and no "Open in Photos" button for records that lack a UUID. Once the record is deleted, it simply disappears from the queue.

---

## Detection

A new function `sync_deleted_photos(photosdb, db, dry_run)` added to `poller/scanner.py`.

**When it runs:** Only during `--all` scans (`since is None`). Incremental scans see only a recent-photos window from osxphotos; that partial set cannot safely be used for absence detection. `--all` is already the established full-reconciliation mode.

**Algorithm:**
1. Call `photosdb.photos()` (unfiltered) to get all current Photos UUIDs → `current_uuids: set[str]`
2. Run plausibility guard (see below)
3. Query `photos WHERE uuid IS NOT NULL AND flickr_id IS NULL` → Photos-only DB records
4. For each record whose `uuid` is not in `current_uuids`: delete it
5. Commit once after all deletions
6. Return count of deleted records

**Dry-run:** Log each would-be deletion at INFO level, write nothing.

---

## Plausibility guard

Two checks before any deletions:

**Zero-result guard:** If `photosdb.photos()` returns 0 photos, abort with an error log and delete nothing. An empty result indicates an osxphotos failure, not a genuinely empty library.

**Mass-deletion guard:** If the number of records that would be deleted exceeds 10% of all Photos-only DB records, abort with a warning log and delete nothing. This prevents a partial osxphotos result (e.g., returning only recently-added photos) from silently nuking large numbers of records. Genuine mass-deletion (user deleted 80+ Photos-only photos at once) requires running `bp scan --all` a second time after investigating the warning.

---

## DB changes

**`db/db.py`** — one new method:

```python
def delete_photo(self, photo_id: int) -> None:
    """Hard-delete a Photos-only record and all cascaded rows."""
    self.conn.execute("DELETE FROM photos WHERE id = ?", (photo_id,))
```

`PRAGMA foreign_keys = ON` is already set in `Database.__init__`. The following tables use `ON DELETE CASCADE` on `photos.id` and are cleaned up automatically:

- `photo_albums`
- `metadata_proposals`
- `metadata_conflicts`

`tag_events` references the stale `photos_old` table name (migration artifact) — no constraint applies. `duplicate_groups.keeper_id` has no CASCADE, but Photos-only records do not appear as duplicate keepers (those are always Flickr-linked).

The commit for all deletions in a single run is issued once, at the end of `sync_deleted_photos()`.

---

## Scanner changes

**`poller/scanner.py`**

New function `sync_deleted_photos(photosdb, db, dry_run) -> int`.

Called at the end of `scan()`, after the main photo loop, only when `since is None`:

```python
deleted = 0
if since is None:
    deleted = sync_deleted_photos(photosdb, db, dry_run)
```

The `scan()` return tuple gains a `deleted` element:

```python
return scanned, matched, enriched, inserted, linked, deleted
```

Summary log line updated:

```
Scan complete: N scanned, N matched to Flickr, N late-linked, N re-enriched,
               N Photos-only inserted, N deleted (Photos removed)
```

`deleted` is always shown during `--all` runs so the user knows cleanup ran (even if 0).

---

## Testing

All tests in `tests/test_core.py`, osxphotos mocked — no live Photos access required.

| Test | Expected behaviour |
|------|--------------------|
| Photos-only record, UUID absent from Photos | Hard-deleted; photo_albums row CASCADE-deleted |
| Linked record (`flickr_id IS NOT NULL`), UUID absent | Not deleted |
| osxphotos returns 0 photos | 0 deletions, error logged, guard fires |
| Would-delete count > 10% of Photos-only total | 0 deletions, warning logged, guard fires |
| Dry-run with deletable records | Log output produced, DB unchanged |
| Multiple deletable records | All deleted, committed atomically |
| `sync_deleted_photos` called during incremental scan | Not called (`since is not None`) |
