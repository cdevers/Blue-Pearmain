# Merge Flickr Donor — Duplicates UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a "Merge into Photos record" action to the duplicates UI so a Flickr-only donor record's identity can be copied onto a Photos-linked target record, with the donor soft-deleted and the group resolved.

**Architecture:** Migration 014 adds `merged_into_id` to `photos`; a new `Database.merge_flickr_donor_in_group()` method does the atomic merge; a new `"merge"` action in the existing `/api/duplicates/<id>/assign` endpoint drives it; the duplicates route passes per-group merge data; the template renders a per-card merge button with inline confirmation.

**Tech Stack:** Python 3.11, SQLite (sqlite3), Flask, Jinja2, unittest + pytest.

**GitHub issue:** #73

---

## Background: key files and patterns

- **`db/db.py` line 111** — `merge_flickr_into_photos()`: existing method that merges a Flickr-only record into a Photos record. The new method follows the same pattern (copy Flickr fields, migrate associated rows, commit internally).
- **`db/schema.sql` line 14** — the `photos` CREATE TABLE. Fresh databases come from this file; ALTER TABLE migrations handle existing installs.
- **`db/migrations/migrate_013_screenshot_flag.py`** — template for migration files (idempotent via `schema_migrations`, PRAGMA table_info check, `run(db_path)` function).
- **`reviewer/app.py` line 469** — `api_dup_assign()`: the endpoint to extend with a `"merge"` action branch.
- **`reviewer/app.py` line 335** — `duplicates()` route: builds `groups` dict; annotates after loop. Merge candidate data goes in the same post-loop annotation block.
- **`reviewer/templates/duplicates.html` line 222** — photo card loop: `{% for photo in group.photos %}`. The merge button and confirm strip go inside `.dup-photo-meta`, after the existing "Make keeper" button.
- **`tests/test_core.py` line 4127** — `TestMergeFlickrIntoPhotos`: reference test class for how to test DB merge logic.
- **`tests/test_review_ui.py` line 515** — `client_with_screenshots` fixture: template for fixtures that need a migration applied before seeding.

**CRITICAL: `duplicate_groups` table and `duplicate_role`/`duplicate_group_id` columns are NOT in `db/schema.sql`** — they are added by `db/migrations/migrate_003_dimensions_and_dedup.py`. Any test that needs them must run that migration. Use the helper pattern `_make_merge_db()` shown in Task 2.

---

## File map

| File | Action |
|------|--------|
| `db/migrations/migrate_014_merged_into_id.py` | **Create** — migration that adds `merged_into_id` column |
| `db/schema.sql` | **Modify** — add `merged_into_id` column to fresh-DB schema |
| `db/db.py` | **Modify** — add `merge_flickr_donor_in_group()` instance method |
| `reviewer/app.py` | **Modify** — new `"merge"` branch in `api_dup_assign()`; compute merge data in `duplicates()` |
| `reviewer/templates/duplicates.html` | **Modify** — merge button + inline confirm strip; CSS; JS |
| `tests/test_core.py` | **Modify** — `TestMigrate014MergedIntoId`, `TestMergeFlickrDonorInGroup` |
| `tests/test_review_ui.py` | **Modify** — `client_with_merge_group` fixture, `TestMergeUI` |
| `README.md` | **Modify** — test count (585 → 603), mention merge action |

---

## Task 1: Migration 014 + schema.sql

**Files:**
- Create: `db/migrations/migrate_014_merged_into_id.py`
- Modify: `db/schema.sql` (line 115 area — after `is_screenshot`)
- Modify: `tests/test_core.py` (append after `TestMigrate013ScreenshotFlag`)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_core.py` (after the last class in the file, around line 7434):

```python
class TestMigrate014MergedIntoId(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "test.db")

    def tearDown(self):
        self._tmp.cleanup()

    def test_migration_adds_merged_into_id_column(self):
        from db.db import Database
        from db.migrations.migrate_014_merged_into_id import run
        db = Database(Path(self.db_path))
        run(self.db_path)
        cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(photos)").fetchall()}
        self.assertIn("merged_into_id", cols)
        db.close()

    def test_migration_is_idempotent(self):
        from db.db import Database
        from db.migrations.migrate_014_merged_into_id import run
        db = Database(Path(self.db_path))
        run(self.db_path)
        run(self.db_path)  # must not raise
        db.close()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_core.py::TestMigrate014MergedIntoId -v
