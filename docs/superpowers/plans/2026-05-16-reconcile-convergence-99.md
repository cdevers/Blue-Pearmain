# GH #99 — Reconcile Convergence + Tag Write-back Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop `bp reconcile` from reprocessing the same photos on every run by introducing a `pushed_tags` write ledger, and add `bp tag-writeback` to propagate those tags back to Photos.app as explicit keywords for Smart Album use.

**Architecture:** A new `pushed_tags TEXT` column records the cumulative set of tags ever successfully pushed to Flickr. Reconcile checks `pushed_tags ⊆ actual_flickr_tags` instead of `proposed_tags ⊆ actual_flickr_tags`. Since `pushed_tags` only grows on confirmed push success (never on scan), the living-document drift that caused non-convergence is eliminated. A new `bp tag-writeback` subcommand reads `pushed_tags` from the DB and writes them back to Photos.app via `photoscript`.

**Critical invariants:**
1. **Flickr API call first, DB ledger update second — never the reverse.** A crash between the two is safe: reconcile will detect the gap and re-push. Updating the DB first would falsely mark tags as pushed on API failure.
2. **`pushed_tags` only grows on confirmed Flickr success.** It is never written on scan, never written on API failure, and never contains an empty JSON array `'[]'` — use NULL for "nothing pushed yet."
3. **`pushed_tags` represents desired state.** Reconcile will re-add any tag in `pushed_tags` that disappears from Flickr (including manual user deletions). This is intentional, not a bug.

**Tech Stack:** Python, SQLite (ALTER TABLE ADD COLUMN), `photoscript` (AppleScript bridge for Photos.app keyword write), `unittest.mock` for testing.

---

## File Map

| File | Role |
|---|---|
| `db/schema.sql` | Add `pushed_tags TEXT` column definition |
| `db/migrations/migrate_016_pushed_tags.py` | New migration — adds column, idempotent |
| `poller/poller.py` | Write `pushed_tags` after initial tag push |
| `poller/reconcile.py` | Read `pushed_tags`; add `db` param to `check_photo`; write back after fix |
| `reviewer/app.py` | Write `pushed_tags` after UI bulk push |
| `poller/tag_writeback.py` | New subcommand — merge `pushed_tags` into Photos.app keywords |
| `bp` | Wire `tag-writeback` into CLI (cmd function + subparser + dispatch) |
| `tests/test_core.py` | Five new test classes (RED-first TDD) |
| `README.md` | Document `bp tag-writeback`; update test count |

---

### Task 1: Migration 016 — add `pushed_tags` column

**Files:**
- Create: `db/migrations/migrate_016_pushed_tags.py`
- Modify: `db/schema.sql`
- Test: `tests/test_core.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_core.py` (before the `if __name__ == "__main__":` line):

```python
# ---------------------------------------------------------------------------
# GH #99 — Task 1: Migration 016 (add pushed_tags column)
# ---------------------------------------------------------------------------


class TestMigrate016PushedTags(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "test.db")

    def tearDown(self):
        self._tmp.cleanup()

    def _cols(self):
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        cols = [row[1] for row in conn.execute("PRAGMA table_info(photos)").fetchall()]
        conn.close()
        return cols

    def test_column_exists_after_run(self):
        from db.migrations.migrate_016_pushed_tags import run
        db = Database(Path(self.db_path))
        db.close()
        run(self.db_path)
        self.assertIn("pushed_tags", self._cols())

    def test_existing_rows_get_null(self):
        import json, sqlite3
        from db.migrations.migrate_016_pushed_tags import run
        db = Database(Path(self.db_path))
        photo_id = db.upsert_photo({
            "uuid": "uuid-mig016",
            "original_filename": "IMG_mig016.JPG",
            "apple_persons": [],
            "apple_labels": [],
        })
        db.close()
        run(self.db_path)
        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT pushed_tags FROM photos WHERE id = ?", (photo_id,)).fetchone()
        conn.close()
        self.assertIsNone(row[0])

    def test_migration_is_idempotent(self):
        from db.migrations.migrate_016_pushed_tags import run
        db = Database(Path(self.db_path))
        db.close()
        run(self.db_path)
        run(self.db_path)  # must not raise
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python -m pytest tests/test_core.py::TestMigrate016PushedTags -q
```

Expected: `ERROR` — `ModuleNotFoundError: No module named 'db.migrations.migrate_016_pushed_tags'`

- [ ] **Step 3: Create the migration**

**NULL vs `'[]'` note:** NULL means "BP has never confirmed pushing any tags." An empty JSON array `'[]'` must never be written to `pushed_tags` — the write sites all guard against empty tag lists. This keeps `WHERE pushed_tags IS NOT NULL` unambiguous as the predicate for "has a ledger entry."

Create `db/migrations/migrate_016_pushed_tags.py`:

```python
"""
migrate_016_pushed_tags.py

Adds pushed_tags TEXT column to the photos table.

pushed_tags is the write ledger: the cumulative set of tags BP has
ever successfully pushed to Flickr for a photo. NULL means nothing
has been pushed and confirmed. Existing rows get NULL (correct default).

Safe to run multiple times (idempotent).

Usage:
    python db/migrations/migrate_016_pushed_tags.py --config config/config.yml
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import yaml

MIGRATION_NAME = "migrate_016_pushed_tags"


def _already_migrated(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute(
            "SELECT id FROM schema_migrations WHERE name = ?", (MIGRATION_NAME,)
        ).fetchone()
        if row is not None:
            return True
    except Exception:
        pass
    cols = [row[1] for row in conn.execute("PRAGMA table_info(photos)").fetchall()]
    return "pushed_tags" in cols


def run(db_path: str, dry_run: bool = False) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    if _already_migrated(conn):
        print("  Skipped:  migration already applied")
        conn.close()
        return

    if not dry_run:
        conn.execute("ALTER TABLE photos ADD COLUMN pushed_tags TEXT")
        conn.execute(
            "INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)",
            (MIGRATION_NAME, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        print("  Applied:  added pushed_tags column to photos")
    else:
        print("  Dry-run:  would add pushed_tags column to photos")

    conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Migration 016 — add pushed_tags column")
    parser.add_argument("--config", default="config/config.yml")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with open(args.config) as f:
        config = yaml.safe_load(f)

    db_path = str(Path(config["database"]["path"]).expanduser())
    run(db_path, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Update `db/schema.sql`**

In `db/schema.sql`, add `pushed_tags` after the `proposed_tags` line (around line 83):

```sql
    -- Proposed tags (staged, not yet pushed)
    proposed_tags           TEXT,                   -- JSON array of tag strings
    pushed_tags             TEXT,                   -- JSON array; cumulative tags confirmed pushed to Flickr (write ledger)
    proposed_description    TEXT,                   -- draft description text (may be AI caption, edited)
```

- [ ] **Step 5: Run tests to confirm they pass**

```bash
python -m pytest tests/test_core.py::TestMigrate016PushedTags -q
```

Expected: `3 passed`

- [ ] **Step 6: Commit**

```bash
git add db/migrations/migrate_016_pushed_tags.py db/schema.sql tests/test_core.py
git commit -m "feat: add pushed_tags write ledger column (GH #99)

Migration 016 adds pushed_tags TEXT to photos — the cumulative set of
tags BP has successfully pushed to Flickr, distinct from proposed_tags
(living document) and flickr_tags (read-only puller cache).

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 2: Write `pushed_tags` on initial push — `poller.py`

**Files:**
- Modify: `poller/poller.py:370-393` (`_push_to_flickr`)
- Test: `tests/test_core.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_core.py`:

```python
# ---------------------------------------------------------------------------
# GH #99 — Task 2: pushed_tags written on initial push (poller)
# ---------------------------------------------------------------------------


class TestPushedTagsOnInitialPush(unittest.TestCase):

    def setUp(self):
        from unittest.mock import MagicMock
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "test.db")
        self.photo_id = self.db.upsert_photo({
            "uuid": "uuid-push-001",
            "original_filename": "IMG_push.JPG",
            "flickr_id": "flickr-push-001",
            "proposed_tags": ["cat", "indoor"],
            "privacy_state": "approved_public",
            "apple_persons": [],
            "apple_labels": [],
        })
        self.mock_client = MagicMock()

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _row(self):
        return dict(self.db.conn.execute(
            "SELECT * FROM photos WHERE id = ?", (self.photo_id,)
        ).fetchone())

    def _db_record(self):
        import json
        row = self._row()
        row["proposed_tags"] = json.loads(row["proposed_tags"] or "[]")
        return row

    def test_pushed_tags_written_on_success(self):
        import json
        from poller.poller import _push_to_flickr
        _push_to_flickr(self.mock_client, "flickr-push-001", self._db_record(), self.db, dry_run=False)
        pushed = json.loads(self._row()["pushed_tags"])
        self.assertEqual(pushed, ["cat", "indoor"])

    def test_pushed_tags_null_when_add_tags_fails(self):
        from flickr.flickr_client import FlickrError
        from poller.poller import _push_to_flickr
        self.mock_client.add_tags.side_effect = FlickrError("api error", code=0)
        _push_to_flickr(self.mock_client, "flickr-push-001", self._db_record(), self.db, dry_run=False)
        self.assertIsNone(self._row()["pushed_tags"])

    def test_pushed_tags_null_when_no_proposed_tags(self):
        import json
        from poller.poller import _push_to_flickr
        record = self._db_record()
        record["proposed_tags"] = []
        _push_to_flickr(self.mock_client, "flickr-push-001", record, self.db, dry_run=False)
        self.assertIsNone(self._row()["pushed_tags"])
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python -m pytest tests/test_core.py::TestPushedTagsOnInitialPush -q
```

Expected: `FAILED` — `AssertionError: None != ['cat', 'indoor']` (pushed_tags not written yet)

- [ ] **Step 3: Update `_push_to_flickr` in `poller/poller.py`**

**Transactional ordering:** the `add_tags()` API call must succeed before the DB write. The existing code already does this correctly — do not reorder.

Find the `add_tags` success block (around line 373) and replace:

```python
    tags = db_record.get("proposed_tags") or []
    if tags:
        try:
            client.add_tags(flickr_id, tags)
            db.conn.execute(
                "UPDATE photos SET tags_pushed_flickr = 1 WHERE flickr_id = ?", (flickr_id,)
            )
            log.info(f"  add_tags OK for {flickr_id} ({len(tags)} tags)")
```

