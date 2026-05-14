# Sync Name Changes (Photos → Flickr) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When an Apple Photos album or folder is renamed, push the new name to its Flickr photoset or Collection on the next sync run.

**Architecture:** Two new Flickr client methods (`edit_photoset_meta`, `edit_collection_meta`) slot into existing sync commands. `sync_albums` gains a `sync_album_titles()` helper that calls `editMeta` for every album already pushed to Flickr. `sync_collections` calls `edit_collection_meta` for every folder that already has a Flickr Collection ID (in Pass 1). No new DB columns, no migration, no new `bp all` stages.

**Tech Stack:** Python 3.11, SQLite (via `db/db.py`), Flickr REST API (`flickr.photosets.editMeta`, `flickr.collections.editMeta`), pytest.

---

## File map

| File | Change |
|------|--------|
| `flickr/flickr_client.py` | Add `edit_photoset_meta()` and `edit_collection_meta()` |
| `flickr/sync_albums.py` | Add `sync_album_titles()` helper; wire into `main()` (remove early-exit so titles always sync) |
| `flickr/sync_collections.py` | Call `edit_collection_meta()` in Pass 1 for folders that already have a collection ID |
| `tests/test_core.py` | Tests for all three changes |
| `README.md` | Note that both commands now also sync titles |
| `pyproject.toml` | Bump to 0.5.0 |
| `uv.lock` | Regenerate |

---

## Task 1: Flickr client — edit_photoset_meta and edit_collection_meta

**Files:**
- Modify: `flickr/flickr_client.py` (insert `edit_photoset_meta` after line 361; insert `edit_collection_meta` after `delete_collection` ~line 400)
- Test: `tests/test_core.py` (add to `TestFlickrCollectionsClient`)

- [ ] **Step 1: Write the failing tests**

Find `class TestFlickrCollectionsClient` in `tests/test_core.py` and add two tests to it:

```python
def test_edit_photoset_meta_calls_correct_method(self):
    from unittest.mock import patch
    client = self._make_client()
    with patch.object(client, "_call", return_value={}) as mock_call:
        client.edit_photoset_meta("ps-123", "New Title")
    mock_call.assert_called_once_with(
        "flickr.photosets.editMeta",
        {"photoset_id": "ps-123", "title": "New Title", "description": ""},
        http_method="POST",
    )

def test_edit_collection_meta_calls_correct_method(self):
    from unittest.mock import patch
    client = self._make_client()
    with patch.object(client, "_call", return_value={}) as mock_call:
        client.edit_collection_meta("col-456", "Updated Folder")
    mock_call.assert_called_once_with(
        "flickr.collections.editMeta",
        {"collection_id": "col-456", "title": "Updated Folder"},
        http_method="POST",
    )
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/test_core.py -k "test_edit_photoset_meta or test_edit_collection_meta" -v
```

Expected: FAIL with `AttributeError: 'FlickrClient' object has no attribute 'edit_photoset_meta'`.

- [ ] **Step 3: Implement the two methods**

In `flickr/flickr_client.py`, add `edit_photoset_meta` after `add_photo_to_photoset` (after line 361, before the `# Collections` comment block):

```python
def edit_photoset_meta(self, photoset_id: str, title: str) -> None:
    """Update the title of an existing Flickr photoset."""
    self._call(
        "flickr.photosets.editMeta",
        {"photoset_id": photoset_id, "title": title, "description": ""},
        http_method="POST",
    )
```

Add `edit_collection_meta` after `delete_collection` (after its closing `)`), before the `# Thumbnail download` comment:

```python
def edit_collection_meta(self, collection_id: str, title: str) -> None:
    """Update the title of an existing Flickr Collection."""
    self._call(
        "flickr.collections.editMeta",
        {"collection_id": collection_id, "title": title},
        http_method="POST",
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/test_core.py -k "test_edit_photoset_meta or test_edit_collection_meta" -v
```

Expected: 2 PASS.

