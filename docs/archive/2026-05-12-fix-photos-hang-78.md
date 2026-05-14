# Fix #78: Unresponsive Photos.app Detection + Photoscript Timeout

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent the Flask request thread from hanging indefinitely when Photos.app is running but unresponsive, by replacing the process-existence check with a responsiveness check and wrapping all photoscript calls in a thread with a 45-second timeout.

**Architecture:** Add `_photos_is_responsive()` (replaces `_photos_is_running()`) that sends a direct AppleScript command to Photos with a 3-second timeout — if Photos is hung, it won't respond. Add `_run_with_timeout()` that runs any callable in a `ThreadPoolExecutor` and returns `{"ok": False, "reason": "Photos not responding"}` if it exceeds 45 seconds. Refactor the three photo-write functions (`_write_tags_to_photos`, `_apply_text_to_photos`, `_write_text_to_photos_both`) to extract their `photoscript` blocks into inner `_do_write` callables run via `_run_with_timeout`; DB writes remain on the main thread.

**Tech Stack:** Python `concurrent.futures.ThreadPoolExecutor`, `subprocess.run` with `timeout`, `unittest.mock.patch`

---

## Files

| File | Change |
|------|--------|
| `flickr/proposal_applier.py` | Add `import concurrent.futures`; replace `_photos_is_running` with `_photos_is_responsive`; add `_run_with_timeout`; refactor three write functions |
| `tests/test_core.py` | Bulk-rename 16 mock patches; add `TestPhotosIsResponsive`, `TestRunWithTimeout`; add timeout tests to `TestApplyProposal`, `TestApplyManualMerge`, `TestSetPhotoText`, `TestStaleUuid` |

---

## Task 1: Add `_photos_is_responsive` and `_run_with_timeout`; rename call sites and mocks

**Files:**
- Modify: `flickr/proposal_applier.py` (lines 12–20 for import, lines 680–689 for implementation)
- Modify: `tests/test_core.py` (add new test classes near existing `TestApplyProposal`)

- [x] **Step 1: Write failing tests for `_photos_is_responsive`**

