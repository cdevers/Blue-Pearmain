# iCloud-Only Thumbnail Resolution — Implementation Plan (#64)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop `bp thumbs` from silently skipping Photos-only records whose originals live in iCloud. Instead, trigger background iCloud downloads via osxphotos, wait up to 60 seconds, resolve what downloaded, and report the rest as queued for the next run.

**Architecture:** Three phases added to `run()` in `poller/thumbnailer.py`. Phase 0: before the main loop, identify Photos-only records (uuid set, no flickr_id), open `osxphotos.PhotosDB` once, build a `uuid → PhotoInfo` map — only if such records exist. Phase 1 (modified loop): when a record has no derivative and no Flickr URL, check whether it is iCloud-only via the map; if so, submit `photo.export(tmpdir, use_photos_export=True)` to a `ThreadPoolExecutor(max_workers=4)` instead of skipping. Phase 2: after the loop, `concurrent.futures.wait(futures, timeout=60)`, retry `derivative_path` for each pending record, write resolved thumbnails, report counts.

**Tech Stack:** `concurrent.futures`, `shutil`, `tempfile` (all stdlib). `osxphotos` (optional; already used in `poller/scanner.py`). No new dependencies. No schema changes. No new CLI flags.

---

## Files

| File | Change |
|------|--------|
| `poller/thumbnailer.py` | Add `concurrent.futures`, `shutil`, `tempfile` imports; add module-level `osxphotos` guard; add Phase 0/Phase 1/Phase 2 logic to `run()`; update log line |
| `tests/test_core.py` | Append `TestThumbnailerICloud` (7 tests) |

---

## Task 1: Imports and Phase 0 (osxphotos setup)

**Files:**
- Modify: `poller/thumbnailer.py` (import block and start of `run()`)
- Test: `tests/test_core.py` (append `TestThumbnailerICloud`, first 2 tests)

- [ ] **Step 1: Write failing tests for Phase 0**

Append a new test class to `tests/test_core.py` after `TestThumbnailer`:

```python
class TestThumbnailerICloud(unittest.TestCase):
    """Tests for iCloud download path added in GH #64."""

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

    def _insert_linked(self, flickr_id: str, uuid: str) -> int:
        return self.db.upsert_photo({
            "uuid": uuid,
            "flickr_id": flickr_id,
            "flickr_secret": "sec",
            "flickr_server": "999",
            "privacy_state": "approved_public",
            "proposed_tags": [],
            "apple_persons": [],
            "apple_labels": [],
        })

    def test_no_photos_only_records_osxphotos_never_opened(self):
        from unittest.mock import MagicMock, patch
        from poller.thumbnailer import run

        # Only a Flickr-linked record — no Photos-only records needing thumbnails
        self._insert_linked("55555555", "LINKED-UUID-0001")

        with patch("poller.thumbnailer.osxphotos") as mock_osxphotos:
            run(db=self.db, library_path="/fake/lib", thumb_root=None,
                flickr_download=False, client=None, limit=None, dry_run=True)

        mock_osxphotos.PhotosDB.assert_not_called()

    def test_photos_only_records_open_photosdb_and_build_map(self):
        from unittest.mock import MagicMock, patch
        from poller.thumbnailer import run

        self._insert_photos_only("ICLOUD-UUID-0001")

        mock_photo = MagicMock()
        mock_photo.uuid = "ICLOUD-UUID-0001"
        mock_photo.iscloudasset = False  # not iCloud — just verify DB was opened
        mock_photo.ismissing = False

        mock_photosdb = MagicMock()
        mock_photosdb.photos.return_value = [mock_photo]

        with patch("poller.thumbnailer.osxphotos") as mock_osxphotos, \
             patch("poller.thumbnailer.derivative_path", return_value=None):
            mock_osxphotos.PhotosDB.return_value = mock_photosdb
            run(db=self.db, library_path="/fake/lib", thumb_root=None,
                flickr_download=False, client=None, limit=None, dry_run=True)

        mock_osxphotos.PhotosDB.assert_called_once_with(dbfile="/fake/lib")
        mock_photosdb.photos.assert_called_once()
```

- [ ] **Step 2: Run to verify they fail**

```bash
python -m pytest "tests/test_core.py::TestThumbnailerICloud::test_no_photos_only_records_osxphotos_never_opened" "tests/test_core.py::TestThumbnailerICloud::test_photos_only_records_open_photosdb_and_build_map" -v
```

Expected: `AttributeError: module 'poller.thumbnailer' has no attribute 'osxphotos'` (or similar — `osxphotos` not yet imported by thumbnailer).

- [ ] **Step 3: Add imports and `osxphotos` guard to `poller/thumbnailer.py`**