```

Expected: `ModuleNotFoundError: No module named 'db.migrations.migrate_014_merged_into_id'`

- [ ] **Step 3: Create the migration file**

Create `db/migrations/migrate_014_merged_into_id.py`:

```python
"""
migrate_014_merged_into_id.py

Adds:
  photos.merged_into_id INTEGER REFERENCES photos(id)

When the duplicates UI soft-merges a Flickr-only donor record into a
Photos-linked target, the donor row is kept but marked with merged_into_id
pointing to the record it was merged into.

Safe to run multiple times (idempotent — checks schema_migrations).

Usage:
    python db/migrations/migrate_014_merged_into_id.py --config config/config.yml
"""

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import yaml

MIGRATION_NAME = "migrate_014_merged_into_id"


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
        if "merged_into_id" not in existing_cols:
            print("  [dry-run] Would add photos.merged_into_id column")
        else:
            print("  [dry-run] photos.merged_into_id already exists")
        conn.close()
        return

    conn.execute("BEGIN")

    if "merged_into_id" not in existing_cols:
        conn.execute(
            "ALTER TABLE photos ADD COLUMN merged_into_id INTEGER REFERENCES photos(id)"
        )

    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (name, applied_at) VALUES (?, ?)",
        (MIGRATION_NAME, now_iso()),
    )
    conn.commit()
    conn.close()
    print("  Applied:  migrate_014_merged_into_id")


def main() -> int:
    parser = argparse.ArgumentParser(description="Migration 014: add merged_into_id")
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

- [ ] **Step 4: Add `merged_into_id` to `db/schema.sql`**

In `db/schema.sql`, the `photos` table ends around line 115. Find the line:

```
    is_screenshot           INTEGER NOT NULL DEFAULT 0, -- 1 if osxphotos flagged this as a screenshot
    updated_at              TEXT                    -- ISO8601, last time this row was written
```

Replace with:

```
    is_screenshot           INTEGER NOT NULL DEFAULT 0, -- 1 if osxphotos flagged this as a screenshot
    merged_into_id          INTEGER REFERENCES photos(id), -- soft-delete: points to record this donor was merged into
    updated_at              TEXT                    -- ISO8601, last time this row was written
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
python -m pytest tests/test_core.py::TestMigrate014MergedIntoId -v
```

Expected: `2 passed`

- [ ] **Step 6: Run full suite to confirm no regressions**

```bash
python -m pytest tests/ -q
```

Expected: `585 passed`

- [ ] **Step 7: Commit**

```bash
git add db/migrations/migrate_014_merged_into_id.py db/schema.sql tests/test_core.py
git commit -m "Migration 014: add merged_into_id column for soft-delete donor tracking

Closes part of #73

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 2: `merge_flickr_donor_in_group` DB method

**Files:**
- Modify: `db/db.py` (after `merge_flickr_into_photos`, around line 234)
- Modify: `tests/test_core.py` (append new class)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_core.py` (after `TestMigrate014MergedIntoId`):