with:

```python
    tags = db_record.get("proposed_tags") or []
    if tags:
        try:
            client.add_tags(flickr_id, tags)
            db.conn.execute(
                "UPDATE photos SET tags_pushed_flickr = 1, pushed_tags = ? WHERE flickr_id = ?",
                (json.dumps(sorted(tags)), flickr_id),
            )
            log.info(f"  add_tags OK for {flickr_id} ({len(tags)} tags)")
```

Also confirm `import json` exists at the top of `poller/poller.py`. It does (the file uses `_json` as an alias — check and add a plain `import json` if needed, or use the existing alias).

Check which json alias is used in the file:

```bash
grep -n "^import json\|_json\s*=" poller/poller.py | head -5
```

If the file uses `import json as _json`, replace `json.dumps` with `_json.dumps` in the line above.

- [ ] **Step 4: Run tests to confirm they pass**

```bash
python -m pytest tests/test_core.py::TestPushedTagsOnInitialPush -q
```

Expected: `3 passed`

- [ ] **Step 5: Run full suite**

```bash
python -m pytest tests/ -q
```

Expected: all passing.

- [ ] **Step 6: Commit**

```bash
git add poller/poller.py tests/test_core.py
git commit -m "feat: write pushed_tags after initial Flickr tag push (GH #99)

_push_to_flickr now records the exact tags pushed as pushed_tags in the
DB on confirmed add_tags success. This is the first write site for the
new pushed_tags ledger.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 3: Reconcile — use `pushed_tags`, update after fix

**Files:**
- Modify: `poller/reconcile.py` — `check_photo` signature, tag check source, fix write-back, query
- Test: `tests/test_core.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_core.py`:

```python
# ---------------------------------------------------------------------------
# GH #99 — Task 3: reconcile uses pushed_tags; updates it after fix
# ---------------------------------------------------------------------------


