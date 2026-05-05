# Sync Album Collections Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Mirror Apple Photos folder hierarchy as Flickr Collections so that Folder > Album in Photos becomes Collection > Photoset on Flickr, with full recursive nesting support.

**Architecture:** Migration 011 adds a self-referential `folders` table and a `folder_id` FK on `albums`. The scanner populates folders during `bp scan`. A new `flickr/sync_collections.py` command reads the folder tree from the DB, creates/updates Flickr Collections in topological order (parents first), and links photosets into them. The `bp` CLI gains a `sync-album-collections` subcommand that slots into `bp all` after `sync-albums`.

**Tech Stack:** Python 3.11, SQLite (via `db/db.py`), Flickr REST API (`flickr.collections.*`), osxphotos (for `--remove` orphan detection), argparse, pytest.

---

## File map

| File | Change |
|------|--------|
| `db/migrations/migrate_011_folders.py` | CREATE: migration adding `folders` table + `albums.folder_id` |
| `db/schema.sql` | MODIFY: add `folders` table + `albums.folder_id` column |
| `db/db.py` | MODIFY: `upsert_folder()`, `upsert_album()` gains `folder_id`, `get_all_folders()`, `set_folder_flickr_collection_id()`, `clear_folder_flickr_collection_id()` |
| `poller/scanner.py` | MODIFY: walk folder ancestry in `sync_photo_albums()` |
| `flickr/flickr_client.py` | MODIFY: `create_collection()`, `edit_collection_sets()`, `delete_collection()` |
| `flickr/sync_collections.py` | CREATE: `bp sync-album-collections` command |
| `bp` | MODIFY: `sync-album-collections` subparser, add to `cmd_all`, update usage docstring |
| `docs/pipeline.md` | MODIFY: add stage 7, renumber checkpoint to 8 |
| `README.md` | MODIFY: document new command |

---

## Task 1: Migration 011 — folders table + albums.folder_id

**Files:**
- Create: `db/migrations/migrate_011_folders.py`
- Modify: `db/schema.sql`
- Test: `tests/test_core.py` (add to existing migration test section near line 5508)

- [ ] **Step 1: Write the failing tests**

Find the block of migration tests near line 5508 in `tests/test_core.py` and add after `TestMigration010`:

```python
class TestMigration011(unittest.TestCase):

    def _run_migration(self, db_path: str):
        import importlib.util, sys
        spec = importlib.util.spec_from_file_location(
            "migrate_011",
            Path(__file__).parent.parent / "db/migrations/migrate_011_folders.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.run(db_path)

    def test_migration_011_creates_folders_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "test.db")
            from db.db import Database
            db = Database(db_path)
            db.close()
            self._run_migration(db_path)
            conn = sqlite3.connect(db_path)
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            self.assertIn("folders", tables)
            conn.close()

    def test_migration_011_folders_has_parent_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "test.db")
            from db.db import Database
            db = Database(db_path)
            db.close()
            self._run_migration(db_path)
            conn = sqlite3.connect(db_path)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(folders)").fetchall()}
            self.assertIn("parent_id", cols)
            self.assertIn("flickr_collection_id", cols)
            conn.close()

    def test_migration_011_albums_has_folder_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "test.db")
            from db.db import Database
            db = Database(db_path)
            db.close()
            self._run_migration(db_path)
            conn = sqlite3.connect(db_path)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(albums)").fetchall()}
            self.assertIn("folder_id", cols)
            conn.close()

    def test_migration_011_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "idempotent.db")
            from db.db import Database
            db = Database(db_path)
            db.close()
            self._run_migration(db_path)
            self._run_migration(db_path)  # second run must not raise
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_core.py -k "Migration011" -v
```
Expected: FAIL with `ModuleNotFoundError` or file-not-found.

- [ ] **Step 3: Write the migration**

Create `db/migrations/migrate_011_folders.py`:

```python
"""
migrate_011_folders.py

Adds:
  1. folders table — self-referential (parent_id → folders.id), tracks
     Apple Photos folder hierarchy and corresponding Flickr Collection IDs.
  2. albums.folder_id — nullable FK to folders.id (ON DELETE SET NULL).

Safe to run multiple times (idempotent — checks schema_migrations).

Usage:
    python db/migrations/migrate_011_folders.py --config config/config.yml
"""

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import yaml


MIGRATION_NAME = "migrate_011_folders"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run(db_path: str) -> None:
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

    # 1. Create folders table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS folders (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            apple_uuid           TEXT NOT NULL UNIQUE,
            name                 TEXT NOT NULL,
            parent_id            INTEGER REFERENCES folders(id) ON DELETE SET NULL,
            flickr_collection_id TEXT,
            created_at           TEXT,
            updated_at           TEXT
        )
    """)

    # 2. Add folder_id to albums (idempotent: only if column absent)
    existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(albums)").fetchall()}
    if "folder_id" not in existing_cols:
        conn.execute(
            "ALTER TABLE albums ADD COLUMN folder_id INTEGER REFERENCES folders(id) ON DELETE SET NULL"
        )

    # 3. Record migration
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (name, applied_at) VALUES (?, ?)",
        (MIGRATION_NAME, now_iso()),
    )
    conn.commit()
    conn.close()
    print("  Applied:  folders table created; albums.folder_id added")


def main():
    parser = argparse.ArgumentParser(description="Blue Pearmain DB migration 011")
    parser.add_argument("--config", default="config/config.yml")
    args = parser.parse_args()

    with open(args.config) as f:
        config = yaml.safe_load(f)

    db_path = str(Path(config["database"]["path"]).expanduser())
    print(f"Database: {db_path}")
    run(db_path)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Update schema.sql**

Add the `folders` table after the albums table definition (around line 202), and add `folder_id` to the `albums` CREATE TABLE:

In `db/schema.sql`, change the albums table to:
```sql
CREATE TABLE IF NOT EXISTS albums (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    apple_uuid      TEXT NOT NULL UNIQUE,   -- Photos album UUID
    name            TEXT NOT NULL,
    folder_id       INTEGER REFERENCES folders(id) ON DELETE SET NULL,
    flickr_set_id   TEXT,                   -- NULL until created on Flickr
    flickr_set_url  TEXT,
    created_at      TEXT,
    updated_at      TEXT
);
```

And add before the albums table:
```sql
-- Folders: Apple Photos folder hierarchy mirrored as Flickr Collections
-- ============================================================

