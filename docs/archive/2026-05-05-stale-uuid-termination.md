# Stale UUID Proposal Termination Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When applying a proposal fails because Photos.app rejects the UUID ("invalid photo ID"), mark the proposal `failed` and set `uuid_stale=1` on the photo row — so it disappears from the pending queue and can be found later.

**Architecture:** Add `failed` to the `metadata_proposals.status` CHECK constraint via a table-recreation migration; add `uuid_stale INTEGER` column to `photos`; detect the specific error string in three write-to-Photos helpers and propagate a sentinel dict up to their four call sites; update `apply_batch` to treat stale-UUID failures as counted-but-silent rather than user-visible errors.

**Tech Stack:** Python 3.11, SQLite (via stdlib `sqlite3`), `photoscript` (mocked in tests), `unittest.mock`.

---

## File Map

| File | Change |
|---|---|
| `db/migrations/migrate_010_stale_uuid.py` | **New** — recreates `metadata_proposals` with `failed` status; adds `uuid_stale` column to `photos` |
| `db/schema.sql` | Update status CHECK; add `uuid_stale` column |
| `db/db.py` | Add `"failed"` to `resolve_proposal` assertion |
| `flickr/proposal_applier.py` | Add `_handle_stale_uuid`; update 3 helpers + 4 call sites + `apply_batch` |
| `tests/test_core.py` | Add `TestStaleUuid` (5 tests) |
| `README.md` | Update test count |

---

### Task 1: Write 5 failing tests (TDD — write first)

**Files:**
- Modify: `tests/test_core.py` (append after the last test class, before `if __name__ == "__main__":`)

- [ ] **Step 1: Append `TestStaleUuid` to `tests/test_core.py`**

Find the line `if __name__ == "__main__":` at the end of the file and insert this class immediately before it:

