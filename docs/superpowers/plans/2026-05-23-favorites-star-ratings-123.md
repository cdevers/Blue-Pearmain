# Favorites / Star Ratings Implementation Plan — Issue #123

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a 0–5 star rating field (`bp_rating`) to the photos database, seeded from Apple Photos' heart flag and the Flickr machine tag `bp:rating=N`, surfaced in the reviewer UI with keyboard shortcuts, written back to both platforms, and reported in `bp reconcile --explain`.

**Architecture:** `bp_rating` (0–5 integer) is the canonical DB value. Scanner applies heart→rating sync rules on every poll. Poller seeds from `bp:rating=N` Flickr tags (once, when unrated). The `/rate/<id>` UI endpoint sets the value directly and calls photoscript for same-session Photos write-back; Flickr tag write-back happens on the next poller run. `bp reconcile --explain` reports DB-vs-Flickr rating drift; `--fix` deduplicates multiple `bp:rating=*` tags. The value 0 (unrated) is represented by *absence* of any `bp:rating=*` tag on Flickr — `bp:rating=0` is never written.

**Tech Stack:** SQLite (ALTER TABLE), Python (db.py, scanner.py, poller.py, reconcile.py, explain.py, exporter.py), Flask (app.py), Jinja2/CSS/JS (review.html), Flickr REST API (`flickr.photos.removeTag`).

**Design spec:** `docs/superpowers/specs/2026-05-22-favorites-star-ratings-123-design.md`

---

## File Map

| File | Change |
|---|---|
| `db/migrations/migrate_022_bp_rating.py` | New — adds `bp_rating` column |
| `db/schema.sql` | Add `bp_rating INTEGER NOT NULL DEFAULT 0` |
| `db/db.py` | `_ensure_schema` guard; `review_queue` SELECT; 4 new functions |
| `flickr/flickr_client.py` | Add `remove_tag(tag_id)` method |
| `poller/scanner.py` | `photos_record_to_db` + scan loop apply_scanner_rating calls |
| `poller/poller.py` | Parse `bp:rating=N` in `_enrich_from_info`; seed + write-back in poll loop |
| `poller/reconcile.py` | Singleton dedup in `check_photo` with `--fix` |
| `poller/explain.py` | `explain_photo_rating`; extend `run_explain` and `format_explain_text` |
| `poller/exporter.py` | Add `bp_rating` to `serialize_photo()` |
| `reviewer/app.py` | New `POST /rate/<int:photo_id>` endpoint |
| `reviewer/templates/review.html` | Star widget CSS + Jinja + JS; keyboard 0–5 |
| `docs/export-format.md` | Document `bp_rating` field |
| `tests/test_bp_rating.py` | New — all #123 unit tests |
| `tests/test_exporter.py` | Add `bp_rating` to `_EXPECTED_PHOTO_KEYS` |

---

### Task 1: Migration 022 + DB foundation (TDD)

**Files:**
- New: `db/migrations/migrate_022_bp_rating.py`
- Modify: `db/schema.sql`
- Modify: `db/db.py`
- New: `tests/test_bp_rating.py` (first test classes)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_bp_rating.py`:

```python
"""
tests/test_bp_rating.py — tests for favorites / star ratings (#123)

Run from repo root:
    python -m pytest tests/test_bp_rating.py -v
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.db import Database


# ===========================================================================
# Task 1 — Migration 022 + DB foundation
# ===========================================================================


class TestMigration022(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "test.db")
        # Minimal DB: just the two tables the migration needs
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(id INTEGER PRIMARY KEY, name TEXT UNIQUE, applied_at TEXT)"
        )
        conn.execute("CREATE TABLE IF NOT EXISTS photos (id INTEGER PRIMARY KEY, uuid TEXT)")
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _import_migration(self):
        spec = importlib.util.spec_from_file_location(
            "migrate_022_bp_rating",
            Path(__file__).parent.parent / "db" / "migrations" / "migrate_022_bp_rating.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_migration_adds_bp_rating_column(self):
        """After migration, photos table has bp_rating column."""
        mod = self._import_migration()
        mod.run(self.db_path)
        conn = sqlite3.connect(self.db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(photos)").fetchall()}
        conn.close()
        self.assertIn("bp_rating", cols)

    def test_migration_idempotent(self):
        """Running migration twice does not raise."""
        mod = self._import_migration()
        mod.run(self.db_path)
        mod.run(self.db_path)  # Must not raise

    def test_migration_default_zero(self):
        """Existing rows get bp_rating=0 after migration (SQLite DEFAULT)."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO photos (uuid) VALUES ('existing-uuid')")
        conn.commit()
        conn.close()
        mod = self._import_migration()
        mod.run(self.db_path)
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT bp_rating FROM photos WHERE uuid = 'existing-uuid'"
        ).fetchone()
        conn.close()
        self.assertEqual(row[0], 0)


class TestDBFoundation(unittest.TestCase):
    """Tests for the new db.py functions and review_queue change."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = Database(Path(self.tmp) / "test.db")
        # Seed one test photo
        self.photo_id = self.db.upsert_photo(
            {
                "uuid": "test-uuid-001",
                "original_filename": "IMG_001.JPG",
                "privacy_state": "candidate_public",
                "apple_persons": [],
                "proposed_tags": [],
            }
        )

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp)

    # --- set_bp_rating ---

    def test_set_bp_rating_updates_db(self):
        """set_bp_rating stores the value directly."""
        self.db.set_bp_rating(self.photo_id, 4)
        row = self.db.conn.execute(
            "SELECT bp_rating FROM photos WHERE id = ?", (self.photo_id,)
        ).fetchone()
        self.assertEqual(row["bp_rating"], 4)

    def test_set_bp_rating_logs_operation(self):
        """set_bp_rating writes a set_rating entry to operation_log."""
        self.db.set_bp_rating(self.photo_id, 3)
        logs = self.db.get_operation_log(photo_id=self.photo_id, operation="set_rating")
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["new_value"], "3")

    # --- get_photo_uuid ---

    def test_get_photo_uuid_returns_uuid(self):
        """get_photo_uuid returns the Apple Photos UUID for a valid photo_id."""
        uuid = self.db.get_photo_uuid(self.photo_id)
        self.assertEqual(uuid, "test-uuid-001")

    def test_get_photo_uuid_returns_none_for_missing(self):
        """get_photo_uuid returns None for a photo_id that doesn't exist."""
        self.assertIsNone(self.db.get_photo_uuid(99999))

    # --- review_queue includes bp_rating ---

    def test_review_queue_includes_bp_rating(self):
        """review_queue rows include bp_rating field."""
        self.db.set_bp_rating(self.photo_id, 2)
        photos = self.db.review_queue(states=["candidate_public"])
        self.assertTrue(len(photos) >= 1)
        found = next((p for p in photos if p["id"] == self.photo_id), None)
        self.assertIsNotNone(found)
        self.assertEqual(found["bp_rating"], 2)

    # --- apply_scanner_rating (sync table) ---

    def test_apply_scanner_heart_true_and_zero_seeds(self):
        """Favorite=True + bp_rating=0 → bp_rating becomes 1."""
        # bp_rating starts at 0 (default)
        self.db.apply_scanner_rating(self.photo_id, apple_favorite=1)
        row = self.db.conn.execute(
            "SELECT bp_rating FROM photos WHERE id = ?", (self.photo_id,)
        ).fetchone()
        self.assertEqual(row["bp_rating"], 1)

    def test_apply_scanner_heart_true_and_rated_unchanged(self):
        """Favorite=True + bp_rating=3 → bp_rating stays 3 (no downgrade)."""
        self.db.set_bp_rating(self.photo_id, 3)
        self.db.apply_scanner_rating(self.photo_id, apple_favorite=1)
        row = self.db.conn.execute(
            "SELECT bp_rating FROM photos WHERE id = ?", (self.photo_id,)
        ).fetchone()
        self.assertEqual(row["bp_rating"], 3)

    def test_apply_scanner_heart_false_and_zero_unchanged(self):
        """Favorite=False + bp_rating=0 → bp_rating stays 0."""
        self.db.apply_scanner_rating(self.photo_id, apple_favorite=0)
        row = self.db.conn.execute(
            "SELECT bp_rating FROM photos WHERE id = ?", (self.photo_id,)
        ).fetchone()
        self.assertEqual(row["bp_rating"], 0)

    def test_apply_scanner_heart_false_and_rated_clears(self):
        """Favorite=False + bp_rating=3 → bp_rating becomes 0 (un-heart clears)."""
        self.db.set_bp_rating(self.photo_id, 3)
        self.db.apply_scanner_rating(self.photo_id, apple_favorite=0)
        row = self.db.conn.execute(
            "SELECT bp_rating FROM photos WHERE id = ?", (self.photo_id,)
        ).fetchone()
        self.assertEqual(row["bp_rating"], 0)

    def test_apply_scanner_seed_logs_to_journal(self):
        """apply_scanner_rating logs seed_rating_from_photos when it sets rating to 1."""
        self.db.apply_scanner_rating(self.photo_id, apple_favorite=1)
        logs = self.db.get_operation_log(
            photo_id=self.photo_id, operation="seed_rating_from_photos"
        )
        self.assertEqual(len(logs), 1)

    def test_apply_scanner_clear_logs_to_journal(self):
        """apply_scanner_rating logs clear_rating_from_photos when it clears rating."""
        self.db.set_bp_rating(self.photo_id, 2)
        self.db.apply_scanner_rating(self.photo_id, apple_favorite=0)
        logs = self.db.get_operation_log(
            photo_id=self.photo_id, operation="clear_rating_from_photos"
        )
        self.assertEqual(len(logs), 1)

    # --- seed_flickr_rating ---

    def test_seed_flickr_rating_seeds_when_unrated(self):
        """seed_flickr_rating sets bp_rating when db is 0."""
        self.db.seed_flickr_rating(self.photo_id, flickr_rating=3)
        row = self.db.conn.execute(
            "SELECT bp_rating FROM photos WHERE id = ?", (self.photo_id,)
        ).fetchone()
        self.assertEqual(row["bp_rating"], 3)

    def test_seed_flickr_rating_ignored_when_already_rated(self):
        """seed_flickr_rating does not overwrite an existing non-zero bp_rating."""
        self.db.set_bp_rating(self.photo_id, 2)
        self.db.seed_flickr_rating(self.photo_id, flickr_rating=5)
        row = self.db.conn.execute(
            "SELECT bp_rating FROM photos WHERE id = ?", (self.photo_id,)
        ).fetchone()
        self.assertEqual(row["bp_rating"], 2)

    def test_seed_flickr_rating_logs_to_journal(self):
        """seed_flickr_rating logs seed_rating_from_flickr when it seeds."""
        self.db.seed_flickr_rating(self.photo_id, flickr_rating=4)
        logs = self.db.get_operation_log(
            photo_id=self.photo_id, operation="seed_rating_from_flickr"
        )
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["new_value"], "4")
```

- [ ] **Step 2: Run tests — expect failures**

```bash
python -m pytest tests/test_bp_rating.py::TestMigration022 tests/test_bp_rating.py::TestDBFoundation -v 2>&1 | tail -20
```

Expected: ImportError or AttributeError — migration file doesn't exist, bp_rating column missing, db functions missing.

- [ ] **Step 3: Create `db/migrations/migrate_022_bp_rating.py`**

```python
"""
migrate_022_bp_rating.py

Adds:
  photos.bp_rating INTEGER NOT NULL DEFAULT 0

bp_rating is the canonical 0–5 star rating stored in BP.
  0 = unrated, 1–5 = star count.

No backfill: the scanner seeds values from photo.favorite on its next run.

Safe to run multiple times (idempotent — checks schema_migrations).

Usage:
    python db/migrations/migrate_022_bp_rating.py --config config/config.yml
"""

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import yaml

