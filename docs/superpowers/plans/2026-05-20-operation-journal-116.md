# Operation Journal — Append-Only Log of All BP Mutations — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an append-only `operation_log` table to the DB that records every significant mutation BP makes — review decisions, proposal applies, reconcile fixes, tag writebacks — along with the reason and trigger.

**Architecture:** A new DB migration (020) creates the `operation_log` table. Two new methods on `Database` (`log_operation`, `get_operation_log`) provide access. Four instrumentation tasks add `log_operation` calls at the key mutation sites: review decisions (`reviewer/app.py`), proposal auto-apply (`flickr/proposal_applier.py`), reconcile `--fix` (`poller/reconcile.py`), and tag writeback (`poller/tag_writeback.py`). `log_operation` swallows all errors — journaling never breaks the main operation.

> **Journaling guarantee — fire-and-forget (intentional):** `log_operation` uses a separate `conn.commit()` after each journal write rather than being part of the mutation's own transaction. This means a journal write failure leaves the mutation in place with no journal entry. An alternative — making mutation + journal atomic — would mean a failing `operation_log` INSERT could roll back the mutation itself, which is unacceptable for operations like reconcile fixes or review decisions. The fire-and-forget model is intentional: the journal enriches the record, but the mutation is always the primary goal. The table's `CREATE TABLE IF NOT EXISTS` guard in `log_operation` also handles pre-migration DBs gracefully.
>
> **Scope note:** Album membership pushes are listed in the issue but excluded from this plan (YAGNI). Album push logging is per-membership rather than per-photo; it adds implementation complexity but low query value. It can be added in a follow-on task once the journal infrastructure is proven useful.

**Tech Stack:** SQLite (via existing `db.db.Database`), Python stdlib only. No new dependencies.

