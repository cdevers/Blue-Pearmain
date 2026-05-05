# Sync Name Changes Phase B (Flickr → Photos) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect Flickr photoset/Collection renames and propagate them back to Apple Photos albums/folders via AppleScript.

**Architecture:** New `flickr_name` columns track the last name pushed to Flickr (by Phase A). Phase B fetches live Flickr titles, detects drift from the baseline, renames Photos via `osascript`, and updates the DB. Photos wins on conflict.

**Tech Stack:** Python 3.11, SQLite, Flickr REST API, subprocess/osascript, pytest.

**GH issue:** #52

---

## File map

| File | Change |
|------|--------|
| `db/migrations/migrate_012_flickr_name.py` | New — ADD COLUMN flickr_name to albums + folders |
| `db/schema.sql` | Add `flickr_name TEXT` to both tables |
| `db/db.py` | Add `set_album_flickr_name`, `set_folder_flickr_name` |
| `flickr/sync_albums.py` | `sync_album_titles`: SELECT id, write flickr_name after push |
| `flickr/sync_collections.py` | Pass 1 else-branch: write flickr_name after push |
| `flickr/album_pusher.py` | Set flickr_name at photoset creation |
| `flickr/flickr_client.py` | Add `get_photosets_titled`, `get_collections_flat` |
| `flickr/sync_names_from_flickr.py` | New command — detect Flickr renames, rename Photos |
| `bp` | Add `sync-names-from-flickr` subcommand; insert into `bp all` |
| `tests/test_core.py` | Tests throughout |
| `README.md` | Document new command; update test count |
| `pyproject.toml` | Bump to 0.6.0 |

---

## Task 1: Migration 012 + DB methods

**Files:**
- Create: `db/migrations/migrate_012_flickr_name.py`
- Modify: `db/schema.sql`
- Modify: `db/db.py`
- Test: `tests/test_core.py`

- [ ] **Step 1: Write the failing tests**

Add a new class `TestMigrate012FlickrName` in `tests/test_core.py` (place near other migration tests):

```python
class TestMigrate012FlickrName(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "test.db")

    def tearDown(self):
        self._tmp.cleanup()

    def test_migration_adds_flickr_name_to_albums(self):
        from db.db import Database
        from db.migrations.migrate_012_flickr_name import run
        db = Database(Path(self.db_path))
        run(self.db_path)
        row = db.conn.execute("PRAGMA table_info(albums)").fetchall()
        cols = {r["name"] for r in row}
        self.assertIn("flickr_name", cols)
        db.close()

    def test_migration_adds_flickr_name_to_folders(self):
        from db.db import Database
        from db.migrations.migrate_012_flickr_name import run
        db = Database(Path(self.db_path))
        run(self.db_path)
        row = db.conn.execute("PRAGMA table_info(folders)").fetchall()
        cols = {r["name"] for r in row}
        self.assertIn("flickr_name", cols)
        db.close()

    def test_migration_is_idempotent(self):
        from db.db import Database
        from db.migrations.migrate_012_flickr_name import run
        db = Database(Path(self.db_path))
        run(self.db_path)
        run(self.db_path)  # second run must not raise
        db.close()
```

Add DB method tests to `TestDatabase` (find the existing class in test_core.py):

```python
def test_set_album_flickr_name(self):
    album_id = self.db.upsert_album("uuid-a1", "Paris")
    self.db.set_album_flickr_name(album_id, "Paris")
    row = self.db.conn.execute(
        "SELECT flickr_name FROM albums WHERE id = ?", (album_id,)
    ).fetchone()
    self.assertEqual(row["flickr_name"], "Paris")

def test_set_folder_flickr_name(self):
    folder_id = self.db.upsert_folder("uuid-f1", "Travel")
    self.db.set_folder_flickr_name(folder_id, "Travel")
    row = self.db.conn.execute(
        "SELECT flickr_name FROM folders WHERE id = ?", (folder_id,)
    ).fetchone()
    self.assertEqual(row["flickr_name"], "Travel")
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/test_core.py -k "TestMigrate012 or test_set_album_flickr_name or test_set_folder_flickr_name" -v
```

Expected: FAIL (`migrate_012_flickr_name` not found, `set_album_flickr_name` not found).

- [ ] **Step 3: Create the migration**

Create `db/migrations/migrate_012_flickr_name.py`:

```python
"""
migrate_012_flickr_name.py

Adds:
  albums.flickr_name  TEXT — last album name successfully pushed to Flickr photoset
  folders.flickr_name TEXT — last folder name successfully pushed to Flickr Collection

Safe to run multiple times (idempotent — checks schema_migrations).

Usage:
    python db/migrations/migrate_012_flickr_name.py --config config/config.yml
"""

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import yaml

MIGRATION_NAME = "migrate_012_flickr_name"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run(db_path: str, dry_run: bool = False) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    try:
        row = conn.execute(
            "SELECT id FROM schema_migrations WHERE name = ?", (MIGRATION_NAME,)
        ).fetchone()
        if row is not None:
            print("  Skipped:  migration already applied")
            conn.close()
            return
    except Exception:
        pass

    if dry_run:
        print("  [dry-run] Would add albums.flickr_name column")
        print("  [dry-run] Would add folders.flickr_name column")
        conn.close()
        return

    conn.execute("BEGIN")
    conn.execute("ALTER TABLE albums  ADD COLUMN flickr_name TEXT")
    conn.execute("ALTER TABLE folders ADD COLUMN flickr_name TEXT")
    conn.execute(
        "INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)",
        (MIGRATION_NAME, now_iso()),
    )
    conn.commit()
    conn.close()
    print("  Applied:  migrate_012_flickr_name")


def main() -> int:
    parser = argparse.ArgumentParser(description="Migration 012: add flickr_name columns")
    parser.add_argument("--config", default="config/config.yml")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with open(args.config) as f:
        config = yaml.safe_load(f)

    db_path = str(Path(config["database"]["path"]).expanduser())
    run(db_path, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
```

- [ ] **Step 4: Update db/schema.sql**

In the `albums` table definition, after the `flickr_set_url` column:

```sql
    flickr_name     TEXT,                   -- last name pushed to Flickr photoset title
```

In the `folders` table definition, after the `flickr_collection_id` column:

```sql
    flickr_name     TEXT,                   -- last name pushed to Flickr Collection title
```

- [ ] **Step 5: Add DB methods to db/db.py**

Find the `set_album_flickr_set_id` method and add after it:

```python
def set_album_flickr_name(self, album_id: int, name: str) -> None:
    """Record the name most recently pushed to the Flickr photoset title."""
    self.conn.execute(
        "UPDATE albums SET flickr_name = ?, updated_at = ? WHERE id = ?",
        (name, _now_iso(), album_id),
    )
    self.conn.commit()
```

Find the `set_folder_flickr_collection_id` method and add after it:

```python
def set_folder_flickr_name(self, folder_id: int, name: str) -> None:
    """Record the name most recently pushed to the Flickr Collection title."""
    self.conn.execute(
        "UPDATE folders SET flickr_name = ?, updated_at = ? WHERE id = ?",
        (name, _now_iso(), folder_id),
    )
    self.conn.commit()
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/test_core.py -k "TestMigrate012 or test_set_album_flickr_name or test_set_folder_flickr_name" -v
```

Expected: 5 PASS.

- [ ] **Step 7: Run full suite**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/ -q
```

Expected: 476 + 5 = 481 passed.

- [ ] **Step 8: Commit**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && git add db/migrations/migrate_012_flickr_name.py db/schema.sql db/db.py tests/test_core.py
git commit -m "feat: migration 012 — add flickr_name to albums and folders

Closes #52 (partial — Phase B baseline tracking)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 2: Track flickr_name in Phase A (sync_albums + sync_collections + album_pusher)

**Files:**
- Modify: `flickr/sync_albums.py`
- Modify: `flickr/sync_collections.py`
- Modify: `flickr/album_pusher.py`
- Test: `tests/test_core.py`

### Context

`sync_album_titles` in `flickr/sync_albums.py` currently queries:
```python
"SELECT name, flickr_set_id FROM albums WHERE flickr_set_id IS NOT NULL"
```
It needs to also select `id` so we can call `db.set_album_flickr_name(album_id, name)`.

`sync_collections.py` Pass 1 else-branch (around line 74):
```python
        else:
            try:
                flickr.edit_collection_meta(collection_id, name)
            except Exception as e:
                log.warning("failed to update collection title for %r: %s", name, e)
            totals["updated"] += 1
```
Needs to also call `db.set_folder_flickr_name(folder_id, name)` on success.

`album_pusher.py` creates new photosets at line ~51. After `db.set_album_flickr_set_id`, also call `db.set_album_flickr_name`.

- [ ] **Step 1: Write the failing tests**

In `TestSyncAlbumTitles`, add one test after the existing ones:

```python
def test_writes_flickr_name_after_successful_push(self):
    from flickr.sync_albums import sync_album_titles
    aid = self._seed_album("uuid-1", "Paris Trip", flickr_set_id="ps-111")
    flickr = self._make_flickr()

    sync_album_titles(self.db, flickr)

    row = self.db.conn.execute(
        "SELECT flickr_name FROM albums WHERE id = ?", (aid,)
    ).fetchone()
    self.assertEqual(row["flickr_name"], "Paris Trip")