MIGRATION_NAME = "migrate_022_bp_rating"


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

    existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(photos)").fetchall()}

    if dry_run:
        if "bp_rating" not in existing_cols:
            print("  [dry-run] Would add photos.bp_rating column")
        else:
            print("  [dry-run] photos.bp_rating already exists")
        conn.close()
        return

    conn.execute("BEGIN")

    if "bp_rating" not in existing_cols:
        conn.execute(
            "ALTER TABLE photos ADD COLUMN bp_rating INTEGER NOT NULL DEFAULT 0"
        )

    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (name, applied_at) VALUES (?, ?)",
        (MIGRATION_NAME, now_iso()),
    )
    conn.commit()
    conn.close()
    print("  Applied:  migrate_022_bp_rating")


def main() -> int:
    parser = argparse.ArgumentParser(description="Migration 022: add bp_rating column")
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

- [ ] **Step 4: Update `db/schema.sql` — add `bp_rating` column**

Find the line containing `is_video` in the `photos` table definition and add `bp_rating` immediately after it:

```sql
    is_video                INTEGER NOT NULL DEFAULT 0, -- 1 if this is a video (MOV/MP4/M4V etc.)
    bp_rating               INTEGER NOT NULL DEFAULT 0, -- 0=unrated, 1–5 star rating
    merged_into_id          INTEGER REFERENCES photos(id),
```

- [ ] **Step 5: Update `db/db.py` — add `_ensure_schema` guard for `bp_rating`**

In `_ensure_schema`, immediately after the `is_video` guard block (after `conn.commit()` on line ~420), add:

```python
        existing = {r[1] for r in self.conn.execute("PRAGMA table_info(photos)").fetchall()}
        if "bp_rating" not in existing:
            self.conn.execute(
                "ALTER TABLE photos ADD COLUMN bp_rating INTEGER NOT NULL DEFAULT 0"
            )
            self.conn.commit()
```

- [ ] **Step 6: Update `db/db.py` — add `bp_rating` to `review_queue` SELECT**

Find the SELECT in `review_queue` (around line 644):

```python
            f"""SELECT id, uuid, flickr_id, original_filename,
                       apple_unknown_faces, apple_named_faces, proposed_tags,
                       display_rotation, is_screenshot, updated_at,
                       geofence_zone, apple_persons, privacy_reason,
                       width, height, is_video
                FROM photos
```

Replace with:

```python
            f"""SELECT id, uuid, flickr_id, original_filename,
                       apple_unknown_faces, apple_named_faces, proposed_tags,
                       display_rotation, is_screenshot, updated_at,
                       geofence_zone, apple_persons, privacy_reason,
                       width, height, is_video, bp_rating
                FROM photos
```

- [ ] **Step 7: Update `db/db.py` — add the 4 new functions**

Add these four methods to the `Database` class, after the `set_privacy_state` method (around line 524). Insert them as a new section:

```python
    # -----------------------------------------------------------------------
    # Star ratings
    # -----------------------------------------------------------------------

    def set_bp_rating(self, photo_id: int, rating: int) -> None:
        """Set bp_rating directly (from reviewer UI). Logs to operation_log."""
        row = self.conn.execute(
            "SELECT bp_rating FROM photos WHERE id = ?", (photo_id,)
        ).fetchone()
        old_rating = row["bp_rating"] if row else 0
        self.conn.execute(
            "UPDATE photos SET bp_rating = ? WHERE id = ?", (rating, photo_id)
        )
        self.conn.commit()
        self.log_operation(
            photo_id, "set_rating", "bp_rating", str(old_rating), str(rating), "reviewer_ui"
        )

    def get_photo_uuid(self, photo_id: int) -> str | None:
        """Return the Apple Photos UUID for the given DB row, or None."""
        row = self.conn.execute(
            "SELECT uuid FROM photos WHERE id = ?", (photo_id,)
        ).fetchone()
        return row["uuid"] if row else None

    def apply_scanner_rating(self, photo_id: int, apple_favorite: int) -> None:
        """Apply scanner sync policy for bp_rating. Logs changes to operation_log.

        Sync table (runs on every poll):
          favorite=True  + bp_rating=0   → set bp_rating=1   (seed from heart)
          favorite=True  + bp_rating>0   → no change          (already rated)
          favorite=False + bp_rating=0   → no change          (nothing to clear)
          favorite=False + bp_rating>0   → set bp_rating=0   (user un-hearted)
        """
        row = self.conn.execute(
            "SELECT bp_rating FROM photos WHERE id = ?", (photo_id,)
        ).fetchone()
        if row is None:
            return
        old_rating = row["bp_rating"]

        if apple_favorite == 1 and old_rating == 0:
            new_rating = 1
        elif apple_favorite == 0 and old_rating > 0:
            new_rating = 0
        else:
            return  # no change

        self.conn.execute(
            "UPDATE photos SET bp_rating = ? WHERE id = ?", (new_rating, photo_id)
        )
        self.conn.commit()

        if new_rating == 1:
            self.log_operation(
                photo_id, "seed_rating_from_photos", "bp_rating",
                str(old_rating), str(new_rating), "scanner"
            )
        else:
            self.log_operation(
                photo_id, "clear_rating_from_photos", "bp_rating",
                str(old_rating), str(new_rating), "scanner"
            )

    def seed_flickr_rating(self, photo_id: int, flickr_rating: int) -> None:
        """Seed bp_rating from Flickr machine tag, only if currently unrated.

        BP is authoritative once a rating is set — Flickr tags are seed-only.
        Never overwrites an existing non-zero bp_rating.
        """
        if flickr_rating <= 0:
            return
        row = self.conn.execute(
            "SELECT bp_rating FROM photos WHERE id = ?", (photo_id,)
        ).fetchone()
        if row is None or row["bp_rating"] != 0:
            return
        self.conn.execute(
            "UPDATE photos SET bp_rating = ? WHERE id = ?", (flickr_rating, photo_id)
        )
        self.conn.commit()
        self.log_operation(
            photo_id, "seed_rating_from_flickr", "bp_rating",
            "0", str(flickr_rating), "poller"
        )
```