CREATE TABLE IF NOT EXISTS folders (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    apple_uuid           TEXT NOT NULL UNIQUE,
    name                 TEXT NOT NULL,
    parent_id            INTEGER REFERENCES folders(id) ON DELETE SET NULL,
    flickr_collection_id TEXT,
    created_at           TEXT,
    updated_at           TEXT
);
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
python -m pytest tests/test_core.py -k "Migration011" -v
```
Expected: 4 PASS.

- [ ] **Step 6: Run full suite**

```bash
python -m pytest tests/ -q
```
Expected: all existing tests pass (431+4 = 435).

- [ ] **Step 7: Commit**

```bash
git add db/migrations/migrate_011_folders.py db/schema.sql tests/test_core.py
git commit -m "feat: migration 011 — folders table and albums.folder_id"
```

---

## Task 2: DB methods for folders

**Files:**
- Modify: `db/db.py` (after `set_album_flickr_set_id`, around line 691)
- Test: `tests/test_core.py` (add `TestFolderDB` class after `TestAlbumDB` around line 2046)

- [ ] **Step 1: Write the failing tests**

Add after `TestAlbumDB` in `tests/test_core.py`:

```python
class TestFolderDB(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = _make_db(self._tmp.name)

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_upsert_folder_creates_and_returns_id(self):
        fid = self.db.upsert_folder("uuid-f1", "Travel")
        self.assertIsInstance(fid, int)
        self.assertGreater(fid, 0)

    def test_upsert_folder_idempotent(self):
        fid1 = self.db.upsert_folder("uuid-f1", "Travel")
        fid2 = self.db.upsert_folder("uuid-f1", "Travel")
        self.assertEqual(fid1, fid2)

    def test_upsert_folder_updates_name(self):
        fid = self.db.upsert_folder("uuid-f1", "Old Name")
        self.db.upsert_folder("uuid-f1", "New Name")
        row = self.db.conn.execute("SELECT name FROM folders WHERE id=?", (fid,)).fetchone()
        self.assertEqual(row["name"], "New Name")

    def test_upsert_folder_with_parent(self):
        parent_id = self.db.upsert_folder("uuid-parent", "Europe")
        child_id  = self.db.upsert_folder("uuid-child", "France", parent_id=parent_id)
        row = self.db.conn.execute("SELECT parent_id FROM folders WHERE id=?", (child_id,)).fetchone()
        self.assertEqual(row["parent_id"], parent_id)

    def test_upsert_album_accepts_folder_id(self):
        fid = self.db.upsert_folder("uuid-f1", "Travel")
        aid = self.db.upsert_album("uuid-a1", "Paris Trip", folder_id=fid)
        row = self.db.conn.execute("SELECT folder_id FROM albums WHERE id=?", (aid,)).fetchone()
        self.assertEqual(row["folder_id"], fid)

    def test_upsert_album_folder_id_defaults_none(self):
        aid = self.db.upsert_album("uuid-a1", "Standalone Album")
        row = self.db.conn.execute("SELECT folder_id FROM albums WHERE id=?", (aid,)).fetchone()
        self.assertIsNone(row["folder_id"])

    def test_get_all_folders_returns_rows(self):
        self.db.upsert_folder("uuid-f1", "Travel")
        self.db.upsert_folder("uuid-f2", "Work")
        folders = self.db.get_all_folders()
        self.assertEqual(len(folders), 2)
        names = {f["name"] for f in folders}
        self.assertEqual(names, {"Travel", "Work"})

    def test_get_all_folders_empty(self):
        self.assertEqual(self.db.get_all_folders(), [])

    def test_set_folder_flickr_collection_id(self):
        fid = self.db.upsert_folder("uuid-f1", "Travel")
        self.db.set_folder_flickr_collection_id(fid, "col-123")
        row = self.db.conn.execute(
            "SELECT flickr_collection_id FROM folders WHERE id=?", (fid,)
        ).fetchone()
        self.assertEqual(row["flickr_collection_id"], "col-123")

    def test_clear_folder_flickr_collection_id(self):
        fid = self.db.upsert_folder("uuid-f1", "Travel")
        self.db.set_folder_flickr_collection_id(fid, "col-123")
        self.db.clear_folder_flickr_collection_id(fid)
        row = self.db.conn.execute(
            "SELECT flickr_collection_id FROM folders WHERE id=?", (fid,)
        ).fetchone()
        self.assertIsNone(row["flickr_collection_id"])
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_core.py -k "TestFolderDB" -v
```
Expected: FAIL with `AttributeError: 'Database' object has no attribute 'upsert_folder'`.

- [ ] **Step 3: Implement the DB methods**

In `db/db.py`, add after `set_album_flickr_set_id` (around line 691):

```python
    # ------------------------------------------------------------------
    # Folder methods
    # ------------------------------------------------------------------

    def upsert_folder(self, apple_uuid: str, name: str, parent_id: int | None = None) -> int:
        """Insert or update a folder record. Returns the folder row id."""
        self.conn.execute(
            "INSERT OR IGNORE INTO folders (apple_uuid, name, parent_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (apple_uuid, name, parent_id, _now_iso(), _now_iso()),
        )
        self.conn.execute(
            "UPDATE folders SET name = ?, parent_id = ?, updated_at = ? WHERE apple_uuid = ?",
            (name, parent_id, _now_iso(), apple_uuid),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT id FROM folders WHERE apple_uuid = ?", (apple_uuid,)
        ).fetchone()
        return row["id"]

    def get_all_folders(self) -> list[dict]:
        """Return all folder rows."""
        rows = self.conn.execute(
            "SELECT id, apple_uuid, name, parent_id, flickr_collection_id FROM folders"
        ).fetchall()
        return [dict(r) for r in rows]

    def set_folder_flickr_collection_id(self, folder_id: int, collection_id: str) -> None:
        """Store the Flickr Collection ID after creating a collection."""
        self.conn.execute(
            "UPDATE folders SET flickr_collection_id = ?, updated_at = ? WHERE id = ?",
            (collection_id, _now_iso(), folder_id),
        )
        self.conn.commit()

    def clear_folder_flickr_collection_id(self, folder_id: int) -> None:
        """Clear a stale Flickr Collection ID (e.g. collection deleted externally)."""
        self.conn.execute(
            "UPDATE folders SET flickr_collection_id = NULL, updated_at = ? WHERE id = ?",
            (_now_iso(), folder_id),
        )
        self.conn.commit()
```

Also update `upsert_album` to accept an optional `folder_id` parameter. Change the signature and body:

```python
    def upsert_album(self, apple_uuid: str, name: str, folder_id: int | None = None) -> int:
        """Insert or update an album record. Returns the album row id."""
        self.conn.execute(
            "INSERT OR IGNORE INTO albums (apple_uuid, name, folder_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (apple_uuid, name, folder_id, _now_iso(), _now_iso()),
        )
        self.conn.execute(
            "UPDATE albums SET name = ?, folder_id = ?, updated_at = ? WHERE apple_uuid = ?",
            (name, folder_id, _now_iso(), apple_uuid),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT id FROM albums WHERE apple_uuid = ?", (apple_uuid,)
        ).fetchone()
        return row["id"]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_core.py -k "TestFolderDB" -v
```
Expected: 11 PASS.

- [ ] **Step 5: Run full suite**

```bash
python -m pytest tests/ -q
```
Expected: all pass (existing album tests still pass since `folder_id` defaults to `None`).

- [ ] **Step 6: Commit**

```bash
git add db/db.py tests/test_core.py
git commit -m "feat: DB methods for folders (upsert_folder, get_all_folders, set/clear collection id)"
```

---

## Task 3: Scanner — walk folder ancestry in sync_photo_albums

**Files:**
- Modify: `poller/scanner.py` (`sync_photo_albums` function, around line 197)
- Test: `tests/test_core.py` (extend `TestSyncPhotoAlbums` class, around line 1866)

- [ ] **Step 1: Write the failing tests**

First, update `_make_album_info` in `TestSyncPhotoAlbums` to accept an optional `parent` parameter:

```python
    def _make_album_info(self, title, uuid, album_type=None, parent=None):
        """Return a simple namespace mimicking an osxphotos AlbumInfo object."""
        from types import SimpleNamespace
        obj = SimpleNamespace(title=title, uuid=uuid, parent=parent)
        if album_type is not None:
            obj.album_type = album_type
        return obj

    def _make_folder_info(self, title, uuid, parent=None):
        """Return a simple namespace mimicking an osxphotos FolderInfo object."""
        from types import SimpleNamespace
        return SimpleNamespace(title=title, uuid=uuid, parent=parent)
```

Then add these tests to `TestSyncPhotoAlbums`:

```python
    def test_album_with_folder_creates_folder_row(self):
        """An album inside a Photos folder must create a folders row and set folder_id."""
        from poller.scanner import sync_photo_albums
        from types import SimpleNamespace

        folder = self._make_folder_info("Travel", "folder-uuid-1")
        album  = self._make_album_info("Paris Trip", "album-uuid-1", parent=folder)
        photo  = SimpleNamespace(album_info=[album])

        sync_photo_albums(photo, self.photo_id, self.db, dry_run=False)

        folder_row = self.db.conn.execute("SELECT * FROM folders WHERE apple_uuid='folder-uuid-1'").fetchone()
        self.assertIsNotNone(folder_row)
        self.assertEqual(folder_row["name"], "Travel")

        album_row = self.db.conn.execute("SELECT folder_id FROM albums WHERE apple_uuid='album-uuid-1'").fetchone()
        self.assertEqual(album_row["folder_id"], folder_row["id"])

    def test_album_with_nested_folders_creates_all_rows(self):
        """Grandparent → parent → album must create two folder rows, with correct parent_id."""
        from poller.scanner import sync_photo_albums
        from types import SimpleNamespace

        grandparent = self._make_folder_info("Europe", "uuid-gp")
        parent      = self._make_folder_info("France", "uuid-p", parent=grandparent)
        album       = self._make_album_info("Paris", "uuid-album", parent=parent)
        photo       = SimpleNamespace(album_info=[album])

        sync_photo_albums(photo, self.photo_id, self.db, dry_run=False)

        gp_row = self.db.conn.execute("SELECT id, parent_id FROM folders WHERE apple_uuid='uuid-gp'").fetchone()
        p_row  = self.db.conn.execute("SELECT id, parent_id FROM folders WHERE apple_uuid='uuid-p'").fetchone()
        self.assertIsNone(gp_row["parent_id"])
        self.assertEqual(p_row["parent_id"], gp_row["id"])

    def test_album_without_folder_has_null_folder_id(self):
        """Albums with no parent folder must still work; folder_id should be NULL."""
        from poller.scanner import sync_photo_albums
        from types import SimpleNamespace

        album = self._make_album_info("No Folder Album", "uuid-nf")  # parent=None by default
        photo = SimpleNamespace(album_info=[album])

        sync_photo_albums(photo, self.photo_id, self.db, dry_run=False)

        row = self.db.conn.execute("SELECT folder_id FROM albums WHERE apple_uuid='uuid-nf'").fetchone()
        self.assertIsNone(row["folder_id"])

    def test_shared_folder_deduplicated_across_albums(self):
        """Two albums in the same folder must produce only one folder row."""
        from poller.scanner import sync_photo_albums
        from types import SimpleNamespace

        folder = self._make_folder_info("Travel", "folder-uuid-shared")
        album1 = self._make_album_info("Paris", "uuid-album-1", parent=folder)
        album2 = self._make_album_info("Rome",  "uuid-album-2", parent=folder)
        photo  = SimpleNamespace(album_info=[album1, album2])

        sync_photo_albums(photo, self.photo_id, self.db, dry_run=False)

        count = self.db.conn.execute("SELECT COUNT(*) AS n FROM folders").fetchone()["n"]
        self.assertEqual(count, 1, "same folder via two albums must produce only one row")

    def test_dry_run_does_not_write_folders(self):
        """dry_run=True must not insert any folder rows."""
        from poller.scanner import sync_photo_albums
        from types import SimpleNamespace

        folder = self._make_folder_info("Travel", "folder-uuid-dry")
        album  = self._make_album_info("Paris", "uuid-album-dry", parent=folder)
        photo  = SimpleNamespace(album_info=[album])

        sync_photo_albums(photo, self.photo_id, self.db, dry_run=True)

        count = self.db.conn.execute("SELECT COUNT(*) AS n FROM folders").fetchone()["n"]
        self.assertEqual(count, 0, "dry_run must not write folders")
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_core.py -k "folder" -v
```
Expected: FAIL — `sync_photo_albums` doesn't touch folders yet.

- [ ] **Step 3: Implement the scanner change**

Replace `sync_photo_albums` in `poller/scanner.py`:

```python
def sync_photo_albums(photo, photo_db_id: int, db: Database, dry_run: bool) -> None:
    """
    Upsert album membership rows for one osxphotos PhotoInfo object.
    Also upserts folder ancestry from album.parent, root-first.

    Uses photo.album_info (list of AlbumInfo objects with .title and .uuid).
    photo.albums returns plain strings and must not be used here.

    Filters to user-created albums only when album_type is available (osxphotos
    >= some future version); when the attribute is absent (osxphotos 0.75.x),
    album_info already excludes smart/system albums so all entries are accepted.
    """
    album_infos = getattr(photo, "album_info", []) or []
    seen_folder_uuids: set[str] = set()

    for album in album_infos:
        album_type = getattr(album, "album_type", "Album")
        if album_type != "Album":
            continue

        if dry_run:
            log.debug("  [dry-run] album: %r (%s)", album.title, album.uuid)
            continue

        # Walk folder ancestry from root to immediate parent.
        ancestors: list = []
        node = getattr(album, "parent", None)
        while node is not None:
            ancestors.append(node)
            node = getattr(node, "parent", None)
        ancestors.reverse()  # root first

        parent_db_id: int | None = None
        for folder in ancestors:
            if folder.uuid not in seen_folder_uuids:
                db.upsert_folder(folder.uuid, folder.title, parent_id=parent_db_id)
                seen_folder_uuids.add(folder.uuid)
            row = db.conn.execute(
                "SELECT id FROM folders WHERE apple_uuid = ?", (folder.uuid,)
            ).fetchone()
            parent_db_id = row["id"]

        album_id = db.upsert_album(album.uuid, album.title, folder_id=parent_db_id)
        db.upsert_photo_album(photo_db_id, album_id)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_core.py -k "TestSyncPhotoAlbums" -v
```
Expected: all PASS (both old and new tests).

- [ ] **Step 5: Run full suite**

```bash
python -m pytest tests/ -q
```
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add poller/scanner.py tests/test_core.py
git commit -m "feat: scanner walks folder ancestry and populates folders table"
```

---

## Task 4: Flickr client — Collections API methods

**Files:**
- Modify: `flickr/flickr_client.py` (after `add_photo_to_photoset`, around line 362)
- Test: `tests/test_core.py` (add `TestFlickrCollectionsClient` near other Flickr client tests)

- [ ] **Step 1: Write the failing tests**

Find the Flickr client test section in `tests/test_core.py` (search for `class TestFlickrClient` or `TestFlickrRetry`) and add:

```python
class TestFlickrCollectionsClient(unittest.TestCase):
    """FlickrClient Collections API methods call the correct Flickr endpoints."""

    def _make_client(self):
        from flickr.flickr_client import FlickrClient
        c = FlickrClient.__new__(FlickrClient)
        c._rate_delay = 0
        c.user_nsid = "me"
        return c

    def test_create_collection_calls_correct_method(self):
        from unittest.mock import patch
        client = self._make_client()
        with patch.object(client, "_call", return_value={"collection": {"id": "col-999"}}) as mock_call:
            result = client.create_collection("My Folder")
        mock_call.assert_called_once_with(
            "flickr.collections.create",
            {"title": "My Folder", "description": ""},
            http_method="POST",
        )
        self.assertEqual(result, "col-999")

    def test_create_collection_passes_description(self):
        from unittest.mock import patch
        client = self._make_client()
        with patch.object(client, "_call", return_value={"collection": {"id": "col-42"}}) as mock_call:
            client.create_collection("Folder", description="desc")
        _, kwargs = mock_call.call_args
        self.assertEqual(mock_call.call_args[0][1]["description"], "desc")

    def test_edit_collection_sets_calls_correct_method(self):
        from unittest.mock import patch
        client = self._make_client()
        with patch.object(client, "_call", return_value={}) as mock_call:
            client.edit_collection_sets("col-1", ["ps-1", "ps-2"], ["col-2"])
        mock_call.assert_called_once_with(
            "flickr.collections.editSets",
            {
                "collection_id": "col-1",
                "photoset_ids":  "ps-1 ps-2",
                "collection_ids": "col-2",
            },
            http_method="POST",
        )

    def test_edit_collection_sets_empty_lists(self):
        from unittest.mock import patch
        client = self._make_client()
        with patch.object(client, "_call", return_value={}) as mock_call:
            client.edit_collection_sets("col-1", [], [])
        call_params = mock_call.call_args[0][1]
        self.assertEqual(call_params["photoset_ids"], "")
        self.assertEqual(call_params["collection_ids"], "")

    def test_delete_collection_calls_correct_method(self):
        from unittest.mock import patch
        client = self._make_client()
        with patch.object(client, "_call", return_value={}) as mock_call:
            client.delete_collection("col-99")
        mock_call.assert_called_once_with(
            "flickr.collections.delete",
            {"collection_id": "col-99"},
            http_method="POST",
        )
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_core.py -k "TestFlickrCollectionsClient" -v
```
Expected: FAIL with `AttributeError: 'FlickrClient' object has no attribute 'create_collection'`.

- [ ] **Step 3: Implement the Flickr client methods**

In `flickr/flickr_client.py`, add after `add_photo_to_photoset` (around line 362):

```python
    # -----------------------------------------------------------------------
    # Collections (Flickr Pro only)
    # -----------------------------------------------------------------------

    def create_collection(self, title: str, description: str = "") -> str:
        """Create a Flickr Collection. Returns the collection_id string."""
        data = self._call(
            "flickr.collections.create",
            {"title": title, "description": description},
            http_method="POST",
        )
        return data["collection"]["id"]

    def edit_collection_sets(
        self,
        collection_id: str,
        photoset_ids: list[str],
        sub_collection_ids: list[str],
    ) -> None:
        """Full replace of a collection's photosets and sub-collections.
        Flickr's editSets is a complete overwrite, not additive."""
        self._call(
            "flickr.collections.editSets",
            {
                "collection_id":  collection_id,
                "photoset_ids":   " ".join(photoset_ids),
                "collection_ids": " ".join(sub_collection_ids),
            },
            http_method="POST",
        )

    def delete_collection(self, collection_id: str) -> None:
        """Delete a Flickr Collection."""
        self._call(
            "flickr.collections.delete",
            {"collection_id": collection_id},
            http_method="POST",
        )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_core.py -k "TestFlickrCollectionsClient" -v
```
Expected: 5 PASS.

- [ ] **Step 5: Run full suite**

```bash
python -m pytest tests/ -q
```
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add flickr/flickr_client.py tests/test_core.py
git commit -m "feat: Flickr client — create_collection, edit_collection_sets, delete_collection"
```

---

## Task 5: sync_collections.py — normal sync (with dry-run)

**Files:**
- Create: `flickr/sync_collections.py`
- Test: `tests/test_core.py` (add `TestSyncCollections` class)

- [ ] **Step 1: Write the failing tests**

Add `TestSyncCollections` to `tests/test_core.py`:

```python
class TestSyncCollections(unittest.TestCase):
    """sync_collections: creates/updates Flickr Collections from DB folder tree."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        from db.db import Database
        self.db = Database(Path(self._tmp.name) / "test.db")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _make_flickr(self, **side_effects):
        from unittest.mock import MagicMock
        m = MagicMock()
        m.create_collection.return_value = "col-new"
        m.edit_collection_sets.return_value = None
        m.delete_collection.return_value = None
        for attr, val in side_effects.items():
            setattr(m, attr, val)
        return m

    def _seed_folder(self, uuid, name, parent_id=None, collection_id=None):
        fid = self.db.upsert_folder(uuid, name, parent_id=parent_id)
        if collection_id:
            self.db.set_folder_flickr_collection_id(fid, collection_id)
        return fid

    def _seed_album(self, uuid, name, folder_id=None, flickr_set_id=None):
        aid = self.db.upsert_album(uuid, name, folder_id=folder_id)
        if flickr_set_id:
            self.db.set_album_flickr_set_id(aid, flickr_set_id)
        return aid

    def test_creates_collection_for_new_folder(self):
        from flickr.sync_collections import sync_collections
        self._seed_folder("uuid-f1", "Travel")
        flickr = self._make_flickr()

        result = sync_collections(self.db, flickr)

        flickr.create_collection.assert_called_once_with("Travel", description="")
        self.assertEqual(result["created"], 1)
        row = self.db.conn.execute("SELECT flickr_collection_id FROM folders WHERE apple_uuid='uuid-f1'").fetchone()
        self.assertEqual(row["flickr_collection_id"], "col-new")

    def test_skips_create_for_existing_collection(self):
        from flickr.sync_collections import sync_collections
        self._seed_folder("uuid-f1", "Travel", collection_id="col-existing")
        flickr = self._make_flickr()

        sync_collections(self.db, flickr)

        flickr.create_collection.assert_not_called()

    def test_edit_sets_called_with_album_photoset_ids(self):
        from flickr.sync_collections import sync_collections
        fid = self._seed_folder("uuid-f1", "Travel", collection_id="col-1")
        self._seed_album("uuid-a1", "Paris", folder_id=fid, flickr_set_id="ps-111")
        self._seed_album("uuid-a2", "Rome",  folder_id=fid, flickr_set_id="ps-222")
        flickr = self._make_flickr()

        sync_collections(self.db, flickr)

        call_args = flickr.edit_collection_sets.call_args
        self.assertEqual(call_args[0][0], "col-1")
        self.assertCountEqual(call_args[0][1], ["ps-111", "ps-222"])

    def test_skips_albums_without_flickr_set_id(self):
        from flickr.sync_collections import sync_collections
        fid = self._seed_folder("uuid-f1", "Travel", collection_id="col-1")
        self._seed_album("uuid-a1", "Not Pushed Yet", folder_id=fid, flickr_set_id=None)
        flickr = self._make_flickr()

        sync_collections(self.db, flickr)

        call_args = flickr.edit_collection_sets.call_args
        self.assertEqual(call_args[0][1], [])  # no photosets — album not yet pushed

    def test_parent_collection_includes_child_collection_id(self):
        from flickr.sync_collections import sync_collections
        from unittest.mock import MagicMock, call
        parent_id = self._seed_folder("uuid-parent", "Europe", collection_id="col-parent")
        child_id  = self._seed_folder("uuid-child", "France", parent_id=parent_id, collection_id="col-child")
        flickr    = self._make_flickr()

        sync_collections(self.db, flickr)

        # edit_collection_sets must be called for parent with child collection id
        calls = flickr.edit_collection_sets.call_args_list
        parent_call = next(c for c in calls if c[0][0] == "col-parent")
        self.assertIn("col-child", parent_call[0][2])  # sub_collection_ids

    def test_no_folders_is_noop(self):
        from flickr.sync_collections import sync_collections
        flickr = self._make_flickr()
        result = sync_collections(self.db, flickr)
        flickr.create_collection.assert_not_called()
        flickr.edit_collection_sets.assert_not_called()
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["updated"], 0)

    def test_dry_run_makes_no_api_calls(self):
        from flickr.sync_collections import sync_collections
        self._seed_folder("uuid-f1", "Travel")
        flickr = self._make_flickr()

        result = sync_collections(self.db, flickr, dry_run=True)

        flickr.create_collection.assert_not_called()
        flickr.edit_collection_sets.assert_not_called()

    def test_stale_collection_id_cleared_and_recreated(self):
        from flickr.sync_collections import sync_collections
        from flickr.flickr_client import FlickrError
        from unittest.mock import MagicMock
        fid = self._seed_folder("uuid-f1", "Travel", collection_id="col-stale")
        flickr = self._make_flickr()
        flickr.edit_collection_sets.side_effect = [
            FlickrError(2, "Collection not found"),  # first call fails
            None,                                     # second call (after recreate) succeeds
        ]
        flickr.create_collection.return_value = "col-new"

        sync_collections(self.db, flickr)

        flickr.create_collection.assert_called_once()
        row = self.db.conn.execute("SELECT flickr_collection_id FROM folders WHERE id=?", (fid,)).fetchone()
        self.assertEqual(row["flickr_collection_id"], "col-new")
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_core.py -k "TestSyncCollections" -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'flickr.sync_collections'`.

- [ ] **Step 3: Implement sync_collections.py**

Create `flickr/sync_collections.py`:

```python
"""
flickr/sync_collections.py — sync Apple Photos folder hierarchy → Flickr Collections

Usage:
    python flickr/sync_collections.py --config config/config.yml [--dry-run]
    python flickr/sync_collections.py --config config/config.yml --remove [--force]

Or via bp CLI:
    bp sync-album-collections [--dry-run] [--remove [--force]]

Requires a Flickr Pro account. Albums without a folder remain as standalone
photosets and are not affected by this command.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

log = logging.getLogger("blue-pearmain.sync_collections")


def _topological_order(folders: list[dict]) -> list[dict]:
    """Return folders sorted parent-before-child (BFS from roots)."""
    by_id = {f["id"]: f for f in folders}
    children: dict[int | None, list[dict]] = {}
    for f in folders:
        children.setdefault(f["parent_id"], []).append(f)

    result: list[dict] = []
    queue = list(children.get(None, []))
    while queue:
        node = queue.pop(0)
        result.append(node)
        queue.extend(children.get(node["id"], []))
    return result


def sync_collections(db, flickr, dry_run: bool = False) -> dict:
    """
    Sync folder tree from DB → Flickr Collections.
    Returns totals dict: {"created": N, "updated": N, "skipped": N}.
    """
    from flickr.flickr_client import FlickrError

    folders = db.get_all_folders()
    if not folders:
        log.info("sync-album-collections: no folders found — nothing to sync")
        return {"created": 0, "updated": 0, "skipped": 0}

    ordered = _topological_order(folders)
    totals = {"created": 0, "updated": 0, "skipped": 0}

    for folder in ordered:
        folder_id    = folder["id"]
        name         = folder["name"]
        collection_id = folder["flickr_collection_id"]

        if dry_run:
            action = "would create" if not collection_id else "would update"
            log.info("[dry-run] %s collection for folder %r", action, name)
            totals["updated" if collection_id else "created"] += 1
            continue

        # Ensure this folder has a Flickr Collection
        if not collection_id:
            collection_id = flickr.create_collection(name, description="")
            db.set_folder_flickr_collection_id(folder_id, collection_id)
            log.info("created collection %r (id=%s)", name, collection_id)
            totals["created"] += 1
        else:
            totals["updated"] += 1

        # Collect direct child photosets (albums in this folder with a pushed set)
        photoset_rows = db.conn.execute(
            "SELECT flickr_set_id FROM albums WHERE folder_id = ? AND flickr_set_id IS NOT NULL",
            (folder_id,),
        ).fetchall()
        photoset_ids = [r["flickr_set_id"] for r in photoset_rows]

        # Collect direct child sub-collections (child folders with a collection ID)
        sub_col_rows = db.conn.execute(
            "SELECT flickr_collection_id FROM folders WHERE parent_id = ? AND flickr_collection_id IS NOT NULL",
            (folder_id,),
        ).fetchall()
        sub_collection_ids = [r["flickr_collection_id"] for r in sub_col_rows]

        try:
            flickr.edit_collection_sets(collection_id, photoset_ids, sub_collection_ids)
            log.debug(
                "updated collection %r — %d photosets, %d sub-collections",
                name, len(photoset_ids), len(sub_collection_ids),
            )
        except FlickrError as e:
            if "not found" in str(e).lower() or e.code == 2:
                log.warning(
                    "collection %r (id=%s) not found on Flickr — recreating",
                    name, collection_id,
                )
                db.clear_folder_flickr_collection_id(folder_id)
                collection_id = flickr.create_collection(name, description="")
                db.set_folder_flickr_collection_id(folder_id, collection_id)
                flickr.edit_collection_sets(collection_id, photoset_ids, sub_collection_ids)
            else:
                log.error("edit_collection_sets failed for %r: %s", name, e)

    log.info(
        "sync-album-collections done — created=%d  updated=%d  skipped=%d",
        totals["created"], totals["updated"], totals["skipped"],
    )
    return totals


def remove_orphaned_collections(
    db, flickr, library_path: str, force: bool = False
) -> dict:
    """
    Find DB folders whose apple_uuid no longer exists in the live Photos library,
    delete their Flickr Collections, and remove the DB rows.

    Returns {"removed": N, "skipped": N}.
    """
    import osxphotos

    photo_lib = osxphotos.PhotosDB(dbfile=library_path)

    # Collect all live folder UUIDs from the Photos library
    live_uuids: set[str] = set()
    for album in photo_lib.album_info:
        node = getattr(album, "parent", None)
        while node is not None:
            live_uuids.add(node.uuid)
            node = getattr(node, "parent", None)

    folders = db.get_all_folders()
    orphans = [f for f in folders if f["apple_uuid"] not in live_uuids and f["flickr_collection_id"]]

    if not orphans:
        log.info("sync-album-collections --remove: no orphaned collections found")
        return {"removed": 0, "skipped": 0}

    removed = 0
    skipped = 0
    for folder in orphans:
        if not force:
            answer = input(
                f"Delete Flickr Collection {folder['flickr_collection_id']!r} "
                f"for removed folder {folder['name']!r}? [y/N] "
            ).strip().lower()
            if answer != "y":
                log.info("skipped removal of folder %r", folder["name"])
                skipped += 1
                continue

        try:
            flickr.delete_collection(folder["flickr_collection_id"])
            db.conn.execute("DELETE FROM folders WHERE id = ?", (folder["id"],))
            db.conn.commit()
            log.info("removed collection for folder %r", folder["name"])
            removed += 1
        except Exception as e:
            log.error("failed to remove collection for %r: %s", folder["name"], e)
            skipped += 1

    return {"removed": removed, "skipped": skipped}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sync Apple Photos folder hierarchy → Flickr Collections"
    )
    parser.add_argument("--config",  default="config/config.yml")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be synced, don't write")
    parser.add_argument("--remove",  action="store_true", help="Remove Flickr Collections for deleted Photos folders")
    parser.add_argument("--force",   action="store_true", help="Skip confirmation prompts with --remove")
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

    try:
        totals = sync_collections(db, flickr, dry_run=args.dry_run)
    except Exception as e:
        from flickr.flickr_client import FlickrError
        if isinstance(e, FlickrError) and "pro" in str(e).lower():
            log.error("Flickr Collections require a Pro account — skipping")
            return 0
        log.error("sync_collections failed: %s", e)
        return 1

    if args.remove:
        library_path = str(Path(config.get("photos_library", {}).get("path", "")).expanduser())
        remove_orphaned_collections(db, flickr, library_path, force=args.force)

    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_core.py -k "TestSyncCollections" -v
