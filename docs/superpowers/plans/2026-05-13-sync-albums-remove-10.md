# `bp sync-albums --remove` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `--remove` / `--apply` flags to `bp sync-albums` so that photos removed from Apple Photos albums (and deleted albums) are also cleaned up in the corresponding Flickr photosets.

**Architecture:** The scanner detects removals by comparing current osxphotos album membership against stored `photo_albums` rows, writing tombstones (`removed_at`) rather than immediately acting. A separate `sync_deleted_albums()` function catches whole-album deletions. `bp sync-albums --remove` previews pending removals; `--remove --apply` executes them via Flickr API calls and cleans the local DB.

**Tech Stack:** Python, SQLite (via `db/db.py`), Flickr REST API (via `flickr/flickr_client.py`), osxphotos (`photosdb.album_info`), pytest.

**Spec:** `docs/superpowers/specs/2026-05-13-sync-albums-remove-10-design.md`

---

## File map

| Action | File | Purpose |
|--------|------|---------|
| Create | `db/migrations/migrate_015_album_removal.py` | Add `photo_albums.removed_at` and `albums.deleted_at` columns |
| Modify | `db/db.py` | New methods: mark/clear/query tombstones; update `upsert_photo_album` |
| Modify | `flickr/flickr_client.py` | Add `remove_photo_from_photoset`, `delete_photoset`, `FLICKR_ERR_PHOTO_NOT_IN_SET` |
| Modify | `poller/scanner.py` | Update `sync_photo_albums` (removal detection); add `sync_deleted_albums` |
| Modify | `flickr/sync_albums.py` | Add `--remove`/`--apply` flags and removal phase |
| Modify | `tests/test_core.py` | Tests for all new behaviour |

---

## Task 1: Migration 015 — add tombstone columns

**Files:**
- Create: `db/migrations/migrate_015_album_removal.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_core.py` inside a new `class TestMigration015(unittest.TestCase)`:

```python
class TestMigration015(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "test.db")
        from db.db import Database
        db = Database(Path(self.db_path))
        db.close()

    def tearDown(self):
        self._tmp.cleanup()

    def test_adds_removed_at_to_photo_albums(self):
        import sqlite3
        from db.migrations.migrate_015_album_removal import run
        run(self.db_path)
        conn = sqlite3.connect(self.db_path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(photo_albums)").fetchall()}
        conn.close()
        self.assertIn("removed_at", cols)

    def test_adds_deleted_at_to_albums(self):
        import sqlite3
        from db.migrations.migrate_015_album_removal import run
        run(self.db_path)
        conn = sqlite3.connect(self.db_path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(albums)").fetchall()}
        conn.close()
        self.assertIn("deleted_at", cols)

    def test_idempotent(self):
        from db.migrations.migrate_015_album_removal import run
        run(self.db_path)
        run(self.db_path)  # must not raise
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python -m pytest tests/test_core.py::TestMigration015 -v
```
Expected: FAIL with `ModuleNotFoundError` or `ImportError`.

- [ ] **Step 3: Create the migration file**

```python
"""
migrate_015_album_removal.py

Adds:
  photo_albums.removed_at TEXT  — tombstone: scanner detected photo was removed from album
  albums.deleted_at        TEXT  — tombstone: scanner detected album was deleted in Apple Photos

Both columns are nullable. NULL = current state; non-NULL = pending Flickr reconciliation.

Safe to run multiple times (idempotent — checks schema_migrations).

Usage:
    python db/migrations/migrate_015_album_removal.py --config config/config.yml
"""

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import yaml

MIGRATION_NAME = "migrate_015_album_removal"


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

    pa_cols = {r[1] for r in conn.execute("PRAGMA table_info(photo_albums)").fetchall()}
    al_cols = {r[1] for r in conn.execute("PRAGMA table_info(albums)").fetchall()}

    if dry_run:
        if "removed_at" not in pa_cols:
            print("  [dry-run] Would add photo_albums.removed_at column")
        if "deleted_at" not in al_cols:
            print("  [dry-run] Would add albums.deleted_at column")
        conn.close()
        return

    conn.execute("BEGIN")

    if "removed_at" not in pa_cols:
        conn.execute("ALTER TABLE photo_albums ADD COLUMN removed_at TEXT")

    if "deleted_at" not in al_cols:
        conn.execute("ALTER TABLE albums ADD COLUMN deleted_at TEXT")

    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (name, applied_at) VALUES (?, ?)",
        (MIGRATION_NAME, now_iso()),
    )
    conn.commit()
    conn.close()
    print("  Applied:  migrate_015_album_removal")


def main() -> int:
    parser = argparse.ArgumentParser(description="Migration 015: add album removal tombstone columns")
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

- [ ] **Step 4: Apply migration to dev database**

```bash
python db/migrations/migrate_015_album_removal.py --config config/config.yml
```
Expected: `Applied:  migrate_015_album_removal`

- [ ] **Step 5: Run tests to confirm they pass**

```bash
python -m pytest tests/test_core.py::TestMigration015 -v
```
Expected: 3 PASS.

- [ ] **Step 6: Run full test suite to confirm no regressions**

```bash
python -m pytest tests/ -q
```
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add db/migrations/migrate_015_album_removal.py tests/test_core.py
git commit -m "feat: migration 015 — add album removal tombstone columns (#10)"
```

---

## Task 2: DB methods for tombstone management

**Files:**
- Modify: `db/db.py` (Album sync section, around line 775)
- Modify: `tests/test_core.py`

- [ ] **Step 1: Write the failing tests**

Add a new `class TestAlbumRemovalDB(unittest.TestCase)` to `tests/test_core.py`:

```python
class TestAlbumRemovalDB(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        from db.db import Database
        self.db = Database(Path(self._tmp.name) / "test.db")
        # One album, one photo, one photo_albums row (flickr_pushed=1)
        self.album_id = self.db.upsert_album("uuid-a1", "Paris")
        self.photo_id = self.db.upsert_photo({
            "uuid": "photo-uuid-001",
            "original_filename": "IMG_001.jpg",
            "privacy_state": "candidate_public",
            "flickr_id": "flickr-111",
        })
        self.db.upsert_photo_album(self.photo_id, self.album_id)
        self.db.mark_album_pushed(self.photo_id, self.album_id)

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_mark_photo_album_removed(self):
        self.db.mark_photo_album_removed(self.photo_id, self.album_id)
        row = self.db.conn.execute(
            "SELECT removed_at FROM photo_albums WHERE photo_id=? AND album_id=?",
            (self.photo_id, self.album_id),
        ).fetchone()
        self.assertIsNotNone(row["removed_at"])

    def test_clear_photo_album_removed(self):
        self.db.mark_photo_album_removed(self.photo_id, self.album_id)
        self.db.clear_photo_album_removed(self.photo_id, self.album_id)
        row = self.db.conn.execute(
            "SELECT removed_at FROM photo_albums WHERE photo_id=? AND album_id=?",
            (self.photo_id, self.album_id),
        ).fetchone()
        self.assertIsNone(row["removed_at"])

    def test_upsert_photo_album_clears_tombstone_on_reobservation(self):
        self.db.mark_photo_album_removed(self.photo_id, self.album_id)
        self.db.upsert_photo_album(self.photo_id, self.album_id)
        row = self.db.conn.execute(
            "SELECT removed_at FROM photo_albums WHERE photo_id=? AND album_id=?",
            (self.photo_id, self.album_id),
        ).fetchone()
        self.assertIsNone(row["removed_at"], "re-observation must clear removed_at tombstone")

    def test_get_pending_album_removals(self):
        self.db.mark_photo_album_removed(self.photo_id, self.album_id)
        rows = self.db.get_pending_album_removals(limit=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["photo_id"], self.photo_id)
        self.assertEqual(rows[0]["flickr_id"], "flickr-111")

    def test_get_pending_album_removals_excludes_unpushed(self):
        # Second photo, never pushed
        photo2 = self.db.upsert_photo({
            "uuid": "photo-uuid-002",
            "original_filename": "IMG_002.jpg",
            "privacy_state": "candidate_public",
            "flickr_id": "flickr-222",
        })
        self.db.upsert_photo_album(photo2, self.album_id)
        # Do NOT mark_album_pushed for photo2 — flickr_pushed stays 0
        self.db.mark_photo_album_removed(self.photo_id, self.album_id)
        self.db.mark_photo_album_removed(photo2, self.album_id)
        rows = self.db.get_pending_album_removals(limit=10)
        photo_ids = [r["photo_id"] for r in rows]
        self.assertIn(self.photo_id, photo_ids)
        self.assertNotIn(photo2, photo_ids)

    def test_get_deleted_albums(self):
        # Give the album a flickr_set_id and mark it deleted
        self.db.set_album_flickr_set_id(self.album_id, "set-999")
        self.db.conn.execute(
            "UPDATE albums SET deleted_at = ? WHERE id = ?",
            ("2026-05-13T00:00:00+00:00", self.album_id),
        )
        self.db.conn.commit()
        rows = self.db.get_deleted_albums()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], self.album_id)
        self.assertEqual(rows[0]["flickr_set_id"], "set-999")

    def test_get_deleted_albums_excludes_no_flickr_set(self):
        # Album has no flickr_set_id — nothing to delete on Flickr side
        self.db.conn.execute(
            "UPDATE albums SET deleted_at = ? WHERE id = ?",
            ("2026-05-13T00:00:00+00:00", self.album_id),
        )
        self.db.conn.commit()
        rows = self.db.get_deleted_albums()
        self.assertEqual(len(rows), 0)

    def test_delete_photo_album_row(self):
        self.db.delete_photo_album_row(self.photo_id, self.album_id)
        row = self.db.conn.execute(
            "SELECT 1 FROM photo_albums WHERE photo_id=? AND album_id=?",
            (self.photo_id, self.album_id),
        ).fetchone()
        self.assertIsNone(row)

    def test_delete_album_cascades(self):
        self.db.delete_album(self.album_id)
        album_row = self.db.conn.execute(
            "SELECT 1 FROM albums WHERE id=?", (self.album_id,)
        ).fetchone()
        self.assertIsNone(album_row, "album row must be deleted")
        pa_row = self.db.conn.execute(
            "SELECT 1 FROM photo_albums WHERE album_id=?", (self.album_id,)
        ).fetchone()
        self.assertIsNone(pa_row, "photo_albums rows must be cascade-deleted")
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python -m pytest tests/test_core.py::TestAlbumRemovalDB -v
```
Expected: FAIL (methods don't exist yet).

- [ ] **Step 3: Update `upsert_photo_album` in `db/db.py`**

Replace the existing `upsert_photo_album` method (around line 775):

```python
def upsert_photo_album(self, photo_id: int, album_id: int) -> None:
    """Record that a photo belongs to an album.
    If the row already exists with a removed_at tombstone (photo was removed
    then re-added before sync ran), clears the tombstone — no Flickr removal needed.
    """
    self.conn.execute(
        "INSERT OR IGNORE INTO photo_albums (photo_id, album_id) VALUES (?, ?)",
        (photo_id, album_id),
    )
    # Clear any tombstone — photo is back in the album
    self.conn.execute(
        "UPDATE photo_albums SET removed_at = NULL WHERE photo_id = ? AND album_id = ? AND removed_at IS NOT NULL",
        (photo_id, album_id),
    )
    self.conn.commit()
```

- [ ] **Step 4: Add new methods to `db/db.py`** after `mark_album_pushed` (around line 811):

```python
def mark_photo_album_removed(self, photo_id: int, album_id: int) -> None:
    """Tombstone a photo→album row: scanner detected the photo is no longer in this album."""
    self.conn.execute(
        "UPDATE photo_albums SET removed_at = ? WHERE photo_id = ? AND album_id = ?",
        (_now_iso(), photo_id, album_id),
    )
    self.conn.commit()

def clear_photo_album_removed(self, photo_id: int, album_id: int) -> None:
    """Clear a removal tombstone when a photo is re-observed in an album."""
    self.conn.execute(
        "UPDATE photo_albums SET removed_at = NULL WHERE photo_id = ? AND album_id = ?",
        (photo_id, album_id),
    )
    self.conn.commit()

def get_pending_album_removals(self, limit: int = 500) -> list[dict]:
    """Return photo→album pairs tombstoned and confirmed pushed, ready for Flickr removePhoto."""
    rows = self.conn.execute(
        """SELECT pa.photo_id, pa.album_id,
                  p.flickr_id,
                  a.name AS album_name, a.flickr_set_id
           FROM photo_albums pa
           JOIN photos p ON p.id = pa.photo_id
           JOIN albums  a ON a.id = pa.album_id
           WHERE pa.removed_at IS NOT NULL
             AND pa.flickr_pushed = 1
             AND a.flickr_set_id IS NOT NULL
             AND p.flickr_id IS NOT NULL
           LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]

def get_deleted_albums(self) -> list[dict]:
    """Return albums marked deleted that have a Flickr photoset to clean up."""
    rows = self.conn.execute(
        """SELECT id, name, flickr_set_id
           FROM albums
           WHERE deleted_at IS NOT NULL
             AND flickr_set_id IS NOT NULL"""
    ).fetchall()
    return [dict(r) for r in rows]

def mark_album_deleted(self, album_id: int) -> None:
    """Mark an album as deleted in Apple Photos (pending Flickr photoset deletion)."""
    self.conn.execute(
        "UPDATE albums SET deleted_at = ? WHERE id = ?",
        (_now_iso(), album_id),
    )
    self.conn.commit()

def delete_photo_album_row(self, photo_id: int, album_id: int) -> None:
    """Hard-delete one photo→album membership row after Flickr removal is confirmed."""
    self.conn.execute(
        "DELETE FROM photo_albums WHERE photo_id = ? AND album_id = ?",
        (photo_id, album_id),
    )
    self.conn.commit()

def delete_album(self, album_id: int) -> None:
    """Hard-delete an album row. ON DELETE CASCADE removes its photo_albums rows."""
    self.conn.execute("DELETE FROM albums WHERE id = ?", (album_id,))
    self.conn.commit()
```

- [ ] **Step 5: Run tests to confirm they pass**

```bash
python -m pytest tests/test_core.py::TestAlbumRemovalDB -v
```
Expected: all PASS.

- [ ] **Step 6: Run full suite**

```bash
python -m pytest tests/ -q
```
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add db/db.py tests/test_core.py
git commit -m "feat: DB methods for album removal tombstones (#10)"
```

---

## Task 3: Flickr client additions

**Files:**
- Modify: `flickr/flickr_client.py`

No new tests needed — these are thin wrappers. The integration is tested via mocked calls in Tasks 4–6.

- [ ] **Step 1: Add error constant and two methods to `flickr/flickr_client.py`**

After the existing constants block (around line 44), add:
```python
FLICKR_ERR_PHOTO_NOT_IN_SET = 2   # photosets.removePhoto: photo not in the set
```

After `edit_photoset_meta` (around line 380), add:
```python
def remove_photo_from_photoset(self, photoset_id: str, photo_id: str) -> None:
    """Remove a photo from a Flickr photoset."""
    self._call(
        "flickr.photosets.removePhoto",
        {"photoset_id": photoset_id, "photo_id": photo_id},
        http_method="POST",
    )

def delete_photoset(self, photoset_id: str) -> None:
    """Delete a Flickr photoset entirely. Photos are not deleted from Flickr."""
    self._call(
        "flickr.photosets.delete",
        {"photoset_id": photoset_id},
        http_method="POST",
    )
```

- [ ] **Step 2: Run full suite to confirm no regressions**

```bash
python -m pytest tests/ -q
```
Expected: all pass.

- [ ] **Step 3: Commit**

```bash
git add flickr/flickr_client.py
git commit -m "feat: Flickr client — add removePhoto and deletePhotoset methods (#10)"
```

---

## Task 4: Scanner — per-photo removal detection

**Files:**
- Modify: `poller/scanner.py` (`sync_photo_albums` function, around line 197)
- Modify: `tests/test_core.py`

- [ ] **Step 1: Write the failing tests**

Add a new `class TestSyncPhotoAlbumsRemovals(unittest.TestCase)` to `tests/test_core.py`. The existing `TestSyncPhotoAlbums` tests the additive path; this class tests the new removal detection path.

```python
class TestSyncPhotoAlbumsRemovals(unittest.TestCase):
    """sync_photo_albums must tombstone photo_albums rows when a photo leaves an album."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        from db.db import Database
        self.db = Database(Path(self._tmp.name) / "test.db")
        self.photo_id = self.db.upsert_photo({
            "uuid": "photo-uuid-001",
            "original_filename": "IMG_001.jpg",
            "privacy_state": "candidate_public",
        })

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _make_album_info(self, title, uuid):
        from types import SimpleNamespace
        return SimpleNamespace(title=title, uuid=uuid)

    def _make_photo(self, album_infos):
        from types import SimpleNamespace
        return SimpleNamespace(album_info=album_infos)

    def _push_photo_to_album(self, album_id):
        """Helper: mark a photo_albums row as flickr_pushed=1."""
        self.db.mark_album_pushed(self.photo_id, album_id)

    def test_removal_from_one_album_tombstones_row(self):
        """When a pushed photo leaves one album but stays in another, only the departed row is tombstoned."""
        from poller.scanner import sync_photo_albums
        album_a = self._make_album_info("Paris", "uuid-a")
        album_b = self._make_album_info("London", "uuid-b")
        # Seed both memberships as pushed
        aid_a = self.db.upsert_album("uuid-a", "Paris")
        aid_b = self.db.upsert_album("uuid-b", "London")
        self.db.upsert_photo_album(self.photo_id, aid_a)
        self.db.upsert_photo_album(self.photo_id, aid_b)
        self._push_photo_to_album(aid_a)
        self._push_photo_to_album(aid_b)

        # Now scan: photo is only in album_b
        photo = self._make_photo([album_b])
        sync_photo_albums(photo, self.photo_id, self.db, dry_run=False)

        row_a = self.db.conn.execute(
            "SELECT removed_at FROM photo_albums WHERE photo_id=? AND album_id=?",
            (self.photo_id, aid_a),
        ).fetchone()
        row_b = self.db.conn.execute(
            "SELECT removed_at FROM photo_albums WHERE photo_id=? AND album_id=?",
            (self.photo_id, aid_b),
        ).fetchone()
        self.assertIsNotNone(row_a["removed_at"], "departed album must be tombstoned")
        self.assertIsNone(row_b["removed_at"], "remaining album must not be tombstoned")

    def test_removal_of_unpushed_row_deletes_immediately(self):
        """A row with flickr_pushed=0 must be deleted outright, not tombstoned."""
        from poller.scanner import sync_photo_albums
        aid = self.db.upsert_album("uuid-a", "Paris")
        self.db.upsert_photo_album(self.photo_id, aid)
        # Do NOT push — flickr_pushed stays 0

        photo = self._make_photo([])  # photo no longer in any album
        sync_photo_albums(photo, self.photo_id, self.db, dry_run=False)

        row = self.db.conn.execute(
            "SELECT 1 FROM photo_albums WHERE photo_id=? AND album_id=?",
            (self.photo_id, aid),
        ).fetchone()
        self.assertIsNone(row, "unpushed row must be deleted, not tombstoned")

    def test_readd_clears_tombstone(self):
        """If a photo is re-added to an album after being tombstoned, removed_at is cleared."""
        from poller.scanner import sync_photo_albums
        album_a = self._make_album_info("Paris", "uuid-a")
        aid = self.db.upsert_album("uuid-a", "Paris")
        self.db.upsert_photo_album(self.photo_id, aid)
        self._push_photo_to_album(aid)
        self.db.mark_photo_album_removed(self.photo_id, aid)

        # Re-scan: photo is back in the album
        photo = self._make_photo([album_a])
        sync_photo_albums(photo, self.photo_id, self.db, dry_run=False)

        row = self.db.conn.execute(
            "SELECT removed_at FROM photo_albums WHERE photo_id=? AND album_id=?",
            (self.photo_id, aid),
        ).fetchone()
        self.assertIsNone(row["removed_at"], "re-added photo must have tombstone cleared")

    def test_dry_run_does_not_tombstone(self):
        """dry_run=True must not write any tombstones."""
        from poller.scanner import sync_photo_albums
        aid = self.db.upsert_album("uuid-a", "Paris")
        self.db.upsert_photo_album(self.photo_id, aid)
        self._push_photo_to_album(aid)

        photo = self._make_photo([])  # album missing
        sync_photo_albums(photo, self.photo_id, self.db, dry_run=True)

        row = self.db.conn.execute(
            "SELECT removed_at FROM photo_albums WHERE photo_id=? AND album_id=?",
            (self.photo_id, aid),
        ).fetchone()
        self.assertIsNone(row["removed_at"], "dry_run must not tombstone")
        self.assertIsNotNone(row, "dry_run must not delete the row either")
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python -m pytest tests/test_core.py::TestSyncPhotoAlbumsRemovals -v
```
Expected: FAIL (removal detection not implemented).

- [ ] **Step 3: Update `sync_photo_albums` in `poller/scanner.py`**

Replace the entire function (starting at line 197) with:

```python
def sync_photo_albums(photo, photo_db_id: int, db: Database, dry_run: bool) -> None:
    """
    Upsert album membership rows for one osxphotos PhotoInfo object, and
    tombstone any rows for albums the photo is no longer in.

    Uses photo.album_info (list of AlbumInfo objects with .title and .uuid).
    photo.albums returns plain strings and must not be used here.

    Filters to user-created albums only when album_type is available (osxphotos
    >= some future version); when the attribute is absent (osxphotos 0.75.x),
    album_info already excludes smart/system albums so all entries are accepted.
    """
    album_infos = getattr(photo, "album_info", []) or []
    seen_folder_uuids: set[str] = set()
    seen_album_uuids: set[str] = set()  # track all accepted albums for removal detection

    for album in album_infos:
        album_type = getattr(album, "album_type", "Album")
        if album_type != "Album":
            continue

        seen_album_uuids.add(album.uuid)  # collect before dry_run check

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
        db.upsert_photo_album(photo_db_id, album_id)  # also clears any removed_at tombstone

    # Removal detection: tombstone rows for albums this photo is no longer in.
    # Only compare against non-tombstoned rows (already-tombstoned rows are pending sync).
    stored_rows = db.conn.execute(
        """SELECT pa.album_id, a.apple_uuid, pa.flickr_pushed
           FROM photo_albums pa
           JOIN albums a ON a.id = pa.album_id
           WHERE pa.photo_id = ? AND pa.removed_at IS NULL""",
        (photo_db_id,),
    ).fetchall()

    for row in stored_rows:
        if row["apple_uuid"] not in seen_album_uuids:
            if dry_run:
                log.debug(
                    "  [dry-run] photo_id=%s would be removed from album %s",
                    photo_db_id, row["apple_uuid"],
                )
            elif row["flickr_pushed"]:
                db.mark_photo_album_removed(photo_db_id, row["album_id"])
                log.debug(
                    "photo_id=%s removed from album_id=%s — tombstoned (was pushed to Flickr)",
                    photo_db_id, row["album_id"],
                )
            else:
                db.delete_photo_album_row(photo_db_id, row["album_id"])
                log.debug(
                    "photo_id=%s removed from album_id=%s — deleted (never pushed)",
                    photo_db_id, row["album_id"],
                )
```

- [ ] **Step 4: Run new tests to confirm they pass**

```bash
python -m pytest tests/test_core.py::TestSyncPhotoAlbumsRemovals -v
```
Expected: all PASS.

- [ ] **Step 5: Run full suite to confirm no regressions**

```bash
python -m pytest tests/ -q
```
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add poller/scanner.py tests/test_core.py
git commit -m "feat: scanner detects per-photo album removals and tombstones rows (#10)"
```

---

## Task 5: Scanner — album deletion detection

**Files:**
- Modify: `poller/scanner.py` (add `sync_deleted_albums`, wire into `scan()`)
- Modify: `tests/test_core.py`

- [ ] **Step 1: Write the failing tests**

Add `class TestSyncDeletedAlbums(unittest.TestCase)` to `tests/test_core.py`:

```python
class TestSyncDeletedAlbums(unittest.TestCase):
    """sync_deleted_albums must mark albums deleted in Apple Photos with deleted_at."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        from db.db import Database
        self.db = Database(Path(self._tmp.name) / "test.db")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _make_photosdb(self, album_uuids):
        """Stub photosdb that returns AlbumInfo objects for the given UUIDs."""
        from types import SimpleNamespace
        return SimpleNamespace(
            album_info=[SimpleNamespace(uuid=u, title=f"Album-{u}") for u in album_uuids]
        )

    def test_marks_deleted_album(self):
        from poller.scanner import sync_deleted_albums
        aid = self.db.upsert_album("uuid-gone", "Gone Album")
        # Seed a second album that still exists
        self.db.upsert_album("uuid-here", "Here Album")

        photosdb = self._make_photosdb(["uuid-here"])  # uuid-gone is absent
        sync_deleted_albums(photosdb, self.db, dry_run=False)

        row = self.db.conn.execute(
            "SELECT deleted_at FROM albums WHERE id=?", (aid,)
        ).fetchone()
        self.assertIsNotNone(row["deleted_at"], "absent album must be tombstoned")

    def test_does_not_mark_present_albums(self):
        from poller.scanner import sync_deleted_albums
        self.db.upsert_album("uuid-here", "Here Album")

        photosdb = self._make_photosdb(["uuid-here"])
        sync_deleted_albums(photosdb, self.db, dry_run=False)

        row = self.db.conn.execute(
            "SELECT deleted_at FROM albums WHERE apple_uuid=?", ("uuid-here",)
        ).fetchone()
        self.assertIsNone(row["deleted_at"])

    def test_plausibility_guard_zero_albums(self):
        from poller.scanner import sync_deleted_albums
        self.db.upsert_album("uuid-a", "Album A")
        self.db.upsert_album("uuid-b", "Album B")

        photosdb = self._make_photosdb([])  # osxphotos returns nothing — suspicious
        sync_deleted_albums(photosdb, self.db, dry_run=False)

        count = self.db.conn.execute(
            "SELECT COUNT(*) AS n FROM albums WHERE deleted_at IS NOT NULL"
        ).fetchone()["n"]
        self.assertEqual(count, 0, "plausibility guard must block tombstoning when osxphotos returns zero")

    def test_plausibility_guard_threshold(self):
        from poller.scanner import sync_deleted_albums
        # 4 albums in DB, osxphotos returns 1 (25% — below 50% threshold)
        for i in range(4):
            self.db.upsert_album(f"uuid-{i}", f"Album {i}")

        photosdb = self._make_photosdb(["uuid-0"])
        sync_deleted_albums(photosdb, self.db, dry_run=False)

        count = self.db.conn.execute(
            "SELECT COUNT(*) AS n FROM albums WHERE deleted_at IS NOT NULL"
        ).fetchone()["n"]
        self.assertEqual(count, 0, "plausibility guard must block when observed < 50% of stored")

    def test_dry_run_does_not_tombstone(self):
        from poller.scanner import sync_deleted_albums
        self.db.upsert_album("uuid-gone", "Gone Album")
        self.db.upsert_album("uuid-here", "Here Album")

        photosdb = self._make_photosdb(["uuid-here"])
        sync_deleted_albums(photosdb, self.db, dry_run=True)

        row = self.db.conn.execute(
            "SELECT deleted_at FROM albums WHERE apple_uuid=?", ("uuid-gone",)
        ).fetchone()
        self.assertIsNone(row["deleted_at"], "dry_run must not tombstone")

    def test_does_not_re_tombstone_already_deleted(self):
        from poller.scanner import sync_deleted_albums
        aid = self.db.upsert_album("uuid-gone", "Gone")
        self.db.conn.execute(
            "UPDATE albums SET deleted_at='2026-01-01T00:00:00+00:00' WHERE id=?", (aid,)
        )
        self.db.conn.commit()

        photosdb = self._make_photosdb([])  # still absent, and also triggers guard...
        # Add a second album to avoid triggering the zero-albums guard
        self.db.upsert_album("uuid-other", "Other")
        # 1 non-deleted album, 1 deleted → stored_count (non-deleted) = 1, observed = 1 → OK
        photosdb = self._make_photosdb(["uuid-other"])
        sync_deleted_albums(photosdb, self.db, dry_run=False)

        row = self.db.conn.execute(
            "SELECT deleted_at FROM albums WHERE id=?", (aid,)
        ).fetchone()
        self.assertEqual(row["deleted_at"], "2026-01-01T00:00:00+00:00",
                         "existing deleted_at must not be overwritten")
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python -m pytest tests/test_core.py::TestSyncDeletedAlbums -v
```
Expected: FAIL (`sync_deleted_albums` not importable).

- [ ] **Step 3: Add `sync_deleted_albums` to `poller/scanner.py`**

Add the function after `sync_photo_albums` (around line 242):

```python
def sync_deleted_albums(photosdb, db: Database, dry_run: bool) -> int:
    """
    Detect albums deleted from Apple Photos and mark them for Flickr photoset cleanup.

    Compares all album UUIDs from osxphotos against stored album rows and tombstones
    any that have disappeared. Includes a plausibility guard: if osxphotos returns
    fewer than 50% of the stored baseline, aborts to prevent mass false-positives
    from transient osxphotos failures.

    Note: the 50% threshold may abort legitimately for very small libraries (e.g.,
    deleting 1 of 2 albums). Blue Pearmain users have large libraries in practice;
    if this ever matters, add an absolute minimum-difference floor alongside the %.

    Returns the count of albums newly marked deleted.
    """
    try:
        current_album_infos = photosdb.album_info
    except Exception as e:
        log.warning("sync_deleted_albums: could not fetch album list from osxphotos: %s", e)
        return 0

    current_uuids = {a.uuid for a in current_album_infos}

    stored_count = db.conn.execute(
        "SELECT COUNT(*) AS n FROM albums WHERE deleted_at IS NULL"
    ).fetchone()["n"]

    if stored_count > 0 and len(current_uuids) < stored_count * 0.5:
        log.warning(
            "sync_deleted_albums: plausibility guard triggered — "
            "osxphotos returned %d albums but DB has %d non-deleted; "
            "aborting to prevent false deletions",
            len(current_uuids), stored_count,
        )
        return 0

    stored_albums = db.conn.execute(
        "SELECT id, apple_uuid, name FROM albums WHERE deleted_at IS NULL"
    ).fetchall()

    marked = 0
    for row in stored_albums:
        if row["apple_uuid"] not in current_uuids:
            if dry_run:
                log.info(
                    "  [dry-run] album %r (%s) would be marked deleted",
                    row["name"], row["apple_uuid"],
                )
            else:
                db.mark_album_deleted(row["id"])
                log.info("album %r (%s) marked deleted", row["name"], row["apple_uuid"])
            marked += 1

    return marked
```

- [ ] **Step 4: Wire `sync_deleted_albums` into `scan()` in `poller/scanner.py`**

Replace the existing `return` statement at the end of `scan()` (around line 594):

```python
    # Detect albums deleted from Apple Photos since last scan
    sync_deleted_albums(photosdb, db, dry_run)
    return scanned, matched, enriched, inserted, linked
```

- [ ] **Step 5: Run new tests to confirm they pass**

```bash
python -m pytest tests/test_core.py::TestSyncDeletedAlbums -v
```
Expected: all PASS.

- [ ] **Step 6: Run full suite**

```bash
python -m pytest tests/ -q
```
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add poller/scanner.py tests/test_core.py
git commit -m "feat: scanner detects deleted albums and tombstones for Flickr cleanup (#10)"
```

---

## Task 6: `sync-albums --remove` action phase

**Files:**
- Modify: `flickr/sync_albums.py`
- Modify: `tests/test_core.py`

- [ ] **Step 1: Write the failing tests**

Add `class TestSyncAlbumsRemoval(unittest.TestCase)` to `tests/test_core.py`:

```python
class TestSyncAlbumsRemoval(unittest.TestCase):
    """Tests for the sync-albums removal phase (run_removal_phase)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        from db.db import Database
        self.db = Database(Path(self._tmp.name) / "test.db")
        # Album with a Flickr set
        self.album_id = self.db.upsert_album("uuid-a1", "Paris")
        self.db.set_album_flickr_set_id(self.album_id, "set-123")
        # Photo pushed to that album
        self.photo_id = self.db.upsert_photo({
            "uuid": "photo-001",
            "original_filename": "IMG_001.jpg",
            "privacy_state": "candidate_public",
            "flickr_id": "flickr-111",
        })
        self.db.upsert_photo_album(self.photo_id, self.album_id)
        self.db.mark_album_pushed(self.photo_id, self.album_id)

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _make_flickr(self, remove_side_effect=None, delete_side_effect=None):
        from unittest.mock import MagicMock
        flickr = MagicMock()
        if remove_side_effect:
            flickr.remove_photo_from_photoset.side_effect = remove_side_effect
        if delete_side_effect:
            flickr.delete_photoset.side_effect = delete_side_effect
        return flickr

    def test_preview_mode_no_writes(self):
        """--remove without --apply must not call Flickr or mutate DB."""
        from flickr.sync_albums import run_removal_phase
        self.db.mark_photo_album_removed(self.photo_id, self.album_id)
        flickr = self._make_flickr()

        result = run_removal_phase(self.db, flickr, apply=False)

        flickr.remove_photo_from_photoset.assert_not_called()
        flickr.delete_photoset.assert_not_called()
        self.assertEqual(result["photos_removed"], 0)
        # Row must still be tombstoned (not cleaned up)
        row = self.db.conn.execute(
            "SELECT removed_at FROM photo_albums WHERE photo_id=? AND album_id=?",
            (self.photo_id, self.album_id),
        ).fetchone()
        self.assertIsNotNone(row["removed_at"])

    def test_apply_removes_photo_from_photoset(self):
        """--apply must call removePhoto and delete the photo_albums row."""
        from flickr.sync_albums import run_removal_phase
        self.db.mark_photo_album_removed(self.photo_id, self.album_id)
        flickr = self._make_flickr()

        result = run_removal_phase(self.db, flickr, apply=True)

        flickr.remove_photo_from_photoset.assert_called_once_with("set-123", "flickr-111")
        self.assertEqual(result["photos_removed"], 1)
        row = self.db.conn.execute(
            "SELECT 1 FROM photo_albums WHERE photo_id=? AND album_id=?",
            (self.photo_id, self.album_id),
        ).fetchone()
        self.assertIsNone(row, "photo_albums row must be deleted after successful removal")

    def test_apply_photo_not_in_set_treated_as_success(self):
        """FLICKR_ERR_PHOTO_NOT_IN_SET during removePhoto must clean up the row
        and count as already_gone (not photos_removed) — desired state achieved."""
        from flickr.sync_albums import run_removal_phase
        from flickr.flickr_client import FlickrError, FLICKR_ERR_PHOTO_NOT_IN_SET
        self.db.mark_photo_album_removed(self.photo_id, self.album_id)
        flickr = self._make_flickr(
            remove_side_effect=FlickrError(FLICKR_ERR_PHOTO_NOT_IN_SET, "Photo not in set")
        )

        result = run_removal_phase(self.db, flickr, apply=True)

        self.assertEqual(result["already_gone"], 1)
        self.assertEqual(result["photos_removed"], 0)
        row = self.db.conn.execute(
            "SELECT 1 FROM photo_albums WHERE photo_id=? AND album_id=?",
            (self.photo_id, self.album_id),
        ).fetchone()
        self.assertIsNone(row, "DB row must still be cleaned up even when Flickr says already gone")

    def test_apply_photoset_not_found_during_remove_photo_treated_as_success(self):
        """FLICKR_ERR_NOT_FOUND (code 1) from removePhoto means photoset gone — clean up
        and count as already_gone."""
        from flickr.sync_albums import run_removal_phase
        from flickr.flickr_client import FlickrError, FLICKR_ERR_NOT_FOUND
        self.db.mark_photo_album_removed(self.photo_id, self.album_id)
        flickr = self._make_flickr(
            remove_side_effect=FlickrError(FLICKR_ERR_NOT_FOUND, "Photoset not found")
        )

        result = run_removal_phase(self.db, flickr, apply=True)

        self.assertEqual(result["already_gone"], 1)
        self.assertEqual(result["photos_removed"], 0)

    def test_apply_remove_failure_leaves_row_for_retry(self):
        """An unexpected Flickr error must leave the tombstone in place for retry."""
        from flickr.sync_albums import run_removal_phase
        from flickr.flickr_client import FlickrError
        self.db.mark_photo_album_removed(self.photo_id, self.album_id)
        flickr = self._make_flickr(
            remove_side_effect=FlickrError(99, "Server exploded")
        )

        result = run_removal_phase(self.db, flickr, apply=True)

        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["photos_removed"], 0)
        row = self.db.conn.execute(
            "SELECT removed_at FROM photo_albums WHERE photo_id=? AND album_id=?",
            (self.photo_id, self.album_id),
        ).fetchone()
        self.assertIsNotNone(row["removed_at"], "tombstone must remain on failure")

    def test_step1_deleted_album_before_step2_prevents_double_processing(self):
        """Deleting an album in Step 1 must CASCADE-delete its photo_albums rows
        so Step 2 does not also process those rows via removePhoto."""
        from flickr.sync_albums import run_removal_phase
        # Mark the whole album as deleted in Apple Photos
        self.db.mark_photo_album_removed(self.photo_id, self.album_id)
        self.db.conn.execute(
            "UPDATE albums SET deleted_at=? WHERE id=?",
            ("2026-05-13T00:00:00+00:00", self.album_id),
        )
        self.db.conn.commit()
        flickr = self._make_flickr()

        run_removal_phase(self.db, flickr, apply=True)

        flickr.delete_photoset.assert_called_once_with("set-123")
        # removePhoto must NOT have been called — the CASCADE from delete_album handles it
        flickr.remove_photo_from_photoset.assert_not_called()

    def test_step1_photoset_not_found_cleans_local_state(self):
        """FLICKR_ERR_NOT_FOUND from delete_photoset means photoset already gone —
        must count as already_gone (not photosets_deleted) and still clean up local state."""
        from flickr.sync_albums import run_removal_phase
        from flickr.flickr_client import FlickrError, FLICKR_ERR_NOT_FOUND
        self.db.conn.execute(
            "UPDATE albums SET deleted_at=? WHERE id=?",
            ("2026-05-13T00:00:00+00:00", self.album_id),
        )
        self.db.conn.commit()
        flickr = self._make_flickr(
            delete_side_effect=FlickrError(FLICKR_ERR_NOT_FOUND, "Photoset not found")
        )

        result = run_removal_phase(self.db, flickr, apply=True)

        self.assertEqual(result["already_gone"], 1)
        self.assertEqual(result["photosets_deleted"], 0)
        album_row = self.db.conn.execute(
            "SELECT 1 FROM albums WHERE id=?", (self.album_id,)
        ).fetchone()
        self.assertIsNone(album_row, "albums row must be deleted even when photoset was already gone")
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python -m pytest tests/test_core.py::TestSyncAlbumsRemoval -v
```
Expected: FAIL (`run_removal_phase` not importable).

- [ ] **Step 3: Add `run_removal_phase` to `flickr/sync_albums.py`**

Add after the `sync_album_titles` function (around line 150):

```python
def run_removal_phase(db, flickr, apply: bool) -> dict:
    """
    Execute the removal phase of sync-albums.

    Dry-run contract: apply=False performs all DB reads (queries tombstones,
    logs what would happen) but makes zero DB writes and zero Flickr API calls.
    This contract must be preserved — callers rely on it for safe previewing.

    If apply=True: calls Flickr API and cleans up local DB rows on success.

    Idempotency contract: FLICKR_ERR_NOT_FOUND and FLICKR_ERR_PHOTO_NOT_IN_SET
    are treated as successful reconciliation outcomes, not errors. The desired
    state (photo not in photoset, photoset gone) is already achieved. The local
    DB row is cleaned up identically to a clean API success. This prevents
    retries on already-reconciled state.

    Two steps (Step 1 before Step 2 is critical — CASCADE from delete_album
    prevents double-processing of photos in deleted albums):
      Step 1: Delete Flickr photosets for albums deleted in Apple Photos
      Step 2: Remove individual photos from surviving photosets

    Return dict keys:
      photosets_deleted  — delete_photoset API call succeeded
      photos_removed     — removePhoto API call succeeded
      already_gone       — Flickr confirmed desired state without our intervention
                           (photoset/photo already absent); local state cleaned up
      failed             — unexpected errors; tombstones left in place for retry
    """
    from flickr.flickr_client import (
        FlickrError,
        FLICKR_ERR_NOT_FOUND,
        FLICKR_ERR_PHOTO_NOT_IN_SET,
    )

    photosets_deleted = 0
    photos_removed    = 0
    already_gone      = 0
    failed            = 0

    # --- Step 1: Whole photoset deletions ---
    deleted_albums = db.get_deleted_albums()
    for row in deleted_albums:
        if not apply:
            log.info("[preview] would delete photoset %s (%r)", row["flickr_set_id"], row["name"])
            continue
        try:
            flickr.delete_photoset(row["flickr_set_id"])
            photosets_deleted += 1
        except FlickrError as e:
            if e.code == FLICKR_ERR_NOT_FOUND:
                # Photoset already gone on Flickr — desired state achieved
                log.warning(
                    "photoset %s not found on Flickr (already deleted?) — cleaning local state",
                    row["flickr_set_id"],
                )
                already_gone += 1
            else:
                log.error("delete_photoset failed for album %r: %s", row["name"], e)
                failed += 1
                continue
        db.delete_album(row["id"])  # CASCADE removes photo_albums rows

    # --- Step 2: Individual photo removals ---
    pending = db.get_pending_album_removals(limit=500)
    for row in pending:
        if not apply:
            log.info(
                "[preview] would remove flickr_id=%s from photoset %s (%r)",
                row["flickr_id"], row["flickr_set_id"], row["album_name"],
            )
            continue
        try:
            flickr.remove_photo_from_photoset(row["flickr_set_id"], row["flickr_id"])
            photos_removed += 1
        except FlickrError as e:
            if e.code in (FLICKR_ERR_NOT_FOUND, FLICKR_ERR_PHOTO_NOT_IN_SET):
                # Photo not in set, or photoset already gone — desired state achieved.
                # Treat as success: clean up local state, do not retry.
                log.warning(
                    "flickr_id=%s / photoset %s: %s — cleaning local state",
                    row["flickr_id"], row["flickr_set_id"], e,
                )
                already_gone += 1
            else:
                log.error(
                    "removePhoto failed flickr_id=%s photoset=%s: %s",
                    row["flickr_id"], row["flickr_set_id"], e,
                )
                failed += 1
                continue
        db.delete_photo_album_row(row["photo_id"], row["album_id"])

    return {
        "photosets_deleted": photosets_deleted,
        "photos_removed":    photos_removed,
        "already_gone":      already_gone,
        "failed":            failed,
    }
```

- [ ] **Step 4: Add `--remove` and `--apply` flags to `main()` in `flickr/sync_albums.py`**

In the `parser = argparse.ArgumentParser(...)` block (around line 29), add after the `--limit` argument:

```python
parser.add_argument("--remove", action="store_true",
                    help="Show pending removals (preview). Add --apply to execute.")
parser.add_argument("--apply", action="store_true",
                    help="Execute removals (requires --remove). Destructive.")
```

At the end of `main()`, after `sync_album_titles(...)` (around line 114), add:

```python
    if args.remove:
        if args.apply and args.dry_run:
            log.warning("--apply and --dry-run are mutually exclusive; running in preview mode")
            removal_result = run_removal_phase(db, flickr, apply=False)
        else:
            removal_result = run_removal_phase(db, flickr, apply=args.apply)
        print(
            f"photosets deleted={removal_result['photosets_deleted']}  "
            f"photos removed={removal_result['photos_removed']}  "
            f"already-reconciled={removal_result['already_gone']}  "
            f"removal failed={removal_result['failed']}"
        )
        if failed == 0:
            failed = removal_result["failed"]
```

- [ ] **Step 5: Run new tests to confirm they pass**

```bash
python -m pytest tests/test_core.py::TestSyncAlbumsRemoval -v
```
Expected: all PASS.

- [ ] **Step 6: Run full suite**

```bash
python -m pytest tests/ -q
```
Expected: all pass.

- [ ] **Step 7: Update README**

In `README.md`, find the `bp sync-albums` command description and add:

```
bp sync-albums [--dry-run] [--album NAME] [--limit N]
               [--remove [--apply]]
```

With a note that `--remove` previews pending removals, and `--remove --apply` executes them.

- [ ] **Step 8: Close issue and commit**

```bash
git add flickr/sync_albums.py tests/test_core.py README.md
git commit -m "feat: bp sync-albums --remove --apply reconciles Flickr photoset removals (Closes #10)"
```

After committing, add a comment to GH #10:

> Implemented. `bp sync-albums --remove` previews pending removals; `--remove --apply` executes. Scanner now detects per-photo album removals and whole-album deletions at scan time and tombstones the relevant DB rows. Flickr photosets are deleted for whole-album removals; `removePhoto` is called for individual photo removals. All error conditions (photo/photoset already gone) treated as non-fatal.
