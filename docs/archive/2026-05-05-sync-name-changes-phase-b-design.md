# Sync Name Changes — Flickr → Apple Photos (GH #52, Phase B) Design Spec

## Goal

When a Flickr photoset or Collection is renamed directly on Flickr, detect it on the
next sync run and rename the corresponding Apple Photos album or folder.

Phase A (GH #50, v0.5.0) already pushes Photos renames → Flickr. Phase B is the
reverse direction.

---

## Conflict resolution policy

**Photos wins.** If both sides were renamed since the last sync, the Flickr rename is
ignored. Phase A will re-push the Photos name to Flickr on the next `sync-albums`
or `sync-album-collections` run, restoring consistency.

---

## Approach

**Last-pushed baseline.** Add a `flickr_name` column to `albums` and `folders` tables.
Phase A writes the pushed name into `flickr_name` after each successful `editMeta` call.
Phase B compares the live Flickr title against `flickr_name` to determine whether Flickr
was renamed since the last push.

Detection logic (per album/folder with a Flickr ID):

| `name` = `flickr_name`? | Flickr title = `flickr_name`? | Conclusion |
|------------------------|-------------------------------|------------|
| yes | yes | In sync — skip |
| yes | no | **Flickr renamed** → rename Photos, update DB |
| no | yes | Photos renamed — Phase A handles it on next sync |
| no | no | **Conflict** — Photos wins, skip (Phase A will re-push Photos name) |

`flickr_name IS NULL` means we have never pushed a title for this album/folder
(pre-Phase A records). Treat NULL as "no baseline" → skip detection, wait for
next Phase A run to establish `flickr_name`.

---

## Data model changes (migration 012)

```sql
ALTER TABLE albums  ADD COLUMN flickr_name TEXT;  -- NULL until first Phase A push
ALTER TABLE folders ADD COLUMN flickr_name TEXT;
```

No new tables. No cascades. Both columns default to NULL.

---

## Phase A changes (populate flickr_name)

### `flickr/sync_albums.py` — `sync_album_titles`

After each successful `flickr.edit_photoset_meta` call, write the pushed name back:

```python
db.set_album_flickr_name(album_id, row["name"])
```

Query must also SELECT `id` to have the album_id for the update.

### `flickr/sync_collections.py` — Pass 1 else branch

After successful `flickr.edit_collection_meta` call, write:

```python
db.set_folder_flickr_name(folder_id, name)
```

### `flickr/album_pusher.py` — photoset creation

When a new photoset is created (`create_photoset`), the album title is used as the
initial title. Set `flickr_name` at creation time:

```python
db.set_album_flickr_name(album_id, album_name)
```

(Albums created by `album_pusher` start with `flickr_name` = initial name.)

---

## New DB methods (`db/db.py`)

```python
def set_album_flickr_name(self, album_id: int, name: str) -> None:
    """Record the name most recently pushed to the Flickr photoset."""

def set_folder_flickr_name(self, folder_id: int, name: str) -> None:
    """Record the name most recently pushed to the Flickr Collection."""
```

---

## New Flickr client methods (`flickr/flickr_client.py`)

```python
def get_photosets_titled(self) -> dict[str, str]:
    """
    Return {flickr_set_id: title} for all the user's photosets.
    Uses flickr.photosets.getList (already present as get_photosets).
    """

def get_collections_flat(self) -> dict[str, str]:
    """
    Return {collection_id: title} by walking flickr.collections.getTree.
    Recursively flattens nested collections. Pro-only; raises FlickrError on
    non-Pro accounts.
    """
```

`get_collections_flat` calls `flickr.collections.getTree` and recursively
walks the returned tree, collecting `{id: title}` from every node.

---

## New command (`flickr/sync_names_from_flickr.py`)

```
bp sync-names-from-flickr [--dry-run]
```

### Algorithm

1. Fetch `{set_id: flickr_title}` via `flickr.get_photosets_titled()`.
2. Fetch `{col_id: flickr_title}` via `flickr.get_collections_flat()` (skip if
   non-Pro — catches `FlickrError` with "pro" in the message, logs, continues).
3. Load all albums where `flickr_set_id IS NOT NULL` from DB.
4. For each album:
   - Look up `flickr_title = set_map.get(flickr_set_id)`. If not found (deleted on
     Flickr), skip with a debug log.
   - If `flickr_name IS NULL`: skip (no baseline — wait for Phase A to populate).
   - Apply the detection table above.
   - If Flickr-renamed: call `_rename_photos_album(apple_uuid, new_name)`, on success
     update `albums.name` and `albums.flickr_name` in DB.
5. Repeat for folders using the collection map.
6. Log summary: `sync-names-from-flickr done — albums renamed=N  skipped=N  folders renamed=N  skipped=N`.

### AppleScript rename helper

```python
import subprocess

def _rename_photos_album(apple_uuid: str, new_name: str) -> bool:
    """Rename a Photos album via AppleScript. Requires Photos.app to be running."""
    safe = new_name.replace("\\", "\\\\").replace('"', '\\"')
    script = f'tell application "Photos" to set name of album id "{apple_uuid}" to "{safe}"'
    r = subprocess.run(["osascript", "-e", script],
                       capture_output=True, text=True, timeout=10)
    return r.returncode == 0
```

Same shape for `_rename_photos_folder` (uses `folder id UUID` in AppleScript).

If Photos is not running or the album/folder cannot be found, `osascript` returns
a non-zero exit code. Log a warning and skip; the Flickr rename will be reverted
on the next `bp sync-albums` run (Phase A pushes the Photos name back).

### `--dry-run`

Log what would be renamed. Make no AppleScript calls and no DB writes.

---

## `bp all` integration

Insert `sync-names-from-flickr` between `poll` and `sync-albums` so that Photos-side
renames are propagated to the DB before Phase A pushes names to Flickr:

```
1. scan --all
2. poll
3. thumbs (non-dry-run only)
4. sync-names-from-flickr        ← new (Phase B)
5. pipeline
6. reconcile
7. sync-albums                   ← Phase A: pushes final names to Flickr
8. sync-album-collections        ← Phase A: pushes collection names to Flickr
9. checkpoint (non-dry-run only)
```

---

## Error handling

| Situation | Behaviour |
|-----------|-----------|
| Photos.app not running | AppleScript exits non-zero — log warning, skip rename |
| Album/folder UUID not found in Photos | osascript error — log warning, skip |
| Flickr set not in fetched list (deleted externally) | Skip with debug log |
| Non-Pro account (collections) | Catch FlickrError, log note, skip collections only |
| `flickr_name IS NULL` (no baseline) | Skip — wait for Phase A to establish baseline |
| Conflict (both renamed) | Photos wins — skip Flickr rename, Phase A re-pushes |
| `--dry-run` | Log only, no AppleScript, no DB writes |

---

## Files touched

| File | Change |
|------|--------|
| `db/migrations/migrate_012_flickr_name.py` | New migration |
| `db/schema.sql` | Add `flickr_name` to `albums` and `folders` |
| `db/db.py` | `set_album_flickr_name`, `set_folder_flickr_name` |
| `flickr/sync_albums.py` | `sync_album_titles` writes `flickr_name` after each push; query selects `id` |
| `flickr/sync_collections.py` | Pass 1 else-branch writes `flickr_name` after push |
| `flickr/album_pusher.py` | Set `flickr_name` at photoset creation time |
| `flickr/flickr_client.py` | `get_photosets_titled`, `get_collections_flat` |
| `flickr/sync_names_from_flickr.py` | New command |
| `bp` | `sync-names-from-flickr` subparser; add to `bp all` |
| `tests/test_core.py` | Tests for all changes |
| `README.md` | Document new command |
| `pyproject.toml` | Bump to 0.6.0 |