```python
class TestMergeFlickrDonorInGroup(unittest.TestCase):
    """Database.merge_flickr_donor_in_group() must correctly soft-merge a
    Flickr-only donor into a Photos-linked target and resolve the group."""

    def _make_merge_db(self):
        """Create a temp DB with duplicate_groups support and a ready-to-merge group."""
        from db.db import Database
        from db.migrations.migrate_003_dimensions_and_dedup import run as migrate_003
        import shutil
        tmp = tempfile.mkdtemp()
        db_path = str(Path(tmp) / "test.db")
        db = Database(Path(db_path))
        migrate_003(db_path)  # adds duplicate_groups table + duplicate_role/duplicate_group_id columns

        # Flickr-only donor: flickr_id set, no uuid
        donor_id = db.upsert_photo({
            "flickr_id":            "F001",
            "flickr_secret":        "sec123",
            "flickr_server":        "65535",
            "flickr_farm":          66,
            "original_filename":    "IMG_9999.JPG",
            "date_taken":           "2024-06-15 12:00:00",
            "date_uploaded_flickr": "2024-06-15 18:00:00",
            "privacy_state":        "candidate_public",
        })

        # Photos-linked target: uuid set, no flickr_id
        target_id = db.upsert_photo({
            "uuid":              "U001",
            "original_filename": "IMG_9999.JPG",
            "date_taken":        "2024-06-15T12:00:00-04:00",
            "privacy_state":     "candidate_public",
            "width":             4000,
            "height":            3000,
            "apple_labels":      [],
            "apple_persons":     [],
            "proposed_tags":     [],
        })

        # Create duplicate group and link both photos to it
        db.conn.execute(
            """INSERT INTO duplicate_groups (match_key, group_type, photo_count, notes)
               VALUES (?, ?, ?, ?)""",
            ("IMG_9999.JPG|2024-06-15 12:00:00", "snapbridge", 2, ""),
        )
        group_id = db.conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        db.conn.execute(
            "UPDATE photos SET duplicate_group_id = ?, duplicate_role = 'discard' WHERE id = ?",
            (group_id, donor_id),
        )
        db.conn.execute(
            "UPDATE photos SET duplicate_group_id = ?, duplicate_role = 'keeper' WHERE id = ?",
            (group_id, target_id),
        )
        db.conn.commit()

        return db, tmp, donor_id, target_id, group_id

    def setUp(self):
        self.db, self._tmp, self.donor_id, self.target_id, self.group_id = self._make_merge_db()

    def tearDown(self):
        self.db.close()
        import shutil
        shutil.rmtree(self._tmp)

    def _row(self, photo_id):
        return self.db.get_photo(photo_id)

    def _group(self):
        return self.db.conn.execute(
            "SELECT * FROM duplicate_groups WHERE id = ?", (self.group_id,)
        ).fetchone()

    def test_flickr_id_copied_to_target(self):
        self.db.merge_flickr_donor_in_group(self.donor_id, self.target_id, self.group_id)
        self.assertEqual(self._row(self.target_id)["flickr_id"], "F001")

    def test_flickr_secret_and_date_uploaded_copied_to_target(self):
        self.db.merge_flickr_donor_in_group(self.donor_id, self.target_id, self.group_id)
        row = self._row(self.target_id)
        self.assertEqual(row["flickr_secret"], "sec123")
        self.assertIsNotNone(row["date_uploaded_flickr"])

    def test_donor_flickr_id_is_null_after_merge(self):
        self.db.merge_flickr_donor_in_group(self.donor_id, self.target_id, self.group_id)
        self.assertIsNone(self._row(self.donor_id)["flickr_id"])

    def test_donor_merged_into_id_points_to_target(self):
        self.db.merge_flickr_donor_in_group(self.donor_id, self.target_id, self.group_id)
        self.assertEqual(self._row(self.donor_id)["merged_into_id"], self.target_id)

    def test_donor_privacy_state_is_duplicate_flickr_and_role_is_discard(self):
        self.db.merge_flickr_donor_in_group(self.donor_id, self.target_id, self.group_id)
        row = self._row(self.donor_id)
        self.assertEqual(row["privacy_state"], "duplicate_flickr")
        self.assertEqual(row["duplicate_role"], "discard")

    def test_target_duplicate_role_is_keeper(self):
        self.db.merge_flickr_donor_in_group(self.donor_id, self.target_id, self.group_id)
        self.assertEqual(self._row(self.target_id)["duplicate_role"], "keeper")

    def test_group_resolved_with_correct_keeper(self):
        self.db.merge_flickr_donor_in_group(self.donor_id, self.target_id, self.group_id)
        g = self._group()
        self.assertEqual(g["resolved"], 1)
        self.assertEqual(g["keeper_id"], self.target_id)

    def test_photo_albums_migrated_to_target(self):
        album_id = self.db.upsert_album("apple-a", "Test Album")
        self.db.upsert_photo_album(self.donor_id, album_id)
        self.db.merge_flickr_donor_in_group(self.donor_id, self.target_id, self.group_id)
        row = self.db.conn.execute(
            "SELECT * FROM photo_albums WHERE photo_id = ? AND album_id = ?",
            (self.target_id, album_id),
        ).fetchone()
        self.assertIsNotNone(row)

    def test_tag_events_migrated_to_target_and_removed_from_donor(self):
        self.db.conn.execute(
            """INSERT INTO tag_events (photo_id, event_at, destination, tags_before, tags_after, success)
               VALUES (?, '2024-06-15T18:00:00Z', 'flickr', '[]', '["travel"]', 1)""",
            (self.donor_id,),
        )
        self.db.conn.commit()
        self.db.merge_flickr_donor_in_group(self.donor_id, self.target_id, self.group_id)
        on_target = self.db.conn.execute(
            "SELECT COUNT(*) AS n FROM tag_events WHERE photo_id = ?", (self.target_id,)
        ).fetchone()["n"]
        on_donor = self.db.conn.execute(
            "SELECT COUNT(*) AS n FROM tag_events WHERE photo_id = ?", (self.donor_id,)
        ).fetchone()["n"]
        self.assertEqual(on_target, 1)
        self.assertEqual(on_donor, 0)

    def test_raises_value_error_if_donor_has_uuid(self):
        with self.assertRaises(ValueError):
            # Pass target as donor — it has a uuid
            self.db.merge_flickr_donor_in_group(self.target_id, self.donor_id, self.group_id)

    def test_raises_value_error_if_target_has_no_uuid(self):
        with self.assertRaises(ValueError):
            # Pass donor as both donor and target — it has no uuid
            self.db.merge_flickr_donor_in_group(self.donor_id, self.donor_id, self.group_id)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_core.py::TestMergeFlickrDonorInGroup -v
```