- [ ] **Step 8: Run tests — expect pass**

```bash
python -m pytest tests/test_bp_rating.py::TestMigration022 tests/test_bp_rating.py::TestDBFoundation -v
```

Expected: all 16 tests PASS.

- [ ] **Step 9: Run full suite + lint**

```bash
python -m pytest tests/ -q
make lint
```

Expected: all tests pass, no lint errors.

- [ ] **Step 10: Commit**

```bash
git add db/migrations/migrate_022_bp_rating.py db/schema.sql db/db.py tests/test_bp_rating.py
git commit -m "feat: add bp_rating column (migration 022) and DB functions (#123)

- migrate_022_bp_rating.py: idempotent ALTER TABLE for bp_rating column
- schema.sql: bp_rating for fresh installs
- db.py: _ensure_schema guard; set_bp_rating, get_photo_uuid,
  apply_scanner_rating, seed_flickr_rating functions; bp_rating in review_queue
- tests: 16 tests covering migration idempotency, all sync rule cases,
  journal logging, and seed-only Flickr logic

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 2: FlickrClient + Scanner (TDD)

**Files:**
- Modify: `flickr/flickr_client.py`
- Modify: `poller/scanner.py`
- Modify: `tests/test_bp_rating.py` (add test classes)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_bp_rating.py`:

```python
# ===========================================================================
# Task 2 — FlickrClient remove_tag + Scanner apple_favorite
# ===========================================================================


class TestFlickrRemoveTag(unittest.TestCase):
    """remove_tag must call flickr.photos.removeTag with the correct tag_id."""

    def test_remove_tag_calls_correct_api(self):
        """remove_tag calls flickr.photos.removeTag with tag_id param."""
        from flickr.flickr_client import FlickrClient

        client = FlickrClient.__new__(FlickrClient)
        client._call = MagicMock(return_value={})
        client.remove_tag("tag-id-abc123")
        client._call.assert_called_once_with(
            "flickr.photos.removeTag",
            {"tag_id": "tag-id-abc123"},
            http_method="POST",
        )


class TestScannerAppleFavorite(unittest.TestCase):
    """photos_record_to_db must include apple_favorite from photo.favorite."""

    def _make_mock_photo(self, favorite: bool) -> MagicMock:
        photo = MagicMock()
        photo.uuid = "scan-uuid-001"
        photo.original_filename = "IMG_001.JPG"
        photo.date = None
        photo.date_added = None
        photo.media_analysis = {}
        photo.exif_info = None
        photo.latitude = None
        photo.place = None
        photo.title = ""
        photo.description = ""
        photo.keywords = []
        photo.labels = []
        photo.persons = []
        photo.score = None
        photo.screenshot = False
        photo.selfie = False
        photo.live_photo = False
        photo.ismovie = False
        photo.fingerprint = ""
        photo.width = 4032
        photo.height = 3024
        photo.favorite = favorite
        return photo

    def test_favorite_true_gives_apple_favorite_1(self):
        """favorite=True → apple_favorite=1 in the row dict."""
        from poller.scanner import photos_record_to_db

        photo = self._make_mock_photo(favorite=True)
        row = photos_record_to_db(photo)
        self.assertEqual(row["apple_favorite"], 1)

    def test_favorite_false_gives_apple_favorite_0(self):
        """favorite=False → apple_favorite=0 in the row dict."""
        from poller.scanner import photos_record_to_db

        photo = self._make_mock_photo(favorite=False)
        row = photos_record_to_db(photo)
        self.assertEqual(row["apple_favorite"], 0)
```

- [ ] **Step 2: Run tests — expect failures**

```bash
python -m pytest tests/test_bp_rating.py::TestFlickrRemoveTag tests/test_bp_rating.py::TestScannerAppleFavorite -v 2>&1 | tail -10
```

Expected: AttributeError — `remove_tag` not found; `apple_favorite` not in scanner row.

- [ ] **Step 3: Add `remove_tag` to `flickr/flickr_client.py`**

Insert immediately after the `add_tags` method (after line ~355):

```python
    def remove_tag(self, tag_id: str) -> None:
        """Remove a single tag by its Flickr tag instance ID.

        The tag_id comes from the 'id' attribute of a <tag> element in
        flickr.photos.getInfo. Each tag instance has a unique ID.
        Does NOT take photo_id — the tag_id identifies the instance globally.
        """
        self._call(
            "flickr.photos.removeTag",
            {"tag_id": tag_id},
            http_method="POST",
        )
```

- [ ] **Step 4: Add `apple_favorite` to `poller/scanner.py` `photos_record_to_db`**

In `photos_record_to_db`, find the `# Dimensions` block at the end (around line 175):

```python
    # Dimensions
    row["width"] = getattr(photo, "width", None)
    row["height"] = getattr(photo, "height", None)

    return row
```

Replace with:

```python
    # Dimensions
    row["width"] = getattr(photo, "width", None)
    row["height"] = getattr(photo, "height", None)

    # Apple Photos heart/Favorites flag
    row["apple_favorite"] = 1 if getattr(photo, "favorite", False) else 0

    return row
```

- [ ] **Step 5: Update the scan loop to call `apply_scanner_rating`**

In `scanner.py`, find the loop body near line 611. Right after `photo_row = photos_record_to_db(photo)`, add an extraction of `apple_favorite` (similar to how `_is_screenshot` is handled):

Find the early-continue block for unchanged photos (around line 652):

```python
            if analysis_unchanged and photos_cache_fresh:
                continue
            enriched_row = build_enriched_row(
                photo_row, existing_by_uuid, zones, self_name, person_policies=person_policies
            )
            if not dry_run:
                db.upsert_photo(enriched_row)
            enriched += 1
            continue
```

Replace with:

```python
            if analysis_unchanged and photos_cache_fresh:
                if not dry_run:
                    db.apply_scanner_rating(existing_by_uuid["id"], photo_row.get("apple_favorite", 0))
                continue
            enriched_row = build_enriched_row(
                photo_row, existing_by_uuid, zones, self_name, person_policies=person_policies
            )
            if not dry_run:
                db.upsert_photo(enriched_row)
                db.apply_scanner_rating(existing_by_uuid["id"], photo_row.get("apple_favorite", 0))
            enriched += 1
            continue
```

Then find the matched-to-Flickr path (around line 673):

```python
            if not dry_run:
                row_id = db.upsert_photo(enriched_row)
                sync_photo_albums(photo, row_id, db, dry_run)
```

Replace with:

```python
            if not dry_run:
                row_id = db.upsert_photo(enriched_row)
                sync_photo_albums(photo, row_id, db, dry_run)
                db.apply_scanner_rating(row_id, photo_row.get("apple_favorite", 0))
```

Then find the Photos-only insert path (around line 718):

```python
            if not dry_run:
                row_id = db.upsert_photo(photo_row)
                sync_photo_albums(photo, row_id, db, dry_run)
```

Replace with:

```python
            if not dry_run:
                row_id = db.upsert_photo(photo_row)
                sync_photo_albums(photo, row_id, db, dry_run)
                db.apply_scanner_rating(row_id, photo_row.get("apple_favorite", 0))
```

**Important:** `apple_favorite` must NOT end up in the data passed to `upsert_photo`. Scanner's `photo_row` gets passed to `upsert_photo` in the Photos-only path (line 719). Before that call happens (line 701 area where `_is_screenshot` etc. are popped), add:

Find the block:
```python
            is_screenshot = photo_row.pop("_is_screenshot", False)
            photo_row.pop("_is_selfie", None)
            photo_row.pop("_is_live", None)
```

This already pops before the insert. Since we call `photo_row.get("apple_favorite", 0)` AFTER this pop block (because apply_scanner_rating is called after upsert_photo), we need to pop `apple_favorite` here too:

```python
            is_screenshot = photo_row.pop("_is_screenshot", False)
            photo_row.pop("_is_selfie", None)
            photo_row.pop("_is_live", None)
            apple_favorite_for_photos_only = photo_row.pop("apple_favorite", 0)
```

And use `apple_favorite_for_photos_only` in the `apply_scanner_rating` call:

```python
            if not dry_run:
                row_id = db.upsert_photo(photo_row)
                sync_photo_albums(photo, row_id, db, dry_run)
                db.apply_scanner_rating(row_id, apple_favorite_for_photos_only)
```

- [ ] **Step 6: Run tests — expect pass**

```bash
python -m pytest tests/test_bp_rating.py::TestFlickrRemoveTag tests/test_bp_rating.py::TestScannerAppleFavorite -v
```

Expected: 3 tests PASS.

- [ ] **Step 7: Run full suite + lint**

```bash
python -m pytest tests/ -q
make lint
```