def test_does_not_write_flickr_name_on_api_error(self):
    from flickr.sync_albums import sync_album_titles
    aid = self._seed_album("uuid-1", "Paris Trip", flickr_set_id="ps-111")
    flickr = self._make_flickr()
    flickr.edit_photoset_meta.side_effect = Exception("timeout")

    sync_album_titles(self.db, flickr)

    row = self.db.conn.execute(
        "SELECT flickr_name FROM albums WHERE id = ?", (aid,)
    ).fetchone()
    self.assertIsNone(row["flickr_name"])
```

In `TestSyncCollections`, add one test:

```python
def test_writes_flickr_name_for_existing_collection(self):
    from flickr.sync_collections import sync_collections
    fid = self._seed_folder("uuid-f1", "Travel", collection_id="col-existing")
    flickr = self._make_flickr()

    sync_collections(self.db, flickr)

    row = self.db.conn.execute(
        "SELECT flickr_name FROM folders WHERE id = ?", (fid,)
    ).fetchone()
    self.assertEqual(row["flickr_name"], "Travel")
```

Note: `_seed_folder` returns the folder id — check whether the existing helper returns it. If not, update it to return `id` by fetching after upsert:

```python
def _seed_folder(self, uuid, name, parent_id=None, collection_id=None):
    fid = self.db.upsert_folder(uuid, name, parent_id=parent_id)
    if collection_id:
        self.db.set_folder_flickr_collection_id(fid, collection_id)
    return fid
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/test_core.py -k "test_writes_flickr_name" -v
```

Expected: FAIL (flickr_name not being written yet).

- [ ] **Step 3: Update sync_album_titles in sync_albums.py**

Change the query and add `db.set_album_flickr_name` on success:

```python
def sync_album_titles(db, flickr, dry_run: bool = False) -> dict:
    """Push current album names to Flickr photoset titles for all pushed albums."""
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
            db.set_album_flickr_name(row["id"], row["name"])
            updated += 1
        except Exception as e:
            log.warning("failed to update photoset title for album %r: %s", row["name"], e)

    if dry_run:
        log.info("sync-album-titles: [dry-run] would-update=%d", updated)
    else:
        log.info("sync-album-titles: updated=%d", updated)
    return {"updated": updated}
```

- [ ] **Step 4: Update sync_collections.py Pass 1 else-branch**

Read the file to find the exact variable names, then update the else-branch. The current code (after Task 3 of Phase A) is:

```python
        else:
            try:
                flickr.edit_collection_meta(collection_id, name)
            except Exception as e:
                log.warning("failed to update collection title for %r: %s", name, e)
            totals["updated"] += 1
```

The variables `folder_id`, `collection_id`, and `name` are bound at the top of the Pass 1 loop. Replace with:

```python
        else:
            try:
                flickr.edit_collection_meta(collection_id, name)
                db.set_folder_flickr_name(folder_id, name)
            except Exception as e:
                log.warning("failed to update collection title for %r: %s", name, e)
            totals["updated"] += 1
```

- [ ] **Step 5: Update album_pusher.py**

After `db.set_album_flickr_set_id(album_id, flickr_set_id)` (around line 52), add:

```python
                db.set_album_flickr_name(album_id, album_name)
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/test_core.py -k "test_writes_flickr_name" -v
```

Expected: 3 PASS.

- [ ] **Step 7: Run full suite**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/ -q
```

Expected: 481 + 3 = 484 passed.

- [ ] **Step 8: Commit**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && git add flickr/sync_albums.py flickr/sync_collections.py flickr/album_pusher.py tests/test_core.py
git commit -m "feat: Phase A writes flickr_name baseline after each title push

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 3: FlickrClient — get_photosets_titled + get_collections_flat

**Files:**
- Modify: `flickr/flickr_client.py`
- Test: `tests/test_core.py`

- [ ] **Step 1: Write the failing tests**

Find `class TestFlickrCollectionsClient` in `tests/test_core.py` and add:

```python
def test_get_photosets_titled_returns_id_title_dict(self):
    from unittest.mock import patch
    client = self._make_client()
    api_response = {
        "stat": "ok",
        "photosets": {
            "photoset": [
                {"id": "ps-1", "title": {"_content": "Paris"}},
                {"id": "ps-2", "title": {"_content": "Rome"}},
            ]
        },
    }
    with patch.object(client, "_call", return_value=api_response):
        result = client.get_photosets_titled()
    self.assertEqual(result, {"ps-1": "Paris", "ps-2": "Rome"})

def test_get_collections_flat_returns_id_title_dict(self):
    from unittest.mock import patch
    client = self._make_client()
    api_response = {
        "stat": "ok",
        "collections": {
            "collection": [
                {
                    "id": "col-1",
                    "title": "Top",
                    "collection": [
                        {"id": "col-2", "title": "Nested", "collection": [], "set": []}
                    ],
                    "set": [],
                }
            ]
        },
    }
    with patch.object(client, "_call", return_value=api_response):
        result = client.get_collections_flat()
    self.assertEqual(result, {"col-1": "Top", "col-2": "Nested"})
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/test_core.py -k "test_get_photosets_titled or test_get_collections_flat" -v
```

Expected: FAIL — methods not found.

- [ ] **Step 3: Implement the two methods**

In `flickr/flickr_client.py`, add `get_photosets_titled` after `get_photosets`:

```python
def get_photosets_titled(self) -> dict[str, str]:
    """Return {photoset_id: title} for all the user's photosets."""
    data = self._call(
        "flickr.photosets.getList",
        {"user_id": self.user_nsid or "me"},
    )
    result = {}
    for ps in data.get("photosets", {}).get("photoset", []):
        title = ps.get("title", {})
        if isinstance(title, dict):
            title = title.get("_content", "")
        result[ps["id"]] = title
    return result
```

Add `get_collections_flat` after `edit_collection_meta`:

```python
def get_collections_flat(self) -> dict[str, str]:
    """Return {collection_id: title} by flattening flickr.collections.getTree.
    Raises FlickrError on non-Pro accounts."""
    data = self._call("flickr.collections.getTree")

    result: dict[str, str] = {}

    def _walk(nodes: list[dict]) -> None:
        for node in nodes:
            result[node["id"]] = node.get("title", "")
            _walk(node.get("collection", []))

    _walk(data.get("collections", {}).get("collection", []))
    return result
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/test_core.py -k "test_get_photosets_titled or test_get_collections_flat" -v
```

Expected: 2 PASS.