```
Expected: all PASS.

- [ ] **Step 5: Run full suite**

```bash
python -m pytest tests/ -q
```
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add flickr/sync_collections.py tests/test_core.py
git commit -m "feat: sync_collections.py — sync Apple Photos folders → Flickr Collections"
```

---

## Task 6: bp CLI — wire sync-album-collections

**Files:**
- Modify: `bp` (usage docstring, `cmd_all`, subparser section, dispatch table)
- Test: `tests/test_core.py` (add CLI smoke test)

- [ ] **Step 1: Write the failing test**

Find the CLI tests (search for `test_sync_albums_help`) and add nearby:

```python
    def test_sync_album_collections_help(self):
        result = subprocess.run(
            [sys.executable, "bp", "sync-album-collections", "--help"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("sync-album-collections", result.stdout + result.stderr)

    def test_sync_album_collections_in_all_help(self):
        result = subprocess.run(
            [sys.executable, "bp", "all", "--help"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0)
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
python -m pytest tests/test_core.py -k "test_sync_album_collections_help" -v
```
Expected: FAIL — subcommand not registered yet.

- [ ] **Step 3: Update the usage docstring in `bp`**

At the top of `bp`, change:

```python
    bp pipeline [--dry-run] [--limit N]  Sync-metadata then auto-apply non-conflict proposals
```
to also add (after the `sync-albums` line):
```python
    bp sync-album-collections [--dry-run] [--remove [--force]]  Sync folder hierarchy → Flickr Collections
```