- [ ] **Step 8: Commit**

```bash
git add flickr/flickr_client.py poller/scanner.py tests/test_bp_rating.py
git commit -m "feat: FlickrClient.remove_tag; scanner apple_favorite sync (#123)

- flickr_client.py: remove_tag(tag_id) using flickr.photos.removeTag
- scanner.py: add apple_favorite to photos_record_to_db; call
  apply_scanner_rating after every upsert (including unchanged photos)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 3: Poller — Flickr seed + tag write-back (TDD)

**Files:**
- Modify: `poller/poller.py`
- Modify: `tests/test_bp_rating.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_bp_rating.py`:

```python
# ===========================================================================
# Task 3 — Poller: Flickr seed + tag write-back
# ===========================================================================


class TestPollerRatingTag(unittest.TestCase):
    """Tests for bp:rating=N tag parsing and write-back in the poller."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = Database(Path(self.tmp) / "test.db")
        self.photo_id = self.db.upsert_photo(
            {
                "uuid": "poller-uuid",
                "flickr_id": "flickr-001",
                "original_filename": "IMG_P.JPG",
                "privacy_state": "candidate_public",
                "apple_persons": [],
                "proposed_tags": [],
            }
        )

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp)

    # --- _parse_bp_rating_from_tags ---

    def test_parse_bp_rating_from_tag_string(self):
        """_parse_bp_rating_from_tags extracts N from 'bp:rating=N' tag."""
        from poller.poller import _parse_bp_rating_from_tags

        tags = ["landscape", "bp:rating=4", "nature"]
        rating, tag_ids = _parse_bp_rating_from_tags(tags)
        self.assertEqual(rating, 4)
        self.assertEqual(tag_ids, [])  # no id dict supplied

    def test_parse_bp_rating_absent_returns_zero(self):
        """_parse_bp_rating_from_tags returns 0 when no bp:rating tag."""
        from poller.poller import _parse_bp_rating_from_tags

        rating, tag_ids = _parse_bp_rating_from_tags(["landscape", "nature"])
        self.assertEqual(rating, 0)
        self.assertEqual(tag_ids, [])

    def test_parse_bp_rating_with_tag_dicts(self):
        """_parse_bp_rating_from_tags returns tag_ids from getInfo tag dicts."""
        from poller.poller import _parse_bp_rating_from_tags

        tag_items = [
            {"raw": "landscape", "id": "id-001"},
            {"raw": "bp:rating=3", "id": "id-002"},
            {"raw": "nature", "id": "id-003"},
        ]
        rating, tag_ids = _parse_bp_rating_from_tags(tag_items)
        self.assertEqual(rating, 3)
        self.assertEqual(tag_ids, ["id-002"])

    def test_parse_bp_rating_multiple_tags_keeps_all_ids(self):
        """Multiple bp:rating=* tags → return all their IDs (for dedup)."""
        from poller.poller import _parse_bp_rating_from_tags

        tag_items = [
            {"raw": "bp:rating=3", "id": "id-001"},
            {"raw": "bp:rating=5", "id": "id-002"},
        ]
        rating, tag_ids = _parse_bp_rating_from_tags(tag_items)
        # Returns the highest value (for dedup consistency)
        self.assertEqual(rating, 5)
        self.assertIn("id-001", tag_ids)
        self.assertIn("id-002", tag_ids)

    # --- _sync_rating_tag (write-back) ---

    def test_sync_rating_adds_tag_when_missing(self):
        """bp_rating=4, no existing tag → add_tags called with bp:rating=4."""
        from poller.poller import _sync_rating_tag

        client = MagicMock()
        self.db.set_bp_rating(self.photo_id, 4)

        tag_items = [{"raw": "landscape", "id": "id-001"}]
        _sync_rating_tag(client, self.db, "flickr-001", self.photo_id, tag_items)

        client.add_tags.assert_called_once_with("flickr-001", ["bp:rating=4"])
        client.remove_tag.assert_not_called()

    def test_sync_rating_removes_tag_when_zero(self):
        """bp_rating=0, tag exists → remove_tag called; never add bp:rating=0."""
        from poller.poller import _sync_rating_tag

        client = MagicMock()
        # bp_rating is 0 (default)

        tag_items = [{"raw": "bp:rating=3", "id": "tag-id-999"}]
        _sync_rating_tag(client, self.db, "flickr-001", self.photo_id, tag_items)

        client.remove_tag.assert_called_once_with("tag-id-999")
        client.add_tags.assert_not_called()

    def test_sync_rating_no_call_when_already_correct(self):
        """bp_rating=3, bp:rating=3 tag already present → no API call."""
        from poller.poller import _sync_rating_tag

        client = MagicMock()
        self.db.set_bp_rating(self.photo_id, 3)

        tag_items = [{"raw": "bp:rating=3", "id": "tag-id-999"}]
        _sync_rating_tag(client, self.db, "flickr-001", self.photo_id, tag_items)

        client.add_tags.assert_not_called()
        client.remove_tag.assert_not_called()

    def test_sync_rating_replaces_wrong_tag(self):
        """bp_rating=4, bp:rating=2 tag on Flickr → remove old, add new."""
        from poller.poller import _sync_rating_tag

        client = MagicMock()
        self.db.set_bp_rating(self.photo_id, 4)

        tag_items = [{"raw": "bp:rating=2", "id": "old-tag-id"}]
        _sync_rating_tag(client, self.db, "flickr-001", self.photo_id, tag_items)

        client.remove_tag.assert_called_once_with("old-tag-id")
        client.add_tags.assert_called_once_with("flickr-001", ["bp:rating=4"])

    def test_sync_rating_never_adds_zero_tag(self):
        """bp_rating=0, no existing tag → no API call at all."""
        from poller.poller import _sync_rating_tag

        client = MagicMock()
        # bp_rating is 0 (default), no tag items

        _sync_rating_tag(client, self.db, "flickr-001", self.photo_id, [])

        client.add_tags.assert_not_called()
        client.remove_tag.assert_not_called()
```

- [ ] **Step 2: Run tests — expect failures**

```bash
python -m pytest tests/test_bp_rating.py::TestPollerRatingTag -v 2>&1 | tail -15
```

Expected: ImportError — `_parse_bp_rating_from_tags` and `_sync_rating_tag` not found.

- [ ] **Step 3: Add helpers to `poller/poller.py`**

Add the following two functions near the top of `poller.py`, after the module-level constants (after `EXTRA_FIELDS`):

```python
def _parse_bp_rating_from_tags(
    tags: list,
) -> tuple[int, list[str]]:
    """Parse bp:rating=N machine tag(s) from a tag list.

    Accepts two formats:
      - list of strings (from normal poll response extras)
      - list of dicts with 'raw' and 'id' keys (from flickr.photos.getInfo)

    Returns (highest_rating, [tag_ids_of_all_bp_rating_tags]).
    rating=0 means absent. tag_ids is [] when tags are plain strings.
    """
    max_rating = 0
    tag_ids: list[str] = []

    for tag in tags:
        if isinstance(tag, dict):
            raw = tag.get("raw", "")
            tid = tag.get("id", "")
        else:
            raw = str(tag)
            tid = ""

        if raw.lower().startswith("bp:rating="):
            try:
                val = int(raw.split("=", 1)[1])
                if val > max_rating:
                    max_rating = val
                if tid:
                    tag_ids.append(tid)
            except (ValueError, IndexError):
                pass

    return max_rating, tag_ids


def _sync_rating_tag(
    client: "FlickrClient",
    db: "Database",
    flickr_id: str,
    photo_id: int,
    tag_items: list[dict],
) -> None:
    """Sync DB bp_rating to Flickr bp:rating=N machine tag.

    tag_items must be the list of tag dicts from flickr.photos.getInfo
    (each with 'raw' and 'id' keys). Called only when getInfo was fetched.

    Rules:
      bp_rating=0, no tag    → no-op
      bp_rating=0, tag exists → remove all bp:rating=* tags
      bp_rating>0, no tag    → add bp:rating=N
      bp_rating>0, correct   → no-op
      bp_rating>0, wrong     → remove all old, add new
    """
    row = db.conn.execute(
        "SELECT bp_rating FROM photos WHERE id = ?", (photo_id,)
    ).fetchone()
    if row is None:
        return
    db_rating = row["bp_rating"]

    flickr_rating, existing_tag_ids = _parse_bp_rating_from_tags(tag_items)

    if db_rating == 0 and not existing_tag_ids:
        return  # nothing to do

    if db_rating == 0 and existing_tag_ids:
        # Remove stale tags
        for tid in existing_tag_ids:
            try:
                client.remove_tag(tid)
            except Exception:
                pass
        return

    if db_rating > 0 and db_rating == flickr_rating and len(existing_tag_ids) == 1:
        return  # already correct, no duplicates

    # Remove any wrong/duplicate tags
    for tid in existing_tag_ids:
        if flickr_rating != db_rating or len(existing_tag_ids) > 1:
            try:
                client.remove_tag(tid)
            except Exception:
                pass

    # Add correct tag
    try:
        client.add_tags(flickr_id, [f"bp:rating={db_rating}"])
    except Exception:
        pass
```

- [ ] **Step 4: Update `_enrich_from_info` to extract `flickr_bp_rating` and `flickr_bp_rating_tag_ids`**

In `_enrich_from_info`, find the tags block:

```python
    # Tags from getInfo are richer (have id, author, raw value)
    tags_container = photo.get("tags", {})
    if isinstance(tags_container, dict):
        tag_items = tags_container.get("tag", [])
        row["flickr_tags"] = [t.get("raw", t.get("_content", "")) for t in tag_items]
```

Replace with:

```python
    # Tags from getInfo are richer (have id, author, raw value)
    tags_container = photo.get("tags", {})
    if isinstance(tags_container, dict):
        tag_items = tags_container.get("tag", [])
        row["flickr_tags"] = [t.get("raw", t.get("_content", "")) for t in tag_items]
        # Extract bp:rating=N for seed/write-back (transient — not stored in DB)
        bp_r, bp_ids = _parse_bp_rating_from_tags(tag_items)
        row["_flickr_bp_rating"] = bp_r
        row["_flickr_bp_rating_tag_items"] = tag_items
```

- [ ] **Step 5: Update the poll loop to seed and write back**

In `poll()`, find the section that drops transient fields (around line 484):

```python
                # Drop transient fields that have no DB column
                for _key in (
                    "thumbnail_url_l",
                    "thumbnail_url_m",
                    "flickr_is_public",
                    "flickr_owner_nsid",
                    "original_format",
                ):
                    row.pop(_key, None)
```

Replace with:

```python
                # Extract transient rating fields before dropping them
                _flickr_bp_rating = row.pop("_flickr_bp_rating", 0)
                _flickr_bp_rating_tag_items = row.pop("_flickr_bp_rating_tag_items", [])

                # Drop transient fields that have no DB column
                for _key in (
                    "thumbnail_url_l",
                    "thumbnail_url_m",
                    "flickr_is_public",
                    "flickr_owner_nsid",
                    "original_format",
                ):
                    row.pop(_key, None)
```

Then find where `db.upsert_photo(row)` is called and `updated` / `new` counters are incremented. There are three such paths (existing/new/auto-push). After each upsert, add the seed and write-back calls.

Find the `existing` branch (around line 501):

```python
                if existing:
                    # Update metadata but preserve any review decisions
                    db.upsert_photo(row)
                    updated += 1
                else:
```

Replace with:

```python
                if existing:
                    # Update metadata but preserve any review decisions
                    photo_row_id = db.upsert_photo(row)
                    if not dry_run and _flickr_bp_rating:
                        db.seed_flickr_rating(photo_row_id, _flickr_bp_rating)
                    if not dry_run and _flickr_bp_rating_tag_items:
                        _sync_rating_tag(client, db, flickr_id, photo_row_id, _flickr_bp_rating_tag_items)
                    updated += 1
                else:
```

Find the `else` branch where new photos go through `_find_approved_photos_record` and then the plain new-photo path (around line 521). After each `db.upsert_photo(row)` in that block, add the same seed/write-back pattern. Find:

```python
                    else:
                        db.upsert_photo(row)
                    new += 1
```

Replace with:

```python
                    else:
                        photo_row_id = db.upsert_photo(row)
                        if not dry_run and _flickr_bp_rating:
                            db.seed_flickr_rating(photo_row_id, _flickr_bp_rating)
                        if not dry_run and _flickr_bp_rating_tag_items:
                            _sync_rating_tag(client, db, flickr_id, photo_row_id, _flickr_bp_rating_tag_items)
                    new += 1
```

- [ ] **Step 6: Run tests — expect pass**

```bash
python -m pytest tests/test_bp_rating.py::TestPollerRatingTag -v
```

Expected: all 9 tests PASS.

- [ ] **Step 7: Run full suite + lint**

```bash
python -m pytest tests/ -q
make lint
```

- [ ] **Step 8: Commit**

```bash
git add poller/poller.py tests/test_bp_rating.py
git commit -m "feat: poller bp:rating tag parsing, Flickr seed, and write-back (#123)

- _parse_bp_rating_from_tags: handle string and dict tag formats
- _sync_rating_tag: idempotent write-back (add/remove/replace/no-op)
- _enrich_from_info: extract _flickr_bp_rating from getInfo tags
- poll loop: seed_flickr_rating and _sync_rating_tag after each upsert

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 4: Reconcile — singleton dedup (TDD)

**Files:**
- Modify: `poller/reconcile.py`
- Modify: `tests/test_bp_rating.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_bp_rating.py`:

```python
# ===========================================================================
# Task 4 — Reconcile: singleton constraint enforcement
# ===========================================================================


class TestReconcileSingleton(unittest.TestCase):
    """check_photo with --fix must deduplicate multiple bp:rating=* tags."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = Database(Path(self.tmp) / "test.db")
        # Seed person_policies table (migration 019 not in schema.sql)
        self.db.conn.execute(
            "CREATE TABLE IF NOT EXISTS person_policies "
            "(id INTEGER PRIMARY KEY, person_name TEXT NOT NULL UNIQUE, "
            "policy TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        self.db.conn.commit()
        self.photo_id = self.db.upsert_photo(
            {
                "uuid": "recon-uuid",
                "flickr_id": "flickr-recon",
                "original_filename": "IMG_R.JPG",
                "privacy_state": "approved_public",
                "apple_persons": [],
                "proposed_tags": [],
                "perms_pushed_flickr": 1,
                "tags_pushed_flickr": 1,
            }
        )
        self.db.set_bp_rating(self.photo_id, 3)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp)

    def _make_info_with_duplicate_rating_tags(self) -> dict:
        """Flickr getInfo response with two conflicting bp:rating=* tags."""
        return {
            "photo": {
                "visibility": {"ispublic": 1, "isfriend": 0, "isfamily": 0},
                "tags": {
                    "tag": [
                        {"raw": "landscape", "id": "tag-land"},
                        {"raw": "bp:rating=3", "id": "tag-rat-3"},
                        {"raw": "bp:rating=5", "id": "tag-rat-5"},
                    ]
                },
            }
        }

    def test_dedup_removes_lower_keeps_higher(self):
        """With two bp:rating=* tags, fix mode removes all but the highest."""
        from poller.reconcile import check_photo

        client = MagicMock()
        client.get_photo_info.return_value = self._make_info_with_duplicate_rating_tags()

        row = dict(
            self.db.conn.execute(
                "SELECT id, flickr_id, privacy_state, pushed_tags, "
                "perms_pushed_flickr, tags_pushed_flickr FROM photos WHERE id = ?",
                (self.photo_id,),
            ).fetchone()
        )

        result = check_photo(client, row, self.db, fix=True, verbose=False)
        # Both bp:rating tags were present; fix should remove the lower one
        removed_ids = [
            call.args[0] for call in client.remove_tag.call_args_list
        ]
        self.assertIn("tag-rat-3", removed_ids)
        self.assertNotIn("tag-rat-5", removed_ids)

    def test_dedup_logs_rating_tag_dedup_to_journal(self):
        """Singleton dedup logs rating_tag_dedup to operation_log."""
        from poller.reconcile import check_photo

        client = MagicMock()
        client.get_photo_info.return_value = self._make_info_with_duplicate_rating_tags()

        row = dict(
            self.db.conn.execute(
                "SELECT id, flickr_id, privacy_state, pushed_tags, "
                "perms_pushed_flickr, tags_pushed_flickr FROM photos WHERE id = ?",
                (self.photo_id,),
            ).fetchone()
        )

        check_photo(client, row, self.db, fix=True, verbose=False)

        logs = self.db.get_operation_log(
            photo_id=self.photo_id, operation="rating_tag_dedup"
        )
        self.assertGreater(len(logs), 0)
