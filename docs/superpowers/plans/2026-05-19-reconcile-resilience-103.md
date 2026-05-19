# Reconcile Resilience + Progress Logging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `bp reconcile` resilient to deleted/4xx Flickr photos (marks them in DB, keeps looping) and emit progress logs every 500 photos so long runs stay observable.

**Architecture:** Fix the abstraction leak in `flickr_client._call` so it always raises `FlickrError`; extend `check_photo` to recognise codes 1 and 404 as "deleted, mark DB and skip"; add a `flickr_deleted` WHERE filter and progress counter to `reconcile.main()`; widen the same check in `metadata_puller`.

**Tech Stack:** Python 3, SQLite via `db.db.Database`, `unittest.mock`, `pytest`

---

### Task 1: `_call` raises `FlickrError` for permanent HTTP codes

**Files:**
- Modify: `flickr/flickr_client.py` (lines ~131-133)
- Modify: `tests/test_core.py` — update two existing tests in `TestFlickrClientRetry`

The two existing tests `test_404_raises_immediately_without_retry` and `test_403_raises_immediately_without_retry` currently assert `req.HTTPError` is raised. After this change they must assert `FlickrError` with the correct `.code`.

- [ ] **Step 1: Update the existing 404 test to assert `FlickrError`**

In `tests/test_core.py`, find `test_404_raises_immediately_without_retry` (~line 1937) and replace its body with:

```python
    def test_404_raises_immediately_without_retry(self):
        """HTTP 404 is a permanent error — raises FlickrError(404), no retry."""
        from unittest.mock import patch
        from flickr.flickr_client import FlickrError

        c = self._make_client()
        not_found = self._mock_response(404)
        call_count = 0

        def counting_get(*a, **kw):
            nonlocal call_count
            call_count += 1
            return not_found

        with patch.object(c._session, "get", side_effect=counting_get):
            with patch("time.sleep"):
                with self.assertRaises(FlickrError) as ctx:
                    c._call("flickr.photos.getInfo")
        self.assertEqual(call_count, 1)   # no retries
        self.assertEqual(ctx.exception.code, 404)
```

- [ ] **Step 2: Update the existing 403 test to assert `FlickrError`**

Find `test_403_raises_immediately_without_retry` (~line 1957) and replace its body:

```python
    def test_403_raises_immediately_without_retry(self):
        """HTTP 403 is a permanent error — raises FlickrError(403), no retry."""
        from unittest.mock import patch
        from flickr.flickr_client import FlickrError

        c = self._make_client()
        forbidden = self._mock_response(403)
        call_count = 0

        def counting_get(*a, **kw):
            nonlocal call_count
            call_count += 1
            return forbidden

        with patch.object(c._session, "get", side_effect=counting_get):
            with patch("time.sleep"):
                with self.assertRaises(FlickrError) as ctx:
                    c._call("flickr.photos.getInfo")
        self.assertEqual(call_count, 1)
        self.assertEqual(ctx.exception.code, 403)
```

- [ ] **Step 3: Run updated tests — confirm they fail**

```bash
python -m pytest tests/test_core.py::TestFlickrClientRetry::test_404_raises_immediately_without_retry tests/test_core.py::TestFlickrClientRetry::test_403_raises_immediately_without_retry -v
```

Expected: both FAIL (`req.HTTPError` is raised, not `FlickrError`)

- [ ] **Step 4: Fix `_call` in `flickr/flickr_client.py`**

Find this block (~line 131):

```python
        # Permanent client errors — raise immediately, no retry
        if resp.status_code in _PERMANENT_HTTP_CODES:
            resp.raise_for_status()  # raises requests.HTTPError
```

Replace with:

```python
        # Permanent client errors — raise immediately, no retry.
        # Wrap in FlickrError so callers only ever see one exception type.
        if resp.status_code in _PERMANENT_HTTP_CODES:
            raise FlickrError(
                resp.status_code,
                getattr(resp, "reason", None) or f"HTTP {resp.status_code}",
            )
```

- [ ] **Step 5: Run updated tests — confirm they pass**

```bash
python -m pytest tests/test_core.py::TestFlickrClientRetry::test_404_raises_immediately_without_retry tests/test_core.py::TestFlickrClientRetry::test_403_raises_immediately_without_retry -v
```

Expected: both PASS

- [ ] **Step 6: Run full test suite — confirm no regressions**

```bash
python -m pytest tests/ -q
```

Expected: all tests pass (count unchanged)

- [ ] **Step 7: Commit**