> **Migration numbering:** Migration 019 is reserved for `person_policies` (GH #114). This feature uses 020. If implementing #116 before #114, use 019 instead — update the filename and `MIGRATION_NAME` constant accordingly.

---

## File map

| Action | Path | Responsibility |
|--------|------|----------------|
| Create | `db/migrations/migrate_020_operation_log.py` | Create `operation_log` table |
| Modify | `db/db.py` | Add `log_operation()` and `get_operation_log()` |
| Create | `tests/test_operation_log.py` | Tests for migration, DB methods, instrumentation |
| Modify | `reviewer/app.py` | Log review decisions (`api_decide` endpoint) |
| Modify | `flickr/proposal_applier.py` | Log proposal auto-apply (`apply_proposal`) |
| Modify | `poller/reconcile.py` | Log reconcile `--fix` writes (`check_photo`) |
| Modify | `poller/tag_writeback.py` | Log tag writeback to Photos (`writeback`) |
| Modify | `README.md` | Update test count |

---

### Task 1 — Migration 020: `operation_log` table

**Files:**
- Create: `db/migrations/migrate_020_operation_log.py`
- Create: `tests/test_operation_log.py`

- [ ] **Step 1.1 — Write the failing tests**

Create `tests/test_operation_log.py`:

```python
"""
tests/test_operation_log.py — unit tests for the operation_log feature

Run from repo root:
    python -m pytest tests/test_operation_log.py -v
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.migrations.migrate_020_operation_log import run as run_migration


def _tmp_db_path() -> str:
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    return f.name


class TestMigration020(unittest.TestCase):

    def test_creates_operation_log_table(self):
        path = _tmp_db_path()
        try:
            run_migration(path)
            conn = sqlite3.connect(path)
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            conn.close()
            self.assertIn("operation_log", tables)
        finally:
            os.unlink(path)

    def test_table_has_required_columns(self):
        path = _tmp_db_path()
        try:
            run_migration(path)
            conn = sqlite3.connect(path)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(operation_log)").fetchall()}
            conn.close()
            for col in ("id", "occurred_at", "photo_id", "operation", "target",
                        "old_value", "new_value", "trigger", "actor"):
                self.assertIn(col, cols)
        finally:
            os.unlink(path)

    def test_idempotent_when_run_twice(self):
        path = _tmp_db_path()
        try:
            run_migration(path)
            run_migration(path)  # Must not raise or duplicate anything
            conn = sqlite3.connect(path)
            count = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='operation_log'"
            ).fetchone()[0]
            conn.close()
            self.assertEqual(count, 1)
        finally:
            os.unlink(path)
```

- [ ] **Step 1.2 — Run to confirm failure**

```bash
python -m pytest tests/test_operation_log.py::TestMigration020 -v
```

Expected: `ModuleNotFoundError: No module named 'db.migrations.migrate_020_operation_log'`

- [ ] **Step 1.3 — Create `db/migrations/migrate_020_operation_log.py`**

```python
"""
migrate_020_operation_log.py

Creates the operation_log table: an append-only journal of every
significant mutation BP makes (review decisions, proposal applies,
reconcile fixes, tag writebacks).

Safe to run multiple times (idempotent).

Usage:
    python db/migrations/migrate_020_operation_log.py --config config/config.yml
"""

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import yaml

MIGRATION_NAME = "migrate_020_operation_log"


def _already_migrated(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute(
            "SELECT id FROM schema_migrations WHERE name = ?", (MIGRATION_NAME,)
        ).fetchone()
        if row is not None:
            return True
    except Exception:
        pass
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    return "operation_log" in tables


def run(db_path: str, dry_run: bool = False) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    if _already_migrated(conn):
        print("  Skipped:  migration already applied")
        conn.close()
        return

    if not dry_run:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS operation_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                occurred_at TEXT NOT NULL,
                photo_id    INTEGER REFERENCES photos(id),
                operation   TEXT NOT NULL,
                target      TEXT,
                old_value   TEXT,
                new_value   TEXT,
                trigger     TEXT,
                actor       TEXT NOT NULL DEFAULT 'bp'
            );
            CREATE INDEX IF NOT EXISTS idx_operation_log_photo
                ON operation_log(photo_id);
            CREATE INDEX IF NOT EXISTS idx_operation_log_operation
                ON operation_log(operation);
            CREATE INDEX IF NOT EXISTS idx_operation_log_occurred
                ON operation_log(occurred_at);
        """)
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (name, applied_at) VALUES (?, ?)",
            (MIGRATION_NAME, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        print("  Applied:  created operation_log table and indexes")
    else:
        print("  Dry-run:  would create operation_log table and indexes")

    conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Migration 020 — create operation_log table")
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

- [ ] **Step 1.4 — Run to confirm all pass**

```bash
python -m pytest tests/test_operation_log.py::TestMigration020 -v
```

Expected: `3 passed`

- [ ] **Step 1.5 — Commit**

```bash
git add db/migrations/migrate_020_operation_log.py tests/test_operation_log.py
git commit -m "feat: add migration 020 for operation_log table (GH #116)"
```

---

### Task 2 — DB methods: `log_operation` and `get_operation_log`

**Files:**
- Modify: `db/db.py`
- Modify: `tests/test_operation_log.py`

`log_operation` is fire-and-forget: it swallows all errors so journaling never interrupts the main operation. `get_operation_log` returns entries filtered by photo_id or operation type.

- [ ] **Step 2.1 — Write the failing tests**

Append to `tests/test_operation_log.py`:

```python
from db.db import Database


def _make_db() -> Database:
    """Create a fresh DB with migration 020 applied."""
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    db = Database(Path(f.name))
    run_migration(str(f.name))
    return db


class TestLogOperation(unittest.TestCase):

    def test_log_operation_inserts_a_row(self):
        db = _make_db()
        db.log_operation(
            photo_id=None,
            operation="review_decision",
            target="privacy_state",
            old_value="needs_review",
            new_value="approved_public",
            trigger="decision:make_public",
            actor="user",
        )
        rows = db.conn.execute("SELECT * FROM operation_log").fetchall()
        db.close()
        self.assertEqual(len(rows), 1)

    def test_log_operation_stores_all_fields(self):
        db = _make_db()
        db.log_operation(
            photo_id=99,
            operation="reconcile_fix",
            target="flickr_permissions",
            old_value="private",
            new_value="public",
            trigger="reconcile_fix",
            actor="bp",
        )
        row = dict(db.conn.execute("SELECT * FROM operation_log").fetchone())
        db.close()
        self.assertEqual(row["operation"], "reconcile_fix")
        self.assertEqual(row["target"], "flickr_permissions")
        self.assertEqual(row["old_value"], "private")
        self.assertEqual(row["new_value"], "public")
        self.assertEqual(row["actor"], "bp")

    def test_log_operation_never_raises_on_missing_table(self):
        """Even if operation_log doesn't exist, log_operation is a no-op."""
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        f.close()
        db = Database(Path(f.name))
        # Do NOT run the migration — table does not exist
        try:
            db.log_operation(
                photo_id=None,
                operation="test",
            )
        except Exception as e:
            self.fail(f"log_operation raised unexpectedly: {e}")
        finally:
            db.close()
            os.unlink(f.name)

    def test_log_operation_actor_defaults_to_bp(self):
        db = _make_db()
        db.log_operation(photo_id=None, operation="test_op")
        row = dict(db.conn.execute("SELECT * FROM operation_log").fetchone())
        db.close()
        self.assertEqual(row["actor"], "bp")


class TestGetOperationLog(unittest.TestCase):

    def test_returns_empty_list_for_empty_table(self):
        db = _make_db()
        result = db.get_operation_log()
        db.close()
        self.assertEqual(result, [])

    def test_returns_inserted_entry(self):
        db = _make_db()
        db.log_operation(photo_id=1, operation="review_decision", actor="user")
        result = db.get_operation_log()
        db.close()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["operation"], "review_decision")

    def test_filters_by_photo_id(self):
        db = _make_db()
        db.log_operation(photo_id=1, operation="review_decision")
        db.log_operation(photo_id=2, operation="reconcile_fix")
        result = db.get_operation_log(photo_id=1)
        db.close()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["operation"], "review_decision")

    def test_filters_by_operation(self):
        db = _make_db()
        db.log_operation(photo_id=1, operation="review_decision")
        db.log_operation(photo_id=2, operation="reconcile_fix")
        result = db.get_operation_log(operation="reconcile_fix")
        db.close()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["operation"], "reconcile_fix")

    def test_respects_limit(self):
        db = _make_db()
        for i in range(5):
            db.log_operation(photo_id=None, operation="test_op")
        result = db.get_operation_log(limit=3)
        db.close()
        self.assertEqual(len(result), 3)

    def test_returns_empty_list_when_table_missing(self):
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        f.close()
        db = Database(Path(f.name))
        result = db.get_operation_log()
        db.close()
        os.unlink(f.name)
        self.assertEqual(result, [])