```

- [ ] **Step 2: Run tests — expect failures**

```bash
python -m pytest tests/test_bp_rating.py::TestReconcileSingleton -v 2>&1 | tail -10
```

Expected: AssertionError — `remove_tag` not called, `rating_tag_dedup` not logged.

- [ ] **Step 3: Update `poller/reconcile.py` `check_photo` — add singleton dedup**

In `check_photo`, after the tag-check block (after line ~180 `if verbose and result["status"] == "ok":`), add a new rating dedup section. Insert before `return result`:

```python
    # --- Rating singleton constraint: at most one bp:rating=* tag ---
    tags_container = photo.get("tags", {})
    bp_rating_tags: list[dict] = []
    if isinstance(tags_container, dict):
        for t in tags_container.get("tag", []):
            raw = t.get("raw", "").lower()
            if raw.startswith("bp:rating="):
                bp_rating_tags.append(t)

    if len(bp_rating_tags) > 1 and fix:
        # Keep the highest-valued tag, remove the rest
        def _tag_val(t: dict) -> int:
            try:
                return int(t.get("raw", "").split("=", 1)[1])
            except (ValueError, IndexError):
                return 0

        bp_rating_tags_sorted = sorted(bp_rating_tags, key=_tag_val, reverse=True)
        kept_tag = bp_rating_tags_sorted[0]
        to_remove = bp_rating_tags_sorted[1:]

        removed_ids = []
        for t in to_remove:
            try:
                client.remove_tag(t["id"])
                removed_ids.append(t["id"])
            except FlickrError as e:
                result["errors"].append(f"rating dedup remove failed: {e}")

        if removed_ids:
            db.log_operation(
                photo_id=result["row_id"],
                operation="rating_tag_dedup",
                target="flickr_tags",
                old_value=str([t.get("raw") for t in bp_rating_tags]),
                new_value=kept_tag.get("raw"),
                trigger="reconcile_fix",
                actor="bp",
            )
            if result["status"] == "ok":
                result["status"] = "tag_mismatch"
            result["fixes"].append("rating_dedup")