```python
class TestStaleUuid(unittest.TestCase):
    """Proposals that fail with 'invalid photo ID' are marked failed; photo gets uuid_stale=1."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        from db.db import Database
        self.db = Database(Path(self._tmp.name) / "test.db")
        # Migration must be applied so uuid_stale column and 'failed' status exist
        from db.migrations.migrate_010_stale_uuid import run as run_migration
        run_migration(str(Path(self._tmp.name) / "test.db"))
        # Seed a photo with both uuid and flickr_id
        self.db.upsert_photo({
            "flickr_id": "F1", "uuid": "U1",
            "privacy_state": "candidate_public",
            "photos_tags": '["nature"]', "photos_tags_hash": "PH1",
            "flickr_tags": '[]',          "flickr_tags_hash": "FH0",
        })
        self.photo_id = self.db.get_photo_by_flickr_id("F1")["id"]
        # Seed a pending non_conflict proposal: flickr→photos tags
        self.db.upsert_proposal({
            "photo_id": self.photo_id, "field": "tags",
            "proposed_value": '["nature"]',
            "source": "flickr", "target": "photos",
            "conflict_type": "non_conflict",
            "source_hash_at_creation": "PH1",
            "target_hash_at_creation": "FH0",
            "created_at": "2026-01-01T00:00:00+00:00",
        })
        self.db.conn.commit()
        self.proposal_id = self.db.conn.execute(
            "SELECT id FROM metadata_proposals WHERE photo_id=? AND status='pending'",
            (self.photo_id,),
        ).fetchone()["id"]

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _mock_stale_uuid(self):
        """Return a context manager that makes photoscript.Photo raise 'invalid photo ID: U1'."""
        from unittest.mock import patch, MagicMock
        mock_ps = MagicMock()
        mock_ps.Photo.side_effect = Exception("invalid photo ID: U1")
        return patch.dict("sys.modules", {"photoscript": mock_ps})

    def test_stale_uuid_marks_proposal_failed(self):
        from unittest.mock import patch
        from flickr.proposal_applier import apply_proposal
        with patch("flickr.proposal_applier._photos_is_running", return_value=True), \
             self._mock_stale_uuid():
            result = apply_proposal(self.db, self.proposal_id, "/fake/lib")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "stale_uuid")
        row = self.db.conn.execute(
            "SELECT status, resolution_note FROM metadata_proposals WHERE id=?",
            (self.proposal_id,),
        ).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["resolution_note"], "stale_uuid")

    def test_stale_uuid_sets_flag_on_photo(self):
        from unittest.mock import patch
        from flickr.proposal_applier import apply_proposal
        with patch("flickr.proposal_applier._photos_is_running", return_value=True), \
             self._mock_stale_uuid():
            apply_proposal(self.db, self.proposal_id, "/fake/lib")
        flag = self.db.conn.execute(
            "SELECT uuid_stale FROM photos WHERE id=?", (self.photo_id,)
        ).fetchone()["uuid_stale"]
        self.assertEqual(flag, 1)

    def test_stale_uuid_in_apply_batch_counted_as_failed_not_error(self):
        from unittest.mock import patch
        from flickr.proposal_applier import apply_batch
        with patch("flickr.proposal_applier._photos_is_running", return_value=True), \
             self._mock_stale_uuid():
            totals = apply_batch(self.db, "/fake/lib")
        self.assertEqual(totals["failed"], 1)
        self.assertEqual(totals["errors"], [])

    def test_non_uuid_error_leaves_proposal_pending(self):
        from unittest.mock import patch, MagicMock
        from flickr.proposal_applier import apply_proposal
        mock_ps = MagicMock()
        mock_ps.Photo.side_effect = Exception("permission denied")
        with patch("flickr.proposal_applier._photos_is_running", return_value=True), \
             patch.dict("sys.modules", {"photoscript": mock_ps}):
            result = apply_proposal(self.db, self.proposal_id, "/fake/lib")
        self.assertFalse(result["ok"])
        self.assertNotEqual(result["reason"], "stale_uuid")
        status = self.db.conn.execute(
            "SELECT status FROM metadata_proposals WHERE id=?", (self.proposal_id,)
        ).fetchone()["status"]
        self.assertEqual(status, "pending")
        flag = self.db.conn.execute(
            "SELECT uuid_stale FROM photos WHERE id=?", (self.photo_id,)
        ).fetchone()["uuid_stale"]
        self.assertEqual(flag, 0)

    def test_migration_010_idempotent(self):
        import tempfile
        from db.db import Database
        from db.migrations.migrate_010_stale_uuid import run as run_migration
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "idempotent.db")
            db = Database(db_path)
            db.close()
            run_migration(db_path)  # first run
            run_migration(db_path)  # second run — must not raise
            import sqlite3
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            # uuid_stale column must exist
            cols = {r[1] for r in conn.execute("PRAGMA table_info(photos)").fetchall()}
            self.assertIn("uuid_stale", cols)
            # 'failed' must be a valid status (insert and delete to verify CHECK passes)
            conn.execute(
                """INSERT INTO metadata_proposals
                   (photo_id, field, proposed_value, source, target, conflict_type,
                    source_hash_at_creation, target_hash_at_creation, status, created_at)
                   SELECT id, 'tags', '[]', 'flickr', 'photos', 'non_conflict',
                          'h', 'h', 'failed', '2026-01-01T00:00:00+00:00'
                   FROM photos LIMIT 1"""
            )
            conn.commit()
            conn.close()
```

- [ ] **Step 2: Run tests to verify they fail for the right reason**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain"
python -m pytest tests/test_core.py -k "TestStaleUuid" -v 2>&1 | tail -20
```

Expected: All 5 tests fail. `test_migration_010_idempotent` and others fail with `ModuleNotFoundError: No module named 'db.migrations.migrate_010_stale_uuid'`. That's the correct TDD failure.

---

### Task 2: Create migration 010

**Files:**
- Create: `db/migrations/migrate_010_stale_uuid.py`

- [ ] **Step 1: Create the migration file**

```python
"""
migrate_010_stale_uuid.py

Two changes:
  1. Recreates metadata_proposals with 'failed' added to the status CHECK
     constraint (SQLite cannot ALTER a CHECK constraint — table recreation required).
  2. Adds photos.uuid_stale INTEGER NOT NULL DEFAULT 0.

Safe to run multiple times (idempotent — checks schema_migrations).

Usage:
    python db/migrations/migrate_010_stale_uuid.py --config config/config.yml
"""

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import yaml


