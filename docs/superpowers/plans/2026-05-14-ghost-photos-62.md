# Ghost Photo Cleanup — Implementation Plan (#62)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect Photos-only DB records whose Apple Photos UUID no longer exists in the library and hard-delete them, eliminating ghost entries that never resolve a thumbnail and break "Open in Photos."

**Architecture:** Two new functions: `db.delete_photo()` (single-line DELETE; CASCADE handles child rows) and `sync_deleted_photos(photosdb, db, dry_run)` (calls `photosdb.photos()` for the current UUID set, plausibility-guards against empty results and mass deletions, then deletes absent Photos-only records and commits once). `sync_deleted_photos` is called at the end of `scan()` only when `since is None` (full reconciliation mode). The `scan()` return tuple gains a `deleted` element; `main()` unpacks it and appends the count to the log line.

**Tech Stack:** Python stdlib, `sqlite3` (via `Database.conn`), `osxphotos` (already used in `scan()`). No new dependencies.

---

## Files

| File | Change |
|------|--------|
| `db/db.py` | Add `delete_photo(self, photo_id: int) -> None` near `mark_flickr_deleted()` (~line 908) |
| `poller/scanner.py` | Add `sync_deleted_photos(photosdb, db, dry_run) -> int`; update `scan()` signature, return tuple, and docstring; update `main()` |
| `tests/test_core.py` | Append `TestDeletePhoto` (2 tests) and `TestSyncDeletedPhotos` (6 tests) |

---

## Task 1: `db.delete_photo()`

**Files:**
- Modify: `db/db.py` (add after `mark_flickr_deleted()`, ~line 914)
- Test: `tests/test_core.py` (append `TestDeletePhoto`)

- [ ] **Step 1: Write the failing tests**

Open `tests/test_core.py` and append a new test class after the last existing test class:

```python
class TestDeletePhoto(unittest.TestCase):
    """db.delete_photo() hard-deletes a Photos-only record and cascades to photo_albums."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "test.db")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_delete_removes_photo_row(self):
        photo_id = self.db.upsert_photo({
            "uuid": "GHOST-0001",
            "flickr_id": None,
            "privacy_state": "candidate_public",
            "proposed_tags": [],
            "apple_persons": [],
            "apple_labels": [],
        })
        self.db.delete_photo(photo_id)
        row = self.db.conn.execute(
            "SELECT id FROM photos WHERE id = ?", (photo_id,)
        ).fetchone()
        self.assertIsNone(row)

    def test_delete_cascades_to_photo_albums(self):
        photo_id = self.db.upsert_photo({
            "uuid": "GHOST-0002",
            "flickr_id": None,
            "privacy_state": "candidate_public",
            "proposed_tags": [],
            "apple_persons": [],
            "apple_labels": [],
        })
        self.db.upsert_photo_album(photo_id, album_id=42)
        # Verify album row exists before delete
        row = self.db.conn.execute(
            "SELECT * FROM photo_albums WHERE photo_id = ?", (photo_id,)
        ).fetchone()
        self.assertIsNotNone(row)

        self.db.delete_photo(photo_id)

        # After delete, album row should be gone (CASCADE)
        row = self.db.conn.execute(
            "SELECT * FROM photo_albums WHERE photo_id = ?", (photo_id,)
        ).fetchone()
        self.assertIsNone(row)
```

- [ ] **Step 2: Run to verify they fail**

```bash
python -m pytest tests/test_core.py::TestDeletePhoto -v
```

Expected: `AttributeError: 'Database' object has no attribute 'delete_photo'`

- [ ] **Step 3: Add `delete_photo()` to `db/db.py`**

Find `mark_flickr_deleted()` in `db/db.py` (around line 908). Add immediately after its closing line:

```python
    def delete_photo(self, photo_id: int) -> None:
        """Hard-delete a Photos-only record. ON DELETE CASCADE handles photo_albums, metadata_proposals, metadata_conflicts."""
        self.conn.execute("DELETE FROM photos WHERE id = ?", (photo_id,))
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_core.py::TestDeletePhoto -v
```

Expected: 2 passed.

- [ ] **Step 5: Run full suite to confirm no regressions**

```bash
python -m pytest tests/ -q
```

Expected: all passed (2 more than before).

- [ ] **Step 6: Commit**

```bash
git add db/db.py tests/test_core.py
git commit -m "feat: add db.delete_photo() with CASCADE for ghost photo cleanup (#62)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 2: `sync_deleted_photos()`

**Files:**
- Modify: `poller/scanner.py` (add `sync_deleted_photos()` before the `backfill_dimensions()` function at ~line 602)
- Test: `tests/test_core.py` (append `TestSyncDeletedPhotos`)

- [ ] **Step 1: Write the failing tests**

Append the following to `tests/test_core.py` after `TestDeletePhoto`. `_make_mock_photos` is a module-level helper (outside the class), defined before the class declaration:

```python
def _make_mock_photos(*uuids: str):
    """Return MagicMock photo objects with the given .uuid values."""
    from unittest.mock import MagicMock
    result = []
    for u in uuids:
        p = MagicMock()
        p.uuid = u
        result.append(p)
    return result