- [ ] **Step 5: Run full suite**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/ -q
```

Expected: 484 + 2 = 486 passed.

- [ ] **Step 6: Commit**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && git add flickr/flickr_client.py tests/test_core.py
git commit -m "feat: FlickrClient — get_photosets_titled, get_collections_flat

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 4: flickr/sync_names_from_flickr.py — main Phase B command

**Files:**
- Create: `flickr/sync_names_from_flickr.py`
- Test: `tests/test_core.py`

### Context

This command:
1. Fetches live Flickr titles for all photosets/collections
2. Compares each against `flickr_name` (baseline) and `name` (Photos-side)
3. If only Flickr changed: rename Photos album via AppleScript, update DB
4. Conflict (both changed): skip — Phase A will re-push Photos name

Decision table for each album where `flickr_set_id IS NOT NULL`:

| `flickr_name IS NULL` | `name` = `flickr_name` | Flickr title = `flickr_name` | Action |
|-----------------------|----------------------|------------------------------|--------|
| yes | — | — | skip (no baseline) |
| no | yes | yes | skip (in sync) |
| no | yes | no | **Flickr renamed** → rename Photos, update DB |
| no | no | yes | skip (Photos renamed, Phase A handles) |
| no | no | no | skip (conflict, Photos wins) |

- [ ] **Step 1: Write the failing tests**

Add `class TestSyncNamesFromFlickr` in `tests/test_core.py` (place after `TestSyncCollections`):

```python
class TestSyncNamesFromFlickr(unittest.TestCase):
    """sync_names_from_flickr: propagate Flickr-side renames back to Photos."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        from db.db import Database
        self.db = Database(Path(self._tmp.name) / "test.db")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _seed_album(self, uuid, name, flickr_set_id=None, flickr_name=None):
        aid = self.db.upsert_album(uuid, name)
        if flickr_set_id:
            self.db.set_album_flickr_set_id(aid, flickr_set_id)
        if flickr_name is not None:
            self.db.set_album_flickr_name(aid, flickr_name)
        return aid

    def _seed_folder(self, uuid, name, collection_id=None, flickr_name=None):
        fid = self.db.upsert_folder(uuid, name)
        if collection_id:
            self.db.set_folder_flickr_collection_id(fid, collection_id)
        if flickr_name is not None:
            self.db.set_folder_flickr_name(fid, flickr_name)
        return fid

    def _make_flickr(self, photosets=None, collections=None):
        from unittest.mock import MagicMock
        m = MagicMock()
        m.get_photosets_titled.return_value = photosets or {}
        m.get_collections_flat.return_value = collections or {}
        return m

    def test_renames_photos_album_when_flickr_title_changed(self):
        from unittest.mock import patch
        from flickr.sync_names_from_flickr import sync_names_from_flickr
        aid = self._seed_album("uuid-1", "Old Name", flickr_set_id="ps-1", flickr_name="Old Name")
        flickr = self._make_flickr(photosets={"ps-1": "New Flickr Name"})

        with patch("flickr.sync_names_from_flickr._rename_photos_album", return_value=True) as mock_rename:
            result = sync_names_from_flickr(self.db, flickr)

        mock_rename.assert_called_once_with("uuid-1", "New Flickr Name")
        row = self.db.conn.execute("SELECT name, flickr_name FROM albums WHERE id = ?", (aid,)).fetchone()
        self.assertEqual(row["name"], "New Flickr Name")
        self.assertEqual(row["flickr_name"], "New Flickr Name")
        self.assertEqual(result["albums_renamed"], 1)

    def test_skips_when_no_baseline(self):
        from unittest.mock import patch
        from flickr.sync_names_from_flickr import sync_names_from_flickr
        self._seed_album("uuid-1", "Old Name", flickr_set_id="ps-1")  # flickr_name=None
        flickr = self._make_flickr(photosets={"ps-1": "Whatever"})

        with patch("flickr.sync_names_from_flickr._rename_photos_album") as mock_rename:
            result = sync_names_from_flickr(self.db, flickr)

        mock_rename.assert_not_called()
        self.assertEqual(result["albums_renamed"], 0)

    def test_skips_when_in_sync(self):
        from unittest.mock import patch
        from flickr.sync_names_from_flickr import sync_names_from_flickr
        self._seed_album("uuid-1", "Paris", flickr_set_id="ps-1", flickr_name="Paris")
        flickr = self._make_flickr(photosets={"ps-1": "Paris"})

        with patch("flickr.sync_names_from_flickr._rename_photos_album") as mock_rename:
            sync_names_from_flickr(self.db, flickr)

        mock_rename.assert_not_called()

    def test_skips_conflict_photos_wins(self):
        from unittest.mock import patch
        from flickr.sync_names_from_flickr import sync_names_from_flickr
        # Both sides renamed: DB name="Photos New", flickr_name="Old", Flickr title="Flickr New"
        self._seed_album("uuid-1", "Photos New", flickr_set_id="ps-1", flickr_name="Old Name")
        flickr = self._make_flickr(photosets={"ps-1": "Flickr New"})

        with patch("flickr.sync_names_from_flickr._rename_photos_album") as mock_rename:
            result = sync_names_from_flickr(self.db, flickr)

        mock_rename.assert_not_called()
        self.assertEqual(result["albums_renamed"], 0)

    def test_dry_run_makes_no_changes(self):
        from unittest.mock import patch
        from flickr.sync_names_from_flickr import sync_names_from_flickr
        aid = self._seed_album("uuid-1", "Old Name", flickr_set_id="ps-1", flickr_name="Old Name")
        flickr = self._make_flickr(photosets={"ps-1": "New Flickr Name"})

        with patch("flickr.sync_names_from_flickr._rename_photos_album") as mock_rename:
            result = sync_names_from_flickr(self.db, flickr, dry_run=True)

        mock_rename.assert_not_called()
        row = self.db.conn.execute("SELECT name FROM albums WHERE id = ?", (aid,)).fetchone()
        self.assertEqual(row["name"], "Old Name")  # unchanged
        self.assertEqual(result["albums_renamed"], 1)  # counted but not applied

    def test_skips_when_rename_fails(self):
        from unittest.mock import patch
        from flickr.sync_names_from_flickr import sync_names_from_flickr
        aid = self._seed_album("uuid-1", "Old Name", flickr_set_id="ps-1", flickr_name="Old Name")
        flickr = self._make_flickr(photosets={"ps-1": "New Flickr Name"})

        with patch("flickr.sync_names_from_flickr._rename_photos_album", return_value=False):
            result = sync_names_from_flickr(self.db, flickr)

        # DB not updated when rename fails
        row = self.db.conn.execute("SELECT name FROM albums WHERE id = ?", (aid,)).fetchone()
        self.assertEqual(row["name"], "Old Name")
        self.assertEqual(result["albums_renamed"], 0)

    def test_renames_photos_folder_when_flickr_collection_changed(self):
        from unittest.mock import patch
        from flickr.sync_names_from_flickr import sync_names_from_flickr
        fid = self._seed_folder("uuid-f1", "Old Folder", collection_id="col-1", flickr_name="Old Folder")
        flickr = self._make_flickr(collections={"col-1": "New Folder Name"})

        with patch("flickr.sync_names_from_flickr._rename_photos_folder", return_value=True) as mock_rename:
            result = sync_names_from_flickr(self.db, flickr)

        mock_rename.assert_called_once_with("uuid-f1", "New Folder Name")
        row = self.db.conn.execute("SELECT name, flickr_name FROM folders WHERE id = ?", (fid,)).fetchone()
        self.assertEqual(row["name"], "New Folder Name")
        self.assertEqual(row["flickr_name"], "New Folder Name")
        self.assertEqual(result["folders_renamed"], 1)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/test_core.py -k "TestSyncNamesFromFlickr" -v
```

Expected: FAIL — `sync_names_from_flickr` module not found.

- [ ] **Step 3: Create flickr/sync_names_from_flickr.py**

```python
"""
flickr/sync_names_from_flickr.py — sync Flickr photoset/Collection renames → Apple Photos

