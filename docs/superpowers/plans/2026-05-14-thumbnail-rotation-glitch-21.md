# Thumbnail Rotation Glitch Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Clear `display_rotation` in the DB when the thumbnailer successfully writes a thumbnail, so the CSS rotation correction is not applied to an already-correctly-oriented image.

**Architecture:** One additional column (`display_rotation = 0`) in the thumbnailer's existing UPDATE statement. No schema changes, no new files.

**Tech Stack:** Python, SQLite, pytest, unittest.mock.

**Spec:** `docs/superpowers/specs/2026-05-14-thumbnail-rotation-glitch-21-design.md`

---

## File map

| Action | File | Change |
|--------|------|--------|
| Modify | `poller/thumbnailer.py:119–123` | Add `display_rotation = 0` to the thumbnail UPDATE |
| Modify | `tests/test_core.py` (class `TestThumbnailer`) | Add 1 new test |

---

## Task 1: Fix and test

**Files:**
- Modify: `tests/test_core.py` (class `TestThumbnailer`, after line 861)
- Modify: `poller/thumbnailer.py:119–123`

- [ ] **Step 1: Write the failing test**

Add this test to `TestThumbnailer` in `tests/test_core.py`, after the existing `test_derivative_path_uses_first_char_shard` test (around line 861):

```python
def test_run_clears_display_rotation_on_success(self):
    """Successful thumbnail write must reset display_rotation to 0."""
    import os, tempfile
    from unittest import mock
    from poller.thumbnailer import run

    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = Database(db_path)

    # Insert a Photos-only record with display_rotation=90 and no thumbnail
    photo_id = db.upsert_photo({
        "uuid": "AAAAAAAA-0000-0000-0000-000000000000",
        "display_rotation": 90,
    })

    # Mock derivative_path so the thumbnailer resolves the local source
    with mock.patch("poller.thumbnailer.derivative_path", return_value="/fake/thumb.jpeg"):
        run(
            db=db,
            library_path="/fake/library",
            thumb_root=None,
            flickr_download=False,
            client=None,
            limit=None,
            dry_run=False,
        )

    row = db.conn.execute(
        "SELECT thumbnail_path, display_rotation FROM photos WHERE id = ?",
        (photo_id,),
    ).fetchone()

    self.assertEqual(row["thumbnail_path"], "/fake/thumb.jpeg")
    self.assertEqual(row["display_rotation"], 0,
        "display_rotation must be 0 after thumbnailer sets thumbnail_path")

    db.close()
    os.unlink(db_path)
```

- [ ] **Step 2: Run the test to confirm it fails**

```bash
python -m pytest tests/test_core.py::TestThumbnailer::test_run_clears_display_rotation_on_success -v
```

Expected: FAIL — `display_rotation` is still 90 because the thumbnailer doesn't clear it yet.

- [ ] **Step 3: Implement the fix**

In `poller/thumbnailer.py`, find the UPDATE inside the `run()` function (around line 120):

```python
        if not dry_run:
            db.conn.execute(
                "UPDATE photos SET thumbnail_path = ? WHERE id = ?",
                (thumb, row_id),
            )
```

Replace with:

```python
        if not dry_run:
            db.conn.execute(
                "UPDATE photos SET thumbnail_path = ?, display_rotation = 0 WHERE id = ?",
                (thumb, row_id),
            )
```

- [ ] **Step 4: Run the new test to confirm it passes**

```bash
python -m pytest tests/test_core.py::TestThumbnailer::test_run_clears_display_rotation_on_success -v
```

Expected: PASS.

- [ ] **Step 5: Run the full TestThumbnailer suite**

```bash
python -m pytest tests/test_core.py::TestThumbnailer -v
```

Expected: all tests PASS.

- [ ] **Step 6: Run the full test suite**

```bash
python -m pytest tests/ -q
```

Expected: all pass.

- [ ] **Step 7: Commit and close issue**

```bash
git add poller/thumbnailer.py tests/test_core.py
git commit -m "fix: clear display_rotation when thumbnailer writes thumbnail_path (Closes #21)"
```

After committing, add a comment to GH #21:

> Fixed. The thumbnailer now sets `display_rotation = 0` alongside `thumbnail_path` when it successfully resolves a thumbnail. The CSS correction disappears exactly when the thumbnail is correct, so the over-rotation glitch can't recur.

Then apply the `has-plan` label:

```bash
gh issue edit 21 --add-label "has-plan"
```
