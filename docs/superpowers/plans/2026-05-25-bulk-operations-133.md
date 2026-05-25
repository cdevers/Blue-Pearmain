# Bulk Operations Implementation Plan — Issue #133

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `/library` page with multi-select and bulk title/description/tag editing that queues proposals, plus a lightweight select mode in `/review`, both backed by a new `bulk_batches` DB table that groups proposals for audit and batch-reject.

**Architecture:** New `/library` route with horizontal filter bar, checkbox selection (manual + filter-based "select all"), and an inline edit panel that stays above the visible grid. Bulk edits call `POST /api/bulk-edit` which resolves the photo set, creates a `bulk_batches` record, and inserts `metadata_proposals` with `source='manual'`, `target='flickr'`, `conflict_type='non_conflict'`, linked by a new nullable `batch_id` FK. The `/proposals` page gains a batch-summary section; a batch-reject endpoint closes all proposals in a batch. The `/review` queue gets a lightweight select toggle reusing the same action bar and panel JS pattern.

**Tech Stack:** SQLite (new table + ALTER TABLE), Python (db.py, app.py), Jinja2/CSS/JS (library.html, base.html, proposals.html, review.html).

**Design spec:** `docs/superpowers/specs/2026-05-24-bulk-operations-design.md`

---

## File Map

| File | Change |
|---|---|
| `db/migrations/migrate_023_bulk_batches.py` | New — bulk_batches table + batch_id on proposals |
| `db/schema.sql` | Add `bulk_batches` table definition |
| `db/db.py` | `_ensure_schema` guard; 8 new methods |
| `reviewer/app.py` | `/library`, `/api/bulk-edit`, `/api/bulk-batches/<id>/reject` routes |
| `reviewer/templates/library.html` | New — library view template |
| `reviewer/templates/base.html` | Add Library nav link (key 8) |
| `reviewer/templates/proposals.html` | Add batch-summary section at top |
| `reviewer/templates/review.html` | Add select-mode toggle + action bar |
| `tests/test_bulk_operations.py` | New — all tests for this feature |
| `README.md` | Update feature list + test count |

---

### Task 1: Migration 023 + schema (TDD)

**Files:**
- New: `db/migrations/migrate_023_bulk_batches.py`
- Modify: `db/schema.sql`
- Modify: `db/db.py` (`_ensure_schema`)
- New: `tests/test_bulk_operations.py`

- [ ] **Step 1: Write the failing migration tests**

Create `tests/test_bulk_operations.py`:

```python
"""
tests/test_bulk_operations.py — tests for bulk operations (#133)

Run from repo root:
    python -m pytest tests/test_bulk_operations.py -v
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.db import Database


# ===========================================================================
# Task 1 — Migration 023
# ===========================================================================


def _import_migration_023():
    spec = importlib.util.spec_from_file_location(
        "migrate_023_bulk_batches",
        Path(__file__).parent.parent / "db" / "migrations" / "migrate_023_bulk_batches.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestMigration023(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "test.db")
        conn = sqlite3.connect(self.db_path)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS schema_migrations
                (id INTEGER PRIMARY KEY, name TEXT UNIQUE, applied_at TEXT);
            CREATE TABLE IF NOT EXISTS photos (id INTEGER PRIMARY KEY, uuid TEXT);
            CREATE TABLE IF NOT EXISTS metadata_proposals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                photo_id INTEGER NOT NULL REFERENCES photos(id),
                field TEXT NOT NULL,
                proposed_value TEXT,
                source TEXT NOT NULL,
                target TEXT NOT NULL,
                conflict_type TEXT NOT NULL,
                source_hash_at_creation TEXT,
                target_hash_at_creation TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                resolved_at TEXT,
                resolution_note TEXT
            );
        """)
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_creates_bulk_batches_table(self):
        mod = _import_migration_023()
        mod.run(self.db_path)
        conn = sqlite3.connect(self.db_path)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()
        self.assertIn("bulk_batches", tables)

    def test_adds_batch_id_to_proposals(self):
        mod = _import_migration_023()
        mod.run(self.db_path)
        conn = sqlite3.connect(self.db_path)
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(metadata_proposals)"
        ).fetchall()}
        conn.close()
        self.assertIn("batch_id", cols)

    def test_batch_id_is_nullable(self):
        """Existing proposals survive migration with batch_id=NULL."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO photos (uuid) VALUES ('u1')")
        conn.execute("""INSERT INTO metadata_proposals
            (photo_id, field, source, target, conflict_type, status, created_at)
            VALUES (1, 'title', 'flickr', 'photos', 'non_conflict', 'pending', '2026-01-01')""")
        conn.commit()
        conn.close()
        mod = _import_migration_023()
        mod.run(self.db_path)
        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT batch_id FROM metadata_proposals WHERE id=1").fetchone()
        conn.close()
        self.assertIsNone(row[0])

    def test_migration_idempotent(self):
        mod = _import_migration_023()
        mod.run(self.db_path)
        mod.run(self.db_path)  # must not raise

    def test_bulk_batches_columns(self):
        mod = _import_migration_023()
        mod.run(self.db_path)
        conn = sqlite3.connect(self.db_path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(bulk_batches)").fetchall()}
        conn.close()
        self.assertGreaterEqual(
            cols,
            {"id", "operation", "field", "value", "tags", "filter", "photo_count", "created_at"},
        )
```

- [ ] **Step 2: Run tests to confirm failure**

```bash
python -m pytest tests/test_bulk_operations.py::TestMigration023 -v
```

Expected: `ERROR` — migration file not found.

- [ ] **Step 3: Create the migration**

Create `db/migrations/migrate_023_bulk_batches.py`:

```python
"""
migrate_023_bulk_batches.py

Adds:
  bulk_batches table — one row per confirmed bulk edit operation
  metadata_proposals.batch_id INTEGER (nullable FK → bulk_batches.id)

Safe to run multiple times (idempotent).

Usage:
    python db/migrations/migrate_023_bulk_batches.py --config config/config.yml
"""

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import yaml

MIGRATION_NAME = "migrate_023_bulk_batches"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run(db_path: str, dry_run: bool = False) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

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

    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    proposal_cols = {
        r[1]
        for r in conn.execute("PRAGMA table_info(metadata_proposals)").fetchall()
    }

    if dry_run:
        if "bulk_batches" not in tables:
            print("  [dry-run] Would create bulk_batches table")
        if "batch_id" not in proposal_cols:
            print("  [dry-run] Would add metadata_proposals.batch_id column")
        conn.close()
        return

    conn.execute("BEGIN")

    if "bulk_batches" not in tables:
        conn.execute("""
            CREATE TABLE bulk_batches (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                operation   TEXT NOT NULL,
                field       TEXT,
                value       TEXT,
                tags        TEXT,
                filter      TEXT,
                photo_count INTEGER NOT NULL,
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)

    if "batch_id" not in proposal_cols:
        conn.execute(
            "ALTER TABLE metadata_proposals ADD COLUMN batch_id INTEGER REFERENCES bulk_batches(id)"
        )

    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (name, applied_at) VALUES (?, ?)",
        (MIGRATION_NAME, now_iso()),
    )
    conn.commit()
    conn.close()
    print("  Applied:  migrate_023_bulk_batches")


def main() -> int:
    parser = argparse.ArgumentParser(description="Migration 023: bulk_batches table")
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

- [ ] **Step 4: Add `bulk_batches` to `db/schema.sql`**

After the `metadata_proposals` block, add:

```sql
-- ============================================================
-- Bulk operation batches: groups proposals created by bulk edits
-- ============================================================

CREATE TABLE IF NOT EXISTS bulk_batches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    operation   TEXT NOT NULL,   -- 'set_title' | 'set_description' | 'tags_add' | 'tags_remove'
    field       TEXT,            -- 'title' | 'description' | NULL for tag ops
    value       TEXT,            -- new text value for title/description ops
    tags        TEXT,            -- JSON array of tag strings for tag ops
    filter      TEXT,            -- JSON filter object if filter-based selection, else NULL
    photo_count INTEGER NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
```

And add `batch_id` to the `metadata_proposals` table definition in schema.sql (after `resolution_note TEXT`):

```sql
    batch_id                INTEGER REFERENCES bulk_batches(id)