- [ ] **Step 4: Add the subparser**

In the subparser section of `bp` (after the `sync-albums` parser, around line 671):

```python
    # sync-album-collections
    p_col = sub.add_parser(
        "sync-album-collections",
        help="Sync Apple Photos folder hierarchy → Flickr Collections (Pro account required)",
    )
    p_col.add_argument("--dry-run", action="store_true",
                       help="Show what would be synced without making API calls")
    p_col.add_argument("--remove",  action="store_true",
                       help="Remove Flickr Collections for folders deleted from Photos")
    p_col.add_argument("--force",   action="store_true",
                       help="Skip confirmation prompts when used with --remove")
    p_col.add_argument("--verbose", action="store_true")
```

- [ ] **Step 5: Add cmd_sync_album_collections function**

Add after `cmd_sync_albums` in `bp`:

```python
def cmd_sync_album_collections(args):
    from flickr.sync_collections import main as _main
    _run(_main, args, [
        ("--config",  args.config),
        ("--dry-run", args.dry_run),
        ("--remove",  getattr(args, "remove", False)),
        ("--force",   getattr(args, "force", False)),
        ("--verbose", args.verbose),
    ])
```

- [ ] **Step 6: Wire into cmd_all and dispatch table**

In `cmd_all`, add `sync-album-collections` after `sync-albums` in the steps list:

```python
        ("sync-albums",            cmd_sync_albums,           _step_args(args, dry_run=dry_run, album=None, limit=None)),
        ("sync-album-collections", cmd_sync_album_collections, _step_args(args, dry_run=dry_run, remove=False, force=False)),
```

In the dispatch table (near line 748), add:
```python
        "sync-album-collections": cmd_sync_album_collections,
```

Also add `if not hasattr(args, "remove"): args.remove = False` and `if not hasattr(args, "force"): args.force = False` to the attribute defaults block.

Update the `cmd_all` docstring to list the new step and renumber checkpoint:

```
      6. sync-albums             sync album memberships → Flickr
      7. sync-album-collections  group photosets → Flickr Collections
      8. checkpoint              trim the WAL file
```

- [ ] **Step 7: Run tests to verify they pass**

```bash
python -m pytest tests/test_core.py -k "sync_album_collections" -v
```
Expected: PASS.

- [ ] **Step 8: Run full suite**

```bash
python -m pytest tests/ -q
```
Expected: all pass.

- [ ] **Step 9: Commit**

```bash
git add bp tests/test_core.py
git commit -m "feat: wire bp sync-album-collections subcommand and bp all integration"
```

---

## Task 7: Docs and README

**Files:**
- Modify: `docs/pipeline.md`
- Modify: `README.md`