```

- [ ] **Step 4: Run tests — expect pass**

```bash
python -m pytest tests/test_bp_rating.py::TestReconcileSingleton -v
```

Expected: 2 tests PASS.

- [ ] **Step 5: Run full suite + lint**

```bash
python -m pytest tests/ -q
make lint
```

- [ ] **Step 6: Commit**

```bash
git add poller/reconcile.py tests/test_bp_rating.py
git commit -m "feat: reconcile singleton constraint for bp:rating=* tags (#123)

- check_photo with --fix detects multiple bp:rating=* tags on a Flickr photo
- Removes all but the highest-valued tag via remove_tag
- Logs rating_tag_dedup to operation_log

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 5: Explain — rating drift reporting (TDD)

**Files:**
- Modify: `poller/explain.py`
- Modify: `tests/test_bp_rating.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_bp_rating.py`:

```python
# ===========================================================================
# Task 5 — Explain: rating drift
# ===========================================================================


class TestExplainRatingDrift(unittest.TestCase):
    """run_explain must detect and report bp_rating vs Flickr tag drift."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = Database(Path(self.tmp) / "test.db")
        # Seed person_policies table
        self.db.conn.execute(
            "CREATE TABLE IF NOT EXISTS person_policies "
            "(id INTEGER PRIMARY KEY, person_name TEXT NOT NULL UNIQUE, "
            "policy TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        self.db.conn.commit()

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp)

    def test_rating_drift_reported_in_explain(self):
        """Photo with bp_rating=4 and Flickr tag bp:rating=2 → drift in explain."""
        photo_id = self.db.upsert_photo(
            {
                "uuid": "explain-uuid",
                "flickr_id": "flickr-explain",
                "original_filename": "IMG_E.JPG",
                "privacy_state": "approved_public",
                "apple_persons": [],
                "proposed_tags": [],
                "perms_pushed_flickr": 1,
                "tags_pushed_flickr": 1,
                "flickr_tags": json.dumps(["landscape", "bp:rating=2"]),
            }
        )
        self.db.set_bp_rating(photo_id, 4)

        from poller.explain import run_explain, format_explain_text

        explanations = run_explain(self.db, limit=50, flickr_username="testuser")
        output = format_explain_text(explanations, flickr_username="testuser")

        # At least one entry should mention the rating drift
        rating_entries = [e for e in explanations if e.get("rating")]
        self.assertGreater(len(rating_entries), 0, "Expected rating drift entry")
        drift_entry = rating_entries[0]["rating"]
        self.assertEqual(drift_entry["db_rating"], 4)
        self.assertEqual(drift_entry["flickr_rating"], 2)

    def test_no_drift_when_rating_matches(self):
        """Photo with bp_rating=3 and bp:rating=3 Flickr tag → no drift."""
        photo_id = self.db.upsert_photo(
            {
                "uuid": "explain-match-uuid",
                "flickr_id": "flickr-match",
                "original_filename": "IMG_M.JPG",
                "privacy_state": "approved_public",
                "apple_persons": [],
                "proposed_tags": [],
                "perms_pushed_flickr": 1,
                "tags_pushed_flickr": 1,
                "flickr_tags": json.dumps(["bp:rating=3"]),
            }
        )
        self.db.set_bp_rating(photo_id, 3)

        from poller.explain import run_explain

        explanations = run_explain(self.db, limit=50, flickr_username="testuser")
        rating_drifts = [e for e in explanations if e.get("rating")]
        self.assertEqual(len(rating_drifts), 0)
```

- [ ] **Step 2: Run tests — expect failures**

```bash
python -m pytest tests/test_bp_rating.py::TestExplainRatingDrift -v 2>&1 | tail -10
```

Expected: AssertionError — explain does not include rating drift yet.

- [ ] **Step 3: Add `explain_photo_rating` to `poller/explain.py`**

After the `explain_photo_perms` function, add:

```python
def explain_photo_rating(row: dict) -> dict | None:
    """
    Return a rating explanation dict, or None if there is no drift.

    Compares DB bp_rating against the bp:rating=N tag in flickr_tags.
    Returns None if they agree (including both being 0/absent).

    Keys:
        db_rating      — integer 0–5 from DB
        flickr_rating  — integer 0–5 parsed from Flickr tags (0 = absent)
        reason         — human-readable drift description
    """
    db_rating = row.get("bp_rating") or 0

    flickr_tags = _json_loads_safe(row.get("flickr_tags"))
    flickr_rating = 0
    for tag in flickr_tags:
        raw = str(tag).lower().strip()
        if raw.startswith("bp:rating="):
            try:
                flickr_rating = int(raw.split("=", 1)[1])
            except (ValueError, IndexError):
                pass

    if db_rating == flickr_rating:
        return None

    return {
        "db_rating": db_rating,
        "flickr_rating": flickr_rating,
        "reason": (
            f"DB has bp_rating={db_rating}, Flickr tag has "
            f"bp:rating={flickr_rating} — will update Flickr tag on next sync"
        ),
    }
```

- [ ] **Step 4: Update `run_explain` to include rating drift**

In `run_explain`, update the SELECT to include `bp_rating`:

```python
    rows = db.conn.execute(
        """SELECT id, flickr_id, flickr_title,
                  flickr_tags, photos_tags, pushed_tags,
                  privacy_state, review_decision, reviewed_at,
                  perms_pushed_flickr, tags_pushed_flickr, bp_rating
           FROM photos
           WHERE flickr_id IS NOT NULL
             AND (flickr_deleted IS NULL OR flickr_deleted = 0)
             AND (
               tags_pushed_flickr = 1
               OR (review_decision IS NOT NULL AND perms_pushed_flickr = 0)
               OR bp_rating != 0
             )
           ORDER BY reviewed_at DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
```

In the loop, add `rating_exp`:

```python
    for row in rows:
        r = dict(row)
        perms_exp = explain_photo_perms(r)
        tags_exp = explain_photo_tags(r)
        rating_exp = explain_photo_rating(r)

        if perms_exp or tags_exp or rating_exp:
            results.append(
                {
                    "photo_id": r["id"],
                    "flickr_id": r.get("flickr_id"),
                    "title": r.get("flickr_title") or "",
                    "perms": perms_exp,
                    "tags": tags_exp,
                    "rating": rating_exp,
                }
            )
```

- [ ] **Step 5: Update `format_explain_text` to render rating drift**

In `format_explain_text`, after the `if exp.get("tags"):` block, add:

```python
        if exp.get("rating"):
            rt = exp["rating"]
            lines.append("  rating")
            lines.append(f"    DB bp_rating:    {rt['db_rating']}")
            lines.append(f"    Flickr tag:      {rt['flickr_rating'] or '(none)'}")
            lines.append(f"    reason:          {rt['reason']}")
            lines.append("")
```

- [ ] **Step 6: Run tests — expect pass**

```bash
python -m pytest tests/test_bp_rating.py::TestExplainRatingDrift -v
```

Expected: 2 tests PASS.

- [ ] **Step 7: Run full suite + lint**

```bash
python -m pytest tests/ -q
make lint
```

- [ ] **Step 8: Commit**