```

- [ ] **Step 5: Add `_ensure_schema` guard in `db/db.py`**

In the `_ensure_schema` method, after the existing `if "operation_log" not in tables:` block, add:

```python
        if "bulk_batches" not in tables:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS bulk_batches (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation   TEXT NOT NULL,
                    field       TEXT,
                    value       TEXT,
                    tags        TEXT,
                    filter      TEXT,
                    photo_count INTEGER NOT NULL,
                    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
                );
            """)
            self.conn.commit()
        prop_cols = {
            r[1]
            for r in self.conn.execute("PRAGMA table_info(metadata_proposals)").fetchall()
        }
        if "batch_id" not in prop_cols:
            self.conn.execute(
                "ALTER TABLE metadata_proposals ADD COLUMN batch_id INTEGER REFERENCES bulk_batches(id)"
            )
            self.conn.commit()
```

- [ ] **Step 6: Run tests**

```bash
python -m pytest tests/test_bulk_operations.py::TestMigration023 -v
```

Expected: 5 passed.

- [ ] **Step 7: Commit**

```bash
git add db/migrations/migrate_023_bulk_batches.py db/schema.sql db/db.py tests/test_bulk_operations.py
git commit -m "feat(#133): migration 023 — bulk_batches table + batch_id on proposals"
```

---

### Task 2: DB methods — library query (TDD)

**Files:**
- Modify: `db/db.py`
- Modify: `tests/test_bulk_operations.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_bulk_operations.py`:

```python
# ===========================================================================
# Task 2 — library_photos query methods
# ===========================================================================


class TestLibraryPhotos(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = Database(Path(self.tmp) / "test.db")
        # Seed photos with varied attributes
        self.p1 = self.db.upsert_photo({
            "uuid": "u1", "original_filename": "A.JPG",
            "privacy_state": "already_public",
            "flickr_id": "f1",
            "date_taken": "2024-05-10 12:00:00",
            "flickr_title": "Paris Trip",
            "flickr_tags": json.dumps(["paris", "france"]),
            "photos_tags": json.dumps([]),
            "apple_persons": [], "proposed_tags": [],
        })
        self.p2 = self.db.upsert_photo({
            "uuid": "u2", "original_filename": "B.JPG",
            "privacy_state": "needs_review",
            "flickr_id": "f2",
            "date_taken": "2024-06-15 08:00:00",
            "flickr_title": "",
            "flickr_tags": json.dumps(["london"]),
            "photos_tags": json.dumps([]),
            "apple_persons": [], "proposed_tags": [],
        })
        self.p3 = self.db.upsert_photo({
            "uuid": "u3", "original_filename": "C.JPG",
            "privacy_state": "auto_private",
            "flickr_id": "f3",
            "date_taken": "2024-07-20 10:00:00",
            "flickr_title": None,
            "flickr_tags": json.dumps([]),
            "photos_tags": json.dumps([]),
            "apple_persons": [], "proposed_tags": [],
        })

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp)

    def test_library_photos_returns_all(self):
        rows = self.db.library_photos()
        self.assertEqual(len(rows), 3)

    def test_library_photos_date_from_filter(self):
        rows = self.db.library_photos(date_from="2024-06-01")
        ids = {r["id"] for r in rows}
        self.assertIn(self.p2, ids)
        self.assertIn(self.p3, ids)
        self.assertNotIn(self.p1, ids)

    def test_library_photos_date_to_filter(self):
        rows = self.db.library_photos(date_to="2024-06-01")
        ids = {r["id"] for r in rows}
        self.assertIn(self.p1, ids)
        self.assertNotIn(self.p2, ids)

    def test_library_photos_status_public(self):
        rows = self.db.library_photos(status="public")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], self.p1)

    def test_library_photos_status_private(self):
        rows = self.db.library_photos(status="private")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], self.p3)

    def test_library_photos_status_pending(self):
        rows = self.db.library_photos(status="pending")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], self.p2)

    def test_library_photos_untitled_only(self):
        rows = self.db.library_photos(untitled_only=True)
        ids = {r["id"] for r in rows}
        self.assertIn(self.p2, ids)
        self.assertIn(self.p3, ids)
        self.assertNotIn(self.p1, ids)

    def test_library_photos_tag_filter(self):
        rows = self.db.library_photos(tag="paris")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], self.p1)

    def test_library_photo_count(self):
        self.assertEqual(self.db.library_photo_count(), 3)

    def test_library_photo_count_with_filter(self):
        self.assertEqual(self.db.library_photo_count(status="public"), 1)

    def test_library_photo_ids(self):
        ids = self.db.library_photo_ids(status="public")
        self.assertEqual(ids, [self.p1])

    def test_library_photo_ids_all(self):
        ids = self.db.library_photo_ids()
        self.assertEqual(len(ids), 3)

    def test_library_photos_pagination(self):
        rows = self.db.library_photos(limit=2, offset=0)
        self.assertEqual(len(rows), 2)
        rows2 = self.db.library_photos(limit=2, offset=2)
        self.assertEqual(len(rows2), 1)

    def test_get_all_albums_empty(self):
        albums = self.db.get_all_albums()
        self.assertEqual(albums, [])

    def test_get_all_albums_returns_non_deleted(self):
        from datetime import datetime, timezone
        aid = self.db.upsert_album("album-uuid-1", "My Album")
        self.db.upsert_album("album-uuid-2", "Deleted Album")
        # Mark second album deleted
        self.db.conn.execute(
            "UPDATE albums SET deleted_at=? WHERE apple_uuid=?",
            (datetime.now(timezone.utc).isoformat(), "album-uuid-2"),
        )
        self.db.conn.commit()
        albums = self.db.get_all_albums()
        names = [a["name"] for a in albums]
        self.assertIn("My Album", names)
        self.assertNotIn("Deleted Album", names)
```

- [ ] **Step 2: Run to confirm failure**

```bash
python -m pytest tests/test_bulk_operations.py::TestLibraryPhotos -v
```

Expected: `AttributeError: 'Database' object has no attribute 'library_photos'`

- [ ] **Step 3: Add the query methods to `db/db.py`**

Add after the `review_queue_count` method (around line 838):

```python
    # -----------------------------------------------------------------------
    # Library view queries (bulk operations)
    # -----------------------------------------------------------------------

    _STATUS_STATES: dict[str, tuple[str, ...]] = {
        "public":  ("already_public", "approved_public",
                    "approved_friends", "approved_family", "approved_friends_family"),
        "private": ("auto_private", "keep_private"),
        "pending": ("needs_review", "candidate_public", "skipped"),
    }

    def _library_where(
        self,
        date_from: str | None,
        date_to: str | None,
        album_id: int | None,
        tag: str | None,
        status: str | None,
        untitled_only: bool,
    ) -> tuple[str, list]:
        """Return (WHERE clause fragment, params list) for library queries."""
        clauses: list[str] = ["p.flickr_deleted = 0"]
        params: list = []

        if date_from:
            clauses.append("p.date_taken >= ?")
            params.append(date_from)
        if date_to:
            clauses.append("p.date_taken <= ?")
            params.append(date_to)
        if status and status in self._STATUS_STATES:
            states = self._STATUS_STATES[status]
            placeholders = ",".join("?" * len(states))
            clauses.append(f"p.privacy_state IN ({placeholders})")
            params.extend(states)
        if untitled_only:
            clauses.append(
                "(p.flickr_title IS NULL OR p.flickr_title = '') "
                "AND (p.photos_title IS NULL OR p.photos_title = '')"
            )
        if tag:
            clauses.append(
                "(EXISTS (SELECT 1 FROM json_each(p.flickr_tags) WHERE value = ?) "
                "OR EXISTS (SELECT 1 FROM json_each(p.photos_tags) WHERE value = ?))"
            )
            params.extend([tag, tag])

        where = "WHERE " + " AND ".join(clauses)

        if album_id is not None:
            # Switch from simple photos alias to a join
            return where + " AND pa.album_id = ?", params + [album_id]

        return where, params

    def library_photos(
        self,
        date_from: str | None = None,
        date_to: str | None = None,
        album_id: int | None = None,
        tag: str | None = None,
        status: str | None = None,
        untitled_only: bool = False,
        limit: int = 120,
        offset: int = 0,
    ) -> list[dict]:
        """Return photos for the library grid, newest first, with filters applied."""
        where, params = self._library_where(
            date_from, date_to, album_id, tag, status, untitled_only
        )
        join = (
            "JOIN photo_albums pa ON pa.photo_id = p.id"
            if album_id is not None
            else ""
        )
        rows = self.conn.execute(
            f"""SELECT p.id, p.flickr_id, p.uuid, p.original_filename,
                       p.thumbnail_path, p.date_taken, p.privacy_state,
                       p.flickr_title, p.photos_title,
                       p.flickr_tags, p.photos_tags,
                       p.is_video, p.width, p.height, p.bp_rating,
                       p.display_rotation
                FROM photos p {join}
                {where}
                ORDER BY p.date_taken DESC, p.id DESC
                LIMIT ? OFFSET ?""",
            params + [limit, offset],
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["flickr_tags"] = _json_loads_safe(d.get("flickr_tags"))
            d["photos_tags"] = _json_loads_safe(d.get("photos_tags"))
            result.append(d)
        return result

    def library_photo_count(
        self,
        date_from: str | None = None,
        date_to: str | None = None,
        album_id: int | None = None,
        tag: str | None = None,
        status: str | None = None,
        untitled_only: bool = False,
    ) -> int:
        """Return total photo count for the given library filters."""
        where, params = self._library_where(
            date_from, date_to, album_id, tag, status, untitled_only
        )
        join = (
            "JOIN photo_albums pa ON pa.photo_id = p.id"
            if album_id is not None
            else ""
        )
        row = self.conn.execute(
            f"SELECT COUNT(*) AS n FROM photos p {join} {where}", params
        ).fetchone()
        return row["n"] if row else 0

    def library_photo_ids(
        self,
        date_from: str | None = None,
        date_to: str | None = None,
        album_id: int | None = None,
        tag: str | None = None,
        status: str | None = None,
        untitled_only: bool = False,
    ) -> list[int]:
        """Return all photo IDs matching the filters (no limit — used by bulk-edit)."""
        where, params = self._library_where(
            date_from, date_to, album_id, tag, status, untitled_only
        )
        join = (
            "JOIN photo_albums pa ON pa.photo_id = p.id"
            if album_id is not None
            else ""
        )
        rows = self.conn.execute(
            f"SELECT p.id FROM photos p {join} {where} ORDER BY p.id",
            params,
        ).fetchall()
        return [r["id"] for r in rows]

    def get_all_albums(self) -> list[dict]:
        """Return all non-deleted albums ordered by name."""
        rows = self.conn.execute(
            """SELECT id, name, flickr_set_id
               FROM albums
               WHERE deleted_at IS NULL
               ORDER BY name""",
        ).fetchall()
        return [dict(r) for r in rows]
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/test_bulk_operations.py::TestLibraryPhotos -v
```

Expected: 15 passed.

- [ ] **Step 5: Run full suite**

```bash
python -m pytest tests/ -q
```

Expected: all existing tests still pass.

- [ ] **Step 6: Lint**

```bash
make lint
```

Fix any mypy or ruff errors before committing.

- [ ] **Step 7: Commit**

```bash
git add db/db.py tests/test_bulk_operations.py
git commit -m "feat(#133): library_photos, library_photo_count, library_photo_ids, get_all_albums"
```

---

### Task 3: DB methods — bulk proposals (TDD)

**Files:**
- Modify: `db/db.py`
- Modify: `tests/test_bulk_operations.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_bulk_operations.py`:

```python
# ===========================================================================
# Task 3 — bulk proposals DB methods
# ===========================================================================


class TestBulkProposals(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = Database(Path(self.tmp) / "test.db")
        self.p1 = self.db.upsert_photo({
            "uuid": "u1", "original_filename": "A.JPG",
            "flickr_id": "f1",
            "privacy_state": "already_public",
            "flickr_title": "",
            "flickr_description": "",
            "flickr_tags": json.dumps(["paris"]),
            "photos_tags": json.dumps([]),
            "apple_persons": [], "proposed_tags": [],
        })
        self.p2 = self.db.upsert_photo({
            "uuid": "u2", "original_filename": "B.JPG",
            "flickr_id": "f2",
            "privacy_state": "already_public",
            "flickr_title": "Existing Title",
            "flickr_description": "",
            "flickr_tags": json.dumps(["london", "uk"]),
            "photos_tags": json.dumps([]),
            "apple_persons": [], "proposed_tags": [],
        })
        self.p3 = self.db.upsert_photo({
            "uuid": "u3", "original_filename": "C.JPG",
            "flickr_id": None,  # Photos-only — should be skipped
            "privacy_state": "needs_review",
            "flickr_title": "",
            "flickr_tags": json.dumps([]),
            "photos_tags": json.dumps([]),
            "apple_persons": [], "proposed_tags": [],
        })

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp)

    def _pending_proposals(self):
        rows = self.db.conn.execute(
            "SELECT * FROM metadata_proposals WHERE status='pending'"
        ).fetchall()
        return [dict(r) for r in rows]

    # --- create_bulk_batch ---

    def test_create_bulk_batch_returns_id(self):
        bid = self.db.create_bulk_batch(
            operation="set_title",
            field="title",
            value="Test Title",
            tags=None,
            filter_json=None,
            photo_count=2,
        )
        self.assertIsInstance(bid, int)
        self.assertGreater(bid, 0)

    def test_create_bulk_batch_stores_data(self):
        bid = self.db.create_bulk_batch(
            operation="tags_add",
            field=None,
            value=None,
            tags=["mfa-boston"],
            filter_json='{"status": "public"}',
            photo_count=10,
        )
        row = self.db.conn.execute(
            "SELECT * FROM bulk_batches WHERE id=?", (bid,)
        ).fetchone()
        self.assertEqual(row["operation"], "tags_add")
        self.assertEqual(json.loads(row["tags"]), ["mfa-boston"])
        self.assertEqual(row["photo_count"], 10)

    # --- insert_bulk_proposals — title ---

    def test_insert_bulk_title_creates_proposals(self):
        bid = self.db.create_bulk_batch("set_title", "title", "MFA Boston", None, None, 2)
        n = self.db.insert_bulk_proposals(
            batch_id=bid,
            photo_ids=[self.p1, self.p2],
            field="title",
            value="MFA Boston",
            skip_existing=False,
        )
        self.assertEqual(n, 2)
        proposals = self._pending_proposals()
        self.assertEqual(len(proposals), 2)
        self.assertTrue(all(p["field"] == "title" for p in proposals))
        self.assertTrue(all(p["proposed_value"] == "MFA Boston" for p in proposals))
        self.assertTrue(all(p["batch_id"] == bid for p in proposals))

    def test_insert_bulk_title_skip_existing(self):
        bid = self.db.create_bulk_batch("set_title", "title", "MFA Boston", None, None, 1)
        n = self.db.insert_bulk_proposals(
            batch_id=bid,
            photo_ids=[self.p1, self.p2],
            field="title",
            value="MFA Boston",
            skip_existing=True,
        )
        # p2 already has 'Existing Title' → should be skipped
        self.assertEqual(n, 1)
        proposals = self._pending_proposals()
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["photo_id"], self.p1)

    def test_insert_bulk_title_skips_photos_without_flickr_id(self):
        bid = self.db.create_bulk_batch("set_title", "title", "X", None, None, 1)
        n = self.db.insert_bulk_proposals(
            batch_id=bid,
            photo_ids=[self.p1, self.p3],  # p3 has no flickr_id
            field="title",
            value="X",
            skip_existing=False,
        )
        self.assertEqual(n, 1)  # only p1

    def test_insert_bulk_title_idempotent(self):
        """Running the same bulk op twice produces no additional proposals."""
        bid = self.db.create_bulk_batch("set_title", "title", "MFA Boston", None, None, 1)
        self.db.insert_bulk_proposals(bid, [self.p1], "title", value="MFA Boston")
        n2 = self.db.insert_bulk_proposals(bid, [self.p1], "title", value="MFA Boston")
        self.assertEqual(n2, 0)
        self.assertEqual(len(self._pending_proposals()), 1)

    # --- insert_bulk_proposals — tags_add ---

    def test_insert_bulk_tags_add(self):
        bid = self.db.create_bulk_batch("tags_add", None, None, ["mfa-boston"], None, 2)
        n = self.db.insert_bulk_proposals(
            batch_id=bid,
            photo_ids=[self.p1, self.p2],
            field="tags_add",
            tags=["mfa-boston"],
        )
        self.assertEqual(n, 2)
        proposals = self._pending_proposals()
        # p1 had ["paris"] → should become ["mfa-boston", "paris"]
        p1_prop = next(p for p in proposals if p["photo_id"] == self.p1)
        self.assertEqual(json.loads(p1_prop["proposed_value"]), ["mfa-boston", "paris"])

    def test_insert_bulk_tags_add_idempotent_per_photo(self):
        """Adding a tag already present on a photo generates no proposal for that photo."""
        bid = self.db.create_bulk_batch("tags_add", None, None, ["paris"], None, 1)
        n = self.db.insert_bulk_proposals(
            batch_id=bid,
            photo_ids=[self.p1],  # p1 already has "paris"
            field="tags_add",
            tags=["paris"],
        )
        self.assertEqual(n, 0)

    # --- insert_bulk_proposals — tags_remove ---

    def test_insert_bulk_tags_remove(self):
        bid = self.db.create_bulk_batch("tags_remove", None, None, ["paris"], None, 1)
        n = self.db.insert_bulk_proposals(
            batch_id=bid,
            photo_ids=[self.p1],
            field="tags_remove",
            tags=["paris"],
        )
        self.assertEqual(n, 1)
        proposals = self._pending_proposals()
        self.assertEqual(json.loads(proposals[0]["proposed_value"]), [])

    def test_insert_bulk_tags_remove_absent_tag_noop(self):
        """Removing a tag not present on a photo generates no proposal."""
        bid = self.db.create_bulk_batch("tags_remove", None, None, ["nonexistent"], None, 1)
        n = self.db.insert_bulk_proposals(
            batch_id=bid,
            photo_ids=[self.p1],
            field="tags_remove",
            tags=["nonexistent"],
        )
        self.assertEqual(n, 0)

    # --- get_pending_bulk_batches / reject_bulk_batch ---

    def test_get_pending_bulk_batches_empty(self):
        self.assertEqual(self.db.get_pending_bulk_batches(), [])

    def test_get_pending_bulk_batches_returns_batch_with_pending_proposals(self):
        bid = self.db.create_bulk_batch("set_title", "title", "X", None, None, 1)
        self.db.insert_bulk_proposals(bid, [self.p1], "title", value="X")
        batches = self.db.get_pending_bulk_batches()
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0]["id"], bid)
        self.assertEqual(batches[0]["pending_count"], 1)

    def test_reject_bulk_batch(self):
        bid = self.db.create_bulk_batch("set_title", "title", "X", None, None, 2)
        self.db.insert_bulk_proposals(bid, [self.p1, self.p2], "title", value="X")
        n = self.db.reject_bulk_batch(bid)
        self.assertEqual(n, 2)
        proposals = self._pending_proposals()
        self.assertEqual(len(proposals), 0)

    def test_reject_bulk_batch_only_affects_pending(self):
        """Already-resolved proposals in the batch are not re-rejected."""
        bid = self.db.create_bulk_batch("set_title", "title", "X", None, None, 2)
        self.db.insert_bulk_proposals(bid, [self.p1, self.p2], "title", value="X")
        proposals = self._pending_proposals()
        # Manually apply one
        self.db.resolve_proposal(proposals[0]["id"], "applied")
        n = self.db.reject_bulk_batch(bid)
        self.assertEqual(n, 1)
```

- [ ] **Step 2: Run to confirm failure**

```bash
python -m pytest tests/test_bulk_operations.py::TestBulkProposals -v
```

Expected: `AttributeError` — methods not yet defined.

- [ ] **Step 3: Add bulk proposals methods to `db/db.py`**

Add after the `get_all_albums` method:

```python
    # -----------------------------------------------------------------------
    # Bulk operations
    # -----------------------------------------------------------------------

    def create_bulk_batch(
        self,
        operation: str,
        field: str | None,
        value: str | None,
        tags: list[str] | None,
        filter_json: str | None,
        photo_count: int,
    ) -> int:
        """Create a bulk_batches record and return the new batch_id."""
        cur = self.conn.execute(
            """INSERT INTO bulk_batches (operation, field, value, tags, filter, photo_count, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                operation,
                field,
                value,
                json.dumps(tags) if tags is not None else None,
                filter_json,
                photo_count,
                _now_iso(),
            ),
        )
        self.conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def insert_bulk_proposals(
        self,
        batch_id: int,
        photo_ids: list[int],
        field: str,
        value: str | None = None,
        tags: list[str] | None = None,
        skip_existing: bool = False,
    ) -> int:
        """
        Insert metadata_proposals for the given photos.

        field must be one of: 'title', 'description', 'tags_add', 'tags_remove'.

        For 'tags_add' / 'tags_remove', proposed_value is the full new tag list
        (sorted JSON array), not the delta. Photos without a flickr_id are skipped.

        Returns count of proposals actually inserted.
        """
        if not photo_ids:
            return 0

        placeholders = ",".join("?" * len(photo_ids))
        rows = self.conn.execute(
            f"""SELECT id, flickr_id,
                       flickr_title, flickr_description,
                       flickr_tags, flickr_tags_hash,
                       photos_title
                FROM photos
                WHERE id IN ({placeholders}) AND flickr_id IS NOT NULL AND flickr_deleted = 0""",
            photo_ids,
        ).fetchall()

        created = 0
        now = _now_iso()

        for row in rows:
            photo_id = row["id"]
            db_field: str
            proposed_value: str

            if field == "title":
                db_field = "title"
                existing = (row["flickr_title"] or "").strip()
                if skip_existing and existing:
                    continue
                proposed_value = value or ""

            elif field == "description":
                db_field = "description"
                existing = (row["flickr_description"] or "").strip()
                if skip_existing and existing:
                    continue
                proposed_value = value or ""

            elif field == "tags_add":
                db_field = "tags"
                assert tags is not None
                current = _json_loads_safe(row["flickr_tags"])
                current_set = set(current)
                new_set = current_set | set(tags)
                if new_set == current_set:
                    continue  # all tags already present
                proposed_value = json.dumps(sorted(new_set))

            elif field == "tags_remove":
                db_field = "tags"
                assert tags is not None
                current = _json_loads_safe(row["flickr_tags"])
                remove_set = set(tags)
                new_list = sorted(t for t in current if t not in remove_set)
                if len(new_list) == len(current):
                    continue  # none of the tags were present
                proposed_value = json.dumps(new_list)

            else:
                raise ValueError(f"Unknown bulk field: {field!r}")

            # INSERT OR IGNORE respects the unique pending index
            cur = self.conn.execute(
                """INSERT OR IGNORE INTO metadata_proposals
                   (photo_id, field, proposed_value, source, target, conflict_type,
                    source_hash_at_creation, target_hash_at_creation,
                    status, created_at, batch_id)
                   VALUES (?, ?, ?, 'manual', 'flickr', 'non_conflict',
                           NULL, ?, 'pending', ?, ?)""",
                (
                    photo_id,
                    db_field,
                    proposed_value,
                    row["flickr_tags_hash"] if db_field == "tags" else None,
                    now,
                    batch_id,
                ),
            )
            if cur.rowcount:
                created += 1

        self.conn.commit()
        return created

    def get_pending_bulk_batches(self) -> list[dict]:
        """Return batches that have at least one pending proposal, newest first."""
        rows = self.conn.execute(
            """SELECT bb.id, bb.operation, bb.field, bb.value, bb.tags,
                      bb.photo_count, bb.created_at,
                      COUNT(mp.id) AS pending_count
               FROM bulk_batches bb
               JOIN metadata_proposals mp ON mp.batch_id = bb.id AND mp.status = 'pending'
               GROUP BY bb.id
               ORDER BY bb.id DESC"""
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            if d.get("tags"):
                d["tags"] = _json_loads_safe(d["tags"])
            result.append(d)
        return result

    def reject_bulk_batch(self, batch_id: int) -> int:
        """Reject all pending proposals in a batch. Returns count rejected."""
        now = _now_iso()
        cur = self.conn.execute(
            """UPDATE metadata_proposals
               SET status='rejected', resolved_at=?, resolution_note='bulk batch rejected'
               WHERE batch_id=? AND status='pending'""",
            (now, batch_id),
        )
        self.conn.commit()
        return cur.rowcount
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/test_bulk_operations.py::TestBulkProposals -v
```

Expected: all pass.

- [ ] **Step 5: Run full suite + lint**

```bash
python -m pytest tests/ -q && make lint
```

- [ ] **Step 6: Commit**

```bash
git add db/db.py tests/test_bulk_operations.py
git commit -m "feat(#133): create_bulk_batch, insert_bulk_proposals, get_pending_bulk_batches, reject_bulk_batch"
```

---

### Task 4: `/library` route + template (grid + filter bar)

**Files:**
- Modify: `reviewer/app.py`
- New: `reviewer/templates/library.html`
- Modify: `reviewer/templates/base.html`
- Modify: `tests/test_bulk_operations.py`

- [ ] **Step 1: Write failing route tests**

Append to `tests/test_bulk_operations.py`:

```python
# ===========================================================================
# Task 4 — /library route
# ===========================================================================

import reviewer.app as app_module


@pytest.fixture(scope="module")
def lib_client():
    with tempfile.TemporaryDirectory() as tmp:
        test_db = Database(Path(tmp) / "test.db")
        for i in range(1, 6):
            test_db.upsert_photo({
                "uuid": f"lib-uuid-{i}",
                "flickr_id": f"flickr-{i}",
                "original_filename": f"IMG_{i:04d}.JPG",
                "privacy_state": "already_public",
                "date_taken": f"2024-0{min(i,9)}-10 12:00:00",
                "flickr_title": f"Title {i}" if i % 2 == 0 else "",
                "flickr_tags": json.dumps([f"tag{i}"]),
                "photos_tags": json.dumps([]),
                "apple_persons": [],
                "proposed_tags": [],
            })
        app_module._db = test_db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test-secret"
        with app_module.app.test_client() as c:
            yield c
        app_module._db = None


class TestLibraryRoute:
    def test_library_page_200(self, lib_client):
        resp = lib_client.get("/library")
        assert resp.status_code == 200

    def test_library_page_shows_photos(self, lib_client):
        resp = lib_client.get("/library")
        html = resp.data.decode()
        assert "IMG_0001.JPG" in html or "library" in html.lower()

    def test_library_filter_status(self, lib_client):
        resp = lib_client.get("/library?status=public")
        assert resp.status_code == 200

    def test_library_filter_untitled(self, lib_client):
        resp = lib_client.get("/library?untitled=1")
        assert resp.status_code == 200

    def test_library_pagination(self, lib_client):
        resp = lib_client.get("/library?page=1&per_page=2")
        assert resp.status_code == 200
```

- [ ] **Step 2: Run to confirm failure**

```bash
python -m pytest tests/test_bulk_operations.py::TestLibraryRoute -v
```

Expected: 404 — route not defined yet.

- [ ] **Step 3: Add `/library` route to `reviewer/app.py`**

Add after the `zones` route (around line 730):

```python
@app.route("/library")
def library() -> str:
    page = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 120))
    offset = (page - 1) * per_page

    date_from = request.args.get("date_from") or None
    date_to = request.args.get("date_to") or None
    album_id_raw = request.args.get("album_id")
    album_id = int(album_id_raw) if album_id_raw else None
    tag = request.args.get("tag") or None
    status = request.args.get("status") or None
    untitled_only = bool(request.args.get("untitled"))

    photos = db().library_photos(
        date_from=date_from,
        date_to=date_to,
        album_id=album_id,
        tag=tag,
        status=status,
        untitled_only=untitled_only,
        limit=per_page,
        offset=offset,
    )
    total = db().library_photo_count(
        date_from=date_from,
        date_to=date_to,
        album_id=album_id,
        tag=tag,
        status=status,
        untitled_only=untitled_only,
    )
    albums = db().get_all_albums()

    return render_template(
        "library.html",
        photos=photos,
        albums=albums,
        total=total,
        page=page,
        per_page=per_page,
        total_pages=max(1, (total + per_page - 1) // per_page),
        filters={
            "date_from": date_from or "",
            "date_to": date_to or "",
            "album_id": album_id,
            "tag": tag or "",
            "status": status or "",
            "untitled": untitled_only,
        },
    )
```

- [ ] **Step 4: Create `reviewer/templates/library.html`**

```html
{% extends "base.html" %}
{% block title %}Library — Blue Pearmain{% endblock %}

{% block extra_style %}
/* ── Filter bar ─────────────────────────────────── */
.lib-filter-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 16px;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  flex-wrap: wrap;
  position: sticky;
  top: 0;
  z-index: 20;
}
.lib-filter-bar label { font-size: 11px; color: var(--muted); }
.lib-filter-bar input[type=text],
.lib-filter-bar input[type=date],
.lib-filter-bar select {
  background: var(--bg);
  border: 1px solid var(--border);
  color: var(--text);
  border-radius: var(--radius);
  padding: 4px 8px;
  font-size: 12px;
}
.lib-filter-bar .filter-chip {
  background: rgba(45,125,70,0.2);
  border: 1px solid var(--green);
  color: #8e8;
  border-radius: 12px;
  padding: 2px 10px;
  font-size: 11px;
  display: flex;
  align-items: center;
  gap: 4px;
}
.lib-filter-bar .filter-chip button {
  background: none; border: none; color: inherit;
  cursor: pointer; padding: 0; line-height: 1;
}
.lib-count { color: var(--muted); font-size: 12px; margin-left: auto; white-space: nowrap; }