- [ ] **Step 5: Run full suite**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/ -q
```

Expected: 466 + 2 = 468 passed.

- [ ] **Step 6: Commit**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && git add flickr/flickr_client.py tests/test_core.py && git commit -m "feat: Flickr client — edit_photoset_meta, edit_collection_meta

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 2: sync_albums — sync_album_titles helper

**Files:**
- Modify: `flickr/sync_albums.py`
- Test: `tests/test_core.py` (add `TestSyncAlbumTitles` class)

### Context

`flickr/sync_albums.py` currently has an early-exit at the top of `main()` when `pending` is empty:

```python
if not pending:
    print("albums created=0  photos added=0  skipped=0  failed=0")
    return 0
```

This must be changed so that `sync_album_titles` always runs even when there are no pending photo-membership pushes (e.g., photos are up to date but album names changed).

- [ ] **Step 1: Write the failing tests**

Add `TestSyncAlbumTitles` to `tests/test_core.py` (place near `TestSyncCollections`):

```python
class TestSyncAlbumTitles(unittest.TestCase):
    """sync_album_titles: pushes current album names to Flickr photoset titles."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        from db.db import Database
        self.db = Database(Path(self._tmp.name) / "test.db")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _make_flickr(self):
        from unittest.mock import MagicMock
        m = MagicMock()
        m.edit_photoset_meta.return_value = None
        return m

    def _seed_album(self, uuid, name, flickr_set_id=None):
        aid = self.db.upsert_album(uuid, name)
        if flickr_set_id:
            self.db.set_album_flickr_set_id(aid, flickr_set_id)
        return aid

    def test_calls_edit_meta_for_each_pushed_album(self):
        from flickr.sync_albums import sync_album_titles
        self._seed_album("uuid-1", "Paris Trip", flickr_set_id="ps-111")
        self._seed_album("uuid-2", "Rome Pics",  flickr_set_id="ps-222")
        flickr = self._make_flickr()

        result = sync_album_titles(self.db, flickr)

        self.assertEqual(flickr.edit_photoset_meta.call_count, 2)
        calls = {c[0] for c in flickr.edit_photoset_meta.call_args_list}
        self.assertIn(("ps-111", "Paris Trip"), calls)
        self.assertIn(("ps-222", "Rome Pics"), calls)
        self.assertEqual(result["updated"], 2)

    def test_skips_albums_without_flickr_set_id(self):
        from flickr.sync_albums import sync_album_titles
        self._seed_album("uuid-1", "Not Pushed Yet")  # no flickr_set_id
        flickr = self._make_flickr()

        sync_album_titles(self.db, flickr)

        flickr.edit_photoset_meta.assert_not_called()

    def test_dry_run_makes_no_api_calls(self):
        from flickr.sync_albums import sync_album_titles
        self._seed_album("uuid-1", "Paris Trip", flickr_set_id="ps-111")
        flickr = self._make_flickr()

        result = sync_album_titles(self.db, flickr, dry_run=True)

        flickr.edit_photoset_meta.assert_not_called()
        self.assertEqual(result["updated"], 1)

    def test_continues_on_api_error(self):
        from flickr.sync_albums import sync_album_titles
        self._seed_album("uuid-1", "Album A", flickr_set_id="ps-111")
        self._seed_album("uuid-2", "Album B", flickr_set_id="ps-222")
        flickr = self._make_flickr()
        flickr.edit_photoset_meta.side_effect = [Exception("timeout"), None]

        result = sync_album_titles(self.db, flickr)

        # Both attempted; one failed but we continued
        self.assertEqual(flickr.edit_photoset_meta.call_count, 2)
        self.assertEqual(result["updated"], 1)

    def test_no_albums_is_noop(self):
        from flickr.sync_albums import sync_album_titles
        flickr = self._make_flickr()
        result = sync_album_titles(self.db, flickr)
        flickr.edit_photoset_meta.assert_not_called()
        self.assertEqual(result["updated"], 0)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/test_core.py -k "TestSyncAlbumTitles" -v
```

Expected: FAIL with `ImportError: cannot import name 'sync_album_titles'`.

- [ ] **Step 3: Implement sync_album_titles**

Add this function to `flickr/sync_albums.py` (after the `_count_created_sets` helper at the bottom, before `if __name__ == "__main__"`):

```python
def sync_album_titles(db, flickr, dry_run: bool = False) -> dict:
    """Push current album names to Flickr photoset titles for all pushed albums."""
    rows = db.conn.execute(
        "SELECT name, flickr_set_id FROM albums WHERE flickr_set_id IS NOT NULL"
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

- [ ] **Step 4: Wire sync_album_titles into main()**

In `flickr/sync_albums.py`, the current `main()` has an early-exit when `pending` is empty (around line 70). Remove it and restructure so `sync_album_titles` always runs. Replace this block:

```python
    if not pending:
        print("albums created=0  photos added=0  skipped=0  failed=0")
        return 0

    # Deduplicate by photo_id so we call push_photo_to_albums once per photo
    seen_photo_ids: set[int] = set()
    unique_photos: list[int] = []
    for row in pending:
        pid = row["photo_id"]
        if pid not in seen_photo_ids:
            seen_photo_ids.add(pid)
            unique_photos.append(pid)

    from flickr.album_pusher import push_photo_to_albums
    from flickr.flickr_client import FlickrError

    albums_before = _count_created_sets(db)
    added   = 0
    skipped = 0
    failed  = 0

    for photo_id in unique_photos:
        if args.dry_run:
            photo = db.get_photo(photo_id)
            flickr_id = photo.get("flickr_id") if photo else None
            if flickr_id:
                log.info("[dry-run] would push photo_id=%s flickr_id=%s to albums", photo_id, flickr_id)
                skipped += 1
            else:
                skipped += 1
            continue

        try:
            n = push_photo_to_albums(db, flickr, photo_id)
            added += n
            if n == 0:
                skipped += 1
        except Exception as e:
            log.error("sync-albums: unexpected error photo_id=%s: %s", photo_id, e)
            failed += 1

    albums_created = _count_created_sets(db) - albums_before
    print(
        f"albums created={albums_created}  "
        f"photos added={added}  "
        f"skipped={skipped}  "
        f"failed={failed}"
    )

    db.close()
    return 1 if failed else 0
```

With:

```python
    # Deduplicate by photo_id so we call push_photo_to_albums once per photo
    seen_photo_ids: set[int] = set()
    unique_photos: list[int] = []
    for row in pending:
        pid = row["photo_id"]
        if pid not in seen_photo_ids:
            seen_photo_ids.add(pid)
            unique_photos.append(pid)

    from flickr.album_pusher import push_photo_to_albums

    albums_before = _count_created_sets(db)
    added   = 0
    skipped = 0
    failed  = 0

    for photo_id in unique_photos:
        if args.dry_run:
            photo = db.get_photo(photo_id)
            flickr_id = photo.get("flickr_id") if photo else None
            if flickr_id:
                log.info("[dry-run] would push photo_id=%s flickr_id=%s to albums", photo_id, flickr_id)
                skipped += 1
            else:
                skipped += 1
            continue

        try:
            n = push_photo_to_albums(db, flickr, photo_id)
            added += n
            if n == 0:
                skipped += 1
        except Exception as e:
            log.error("sync-albums: unexpected error photo_id=%s: %s", photo_id, e)
            failed += 1

    albums_created = _count_created_sets(db) - albums_before
    print(
        f"albums created={albums_created}  "
        f"photos added={added}  "
        f"skipped={skipped}  "
        f"failed={failed}"
    )

    sync_album_titles(db, flickr, dry_run=args.dry_run)

    db.close()
    return 1 if failed else 0
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/test_core.py -k "TestSyncAlbumTitles" -v
```

Expected: 5 PASS.

- [ ] **Step 6: Run full suite**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/ -q
```

Expected: 468 + 5 = 473 passed.

- [ ] **Step 7: Commit**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && git add flickr/sync_albums.py tests/test_core.py && git commit -m "feat: sync_albums pushes album title changes to Flickr photosets

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 3: sync_collections — push title updates for existing collections

**Files:**
- Modify: `flickr/sync_collections.py` (Pass 1, `else` branch ~line 74)
- Test: `tests/test_core.py` (add tests to `TestSyncCollections`)

- [ ] **Step 1: Write the failing tests**

Find `class TestSyncCollections` in `tests/test_core.py` and add two tests:

```python
def test_updates_collection_title_for_existing_collection(self):
    from flickr.sync_collections import sync_collections
    self._seed_folder("uuid-f1", "New Name", collection_id="col-existing")
    flickr = self._make_flickr()

    sync_collections(self.db, flickr)

    flickr.edit_collection_meta.assert_called_once_with("col-existing", "New Name")

def test_dry_run_does_not_call_edit_collection_meta(self):
    from flickr.sync_collections import sync_collections
    self._seed_folder("uuid-f1", "Travel", collection_id="col-existing")
    flickr = self._make_flickr()

    sync_collections(self.db, flickr, dry_run=True)

    flickr.edit_collection_meta.assert_not_called()
```

Also update `_make_flickr` in `TestSyncCollections` to include `edit_collection_meta`:

```python
def _make_flickr(self, **side_effects):
    from unittest.mock import MagicMock
    m = MagicMock()
    m.create_collection.return_value = "col-new"
    m.edit_collection_sets.return_value = None
    m.edit_collection_meta.return_value = None
    m.delete_collection.return_value = None
    for attr, val in side_effects.items():
        setattr(m, attr, val)
    return m
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/test_core.py -k "test_updates_collection_title or test_dry_run_does_not_call_edit_collection_meta" -v
```

Expected: FAIL — `edit_collection_meta` not called yet.

- [ ] **Step 3: Implement the change**

In `flickr/sync_collections.py`, find the Pass 1 `else` branch (around line 74):

```python
        else:
            totals["updated"] += 1
```

Replace with:

```python
        else:
            try:
                flickr.edit_collection_meta(collection_id, name)
            except Exception as e:
                log.warning("failed to update collection title for %r: %s", name, e)
            totals["updated"] += 1
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/test_core.py -k "TestSyncCollections" -v
```

Expected: all PASS (existing + 2 new).

- [ ] **Step 5: Run full suite**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/ -q
```

Expected: 473 + 2 = 475 passed.

- [ ] **Step 6: Commit**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && git add flickr/sync_collections.py tests/test_core.py && git commit -m "feat: sync_collections pushes folder title changes to Flickr Collections

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 4: README, version bump, close issue

**Files:**
- Modify: `README.md`
- Modify: `pyproject.toml`
- Modify: `uv.lock`

- [ ] **Step 1: Update README.md**

Find the section describing `bp sync-albums` and `bp sync-album-collections` commands and add a note that they now also sync title changes. For example, in the description of `bp sync-albums`:

```
bp sync-albums           # Push photo membership + title changes → Flickr photosets
```

And for `bp sync-album-collections`:

```
bp sync-album-collections           # Sync folder hierarchy + title changes → Flickr Collections
```

Also update the test count to match the actual passing count from the full suite run in Task 3.

- [ ] **Step 2: Bump version**

In `pyproject.toml`, change:

```toml
version = "0.4.0"
```

to:

```toml
version = "0.5.0"
```

- [ ] **Step 3: Regenerate lock file**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && uv lock
```

- [ ] **Step 4: Run full suite one final time**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/ -q
```

Expected: 475 passed.

- [ ] **Step 5: Commit and tag**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && git add README.md pyproject.toml uv.lock && git commit -m "Bump version to 0.5.0 — sync album/collection title changes (closes #50)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
git tag v0.5.0
```

- [ ] **Step 6: Close GH issue**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && gh issue close 50 --comment "Phase A implemented in v0.5.0: bp sync-albums now calls flickr.photosets.editMeta for all pushed albums on every run; bp sync-album-collections calls flickr.collections.editMeta for existing collections. Phase B (Flickr → Photos) tracked separately."
```