```bash
git add poller/explain.py tests/test_bp_rating.py
git commit -m "feat: explain rating drift for bp_rating vs Flickr tag (#123)

- explain_photo_rating: compares DB bp_rating to flickr_tags bp:rating=N
- run_explain: add bp_rating to SELECT; include rating_exp in results
- format_explain_text: render rating drift section
- run_explain WHERE: also includes photos with bp_rating != 0

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 6: Exporter + docs/export-format.md (TDD)

**Files:**
- Modify: `poller/exporter.py`
- Modify: `tests/test_exporter.py`
- Modify: `docs/export-format.md`

- [ ] **Step 1: Write the failing test — add `bp_rating` to `_EXPECTED_PHOTO_KEYS`**

In `tests/test_exporter.py`, find `_EXPECTED_PHOTO_KEYS` in the `TestExportFormatVersion` class and add `"bp_rating"`:

```python
    _EXPECTED_PHOTO_KEYS = {
        "id",
        "flickr_id",
        "apple_uuid",
        "original_filename",
        "title",
        "description",
        "tags",
        "privacy_state",
        "review_decision",
        "reviewed_at",
        "date_taken",
        "location",
        "geofenced",
        "faces",
        "albums",
        "bp_rating",   # <-- add this line
    }
```

- [ ] **Step 2: Run the test — expect failure**

```bash
python -m pytest tests/test_exporter.py::TestExportFormatVersion::test_serialize_photo_exact_keys -v 2>&1 | tail -10
```

Expected: FAIL — "Extra keys: set() | Missing keys: {'bp_rating'}".

- [ ] **Step 3: Add `bp_rating` to `serialize_photo` in `poller/exporter.py`**

Find the `return {` dict in `serialize_photo` and add `bp_rating`:

```python
    return {
        "id": row["id"],
        "flickr_id": row.get("flickr_id"),
        "apple_uuid": row.get("uuid"),
        "original_filename": row.get("original_filename"),
        "title": row.get("flickr_title") or "",
        "description": row.get("flickr_description") or "",
        "tags": tags,
        "privacy_state": row["privacy_state"],
        "review_decision": row.get("review_decision"),
        "reviewed_at": row.get("reviewed_at"),
        "date_taken": row.get("date_taken"),
        "location": location,
        "geofenced": bool(row.get("geofence_zone")),
        "faces": faces,
        "albums": album_names,
        "bp_rating": row.get("bp_rating") or 0,
    }
```

- [ ] **Step 4: Update `docs/export-format.md` — add `bp_rating` field**

In the "Version 1 — `photos.ndjson` fields" table, add a new row for `bp_rating`. Find the `albums` row and add after it:

```markdown
| `bp_rating` | integer | no | Star rating 0–5; 0 means unrated |
```

Also update the version history table row to reflect the new field count:

```markdown
| 1 | 2026-05-23 | Initial format: 15 photo fields, 8 zone fields |
```

→ This was 15 fields; it is now 16. Update the row:

```markdown
| 1 | 2026-05-23 | Initial format: 15 photo fields, 8 zone fields |
| 1.1 (additive) | 2026-05-23 | Added `bp_rating` field to photos (#123) |
```

Wait — the version policy says additive fields do NOT bump the version. Instead, just add the field to the table and add a note to the CHANGELOG:

The CHANGELOG entry should read:
```markdown
| 1 | 2026-05-23 | Initial format: 15 photo fields, 8 zone fields |
| 1 (additive) | 2026-05-23 | Added `bp_rating` integer field (0=unrated, 1–5 stars) (#123) |
```

- [ ] **Step 5: Run test — expect pass**

```bash
python -m pytest tests/test_exporter.py::TestExportFormatVersion -v
```

Expected: both tests PASS.

- [ ] **Step 6: Run full suite + lint**

```bash
python -m pytest tests/ -q
make lint
```

- [ ] **Step 7: Commit**

```bash
git add poller/exporter.py tests/test_exporter.py docs/export-format.md
git commit -m "feat: add bp_rating to export format (#123)

- exporter.py: bp_rating added to serialize_photo() output (0=unrated, 1–5)
- test_exporter.py: add bp_rating to TestExportFormatVersion._EXPECTED_PHOTO_KEYS
- docs/export-format.md: document bp_rating field (additive; no version bump)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 7: Reviewer UI — /rate endpoint + star widget (TDD)

**Files:**
- Modify: `reviewer/app.py`
- Modify: `reviewer/templates/review.html`
- Modify: `tests/test_bp_rating.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_bp_rating.py`:

```python
# ===========================================================================
# Task 7 — Reviewer UI: /rate endpoint + star widget
# ===========================================================================


@pytest.fixture()
def rating_flask_client(tmp_path):
    """Flask test client for rating endpoint tests."""
    import reviewer.app as app_module

    db = Database(tmp_path / "test.db")
    db.conn.execute(
        "CREATE TABLE IF NOT EXISTS person_policies "
        "(id INTEGER PRIMARY KEY, person_name TEXT NOT NULL UNIQUE, "
        "policy TEXT NOT NULL, created_at TEXT NOT NULL)"
    )
    db.conn.commit()

    photo_id = db.upsert_photo(
        {
            "uuid": "rate-test-uuid",
            "original_filename": "IMG_RATE.JPG",
            "privacy_state": "candidate_public",
            "apple_persons": [],
            "proposed_tags": [],
        }
    )
    db.close()

    app_module.DATABASE_PATH = str(tmp_path / "test.db")
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as client:
        yield client, photo_id


class TestRateEndpoint(unittest.TestCase):
    """POST /rate/<id> endpoint tests."""

    def _get_client_and_id(self, tmp_path):
        import reviewer.app as app_module

        db = Database(tmp_path / "test.db")
        db.conn.execute(
            "CREATE TABLE IF NOT EXISTS person_policies "
            "(id INTEGER PRIMARY KEY, person_name TEXT NOT NULL UNIQUE, "
            "policy TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        db.conn.commit()
        photo_id = db.upsert_photo(
            {
                "uuid": "rate-test-uuid",
                "original_filename": "IMG_RATE.JPG",
                "privacy_state": "candidate_public",
                "apple_persons": [],
                "proposed_tags": [],
            }
        )
        db.close()
        app_module.DATABASE_PATH = str(tmp_path / "test.db")
        app_module.app.config["TESTING"] = True
        return app_module.app.test_client(), photo_id

    def test_valid_rating_accepted(self):
        """POST /rate/<id> with rating 0–5 returns 200."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            client, photo_id = self._get_client_and_id(Path(tmp))
            with client:
                r = client.post(
                    f"/rate/{photo_id}",
                    json={"rating": 3},
                    content_type="application/json",
                )
                self.assertEqual(r.status_code, 200)
                data = r.get_json()
                self.assertTrue(data["ok"])
                self.assertEqual(data["bp_rating"], 3)

    def test_invalid_rating_rejected(self):
        """POST /rate/<id> with rating outside 0–5 returns 400."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            client, photo_id = self._get_client_and_id(Path(tmp))
            with client:
                r6 = client.post(
                    f"/rate/{photo_id}",
                    json={"rating": 6},
                    content_type="application/json",
                )
                self.assertEqual(r6.status_code, 400)
                r_neg = client.post(
                    f"/rate/{photo_id}",
                    json={"rating": -1},
                    content_type="application/json",
                )
                self.assertEqual(r_neg.status_code, 400)

    def test_rating_updates_db(self):
        """POST /rate/<id> stores the rating in the database."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            client, photo_id = self._get_client_and_id(Path(tmp))
            with client:
                client.post(
                    f"/rate/{photo_id}",
                    json={"rating": 5},
                    content_type="application/json",
                )

            import reviewer.app as app_module

            db = Database(Path(tmp) / "test.db")
            row = db.conn.execute(
                "SELECT bp_rating FROM photos WHERE id = ?", (photo_id,)
            ).fetchone()
            db.close()
            self.assertEqual(row["bp_rating"], 5)

    def test_rating_logs_to_operation_log(self):
        """POST /rate/<id> writes a set_rating entry to operation_log."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            client, photo_id = self._get_client_and_id(Path(tmp))
            with client:
                client.post(
                    f"/rate/{photo_id}",
                    json={"rating": 2},
                    content_type="application/json",
                )

            db = Database(Path(tmp) / "test.db")
            logs = db.get_operation_log(photo_id=photo_id, operation="set_rating")
            db.close()
            self.assertEqual(len(logs), 1)
            self.assertEqual(logs[0]["new_value"], "2")


class TestStarWidgetHTML(unittest.TestCase):
    """Star widget and keyboard shortcut JS must appear in review.html."""

    def _get_review_html(self, tmp_path):
        import reviewer.app as app_module

        db = Database(tmp_path / "test.db")
        db.conn.execute(
            "CREATE TABLE IF NOT EXISTS person_policies "
            "(id INTEGER PRIMARY KEY, person_name TEXT NOT NULL UNIQUE, "
            "policy TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        db.conn.commit()
        db.upsert_photo(
            {
                "uuid": "star-uuid",
                "original_filename": "IMG_STAR.JPG",
                "privacy_state": "candidate_public",
                "apple_persons": [],
                "proposed_tags": [],
            }
        )
        db.close()
        app_module.DATABASE_PATH = str(tmp_path / "test.db")
        app_module.app.config["TESTING"] = True
        with app_module.app.test_client() as client:
            r = client.get("/review?state=candidate_public")
            return r.data.decode()

    def test_star_rating_div_present(self):
        """review.html must contain star-rating div elements."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            html = self._get_review_html(Path(tmp))
            self.assertIn("star-rating", html)

    def test_star_widget_prefilled_from_bp_rating(self):
        """Star widget data-rating attribute is present in review.html."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            html = self._get_review_html(Path(tmp))
            self.assertIn("data-rating=", html)

    def test_keyboard_shortcuts_0_to_5_in_js(self):
        """The keyboard 0–5 shortcut handler must appear in review.html."""
        template_path = (
            Path(__file__).parent.parent / "reviewer" / "templates" / "review.html"
        )
        source = template_path.read_text()
        # The JS block must handle digit keys 0-5 for rating
        self.assertIn("setRating", source)
        self.assertIn("digit >= 0 && digit <= 5", source)

    def test_star_css_present(self):
        """The .star-rating CSS rule must appear in review.html."""
        template_path = (
            Path(__file__).parent.parent / "reviewer" / "templates" / "review.html"
        )
        source = template_path.read_text()
        self.assertIn(".star-rating", source)
```

- [ ] **Step 2: Run tests — expect failures**

```bash
python -m pytest tests/test_bp_rating.py::TestRateEndpoint tests/test_bp_rating.py::TestStarWidgetHTML -v 2>&1 | tail -15
```

Expected: 404 for `/rate/<id>`, no `star-rating` in template.

- [ ] **Step 3: Add `POST /rate/<int:photo_id>` endpoint to `reviewer/app.py`**

Find the end of the routes section in `app.py` (e.g., near the `/api/stats` route). Add the new endpoint:

```python
@app.route("/rate/<int:photo_id>", methods=["POST"])
def rate_photo(photo_id: int):
    """Set a star rating (0–5) on a photo. Writes back to Apple Photos via photoscript."""
    data = request.get_json(silent=True) or {}
    rating = data.get("rating")
    if rating is None:
        return jsonify({"error": "missing rating"}), 400
    try:
        rating = int(rating)
    except (TypeError, ValueError):
        return jsonify({"error": "invalid rating"}), 400
    if not 0 <= rating <= 5:
        return jsonify({"error": "rating must be 0–5"}), 400

    db = _get_db()
    db.set_bp_rating(photo_id, rating)

    # Write-back to Apple Photos (macOS only, fire-and-forget)
    uuid = db.get_photo_uuid(photo_id)
    if uuid:
        try:
            import photoscript  # type: ignore[import]

            photo = photoscript.Photo(uuid)
            photo.favorite = rating >= 1
        except Exception as exc:
            app.logger.warning("photoscript write failed for %s: %s", uuid, exc)

    return jsonify({"ok": True, "bp_rating": rating})
```

**Note:** Check how other routes access the DB in `app.py`. If they use `_get_db()`, use that. If they instantiate `Database(DATABASE_PATH)` directly, do the same. Match the existing pattern exactly.

- [ ] **Step 4: Add star widget to `reviewer/templates/review.html`**

**4a. Add CSS** — find the existing `.pano` CSS block (or the `<style>` section). Add the star rating CSS immediately before the closing `</style>` tag:

```css
/* Star rating widget */
.star-rating {
  margin: 6px 0 4px;
  cursor: pointer;
  font-size: 18px;
  line-height: 1;
  user-select: none;
}
.star-rating .star { color: #555; transition: color 0.1s; }
.star-rating .star.filled { color: #f5a623; }
```

**4b. Add Jinja template** — find the `.photo-card` div inside the `{% for photo in photos %}` loop. Add the star widget immediately before the decision buttons (look for the `<div class="actions">` or similar button container):

```html
<div class="star-rating" data-id="{{ photo.id }}" data-rating="{{ photo.bp_rating }}">
  {% for n in [1, 2, 3, 4, 5] %}
    <span class="star{% if n <= photo.bp_rating %} filled{% endif %}"
          data-value="{{ n }}">★</span>
  {% endfor %}
</div>
```

**4c. Add JavaScript** — find the existing `<script>` block. Add the star widget JS before the closing `</script>` tag:

```javascript
// Star rating widget
function initStarWidgets() {
  document.querySelectorAll('.star-rating').forEach(container => {
    const stars = [...container.querySelectorAll('.star')];
    const current = () => parseInt(container.dataset.rating) || 0;

    stars.forEach((star, idx) => {
      star.addEventListener('mouseover', () => {
        stars.forEach((s, i) => s.classList.toggle('filled', i <= idx));
      });
    });
    container.addEventListener('mouseleave', () => {
      const c = current();
      stars.forEach((s, i) => s.classList.toggle('filled', i < c));
    });

    stars.forEach(star => {
      star.addEventListener('click', e => {
        e.stopPropagation();
        const val = parseInt(star.dataset.value);
        const newRating = val === current() ? 0 : val;
        setRating(parseInt(container.dataset.id), newRating, container);
      });
    });
  });
}

async function setRating(id, rating, container) {
  const r = await fetch(`/rate/${id}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ rating }),
  });
  if (!r.ok) return;
  const d = await r.json();
  if (d.ok) {
    container.dataset.rating = d.bp_rating;
    const stars = [...container.querySelectorAll('.star')];
    stars.forEach((s, i) => s.classList.toggle('filled', i < d.bp_rating));
  }
}