/* ── Selection toolbar ──────────────────────────── */
.lib-select-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 16px;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  font-size: 12px;
}
.lib-select-bar label { color: var(--muted); display: flex; align-items: center; gap: 6px; cursor: pointer; }

/* ── Action bar (appears when photos selected) ───── */
.lib-action-bar {
  display: none;
  align-items: center;
  gap: 12px;
  padding: 8px 16px;
  background: #1a2a4a;
  border-bottom: 1px solid #3a5fff;
  font-size: 13px;
  position: sticky;
  top: 41px;   /* below filter bar */
  z-index: 19;
}
.lib-action-bar.visible { display: flex; }
.lib-action-bar .sel-count { color: #ccc; font-weight: 600; }
.lib-action-bar .sep { color: #444; }
.lib-action-bar button {
  background: none; border: none;
  color: #7a9fff; cursor: pointer; font-size: 13px; padding: 2px 0;
}
.lib-action-bar button:hover { text-decoration: underline; }
.lib-action-bar .clear-btn { color: var(--muted); margin-left: auto; }

/* ── Inline edit panel ───────────────────────────── */
.lib-edit-panel {
  display: none;
  padding: 12px 16px;
  background: #141e38;
  border-bottom: 2px solid #3a5fff;
}
.lib-edit-panel.visible { display: block; }
.lib-edit-panel h4 { color: #7a9fff; font-size: 12px; text-transform: uppercase;
                     letter-spacing: .06em; margin-bottom: 10px; }
.lib-edit-panel input[type=text],
.lib-edit-panel textarea {
  width: 100%; background: var(--bg); border: 1px solid #3a5fff;
  color: var(--text); border-radius: var(--radius); padding: 6px 10px;
  font-size: 13px; box-sizing: border-box;
}
.lib-edit-panel textarea { height: 70px; resize: vertical; }
.lib-edit-panel .panel-meta {
  margin: 8px 0; font-size: 12px; color: var(--muted);
}
.lib-edit-panel .panel-meta strong { color: #ccc; }
.lib-edit-panel .panel-meta .warn { color: #f88; font-weight: 600; }
.panel-actions { display: flex; align-items: center; gap: 10px; margin-top: 10px; }
.panel-actions .btn-confirm {
  background: #3a5fff; color: white; border: none;
  border-radius: var(--radius); padding: 6px 16px; font-size: 13px; cursor: pointer;
}
.panel-actions .btn-confirm:disabled { opacity: .5; cursor: default; }
.panel-actions .btn-cancel {
  background: none; border: none; color: var(--muted);
  font-size: 13px; cursor: pointer;
}
/* Tag chips */
.tag-chip-input-wrap {
  display: flex; flex-wrap: wrap; gap: 6px; align-items: center;
  background: var(--bg); border: 1px solid #3a5fff;
  border-radius: var(--radius); padding: 6px 8px; min-height: 36px;
}
.tag-chip {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 2px 8px; border-radius: 10px; font-size: 12px;
  background: rgba(58,95,255,.25); border: 1px solid #3a5fff; color: #aac;
}
.tag-chip.remove-chip {
  background: rgba(139,32,32,.25); border-color: #8b2020; color: #f88;
}
.tag-chip button { background: none; border: none; color: inherit; cursor: pointer; padding: 0; }
.tag-chip-input { background: none; border: none; outline: none;
                  color: var(--text); font-size: 12px; min-width: 80px; flex: 1; }

/* ── Photo grid ─────────────────────────────────── */
.lib-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
  gap: 4px;
  padding: 8px 16px;
}
.lib-thumb {
  position: relative;
  aspect-ratio: 1;
  background: var(--surface);
  border-radius: 3px;
  overflow: hidden;
  cursor: pointer;
}
.lib-thumb img {
  width: 100%; height: 100%; object-fit: cover; display: block;
}
.lib-thumb .placeholder-icon {
  position: absolute; inset: 0;
  display: flex; align-items: center; justify-content: center;
  color: var(--muted); font-size: 28px;
}
.lib-thumb input[type=checkbox] {
  position: absolute; top: 5px; left: 5px;
  width: 18px; height: 18px; cursor: pointer;
  accent-color: #3a5fff;
}
.lib-thumb.selected { outline: 2px solid #3a5fff; outline-offset: -2px; }
.lib-thumb .thumb-title {
  position: absolute; bottom: 0; left: 0; right: 0;
  background: rgba(0,0,0,.65); color: #ccc;
  font-size: 10px; padding: 3px 5px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}

/* ── Pagination ─────────────────────────────────── */
.lib-pagination {
  display: flex; gap: 8px; align-items: center;
  padding: 12px 16px; font-size: 13px; color: var(--muted);
}
.lib-pagination a { color: var(--accent); }
{% endblock %}

{% block content %}
<!-- Filter bar -->
<form id="lib-filter-form" method="get" action="{{ url_for('library') }}">
<div class="lib-filter-bar">
  <label>From <input type="date" name="date_from" value="{{ filters.date_from }}"></label>
  <label>To <input type="date" name="date_to" value="{{ filters.date_to }}"></label>
  <label>Album
    <select name="album_id">
      <option value="">All albums</option>
      {% for a in albums %}
        <option value="{{ a.id }}" {% if filters.album_id == a.id %}selected{% endif %}>{{ a.name }}</option>
      {% endfor %}
    </select>
  </label>
  <label>Tag <input type="text" name="tag" value="{{ filters.tag }}" placeholder="filter by tag…" style="width:120px"></label>
  <label>Status
    <select name="status">
      <option value="">All</option>
      <option value="public"  {% if filters.status == 'public'  %}selected{% endif %}>Public</option>
      <option value="private" {% if filters.status == 'private' %}selected{% endif %}>Private</option>
      <option value="pending" {% if filters.status == 'pending' %}selected{% endif %}>Pending</option>
    </select>
  </label>
  <label style="display:flex;align-items:center;gap:5px">
    <input type="checkbox" name="untitled" value="1" {% if filters.untitled %}checked{% endif %} onchange="this.form.submit()">
    Untitled only
  </label>
  <button type="submit" style="background:var(--accent);border:none;color:white;padding:4px 12px;border-radius:var(--radius);font-size:12px;cursor:pointer">Apply</button>
  {% if filters.date_from or filters.date_to or filters.album_id or filters.tag or filters.status or filters.untitled %}
  <a href="{{ url_for('library') }}" style="font-size:12px;color:var(--muted)">Clear filters</a>
  {% endif %}
  <span class="lib-count">{{ total }} photo{{ 's' if total != 1 }}</span>
</div>
</form>

<!-- Select-all row -->
<div class="lib-select-bar">
  <label>
    <input type="checkbox" id="select-all-cb">
    Select all {{ total }} matching
  </label>
  <span id="sel-filter-note" style="display:none;font-size:11px;color:var(--muted)">
    — filter-based selection active
  </span>
</div>

<!-- Action bar (hidden until selection) -->
<div class="lib-action-bar" id="lib-action-bar">
  <span class="sel-count" id="sel-count-label">0 selected</span>
  <span class="sep">│</span>
  <button onclick="openPanel('title')">Edit title</button>
  <button onclick="openPanel('description')">Edit description</button>
  <button onclick="openPanel('tags_add')">Add tags</button>
  <button onclick="openPanel('tags_remove')">Remove tags</button>
  <button class="clear-btn" onclick="clearSelection()">✕ Clear</button>
</div>

<!-- Inline edit panel (hidden until action clicked) -->
<div class="lib-edit-panel" id="lib-edit-panel">
  <h4 id="panel-title">Edit</h4>

  <!-- Title / description inputs -->
  <div id="panel-text-wrap" style="display:none">
    <input type="text" id="panel-text-input" placeholder="New value…">
    <textarea id="panel-textarea" placeholder="New value…" style="display:none"></textarea>
    <div style="margin-top:8px;display:flex;align-items:center;gap:8px">
      <label style="font-size:12px;color:var(--muted);display:flex;align-items:center;gap:5px">
        <input type="checkbox" id="skip-existing-cb" checked>
        Skip photos that already have a value
      </label>
    </div>
  </div>

  <!-- Tag chip input -->
  <div id="panel-tag-wrap" style="display:none">
    <div class="tag-chip-input-wrap" id="tag-chip-wrap">
      <input class="tag-chip-input" id="tag-chip-input" placeholder="type tag, press Enter…">
    </div>
  </div>

  <div class="panel-meta" id="panel-preview">
    <!-- filled by JS preview request -->
  </div>

  <div class="panel-actions">
    <button class="btn-confirm" id="panel-confirm-btn" onclick="submitPanel()" disabled>Queue proposals</button>
    <button class="btn-cancel" onclick="closePanel()">Cancel</button>
  </div>
</div>

<!-- Photo grid -->
<div class="lib-grid" id="lib-grid">
{% for photo in photos %}
  <div class="lib-thumb {% if photo.is_video %}is-video{% endif %}"
       id="thumb-{{ photo.id }}"
       data-id="{{ photo.id }}"
       data-title="{{ (photo.flickr_title or photo.photos_title or '') | e }}"
       data-description=""
       data-tags="{{ (photo.flickr_tags or []) | tojson | e }}"
       onclick="thumbClick(event, {{ photo.id }})">
    {% if photo.thumbnail_path %}
      <img src="{{ url_for('thumb', photo_id=photo.id) }}"
           loading="lazy"
           style="transform: rotate({{ photo.display_rotation }}deg)">
    {% else %}
      <div class="placeholder-icon">{% if photo.is_video %}🎬{% else %}📷{% endif %}</div>
    {% endif %}
    <input type="checkbox" class="photo-cb" data-id="{{ photo.id }}"
           onclick="event.stopPropagation(); togglePhoto({{ photo.id }})">
    {% set title = photo.flickr_title or photo.photos_title %}
    {% if title %}
      <div class="thumb-title">{{ title }}</div>
    {% endif %}
  </div>
{% else %}
  <p style="grid-column:1/-1;color:var(--muted);padding:24px">No photos match the current filters.</p>
{% endfor %}
</div>

<!-- Pagination -->
{% if total_pages > 1 %}
<div class="lib-pagination">
  {% if page > 1 %}
    <a href="{{ url_for('library', page=page-1, per_page=per_page, **filters) }}">← Prev</a>
  {% endif %}
  <span>Page {{ page }} of {{ total_pages }}</span>
  {% if page < total_pages %}
    <a href="{{ url_for('library', page=page+1, per_page=per_page, **filters) }}">Next →</a>
  {% endif %}
</div>
{% endif %}
{% endblock %}

{% block scripts %}
<script>
// ── Selection state ─────────────────────────────────────────────────
// Two modes: explicit (Set of photo IDs) or filter-all (boolean)
let _selectedIds = new Set();
let _selectAllFilter = false;  // true = "all matching" selected
const _totalMatching = {{ total }};

// Photo data for client-side preview (title/description)
const _photoData = {};
{% for photo in photos %}
_photoData[{{ photo.id }}] = {
  title: {{ (photo.flickr_title or '') | tojson }},
  description: '',
  tags: {{ (photo.flickr_tags or []) | tojson }},
};
{% endfor %}

function thumbClick(evt, id) {
  if (evt.target.tagName === 'INPUT') return;
  togglePhoto(id);
}

function togglePhoto(id) {
  if (_selectAllFilter) {
    // Deselect from filter-all: switch to explicit mode
    _selectAllFilter = false;
    _selectedIds = new Set(Object.keys(_photoData).map(Number));
    document.getElementById('sel-filter-note').style.display = 'none';
  }
  const el = document.getElementById(`thumb-${id}`);
  const cb = el.querySelector('.photo-cb');
  if (_selectedIds.has(id)) {
    _selectedIds.delete(id);
    el.classList.remove('selected');
    cb.checked = false;
  } else {
    _selectedIds.add(id);
    el.classList.add('selected');
    cb.checked = true;
  }
  _updateUI();
}

document.getElementById('select-all-cb').addEventListener('change', function() {
  if (this.checked) {
    _selectAllFilter = true;
    _selectedIds.clear();
    document.querySelectorAll('.photo-cb').forEach(cb => { cb.checked = true; });
    document.querySelectorAll('.lib-thumb').forEach(el => el.classList.add('selected'));
    document.getElementById('sel-filter-note').style.display = '';
  } else {
    clearSelection();
  }
  _updateUI();
});

function clearSelection() {
  _selectAllFilter = false;
  _selectedIds.clear();
  document.querySelectorAll('.photo-cb').forEach(cb => { cb.checked = false; });
  document.querySelectorAll('.lib-thumb').forEach(el => el.classList.remove('selected'));
  document.getElementById('select-all-cb').checked = false;
  document.getElementById('sel-filter-note').style.display = 'none';
  _updateUI();
  closePanel();
}

function _selectionCount() {
  return _selectAllFilter ? _totalMatching : _selectedIds.size;
}

function _updateUI() {
  const n = _selectionCount();
  const actionBar = document.getElementById('lib-action-bar');
  document.getElementById('sel-count-label').textContent = `${n} selected`;
  if (n > 0) {
    actionBar.classList.add('visible');
  } else {
    actionBar.classList.remove('visible');
    closePanel();
  }
}

// ── Inline edit panel ────────────────────────────────────────────────
let _currentField = null;
let _panelTags = [];

function openPanel(field) {
  _currentField = field;
  _panelTags = [];
  const panel = document.getElementById('lib-edit-panel');
  const textWrap = document.getElementById('panel-text-wrap');
  const tagWrap = document.getElementById('panel-tag-wrap');
  const titleEl = document.getElementById('panel-title');
  const input = document.getElementById('panel-text-input');
  const textarea = document.getElementById('panel-textarea');

  panel.classList.add('visible');

  if (field === 'title') {
    titleEl.textContent = `Edit title · ${_selectionCount()} photos`;
    textWrap.style.display = '';
    tagWrap.style.display = 'none';
    input.style.display = '';
    textarea.style.display = 'none';
    input.value = '';
    input.focus();
    input.oninput = _updatePreview;
    document.getElementById('skip-existing-cb').onchange = _updatePreview;
  } else if (field === 'description') {
    titleEl.textContent = `Edit description · ${_selectionCount()} photos`;
    textWrap.style.display = '';
    tagWrap.style.display = 'none';
    input.style.display = 'none';
    textarea.style.display = '';
    textarea.value = '';
    textarea.focus();
    textarea.oninput = _updatePreview;
    document.getElementById('skip-existing-cb').onchange = _updatePreview;
  } else {
    titleEl.textContent = (field === 'tags_add' ? 'Add tags' : 'Remove tags') +
      ` · ${_selectionCount()} photos`;
    textWrap.style.display = 'none';
    tagWrap.style.display = '';
    _renderTagChips();
    document.getElementById('tag-chip-input').focus();
  }

  _updatePreview();
}

function closePanel() {
  document.getElementById('lib-edit-panel').classList.remove('visible');
  document.getElementById('panel-preview').innerHTML = '';
  document.getElementById('panel-confirm-btn').disabled = true;
  _currentField = null;
  _panelTags = [];
  // Clear tag chips
  const wrap = document.getElementById('tag-chip-wrap');
  if (wrap) {
    wrap.querySelectorAll('.tag-chip').forEach(c => c.remove());
  }
}

// Tag chip input
document.getElementById('tag-chip-input').addEventListener('keydown', function(e) {
  if (e.key === 'Enter' || e.key === ',') {
    e.preventDefault();
    const val = this.value.trim().replace(/,+$/, '');
    if (val && !_panelTags.includes(val)) {
      _panelTags.push(val);
      _renderTagChips();
      _updatePreview();
    }
    this.value = '';
  } else if (e.key === 'Backspace' && !this.value && _panelTags.length) {
    _panelTags.pop();
    _renderTagChips();
    _updatePreview();
  }
});

function _renderTagChips() {
  const wrap = document.getElementById('tag-chip-wrap');
  wrap.querySelectorAll('.tag-chip').forEach(c => c.remove());
  const input = document.getElementById('tag-chip-input');
  const isRemove = _currentField === 'tags_remove';
  _panelTags.forEach(tag => {
    const chip = document.createElement('span');
    chip.className = 'tag-chip' + (isRemove ? ' remove-chip' : '');
    chip.innerHTML = `${tag} <button onclick="removeChip('${tag}')">×</button>`;
    wrap.insertBefore(chip, input);
  });
}

function removeChip(tag) {
  _panelTags = _panelTags.filter(t => t !== tag);
  _renderTagChips();
  _updatePreview();
}

function _getTextValue() {
  if (_currentField === 'title') return document.getElementById('panel-text-input').value.trim();
  if (_currentField === 'description') return document.getElementById('panel-textarea').value.trim();
  return '';
}

function _updatePreview() {
  const previewEl = document.getElementById('panel-preview');
  const confirmBtn = document.getElementById('panel-confirm-btn');

  // Determine if there's something to submit
  const hasInput = (_currentField === 'tags_add' || _currentField === 'tags_remove')
    ? _panelTags.length > 0
    : _getTextValue().length > 0;

  if (!hasInput) {
    previewEl.innerHTML = '';
    confirmBtn.disabled = true;
    return;
  }

  // For manual selection with data loaded, compute locally
  if (!_selectAllFilter && _selectedIds.size > 0 && _selectedIds.size <= Object.keys(_photoData).length) {
    const ids = [..._selectedIds];
    const skipExisting = document.getElementById('skip-existing-cb')?.checked ?? true;
    let willUpdate = 0, willSkip = 0;

    for (const id of ids) {
      const d = _photoData[id];
      if (!d) continue;  // not on this page — skip local preview
      if (_currentField === 'title') {
        if (skipExisting && (d.title || '').trim()) { willSkip++; } else { willUpdate++; }
      } else if (_currentField === 'description') {
        if (skipExisting && (d.description || '').trim()) { willSkip++; } else { willUpdate++; }
      } else if (_currentField === 'tags_add') {
        const missing = _panelTags.filter(t => !d.tags.includes(t));
        if (missing.length) willUpdate++; else willSkip++;
      } else if (_currentField === 'tags_remove') {
        const present = _panelTags.filter(t => d.tags.includes(t));
        if (present.length) willUpdate++; else willSkip++;
      }
    }
    _renderPreviewCounts(willUpdate, willSkip);
    confirmBtn.disabled = willUpdate === 0;
    return;
  }

  // Fall back to server-side preview for filter-based selection
  _fetchPreview().then(result => {
    _renderPreviewCounts(result.would_update, result.would_skip);
    confirmBtn.disabled = result.would_update === 0;
  });
}

function _renderPreviewCounts(willUpdate, willSkip) {
  const previewEl = document.getElementById('panel-preview');
  const isRemove = _currentField === 'tags_remove';
  const updateHtml = isRemove
    ? `<span class="warn">${willUpdate} photo${willUpdate !== 1 ? 's' : ''} will be updated</span>`
    : `<strong>${willUpdate} photo${willUpdate !== 1 ? 's' : ''} will be updated</strong>`;
  const skipHtml = willSkip ? ` · ${willSkip} skipped` : '';
  previewEl.innerHTML = updateHtml + skipHtml;
}

async function _fetchPreview() {
  const payload = _buildPayload(true);
  try {
    const r = await fetch('/api/bulk-edit', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest'},
      body: JSON.stringify(payload),
    });
    return await r.json();
  } catch (e) {
    return {would_update: 0, would_skip: 0};
  }
}

function _buildPayload(dryRun) {
  const payload = {
    field: _currentField,
    dry_run: dryRun,
  };
  if (_currentField === 'title' || _currentField === 'description') {
    payload.value = _getTextValue();
    payload.skip_existing = document.getElementById('skip-existing-cb')?.checked ?? true;
  } else {
    payload.tags = _panelTags;
  }
  if (_selectAllFilter) {
    const form = document.getElementById('lib-filter-form');
    const fd = new FormData(form);
    payload.filter = {
      date_from: fd.get('date_from') || null,
      date_to: fd.get('date_to') || null,
      album_id: fd.get('album_id') ? parseInt(fd.get('album_id')) : null,
      tag: fd.get('tag') || null,
      status: fd.get('status') || null,
      untitled: fd.get('untitled') === '1',
    };
  } else {
    payload.photo_ids = [..._selectedIds];
  }
  return payload;
}

async function submitPanel() {
  const btn = document.getElementById('panel-confirm-btn');
  btn.disabled = true;
  btn.textContent = 'Queuing…';
  try {
    const payload = _buildPayload(false);
    const r = await fetch('/api/bulk-edit', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest'},
      body: JSON.stringify(payload),
    });
    const data = await r.json();
    if (data.ok) {
      const n = data.proposals_created;
      btn.textContent = `✓ Queued ${n} proposal${n !== 1 ? 's' : ''}`;
      setTimeout(() => {
        closePanel();
        clearSelection();
        btn.textContent = 'Queue proposals';
      }, 1500);
    } else {
      btn.textContent = 'Error — try again';
      setTimeout(() => { btn.textContent = 'Queue proposals'; btn.disabled = false; }, 2000);
    }
  } catch (e) {
    btn.textContent = 'Error';
    btn.disabled = false;
  }
}
</script>
{% endblock %}
```

- [ ] **Step 5: Add Library nav link to `reviewer/templates/base.html`**

After the Proposals nav link, add:
```html
  <a href="{{ url_for('library') }}" {% if request.endpoint == 'library' %}class="active"{% endif %}><kbd class="nav-key">8</kbd>Library</a>
```

In the mobile nav drawer, add after the Proposals entry:
```html
    <a href="{{ url_for('library') }}" {% if request.endpoint == 'library' %}class="active"{% endif %}>
      Library
    </a>
```

In the keyboard shortcut JS at the bottom of base.html, add `'8'` to the nav map:
```javascript
    '8': {{ url_for('library') | tojson }},
```

- [ ] **Step 6: Run tests**

```bash
python -m pytest tests/test_bulk_operations.py::TestLibraryRoute -v
```

Expected: 5 passed.

- [ ] **Step 7: Run full suite + lint**

```bash
python -m pytest tests/ -q && make lint
```

- [ ] **Step 8: Commit**

```bash
git add reviewer/app.py reviewer/templates/library.html reviewer/templates/base.html tests/test_bulk_operations.py
git commit -m "feat(#133): /library route, template, nav entry"
```

---

### Task 5: `POST /api/bulk-edit` endpoint (TDD)

**Files:**
- Modify: `reviewer/app.py`
- Modify: `tests/test_bulk_operations.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_bulk_operations.py`:

```python
# ===========================================================================
# Task 5 — POST /api/bulk-edit
# ===========================================================================


@pytest.fixture(scope="module")
def bulk_client():
    with tempfile.TemporaryDirectory() as tmp:
        test_db = Database(Path(tmp) / "test.db")
        for i in range(1, 4):
            test_db.upsert_photo({
                "uuid": f"be-uuid-{i}",
                "flickr_id": f"be-flickr-{i}",
                "original_filename": f"BE_{i:04d}.JPG",
                "privacy_state": "already_public",
                "flickr_title": "Existing" if i == 1 else "",
                "flickr_description": "",
                "flickr_tags": json.dumps(["paris"] if i == 1 else []),
                "photos_tags": json.dumps([]),
                "apple_persons": [],
                "proposed_tags": [],
            })
        app_module._db = test_db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test-secret"
        with app_module.app.test_client() as c:
            yield c, test_db
        app_module._db = None


class TestBulkEditEndpoint:
    def _post(self, client, payload):
        return client.post(
            "/api/bulk-edit",
            json=payload,
            headers={"X-Requested-With": "XMLHttpRequest"},
        )

    def test_bulk_edit_set_title_returns_ok(self, bulk_client):
        c, db = bulk_client
        ids = [r["id"] for r in db.library_photos()]
        resp = self._post(c, {"field": "title", "value": "Test", "photo_ids": ids})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert "proposals_created" in data
        assert "batch_id" in data

    def test_bulk_edit_dry_run_returns_counts_not_proposals(self, bulk_client):
        c, db = bulk_client
        ids = [r["id"] for r in db.library_photos()]
        resp = self._post(c, {
            "field": "title", "value": "Dry", "photo_ids": ids,
            "dry_run": True, "skip_existing": True,
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert "would_update" in data
        assert "would_skip" in data
        # dry_run must not create any proposals
        pending = db.conn.execute(
            "SELECT COUNT(*) FROM metadata_proposals WHERE status='pending'"
        ).fetchone()[0]
        # only proposals from previous test — dry run adds none
        assert data.get("batch_id") is None

    def test_bulk_edit_tags_add(self, bulk_client):
        c, db = bulk_client
        ids = [r["id"] for r in db.library_photos()]
        # Clear leftover proposals
        db.conn.execute("DELETE FROM metadata_proposals")
        db.conn.execute("DELETE FROM bulk_batches")
        db.conn.commit()
        resp = self._post(c, {"field": "tags_add", "tags": ["mfa-boston"], "photo_ids": ids})
        data = resp.get_json()
        assert data["ok"] is True
        assert data["proposals_created"] >= 1

    def test_bulk_edit_filter_based_selection(self, bulk_client):
        c, db = bulk_client
        db.conn.execute("DELETE FROM metadata_proposals")
        db.conn.execute("DELETE FROM bulk_batches")
        db.conn.commit()
        resp = self._post(c, {
            "field": "tags_add",
            "tags": ["london"],
            "filter": {"status": "public", "date_from": None, "date_to": None,
                       "album_id": None, "tag": None, "untitled": False},
        })
        data = resp.get_json()
        assert data["ok"] is True

    def test_bulk_edit_missing_field_returns_400(self, bulk_client):
        c, _ = bulk_client
        resp = self._post(c, {"value": "X", "photo_ids": [1]})
        assert resp.status_code == 400

    def test_bulk_edit_tags_requires_tags_list(self, bulk_client):
        c, _ = bulk_client
        resp = self._post(c, {"field": "tags_add", "photo_ids": [1]})
        assert resp.status_code == 400
```

- [ ] **Step 2: Run to confirm failure**

```bash
python -m pytest tests/test_bulk_operations.py::TestBulkEditEndpoint -v
```

Expected: 404 — endpoint not yet defined.

- [ ] **Step 3: Add `POST /api/bulk-edit` to `reviewer/app.py`**

Add after the `api_zone_delete` route:

```python
@app.route("/api/bulk-edit", methods=["POST"])
def api_bulk_edit() -> _JsonResp:
    """
    Bulk-edit metadata across a set of photos.

    Payload (JSON):
      field        str   — 'title' | 'description' | 'tags_add' | 'tags_remove'
      dry_run      bool  — if true, return counts without creating proposals
      skip_existing bool — for title/description: skip photos that already have a value
      value        str   — new text (for title/description)
      tags         list  — tags to add/remove (for tag ops)
      photo_ids    list  — explicit selection (mutually exclusive with filter)
      filter       dict  — {date_from, date_to, album_id, tag, status, untitled}

    Returns:
      {ok, proposals_created, batch_id}          (commit)
      {ok, would_update, would_skip, batch_id:null}  (dry_run)
    """
    data = request.get_json() or {}

    field = data.get("field")
    if field not in ("title", "description", "tags_add", "tags_remove"):
        return jsonify({"ok": False, "error": "field must be title/description/tags_add/tags_remove"}), 400

    is_tag_op = field in ("tags_add", "tags_remove")
    value: str | None = data.get("value") if not is_tag_op else None
    tags: list | None = data.get("tags") if is_tag_op else None

    if is_tag_op and not isinstance(tags, list):
        return jsonify({"ok": False, "error": "tags must be a list for tag operations"}), 400

    dry_run = bool(data.get("dry_run", False))
    skip_existing = bool(data.get("skip_existing", True))

    # Resolve photo IDs
    _filter = data.get("filter")
    photo_ids: list[int]
    filter_json: str | None = None

    if _filter is not None:
        filter_json = json.dumps(_filter)
        photo_ids = db().library_photo_ids(
            date_from=_filter.get("date_from"),
            date_to=_filter.get("date_to"),
            album_id=_filter.get("album_id"),
            tag=_filter.get("tag"),
            status=_filter.get("status"),
            untitled_only=bool(_filter.get("untitled")),
        )
    elif isinstance(data.get("photo_ids"), list):
        photo_ids = [int(i) for i in data["photo_ids"]]
    else:
        return jsonify({"ok": False, "error": "provide photo_ids or filter"}), 400

    if not photo_ids:
        return jsonify({"ok": True, "proposals_created": 0, "batch_id": None,
                        "would_update": 0, "would_skip": 0})

    # For dry_run: compute counts without writing
    if dry_run:
        # Use a scratch batch to count, then roll back
        _db = db()
        # Compute manually to avoid writing
        placeholders = ",".join("?" * len(photo_ids))
        rows = _db.conn.execute(
            f"""SELECT id, flickr_id, flickr_title, flickr_description,
                       flickr_tags, photos_title
                FROM photos
                WHERE id IN ({placeholders}) AND flickr_id IS NOT NULL AND flickr_deleted = 0""",
            photo_ids,
        ).fetchall()

        would_update = would_skip = 0
        for row in rows:
            if field == "title":
                existing = (row["flickr_title"] or "").strip()
                if skip_existing and existing:
                    would_skip += 1
                else:
                    would_update += 1
            elif field == "description":
                existing = (row["flickr_description"] or "").strip()
                if skip_existing and existing:
                    would_skip += 1
                else:
                    would_update += 1
            elif field == "tags_add":
                from db.db import _json_loads_safe
                current = _json_loads_safe(row["flickr_tags"])
                missing = [t for t in (tags or []) if t not in current]
                if missing:
                    would_update += 1
                else:
                    would_skip += 1
            elif field == "tags_remove":
                from db.db import _json_loads_safe
                current = _json_loads_safe(row["flickr_tags"])
                present = [t for t in (tags or []) if t in current]
                if present:
                    would_update += 1
                else:
                    would_skip += 1

        return jsonify({"ok": True, "would_update": would_update,
                        "would_skip": would_skip, "batch_id": None})

    # Commit path
    _db = db()
    operation_map = {
        "title": "set_title", "description": "set_description",
        "tags_add": "tags_add", "tags_remove": "tags_remove",
    }
    db_field_map = {
        "title": "title", "description": "description",
        "tags_add": None, "tags_remove": None,
    }

    batch_id = _db.create_bulk_batch(
        operation=operation_map[field],
        field=db_field_map[field],
        value=value,
        tags=tags,
        filter_json=filter_json,
        photo_count=len(photo_ids),
    )

    created = _db.insert_bulk_proposals(
        batch_id=batch_id,
        photo_ids=photo_ids,
        field=field,
        value=value,
        tags=tags,
        skip_existing=skip_existing,
    )

    return jsonify({"ok": True, "proposals_created": created, "batch_id": batch_id})
```

- [ ] **Step 4: Fix the `_json_loads_safe` import in `api_bulk_edit`**

`_json_loads_safe` is a module-level function in `db/db.py`. Rather than importing it inside the route, use it directly from the db instance since it's already imported at the module level. Replace the two inline imports in the dry_run block:

```python
            elif field == "tags_add":
                current = _json_loads_safe(row["flickr_tags"])
```

where `_json_loads_safe` is `db.db._json_loads_safe`. Since `db.py` is already imported via `from db.db import Database`, add to the top of `app.py` (near the other imports):

```python
from db.db import Database, _json_loads_safe as _parse_json_list
```

Then use `_parse_json_list` in the dry_run block.

- [ ] **Step 5: Run tests**

```bash
python -m pytest tests/test_bulk_operations.py::TestBulkEditEndpoint -v
```

Expected: all pass.

- [ ] **Step 6: Run full suite + lint**

```bash
python -m pytest tests/ -q && make lint
```

- [ ] **Step 7: Commit**

```bash
git add reviewer/app.py tests/test_bulk_operations.py
git commit -m "feat(#133): POST /api/bulk-edit — dry_run + commit, explicit and filter-based selection"
```

---

### Task 6: Proposals batch grouping + reject endpoint (TDD)

**Files:**
- Modify: `reviewer/app.py`
- Modify: `reviewer/templates/proposals.html`
- Modify: `tests/test_bulk_operations.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_bulk_operations.py`:

```python
# ===========================================================================
# Task 6 — Proposals batch grouping + reject endpoint
# ===========================================================================


@pytest.fixture(scope="module")
def batch_client():
    with tempfile.TemporaryDirectory() as tmp:
        test_db = Database(Path(tmp) / "test.db")
        pid = test_db.upsert_photo({
            "uuid": "batch-u1", "flickr_id": "batch-f1",
            "original_filename": "BATCH.JPG",
            "privacy_state": "already_public",
            "flickr_title": "", "flickr_description": "",
            "flickr_tags": json.dumps([]),
            "photos_tags": json.dumps([]),
            "apple_persons": [], "proposed_tags": [],
        })
        # Create a batch with one proposal
        bid = test_db.create_bulk_batch("set_title", "title", "Batch Test", None, None, 1)
        test_db.insert_bulk_proposals(bid, [pid], "title", value="Batch Test")
        app_module._db = test_db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test-secret"
        with app_module.app.test_client() as c:
            yield c, test_db, bid
        app_module._db = None


class TestProposalsBatchGrouping:
    def test_proposals_page_shows_batch_section(self, batch_client):
        c, db, bid = batch_client
        resp = c.get("/proposals")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "Bulk" in html or "bulk" in html or "batch" in html.lower()

    def test_reject_batch_endpoint(self, batch_client):
        c, db, bid = batch_client
        # Create a fresh batch to reject (the fixture batch)
        resp = c.post(
            f"/api/bulk-batches/{bid}/reject",
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["rejected"] >= 1

    def test_reject_batch_nonexistent(self, batch_client):
        c, _, _ = batch_client
        resp = c.post(
            "/api/bulk-batches/99999/reject",
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["rejected"] == 0
```

- [ ] **Step 2: Run to confirm failure**

```bash
python -m pytest tests/test_bulk_operations.py::TestProposalsBatchGrouping -v
```

Expected: `proposals_page_shows_batch_section` fails (no batch section yet); reject endpoint 404.

- [ ] **Step 3: Pass batch data to proposals route in `reviewer/app.py`**

In the existing `proposals()` route function, add `bulk_batches` to the template context:

```python
@app.route("/proposals")
def proposals() -> str:
    page = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 50))
    offset = (page - 1) * per_page
    items = db().get_pending_proposals(limit=per_page, offset=offset)
    counts = db().get_proposal_counts()
    total = counts["total"]
    bulk_batches = db().get_pending_bulk_batches()          # ← add this
    return render_template(
        "proposals.html",
        proposals=items,
        counts=counts,
        page=page,
        total_pages=max(1, (total + per_page - 1) // per_page),
        total=total,
        bulk_batches=bulk_batches,                          # ← add this
    )
```

- [ ] **Step 4: Add batch-summary section to `reviewer/templates/proposals.html`**

At the very top of `{% block content %}`, before the existing toolbar, add:

```html
{% if bulk_batches %}
<div style="margin: 12px 24px 0;">
  <div style="font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin-bottom:8px">
    Bulk operations — {{ bulk_batches|length }} pending batch{{ 'es' if bulk_batches|length != 1 }}
  </div>
  {% for batch in bulk_batches %}
  <div style="background:var(--surface);border:1px solid var(--border);border-left:3px solid #3a5fff;
              border-radius:var(--radius);padding:10px 16px;margin-bottom:8px;
              display:flex;align-items:center;gap:12px;">
    <div style="flex:1;min-width:0">
      <span style="font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em">Bulk · </span>
      <strong style="font-size:13px">
        {% if batch.operation == 'set_title' %}Set title: "{{ batch.value }}"
        {% elif batch.operation == 'set_description' %}Set description
        {% elif batch.operation == 'tags_add' %}Add tags: {{ batch.tags | join(', ') if batch.tags else '—' }}
        {% elif batch.operation == 'tags_remove' %}Remove tags: {{ batch.tags | join(', ') if batch.tags else '—' }}
        {% else %}{{ batch.operation }}{% endif %}
      </strong>
      <span style="font-size:12px;color:var(--muted);margin-left:8px">
        {{ batch.pending_count }} proposal{{ 's' if batch.pending_count != 1 }} pending
        · queued {{ batch.created_at[:10] }}
      </span>
    </div>
    <button onclick="rejectBatch({{ batch.id }}, this)"
            style="background:rgba(139,32,32,.2);border:1px solid var(--red);color:#f88;
                   border-radius:var(--radius);padding:4px 12px;font-size:12px;cursor:pointer">
      Reject all
    </button>
  </div>
  {% endfor %}
</div>
{% endif %}
```

Add the JS for `rejectBatch` in the proposals template's `{% block scripts %}` (or append to existing script block):

```html
<script>
async function rejectBatch(batchId, btn) {
  if (!confirm('Reject all pending proposals in this batch?')) return;
  btn.disabled = true;
  btn.textContent = 'Rejecting…';
  try {
    const r = await fetch(`/api/bulk-batches/${batchId}/reject`, {
      method: 'POST',
      headers: {'X-Requested-With': 'XMLHttpRequest'},
    });
    const data = await r.json();
    if (data.ok) {
      btn.closest('div[style]').remove();
    } else {
      btn.disabled = false;
      btn.textContent = 'Reject all';
    }
  } catch (e) {
    btn.disabled = false;
    btn.textContent = 'Reject all';
  }
}
</script>
```

- [ ] **Step 5: Add `POST /api/bulk-batches/<id>/reject` to `reviewer/app.py`**

```python
@app.route("/api/bulk-batches/<int:batch_id>/reject", methods=["POST"])
def api_bulk_batch_reject(batch_id: int) -> _JsonResp:
    n = db().reject_bulk_batch(batch_id)
    return jsonify({"ok": True, "rejected": n})
```

- [ ] **Step 6: Run tests**

```bash
python -m pytest tests/test_bulk_operations.py::TestProposalsBatchGrouping -v
```

Expected: all pass.

- [ ] **Step 7: Run full suite + lint**

```bash
python -m pytest tests/ -q && make lint
```

- [ ] **Step 8: Commit**

```bash
git add reviewer/app.py reviewer/templates/proposals.html tests/test_bulk_operations.py
git commit -m "feat(#133): proposals batch grouping + POST /api/bulk-batches/<id>/reject"
```

---

### Task 7: Review queue select mode

**Files:**
- Modify: `reviewer/templates/review.html`
- Modify: `tests/test_bulk_operations.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_bulk_operations.py`:

```python
# ===========================================================================
# Task 7 — Review queue select mode
# ===========================================================================


class TestReviewSelectMode:
    def test_review_page_has_select_toggle(self, lib_client):
        resp = lib_client.get("/review?state=candidate_public")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "select-mode" in html or "Select" in html
```

- [ ] **Step 2: Run to confirm failure**

```bash
python -m pytest tests/test_bulk_operations.py::TestReviewSelectMode -v
```

Expected: assertion error — no select mode in review template yet.

- [ ] **Step 3: Add select mode to `reviewer/templates/review.html`**

Locate the existing review toolbar (the `<div class="toolbar">` near the top of `{% block content %}`). Add a "Select" toggle button to the right side of the toolbar:

```html
<button id="select-mode-btn"
        onclick="toggleSelectMode()"
        title="Bulk select"
        style="background:none;border:1px solid var(--border);color:var(--muted);
               padding:4px 10px;border-radius:var(--radius);font-size:12px;cursor:pointer">
  Select
</button>
```

Add the select-mode CSS in `{% block extra_style %}` (append after existing styles):

```css
/* ── Review select mode ─────────────────────────── */
body.select-mode .thumb-wrap { cursor: pointer; }
body.select-mode .thumb-wrap input[type=checkbox] { display: block; }
.thumb-wrap input[type=checkbox] {
  display: none;
  position: absolute; top: 5px; left: 5px;
  width: 18px; height: 18px; accent-color: #3a5fff; z-index: 5;
}
.thumb-wrap.selected { outline: 2px solid #3a5fff; outline-offset: -2px; }

/* Action bar (same style as library.html) */
.review-action-bar {
  display: none;
  align-items: center;
  gap: 12px;
  padding: 8px 16px;
  background: #1a2a4a;
  border-top: 1px solid #3a5fff;
  font-size: 13px;
  position: fixed;
  bottom: 0; left: 0; right: 0;
  z-index: 30;
}
.review-action-bar.visible { display: flex; }
.review-action-bar button {
  background: none; border: none;
  color: #7a9fff; cursor: pointer; font-size: 13px;
}
.review-action-bar .sel-count { color: #ccc; font-weight: 600; }
.review-action-bar .clear-btn { color: var(--muted); margin-left: auto; }

/* Inline edit panel (fixed above action bar) */
.review-edit-panel {
  display: none;
  padding: 12px 16px;
  background: #141e38;
  border-top: 2px solid #3a5fff;
  position: fixed;
  bottom: 42px; left: 0; right: 0;
  z-index: 29;
}
.review-edit-panel.visible { display: block; }
```

Add the action bar and panel markup just before `{% endblock %}` (before `{% block scripts %}`):

```html
<!-- Review select mode: action bar + inline panel -->
<div class="review-action-bar" id="rev-action-bar">
  <span class="sel-count" id="rev-sel-count">0 selected</span>
  <span style="color:#444">│</span>
  <button onclick="revOpenPanel('title')">Edit title</button>
  <button onclick="revOpenPanel('description')">Edit description</button>
  <button onclick="revOpenPanel('tags_add')">Add tags</button>
  <button onclick="revOpenPanel('tags_remove')">Remove tags</button>
  <button class="clear-btn" onclick="revClearSelection()">✕ Clear</button>
</div>

<div class="review-edit-panel" id="rev-edit-panel">
  <h4 id="rev-panel-title" style="color:#7a9fff;font-size:12px;text-transform:uppercase;
      letter-spacing:.06em;margin-bottom:10px">Edit</h4>
  <div id="rev-text-wrap" style="display:none">
    <input type="text" id="rev-text-input"
           style="width:100%;background:var(--bg);border:1px solid #3a5fff;
                  color:var(--text);border-radius:var(--radius);padding:6px 10px;
                  font-size:13px;box-sizing:border-box">
    <label style="font-size:12px;color:var(--muted);display:flex;align-items:center;gap:5px;margin-top:8px">
      <input type="checkbox" id="rev-skip-cb" checked> Skip photos that already have a value
    </label>
  </div>
  <div id="rev-tag-wrap" style="display:none">
    <div id="rev-chip-wrap"
         style="display:flex;flex-wrap:wrap;gap:6px;align-items:center;
                background:var(--bg);border:1px solid #3a5fff;
                border-radius:var(--radius);padding:6px 8px;min-height:36px">
      <input id="rev-chip-input" placeholder="type tag, press Enter…"
             style="background:none;border:none;outline:none;color:var(--text);
                    font-size:12px;min-width:80px;flex:1">
    </div>
  </div>
  <div id="rev-preview" style="margin:8px 0;font-size:12px;color:var(--muted)"></div>
  <div style="display:flex;align-items:center;gap:10px;margin-top:10px">
    <button id="rev-confirm-btn" onclick="revSubmitPanel()" disabled
            style="background:#3a5fff;color:white;border:none;
                   border-radius:var(--radius);padding:6px 16px;font-size:13px;cursor:pointer">
      Queue proposals
    </button>
    <button onclick="revClosePanel()"
            style="background:none;border:none;color:var(--muted);font-size:13px;cursor:pointer">
      Cancel
    </button>
  </div>
</div>
```

Add the select-mode JS in `{% block scripts %}` (append to the end of the existing script block, or add a new one):

```html
<script>
// ── Review select mode ────────────────────────────────────────────────
let _revSelected = new Set();
let _revField = null;
let _revTags = [];

function toggleSelectMode() {
  const active = document.body.classList.toggle('select-mode');
  const btn = document.getElementById('select-mode-btn');
  btn.style.background = active ? 'rgba(58,95,255,.2)' : '';
  btn.style.color = active ? '#7a9fff' : '';
  btn.style.borderColor = active ? '#3a5fff' : '';
  if (!active) revClearSelection();
}

document.querySelectorAll('.thumb-wrap').forEach(el => {
  el.addEventListener('click', function(e) {
    if (!document.body.classList.contains('select-mode')) return;
    if (e.target.tagName === 'INPUT') return;
    const id = parseInt(this.dataset.id);
    const cb = this.querySelector('input[type=checkbox]');
    if (_revSelected.has(id)) {
      _revSelected.delete(id);
      this.classList.remove('selected');
      cb.checked = false;
    } else {
      _revSelected.add(id);
      this.classList.add('selected');
      cb.checked = true;
    }
    _revUpdateUI();
  });
  const cb = el.querySelector('input[type=checkbox]');
  if (cb) {
    cb.addEventListener('change', function(e) {
      e.stopPropagation();
      const id = parseInt(el.dataset.id);
      if (this.checked) { _revSelected.add(id); el.classList.add('selected'); }
      else { _revSelected.delete(id); el.classList.remove('selected'); }
      _revUpdateUI();
    });
  }
});

function _revUpdateUI() {
  const n = _revSelected.size;
  document.getElementById('rev-sel-count').textContent = `${n} selected`;
  const bar = document.getElementById('rev-action-bar');
  if (n > 0) bar.classList.add('visible');
  else { bar.classList.remove('visible'); revClosePanel(); }
}

function revClearSelection() {
  _revSelected.clear();
  document.querySelectorAll('.thumb-wrap').forEach(el => {
    el.classList.remove('selected');
    const cb = el.querySelector('input[type=checkbox]');
    if (cb) cb.checked = false;
  });
  _revUpdateUI();
  revClosePanel();
}

function revOpenPanel(field) {
  _revField = field;
  _revTags = [];
  const panel = document.getElementById('rev-edit-panel');
  panel.classList.add('visible');
  document.getElementById('rev-panel-title').textContent =
    ({title:'Edit title', description:'Edit description',
      tags_add:'Add tags', tags_remove:'Remove tags'}[field]) +
    ` · ${_revSelected.size} photos`;
  const textWrap = document.getElementById('rev-text-wrap');
  const tagWrap = document.getElementById('rev-tag-wrap');
  if (field === 'title' || field === 'description') {
    textWrap.style.display = '';
    tagWrap.style.display = 'none';
    document.getElementById('rev-text-input').value = '';
    document.getElementById('rev-text-input').focus();
    document.getElementById('rev-text-input').oninput = _revUpdatePreview;
    document.getElementById('rev-skip-cb').onchange = _revUpdatePreview;
  } else {
    textWrap.style.display = 'none';
    tagWrap.style.display = '';
    document.getElementById('rev-chip-input').focus();
    _revRenderChips();
  }
  _revUpdatePreview();
}

function revClosePanel() {
  document.getElementById('rev-edit-panel').classList.remove('visible');
  document.getElementById('rev-preview').innerHTML = '';
  document.getElementById('rev-confirm-btn').disabled = true;
  _revField = null; _revTags = [];
  document.querySelectorAll('#rev-chip-wrap .tag-chip').forEach(c => c.remove());
}

document.getElementById('rev-chip-input').addEventListener('keydown', function(e) {
  if (e.key === 'Enter' || e.key === ',') {
    e.preventDefault();
    const val = this.value.trim();
    if (val && !_revTags.includes(val)) {
      _revTags.push(val);
      _revRenderChips();
      _revUpdatePreview();
    }
    this.value = '';
  } else if (e.key === 'Backspace' && !this.value && _revTags.length) {
    _revTags.pop();
    _revRenderChips();
    _revUpdatePreview();
  }
});

function _revRenderChips() {
  const wrap = document.getElementById('rev-chip-wrap');
  wrap.querySelectorAll('.tag-chip').forEach(c => c.remove());
  const input = document.getElementById('rev-chip-input');
  const isRemove = _revField === 'tags_remove';
  _revTags.forEach(tag => {
    const chip = document.createElement('span');
    chip.className = 'tag-chip' + (isRemove ? ' remove-chip' : '');
    chip.style.cssText = isRemove
      ? 'display:inline-flex;align-items:center;gap:4px;padding:2px 8px;border-radius:10px;font-size:12px;background:rgba(139,32,32,.25);border:1px solid #8b2020;color:#f88'
      : 'display:inline-flex;align-items:center;gap:4px;padding:2px 8px;border-radius:10px;font-size:12px;background:rgba(58,95,255,.25);border:1px solid #3a5fff;color:#aac';
    chip.innerHTML = `${tag} <button onclick="revRemoveChip('${tag}')" style="background:none;border:none;color:inherit;cursor:pointer;padding:0">×</button>`;
    wrap.insertBefore(chip, input);
  });
}

function revRemoveChip(tag) {
  _revTags = _revTags.filter(t => t !== tag);
  _revRenderChips();
  _revUpdatePreview();
}

function _revUpdatePreview() {
  const hasInput = (_revField === 'tags_add' || _revField === 'tags_remove')
    ? _revTags.length > 0
    : (document.getElementById('rev-text-input')?.value || '').trim().length > 0;
  const btn = document.getElementById('rev-confirm-btn');
  if (!hasInput) {
    document.getElementById('rev-preview').innerHTML = '';
    btn.disabled = true;
    return;
  }
  btn.disabled = false;
  document.getElementById('rev-preview').innerHTML =
    `<strong>${_revSelected.size} photo${_revSelected.size !== 1 ? 's' : ''}</strong> will be updated`;
}

async function revSubmitPanel() {
  const btn = document.getElementById('rev-confirm-btn');
  btn.disabled = true;
  btn.textContent = 'Queuing…';
  const payload = {
    field: _revField,
    photo_ids: [..._revSelected],
    skip_existing: document.getElementById('rev-skip-cb')?.checked ?? true,
  };
  if (_revField === 'title' || _revField === 'description') {
    payload.value = document.getElementById('rev-text-input').value.trim();
  } else {
    payload.tags = _revTags;
  }
  try {
    const r = await fetch('/api/bulk-edit', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest'},
      body: JSON.stringify(payload),
    });
    const data = await r.json();
    if (data.ok) {
      const n = data.proposals_created;
      btn.textContent = `✓ ${n} queued`;
      setTimeout(() => { revClosePanel(); revClearSelection(); btn.textContent = 'Queue proposals'; }, 1500);
    } else {
      btn.textContent = 'Error'; btn.disabled = false;
    }
  } catch (e) { btn.textContent = 'Error'; btn.disabled = false; }
}
</script>
```

Also add `data-id="{{ photo.id }}"` to each `.thumb-wrap` div and add `<input type="checkbox" class="photo-cb" data-id="{{ photo.id }}">` inside each thumb-wrap (before the `<img>`), if those attributes aren't already present. Check the existing review.html template to find the thumb-wrap div and add these attributes.

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/test_bulk_operations.py::TestReviewSelectMode -v
```

Expected: pass.

- [ ] **Step 5: Run full suite + lint**

```bash
python -m pytest tests/ -q && make lint
```

- [ ] **Step 6: Commit**

```bash
git add reviewer/templates/review.html tests/test_bulk_operations.py
git commit -m "feat(#133): review queue select mode — bulk action bar + inline panel"
```

---

### Task 8: README + close issue

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Update README**

Update the feature list in `README.md` to mention the library view and bulk operations. Add a line in the "What it does" list:

```
- Provides a library view with multi-select for bulk title, description, and tag editing across photo sets; changes queue as proposals before writing
```

Update the test count line (e.g. "1111 tests" → reflect actual count after `python -m pytest tests/ -q`).

- [ ] **Step 2: Run full suite to get final test count**

```bash
python -m pytest tests/ -q 2>&1 | tail -3
```

Update the test count in README.md to match.

- [ ] **Step 3: Commit + push**

```bash
git add README.md
git commit -m "docs(#133): update README for bulk operations feature

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
git push origin main
```

- [ ] **Step 4: Close GitHub issue**

```bash
gh issue comment 133 --body "Implementation complete. All tasks shipped:
- Migration 023: \`bulk_batches\` table + nullable \`batch_id\` on \`metadata_proposals\`
- \`/library\` page with horizontal filter bar, checkbox selection (manual + select-all-matching), and pagination
- Inline edit panel for title, description, add tags, remove tags — grid stays visible while panel is open
- \`POST /api/bulk-edit\` with \`dry_run\` support and both explicit and filter-based selection
- Proposals page batch-summary section + \`POST /api/bulk-batches/<id>/reject\`
- Review queue select mode with the same action bar and inline panel"

gh issue close 133 --reason completed
```