- [ ] **Step 1: Update pipeline.md**

Change the stage order block to:

```
bp all
  1. scan --all        Read Apple Photos → DB
  2. poll              Read Flickr API   → DB
  3. thumbs            Download thumbnails → disk
  4. pipeline          Diff DB caches    → proposals → apply non-conflicts
  5. reconcile --fix   Validate DB state → push corrections to Flickr
  6. sync-albums       Sync album memberships → Flickr
  7. sync-album-collections  Group photosets → Flickr Collections
  8. checkpoint        Trim WAL file
```

Update the sentence "Stages 5–7 are independent of each other once stage 4 has run." to "Stages 5–8 are independent of each other once stage 4 has run."

Add a new stage section for `sync-album-collections` after the `sync-albums` section:

```markdown
### 7. `bp sync-album-collections [--dry-run] [--remove [--force]]`

**What it does:** Reads the folder tree from the `folders` table (populated by `bp scan`) and mirrors it as Flickr Collections. Creates a Flickr Collection for each DB folder, then calls `flickr.collections.editSets` to link the correct photosets and sub-collections into each collection. Requires a Flickr Pro account.

**Reads:** `folders` and `albums` tables  
**Writes:** Flickr Collections (create, update contents)  
**External writes:** Yes  
**Idempotent:** Yes — `editSets` is a full replace; re-running produces the same result  

`--dry-run` logs what would be synced without making API calls.  
`--remove [--force]` reads the live Photos library, finds DB folders no longer present, and deletes their Flickr Collections after confirmation. Never runs automatically from `bp all`.
```