class TestReconcilePushedTags(unittest.TestCase):

    def setUp(self):
        from unittest.mock import MagicMock
        self._tmp = tempfile.mkdtemp()
        self.db = Database(Path(self._tmp) / "test.db")
        self.mock_client = MagicMock()

    def tearDown(self):
        self.db.close()
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _make_photo(self, pushed_tags=None, proposed_tags=None, flickr_id="flickr-pt-001"):
        import json
        photo_id = self.db.upsert_photo({
            "uuid": f"uuid-pt-{flickr_id}",
            "original_filename": "IMG_pt.JPG",
            "flickr_id": flickr_id,
            "privacy_state": "approved_public",
            "perms_pushed_flickr": 1,
            "tags_pushed_flickr": 1,
            "proposed_tags": proposed_tags or [],
            "apple_persons": [],
            "apple_labels": [],
        })
        if pushed_tags is not None:
            self.db.conn.execute(
                "UPDATE photos SET pushed_tags = ? WHERE id = ?",
                (json.dumps(pushed_tags), photo_id),
            )
            self.db.conn.commit()
        return photo_id

    def _row(self, photo_id):
        return dict(self.db.conn.execute(
            "SELECT * FROM photos WHERE id = ?", (photo_id,)
        ).fetchone())

    def _flickr_info(self, tags):
        return {
            "photo": {
                "visibility": {"ispublic": 1, "isfriend": 0, "isfamily": 0},
                "tags": {"tag": [{"raw": t} for t in tags]},
            }
        }

    # --- reads pushed_tags, not proposed_tags ---

    def test_null_pushed_tags_skips_tag_check(self):
        """pushed_tags=NULL → skip tag check even if proposed_tags is non-empty."""
        from poller.reconcile import check_photo
        photo_id = self._make_photo(pushed_tags=None, proposed_tags=["cat", "dog"])
        self.mock_client.get_photo_info.return_value = self._flickr_info([])
        result = check_photo(self.mock_client, self._row(photo_id), self.db, fix=False, verbose=False)
        self.assertEqual(result["status"], "ok")

    def test_pushed_tags_subset_of_flickr_is_ok(self):
        """pushed_tags ⊆ flickr_tags → ok, even if Flickr has extra tags."""
        from poller.reconcile import check_photo
        photo_id = self._make_photo(pushed_tags=["cat"], proposed_tags=["cat", "new-ml"])
        self.mock_client.get_photo_info.return_value = self._flickr_info(["cat", "extra"])
        result = check_photo(self.mock_client, self._row(photo_id), self.db, fix=False, verbose=False)
        self.assertEqual(result["status"], "ok")

    def test_pushed_tag_missing_from_flickr_is_mismatch(self):
        from poller.reconcile import check_photo
        photo_id = self._make_photo(pushed_tags=["cat", "dog"], proposed_tags=["cat", "dog"])
        self.mock_client.get_photo_info.return_value = self._flickr_info(["cat"])
        result = check_photo(self.mock_client, self._row(photo_id), self.db, fix=False, verbose=False)
        self.assertEqual(result["status"], "tag_mismatch")
        self.assertIn("dog", result["tags_missing"])

    def test_proposed_tag_not_in_pushed_tags_not_checked(self):
        """New ML label in proposed_tags but not in pushed_tags → not a mismatch."""
        from poller.reconcile import check_photo
        photo_id = self._make_photo(pushed_tags=["cat"], proposed_tags=["cat", "new-ml-label"])
        self.mock_client.get_photo_info.return_value = self._flickr_info(["cat"])
        result = check_photo(self.mock_client, self._row(photo_id), self.db, fix=False, verbose=False)
        self.assertEqual(result["status"], "ok")

    # --- fix writes pushed_tags back to DB ---

    def test_fix_appends_newly_pushed_tags(self):
        import json
        from poller.reconcile import check_photo
        photo_id = self._make_photo(pushed_tags=["cat"], proposed_tags=["cat"])
        self.mock_client.get_photo_info.return_value = self._flickr_info([])  # cat missing
        check_photo(self.mock_client, self._row(photo_id), self.db, fix=True, verbose=False)
        pushed = json.loads(self._row(photo_id)["pushed_tags"])
        self.assertIn("cat", pushed)

    def test_fix_preserves_existing_pushed_tags(self):
        import json
        from poller.reconcile import check_photo
        photo_id = self._make_photo(pushed_tags=["cat", "dog"], proposed_tags=["cat", "dog"])
        self.mock_client.get_photo_info.return_value = self._flickr_info(["cat"])  # dog missing
        check_photo(self.mock_client, self._row(photo_id), self.db, fix=True, verbose=False)
        pushed = json.loads(self._row(photo_id)["pushed_tags"])
        self.assertIn("cat", pushed)  # preserved
        self.assertIn("dog", pushed)  # re-confirmed

    def test_fix_does_not_update_pushed_tags_on_api_failure(self):
        import json
        from flickr.flickr_client import FlickrError
        from poller.reconcile import check_photo
        photo_id = self._make_photo(pushed_tags=["cat"], proposed_tags=["cat"])
        self.mock_client.get_photo_info.return_value = self._flickr_info([])
        self.mock_client.add_tags.side_effect = FlickrError("fail", code=0)
        check_photo(self.mock_client, self._row(photo_id), self.db, fix=True, verbose=False)
        # pushed_tags should be unchanged (still just ["cat"])
        pushed = json.loads(self._row(photo_id)["pushed_tags"])
        self.assertEqual(pushed, ["cat"])
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python -m pytest tests/test_core.py::TestReconcilePushedTags -q
```

Expected: `FAILED` — `TypeError: check_photo() takes 4 positional arguments but 5 were given` (db param not yet added)

- [ ] **Step 3: Update `check_photo` in `poller/reconcile.py`**

**Transactional ordering:** `client.add_tags()` must succeed before `db.conn.execute()` updates `pushed_tags`. The DB write is inside the `try` block after the API call — keep it that way.

Replace the entire `check_photo` function signature and tag-check block:

```python
def check_photo(
    client: FlickrClient,
    row: dict,
    db: Database,
    fix: bool,
    verbose: bool,
) -> dict:
    """
    Check a single photo against Flickr. Returns a result dict with fields:
        flickr_id, status, details
    where status is one of: ok | perm_mismatch | tag_mismatch | both_mismatch | flickr_error
    """
    flickr_id = row["flickr_id"]
    db_state = row["privacy_state"]
    db_perms_pushed = row["perms_pushed_flickr"]
    db_tags_pushed = row["tags_pushed_flickr"]

    # Use pushed_tags (write ledger) — not proposed_tags (living document)
    db_pushed = row.get("pushed_tags") or []
    if isinstance(db_pushed, str):
        try:
            db_pushed = json.loads(db_pushed)
        except (json.JSONDecodeError, TypeError, ValueError):
            db_pushed = []

    result = {
        "flickr_id": flickr_id,
        "status": "ok",
        "row_id": row["id"],
        "perm_expected": None,
        "perm_actual": None,
        "tags_expected": [],
        "tags_missing": [],
        "fixes": [],
        "errors": [],
    }

    try:
        info = client.get_photo_info(flickr_id)
    except FlickrError as e:
        result["status"] = "flickr_error"
        result["errors"] = [str(e)]
        return result

    photo = info.get("photo", {})

    # --- Permission check ---
    if db_perms_pushed:
        from flickr.flickr_client import state_to_perms

        visibility = photo.get("visibility", {})
        actual = (
            int(visibility.get("ispublic", 0)),
            int(visibility.get("isfriend", 0)),
            int(visibility.get("isfamily", 0)),
        )
        expected = state_to_perms(db_state)

        _PERM_LABEL: dict[tuple[int, int, int], str] = {
            (1, 0, 0): "public",
            (0, 1, 0): "friends",
            (0, 0, 1): "family",
            (0, 1, 1): "friends+family",
        }
        result["perm_expected"] = _PERM_LABEL.get(expected, "private")
        result["perm_actual"] = _PERM_LABEL.get(actual, "private")

        if actual != expected:
            result["status"] = "perm_mismatch"
            if fix:
                try:
                    client.set_permissions(
                        flickr_id,
                        is_public=expected[0],
                        is_friend=expected[1],
                        is_family=expected[2],
                    )
                    result["fixes"].append("perm")
                except FlickrError as e:
                    result["errors"].append(f"perm fix failed: {e}")

    # --- Tag check: only verify what was confirmed pushed (pushed_tags ledger) ---
    if db_tags_pushed and db_pushed:
        tags_container = photo.get("tags", {})
        flickr_tags = set()
        if isinstance(tags_container, dict):
            for t in tags_container.get("tag", []):
                flickr_tags.add(t.get("raw", "").lower().strip())

        expected_tags = set(t.lower().strip() for t in db_pushed if t.strip())
        missing = sorted(expected_tags - flickr_tags)

        result["tags_expected"] = sorted(expected_tags)
        result["tags_missing"] = missing

        if missing:
            result["status"] = (
                "both_mismatch" if result["status"] == "perm_mismatch" else "tag_mismatch"
            )
            if fix:
                try:
                    client.add_tags(flickr_id, missing)
                    result["fixes"].append("tags")
                    new_pushed = sorted(set(db_pushed) | set(missing))
                    db.conn.execute(
                        "UPDATE photos SET pushed_tags = ? WHERE id = ?",
                        (json.dumps(new_pushed), result["row_id"]),
                    )
                    db.conn.commit()
                except FlickrError as e:
                    result["errors"].append(f"tag fix failed: {e}")

    if verbose and result["status"] == "ok":
        log.debug(f"{flickr_id}: ok")

    return result