MIGRATION_NAME = "migrate_010_stale_uuid"


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
        print("  [dry-run] Would recreate metadata_proposals with 'failed' status")
        print("  [dry-run] Would add photos.uuid_stale column")
        conn.close()
        return

    # ------------------------------------------------------------------
    # 1. Recreate metadata_proposals with 'failed' in status CHECK
    # ------------------------------------------------------------------
    conn.execute("ALTER TABLE metadata_proposals RENAME TO metadata_proposals_old")

    conn.executescript("""
        CREATE TABLE metadata_proposals (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            photo_id                INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
            field                   TEXT NOT NULL
                                        CHECK(field IN ('title', 'description', 'tags')),
            proposed_value          TEXT,
            source                  TEXT NOT NULL
                                        CHECK(source IN ('flickr', 'photos', 'manual')),
            target                  TEXT NOT NULL
                                        CHECK(target IN ('flickr', 'photos')),
            conflict_type           TEXT NOT NULL
                                        CHECK(conflict_type IN ('non_conflict', 'divergence', 'collision')),
            source_hash_at_creation TEXT,
            target_hash_at_creation TEXT,
            status                  TEXT NOT NULL DEFAULT 'pending'
                                        CHECK(status IN ('pending', 'applied', 'rejected', 'superseded', 'failed')),
            created_at              TEXT NOT NULL,
            resolved_at             TEXT,
            resolution_note         TEXT
        );

        INSERT INTO metadata_proposals
            SELECT * FROM metadata_proposals_old;

        DROP TABLE metadata_proposals_old;

        CREATE INDEX idx_proposals_photo
            ON metadata_proposals(photo_id);
        CREATE INDEX idx_proposals_pending
            ON metadata_proposals(status)
            WHERE status = 'pending';
        CREATE INDEX idx_proposals_field_target
            ON metadata_proposals(field, target, status)
            WHERE status = 'pending';
        CREATE UNIQUE INDEX idx_proposals_identity
            ON metadata_proposals(photo_id, field, proposed_value, target, source)
            WHERE status = 'pending';
    """)

    # ------------------------------------------------------------------
    # 2. Add uuid_stale column to photos
    # ------------------------------------------------------------------
    existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(photos)").fetchall()}
    if "uuid_stale" not in existing_cols:
        conn.execute(
            "ALTER TABLE photos ADD COLUMN uuid_stale INTEGER NOT NULL DEFAULT 0"
        )

    # ------------------------------------------------------------------
    # 3. Record migration
    # ------------------------------------------------------------------
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (name, applied_at) VALUES (?, ?)",
        (MIGRATION_NAME, now_iso()),
    )
    conn.commit()
    conn.close()
    print("  Applied:  metadata_proposals.status now includes 'failed'; photos.uuid_stale added")


def main():
    parser = argparse.ArgumentParser(description="Blue Pearmain DB migration 010")
    parser.add_argument("--config",  default="config/config.yml")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with open(args.config) as f:
        config = yaml.safe_load(f)

    db_path = str(Path(config["database"]["path"]).expanduser())
    print(f"Database: {db_path}")
    run(db_path, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run migration against the live database**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain"
python db/migrations/migrate_010_stale_uuid.py --config config/config.yml
```

Expected output:
```
Database: data/curator.db
  Applied:  metadata_proposals.status now includes 'failed'; photos.uuid_stale added
```

- [ ] **Step 3: Verify schema in live DB**

```bash
sqlite3 data/curator.db "PRAGMA table_info(photos)" | grep uuid_stale
sqlite3 data/curator.db ".schema metadata_proposals" | grep failed
```

Expected: `uuid_stale` appears in `PRAGMA table_info`; `failed` appears in the CHECK constraint.

---

### Task 3: Update schema.sql

**Files:**
- Modify: `db/schema.sql`

- [ ] **Step 1: Add `uuid_stale` column to the photos table definition**

In `db/schema.sql`, find the line:
```sql
    updated_at              TEXT                    -- ISO8601, last time this row was written
```
And insert before the closing `);`:
```sql
    uuid_stale              INTEGER NOT NULL DEFAULT 0, -- 1 if Photos.app rejected UUID as invalid

    updated_at              TEXT                    -- ISO8601, last time this row was written
```

- [ ] **Step 2: Update the `metadata_proposals` status CHECK**

In `db/schema.sql`, find:
```sql
    status                  TEXT NOT NULL DEFAULT 'pending'
                                CHECK(status IN ('pending', 'applied', 'rejected', 'superseded')),
```
Replace with:
```sql
    status                  TEXT NOT NULL DEFAULT 'pending'
                                CHECK(status IN ('pending', 'applied', 'rejected', 'superseded', 'failed')),
```

---

### Task 4: Update `db/db.py`

**Files:**
- Modify: `db/db.py:916`

- [ ] **Step 1: Add `"failed"` to `resolve_proposal` assertion**

Find:
```python
        assert status in ("rejected", "applied", "superseded")