Expected: `AttributeError: 'Database' object has no attribute 'merge_flickr_donor_in_group'`

- [ ] **Step 3: Implement `merge_flickr_donor_in_group` in `db/db.py`**

In `db/db.py`, insert the new method directly after `merge_flickr_into_photos` (after the blank line at line 234, before `_ensure_schema`):

```python
    _FLICKR_COPY_FIELDS: list[str] = [
        "flickr_id", "flickr_secret", "flickr_server", "flickr_farm",
        "date_uploaded_flickr", "tags_pushed_flickr", "perms_pushed_flickr",
        "flickr_deleted", "flickr_title", "flickr_description", "flickr_tags",
        "flickr_tags_hash", "flickr_last_updated", "meta_synced_flickr_at",
        "tags_truncated_for_flickr", "display_rotation",
    ]

    def merge_flickr_donor_in_group(
        self, donor_id: int, target_id: int, group_id: int
    ) -> None:
        """
        Soft-merge a Flickr-only donor record into a Photos-linked target record.

        Copies all Flickr identity fields from donor → target, migrates
        photo_albums/tag_events/metadata_conflicts, then soft-deletes the donor
        (sets merged_into_id, privacy_state='duplicate_flickr', duplicate_role='discard')
        and resolves the duplicate group.

        Raises ValueError if preconditions are not met.
        """
        donor = self.conn.execute(
            "SELECT * FROM photos WHERE id = ?", (donor_id,)
        ).fetchone()
        target = self.conn.execute(
            "SELECT * FROM photos WHERE id = ?", (target_id,)
        ).fetchone()

        if not donor:
            raise ValueError(f"donor {donor_id} not found")
        if not donor["flickr_id"]:
            raise ValueError(f"donor {donor_id} has no flickr_id")
        if donor["uuid"] is not None:
            raise ValueError(f"donor {donor_id} has a uuid — only Flickr-only records can be donors")
        if not target:
            raise ValueError(f"target {target_id} not found")
        if target["uuid"] is None:
            raise ValueError(f"target {target_id} has no uuid — only Photos-linked records can be targets")

        donor = dict(donor)

        # 1. Migrate album memberships
        albums = self.conn.execute(
            "SELECT album_id, flickr_pushed, pushed_at FROM photo_albums WHERE photo_id = ?",
            (donor_id,),
        ).fetchall()
        for a in albums:
            self.conn.execute(
                """INSERT OR IGNORE INTO photo_albums (photo_id, album_id, flickr_pushed, pushed_at)
                   VALUES (?, ?, ?, ?)""",
                (target_id, a["album_id"], a["flickr_pushed"], a["pushed_at"]),
            )

        # 2. Migrate tag_events — DELETE + re-INSERT to work around SQLite FK/ALTER TABLE bug
        tag_rows = self.conn.execute(
            "SELECT event_at, destination, tags_before, tags_after, success, error "
            "FROM tag_events WHERE photo_id = ?",
            (donor_id,),
        ).fetchall()
        if tag_rows:
            self.conn.execute("DELETE FROM tag_events WHERE photo_id = ?", (donor_id,))
            for t in tag_rows:
                self.conn.execute(
                    """INSERT INTO tag_events
                       (photo_id, event_at, destination, tags_before, tags_after, success, error)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (target_id, t["event_at"], t["destination"],
                     t["tags_before"], t["tags_after"], t["success"], t["error"]),
                )

        # 3. Migrate metadata_conflicts
        conflicts = self.conn.execute(
            "SELECT * FROM metadata_conflicts WHERE photo_id = ?",
            (donor_id,),
        ).fetchall()
        for c in conflicts:
            self.conn.execute(
                """INSERT OR IGNORE INTO metadata_conflicts
                   (photo_id, field, flickr_value, photos_value,
                    resolved, resolution, resolved_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (target_id, c["field"], c["flickr_value"], c["photos_value"],
                 c["resolved"], c["resolution"], c["resolved_at"], c["created_at"]),
            )

        # 4. Build set of Flickr fields to copy to target (skip nulls)
        update = {f: donor[f] for f in self._FLICKR_COPY_FIELDS if donor.get(f) is not None}
        update["updated_at"] = _now_iso()

        # 5. Clear flickr_id on donor BEFORE writing it to target (UNIQUE constraint)
        self.conn.execute("UPDATE photos SET flickr_id = NULL WHERE id = ?", (donor_id,))

        # 6. Copy Flickr fields to target
        if update:
            placeholders = ", ".join(f"{k} = ?" for k in update)
            self.conn.execute(
                f"UPDATE photos SET {placeholders} WHERE id = ?",
                list(update.values()) + [target_id],
            )

        # 7. Soft-delete donor
        self.conn.execute(
            """UPDATE photos
               SET merged_into_id = ?, privacy_state = 'duplicate_flickr', duplicate_role = 'discard'
               WHERE id = ?""",
            (target_id, donor_id),
        )

        # 8. Promote target role
        self.conn.execute(
            "UPDATE photos SET duplicate_role = 'keeper' WHERE id = ?", (target_id,)
        )

        # 9. Resolve the duplicate group
        self.conn.execute(
            """UPDATE duplicate_groups
               SET resolved = 1, keeper_id = ?, resolved_at = datetime('now')
               WHERE id = ?""",
            (target_id, group_id),
        )

        self.conn.commit()
```