```

- [ ] **Step 4: Update the query and call site in `main()`**

In `main()`, update the SELECT query to include `pushed_tags`:

```python
    rows = db.conn.execute(
        """SELECT id, flickr_id, privacy_state, proposed_tags, pushed_tags,
                  perms_pushed_flickr, tags_pushed_flickr
           FROM photos
           WHERE flickr_id IS NOT NULL
             AND (perms_pushed_flickr = 1 OR tags_pushed_flickr = 1)
           ORDER BY reviewed_at DESC
           LIMIT ?""",
        (args.limit,),
    ).fetchall()
```

Update the `check_photo` call to pass `db`:

```python
            result = check_photo(client, dict(row), db, fix=args.fix, verbose=args.verbose)
```

- [ ] **Step 5: Run tests to confirm they pass**

```bash
python -m pytest tests/test_core.py::TestReconcilePushedTags -q
```

Expected: `7 passed`

- [ ] **Step 6: Run full suite**

```bash
python -m pytest tests/ -q
```

Expected: all passing.

- [ ] **Step 7: Commit**

```bash
git add poller/reconcile.py tests/test_core.py
git commit -m "fix: reconcile checks pushed_tags not proposed_tags (GH #99)

check_photo now reads from pushed_tags (the write ledger) instead of
proposed_tags (the living document). Photos with NULL pushed_tags skip
the tag check entirely. After a successful --fix push, pushed_tags is
updated additively in the DB.

This eliminates the non-convergence: proposed_tags drifting due to new
ML labels no longer causes reconcile to see the same photos as dirty.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 4: Write `pushed_tags` after UI bulk push — `app.py`

**Files:**
- Modify: `reviewer/app.py:1093-1100` (`api_push_approved`)
- Test: `tests/test_review_ui.py`

- [ ] **Step 1: Write the failing test**

Find `TestPushApproved` (or the relevant push test class) in `tests/test_review_ui.py` and append a new test, or add a new class:

```python
class TestApiPushApprovedWritesPushedTags:
    """api_push_approved must write pushed_tags to DB after successful add_tags."""

    def test_pushed_tags_set_after_successful_push(self, app, db_path):
        import json, sqlite3
        from unittest.mock import MagicMock, patch

        # Insert a photo ready for push
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("""
            INSERT INTO photos (uuid, original_filename, flickr_id, privacy_state,
                                proposed_tags, perms_pushed_flickr, tags_pushed_flickr,
                                apple_persons, apple_labels)
            VALUES ('uuid-push-ui', 'IMG_push_ui.JPG', 'flickr-push-ui',
                    'approved_public', '["cat","indoor"]', 0, 0, '[]', '[]')
        """)
        conn.commit()
        photo_id = conn.execute(
            "SELECT id FROM photos WHERE uuid='uuid-push-ui'"
        ).fetchone()[0]
        conn.close()

        mock_client = MagicMock()
        with patch("reviewer.app.client", return_value=mock_client):
            resp = app.post("/api/push-approved")
        assert resp.status_code == 200

        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT pushed_tags FROM photos WHERE id = ?", (photo_id,)
        ).fetchone()
        conn.close()
        pushed = json.loads(row[0])
        assert "cat" in pushed
        assert "indoor" in pushed
```

Note: check how the existing `test_review_ui.py` fixtures are structured (specifically `app` and `db_path` fixtures) and follow that pattern exactly. If the fixture is named differently, match what's used in that file.

- [ ] **Step 2: Run test to confirm it fails**

```bash
python -m pytest tests/test_review_ui.py::TestApiPushApprovedWritesPushedTags -q
```

Expected: `FAILED` — `AssertionError` (pushed_tags is NULL)

- [ ] **Step 3: Update `api_push_approved` in `reviewer/app.py`**

**Transactional ordering:** `c.add_tags()` must succeed before the DB write. The existing code already follows this — do not reorder.

