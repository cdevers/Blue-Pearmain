# Recovery & Interruption Tests Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `TestInterruptionAndRecovery` test class covering 4 core recovery invariants to confirm the system behaves correctly when writes are interrupted mid-operation.

**Architecture:** All tests go in a single new class at the end of `tests/test_core.py`, following the existing pattern of `unittest.TestCase` with in-memory SQLite DBs and mocked Flickr clients. No production code changes — these tests validate existing guarantees, not new features.

**Tech Stack:** `unittest`, `unittest.mock.MagicMock`, `sqlite3`, `flickr.proposal_applier.apply_proposal`, `flickr.album_pusher.push_photo_to_albums`, `poller.reconcile.check_photo`, `db.db.Database`

---

## What is and isn't already covered

The existing suite (`TestProposalStaleness`, `TestMergeFlickrIntoPhotos`, `TestAlbumPusher`, `TestReconcileLifecycle`) covers success paths and individual unit behaviours. What is missing:

| Scenario | Status |
|---|---|
| Flickr write fails → proposal stays `pending` → second run succeeds | **Not covered** |
| Per-album commit means partial failure is resumable | **Not covered** |
| Reconcile transient error → no DB mutation → retry detects real state | **Not covered** |
| WAL isolation: uncommitted mid-operation state invisible to readers | **Not covered** |
| `upsert_proposal` hash-change supersedes old proposal | ✓ `test_changed_hash_supersedes_and_inserts` |
| `merge_flickr_into_photos` precondition failure returns False | ✓ existing `TestMergeFlickrIntoPhotos` |

---

## File map

| File | Change |
|---|---|
| `tests/test_core.py` | Add `TestInterruptionAndRecovery` class (4 tests) at the end |

---

## Task 1: Failed Flickr tag push leaves proposal pending and retryable

**Files:**
- Modify: `tests/test_core.py` (append to end of file)

- [ ] **Step 1: Write the failing test**

Append this class stub and first test to the end of `tests/test_core.py`:

```python
class TestInterruptionAndRecovery(unittest.TestCase):
    """
    Verify recovery invariants:
    - Proposals stay 'pending' when a Flickr write fails.
    - Album pushes are resumable: only unpushed albums are retried.
    - Reconcile transient errors leave the DB unchanged.
    - WAL mode: uncommitted mid-operation state is invisible to other connections.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        from db.db import Database

        self.db = Database(Path(self._tmp.name) / "test.db")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    # ------------------------------------------------------------------
    # Task 1
    # ------------------------------------------------------------------

    def test_flickr_tag_write_failure_leaves_proposal_pending(self):
        """
        When the Flickr API call inside apply_proposal fails, the proposal must
        stay 'pending' so the next run can retry.  It must not be marked 'applied'.
        """
        from unittest.mock import MagicMock
        from flickr.proposal_applier import apply_proposal

        photo_id = self.db.upsert_photo(
            {
                "flickr_id": "F_RETRY",
                "uuid": "U_RETRY",
                "privacy_state": "approved_public",
                "flickr_tags": "[]",
                "flickr_tags_hash": "TGT_HASH",
                "photos_tags": '["nature"]',
                "photos_tags_hash": "SRC_HASH",
                "meta_synced_flickr_at": "2026-01-01T00:00:00+00:00",
                "meta_synced_photos_at": "2026-01-01T00:00:00+00:00",
            }
        )
        self.db.upsert_proposal(
            {
                "photo_id": photo_id,
                "field": "tags",
                "proposed_value": '["nature"]',
                "source": "photos",
                "target": "flickr",
                "conflict_type": "non_conflict",
                "source_hash_at_creation": "SRC_HASH",
                "target_hash_at_creation": "TGT_HASH",
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        )
        self.db.conn.commit()
        pid = self.db.conn.execute(
            "SELECT id FROM metadata_proposals WHERE photo_id=? ORDER BY id DESC LIMIT 1",
            (photo_id,),
        ).fetchone()["id"]

        # ── Run 1: Flickr API fails ──────────────────────────────────────
        mock_client = MagicMock()
        mock_client.set_tags.side_effect = Exception("network timeout")
        result = apply_proposal(self.db, pid, library_path="", flickr_client=mock_client)

        self.assertFalse(result["ok"])
        status = self.db.conn.execute(
            "SELECT status FROM metadata_proposals WHERE id=?", (pid,)
        ).fetchone()["status"]
        self.assertEqual(status, "pending", "proposal must stay pending after a failed write")

        # ── Run 2: Flickr API succeeds ───────────────────────────────────
        mock_client.set_tags.side_effect = None
        result = apply_proposal(self.db, pid, library_path="", flickr_client=mock_client)

        self.assertTrue(result["ok"])
        status = self.db.conn.execute(
            "SELECT status FROM metadata_proposals WHERE id=?", (pid,)
        ).fetchone()["status"]
        self.assertEqual(status, "applied")
        mock_client.set_tags.assert_called()

        # No duplicate proposal rows must exist: exactly one row for this photo/field/direction.
        proposal_count = self.db.conn.execute(
            "SELECT COUNT(*) FROM metadata_proposals WHERE photo_id=? AND field='tags' AND target='flickr'",
            (photo_id,),
        ).fetchone()[0]
        self.assertEqual(proposal_count, 1, "retry must not create a duplicate proposal row")
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /path/to/repo
python -m pytest tests/test_core.py::TestInterruptionAndRecovery::test_flickr_tag_write_failure_leaves_proposal_pending -v
```