In `poller/thumbnailer.py`, replace the entire file header — lines 1–31 (docstring, imports, and `log = logging.getLogger(...)`) — with:

```python
"""
thumbnailer.py — populate thumbnail_path for all DB records

Three sources, in priority order:
  1. Photos library derivative JPEG (already generated by Photos, fastest)
  2. Flickr static URL (reconstructed from server/id/secret, no download)
  3. iCloud download (triggered via osxphotos if Photos.app is running;
     waits up to 60 s, queues remainder for next run)

Usage:
    python poller/thumbnailer.py --config config/config.yml
    python poller/thumbnailer.py --config config/config.yml --flickr-download
    python poller/thumbnailer.py --config config/config.yml --limit 1000

The default mode sets thumbnail_path to either a local file path or a
Flickr URL — no downloads unless --flickr-download is passed.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import logging
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

try:
    import osxphotos
except ImportError:
    osxphotos = None  # type: ignore

sys.path.insert(0, str(Path(__file__).parent.parent))
from db.db import Database
from flickr.flickr_client import FlickrClient

log = logging.getLogger("blue-pearmain.thumbnailer")
```

- [ ] **Step 4: Add Phase 0 to `run()` in `poller/thumbnailer.py`**

In `run()`, find the line `local_count = flickr_url_count = download_count = skipped = 0` (currently ~line 78). Insert Phase 0 immediately before the `for row in rows:` loop:

```python
    local_count = flickr_url_count = download_count = skipped = 0

    # Phase 0 — build iCloud lookup map (only when Photos-only records are present)
    uuid_to_photo: dict[str, Any] = {}
    icloud_pending: list[tuple[int, str, Any]] = []  # (row_id, uuid, future)
    executor: concurrent.futures.ThreadPoolExecutor | None = None
    tmpdir: str | None = None

    photos_only_uuids = [r["uuid"] for r in rows if r["uuid"] and not r["flickr_id"]]
    if photos_only_uuids and osxphotos is not None:
        photosdb = osxphotos.PhotosDB(dbfile=library_path)
        uuid_to_photo = {
            p.uuid: p
            for p in photosdb.photos(uuid=photos_only_uuids)
        }
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)
        tmpdir = tempfile.mkdtemp()
        log.debug(
            "iCloud lookup ready: %d Photos-only records, %d found in library",
            len(photos_only_uuids),
            len(uuid_to_photo),
        )

    for row in rows:
```

- [ ] **Step 5: Run Phase 0 tests to verify they pass**

```bash
python -m pytest "tests/test_core.py::TestThumbnailerICloud::test_no_photos_only_records_osxphotos_never_opened" "tests/test_core.py::TestThumbnailerICloud::test_photos_only_records_open_photosdb_and_build_map" -v
```

Expected: 2 passed.

- [ ] **Step 6: Run full suite to confirm no regressions**

```bash
python -m pytest tests/ -q
```

Expected: all passed (2 more than before).

- [ ] **Step 7: Commit**