Note: `_FLICKR_COPY_FIELDS` is a class-level list defined just before the method, inside the `Database` class body.

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_core.py::TestMergeFlickrDonorInGroup -v
```

Expected: `11 passed`

- [ ] **Step 5: Run full suite to confirm no regressions**

```bash
python -m pytest tests/ -q
```

Expected: `597 passed` (585 + 2 from Task 1 + 11 from this task — wait: 585 + 2 = 587, + 11 = 598; recount: Task 1 adds 2, Task 2 adds 11, running total 585+2+11 = 598)

- [ ] **Step 6: Commit**

```bash
git add db/db.py tests/test_core.py
git commit -m "feat: add merge_flickr_donor_in_group DB method (#73)

Soft-merges a Flickr-only donor record into a Photos-linked target:
copies Flickr fields, migrates album/tag/conflict rows, nulls donor's
flickr_id, sets merged_into_id, resolves duplicate group.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 3: API `merge` action

**Files:**
- Modify: `reviewer/app.py` (lines 507–517 — `api_dup_assign`, before the `else` clause)
- Modify: `tests/test_review_ui.py` (append fixture + class)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_review_ui.py`:

```python
@pytest.fixture
def client_with_merge_group():
    """DB with one unresolved snapbridge group: Flickr-only donor + Photos-linked target."""
    with tempfile.TemporaryDirectory() as tmp:
        from db.migrations.migrate_003_dimensions_and_dedup import run as migrate_003
        db_path = Path(tmp) / "test.db"
        test_db = Database(db_path)
        migrate_003(str(db_path))  # creates duplicate_groups table + duplicate_role/duplicate_group_id

        # Flickr-only donor
        donor_id = test_db.upsert_photo({
            "flickr_id":         "F001",
            "flickr_secret":     "sec",
            "flickr_server":     "65535",
            "original_filename": "IMG_999.JPG",
            "date_taken":        "2024-06-15 12:00:00",
            "privacy_state":     "candidate_public",
        })

        # Photos-linked target (higher-res)
        target_id = test_db.upsert_photo({
            "uuid":              "U001",
            "original_filename": "IMG_999.JPG",
            "date_taken":        "2024-06-15T12:00:00-04:00",
            "privacy_state":     "candidate_public",
            "width":             4000,
            "height":            3000,
            "apple_labels":      [],
            "apple_persons":     [],
            "proposed_tags":     [],
        })

        # Link both to a duplicate group
        test_db.conn.execute(
            "INSERT INTO duplicate_groups (match_key, group_type, photo_count) VALUES (?,?,?)",
            ("IMG_999.JPG|2024-06-15 12:00:00", "snapbridge", 2),
        )
        group_id = test_db.conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        test_db.conn.execute(
            "UPDATE photos SET duplicate_group_id = ?, duplicate_role = 'discard' WHERE id = ?",
            (group_id, donor_id),
        )
        test_db.conn.execute(
            "UPDATE photos SET duplicate_group_id = ?, duplicate_role = 'keeper' WHERE id = ?",
            (group_id, target_id),
        )
        test_db.conn.commit()

        app_module._db = test_db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test-secret"

        with app_module.app.test_client() as c:
            yield c, test_db, group_id, donor_id, target_id

        app_module._db = None


