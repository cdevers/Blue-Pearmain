# Sync Album Collections (GH #11) — Design Spec

## Goal

Mirror Apple Photos folder hierarchy as Flickr Collections, so that a
`Folder > Album` (or `Folder > Sub-folder > Album`) structure in Photos becomes
a matching `Collection > (Sub-collection >) Photoset` structure on Flickr.

Requires a Flickr Pro account. Albums with no folder remain as standalone
photosets, unchanged.

---

## New command

```
bp sync-album-collections [--dry-run] [--remove [--force]]
```

Slots into `bp all` after `sync-albums`. The `--remove` flag is never called
from `bp all` — it is opt-in only.

---

## Data model

### Migration 011

```sql
CREATE TABLE folders (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    apple_uuid           TEXT NOT NULL UNIQUE,
    name                 TEXT NOT NULL,
    parent_id            INTEGER REFERENCES folders(id) ON DELETE SET NULL,
    flickr_collection_id TEXT,   -- NULL until created on Flickr
    created_at           TEXT,
    updated_at           TEXT
);

ALTER TABLE albums ADD COLUMN folder_id INTEGER REFERENCES folders(id) ON DELETE SET NULL;
```

`parent_id` is self-referential: top-level folders have `NULL`, sub-folders
point to their parent row. `folder_id` on `albums` is `NULL` for albums not
inside any folder (they remain standalone photosets).

---

## Scanner changes (`poller/scanner.py`)

`sync_photo_albums()` already iterates `photo.album_info` per photo. It is
extended to also walk each album's folder ancestry.

### Algorithm

For each album in `photo.album_info`:

1. Walk `album.parent` up to the root, collecting `FolderInfo` objects in
   root-first order (so parents exist in the DB before children reference them).
2. Upsert each folder with `db.upsert_folder(apple_uuid, name, parent_id)`.
   A local `set` per scan run deduplicates calls — many photos share the same
   folder tree.
3. Pass the immediate parent's DB `id` as `folder_id` when upserting the album.

### New DB methods

- `upsert_folder(apple_uuid: str, name: str, parent_id: int | None = None) -> int`
  — upserts by `apple_uuid`, returns the row `id`.
- `upsert_album` gains an optional `folder_id: int | None = None` parameter
  (defaults to `None`; existing call sites are unaffected).

---

## Flickr client additions (`flickr/flickr_client.py`)

Three new methods:

```python
def create_collection(self, title: str, description: str = "") -> str:
    """Create a Flickr Collection. Returns collection_id."""

def edit_collection_sets(
    self,
    collection_id: str,
    photoset_ids: list[str],
    sub_collection_ids: list[str],
) -> None:
    """Full replace of a collection's contents.
    Flickr's flickr.collections.editSets overwrites — not additive."""

def delete_collection(self, collection_id: str) -> None:
    """Delete a Flickr Collection (used by --remove)."""
```

Non-Pro accounts receive a `FlickrError` from any Collections API call.
`sync-album-collections` catches this and logs:
`"Flickr Collections require a Pro account — skipping"`, then exits cleanly.

---

## `sync-album-collections` command (`flickr/sync_collections.py`)

### Normal sync

1. Load all `folders` rows from DB. Build a `parent_id → [child_ids]` map.
2. Topological sort: root folders first, then children recursively.
3. For each folder in order:
   a. If `flickr_collection_id` is `NULL`: call `create_collection(name)`,
      persist the returned ID to DB.
   b. Collect direct child photosets: albums where `folder_id = this folder`
      and `flickr_set_id IS NOT NULL`.
   c. Collect direct child sub-collections: child folders where
      `flickr_collection_id IS NOT NULL`.
   d. Call `edit_collection_sets(collection_id, photoset_ids, sub_collection_ids)`.
      This is a full replace every run, keeping Flickr state in sync with DB state.
4. Log summary: `collections created=N  updated=N  skipped=N`.

**Skipped cases** (logged at DEBUG, not errors):
- Albums in the folder with no `flickr_set_id` — not yet pushed to Flickr;
  will be included on the next run after `sync-albums` creates the photoset.
- Child folders with no `flickr_collection_id` — should not occur (parents
  are processed first), but defensively skipped with a warning.

### `--dry-run`

Logs what would be created or updated. No Flickr API calls. Respects the
same `--dry-run` flag passed through from `bp all --dry-run`.

### `--remove`

After the normal sync:

1. Read the live Photos library via osxphotos to get the current set of folder
   UUIDs. (Requires Photos to be accessible, same as `bp scan`.)
2. Compare against `folders` rows in the DB.
3. For any DB folder whose `apple_uuid` is not present in the live library,
   print the folder name and Flickr Collection ID and ask for confirmation
   (unless `--force` is also passed, which skips the prompt for scripted use).
4. On confirmation: call `delete_collection`, remove the DB row. The albums
   that were in that folder have their `folder_id` set to `NULL` by the FK's
   `ON DELETE SET NULL` cascade — they revert to standalone photosets.

---

## `bp all` integration

`sync-album-collections` is added to `bp all` after `sync-albums`:

```
bp all
  ...
  6. sync-albums           push photo membership → Flickr photosets
  7. sync-album-collections  group photosets → Flickr Collections
  8. checkpoint            trim WAL
```

`bp all --dry-run` passes `--dry-run` through. `--remove` is never included
in `bp all`.

---

## Error handling

| Situation | Behaviour |
|-----------|-----------|
| Non-Pro Flickr account | Log clear message, exit cleanly (not an error) |
| Album not yet pushed to Flickr (`flickr_set_id` NULL) | Skip silently; included on next run |
| Flickr API transient error | Existing retry logic in `FlickrClient._retry` handles it |
| Stale `flickr_collection_id` (collection deleted externally) | On `edit_collection_sets` error, clear the stored ID and re-create the collection |
| Photos not accessible during `--remove` | Log error, abort `--remove` step |
| Empty `folders` table | No-op; log `"no folders found — nothing to sync"` |

---

## Files touched

| File | Change |
|------|--------|
| `db/migrations/migrate_011_folders.py` | New migration |
| `db/schema.sql` | Updated to match |
| `db/db.py` | `upsert_folder()`, `upsert_album()` gains `folder_id` |
| `poller/scanner.py` | Walk folder ancestry in `sync_photo_albums()` |
| `flickr/flickr_client.py` | `create_collection`, `edit_collection_sets`, `delete_collection` |
| `flickr/sync_collections.py` | New command |
| `bp` | `sync-album-collections` subparser, add to `bp all` |
| `docs/pipeline.md` | Add stage 7, renumber checkpoint to 8 |
| `README.md` | Document new command |