Find the `add_tags` success block (around line 1093) and replace:

```python
        if not not_found and tags:
            try:
                c.add_tags(flickr_id, tags)
                db().conn.execute(
                    "UPDATE photos SET tags_pushed_flickr = 1 WHERE id = ?", (photo_id,)
                )
            except FlickrError as e:
                errors.append(str(e))
```

with:

```python
        if not not_found and tags:
            try:
                c.add_tags(flickr_id, tags)
                db().conn.execute(
                    "UPDATE photos SET tags_pushed_flickr = 1, pushed_tags = ? WHERE id = ?",
                    (json.dumps(sorted(tags)), photo_id),
                )
            except FlickrError as e:
                errors.append(str(e))
```

`json` is already imported at the top of `reviewer/app.py` (line 23).

- [ ] **Step 4: Run test to confirm it passes**

```bash
python -m pytest tests/test_review_ui.py::TestApiPushApprovedWritesPushedTags -q
```

Expected: `1 passed`

- [ ] **Step 5: Run full suite**

```bash
python -m pytest tests/ -q
```

Expected: all passing.

- [ ] **Step 6: Commit**

```bash
git add reviewer/app.py tests/test_review_ui.py
git commit -m "feat: write pushed_tags after UI bulk push (GH #99)

api_push_approved now records pushed_tags = proposed_tags in the DB
after a successful add_tags call, completing the write ledger for the
reviewer push path.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 5: New subcommand — `bp tag-writeback`

**Files:**
- Create: `poller/tag_writeback.py`
- Test: `tests/test_core.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_core.py`:

```python
# ---------------------------------------------------------------------------
# GH #99 — Task 5: bp tag-writeback subcommand
# ---------------------------------------------------------------------------