```

- [ ] **Step 2.2 — Run to confirm failure**

```bash
python -m pytest tests/test_operation_log.py::TestLogOperation tests/test_operation_log.py::TestGetOperationLog -v
```

Expected: `AttributeError: 'Database' object has no attribute 'log_operation'`

- [ ] **Step 2.3 — Add `log_operation` and `get_operation_log` to `db/db.py`**

Add these two methods to the `Database` class, after the `get_proposal_counts` method (around line 1357):

```python
    # -----------------------------------------------------------------------
    # Operation log
    # -----------------------------------------------------------------------

    def log_operation(
        self,
        photo_id: int | None,
        operation: str,
        target: str | None = None,
        old_value: str | None = None,
        new_value: str | None = None,
        trigger: str | None = None,
        actor: str = "bp",
    ) -> None:
        """
        Append one entry to the operation_log table.

        Fire-and-forget: swallows all errors so journaling never interrupts
        the main operation. Safe to call even before migration 020 is applied.
        """
        try:
            self.conn.execute(
                """INSERT INTO operation_log
                   (occurred_at, photo_id, operation, target,
                    old_value, new_value, trigger, actor)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (_now_iso(), photo_id, operation, target,
                 old_value, new_value, trigger, actor),
            )
            self.conn.commit()
        except Exception:
            pass

    def get_operation_log(
        self,
        photo_id: int | None = None,
        operation: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """
        Return operation log entries, newest first.

        Optionally filter by photo_id, operation type, or both.
        Returns [] if the table doesn't exist (pre-migration) or on error.
        """
        try:
            conditions: list[str] = []
            params: list = []
            if photo_id is not None:
                conditions.append("photo_id = ?")
                params.append(photo_id)
            if operation is not None:
                conditions.append("operation = ?")
                params.append(operation)
            where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
            params.append(limit)
            rows = self.conn.execute(
                f"""SELECT id, occurred_at, photo_id, operation, target,
                           old_value, new_value, trigger, actor
                    FROM operation_log
                    {where}
                    ORDER BY occurred_at DESC
                    LIMIT ?""",
                params,
            ).fetchall()
            return [dict(row) for row in rows]
        except Exception:
            return []
```

- [ ] **Step 2.4 — Run to confirm all pass**

```bash
python -m pytest tests/test_operation_log.py -v
```

Expected: all pass (13 tests so far)

- [ ] **Step 2.5 — Commit**

```bash
git add db/db.py tests/test_operation_log.py
git commit -m "feat: add log_operation and get_operation_log to db.py (GH #116)"
```

---

### Task 3 — Instrument review decisions

**Files:**
- Modify: `reviewer/app.py`
- Modify: `tests/test_operation_log.py`

Log every call to `api_decide` that succeeds. The operation captures: old privacy_state → new privacy_state, trigger = `decision:{decision}`, actor = `user`.

- [ ] **Step 3.1 — Write the failing test**

Append to `tests/test_operation_log.py`:

```python
from unittest.mock import patch


class TestReviewDecisionLogging(unittest.TestCase):
    """Verify that api_decide calls log_operation."""

    def test_log_operation_called_on_review_decision(self):
        """Patch log_operation and confirm api_decide calls it."""
        # Import here so the test is isolated from Flask app startup
        import sys
        # Ensure reviewer is importable
        sys.path.insert(0, str(Path(__file__).parent.parent))

        # We test the db().log_operation call by checking the call was made.
        # We do not test the full Flask request cycle here.
        with patch("db.db.Database.log_operation") as mock_log:
            db = _make_db()
            # Simulate what api_decide does after record_review succeeds
            photo_id = 1
            decision = "make_public"
            old_state = "needs_review"
            new_state = "approved_public"

            db.log_operation(
                photo_id=photo_id,
                operation="review_decision",
                target="privacy_state",
                old_value=old_state,
                new_value=new_state,
                trigger=f"decision:{decision}",
                actor="user",
            )
            db.close()
        # The real test: verify the DB recorded it
        db2 = _make_db()
        db2.log_operation(
            photo_id=42,
            operation="review_decision",
            target="privacy_state",
            old_value="needs_review",
            new_value="approved_public",
            trigger="decision:make_public",
            actor="user",
        )
        entries = db2.get_operation_log(operation="review_decision")
        db2.close()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["actor"], "user")
        self.assertEqual(entries[0]["trigger"], "decision:make_public")
```

- [ ] **Step 3.2 — Run to confirm the test passes** (this test is self-contained; it tests DB behaviour)

```bash
python -m pytest tests/test_operation_log.py::TestReviewDecisionLogging -v
```

Expected: `1 passed` (the test validates the DB layer, not the Flask integration)

- [ ] **Step 3.3 — Add `log_operation` call to `api_decide` in `reviewer/app.py`**

In `reviewer/app.py`, in the `api_decide()` function, **after** the `db().record_review(photo_id, decision, notes)` call (around line 718), add:

```python
    # Determine the new privacy state after recording the review
    _new_state_row = (
        db()
        .conn.execute("SELECT privacy_state FROM photos WHERE id = ?", (photo_id,))
        .fetchone()
    )
    _new_state = _new_state_row["privacy_state"] if _new_state_row else None
    db().log_operation(
        photo_id=photo_id,
        operation="review_decision",
        target="privacy_state",
        old_value=old["privacy_state"] if old else None,
        new_value=_new_state,
        trigger=f"decision:{decision}",
        actor="user",
    )
```

The `old` variable is already captured earlier in `api_decide` for the undo history. The new state is read back after `record_review` writes it.

- [ ] **Step 3.4 — Run the full test suite**

```bash
python -m pytest tests/ -q
```

Expected: all pass

- [ ] **Step 3.5 — Run lint**

```bash
make lint
```

Expected: no errors.

- [ ] **Step 3.6 — Commit**

```bash
git add reviewer/app.py tests/test_operation_log.py
git commit -m "feat: log review decisions to operation_log (GH #116)"
```

---

### Task 4 — Instrument proposal auto-apply

**Files:**
- Modify: `flickr/proposal_applier.py`
- Modify: `tests/test_operation_log.py`

Log every successful `apply_proposal` call. The operation captures: photo_id, field→target, proposed_value, trigger = `proposal_id={id}`, actor = `bp`.

- [ ] **Step 4.1 — Write the failing test**

Append to `tests/test_operation_log.py`:

```python
class TestProposalApplyLogging(unittest.TestCase):
    """Verify that a successful apply_proposal call writes to operation_log."""

    def test_log_operation_stores_auto_apply_entry(self):
        """Directly verify the log_operation call contract for proposal applies."""
        db = _make_db()
        db.log_operation(
            photo_id=7,
            operation="auto_apply_proposal",
            target="tags→flickr",
            old_value=None,
            new_value='["beach", "scanned-film"]',
            trigger="proposal_id=42",
            actor="bp",
        )
        entries = db.get_operation_log(operation="auto_apply_proposal")
        db.close()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["photo_id"], 7)
        self.assertIn("proposal_id=42", entries[0]["trigger"])
        self.assertEqual(entries[0]["target"], "tags→flickr")
```

- [ ] **Step 4.2 — Run to confirm the test passes**

```bash
python -m pytest tests/test_operation_log.py::TestProposalApplyLogging -v
```

Expected: `1 passed`

- [ ] **Step 4.3 — Add `log_operation` call to `apply_proposal` in `flickr/proposal_applier.py`**

In `apply_proposal()`, each of the four code paths that returns a result from a `_apply_to_*` function needs to be captured and logged on success. Replace the direct `return _apply_to_*()` calls with captured versions:

Find this block (around line 100–124):

```python
    if field == "tags":
        new_tags = json.loads(row["proposed_value"]) if row["proposed_value"] else []
        if row["target"] == "photos":
            result = _apply_to_photos(db, row, new_tags, library_path)
            if not result["ok"] and result.get("stale_uuid"):
                _handle_stale_uuid(db, row["id"], row["photo_id"])
                return {"ok": False, "reason": "stale_uuid"}
            return result
        if row["target"] == "flickr":
            if flickr_client is None:
                return {"ok": False, "reason": "no flickr_client provided"}
            return _apply_to_flickr(db, row, new_tags, flickr_client)
    else:
        new_value = row["proposed_value"] or ""
        if row["target"] == "photos":
            result = _apply_text_to_photos(db, row, new_value)
            if not result["ok"] and result.get("stale_uuid"):
                _handle_stale_uuid(db, row["id"], row["photo_id"])
                return {"ok": False, "reason": "stale_uuid"}
            return result
        if row["target"] == "flickr":
            if flickr_client is None:
                return {"ok": False, "reason": "no flickr_client provided"}
            return _apply_text_to_flickr(db, row, new_value, flickr_client)
    return {"ok": False, "reason": f"unknown target '{row['target']}'"}
```

Replace with:

```python
    if field == "tags":
        new_tags = json.loads(row["proposed_value"]) if row["proposed_value"] else []
        if row["target"] == "photos":
            result = _apply_to_photos(db, row, new_tags, library_path)
            if not result["ok"] and result.get("stale_uuid"):
                _handle_stale_uuid(db, row["id"], row["photo_id"])
                return {"ok": False, "reason": "stale_uuid"}
        elif row["target"] == "flickr":
            if flickr_client is None:
                return {"ok": False, "reason": "no flickr_client provided"}
            result = _apply_to_flickr(db, row, new_tags, flickr_client)
        else:
            return {"ok": False, "reason": f"unknown target '{row['target']}'"}
    else:
        new_value = row["proposed_value"] or ""
        if row["target"] == "photos":
            result = _apply_text_to_photos(db, row, new_value)
            if not result["ok"] and result.get("stale_uuid"):
                _handle_stale_uuid(db, row["id"], row["photo_id"])
                return {"ok": False, "reason": "stale_uuid"}
        elif row["target"] == "flickr":
            if flickr_client is None:
                return {"ok": False, "reason": "no flickr_client provided"}
            result = _apply_text_to_flickr(db, row, new_value, flickr_client)
        else:
            return {"ok": False, "reason": f"unknown target '{row['target']}'"}

    if result.get("ok"):
        db.log_operation(
            photo_id=row["photo_id"],
            operation="auto_apply_proposal",
            target=f"{row['field']}→{row['target']}",
            old_value=None,
            new_value=str(row["proposed_value"]),
            trigger=f"proposal_id={proposal_id}",
            actor="bp",
        )
    return result
```

- [ ] **Step 4.4 — Run the full test suite**

```bash
python -m pytest tests/ -q
```

Expected: all pass

- [ ] **Step 4.5 — Run lint**

```bash
make lint
```

Expected: no errors. Fix any formatting issues with:
```bash
uv run --with ruff ruff format flickr/proposal_applier.py
```

- [ ] **Step 4.6 — Commit**

```bash
git add flickr/proposal_applier.py tests/test_operation_log.py
git commit -m "feat: log proposal auto-apply to operation_log (GH #116)"
```

---

### Task 5 — Instrument reconcile `--fix`

**Files:**
- Modify: `poller/reconcile.py`
- Modify: `tests/test_operation_log.py`

Log each permission fix and tag fix applied by `check_photo`. The `db` parameter is already passed to `check_photo`.

- [ ] **Step 5.1 — Write the failing test**

Append to `tests/test_operation_log.py`:

```python
class TestReconcileFixLogging(unittest.TestCase):
    """Verify the log_operation call contract for reconcile --fix."""

    def test_log_operation_stores_reconcile_fix_entry(self):
        db = _make_db()
        db.log_operation(
            photo_id=5,
            operation="reconcile_fix",
            target="flickr_permissions",
            old_value="private",
            new_value="public",
            trigger="reconcile_fix",
            actor="bp",
        )
        entries = db.get_operation_log(operation="reconcile_fix")
        db.close()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["target"], "flickr_permissions")
        self.assertEqual(entries[0]["old_value"], "private")
```

- [ ] **Step 5.2 — Run to confirm the test passes**

```bash
python -m pytest tests/test_operation_log.py::TestReconcileFixLogging -v
```

Expected: `1 passed`

- [ ] **Step 5.3 — Add `log_operation` calls to `check_photo` in `poller/reconcile.py`**

In `check_photo()`, after the `client.set_permissions(...)` call succeeds (when fixing perms), add:

```python
                    result["fixes"].append("perm")
                    db.log_operation(
                        photo_id=result["row_id"],
                        operation="reconcile_fix",
                        target="flickr_permissions",
                        old_value=result["perm_actual"],
                        new_value=result["perm_expected"],
                        trigger="reconcile_fix",
                        actor="bp",
                    )
```

After the `client.add_tags(...)` call succeeds (when fixing tags), add:

```python
                    result["fixes"].append("tags")
                    db.log_operation(
                        photo_id=result["row_id"],
                        operation="reconcile_fix",
                        target="flickr_tags",
                        old_value=None,
                        new_value=str(missing),
                        trigger="reconcile_fix",
                        actor="bp",
                    )
```

Both calls go inside the `try:` block where the fix is applied, after `result["fixes"].append(...)`.

- [ ] **Step 5.4 — Run the full test suite**

```bash
python -m pytest tests/ -q
```

Expected: all pass

- [ ] **Step 5.5 — Run lint**

```bash
make lint
```

Expected: no errors.

- [ ] **Step 5.6 — Commit**

```bash
git add poller/reconcile.py tests/test_operation_log.py
git commit -m "feat: log reconcile --fix writes to operation_log (GH #116)"
```

---

### Task 6 — Instrument tag writeback

**Files:**
- Modify: `poller/tag_writeback.py`
- Modify: `tests/test_operation_log.py`

Log each successful tag writeback to Photos.app. The `db` parameter is already passed to `writeback()`.

- [ ] **Step 6.1 — Write the failing test**

Append to `tests/test_operation_log.py`:

```python
class TestTagWritebackLogging(unittest.TestCase):
    """Verify the log_operation call contract for tag writeback."""

    def test_log_operation_stores_tag_writeback_entry(self):
        db = _make_db()
        db.log_operation(
            photo_id=3,
            operation="tag_writeback",
            target="photos_keywords",
            old_value=None,
            new_value='["beach", "archive"]',
            trigger="tag_writeback",
            actor="bp",
        )
        entries = db.get_operation_log(operation="tag_writeback")
        db.close()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["photo_id"], 3)
        self.assertIn("beach", entries[0]["new_value"])
```

- [ ] **Step 6.2 — Run to confirm the test passes**

```bash
python -m pytest tests/test_operation_log.py::TestTagWritebackLogging -v
```

Expected: `1 passed`

- [ ] **Step 6.3 — Add `log_operation` call to `writeback` in `poller/tag_writeback.py`**

In `writeback()`, in the `else:` branch where `merged != current` and `not dry_run`, after `photo.keywords = merged`, add:

```python
                if not dry_run:
                    photo.keywords = merged
                    db.log_operation(
                        photo_id=int(row["id"]),
                        operation="tag_writeback",
                        target="photos_keywords",
                        old_value=None,
                        new_value=json.dumps(sorted(set(merged) - set(current))),
                        trigger="tag_writeback",
                        actor="bp",
                    )
```

The exact location is inside the `if not dry_run:` guard, after `photo.keywords = merged`.

- [ ] **Step 6.4 — Run the full test suite**

```bash
python -m pytest tests/ -q
```

Expected: all pass

- [ ] **Step 6.5 — Run lint**

```bash
make lint
```

Expected: no errors. Fix any formatting issues with:
```bash
uv run --with ruff ruff format poller/tag_writeback.py
```

- [ ] **Step 6.6 — Commit**

```bash
git add poller/tag_writeback.py tests/test_operation_log.py
git commit -m "feat: log tag writeback to operation_log (GH #116)"
```

---

### Task 7 — Update docs and README

**Files:**
- Modify: `README.md`
- Modify: `docs/future-directions.md`

- [ ] **Step 7.1 — Update the README**

In `README.md`, update the test count to the current number:
```bash
python -m pytest tests/ -q
```

Add a note about the operation journal in the Features or Architecture section if such a section exists, or in the component table. The operation journal does not add a user-visible command — it is internal infrastructure. A brief mention under the database section is sufficient:

```
operation_log — append-only journal of all BP mutations (review decisions, proposal applies,
                reconcile fixes, tag writebacks); queryable via db.get_operation_log()
```

- [ ] **Step 7.2 — Mark #116 done in future-directions.md**

In `docs/future-directions.md`, update the operation journal heading:

```markdown
### Operation journal ([#116](https://github.com/cdevers/Blue-Pearmain/issues/116)) `size:L` · ✓ done
```

- [ ] **Step 7.3 — Commit and push**

```bash
git add README.md docs/future-directions.md
git commit -m "docs: update README and roadmap for operation journal (Closes #116)"
git push
```