document.addEventListener('DOMContentLoaded', initStarWidgets);

// Keyboard shortcuts: 0–5 to rate the selected card (no auto-advance)
document.addEventListener('keydown', e => {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
  if (!selected) return;
  const digit = parseInt(e.key);
  if (!isNaN(digit) && digit >= 0 && digit <= 5) {
    e.preventDefault();
    const container = selected.querySelector('.star-rating');
    if (container) setRating(+selected.dataset.id, digit, container);
  }
});
```

- [ ] **Step 5: Run tests — expect pass**

```bash
python -m pytest tests/test_bp_rating.py::TestRateEndpoint tests/test_bp_rating.py::TestStarWidgetHTML -v
```

Expected: all 7 tests PASS.

- [ ] **Step 6: Run full suite + lint**

```bash
python -m pytest tests/ -q
make lint
```

- [ ] **Step 7: Commit**

```bash
git add reviewer/app.py reviewer/templates/review.html tests/test_bp_rating.py
git commit -m "feat: reviewer UI star rating widget and /rate endpoint (#123)

- app.py: POST /rate/<id> endpoint; validates 0-5; calls set_bp_rating;
  writes photo.favorite via photoscript (fire-and-forget)
- review.html: star widget CSS, Jinja (prefilled from bp_rating), JS
  (hover preview, click-to-rate, click-same-clears, keyboard 0-5)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 8: Wrap-up

- [ ] **Step 1: Run full test suite — verify all pass**

```bash
python -m pytest tests/ -q
```

Expected: 1063 tests passing (1029 + 34 new).

- [ ] **Step 2: Run lint**

```bash
make lint
```

Expected: no errors.

- [ ] **Step 3: Update README.md test count**

Find the line:

```
| `tests/` | Unit tests (1029 tests) |
```

Replace with:

```
| `tests/` | Unit tests (1063 tests) |
```

Also update the prose description line (search for `1029 tests covering`):

Replace `1029 tests covering` with `1063 tests covering` and add `star ratings (bp_rating column, scanner/poller/UI sync, Flickr tag write-back, reconcile singleton dedup, explain drift reporting),` to the features list.

- [ ] **Step 4: Close GitHub issue #123**

```bash
gh issue close 123 --comment "Implemented in this session.

**What was built:**
- \`bp_rating\` (0–5) column added via migration 022; fresh installs via schema.sql
- Scanner: \`apple_favorite\` field syncs Photos heart flag → DB on every scan
- Poller: parses \`bp:rating=N\` machine tags; seeds DB when unrated; writes back correct tag after each poll (add/remove/replace, never bp:rating=0)
- Reconcile: singleton constraint — \`--fix\` removes duplicate \`bp:rating=*\` tags, logs \`rating_tag_dedup\` to operation journal
- Explain: \`bp reconcile --explain\` reports DB-vs-Flickr rating drift
- Exporter: \`bp_rating\` added to \`serialize_photo()\` output (additive, no version bump)
- Reviewer UI: \`POST /rate/<id>\` endpoint; star widget (hover, click, click-same-clears); keyboard shortcuts 0–5 (no auto-advance); photoscript Photos.favorite write-back
- 34 new tests; all 1063 tests pass"
```

- [ ] **Step 5: Bump version to 1.0.11 in `pyproject.toml`**

Change:

```toml
version = "1.0.10"
```

To:

```toml
version = "1.0.11"
```

- [ ] **Step 6: Commit version bump + README**

```bash
git add README.md pyproject.toml
git commit -m "Bump version to 1.0.11"
```

- [ ] **Step 7: Push to origin**

```bash
git push origin main
```