Add a new class near the bottom of `tests/test_core.py` (before `TestRunWithTimeout` which you'll add next):

```python
class TestPhotosIsResponsive(unittest.TestCase):
    def test_returns_true_when_osascript_succeeds(self):
        from unittest.mock import patch, MagicMock
        from flickr.proposal_applier import _photos_is_responsive
        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("flickr.proposal_applier.subprocess.run", return_value=mock_result):
            self.assertTrue(_photos_is_responsive())

    def test_returns_false_when_osascript_nonzero(self):
        from unittest.mock import patch, MagicMock
        from flickr.proposal_applier import _photos_is_responsive
        mock_result = MagicMock()
        mock_result.returncode = 1
        with patch("flickr.proposal_applier.subprocess.run", return_value=mock_result):
            self.assertFalse(_photos_is_responsive())

    def test_returns_false_on_subprocess_timeout(self):
        import subprocess
        from unittest.mock import patch
        from flickr.proposal_applier import _photos_is_responsive
        with patch("flickr.proposal_applier.subprocess.run",
                   side_effect=subprocess.TimeoutExpired("osascript", 3)):
            self.assertFalse(_photos_is_responsive())


class TestRunWithTimeout(unittest.TestCase):
    def test_returns_fn_result_on_success(self):
        from flickr.proposal_applier import _run_with_timeout
        result = _run_with_timeout(lambda: {"ok": True, "value": 42})
        self.assertEqual(result, {"ok": True, "value": 42})

    def test_returns_not_responding_when_fn_exceeds_timeout(self):
        import threading
        from flickr.proposal_applier import _run_with_timeout
        blocker = threading.Event()
        def slow():
            blocker.wait(timeout=5)
            return {"ok": True}
        result = _run_with_timeout(slow, timeout=0.05)
        blocker.set()  # release thread immediately so it doesn't linger
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "Photos not responding")
```

- [x] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_core.py::TestPhotosIsResponsive tests/test_core.py::TestRunWithTimeout -v
```

Expected: ImportError or AttributeError — `_photos_is_responsive` and `_run_with_timeout` don't exist yet.

- [x] **Step 3: Add `import concurrent.futures` to `proposal_applier.py`**

At line 18 (after `import subprocess`), add:

```python
import concurrent.futures
import subprocess
```

(Replace the existing `import subprocess` line with these two lines in alphabetical order.)

- [x] **Step 4: Add `_photos_is_responsive`, `_run_with_timeout`, and `_PHOTOS_WRITE_TIMEOUT` to `proposal_applier.py`**

Replace the existing `_photos_is_running` function (lines 680–689) with:

```python
_PHOTOS_WRITE_TIMEOUT = 45  # seconds before a hung photoscript call is abandoned


def _photos_is_responsive(timeout: int = 3) -> bool:
    """
    Return True only if Photos.app is running AND responds to a test AppleScript
    command within `timeout` seconds.  Catches the case where the process exists
    but is hung — which a process-existence check cannot detect.
    """
    try:
        result = subprocess.run(
            ["osascript", "-e", 'tell application "Photos" to name'],
            capture_output=True, text=True, timeout=timeout,
        )
        return result.returncode == 0
    except Exception:
        return False


def _run_with_timeout(fn, *args, timeout: int = _PHOTOS_WRITE_TIMEOUT) -> dict:
    """
    Run fn(*args) in a ThreadPoolExecutor with a timeout.
    Returns {"ok": False, "reason": "Photos not responding"} if the timeout
    fires.  The stray thread cannot be killed (OS limitation) but the Flask
    handler is unblocked and the user gets a clear error.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(fn, *args)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            return {"ok": False, "reason": "Photos not responding"}
        except Exception as exc:
            return {"ok": False, "reason": str(exc)}
```

- [x] **Step 5: Run the new tests to verify they pass**

```bash
python -m pytest tests/test_core.py::TestPhotosIsResponsive tests/test_core.py::TestRunWithTimeout -v
```

Expected: 5 passed.

- [x] **Step 6: Replace the three `_photos_is_running()` call sites with `_photos_is_responsive()`**

In `flickr/proposal_applier.py`, find and replace (3 occurrences):

| Line | Old | New |
|------|-----|-----|
| ~361 | `if not _photos_is_running():` | `if not _photos_is_responsive():` |
| ~450 | `if not _photos_is_running():` | `if not _photos_is_responsive():` |
| ~628 | `if not _photos_is_running():` | `if not _photos_is_responsive():` |

Also update the return reason strings from `"Photos.app is not running"` to `"Photos not responding"` at all three sites.

- [x] **Step 7: Bulk-rename `_photos_is_running` → `_photos_is_responsive` in the test file**

Run this sed command (it only matches `flickr.proposal_applier._photos_is_running`, leaving the unrelated `flickr.metadata_puller._photos_is_running` at line 3026 untouched):

```bash
sed -i '' 's/flickr\.proposal_applier\._photos_is_running/flickr.proposal_applier._photos_is_responsive/g' tests/test_core.py
```

Verify the count — there should be exactly 16 replacements. Check with:

```bash
grep -c "flickr.proposal_applier._photos_is_responsive" tests/test_core.py
```

Expected: `16`

Also verify the metadata_puller reference is unchanged:

```bash
grep "metadata_puller._photos_is_running" tests/test_core.py
```

Expected: 1 line (line ~3026).

- [x] **Step 8: Update the existing `test_photos_not_running_returns_error` assertion**

In `tests/test_core.py`, find `test_photos_not_running_returns_error` (was patching `_photos_is_running`, now `_photos_is_responsive`). The `assertIn("Photos", result["reason"])` assertion still passes (new reason is `"Photos not responding"`), but update the test name to `test_photos_not_responding_returns_error` and the assertion to be more precise:

```python
def test_photos_not_responding_returns_error(self):
    from unittest.mock import patch
    from flickr.proposal_applier import apply_proposal
    pid = self._insert_proposal()
    with patch("flickr.proposal_applier._photos_is_responsive", return_value=False):
        result = apply_proposal(self.db, pid, library_path="/fake/path")
    self.assertFalse(result["ok"])
    self.assertEqual(result["reason"], "Photos not responding")
```

- [x] **Step 9: Run the full test suite**

```bash
python -m pytest tests/ -q
```

Expected: same count as before (604), all passing. If any test fails it will reference the old reason string `"Photos.app is not running"` — update those assertions to `"Photos not responding"`.

- [x] **Step 10: Commit**

```bash
git add flickr/proposal_applier.py tests/test_core.py
git commit -m "feat: replace _photos_is_running with _photos_is_responsive + add _run_with_timeout (#78)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 2: Wrap `_write_tags_to_photos` photoscript block

**Files:**
- Modify: `flickr/proposal_applier.py` (~lines 357–386)
- Modify: `tests/test_core.py` (add test to `TestApplyProposal` or a new class)

- [x] **Step 1: Write a failing test for the timeout path**

Add this test to `TestApplyProposal` in `tests/test_core.py`:

```python
def test_write_tags_timeout_returns_not_responding(self):
    import sys
    from unittest.mock import patch, MagicMock
    from flickr.proposal_applier import _write_tags_to_photos
    with patch("flickr.proposal_applier._photos_is_responsive", return_value=True), \
         patch.dict(sys.modules, {"photoscript": MagicMock()}), \
         patch("flickr.proposal_applier._run_with_timeout",
               return_value={"ok": False, "reason": "Photos not responding"}):
        result = _write_tags_to_photos(MagicMock(), 1, "U1", [], "/path")
    self.assertFalse(result["ok"])
    self.assertEqual(result["reason"], "Photos not responding")
```

- [x] **Step 2: Run to verify it fails**

```bash
python -m pytest tests/test_core.py::TestApplyProposal::test_write_tags_timeout_returns_not_responding -v
```

Expected: FAIL — `_write_tags_to_photos` doesn't call `_run_with_timeout` yet.

- [x] **Step 3: Refactor `_write_tags_to_photos`**

Replace the entire function body with:

```python
def _write_tags_to_photos(
    db: "Database", photo_id: int, uuid: str, new_tags: list[str], library_path: str
) -> dict:
    """Write tags to Photos.app and update the DB cache. Does not touch proposal state."""
    if not _photos_is_responsive():
        return {"ok": False, "reason": "Photos not responding"}
    try:
        import photoscript
    except ImportError:
        return {"ok": False, "reason": "photoscript not installed"}

    def _do_write():
        try:
            photo = photoscript.Photo(uuid)
        except Exception as e:
            if "invalid photo id" in str(e).lower():
                return {"ok": False, "reason": "stale_uuid", "stale_uuid": True}
            return {"ok": False, "reason": f"photo not found in Photos: {e}"}
        try:
            photo.keywords = new_tags
        except Exception as e:
            return {"ok": False, "reason": f"write failed: {e}"}
        try:
            written = list(photo.keywords or [])
        except Exception:
            written = new_tags
        return {"ok": True, "written": written}

    result = _run_with_timeout(_do_write)
    if not result.get("ok"):
        return result
    written = result.get("written", new_tags)
    now = _now_iso()
    db.conn.execute(
        "UPDATE photos SET photos_tags=?, photos_tags_hash=?, meta_synced_photos_at=?, updated_at=? WHERE id=?",
        (json.dumps(written), _compute_hash(written), now, now, photo_id),
    )
    return {"ok": True, "written": written}
```

- [x] **Step 4: Run tests**

```bash
python -m pytest tests/test_core.py::TestApplyProposal -v
```

Expected: all pass including the new timeout test.

- [x] **Step 5: Run full suite**

```bash
python -m pytest tests/ -q
```

Expected: all pass.

- [x] **Step 6: Commit**

```bash
git add flickr/proposal_applier.py tests/test_core.py
git commit -m "refactor: wrap _write_tags_to_photos photoscript block in _run_with_timeout (#78)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 3: Wrap `_apply_text_to_photos` photoscript block

**Files:**
- Modify: `flickr/proposal_applier.py` (~lines 445–492)
- Modify: `tests/test_core.py`

- [x] **Step 1: Write a failing test**

Find the test class that tests `_apply_text_to_photos` (or `TestSetPhotoText` around line 6110). Add:

```python
def test_apply_text_to_photos_timeout_returns_not_responding(self):
    import sys
    from unittest.mock import patch, MagicMock
    from flickr.proposal_applier import _apply_text_to_photos
    row = {"field": "title", "uuid": "U1", "photo_id": 1, "id": 10}
    with patch("flickr.proposal_applier._photos_is_responsive", return_value=True), \
         patch.dict(sys.modules, {"photoscript": MagicMock()}), \
         patch("flickr.proposal_applier._run_with_timeout",
               return_value={"ok": False, "reason": "Photos not responding"}):
        result = _apply_text_to_photos(MagicMock(), row, "new title")
    self.assertFalse(result["ok"])
    self.assertEqual(result["reason"], "Photos not responding")
```

- [x] **Step 2: Run to verify it fails**

```bash
python -m pytest tests/test_core.py::TestSetPhotoText::test_apply_text_to_photos_timeout_returns_not_responding -v
```

Expected: FAIL.

- [x] **Step 3: Refactor `_apply_text_to_photos`**

Replace the function body with:

```python
def _apply_text_to_photos(db: "Database", row, new_value: str) -> dict:
    field = row["field"]  # "title" or "description"
    uuid = row["uuid"]
    if not uuid:
        return {"ok": False, "reason": "photo has no uuid"}
    if not _photos_is_responsive():
        return {"ok": False, "reason": "Photos not responding"}

    try:
        import photoscript
    except ImportError:
        return {"ok": False, "reason": "photoscript not installed"}

    def _do_write():
        try:
            photo = photoscript.Photo(uuid)
        except Exception as e:
            if "invalid photo id" in str(e).lower():
                return {"ok": False, "reason": "stale_uuid", "stale_uuid": True}
            return {"ok": False, "reason": f"photo not found in Photos: {e}"}
        try:
            if field == "title":
                photo.title = new_value
            else:
                photo.description = new_value
        except Exception as e:
            return {"ok": False, "reason": f"write failed: {e}"}
        try:
            written = photo.title if field == "title" else photo.description
            written = (written or "").strip()
        except Exception:
            written = new_value
        return {"ok": True, "written": written}

    result = _run_with_timeout(_do_write)
    if not result.get("ok"):
        return result

    written = result.get("written", new_value)
    now = _now_iso()
    assert field in ("title", "description", "tags"), f"unexpected field: {field!r}"
    col = f"photos_{field}"
    db.conn.execute(
        f"UPDATE photos SET {col}=?, meta_synced_photos_at=?, updated_at=? WHERE id=?",
        (written, now, now, row["photo_id"]),
    )
    _mark_applied(db, row["id"])
    db.conn.commit()
    log.info(
        "applied proposal %s → Photos  photo_id=%s  field=%s",
        row["id"], row["photo_id"], field,
    )
    return {"ok": True}
```

- [x] **Step 4: Run tests**

```bash
python -m pytest tests/test_core.py::TestSetPhotoText -v
```

Expected: all pass.

- [x] **Step 5: Run full suite**

```bash
python -m pytest tests/ -q
```

Expected: all pass.

- [x] **Step 6: Commit**

```bash
git add flickr/proposal_applier.py tests/test_core.py
git commit -m "refactor: wrap _apply_text_to_photos photoscript block in _run_with_timeout (#78)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 4: Wrap `_write_text_to_photos_both` photoscript block

**Files:**
- Modify: `flickr/proposal_applier.py` (~lines 624–658)
- Modify: `tests/test_core.py`

- [x] **Step 1: Write a failing test**

Add to an appropriate test class (e.g. `TestSetPhotoText`):

```python
def test_write_text_both_timeout_returns_not_responding(self):
    import sys
    from unittest.mock import patch, MagicMock
    from flickr.proposal_applier import _write_text_to_photos_both
    with patch("flickr.proposal_applier._photos_is_responsive", return_value=True), \
         patch.dict(sys.modules, {"photoscript": MagicMock()}), \
         patch("flickr.proposal_applier._run_with_timeout",
               return_value={"ok": False, "reason": "Photos not responding"}):
        result = _write_text_to_photos_both(MagicMock(), 1, "U1", "title", "desc")
    self.assertFalse(result["ok"])
    self.assertEqual(result["reason"], "Photos not responding")
```

- [x] **Step 2: Run to verify it fails**

```bash
python -m pytest tests/test_core.py::TestSetPhotoText::test_write_text_both_timeout_returns_not_responding -v
```

Expected: FAIL.

- [x] **Step 3: Refactor `_write_text_to_photos_both`**

Replace the function body with:

```python
def _write_text_to_photos_both(
    db: "Database", photo_id: int, uuid: str, title: str, description: str
) -> dict:
    """Write both title and description to Photos.app and update the DB cache."""
    if not _photos_is_responsive():
        return {"ok": False, "reason": "Photos not responding"}
    try:
        import photoscript
    except ImportError:
        return {"ok": False, "reason": "photoscript not installed"}

    def _do_write():
        try:
            photo = photoscript.Photo(uuid)
        except Exception as e:
            if "invalid photo id" in str(e).lower():
                return {"ok": False, "reason": "stale_uuid", "stale_uuid": True}
            return {"ok": False, "reason": f"photo not found in Photos: {e}"}
        try:
            photo.title = title
            photo.description = description
        except Exception as e:
            return {"ok": False, "reason": f"write failed: {e}"}
        try:
            written_title = (photo.title or "").strip()
            written_desc  = (photo.description or "").strip()
        except Exception:
            written_title = title
            written_desc  = description
        return {"ok": True, "written_title": written_title, "written_desc": written_desc}

    result = _run_with_timeout(_do_write)
    if not result.get("ok"):
        return result

    now = _now_iso()
    db.conn.execute(
        """UPDATE photos
           SET photos_title=?, photos_description=?, meta_synced_photos_at=?, updated_at=?
           WHERE id=?""",
        (result["written_title"], result["written_desc"], now, now, photo_id),
    )
    return {"ok": True}
```

- [x] **Step 4: Run tests**

```bash
python -m pytest tests/test_core.py::TestSetPhotoText -v
```

Expected: all pass.

- [x] **Step 5: Run full suite**

```bash
python -m pytest tests/ -q
```

Expected: all pass.

- [x] **Step 6: Commit**

```bash
git add flickr/proposal_applier.py tests/test_core.py
git commit -m "refactor: wrap _write_text_to_photos_both photoscript block in _run_with_timeout (#78)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 5: README and issue close

**Files:**
- Modify: `README.md`

- [x] **Step 1: Get final test count**

```bash
python -m pytest tests/ -q 2>&1 | tail -1
```

Note the number.

- [x] **Step 2: Update README**

Find the test count in `README.md` (appears in the Components table and Tests section) and update it to the new number.

- [x] **Step 3: Run full suite one final time**

```bash
python -m pytest tests/ -q
```

Expected: all pass, count matches README.

- [x] **Step 4: Final commit**

```bash
git add README.md
git commit -m "Docs: update README test count for #78

Closes #78

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```