```
Replace with:
```python
        assert status in ("rejected", "applied", "superseded", "failed")
```

---

### Task 5: Update `flickr/proposal_applier.py`

**Files:**
- Modify: `flickr/proposal_applier.py`

All edits in this task are to `flickr/proposal_applier.py`.

- [ ] **Step 1: Add `_handle_stale_uuid` helper (insert before `_supersede`)**

Find the line:
```python
def _supersede(db: "Database", proposal_id: int) -> None:
```
Insert immediately before it:
```python
def _handle_stale_uuid(db: "Database", proposal_id: int, photo_id: int) -> None:
    """Mark a proposal failed and flag the photo row when Photos rejects the UUID."""
    now = _now_iso()
    db.conn.execute(
        "UPDATE photos SET uuid_stale=1, updated_at=? WHERE id=?",
        (now, photo_id),
    )
    db.conn.execute(
        """UPDATE metadata_proposals
           SET status='failed', resolved_at=?, resolution_note='stale_uuid'
           WHERE id=?""",
        (now, proposal_id),
    )
    db.conn.commit()
    log.warning("stale UUID: photo_id=%s proposal %s marked failed", photo_id, proposal_id)


```

- [ ] **Step 2: Update `_write_tags_to_photos` — detect stale UUID on Photo() call**

Find (in `_write_tags_to_photos`):
```python
    try:
        photo = photoscript.Photo(uuid)
    except Exception as e:
        return {"ok": False, "reason": f"photo not found in Photos: {e}"}
```
Replace with:
```python
    try:
        photo = photoscript.Photo(uuid)
    except Exception as e:
        if "invalid photo id" in str(e).lower():
            return {"ok": False, "reason": "stale_uuid", "stale_uuid": True}
        return {"ok": False, "reason": f"photo not found in Photos: {e}"}
```

- [ ] **Step 3: Update `_apply_text_to_photos` — detect stale UUID on Photo() call**

Find (in `_apply_text_to_photos`):
```python
    try:
        photo = photoscript.Photo(uuid)
    except Exception as e:
        return {"ok": False, "reason": f"photo not found in Photos: {e}"}

    try:
        if field == "title":
```
Replace with:
```python
    try:
        photo = photoscript.Photo(uuid)
    except Exception as e:
        if "invalid photo id" in str(e).lower():
            return {"ok": False, "reason": "stale_uuid", "stale_uuid": True}
        return {"ok": False, "reason": f"photo not found in Photos: {e}"}

    try:
        if field == "title":
```

- [ ] **Step 4: Update `_write_text_to_photos_both` — detect stale UUID on Photo() call**

Find (in `_write_text_to_photos_both`):
```python
    try:
        photo = photoscript.Photo(uuid)
    except Exception as e:
        return {"ok": False, "reason": f"photo not found in Photos: {e}"}
    try:
        photo.title = title
```
Replace with:
```python
    try:
        photo = photoscript.Photo(uuid)
    except Exception as e:
        if "invalid photo id" in str(e).lower():
            return {"ok": False, "reason": "stale_uuid", "stale_uuid": True}
        return {"ok": False, "reason": f"photo not found in Photos: {e}"}
    try:
        photo.title = title
```

- [ ] **Step 5: Update `_apply_to_photos` — propagate stale_uuid from helper**

The current `_apply_to_photos` already returns the helper result directly:
```python
    result = _write_tags_to_photos(db, row["photo_id"], uuid, new_tags, library_path)
    if not result["ok"]:
        return result
```
This already propagates `stale_uuid: True` up to `apply_proposal`. No change needed here — the sentinel passes through automatically.

- [ ] **Step 6: Update `apply_proposal` — handle stale_uuid at both return points**

Find the tags branch in `apply_proposal`:
```python
    if field == "tags":
        new_tags = json.loads(row["proposed_value"]) if row["proposed_value"] else []
        if row["target"] == "photos":
            return _apply_to_photos(db, row, new_tags, library_path)
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
            return result
```

Find the text field branch in `apply_proposal`:
```python
    else:
        new_value = row["proposed_value"] or ""
        if row["target"] == "photos":
            return _apply_text_to_photos(db, row, new_value)
```
Replace with:
```python
    else:
        new_value = row["proposed_value"] or ""
        if row["target"] == "photos":
            result = _apply_text_to_photos(db, row, new_value)
            if not result["ok"] and result.get("stale_uuid"):
                _handle_stale_uuid(db, row["id"], row["photo_id"])
                return {"ok": False, "reason": "stale_uuid"}
            return result