```bash
git add flickr/flickr_client.py tests/test_core.py
git commit -m "fix: _call raises FlickrError for permanent HTTP codes (GH #103)

Replaces raise_for_status() (which raised requests.HTTPError) with an
explicit FlickrError so callers only ever handle one exception type.
HTTP status code is preserved in FlickrError.code.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 2: `check_photo` handles FlickrError(1) and FlickrError(404) as flickr-deleted

**Files:**
- Modify: `tests/test_core.py` — add new `TestCheckPhoto` class
- Modify: `poller/reconcile.py` — `check_photo` function

`check_photo` signature: `check_photo(client, row, db, fix, verbose) -> dict`

`row` dict needs keys: `id`, `flickr_id`, `privacy_state`, `perms_pushed_flickr`, `tags_pushed_flickr`, `pushed_tags`

- [ ] **Step 1: Write failing tests — add `TestCheckPhoto` class to `tests/test_core.py`**

Add after `TestReconcileExitCodes` (~line 2744):

```python
class TestCheckPhoto(unittest.TestCase):
    """check_photo and reconcile main(): deleted-photo handling."""

    def setUp(self):
        import tempfile, os
        from db.db import Database
        fd, self.tmp_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = Database(self.tmp_path)
        self.photo_id = self.db.upsert_photo({
            "flickr_id": "99999",
            "privacy_state": "approved_public",
            "perms_pushed_flickr": 1,
        })

    def tearDown(self):
        import os
        self.db.close()
        os.unlink(self.tmp_path)

    def _make_row(self):
        return {
            "id": self.photo_id,
            "flickr_id": "99999",
            "privacy_state": "approved_public",
            "perms_pushed_flickr": 1,
            "tags_pushed_flickr": 0,
            "pushed_tags": None,
        }

    def _make_client(self, error):
        from unittest.mock import MagicMock
        client = MagicMock()
        client.get_photo_info.side_effect = error
        return client

    def _run_main(self, extra_argv=None):
        """Run reconcile.main() against self.tmp_path with a mocked FlickrClient.

        Returns (exit_code, stdout_text, mock_client).
        The mock client's get_photo_info raises FlickrError(404).
        """
        import sys, tempfile, os, io, yaml
        from contextlib import redirect_stdout
        from unittest.mock import patch, MagicMock
        from flickr.flickr_client import FlickrError

        config = {
            "database": {"path": self.tmp_path},
            "flickr": {
                "username": "tester",
                "api_key": "k", "api_secret": "s",
                "oauth_token": "t", "oauth_token_secret": "ts",
            },
        }
        with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as f:
            yaml.dump(config, f)
            cfg_path = f.name

        mock_client = MagicMock()
        mock_client.test_login.return_value = {}
        mock_client.get_photo_info.side_effect = FlickrError(404, "HTTP 404")

        old_argv = sys.argv[:]
        sys.argv = ["bp", "--config", cfg_path, "--limit", "100"] + (extra_argv or [])
        buf = io.StringIO()
        try:
            with patch("poller.reconcile.FlickrClient") as MockFC:
                MockFC.from_config.return_value = mock_client
                with redirect_stdout(buf):
                    from poller.reconcile import main
                    code = main()
        finally:
            sys.argv = old_argv
            os.unlink(cfg_path)

        return code, buf.getvalue(), mock_client

    def test_flickr_error_code_1_returns_deleted_status(self):
        """FlickrError(1) → status flickr_deleted, mark_flickr_deleted called."""
        from flickr.flickr_client import FlickrError
        from poller.reconcile import check_photo

        client = self._make_client(FlickrError(1, "Photo not found"))
        result = check_photo(client, self._make_row(), self.db, fix=False, verbose=False)

        self.assertEqual(result["status"], "flickr_deleted")
        row = self.db.conn.execute(
            "SELECT flickr_deleted FROM photos WHERE id = ?", (self.photo_id,)
        ).fetchone()
        self.assertEqual(row["flickr_deleted"], 1)

    def test_flickr_error_code_404_returns_deleted_status(self):
        """FlickrError(404) (from HTTP 404) → status flickr_deleted, mark_flickr_deleted called."""
        from flickr.flickr_client import FlickrError
        from poller.reconcile import check_photo

        client = self._make_client(FlickrError(404, "HTTP 404"))
        result = check_photo(client, self._make_row(), self.db, fix=False, verbose=False)

        self.assertEqual(result["status"], "flickr_deleted")
        row = self.db.conn.execute(
            "SELECT flickr_deleted FROM photos WHERE id = ?", (self.photo_id,)
        ).fetchone()
        self.assertEqual(row["flickr_deleted"], 1)

    def test_other_flickr_error_returns_flickr_error_status(self):
        """A non-404/non-1 FlickrError stays flickr_error and does not mark deleted."""
        from flickr.flickr_client import FlickrError
        from poller.reconcile import check_photo

        client = self._make_client(FlickrError(500, "Server error"))
        result = check_photo(client, self._make_row(), self.db, fix=False, verbose=False)

        self.assertEqual(result["status"], "flickr_error")
        row = self.db.conn.execute(
            "SELECT flickr_deleted FROM photos WHERE id = ?", (self.photo_id,)
        ).fetchone()
        self.assertNotEqual(row["flickr_deleted"], 1)
