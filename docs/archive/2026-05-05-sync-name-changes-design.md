# Sync Name Changes — Photos → Flickr (GH #50, Phase A) Design Spec

## Goal

When an Apple Photos album or folder is renamed, push the new name to the corresponding
Flickr photoset (via `flickr.photosets.editMeta`) or Collection (via
`flickr.collections.editMeta`) on the next sync run. Phase B (Flickr → Photos) is a
separate future issue.

---

## Approach

**Always push** — on every `sync-albums` and `sync-album-collections` run, unconditionally
send the current DB name to Flickr for every album/folder that already has a Flickr ID.
No new DB columns, no migration, no change-detection logic. The extra API calls are
negligible at personal-library scale (dozens to low hundreds of albums).

---

## Data model

No changes. After `bp scan`, `albums.name` and `folders.name` always reflect the current
Photos-side name. `albums.flickr_set_id` and `folders.flickr_collection_id` already
provide the Flickr handle needed to call `editMeta`.

---

## Flickr client additions (`flickr/flickr_client.py`)

Two new methods:

```python
def edit_photoset_meta(self, photoset_id: str, title: str) -> None:
    """Update the title of an existing Flickr photoset."""
    self._call(
        "flickr.photosets.editMeta",
        {"photoset_id": photoset_id, "title": title, "description": ""},
        http_method="POST",
    )

def edit_collection_meta(self, collection_id: str, title: str) -> None:
    """Update the title of an existing Flickr Collection."""
    self._call(
        "flickr.collections.editMeta",
        {"collection_id": collection_id, "title": title},
        http_method="POST",
    )
```

Both inherit the existing `_retry` / rate-limit logic from `_call`.

---

## sync_albums changes (`flickr/sync_albums.py`)

Add a `sync_album_titles(db, flickr, dry_run)` helper and call it at the end of
`sync_albums()` (or equivalent entry point — read the file to find the right hook):

```python
def sync_album_titles(db, flickr, dry_run: bool = False) -> dict:
    """Push current album names to Flickr photoset titles."""
    rows = db.conn.execute(
        "SELECT id, name, flickr_set_id FROM albums WHERE flickr_set_id IS NOT NULL"
    ).fetchall()

    updated = 0
    for row in rows:
        if dry_run:
            log.info("[dry-run] would update photoset title %r → %r", row["flickr_set_id"], row["name"])
            updated += 1
            continue
        try:
            flickr.edit_photoset_meta(row["flickr_set_id"], row["name"])
            updated += 1
        except Exception as e:
            log.warning("failed to update photoset title for album %r: %s", row["name"], e)

    log.info("sync-album-titles: updated=%d", updated)
    return {"updated": updated}
```

This is called once per `sync-albums` run, after the photo-membership sync. Respects
`--dry-run`.

---

## sync_collections changes (`flickr/sync_collections.py`)

In Pass 1, the "folder already has a collection ID" branch currently only increments the
`updated` counter. Extend it to also call `edit_collection_meta`:

```python
# existing folder — update title in case it was renamed in Photos
flickr.edit_collection_meta(collection_id, name)
totals["updated"] += 1
```

Wrap in a try/except to log and continue on failure (consistent with existing error
handling in Pass 2). Respects `--dry-run` because the dry-run block already returns
before Pass 1 executes.

---

## Error handling

| Situation | Behaviour |
|-----------|-----------|
| Transient API error on `editMeta` | `_retry` in `_call` handles up to 4 retries |
| Photoset/collection not found on Flickr | Log warning, continue — stale IDs are handled by existing sync logic |
| Flickr Pro required (collections) | Already handled upstream; `edit_collection_meta` only called when `flickr_collection_id` is set |
| `--dry-run` | Log what would be updated, make no API calls |

---

## Files touched

| File | Change |
|------|--------|
| `flickr/flickr_client.py` | Add `edit_photoset_meta`, `edit_collection_meta` |
| `flickr/sync_albums.py` | Add `sync_album_titles()`, call it from main sync flow |
| `flickr/sync_collections.py` | Call `edit_collection_meta` in Pass 1 for existing collections |
| `tests/test_core.py` | Tests for new client methods and title-sync behaviour |
| `README.md` | Note that `sync-albums` and `sync-album-collections` now also sync titles |

No migration. No new `bp all` stages. No new subcommands.