```bash
git add poller/thumbnailer.py tests/test_core.py
git commit -m "feat: add Phase 0 osxphotos setup to thumbnailer for iCloud lookup (#64)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 2: Phase 1 + Phase 2 (iCloud path and retry)

**Files:**
- Modify: `poller/thumbnailer.py` (Phase 1 loop change, Phase 2 wait + retry, updated log line)
- Test: `tests/test_core.py` (append 5 more tests to `TestThumbnailerICloud`)

- [ ] **Step 1: Write the failing tests**

Append 5 more test methods inside `TestThumbnailerICloud` in `tests/test_core.py`:

```python
    def test_icloud_photo_resolved_when_download_completes(self):
        from unittest.mock import MagicMock, patch
        from poller.thumbnailer import run

        photo_id = self._insert_photos_only("ICLOUD-UUID-0001")

        mock_photo = MagicMock()
        mock_photo.uuid = "ICLOUD-UUID-0001"
        mock_photo.iscloudasset = True
        mock_photo.ismissing = True
        # export() is a no-op — completes immediately in the thread pool

        mock_photosdb = MagicMock()
        mock_photosdb.photos.return_value = [mock_photo]

        # derivative_path: None first (Phase 1 check), then path (Phase 2 retry)
        mock_deriv = MagicMock(side_effect=[None, "/fake/lib/resources/derivatives/masters/i/ICLOUD-UUID-0001_4_5005_c.jpeg"])

        with patch("poller.thumbnailer.osxphotos") as mock_osxphotos, \
             patch("poller.thumbnailer.derivative_path", mock_deriv):
            mock_osxphotos.PhotosDB.return_value = mock_photosdb
            run(db=self.db, library_path="/fake/lib", thumb_root=None,
                flickr_download=False, client=None, limit=None, dry_run=False)

        mock_photo.export.assert_called_once()
        row = self.db.conn.execute(
            "SELECT thumbnail_path, display_rotation FROM photos WHERE id = ?", (photo_id,)
        ).fetchone()
        self.assertIsNotNone(row["thumbnail_path"])
        self.assertEqual(row["display_rotation"], 0)

    def test_icloud_photo_queued_when_derivative_not_found_after_wait(self):
        from unittest.mock import MagicMock, patch
        from poller.thumbnailer import run

        photo_id = self._insert_photos_only("ICLOUD-UUID-0002")

        mock_photo = MagicMock()
        mock_photo.uuid = "ICLOUD-UUID-0002"
        mock_photo.iscloudasset = True
        mock_photo.ismissing = True

        mock_photosdb = MagicMock()
        mock_photosdb.photos.return_value = [mock_photo]

        with patch("poller.thumbnailer.osxphotos") as mock_osxphotos, \
             patch("poller.thumbnailer.derivative_path", return_value=None):
            mock_osxphotos.PhotosDB.return_value = mock_photosdb
            run(db=self.db, library_path="/fake/lib", thumb_root=None,
                flickr_download=False, client=None, limit=None, dry_run=False)

        # No thumbnail written — photo is queued
        row = self.db.conn.execute(
            "SELECT thumbnail_path FROM photos WHERE id = ?", (photo_id,)
        ).fetchone()
        self.assertIn(row["thumbnail_path"], (None, ""))

    def test_icloud_queued_when_export_raises_no_crash(self):
        from unittest.mock import MagicMock, patch
        from poller.thumbnailer import run

        photo_id = self._insert_photos_only("ICLOUD-UUID-0003")

        mock_photo = MagicMock()
        mock_photo.uuid = "ICLOUD-UUID-0003"
        mock_photo.iscloudasset = True
        mock_photo.ismissing = True
        mock_photo.export.side_effect = Exception("Photos.app not running")

        mock_photosdb = MagicMock()
        mock_photosdb.photos.return_value = [mock_photo]

        # Should complete without raising; photo ends up queued
        with patch("poller.thumbnailer.osxphotos") as mock_osxphotos, \
             patch("poller.thumbnailer.derivative_path", return_value=None):
            mock_osxphotos.PhotosDB.return_value = mock_photosdb
            run(db=self.db, library_path="/fake/lib", thumb_root=None,
                flickr_download=False, client=None, limit=None, dry_run=False)

        row = self.db.conn.execute(
            "SELECT thumbnail_path FROM photos WHERE id = ?", (photo_id,)
        ).fetchone()
        self.assertIn(row["thumbnail_path"], (None, ""))

    def test_dry_run_triggers_export_but_does_not_write_db(self):
        from unittest.mock import MagicMock, patch
        from poller.thumbnailer import run

        photo_id = self._insert_photos_only("ICLOUD-UUID-0004")

        mock_photo = MagicMock()
        mock_photo.uuid = "ICLOUD-UUID-0004"
        mock_photo.iscloudasset = True
        mock_photo.ismissing = True

        mock_photosdb = MagicMock()
        mock_photosdb.photos.return_value = [mock_photo]

        # derivative_path: None first, then a path (download "completes")
        mock_deriv = MagicMock(side_effect=[None, "/fake/icloud.jpeg"])

        with patch("poller.thumbnailer.osxphotos") as mock_osxphotos, \
             patch("poller.thumbnailer.derivative_path", mock_deriv):
            mock_osxphotos.PhotosDB.return_value = mock_photosdb
            run(db=self.db, library_path="/fake/lib", thumb_root=None,
                flickr_download=False, client=None, limit=None, dry_run=True)

        # export WAS submitted (download triggered even in dry-run)
        mock_photo.export.assert_called_once()
        # DB was NOT written
        row = self.db.conn.execute(
            "SELECT thumbnail_path FROM photos WHERE id = ?", (photo_id,)
        ).fetchone()
        self.assertIn(row["thumbnail_path"], (None, ""))

    def test_skipped_count_excludes_icloud_pending_records(self):
        from unittest.mock import MagicMock, patch
        from poller.thumbnailer import run
        import re

        # One iCloud-pending record and one genuinely unresolvable record (no uuid, no flickr_id)
        self._insert_photos_only("ICLOUD-UUID-0005")
        self.db.upsert_photo({
            "uuid": None,
            "flickr_id": None,
            "privacy_state": "candidate_public",
            "proposed_tags": [],
            "apple_persons": [],
            "apple_labels": [],
        })

        mock_photo = MagicMock()
        mock_photo.uuid = "ICLOUD-UUID-0005"
        mock_photo.iscloudasset = True
        mock_photo.ismissing = True

        mock_photosdb = MagicMock()
        mock_photosdb.photos.return_value = [mock_photo]

        log_output = []

        with patch("poller.thumbnailer.osxphotos") as mock_osxphotos, \
             patch("poller.thumbnailer.derivative_path", return_value=None), \
             patch.object(log, "info", side_effect=lambda msg, *a: log_output.append(msg % a if a else msg)):
            mock_osxphotos.PhotosDB.return_value = mock_photosdb
            run(db=self.db, library_path="/fake/lib", thumb_root=None,
                flickr_download=False, client=None, limit=None, dry_run=True)

        done_line = next((l for l in log_output if l.startswith("Done:")), "")
        # skipped should be 1 (the no-uuid record), not 2
        match = re.search(r"(\d+) skipped", done_line)
        self.assertIsNotNone(match, f"Expected 'N skipped' in: {done_line!r}")
        self.assertEqual(int(match.group(1)), 1)