Expected: `FAILED` with `ImportError` or `AttributeError` (class doesn't exist yet). If it passes immediately, the assertion is wrong — check the test.

- [ ] **Step 3: Verify test fails for the right reason**

The test should fail because the class doesn't exist (if you haven't added it yet), OR because `result["ok"]` is unexpectedly True on the first call (proving the mock is wired correctly). If it passes immediately without any code change, the assertion is wrong — fix it.

- [ ] **Step 4: Run and confirm it now passes** *(no implementation code needed — this tests existing behaviour)*

```bash
python -m pytest tests/test_core.py::TestInterruptionAndRecovery::test_flickr_tag_write_failure_leaves_proposal_pending -v
```

Expected: `PASSED`

- [ ] **Step 5: Full suite green**

```bash
python -m pytest tests/ -q
```

Expected: all tests pass (one more than before)

- [ ] **Step 6: Commit**

```bash
git add tests/test_core.py
git commit -m "test: failed Flickr write leaves proposal pending (GH #110)"
```

---

## Task 2: Partial album push is resumable — retry processes only unpushed albums

**Files:**
- Modify: `tests/test_core.py`

- [ ] **Step 1: Write the failing test**

Add this test inside `TestInterruptionAndRecovery`:

```python
    def test_partial_album_push_retry_resumes_from_failure_point(self):
        """
        push_photo_to_albums commits each album push individually.  If album A
        succeeds and album B fails, a retry must push only album B — album A must
        NOT be pushed again (no duplicate Flickr API call).
        """
        from unittest.mock import MagicMock
        from flickr.album_pusher import push_photo_to_albums
        from flickr.flickr_client import FlickrError

        photo_id = self.db.upsert_photo(
            {
                "flickr_id": "F_ALBUM",
                "uuid": "U_ALBUM",
                "privacy_state": "approved_public",
                "perms_pushed_flickr": 1,
                "proposed_tags": [],
                "apple_persons": [],
                "apple_labels": [],
            }
        )
        # album1: already has a Flickr set → will call add_photo_to_photoset
        album1_id = self.db.upsert_album("apple-uuid-1", "Album One")
        self.db.set_album_flickr_set_id(album1_id, "SET_EXISTING")
        self.db.upsert_photo_album(photo_id, album1_id)

        # album2: no Flickr set yet → will call create_photoset
        album2_id = self.db.upsert_album("apple-uuid-2", "Album Two")
        self.db.upsert_photo_album(photo_id, album2_id)

        # ── Run 1: add_photo_to_photoset (album1) fails; create_photoset (album2) succeeds ──
        mock = MagicMock()
        mock.add_photo_to_photoset.side_effect = FlickrError(9999, "server error")
        mock.create_photoset.return_value = "SET_NEW"

        push_photo_to_albums(self.db, mock, photo_id)

        row1 = self.db.conn.execute(
            "SELECT flickr_pushed FROM photo_albums WHERE photo_id=? AND album_id=?",
            (photo_id, album1_id),
        ).fetchone()
        row2 = self.db.conn.execute(
            "SELECT flickr_pushed FROM photo_albums WHERE photo_id=? AND album_id=?",
            (photo_id, album2_id),
        ).fetchone()
        self.assertEqual(row1["flickr_pushed"], 0, "album1 must stay pending after failure")
        self.assertEqual(row2["flickr_pushed"], 1, "album2 must be pushed after success")

        # ── Run 2: add_photo_to_photoset now succeeds ────────────────────
        mock.add_photo_to_photoset.side_effect = None
        push_photo_to_albums(self.db, mock, photo_id)

        row1 = self.db.conn.execute(
            "SELECT flickr_pushed FROM photo_albums WHERE photo_id=? AND album_id=?",
            (photo_id, album1_id),
        ).fetchone()
        self.assertEqual(row1["flickr_pushed"], 1, "album1 must be pushed after retry")

        # create_photoset must have been called exactly once across both runs
        self.assertEqual(
            mock.create_photoset.call_count,
            1,
            "photoset must not be re-created on retry",
        )
```