class TestSyncDeletedPhotos(unittest.TestCase):
    """sync_deleted_photos() detects and deletes Photos-only records absent from osxphotos."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "test.db")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _insert_photos_only(self, uuid: str) -> int:
        return self.db.upsert_photo({
            "uuid": uuid,
            "flickr_id": None,
            "privacy_state": "candidate_public",
            "proposed_tags": [],
            "apple_persons": [],
            "apple_labels": [],
        })

    def _insert_linked(self, uuid: str, flickr_id: str) -> int:
        return self.db.upsert_photo({
            "uuid": uuid,
            "flickr_id": flickr_id,
            "privacy_state": "approved_public",
            "proposed_tags": [],
            "apple_persons": [],
            "apple_labels": [],
        })

    def test_absent_uuid_photos_only_is_deleted_with_cascade(self):
        from unittest.mock import MagicMock
        from poller.scanner import sync_deleted_photos

        photo_id = self._insert_photos_only("GHOST-0001")
        self.db.upsert_photo_album(photo_id, album_id=42)

        mock_photosdb = MagicMock()
        mock_photosdb.photos.return_value = _make_mock_photos("OTHER-UUID")

        deleted = sync_deleted_photos(mock_photosdb, self.db, dry_run=False)

        self.assertEqual(deleted, 1)
        self.assertIsNone(
            self.db.conn.execute("SELECT id FROM photos WHERE id = ?", (photo_id,)).fetchone()
        )
        self.assertIsNone(
            self.db.conn.execute(
                "SELECT * FROM photo_albums WHERE photo_id = ?", (photo_id,)
            ).fetchone()
        )

    def test_linked_record_not_deleted_when_uuid_absent(self):
        from unittest.mock import MagicMock
        from poller.scanner import sync_deleted_photos

        photo_id = self._insert_linked("LINKED-0001", "55555555")

        mock_photosdb = MagicMock()
        mock_photosdb.photos.return_value = _make_mock_photos("OTHER-UUID")

        deleted = sync_deleted_photos(mock_photosdb, self.db, dry_run=False)

        self.assertEqual(deleted, 0)
        self.assertIsNotNone(
            self.db.conn.execute("SELECT id FROM photos WHERE id = ?", (photo_id,)).fetchone()
        )

    def test_zero_photos_guard_prevents_all_deletions(self):
        from unittest.mock import MagicMock
        from poller.scanner import sync_deleted_photos

        self._insert_photos_only("GHOST-0001")

        mock_photosdb = MagicMock()
        mock_photosdb.photos.return_value = []  # osxphotos returned nothing — library read failed

        deleted = sync_deleted_photos(mock_photosdb, self.db, dry_run=False)

        self.assertEqual(deleted, 0)
        self.assertIsNotNone(
            self.db.conn.execute(
                "SELECT id FROM photos WHERE uuid = 'GHOST-0001'"
            ).fetchone()
        )

    def test_mass_deletion_guard_fires_above_ten_percent(self):
        from unittest.mock import MagicMock
        from poller.scanner import sync_deleted_photos

        # Insert 10 Photos-only records; 9 will be absent (90% > 10% threshold)
        for i in range(10):
            self._insert_photos_only(f"GHOST-{i:04d}")

        mock_photosdb = MagicMock()
        mock_photosdb.photos.return_value = _make_mock_photos("GHOST-0000")  # only 1 present

        deleted = sync_deleted_photos(mock_photosdb, self.db, dry_run=False)

        self.assertEqual(deleted, 0)
        count = self.db.conn.execute(
            "SELECT COUNT(*) AS n FROM photos WHERE flickr_id IS NULL AND uuid IS NOT NULL"
        ).fetchone()["n"]
        self.assertEqual(count, 10)

    def test_dry_run_returns_count_but_leaves_db_unchanged(self):
        from unittest.mock import MagicMock
        from poller.scanner import sync_deleted_photos

        photo_id = self._insert_photos_only("GHOST-0001")

        mock_photosdb = MagicMock()
        mock_photosdb.photos.return_value = _make_mock_photos("OTHER-UUID")

        deleted = sync_deleted_photos(mock_photosdb, self.db, dry_run=True)

        self.assertEqual(deleted, 1)  # dry-run reports would-delete count
        self.assertIsNotNone(
            self.db.conn.execute("SELECT id FROM photos WHERE id = ?", (photo_id,)).fetchone()
        )

    def test_multiple_absent_uuids_all_deleted(self):
        from unittest.mock import MagicMock
        from poller.scanner import sync_deleted_photos

        # 2 ghosts + 20 keepers = 22 total; 2/22 = 9.1% < 10% → guard does not fire
        ghost_ids = [self._insert_photos_only(f"GHOST-{i:04d}") for i in range(2)]
        for i in range(20):
            self._insert_photos_only(f"KEEP-{i:04d}")

        mock_photosdb = MagicMock()
        mock_photosdb.photos.return_value = _make_mock_photos(
            *[f"KEEP-{i:04d}" for i in range(20)]
        )

        deleted = sync_deleted_photos(mock_photosdb, self.db, dry_run=False)

        self.assertEqual(deleted, 2)
        for pid in ghost_ids:
            self.assertIsNone(
                self.db.conn.execute("SELECT id FROM photos WHERE id = ?", (pid,)).fetchone()
            )
```

- [ ] **Step 2: Run to verify they fail**

```bash
python -m pytest tests/test_core.py::TestSyncDeletedPhotos -v
```

Expected: `ImportError` — `sync_deleted_photos` doesn't exist yet.

- [ ] **Step 3: Add `sync_deleted_photos()` to `poller/scanner.py`**

Find the line `def backfill_dimensions(db, library) -> int:` (~line 602). Insert the following function immediately before it:

```python
def sync_deleted_photos(photosdb, db: Database, dry_run: bool) -> int:
    """Delete Photos-only DB records whose UUID is no longer in the Photos library.

    Safe to call only during --all scans: photosdb.photos() must return the full
    library (no date filter) so that absence of a UUID is meaningful.

    Returns the count of records deleted (or would-be deleted in dry-run).
    """
    all_photos = photosdb.photos()

    if len(all_photos) == 0:
        log.error(
            "sync_deleted_photos: osxphotos returned 0 photos — "
            "aborting (plausibility guard: empty result indicates library read failure)"
        )
        return 0

    current_uuids: set[str] = {p.uuid for p in all_photos}

    rows = db.conn.execute(
        "SELECT id, uuid FROM photos WHERE uuid IS NOT NULL AND flickr_id IS NULL"
    ).fetchall()

    if not rows:
        log.info("sync_deleted_photos: no Photos-only records to check")
        return 0

    to_delete = [r for r in rows if r["uuid"] not in current_uuids]

    if not to_delete:
        log.info("sync_deleted_photos: all %d Photos-only records still present", len(rows))
        return 0

    deletion_ratio = len(to_delete) / len(rows)
    if deletion_ratio > 0.10:
        log.warning(
            "sync_deleted_photos: would delete %d/%d Photos-only records (%.0f%%) — "
            "exceeds 10%% threshold, aborting. Investigate and re-run if intentional.",
            len(to_delete),
            len(rows),
            deletion_ratio * 100,
        )
        return 0

    for row in to_delete:
        log.info(
            "sync_deleted_photos: %s uuid=%s id=%d",
            "would delete" if dry_run else "deleting",
            row["uuid"],
            row["id"],
        )
        if not dry_run:
            db.delete_photo(row["id"])

    if not dry_run:
        db.conn.commit()

    log.info(
        "sync_deleted_photos: %s %d record(s)",
        "dry-run, would delete" if dry_run else "deleted",
        len(to_delete),
    )
    return len(to_delete)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_core.py::TestSyncDeletedPhotos -v
```

Expected: 6 passed.

- [ ] **Step 5: Run full suite**

```bash
python -m pytest tests/ -q
```

Expected: all passed (6 more than before this task).

- [ ] **Step 6: Commit**

```bash
git add poller/scanner.py tests/test_core.py
git commit -m "feat: add sync_deleted_photos() to detect and remove ghost Photos records (#62)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 3: Wire into `scan()` and update `main()`

**Files:**
- Modify: `poller/scanner.py` (`scan()` signature, docstring, body, return tuple; `main()` unpack and log line)

One new test: verify that `sync_deleted_photos` is **not called** when `since` is set (incremental scan). The full-suite run after the change verifies no regressions.

- [ ] **Step 1: Write the failing test for incremental-scan guard**

Append to `tests/test_core.py` inside `TestSyncDeletedPhotos`:

```python
    def test_not_called_during_incremental_scan(self):
        from unittest.mock import MagicMock, patch
        from poller.scanner import scan
        from datetime import datetime, timezone

        # Minimal mock osxphotos
        mock_photo = MagicMock()
        mock_photo.uuid = "KEEP-0001"
        mock_photo.original_filename = "IMG_001.JPG"
        mock_photo.date = None
        mock_photo.date_added = None
        mock_photo.exif_info = None
        mock_photo.latitude = None
        mock_photo.place = None
        mock_photo.media_analysis = {}
        mock_photo.score = None
        mock_photo.labels = []
        mock_photo.persons = []
        mock_photo.fingerprint = ""
        mock_photo.width = None
        mock_photo.height = None
        mock_photo.screenshot = False
        mock_photo.selfie = False
        mock_photo.live_photo = False
        mock_photo.album_info = []
        mock_photo.title = ""
        mock_photo.description = ""
        mock_photo.keywords = []

        mock_photosdb = MagicMock()
        mock_photosdb.photos.return_value = [mock_photo]

        with patch("poller.scanner.osxphotos") as mock_osxphotos, \
             patch("poller.scanner.sync_deleted_photos") as mock_sync:
            mock_osxphotos.PhotosDB.return_value = mock_photosdb

            # since is set → incremental scan → sync_deleted_photos must NOT be called
            since = datetime(2026, 1, 1, tzinfo=timezone.utc)
            scanned, matched, enriched, inserted, linked, deleted = scan(
                library_path="/fake/library",
                db=self.db,
                since=since,
                dry_run=True,
                self_name="Test User",
            )

        mock_sync.assert_not_called()
        self.assertEqual(deleted, 0)
```

- [ ] **Step 2: Run to verify it fails**

```bash
python -m pytest "tests/test_core.py::TestSyncDeletedPhotos::test_not_called_during_incremental_scan" -v
```

Expected: `ValueError` (scan returns 5 values, not 6) or `AttributeError` — either confirms the wiring doesn't exist yet.

- [ ] **Step 3: Update `scan()` signature and docstring**

Find `def scan(` in `poller/scanner.py` (~line 443). Replace its current signature and docstring:

```python
def scan(
    library_path: str,
    db: Database,
    since: datetime | None,
    dry_run: bool,
    self_name: str,
) -> tuple[int, int, int, int, int, int]:
    """
    Scan the Photos library and sync to DB.

    Returns (scanned, matched, enriched, inserted, linked, deleted) counts.
    `deleted` is always 0 for incremental scans (only runs during --all).
    """
```

- [ ] **Step 4: Add `deleted` counter and call `sync_deleted_photos()` in `scan()`**

Find the line `linked = 0  # Photos-only records late-linked to a Flickr record` (~line 468). Add `deleted` to the counter block:

```python
    linked  = 0  # Photos-only records late-linked to a Flickr record
    deleted = 0
```

Find `return scanned, matched, enriched, inserted, linked` at the end of `scan()` (~line 594). Replace with:

```python
    if since is None:
        deleted = sync_deleted_photos(photosdb, db, dry_run)

    return scanned, matched, enriched, inserted, linked, deleted
```

- [ ] **Step 5: Update `main()` to unpack `deleted` and show it in the log**

Find `scanned, matched, enriched, inserted, linked = scan(` in `main()` (~line 698). Replace the unpack line:

```python
        scanned, matched, enriched, inserted, linked, deleted = scan(
            library_path=library_path,
            db=db,
            since=since,
            dry_run=args.dry_run,
            self_name=self_name,
        )
```

Find the `log.info("Scan complete: ...")` call just below. Replace it:

```python
        base_msg = (
            f"Scan complete: {scanned} scanned, {matched} matched to Flickr, "
            f"{linked} late-linked, {enriched} re-enriched, {inserted} Photos-only inserted"
        )
        if since is None:
            base_msg += f", {deleted} deleted (Photos removed)"
        log.info(base_msg)
```

- [ ] **Step 6: Run full suite**

```bash
python -m pytest tests/ -q
```

Expected: all passed (1 more test than after Task 2 — the incremental-scan guard test).

- [ ] **Step 7: Verify end-to-end with dry-run**

```bash
python poller/scanner.py --config config/config.yml --all --dry-run 2>&1 | tail -5
```

Expected: log line ends with `, N deleted (Photos removed)`. No errors.

- [ ] **Step 8: Commit**

```bash
git add poller/scanner.py
git commit -m "feat: call sync_deleted_photos in bp scan --all, add deleted count to output (#62)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 4: README + GitHub issue

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Update test count in README**

```bash
python -m pytest tests/ -q 2>&1 | tail -3
```

Find the test-count line in `README.md` and update it to the new total.

- [ ] **Step 2: Apply `has-plan` label and comment on GH #62**

```bash
gh issue edit 62 --add-label "has-plan"
gh issue comment 62 --body "Implementation plan written: \`docs/superpowers/plans/2026-05-14-ghost-photos-62.md\`. Ready to implement."
```

- [ ] **Step 3: Commit README**

```bash
git add README.md
git commit -m "docs: update test count after #62 implementation

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```