```

- [ ] **Step 2: Run to verify they fail**

```bash
python -m pytest tests/test_core.py::TestThumbnailerICloud -v
```

Expected: the 5 new tests FAIL (no `icloud_pending` list, no Phase 2 code yet). The 2 earlier tests should still pass.

- [ ] **Step 3: Add Phase 1 iCloud path to the main loop in `poller/thumbnailer.py`**

Find the `if not thumb:` block at the end of the `for row in rows:` loop (currently `skipped += 1; continue`). Replace it:

```python
        if not thumb:
            if uuid and executor is not None:
                photo = uuid_to_photo.get(uuid)
                if photo and photo.iscloudasset and photo.ismissing:
                    future = executor.submit(photo.export, tmpdir, use_photos_export=True)
                    icloud_pending.append((row_id, uuid, future))
                    continue  # pending — not skipped
            skipped += 1
            continue
```

- [ ] **Step 4: Add Phase 2 and updated log line to `run()` in `poller/thumbnailer.py`**

Find `if not dry_run:` followed by `db.conn.commit()` at the end of `run()`. Insert Phase 2 immediately before it, then replace the `log.info(...)` call:

```python
    # Phase 2 — wait for iCloud downloads, retry, then clean up
    icloud_resolved = icloud_queued = 0
    if icloud_pending:
        futures = [f for _, _, f in icloud_pending]
        concurrent.futures.wait(futures, timeout=60)

        for row_id, uuid, _ in icloud_pending:
            thumb = derivative_path(uuid, library_path)
            if thumb:
                icloud_resolved += 1
                if not dry_run:
                    db.conn.execute(
                        "UPDATE photos SET thumbnail_path = ?, display_rotation = 0 WHERE id = ?",
                        (thumb, row_id),
                    )
            else:
                icloud_queued += 1

        if executor is not None:
            executor.shutdown(wait=False)
        if tmpdir is not None:
            shutil.rmtree(tmpdir, ignore_errors=True)

    if not dry_run:
        db.conn.commit()

    icloud_msg = ""
    if icloud_resolved or icloud_queued:
        icloud_msg = (
            f", {icloud_resolved} iCloud resolved, {icloud_queued} iCloud queued (run again)"
        )
    log.info(
        f"Done: {local_count} local derivatives, {flickr_url_count} Flickr URLs, "
        f"{download_count} downloaded{icloud_msg}, {skipped} skipped"
    )
```

- [ ] **Step 5: Run all `TestThumbnailerICloud` tests**

```bash
python -m pytest tests/test_core.py::TestThumbnailerICloud -v
```

Expected: 7 passed.

- [ ] **Step 6: Run full suite**

```bash
python -m pytest tests/ -q
```

Expected: all passed (5 more than after Task 1).

- [ ] **Step 7: Verify end-to-end dry-run**

```bash
python poller/thumbnailer.py --config config/config.yml --dry-run 2>&1 | tail -5
```

Expected: "Done: N local derivatives, N Flickr URLs, 0 downloaded, N skipped" (no iCloud section since no actual iCloud records resolve in dry-run without Photos.app). No errors.

- [ ] **Step 8: Commit**

```bash
git add poller/thumbnailer.py tests/test_core.py
git commit -m "feat: add iCloud download path to bp thumbs with 60s wait and retry (#64)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 3: README + GitHub issue

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Update test count in README**

```bash
python -m pytest tests/ -q 2>&1 | tail -3
```

Find the test-count line in `README.md` and update it to the new total.

- [ ] **Step 2: Apply `has-plan` label and comment on GH #64**

```bash
gh issue edit 64 --add-label "has-plan"
gh issue comment 64 --body "Implementation plan written: \`docs/superpowers/plans/2026-05-14-icloud-thumbs-64.md\`. Ready to implement."
```

- [ ] **Step 3: Commit README**

```bash
git add README.md
git commit -m "docs: update test count after #64 implementation

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```