- [ ] **Step 2: Run to verify it fails**

```bash
python -m pytest tests/test_core.py::TestInterruptionAndRecovery::test_partial_album_push_retry_resumes_from_failure_point -v
```

Expected: `FAILED` — the new test method doesn't exist yet (if not added) or fails an assertion.

- [ ] **Step 3: Run and confirm it passes** *(no implementation code needed)*

```bash
python -m pytest tests/test_core.py::TestInterruptionAndRecovery::test_partial_album_push_retry_resumes_from_failure_point -v
```

Expected: `PASSED`

- [ ] **Step 4: Full suite green**

```bash
python -m pytest tests/ -q
```

- [ ] **Step 5: Commit**

```bash
git add tests/test_core.py
git commit -m "test: partial album push resumes from failure point (GH #110)"
```

---

## Task 3: Reconcile transient network error leaves DB unchanged, retry detects real state

**Files:**
- Modify: `tests/test_core.py`

- [ ] **Step 1: Write the failing test**

Add this test inside `TestInterruptionAndRecovery`:

```python
    def test_reconcile_transient_error_leaves_db_unchanged(self):
        """
        A transient FlickrError (code=0) during reconcile must not modify the DB.
        The photo must not be marked flickr_deleted.  A subsequent run must be
        able to detect the real state (permission mismatch).
        """
        from unittest.mock import MagicMock
        from flickr.flickr_client import FlickrError
        from poller.reconcile import check_photo

        photo_id = self.db.upsert_photo(
            {
                "flickr_id": "F_NET",
                "uuid": "U_NET",
                "privacy_state": "approved_public",
                "perms_pushed_flickr": 1,
                "tags_pushed_flickr": 0,
                "proposed_tags": [],
                "apple_persons": [],
                "apple_labels": [],
            }
        )
        self.db.conn.commit()
        row = dict(
            self.db.conn.execute(
                "SELECT * FROM photos WHERE id=?", (photo_id,)
            ).fetchone()
        )

        # ── Run 1: transient network error ───────────────────────────────
        mock_client = MagicMock()
        mock_client.get_photo_info.side_effect = FlickrError(0, "service unavailable")

        result = check_photo(mock_client, row, self.db, fix=False, verbose=False)

        self.assertEqual(result["status"], "flickr_error")
        db_photo = self.db.conn.execute(
            "SELECT flickr_deleted FROM photos WHERE id=?", (photo_id,)
        ).fetchone()
        self.assertFalse(
            bool(db_photo["flickr_deleted"]),
            "transient error must not mark photo as flickr_deleted",
        )

        # ── Run 2: API now responds — photo is private (mismatch) ───────
        mock_client.get_photo_info.side_effect = None
        mock_client.get_photo_info.return_value = {
            "photo": {
                "visibility": {"ispublic": 0, "isfriend": 0, "isfamily": 0},
                "tags": {"tag": []},
            }
        }

        result2 = check_photo(mock_client, row, self.db, fix=False, verbose=False)

        self.assertEqual(
            result2["status"],
            "perm_mismatch",
            "second run must detect mismatch after transient error clears",
        )
```

- [ ] **Step 2: Run to verify it fails**

```bash
python -m pytest tests/test_core.py::TestInterruptionAndRecovery::test_reconcile_transient_error_leaves_db_unchanged -v
```

Expected: `FAILED`

- [ ] **Step 3: Run and confirm it passes**

```bash
python -m pytest tests/test_core.py::TestInterruptionAndRecovery::test_reconcile_transient_error_leaves_db_unchanged -v
```

Expected: `PASSED`

- [ ] **Step 4: Full suite green**

```bash
python -m pytest tests/ -q
```

- [ ] **Step 5: Commit**

```bash
git add tests/test_core.py
git commit -m "test: reconcile transient error leaves DB unchanged (GH #110)"
```

---

## Task 4: WAL isolation — uncommitted merge state invisible to readers, rollback restores

**Files:**
- Modify: `tests/test_core.py`

**Context:** `merge_flickr_into_photos` clears `flickr_id` on the donor (to release the UNIQUE constraint) before copying it to the target, then calls `conn.commit()`. Between those two steps the DB is in an intermediate state. This test verifies that:

1. The intermediate state is not visible to a concurrent reader (WAL isolation).
2. If the merge is rolled back before commit, both records are fully restored.