```

- [ ] **Step 2: Run new tests — confirm they fail**

```bash
python -m pytest tests/test_core.py::TestCheckPhoto::test_flickr_error_code_1_returns_deleted_status tests/test_core.py::TestCheckPhoto::test_flickr_error_code_404_returns_deleted_status tests/test_core.py::TestCheckPhoto::test_other_flickr_error_returns_flickr_error_status -v
```

Expected: first two FAIL (`check_photo` currently sets `status = "flickr_error"` for all codes); third PASS

- [ ] **Step 3: Update `check_photo` in `poller/reconcile.py`**

Find the `except FlickrError as e:` block in `check_photo` (~line 79):

```python
    except FlickrError as e:
        result["status"] = "flickr_error"
        result["errors"] = [str(e)]
        return result
```

Replace with:

```python
    except FlickrError as e:
        if e.code in (1, 404):
            db.mark_flickr_deleted(row["id"])
            result["status"] = "flickr_deleted"
        else:
            result["status"] = "flickr_error"
            result["errors"] = [str(e)]
        return result
```

- [ ] **Step 4: Run new tests — confirm they pass**

```bash
python -m pytest tests/test_core.py::TestCheckPhoto::test_flickr_error_code_1_returns_deleted_status tests/test_core.py::TestCheckPhoto::test_flickr_error_code_404_returns_deleted_status tests/test_core.py::TestCheckPhoto::test_other_flickr_error_returns_flickr_error_status -v
```

Expected: all three PASS

- [ ] **Step 5: Run full test suite — confirm no regressions**

```bash
python -m pytest tests/ -q
```

Expected: all tests pass

- [ ] **Step 6: Commit**

```bash
git add poller/reconcile.py tests/test_core.py
git commit -m "fix: check_photo marks flickr_deleted for FlickrError codes 1 and 404 (GH #103)

FlickrError code 1 is the Flickr API 'photo not found' signal; code 404
is the HTTP 404 that now arrives via the fixed _call. Both mean the photo
no longer exists on Flickr — mark it deleted in the DB so future reconcile
runs skip it rather than stopping the loop.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 3: Filter `flickr_deleted` photos from the reconcile query

**Files:**
- Modify: `tests/test_core.py` — add test to `TestCheckPhoto` using `_run_main()`
- Modify: `poller/reconcile.py` — `main()` SELECT query

The test calls `main()` directly (via `_run_main`) so it exercises the actual SQL in reconcile.py, not a hardcoded copy.

- [ ] **Step 1: Write failing test**

Add to `TestCheckPhoto` (after `test_other_flickr_error_returns_flickr_error_status`):

```python
    def test_reconcile_skips_flickr_deleted_photos(self):
        """main() must not call get_photo_info for photos already marked flickr_deleted."""
        # Pre-mark the photo as deleted (as if a previous run already handled it)
        self.db.mark_flickr_deleted(self.photo_id)

        _code, _out, mock_client = self._run_main()

        mock_client.get_photo_info.assert_not_called()
```

- [ ] **Step 2: Run new test — confirm it fails**

```bash
python -m pytest tests/test_core.py::TestCheckPhoto::test_reconcile_skips_flickr_deleted_photos -v
```

Expected: FAIL — `get_photo_info` IS called because the current query has no `flickr_deleted` filter.

- [ ] **Step 3: Add `flickr_deleted` filter to the SELECT in `reconcile.main()`**

In `poller/reconcile.py`, find the SELECT inside `main()` (~line 242):

```python
    rows = db.conn.execute(
        """SELECT id, flickr_id, privacy_state, pushed_tags,
                  perms_pushed_flickr, tags_pushed_flickr
           FROM photos
           WHERE flickr_id IS NOT NULL
             AND (perms_pushed_flickr = 1 OR tags_pushed_flickr = 1)
           ORDER BY reviewed_at DESC
           LIMIT ?""",
        (args.limit,),
    ).fetchall()
```

Replace with:

```python
    rows = db.conn.execute(
        """SELECT id, flickr_id, privacy_state, pushed_tags,
                  perms_pushed_flickr, tags_pushed_flickr
           FROM photos
           WHERE flickr_id IS NOT NULL
             AND (perms_pushed_flickr = 1 OR tags_pushed_flickr = 1)
             AND (flickr_deleted IS NULL OR flickr_deleted = 0)
           ORDER BY reviewed_at DESC
           LIMIT ?""",
        (args.limit,),
    ).fetchall()
```

- [ ] **Step 4: Run new test — confirm it passes**

```bash
python -m pytest tests/test_core.py::TestCheckPhoto::test_reconcile_skips_flickr_deleted_photos -v
```

Expected: PASS

- [ ] **Step 5: Run full test suite — confirm no regressions**

```bash
python -m pytest tests/ -q
```

Expected: all tests pass

- [ ] **Step 6: Commit**

```bash
git add poller/reconcile.py tests/test_core.py
git commit -m "fix: reconcile query skips flickr_deleted photos (GH #103)

Matches the filter already used by sync_metadata. Photos marked
flickr_deleted=1 by check_photo (or by sync-metadata) are excluded
from future reconcile runs.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 4: `flickr_deleted_count`, updated summary, and progress logging in `reconcile.main()`

**Files:**
- Modify: `tests/test_core.py` — add test to `TestCheckPhoto` using `_run_main()`
- Modify: `poller/reconcile.py` — `main()` counter, result-loop, summary line, progress log

Before this task, a `"flickr_deleted"` status from `check_photo` falls into the `else` branch of the result loop, which increments `mismatch_count`. This causes exit code 1 (mismatches found) when it should be 0 (clean). The failing test catches this.

- [ ] **Step 1: Write failing test**

Add to `TestCheckPhoto`:

```python
    def test_reconcile_deleted_photo_exits_zero_and_reports_in_summary(self):
        """A run where the only outcome is a deleted photo exits 0, not 1 (mismatch).
        The summary line must include 'flickr-deleted=1'."""
        # The mock client in _run_main raises FlickrError(404) for every photo.
        # Our DB has exactly one pushed photo, so we expect 1 deleted, 0 mismatches.
        code, output, _client = self._run_main()

        self.assertEqual(code, 0, f"Expected exit 0 (clean). Output:\n{output}")
        self.assertIn("flickr-deleted=1", output)
        self.assertIn("mismatched=0", output)
```

- [ ] **Step 2: Run new test — confirm it fails**

```bash
python -m pytest tests/test_core.py::TestCheckPhoto::test_reconcile_deleted_photo_exits_zero_and_reports_in_summary -v
```

Expected: FAIL — exit code is 1 (the `else` branch increments `mismatch_count`) and `"flickr-deleted="` is absent from the summary.

- [ ] **Step 3: Update `reconcile.main()` — add counter, handle status in loop, update summary, add progress log**

In `poller/reconcile.py`, make three changes inside `main()`:

**a) Add `flickr_deleted_count` after the other counters (~line 255):**

```python
    total = len(rows)
    ok_count = 0
    mismatch_count = 0
    error_count = 0
    fix_ok_count = 0
    fix_fail_count = 0
    flickr_deleted_count = 0
```

**b) Replace the result-handling `try/for` block (~lines 266–292). The full updated block:**

```python
    try:
        for i, row in enumerate(rows, 1):
            result = check_photo(client, dict(row), db, fix=args.fix, verbose=args.verbose)
            ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            fid = result["flickr_id"]
            url = f"https://www.flickr.com/photos/{flickr_username}/{fid}"

            if result["status"] == "ok":
                ok_count += 1
                if args.verbose:
                    print(format_result_line(result, url, ts))

            elif result["status"] == "flickr_deleted":
                flickr_deleted_count += 1
                log.warning("%s [deleted] %s — marked flickr_deleted in DB", ts, url)

            elif result["status"] == "flickr_error":
                error_count += 1
                print(format_result_line(result, url, ts))
                for msg in result["errors"]:
                    print(f"      error: {msg}")

            else:
                mismatch_count += 1
                fix_ok_count += len(result["fixes"])
                fix_fail_count += len(result["errors"])
                print(format_result_line(result, url, ts))
                for msg in result["errors"]:
                    print(f"      error: {msg}")

            if i % 500 == 0:
                log.info(
                    "progress: %d/%d checked  ok=%d  mismatch=%d  deleted=%d  errors=%d",
                    i, total, ok_count, mismatch_count, flickr_deleted_count, error_count,
                )

    except Exception as e:
        log.error(f"Reconcile interrupted: {e}")
        error_count += 1