class TestMergeUI:
    """API and UI tests for the duplicate merge action."""

    def test_merge_action_returns_ok(self, client_with_merge_group):
        c, db, group_id, donor_id, target_id = client_with_merge_group
        resp = c.post(
            f"/api/duplicates/{group_id}/assign",
            json={"action": "merge", "donor_id": donor_id, "target_id": target_id},
        )
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_merge_with_photo_not_in_group_returns_400(self, client_with_merge_group):
        c, db, group_id, donor_id, target_id = client_with_merge_group
        resp = c.post(
            f"/api/duplicates/{group_id}/assign",
            json={"action": "merge", "donor_id": 99999, "target_id": target_id},
        )
        assert resp.status_code == 400

    def test_merge_with_donor_having_uuid_returns_400(self, client_with_merge_group):
        c, db, group_id, donor_id, target_id = client_with_merge_group
        # target_id has a uuid — passing it as the donor must be rejected
        resp = c.post(
            f"/api/duplicates/{group_id}/assign",
            json={"action": "merge", "donor_id": target_id, "target_id": donor_id},
        )
        assert resp.status_code == 400
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_review_ui.py::TestMergeUI -v
```

Expected: `FAILED — invalid action` (the endpoint returns 400 "invalid action" for the unknown `"merge"` action)

- [ ] **Step 3: Add the `"merge"` branch to `api_dup_assign` in `reviewer/app.py`**

In `reviewer/app.py`, find the `api_dup_assign` function (line 469). Replace the `else` clause at the end:

```python
    elif action == "not_duplicate":
        db().conn.execute(
            "UPDATE photos SET duplicate_group_id = NULL, duplicate_role = NULL WHERE duplicate_group_id = ?",
            (group_id,),
        )
        db().conn.execute("DELETE FROM duplicate_groups WHERE id = ?", (group_id,))
        db().conn.commit()
        return jsonify({"ok": True})

    else:
        return jsonify({"ok": False, "error": "invalid action"}), 400
```

Replace with:

```python
    elif action == "not_duplicate":
        db().conn.execute(
            "UPDATE photos SET duplicate_group_id = NULL, duplicate_role = NULL WHERE duplicate_group_id = ?",
            (group_id,),
        )
        db().conn.execute("DELETE FROM duplicate_groups WHERE id = ?", (group_id,))
        db().conn.commit()
        return jsonify({"ok": True})

    elif action == "merge":
        donor_id  = data.get("donor_id")
        target_id = data.get("target_id")
        if not donor_id or not target_id:
            return jsonify({"ok": False, "error": "missing donor_id or target_id"}), 400
        for pid in (donor_id, target_id):
            member = db().conn.execute(
                "SELECT id FROM photos WHERE id = ? AND duplicate_group_id = ?",
                (pid, group_id),
            ).fetchone()
            if not member:
                return jsonify({"ok": False, "error": f"photo {pid} not in group"}), 400
        try:
            db().merge_flickr_donor_in_group(donor_id, target_id, group_id)
            return jsonify({"ok": True})
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    else:
        return jsonify({"ok": False, "error": "invalid action"}), 400
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_review_ui.py::TestMergeUI -v
```

Expected: `3 passed`

- [ ] **Step 5: Run full suite to confirm no regressions**

```bash
python -m pytest tests/ -q
```

Expected: `601 passed` (598 + 3)

- [ ] **Step 6: Commit**

```bash
git add reviewer/app.py tests/test_review_ui.py
git commit -m "feat: add merge action to /api/duplicates/<id>/assign (#73)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 4: Route data + template merge button

**Files:**
- Modify: `reviewer/app.py` (post-loop annotation block in `duplicates()`, lines 413–418)
- Modify: `reviewer/templates/duplicates.html` (CSS, photo card, JS)
- Modify: `tests/test_review_ui.py` (add 2 tests to `TestMergeUI`)

- [ ] **Step 1: Write the failing tests**

Add two more test methods to `TestMergeUI` in `tests/test_review_ui.py`:

```python
    def test_merge_button_shown_on_flickr_only_card(self, client_with_merge_group):
        c, db, group_id, donor_id, target_id = client_with_merge_group
        resp = c.get("/duplicates")
        assert resp.status_code == 200
        assert b"Merge into Photos record" in resp.data

    def test_merge_button_appears_exactly_once(self, client_with_merge_group):
        c, db, group_id, donor_id, target_id = client_with_merge_group
        resp = c.get("/duplicates")
        # Only the Flickr-only card (donor) should have the button; the Photos-linked card should not
        assert resp.data.decode().count("Merge into Photos record") == 1
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest "tests/test_review_ui.py::TestMergeUI::test_merge_button_shown_on_flickr_only_card" \
                 "tests/test_review_ui.py::TestMergeUI::test_merge_button_appears_exactly_once" -v
```

Expected: `FAILED — "Merge into Photos record" not in response` (route doesn't pass merge data yet)

- [ ] **Step 3: Extend the `duplicates()` route to pass merge candidate data**

In `reviewer/app.py`, find the post-loop annotation block (after the rows loop, around line 413):

```python
    # Annotate each group with whether all its photos have a usable thumbnail
    for g in groups.values():
        g["has_all_thumbs"] = all(
            p["thumbnail_path"] or (p["flickr_secret"] and p["flickr_server"])
            for p in g["photos"]
        )
```

Replace with:

```python
    # Annotate each group with thumbnail availability and merge candidate data
    for g in groups.values():
        g["has_all_thumbs"] = all(
            p["thumbnail_path"] or (p["flickr_secret"] and p["flickr_server"])
            for p in g["photos"]
        )
        # Photos-linked records sorted highest-res first (merge targets)
        photos_targets = sorted(
            [p for p in g["photos"] if p.get("uuid")],
            key=lambda p: (p.get("width") or 0) * (p.get("height") or 0),
            reverse=True,
        )
        g["flickr_only_ids"] = {
            p["id"] for p in g["photos"] if p.get("flickr_id") and not p.get("uuid")
        }
        g["photos_targets"] = [
            {
                "id": p["id"],
                "label": (
                    f"{p['original_filename']} ({p['width']}×{p['height']}px)"
                    if p.get("width") and p.get("height")
                    else p["original_filename"]
                ),
            }
            for p in photos_targets
        ]
```

- [ ] **Step 4: Add CSS to `reviewer/templates/duplicates.html`**

In `duplicates.html`, find the `{% endblock %}` that closes `{% block extra_style %}` (around line 170). Just before it, add:

```css
.btn-merge {
  font-size: 12px;
  padding: 4px 10px;
  color: #6ab4f5;
  margin-top: 4px;
}
.merge-confirm {
  margin-top: 6px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 12px;
  padding: 8px;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius);
}
.merge-confirm label { color: var(--text); }
.merge-confirm select { font-size: 12px; background: var(--bg); color: var(--text); border: 1px solid var(--border); border-radius: 4px; padding: 2px 4px; }
.merge-cancel { font-size: 12px; color: var(--muted); text-decoration: none; }
.merge-cancel:hover { color: var(--text); }
```

- [ ] **Step 5: Add the merge button to the photo card in `duplicates.html`**

Find the section inside `{% for photo in group.photos %}` that contains the "Make keeper" button (around line 245):

```html
            <div class="dup-make-keeper">
              <button class="btn" style="font-size:12px; padding:4px 10px"
                      onclick="assignKeeper({{ group.id }}, {{ photo.id }}, this)">
                Make keeper
              </button>
            </div>
```

Replace with:

```html
            <div class="dup-make-keeper">
              <button class="btn" style="font-size:12px; padding:4px 10px"
                      onclick="assignKeeper({{ group.id }}, {{ photo.id }}, this)">
                Make keeper
              </button>
            </div>
            {% if photo.id in group.flickr_only_ids and group.photos_targets %}
            <div class="dup-merge-wrap">
              <button class="btn btn-merge"
                      onclick="showMergeConfirm(this, {{ group.id }}, {{ photo.id }},
                               {{ group.photos_targets | tojson }})">
                Merge into Photos record
              </button>
              <div class="merge-confirm" style="display:none">
                <label>Into:
                  <select class="merge-target-select"></select>
                </label>
                <button class="btn btn-primary merge-confirm-btn">Confirm merge</button>
                <a class="merge-cancel" href="#">Cancel</a>
              </div>
            </div>
            {% endif %}
```

- [ ] **Step 6: Add JS functions to `duplicates.html`**

In the `{% block scripts %}` section, find `async function notDuplicate(groupId, btn) {` and add the following two functions before it:

```javascript
function showMergeConfirm(btn, groupId, donorId, targets) {
  const wrap = btn.closest('.dup-merge-wrap');
  btn.style.display = 'none';
  const confirm = wrap.querySelector('.merge-confirm');
  const sel = wrap.querySelector('.merge-target-select');
  sel.innerHTML = targets.map(t =>
    `<option value="${t.id}">${t.label}</option>`
  ).join('');
  confirm.style.display = '';
  wrap.querySelector('.merge-confirm-btn').onclick =
    () => confirmMerge(groupId, donorId, wrap);
  wrap.querySelector('.merge-cancel').onclick = e => {
    e.preventDefault();
    confirm.style.display = 'none';
    btn.style.display = '';
  };
}

async function confirmMerge(groupId, donorId, wrap) {
  const targetId = parseInt(wrap.querySelector('.merge-target-select').value, 10);
  const confirmBtn = wrap.querySelector('.merge-confirm-btn');
  confirmBtn.disabled = true;
  const r = await apiFetch(`/api/duplicates/${groupId}/assign`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({action: 'merge', donor_id: donorId, target_id: targetId}),
  });
  const d = await r.json();
  if (d.ok) {
    toast('Merged — Flickr identity moved to Photos record');
    const card = document.getElementById(`group-${groupId}`);
    card.style.opacity = '0.4';
    card.querySelectorAll('button').forEach(b => b.disabled = true);
    refreshStats();
  } else {
    toast('Error: ' + (d.error || 'unknown'), 'err');
    confirmBtn.disabled = false;
  }
}
```

- [ ] **Step 7: Run tests to verify they pass**

```bash
python -m pytest tests/test_review_ui.py::TestMergeUI -v
```

Expected: `5 passed`

- [ ] **Step 8: Run full suite to confirm no regressions**

```bash
python -m pytest tests/ -q
```

Expected: `603 passed`

- [ ] **Step 9: Commit**

```bash
git add reviewer/app.py reviewer/templates/duplicates.html tests/test_review_ui.py
git commit -m "feat: merge button + inline confirm in duplicates UI (#73)

Flickr-only cards show 'Merge into Photos record' button when the group
contains at least one Photos-linked record. Clicking reveals an inline
confirm strip with a target dropdown (highest-res first).

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 5: README + docs update

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Update test count in README**

In `README.md`, find the line containing `585 tests` and update the count to `603`. Also find the sentence describing what tests cover and append the new coverage. Find:

```
585 tests covering the privacy classifier, ...
```

Replace `585` with `603` and append to the end of the coverage sentence (before the closing period):

```
, and duplicate merge action (soft-merging a Flickr-only donor record into a Photos-linked target record, copying Flickr identity fields and resolving the group)
```

- [ ] **Step 2: Run the full suite one final time**

```bash
python -m pytest tests/ -q
```

Expected: `603 passed`

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "Docs: update test count to 603, note merge action (GH #73)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Self-review checklist

**Spec coverage:**
- ✅ Migration 014 adds `merged_into_id` — Task 1
- ✅ `schema.sql` updated for fresh DBs — Task 1
- ✅ `merge_flickr_donor_in_group` copies all Flickr fields — Task 2
- ✅ `photo_albums`, `tag_events`, `metadata_conflicts` migrated — Task 2
- ✅ Donor `flickr_id` nulled before copying (UNIQUE constraint) — Task 2
- ✅ Donor soft-deleted: `merged_into_id`, `duplicate_flickr`, `discard` — Task 2
- ✅ Group resolved with `keeper_id` — Task 2
- ✅ API `merge` action with validation — Task 3
- ✅ ValueError from method surfaces as 400 — Task 3
- ✅ Route passes `flickr_only_ids` + `photos_targets` — Task 4
- ✅ Merge button on Flickr-only cards only — Task 4
- ✅ Inline confirm strip with target dropdown (highest-res first) — Task 4
- ✅ JS `showMergeConfirm` / `confirmMerge` — Task 4
- ✅ README test count updated — Task 5

**Type consistency:** `donor_id`, `target_id`, `group_id` are integers throughout. `photos_targets` is a list of `{id, label}` dicts — consistent between route and JS template. `flickr_only_ids` is a Python set — Jinja2's `in` operator handles sets correctly.

**No placeholders found.**