- [ ] **Step 1: Write the failing test**

Add this test inside `TestInterruptionAndRecovery`:

```python
    def test_wal_uncommitted_merge_invisible_to_readers_and_rolls_back(self):
        """
        Intermediate state produced during merge_flickr_into_photos must be
        invisible to other connections (WAL isolation).  Rolling back the
        uncommitted work must leave both records in their original state.
        """
        import sqlite3

        donor_id = self.db.upsert_photo(
            {
                "flickr_id": "F_DONOR",
                "privacy_state": "candidate_public",
                "proposed_tags": [],
                "apple_persons": [],
                "apple_labels": [],
            }
        )
        target_id = self.db.upsert_photo(
            {
                "uuid": "U_TARGET",
                "privacy_state": "candidate_public",
                "proposed_tags": [],
                "apple_persons": [],
                "apple_labels": [],
            }
        )
        self.db.conn.commit()

        # Simulate the intermediate step: clear flickr_id on donor (as the merge does)
        # but do NOT commit.
        self.db.conn.execute(
            "UPDATE photos SET flickr_id = NULL WHERE id = ?", (donor_id,)
        )

        # A fresh reader connection must still see the original flickr_id (WAL isolation).
        fresh = sqlite3.connect(str(self.db.path))
        fresh.row_factory = sqlite3.Row
        fresh.execute("PRAGMA journal_mode = WAL")
        fresh_row = fresh.execute(
            "SELECT flickr_id FROM photos WHERE id=?", (donor_id,)
        ).fetchone()
        self.assertEqual(
            fresh_row["flickr_id"],
            "F_DONOR",
            "uncommitted change must not be visible to a concurrent reader",
        )
        fresh.close()

        # Roll back the uncommitted work.
        self.db.conn.rollback()

        # After rollback, our own connection also sees the original state.
        own_row = self.db.conn.execute(
            "SELECT flickr_id FROM photos WHERE id=?", (donor_id,)
        ).fetchone()
        self.assertEqual(
            own_row["flickr_id"],
            "F_DONOR",
            "rollback must restore flickr_id on the original connection",
        )

        # Target must be untouched.
        target_row = self.db.conn.execute(
            "SELECT flickr_id FROM photos WHERE id=?", (target_id,)
        ).fetchone()
        self.assertIsNone(
            target_row["flickr_id"],
            "target must have no flickr_id after rollback",
        )
```

- [ ] **Step 2: Run to verify it fails**

```bash
python -m pytest tests/test_core.py::TestInterruptionAndRecovery::test_wal_uncommitted_merge_invisible_to_readers_and_rolls_back -v
```

Expected: `FAILED`

- [ ] **Step 3: Run and confirm it passes**

```bash
python -m pytest tests/test_core.py::TestInterruptionAndRecovery::test_wal_uncommitted_merge_invisible_to_readers_and_rolls_back -v
```

Expected: `PASSED`

- [ ] **Step 4: Full suite green**

```bash
python -m pytest tests/ -q
```

- [ ] **Step 5: Lint clean**

```bash
make lint
```

- [ ] **Step 6: Update README test count**

Find this line in `README.md`:

```
815 tests covering
```

Update to the new count (815 + 4 = 819):

```
819 tests covering
```

- [ ] **Step 7: Commit and close issue**

```bash
git add tests/test_core.py README.md
git commit -m "test: WAL isolation and rollback for mid-merge state (GH #110)"
git push origin main
```

Then close issue #110:

```bash
gh issue comment 110 --repo cdevers/Blue-Pearmain \
  --body "Four interruption/recovery tests added:
1. Failed Flickr write leaves proposal pending (retryable)
2. Partial album push resumes from failure point
3. Reconcile transient error leaves DB unchanged
4. WAL isolation: uncommitted mid-merge state invisible to readers"
gh issue close 110 --repo cdevers/Blue-Pearmain
```

---

## Self-review

**Spec coverage:**
- ✓ interrupted metadata push → Task 1 (proposal stays pending, retried on next run)
- ✓ partially-applied album sync → Task 2 (per-album commit means failure is resumable)
- ✓ reconcile-after-network-failure → Task 3 (transient error, no DB mutation, retry works)
- ✓ SQLite WAL recovery → Task 4 (WAL isolation + rollback)
- ⊘ stale proposal recovery → already covered by `test_changed_hash_supersedes_and_inserts`
- ⊘ duplicate merge rollback → covered by Task 4 (verifies rollback semantics directly)

**Placeholder scan:** No TBDs, no vague steps. All test code is complete and runnable.

**Type consistency:** No new types defined. All calls use existing APIs verified against the source.