```

Note: `for row in rows:` becomes `for i, row in enumerate(rows, 1):`.

**c) Update the summary `print` (~line 296):**

```python
    print(
        f"  checked={total}"
        f"  ok={ok_count}"
        f"  mismatched={mismatch_count}"
        f"  flickr-deleted={flickr_deleted_count}"
        + (f"  fixed={fix_ok_count}  fix-failed={fix_fail_count}" if args.fix else "")
        + f"  api-errors={error_count}"
    )
```

- [ ] **Step 4: Run new test — confirm it passes**

```bash
python -m pytest tests/test_core.py::TestCheckPhoto::test_reconcile_deleted_photo_exits_zero_and_reports_in_summary -v
```

Expected: PASS

- [ ] **Step 5: Run full test suite — confirm no regressions**

```bash
python -m pytest tests/ -q
```

Expected: all tests pass

- [ ] **Step 6: Commit**

```bash
git add poller/reconcile.py tests/test_core.py
git commit -m "feat: reconcile progress logging + flickr_deleted counter (GH #103)

- Log progress every 500 photos so long runs stay observable
- Count and report flickr_deleted results separately from mismatches
  (flickr_deleted is not a mismatch; it exits 0, not 1)
- Summary line gains flickr-deleted=N field

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 5: `metadata_puller` handles `FlickrError(404)` as flickr-deleted

**Files:**
- Modify: `tests/test_core.py` — add test to `TestMetadataPuller`
- Modify: `flickr/metadata_puller.py` — widen `e.code == 1` check

- [ ] **Step 1: Write failing test — add to `TestMetadataPuller`**

Add after `test_flickr_not_found_dry_run_does_not_write_db` (~line 3927):

```python
    def test_http_404_returns_flickr_deleted_status(self):
        """FlickrError(404) from HTTP 404 is treated identically to code 1."""
        from flickr.flickr_client import FlickrError

        self.mock_flickr.get_photo_info.side_effect = FlickrError(404, "HTTP 404")
        from flickr.metadata_puller import pull_photo_metadata

        result = pull_photo_metadata(self.db, self.mock_flickr, self.photo_id, self.library)
        self.assertEqual(result["status"], "flickr_deleted")
        row = self.db.conn.execute(
            "SELECT flickr_deleted FROM photos WHERE id = ?", (self.photo_id,)
        ).fetchone()
        self.assertEqual(row["flickr_deleted"], 1)
```

- [ ] **Step 2: Run new test — confirm it fails**

```bash
python -m pytest tests/test_core.py::TestMetadataPuller::test_http_404_returns_flickr_deleted_status -v
```

Expected: FAIL (`FlickrError(404)` hits the `else` branch → status `"flickr_error"`, not `"flickr_deleted"`)

- [ ] **Step 3: Update `metadata_puller.py`**

In `flickr/metadata_puller.py`, find the deleted-photo guard (~line 419):

```python
            if e.code == 1:
```

Replace with:

```python
            if e.code in (1, 404):
```

- [ ] **Step 4: Run new test — confirm it passes**

```bash
python -m pytest tests/test_core.py::TestMetadataPuller::test_http_404_returns_flickr_deleted_status -v
```

Expected: PASS

- [ ] **Step 5: Run full test suite — confirm all tests pass**

```bash
python -m pytest tests/ -q
```

Expected: all tests pass (7 new tests added: 3 in Task 2, 1 in Task 3, 1 in Task 4, 1 in Task 5, plus 1 `_run_main` helper method — net new test count = +6 test methods)

- [ ] **Step 6: Run lint**

```bash
make lint
```

Expected: no errors

- [ ] **Step 7: Update README test count**

In `README.md`, find the test count line and increment it by 6 (6 new test methods across Tasks 2–5; the 2 updated tests in Task 1 replace existing ones).

- [ ] **Step 8: Update GH issue #103**

Post a comment on https://github.com/cdevers/Blue-Pearmain/issues/103 summarising what was done, then close it.

- [ ] **Step 9: Final commit**

```bash
git add flickr/metadata_puller.py tests/test_core.py README.md
git commit -m "fix: metadata_puller treats FlickrError(404) as flickr_deleted (GH #103)

Closes #103

After _call was changed to raise FlickrError for HTTP 404 responses,
metadata_puller's guard (previously 'e.code == 1') would miss them.
Widen to (1, 404) so both the Flickr API 'photo not found' and the
HTTP 404 path are handled consistently.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

- [ ] **Step 10: Push to origin**

```bash
git push
```