class TestTagWriteback(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "test.db")
        import json
        self.photo_id = self.db.upsert_photo({
            "uuid": "uuid-wb-001",
            "original_filename": "IMG_wb.JPG",
            "flickr_id": "flickr-wb-001",
            "tags_pushed_flickr": 1,
            "apple_persons": [],
            "apple_labels": [],
        })
        self.db.conn.execute(
            "UPDATE photos SET pushed_tags = ? WHERE id = ?",
            (json.dumps(["cat", "indoor"]), self.photo_id),
        )
        self.db.conn.commit()

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _run(self, **kwargs):
        from poller.tag_writeback import writeback
        return writeback(self.db, **kwargs)

    def test_keywords_merged_additively(self):
        from unittest.mock import MagicMock, patch
        mock_photo = MagicMock()
        mock_photo.uuid = "uuid-wb-001"
        mock_photo.keywords = ["existing"]

        mock_lib = MagicMock()
        mock_lib.photos.return_value = iter([mock_photo])

        with patch("poller.tag_writeback.photoscript") as mock_ps:
            mock_ps.PhotosLibrary.return_value = mock_lib
            result = self._run(dry_run=False, limit=500)

        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["ok"], 0)
        # keywords setter called with merged set
        mock_photo.__setattr__  # ensure setter was invoked via property
        self.assertEqual(sorted(mock_photo.keywords), ["cat", "existing", "indoor"])

    def test_already_has_all_keywords_is_ok(self):
        from unittest.mock import MagicMock, patch
        mock_photo = MagicMock()
        mock_photo.uuid = "uuid-wb-001"
        mock_photo.keywords = ["cat", "indoor"]

        mock_lib = MagicMock()
        mock_lib.photos.return_value = iter([mock_photo])

        with patch("poller.tag_writeback.photoscript") as mock_ps:
            mock_ps.PhotosLibrary.return_value = mock_lib
            result = self._run(dry_run=False, limit=500)

        self.assertEqual(result["ok"], 1)
        self.assertEqual(result["updated"], 0)

    def test_dry_run_does_not_write_keywords(self):
        from unittest.mock import MagicMock, patch, PropertyMock
        mock_photo = MagicMock()
        mock_photo.uuid = "uuid-wb-001"
        mock_photo.keywords = ["existing"]

        mock_lib = MagicMock()
        mock_lib.photos.return_value = iter([mock_photo])

        with patch("poller.tag_writeback.photoscript") as mock_ps:
            mock_ps.PhotosLibrary.return_value = mock_lib
            result = self._run(dry_run=True, limit=500)

        # keywords property setter must not have been called
        # In dry_run, updated count is reported but no write occurs
        self.assertEqual(result["updated"], 1)
        # The mock_photo.keywords list should be unchanged (still ["existing"])
        self.assertEqual(mock_photo.keywords, ["existing"])

    def test_not_found_uuid_counted(self):
        from unittest.mock import MagicMock, patch
        mock_lib = MagicMock()
        mock_lib.photos.return_value = iter([])  # empty — photo not in Photos.app

        with patch("poller.tag_writeback.photoscript") as mock_ps:
            mock_ps.PhotosLibrary.return_value = mock_lib
            result = self._run(dry_run=False, limit=500)

        self.assertEqual(result["not_found"], 1)

    def test_photos_without_uuid_skipped(self):
        """Flickr-only records (uuid=NULL) must not appear in writeback query."""
        import json
        from unittest.mock import MagicMock, patch
        # Insert a Flickr-only record (no uuid)
        self.db.conn.execute("""
            INSERT INTO photos (uuid, original_filename, flickr_id, privacy_state,
                                tags_pushed_flickr, pushed_tags, apple_persons, apple_labels)
            VALUES (NULL, 'flickr_only.JPG', 'flickr-only-001', 'already_public',
                    1, '["cat"]', '[]', '[]')
        """)
        self.db.conn.commit()

        mock_photo = MagicMock()
        mock_photo.uuid = "uuid-wb-001"
        mock_photo.keywords = []

        mock_lib = MagicMock()
        mock_lib.photos.return_value = iter([mock_photo])

        with patch("poller.tag_writeback.photoscript") as mock_ps:
            mock_ps.PhotosLibrary.return_value = mock_lib
            result = self._run(dry_run=False, limit=500)

        # Only 1 photo processed (the one with uuid), not the Flickr-only record
        self.assertEqual(result["updated"] + result["ok"], 1)
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python -m pytest tests/test_core.py::TestTagWriteback -q
```

Expected: `ERROR` — `ModuleNotFoundError: No module named 'poller.tag_writeback'`

- [ ] **Step 3: Create `poller/tag_writeback.py`**

```python
"""
tag_writeback.py — write pushed_tags back to Photos.app as explicit keywords

Reads pushed_tags from the DB for Photos-linked records and merges them
into the photo's keyword list in Photos.app via photoscript. This makes
ML-derived tags visible in Smart Albums.

Photos.app must be running. Keywords are merged additively — existing
keywords are never removed.

Usage:
    python poller/tag_writeback.py --config config/config.yml
    python poller/tag_writeback.py --config config/config.yml --dry-run
    python poller/tag_writeback.py --config config/config.yml --limit 1000

Options:
    --dry-run   Report what would change without writing
    --limit N   Process at most N photos (default: 500)
    --verbose   Show ok results too
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    import photoscript
except ImportError:
    photoscript = None  # type: ignore[assignment]

from db.db import Database

log = logging.getLogger("blue-pearmain.tag-writeback")


def writeback(
    db: Database,
    dry_run: bool = False,
    limit: int = 500,
    verbose: bool = False,
    source: str = "pushed-tags",
) -> dict:
    """
    Merge tag candidates into Photos.app keywords for all Photos-linked records.

    source: "pushed-tags" (default) reads from pushed_tags column;
            "proposed-tags" reads from proposed_tags column.
    Returns a dict: {ok, updated, not_found, errors}
    """
    tag_col = "pushed_tags" if source == "pushed-tags" else "proposed_tags"
    rows = db.conn.execute(
        f"""SELECT id, uuid, {tag_col} AS tag_source
           FROM photos
           WHERE {tag_col} IS NOT NULL
             AND uuid IS NOT NULL
           LIMIT ?""",
        (limit,),
    ).fetchall()

    totals: dict[str, int] = {"ok": 0, "updated": 0, "not_found": 0, "errors": 0}

    if not rows:
        return totals

    lib = photoscript.PhotosLibrary()

    for row in rows:
        uuid = row["uuid"]
        pushed = json.loads(row["tag_source"] or "[]")
        if not pushed:
            continue

        try:
            photos = list(lib.photos(uuid=[uuid]))
        except Exception as e:
            log.error(f"  {uuid}: lookup error — {e}")
            totals["errors"] += 1
            continue

        if not photos:
            log.debug(f"  {uuid}: not found in Photos.app")
            totals["not_found"] += 1
            continue

        photo = photos[0]
        try:
            current = sorted(photo.keywords)
            merged = sorted(set(current) | set(pushed))
            if merged == current:
                if verbose:
                    log.debug(f"  {uuid}: ok (no new keywords)")
                totals["ok"] += 1
            else:
                if not dry_run:
                    photo.keywords = merged
                totals["updated"] += 1
                log.info(
                    f"  {uuid}: {'would add' if dry_run else 'added'} "
                    f"{sorted(set(merged) - set(current))}"
                )
        except Exception as e:
            log.error(f"  {uuid}: keyword write error — {e}")
            totals["errors"] += 1

    return totals


def setup_logging(verbose: bool) -> None:
    from poller.bp_logging import configure
    configure("tag-writeback", verbose)


def main() -> int:
    parser = argparse.ArgumentParser(description="Blue Pearmain tag write-back to Photos.app")
    parser.add_argument("--config", default="config/config.yml")
    parser.add_argument("--dry-run", action="store_true", help="Report without writing")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--source",
        choices=["pushed-tags", "proposed-tags"],
        default="pushed-tags",
        help="Which DB field to read tag candidates from (default: pushed-tags)",
    )
    args = parser.parse_args()

    setup_logging(args.verbose)

    if photoscript is None:
        log.error("photoscript is not installed. Run: pip install photoscript")
        return 1

    with open(args.config) as f:
        config = yaml.safe_load(f)

    db_path = Path(config["database"]["path"]).expanduser()
    db = Database(db_path)

    log.info("Blue Pearmain tag write-back starting")
    totals = writeback(db, dry_run=args.dry_run, limit=args.limit, verbose=args.verbose, source=args.source)

    print()
    print(
        f"  ok={totals['ok']}"
        f"  updated={totals['updated']}"
        f"  not_found={totals['not_found']}"
        f"  errors={totals['errors']}"
    )
    if args.dry_run:
        print("  (dry-run — no keywords were written)")

    db.close()
    return 1 if totals["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Fix the dry_run test — keywords are a MagicMock property**

The `test_dry_run_does_not_write_keywords` test checks that `mock_photo.keywords` is unchanged after a dry run. Because `mock_photo` is a `MagicMock`, setting `mock_photo.keywords = merged` in writeback would actually update the attribute. The dry-run guard `if not dry_run: photo.keywords = merged` prevents the write, so `mock_photo.keywords` stays as the list `["existing"]`. The test assertion `self.assertEqual(mock_photo.keywords, ["existing"])` should pass as long as the guard is in place.

- [ ] **Step 5: Run tests to confirm they pass**

```bash
python -m pytest tests/test_core.py::TestTagWriteback -q
```

Expected: `5 passed`

- [ ] **Step 6: Run full suite**

```bash
python -m pytest tests/ -q
```

Expected: all passing.

- [ ] **Step 7: Commit**

```bash
git add poller/tag_writeback.py tests/test_core.py
git commit -m "feat: add bp tag-writeback subcommand (GH #99)

Reads pushed_tags from DB and merges them into Photos.app keywords via
photoscript. Makes ML-derived tags available for Smart Albums.
Additive only — never removes existing keywords.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 6: Wire up CLI + update README

**Files:**
- Modify: `bp` (cmd function, subparser, dispatch)
- Modify: `README.md`

- [ ] **Step 1: Add `cmd_tag_writeback` to `bp`**

After `cmd_reconcile` (around line 235), add:

```python
def cmd_tag_writeback(args):
    from poller.tag_writeback import main
    _run(main, args, [
        ("--config",   args.config),
        ("--dry-run",  args.dry_run),
        ("--limit",    str(args.limit) if args.limit is not None else None),
        ("--verbose",  args.verbose),
        ("--source",   args.source),
    ])
```

- [ ] **Step 2: Add subparser for `tag-writeback` in `bp`**

After the `# reconcile` subparser block (around line 730), add:

```python
    # tag-writeback
    p_twb = sub.add_parser(
        "tag-writeback",
        help="Write pushed Flickr tags back to Photos.app as explicit keywords",
    )
    p_twb.add_argument("--dry-run",  action="store_true")
    p_twb.add_argument("--limit",    type=int, default=None)
    p_twb.add_argument("--verbose",  action="store_true")
    p_twb.add_argument(
        "--source",
        choices=["pushed-tags", "proposed-tags"],
        default="pushed-tags",
    )
```

- [ ] **Step 3: Add to dispatch dict in `bp`**

In the `dispatch` dict (around line 887), add:

```python
        "tag-writeback":  cmd_tag_writeback,
```

- [ ] **Step 4: Update the usage docstring in `bp`**

Find the usage block near the top of `main()` or the module docstring that lists commands (around line 11) and add:

```
    bp tag-writeback [--dry-run] [--limit N]  Write pushed tags back to Photos.app keywords
```

- [ ] **Step 5: Smoke-test the CLI wiring**

```bash
python bp tag-writeback --help
```

Expected output includes `--dry-run`, `--limit`, `--verbose`.

- [ ] **Step 6: Update `README.md`**

Find the keyboard shortcuts / command reference table and add an entry for `tag-writeback`. Add a short paragraph under the relevant section explaining its purpose. Update the test count (count by running `python -m pytest tests/ -q` and reading the final line).

Example addition to the command table:

```
| `bp tag-writeback` | Write confirmed-pushed Flickr tags back to Photos.app as keywords (enables Smart Albums) |
```

- [ ] **Step 7: Run the full test suite one final time**

```bash
python -m pytest tests/ -q
```

Note the passing count and update `README.md` if the test count has changed.

- [ ] **Step 8: Run `make lint`**

```bash
make lint
```

Fix any mypy or ruff errors before committing.

- [ ] **Step 9: Commit**

```bash
git add bp README.md
git commit -m "feat: wire bp tag-writeback CLI entry point + update README (GH #99)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

- [ ] **Step 10: Push**

```bash
git push && git push --tags
```

- [ ] **Step 11: Close GH #99**

```bash
gh issue comment 99 --body "Fixed in <commit hashes from tasks 1-6>.

**What was done:**
- Added \`pushed_tags TEXT\` column (migration 016): cumulative write ledger of tags confirmed pushed to Flickr
- \`bp reconcile\` now checks \`pushed_tags ⊆ actual_flickr_tags\` instead of \`proposed_tags\`. Photos with NULL \`pushed_tags\` skip the tag check. After a successful \`--fix\` push, \`pushed_tags\` is updated additively.
- \`_push_to_flickr()\` (poller) and \`api_push_approved\` (reviewer UI) both write \`pushed_tags\` on confirmed push success.
- New \`bp tag-writeback\` subcommand merges \`pushed_tags\` into Photos.app keywords via \`photoscript\`, making ML-derived labels available for Smart Albums.
- N new tests passing."
gh issue close 99
```