```

- [ ] **Step 7: Update `apply_manual_merge` — handle stale_uuid from Photos write**

Find in `apply_manual_merge`:
```python
    if row["uuid"]:
        r = _write_tags_to_photos(db, row["photo_id"], row["uuid"], custom_tags, library_path)
        if not r["ok"]:
            errors.append(f"Photos: {r['reason']}")
```
Replace with:
```python
    if row["uuid"]:
        r = _write_tags_to_photos(db, row["photo_id"], row["uuid"], custom_tags, library_path)
        if not r["ok"]:
            if r.get("stale_uuid"):
                _handle_stale_uuid(db, proposal_id, row["photo_id"])
                return {"ok": False, "reason": "stale_uuid"}
            errors.append(f"Photos: {r['reason']}")
```

- [ ] **Step 8: Update `set_photo_text` — set uuid_stale on stale UUID (no proposal to mark failed)**

Find in `set_photo_text`:
```python
    if uuid:
        r = _write_text_to_photos_both(db, photo_id, uuid, title, description)
        if not r["ok"]:
            warnings.append(f"Photos: {r['reason']}")
```
Replace with:
```python
    if uuid:
        r = _write_text_to_photos_both(db, photo_id, uuid, title, description)
        if not r["ok"]:
            if r.get("stale_uuid"):
                db.conn.execute(
                    "UPDATE photos SET uuid_stale=1, updated_at=? WHERE id=?",
                    (_now_iso(), photo_id),
                )
                warnings.append("Photos: stale UUID — photo no longer in library")
            else:
                warnings.append(f"Photos: {r['reason']}")
```

- [ ] **Step 9: Update `apply_batch` — count stale_uuid as silent failure, not user-visible error**

Find in `apply_batch`:
```python
        else:
            reason = result.get("reason", "unknown")
            totals["failed"] += 1
            totals["errors"].append({"proposal_id": r["id"], "reason": reason})
            log.warning("apply_batch: proposal %s failed: %s", r["id"], reason)
```
Replace with:
```python
        else:
            reason = result.get("reason", "unknown")
            totals["failed"] += 1
            if reason == "stale_uuid":
                log.info("apply_batch: proposal %s permanently failed (stale UUID)", r["id"])
            else:
                totals["errors"].append({"proposal_id": r["id"], "reason": reason})
                log.warning("apply_batch: proposal %s failed: %s", r["id"], reason)
```

---

### Task 6: Run tests and verify

- [ ] **Step 1: Run the new test class**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain"
python -m pytest tests/test_core.py -k "TestStaleUuid" -v 2>&1
```

Expected: `5 passed`.

- [ ] **Step 2: Run the full test suite**

```bash
python -m pytest tests/ -q 2>&1 | tail -5
```

Expected: All tests pass (count will be 427).

---

### Task 7: Update README, commit, close issue

- [ ] **Step 1: Update test count in `README.md`**

Find `422 tests` in two places (the components table and the test summary paragraph) and replace with `427`.

Also add `stale_uuid termination` to the test summary sentence near the end — find:
```
set_photo_text, staleness/drift re-checks, title/description apply
```
Replace with:
```
set_photo_text, stale_uuid termination, staleness/drift re-checks, title/description apply
```

- [ ] **Step 2: Commit**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain"
git add db/migrations/migrate_010_stale_uuid.py db/schema.sql db/db.py \
        flickr/proposal_applier.py tests/test_core.py README.md
git commit -m "$(cat <<'EOF'
Fix #23: mark proposals failed when Photos rejects UUID as invalid

When photoscript.Photo(uuid) raises 'invalid photo ID', the three
write-to-Photos helpers now return a stale_uuid sentinel. Call sites in
apply_proposal and apply_manual_merge catch it, mark the proposal
'failed' (new terminal status), and set uuid_stale=1 on the photo row.
set_photo_text sets uuid_stale=1 and issues a warning. apply_batch
counts stale-UUID failures silently rather than adding to the user-
visible errors list. Migration 010 adds 'failed' to the status CHECK
and adds the uuid_stale column. 5 tests added (427 total).

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 3: Close GH issue #23**

```bash
gh issue close 23 --repo cdevers/Blue-Pearmain --comment \
  "Done. Migration 010 adds 'failed' status and uuid_stale column. \
Proposals with stale UUIDs are now permanently marked failed on first \
apply attempt, removing them from the pending queue. 5 tests added."
```