Renumber the old stage 7 (checkpoint) to stage 8 throughout.

Update the external writes table to add:
```
| `sync-album-collections` | Flickr (Collection create/update) via API |
```

- [ ] **Step 2: Update README.md**

In the Running section, add to the command list:
```
bp sync-album-collections           # Sync folder hierarchy → Flickr Collections
bp sync-album-collections --dry-run # Preview without API calls
bp sync-album-collections --remove  # Remove collections for deleted folders
```

Update the test count (run `python -m pytest tests/ -q` and use the actual count).

- [ ] **Step 3: Run full suite to confirm count**

```bash
python -m pytest tests/ -q
```
Note the count. Update README accordingly.

- [ ] **Step 4: Commit**

```bash
git add docs/pipeline.md README.md
git commit -m "docs: update pipeline.md and README for sync-album-collections (closes #11)"
```

---

## Task 8: Version bump

- [ ] **Step 1: Bump version in pyproject.toml**

This is a new feature (MINOR bump). Change:
```toml
version = "0.3.0"
```
to:
```toml
version = "0.4.0"
```

- [ ] **Step 2: Regenerate lock file**

```bash
uv lock
```

- [ ] **Step 3: Commit and tag**

```bash
git add pyproject.toml uv.lock
git commit -m "Bump version to 0.4.0 — sync-album-collections (closes #11)"
git tag v0.4.0
```

- [ ] **Step 4: Close the GitHub issue**

```bash
gh issue close 11 --comment "Implemented in this release. bp sync-album-collections mirrors Apple Photos folder hierarchy as Flickr Collections with full recursive nesting, --dry-run, and --remove support."
```