Usage:
    python flickr/sync_names_from_flickr.py --config config/config.yml [--dry-run]

Or via bp CLI:
    bp sync-names-from-flickr [--dry-run]

When a Flickr photoset or Collection is renamed directly on Flickr, this command detects
the change and renames the corresponding Apple Photos album or folder via AppleScript.
Requires Photos.app to be running. Photos wins on conflict (both sides renamed).
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

log = logging.getLogger("blue-pearmain.sync_names_from_flickr")


def _rename_photos_album(apple_uuid: str, new_name: str) -> bool:
    """Rename an Apple Photos album via AppleScript. Requires Photos.app to be running."""
    safe = new_name.replace("\\", "\\\\").replace('"', '\\"')
    script = f'tell application "Photos" to set name of album id "{apple_uuid}" to "{safe}"'
    r = subprocess.run(["osascript", "-e", script],
                       capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        log.warning("Photos rename failed for album %r: %s", new_name, r.stderr.strip())
    return r.returncode == 0


def _rename_photos_folder(apple_uuid: str, new_name: str) -> bool:
    """Rename an Apple Photos folder via AppleScript. Requires Photos.app to be running."""
    safe = new_name.replace("\\", "\\\\").replace('"', '\\"')
    script = f'tell application "Photos" to set name of folder id "{apple_uuid}" to "{safe}"'
    r = subprocess.run(["osascript", "-e", script],
                       capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        log.warning("Photos rename failed for folder %r: %s", new_name, r.stderr.strip())
    return r.returncode == 0


def sync_names_from_flickr(db, flickr, dry_run: bool = False) -> dict:
    """
    Detect Flickr photoset/Collection renames and propagate them to Apple Photos.
    Returns {"albums_renamed": N, "albums_skipped": N, "folders_renamed": N, "folders_skipped": N}.
    """
    set_map = flickr.get_photosets_titled()

    try:
        col_map = flickr.get_collections_flat()
    except Exception as e:
        if "pro" in str(e).lower():
            log.info("sync-names-from-flickr: Flickr Collections require Pro — skipping folders")
        else:
            log.warning("sync-names-from-flickr: could not fetch collections: %s", e)
        col_map = {}

    albums_renamed = 0
    albums_skipped = 0

    album_rows = db.conn.execute(
        "SELECT id, apple_uuid, name, flickr_set_id, flickr_name "
        "FROM albums WHERE flickr_set_id IS NOT NULL"
    ).fetchall()

    for row in album_rows:
        flickr_title = set_map.get(row["flickr_set_id"])
        if flickr_title is None:
            log.debug("photoset %s not in Flickr list — skipping", row["flickr_set_id"])
            albums_skipped += 1
            continue

        baseline = row["flickr_name"]
        if baseline is None:
            log.debug("album %r has no flickr_name baseline — skipping", row["name"])
            albums_skipped += 1
            continue

        photos_name   = row["name"]
        photos_changed = photos_name != baseline
        flickr_changed = flickr_title != baseline

        if not flickr_changed:
            albums_skipped += 1
            continue

        if photos_changed:
            log.info(
                "conflict: album %r renamed on both sides (Photos=%r, Flickr=%r) — Photos wins",
                baseline, photos_name, flickr_title,
            )
            albums_skipped += 1
            continue

        # Only Flickr was renamed
        log.info(
            "%salbumy %r → %r (Flickr-side rename)",
            "[dry-run] would rename " if dry_run else "renaming ",
            photos_name, flickr_title,
        )
        if dry_run:
            albums_renamed += 1
            continue

        if _rename_photos_album(row["apple_uuid"], flickr_title):
            db.conn.execute(
                "UPDATE albums SET name = ?, flickr_name = ?, updated_at = ? WHERE id = ?",
                (flickr_title, flickr_title, _now_iso(), row["id"]),
            )
            db.conn.commit()
            albums_renamed += 1
        else:
            albums_skipped += 1

    folders_renamed = 0
    folders_skipped = 0

    if col_map:
        folder_rows = db.conn.execute(
            "SELECT id, apple_uuid, name, flickr_collection_id, flickr_name "
            "FROM folders WHERE flickr_collection_id IS NOT NULL"
        ).fetchall()

        for row in folder_rows:
            flickr_title = col_map.get(row["flickr_collection_id"])
            if flickr_title is None:
                folders_skipped += 1
                continue

            baseline = row["flickr_name"]
            if baseline is None:
                folders_skipped += 1
                continue

            photos_name    = row["name"]
            photos_changed = photos_name != baseline
            flickr_changed = flickr_title != baseline

            if not flickr_changed:
                folders_skipped += 1
                continue

            if photos_changed:
                log.info(
                    "conflict: folder %r renamed on both sides (Photos=%r, Flickr=%r) — Photos wins",
                    baseline, photos_name, flickr_title,
                )
                folders_skipped += 1
                continue

            log.info(
                "%sfolder %r → %r (Flickr-side rename)",
                "[dry-run] would rename " if dry_run else "renaming ",
                photos_name, flickr_title,
            )
            if dry_run:
                folders_renamed += 1
                continue

            if _rename_photos_folder(row["apple_uuid"], flickr_title):
                db.conn.execute(
                    "UPDATE folders SET name = ?, flickr_name = ?, updated_at = ? WHERE id = ?",
                    (flickr_title, flickr_title, _now_iso(), row["id"]),
                )
                db.conn.commit()
                folders_renamed += 1
            else:
                folders_skipped += 1

    log.info(
        "sync-names-from-flickr done — albums renamed=%d skipped=%d  folders renamed=%d skipped=%d",
        albums_renamed, albums_skipped, folders_renamed, folders_skipped,
    )
    return {
        "albums_renamed":  albums_renamed,
        "albums_skipped":  albums_skipped,
        "folders_renamed": folders_renamed,
        "folders_skipped": folders_skipped,
    }


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sync Flickr photoset/Collection renames → Apple Photos"
    )
    parser.add_argument("--config",  default="config/config.yml")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    try:
        with open(args.config) as f:
            config = yaml.safe_load(f)
    except Exception as e:
        log.error("Cannot read config: %s", e)
        return 2

    try:
        from db.db import Database
        db = Database(Path(config["database"]["path"]).expanduser())
    except Exception as e:
        log.error("Cannot open database: %s", e)
        return 2

    try:
        from flickr.flickr_client import FlickrClient
        flickr = FlickrClient.from_config(config)
    except Exception as e:
        log.error("Cannot initialise Flickr client: %s", e)
        return 2

    sync_names_from_flickr(db, flickr, dry_run=args.dry_run)
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/test_core.py -k "TestSyncNamesFromFlickr" -v
```

Expected: 7 PASS.

- [ ] **Step 5: Run full suite**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/ -q
```

Expected: 486 + 7 = 493 passed.

- [ ] **Step 6: Commit**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && git add flickr/sync_names_from_flickr.py tests/test_core.py
git commit -m "feat: sync_names_from_flickr — detect Flickr renames, propagate to Photos

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 5: Wire into bp CLI + bp all

**Files:**
- Modify: `bp`
- Test: `tests/test_core.py`

### Context

The `bp all` step list (in `cmd_all`) must insert `sync-names-from-flickr` between `poll` and `pipeline`. Read the current step list in `bp` before editing.

The step list currently looks like:
```python
steps = [
    ("scan --all",      cmd_scan,        _step_args(args, all=True, dry_run=dry_run, days=None)),
    ("poll",            cmd_poll,        _step_args(args, backfill=False, dry_run=dry_run, days=None)),
    ("pipeline",        cmd_pipeline,    _step_args(args, limit=None, dry_run=dry_run)),
    ("reconcile",       cmd_reconcile,   _step_args(args, fix=not dry_run, apply_proposals=False, limit=None)),
    ("sync-albums",            cmd_sync_albums,            _step_args(args, dry_run=dry_run, album=None, limit=None)),
    ("sync-album-collections", cmd_sync_album_collections, _step_args(args, dry_run=dry_run, remove=False, force=False)),
]
```

Insert `sync-names-from-flickr` AFTER `poll` and BEFORE `pipeline` (index 2):

```python
    ("sync-names-from-flickr", cmd_sync_names_from_flickr, _step_args(args, dry_run=dry_run)),
```

- [ ] **Step 1: Write the failing test**

Find `class TestCmdAll` in `tests/test_core.py`. The test patches each step's function. Add `cmd_sync_names_from_flickr` to the `_patch_steps` list and verify the step count increments.

Read the existing `TestCmdAll` to find the exact pattern, then add `"bp.cmd_sync_names_from_flickr"` to the patch list and update the expected step count.

The current test in `TestCmdAll` (based on prior work) patches 8 steps and asserts they all ran. After adding `sync-names-from-flickr`, the expected count should be 9 steps (8 + 1 new).

Add a targeted test for the new subcommand wiring:

```python
def test_sync_names_from_flickr_subcommand_exists(self):
    """bp sync-names-from-flickr subparser must be registered."""
    import subprocess, sys
    result = subprocess.run(
        [sys.executable, "bp", "sync-names-from-flickr", "--help"],
        capture_output=True, text=True,
        cwd="/Users/cdevers/Documents/GitHub/Blue Pearmain",
    )
    self.assertEqual(result.returncode, 0)
    self.assertIn("sync-names-from-flickr", result.stdout + result.stderr)
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/test_core.py -k "test_sync_names_from_flickr_subcommand_exists" -v
```

Expected: FAIL — subcommand not registered.

- [ ] **Step 3: Add the subparser and cmd function to bp**

In `bp`, add a new function `cmd_sync_names_from_flickr`:

```python
def cmd_sync_names_from_flickr(args):
    from flickr.sync_names_from_flickr import main
    _run(main, args, [
        ("--config",  args.config),
        ("--dry-run", args.dry_run),
        ("--verbose", args.verbose),
    ])
```

Add the subparser (in the `main()` function, near the other subparsers):

```python
p_sync_names = sub.add_parser("sync-names-from-flickr",
    help="Sync Flickr photoset/Collection renames → Apple Photos albums/folders")
p_sync_names.add_argument("--dry-run", action="store_true")
p_sync_names.add_argument("--verbose", action="store_true")
```

Add to the dispatch table:
```python
"sync-names-from-flickr": cmd_sync_names_from_flickr,
```

Update `cmd_all` step list to insert the new step after `poll`:
```python
    ("sync-names-from-flickr", cmd_sync_names_from_flickr, _step_args(args, dry_run=dry_run)),
```

Update the usage docstring at the top of `bp` to include the new command:
```
    bp sync-names-from-flickr [--dry-run]   Sync Flickr renames → Apple Photos
```

- [ ] **Step 4: Update TestCmdAll**

Find the existing `TestCmdAll` test class and update:
- Add `"bp.cmd_sync_names_from_flickr"` to the `_patch_steps` list
- Update the expected step count from 8 to 9

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/test_core.py -k "TestCmdAll or test_sync_names_from_flickr_subcommand" -v
```

Expected: all PASS.

- [ ] **Step 6: Run full suite**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/ -q
```

Expected: 493 + 2 = 495 passed.

- [ ] **Step 7: Commit**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && git add bp tests/test_core.py
git commit -m "feat: wire sync-names-from-flickr into bp CLI and bp all pipeline

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 6: README, version bump, tag, close issue

**Files:**
- Modify: `README.md`
- Modify: `pyproject.toml`
- Modify: `uv.lock`

- [ ] **Step 1: Update README.md**

Add a new command entry for `bp sync-names-from-flickr`. It belongs near `bp sync-albums` and `bp sync-album-collections`. Describe it briefly:

```
bp sync-names-from-flickr        # Sync Flickr photoset/Collection renames → Apple Photos
bp sync-names-from-flickr --dry-run  # Preview renames without applying
```

Also update:
- The `bp all` description to mention that it includes the new step
- Test count to the actual passing total from the Task 5 full-suite run

- [ ] **Step 2: Bump version**

In `pyproject.toml`, change:
```toml
version = "0.5.0"
```
to:
```toml
version = "0.6.0"
```

- [ ] **Step 3: Regenerate lock file**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && uv lock
```

- [ ] **Step 4: Run full suite one final time**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/ -q
```

Expected: 495 passed (or whatever the count was after Task 5).

- [ ] **Step 5: Apply migration to live DB**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python db/migrations/migrate_012_flickr_name.py --config config/config.yml
```

Expected: `Applied: migrate_012_flickr_name`

- [ ] **Step 6: Commit and tag**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && git add README.md pyproject.toml uv.lock
git commit -m "Bump version to 0.6.0 — Phase B: sync Flickr renames → Apple Photos (closes #52)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
git tag v0.6.0
```

- [ ] **Step 7: Close GH issue**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && gh issue close 52 --comment "Implemented in v0.6.0. New command \`bp sync-names-from-flickr\` detects Flickr-side renames and propagates them to Apple Photos via AppleScript. Runs as part of \`bp all\` between poll and sync-albums. Photos wins on conflict. Requires Photos.app to be running."
```
