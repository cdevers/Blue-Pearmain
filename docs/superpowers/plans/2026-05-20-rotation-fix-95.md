# Rotation double-apply + stale grid thumbnail fix (GH #95) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix two rotation bugs: (1) rotating 90° sometimes shows 180° because the CDN URL already serves the rotated image but CSS rotates it again; (2) the review grid shows stale thumbnails after rotation because browser caches the old `/thumb/` URL.

**Architecture:** Two independent fixes. Fix 1 changes `api_rotate_flickr` in `reviewer/app.py` to set `display_rotation=0` when Flickr returns a refreshed secret (CDN already correct), and updates the JS to force a fresh image fetch. Fix 2 adds `updated_at` to thumbnail URLs as a cache-busting query param; requires adding `updated_at` to two SQL queries that omit it.

**Tech Stack:** Python/Flask (backend route), Jinja2 (templates), vanilla JS (frontend), SQLite (DB via `db/db.py`), pytest (tests in `tests/test_core.py`).

---

## File map

| File | Change |
|------|--------|
| `reviewer/app.py` | `api_rotate_flickr`: conditional `display_rotation`; screenshot SELECT adds `updated_at` |
| `db/db.py` | `review_queue()` SELECT adds `updated_at` |
| `reviewer/templates/photo.html` | `rotateFlickr()` JS: force img src refresh; initial `<img>` adds `v=` param |
| `reviewer/templates/review.html` | Grid `<img>` adds `v=` cache-bust param |
| `tests/test_core.py` | Three new tests in `TestRotateFlickrApi` |

---

### Task 1: Tests — `display_rotation` conditional on `get_photo_info` success/failure

**Context:** `TestRotateFlickrApi` (line 7808 of `tests/test_core.py`) already has a `setUp` that creates a test DB and Flask test client, and a `_post(photo_id, degrees)` helper. All new tests go in this class. The existing `test_rotate_calls_client_and_returns_ok` mocks both `client.rotate` and `client.get_photo_info` but does not assert on `display_rotation`.

**Files:**
- Modify: `tests/test_core.py` (after line 7896, inside `TestRotateFlickrApi`)

- [ ] **Step 1: Write two failing tests**

Add after `test_rotate_calls_client_and_returns_ok` (after line 7896):

```python
def test_rotate_clears_display_rotation_when_info_refreshes(self):
    """When get_photo_info returns a new secret, display_rotation must be 0."""
    from unittest.mock import MagicMock
    import reviewer.app as reviewer_app

    mock_c = MagicMock()
    mock_c.rotate.return_value = {"stat": "ok"}
    mock_c.get_photo_info.return_value = {
        "photo": {"secret": "newsecret999", "server": "65535"}
    }
    reviewer_app._client = mock_c
    try:
        resp = self._post(self.photo_id, 90)
        self.assertEqual(resp.status_code, 200)
        d = resp.get_json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["display_rotation"], 0)
        row = self._db.conn.execute(
            "SELECT display_rotation FROM photos WHERE id = ?", (self.photo_id,)
        ).fetchone()
        self.assertEqual(row["display_rotation"], 0)
    finally:
        reviewer_app._client = None

def test_rotate_keeps_display_rotation_when_info_fails(self):
    """When get_photo_info raises FlickrError, display_rotation must equal degrees."""
    from unittest.mock import MagicMock
    from flickr.flickr_client import FlickrError
    import reviewer.app as reviewer_app

    mock_c = MagicMock()
    mock_c.rotate.return_value = {"stat": "ok"}
    mock_c.get_photo_info.side_effect = FlickrError(1, "not found")
    reviewer_app._client = mock_c
    try:
        resp = self._post(self.photo_id, 90)
        self.assertEqual(resp.status_code, 200)
        d = resp.get_json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["display_rotation"], 90)
        row = self._db.conn.execute(
            "SELECT display_rotation FROM photos WHERE id = ?", (self.photo_id,)
        ).fetchone()
        self.assertEqual(row["display_rotation"], 90)
    finally:
        reviewer_app._client = None
```

- [ ] **Step 2: Run tests to confirm they fail**

```
python -m pytest tests/test_core.py::TestRotateFlickrApi::test_rotate_clears_display_rotation_when_info_refreshes tests/test_core.py::TestRotateFlickrApi::test_rotate_keeps_display_rotation_when_info_fails -v
```

Expected: both FAIL (currently `display_rotation` is always `(0 + degrees) % 360 = degrees`, not 0).

---

### Task 2: Backend — `api_rotate_flickr` conditional `display_rotation`

**Context:** `api_rotate_flickr` is in `reviewer/app.py` at line 1148. The block to change is lines 1169–1206. Currently it computes `new_rotation = (current + degrees) % 360` unconditionally, then fetches `get_photo_info` to update the secret. The fix: compute `new_rotation` *after* the `get_photo_info` attempt, setting it to `0` when a fresh secret was obtained.

**Files:**
- Modify: `reviewer/app.py:1169-1206`

- [ ] **Step 3: Replace the rotation logic block**

Replace lines 1169–1206 (from the comment through `return jsonify(...)`) with:

```python
    # display_rotation is a temporary CSS correction for the stale-thumbnail
    # window. If get_photo_info returns a fresh secret, the /thumb/ route will
    # redirect to the post-rotation CDN URL directly — no CSS correction needed.
    # Only set it when we can't refresh the secret and the CDN URL is still stale.
    current = photo.get("display_rotation") or 0

    new_secret = photo.get("flickr_secret") or ""
    new_server = photo.get("flickr_server") or ""
    info_refreshed = False
    try:
        info = c.get_photo_info(photo["flickr_id"])
        p = info.get("photo", {})
        fetched_secret = p.get("secret")
        if fetched_secret:
            new_secret = fetched_secret
            new_server = p.get("server") or new_server
            info_refreshed = True
    except FlickrError:
        pass  # stale secret is better than crashing; thumbnailer will retry

    new_rotation = 0 if info_refreshed else (current + degrees) % 360

    db().conn.execute(
        """UPDATE photos
           SET display_rotation = ?,
               flickr_secret    = ?,
               flickr_server    = ?,
               thumbnail_path   = NULL,
               updated_at       = datetime('now')
           WHERE id = ?""",
        (new_rotation, new_secret, new_server, photo_id),
    )
    db().conn.commit()

    # Delete the stale local file (thumbnail_path already cleared above)
    old_path = photo.get("thumbnail_path") or ""
    if old_path and not old_path.startswith("http"):
        try:
            Path(old_path).unlink(missing_ok=True)
        except OSError:
            pass

    return jsonify({"ok": True, "display_rotation": new_rotation})
```

- [ ] **Step 4: Run the two new tests — both must pass**

```
python -m pytest tests/test_core.py::TestRotateFlickrApi::test_rotate_clears_display_rotation_when_info_refreshes tests/test_core.py::TestRotateFlickrApi::test_rotate_keeps_display_rotation_when_info_fails -v
```

Expected: both PASS.

- [ ] **Step 5: Run the full test suite**

```
python -m pytest tests/ -q
```

Expected: all tests pass (previously 811).

- [ ] **Step 6: Commit**

```bash
git add reviewer/app.py tests/test_core.py
git commit -m "fix: clear display_rotation when Flickr secret refreshes after rotate (GH #95)"
```

---

### Task 3: Test — `updated_at` present in `review_queue` results

**Context:** `review_queue()` in `db/db.py` uses a hand-written SELECT that lists specific columns. `updated_at` is currently absent. There is an existing `TestReviewQueue` or similar class — check by grepping. If none exists, add the test to a suitable DB test class.

**Files:**
- Modify: `tests/test_core.py`

- [ ] **Step 7: Find the right test class**

```
grep -n "class Test.*Queue\|class Test.*DB\|review_queue" tests/test_core.py | head -20
```

Note the class name and line number. Add the new test inside that class (or `TestDatabase` if it exists).

- [ ] **Step 8: Write the failing test**

```python
def test_review_queue_includes_updated_at(self):
    """review_queue() results must include updated_at so templates can cache-bust thumbnails."""
    # Insert a minimal photo with a known updated_at
    conn = self.db.conn  # adjust to match the class's db attribute name
    conn.execute(
        """INSERT INTO photos (uuid, original_filename, privacy_state, updated_at)
           VALUES ('uuid-updated-at-test', 'test.jpg', 'candidate_public', '2026-01-15 10:00:00')"""
    )
    conn.commit()
    rows = self.db.review_queue(states=["candidate_public"], limit=10)
    match = next((r for r in rows if r.get("uuid") == "uuid-updated-at-test"), None)
    self.assertIsNotNone(match, "seeded photo not found in review_queue results")
    self.assertIn("updated_at", match, "updated_at must be present in review_queue results")
    self.assertEqual(match["updated_at"], "2026-01-15 10:00:00")
```

> **Note:** The test class may use `self.db`, `self._db`, or `self.conn` — grep for `setUp` in the target class to find the attribute name. If there is no suitable class, add the test to a new `class TestReviewQueueColumns(unittest.TestCase)` with a minimal setUp that creates a `Database` in a `tempfile.TemporaryDirectory()`. Pattern:
>
> ```python
> class TestReviewQueueColumns(unittest.TestCase):
>     def setUp(self):
>         self._tmp = tempfile.TemporaryDirectory()
>         self.db = Database(Path(self._tmp.name) / "test.db")
>
>     def tearDown(self):
>         self.db.close()
>         self._tmp.cleanup()
> ```

- [ ] **Step 9: Run the test to confirm it fails**

```
python -m pytest tests/test_core.py::TestReviewQueueColumns::test_review_queue_includes_updated_at -v
```

(Replace the class name if you added it to an existing class.)

Expected: FAIL — `updated_at` key absent from result dict.

---

### Task 4: Backend — add `updated_at` to `review_queue()` and screenshot SELECT

**Context:** Two SQL queries omit `updated_at`:
1. `db/db.py` `review_queue()` at line 611 — lists columns explicitly.
2. `reviewer/app.py` screenshot query at line 202 — another explicit column list.

**Files:**
- Modify: `db/db.py:611-613`
- Modify: `reviewer/app.py:202-204`

- [ ] **Step 10: Add `updated_at` to `review_queue()` SELECT**

In `db/db.py`, change the SELECT inside `review_queue()`:

```python
# before
        rows = self.conn.execute(
            f"""SELECT id, uuid, flickr_id, original_filename,
                       apple_unknown_faces, apple_named_faces, proposed_tags,
                       display_rotation, is_screenshot

# after
        rows = self.conn.execute(
            f"""SELECT id, uuid, flickr_id, original_filename,
                       apple_unknown_faces, apple_named_faces, proposed_tags,
                       display_rotation, is_screenshot, updated_at
```

- [ ] **Step 11: Add `updated_at` to the screenshot SELECT in `app.py`**

In `reviewer/app.py`, change the screenshot branch SELECT:

```python
# before
                f"""SELECT id, flickr_id, original_filename,
                       apple_unknown_faces, apple_named_faces, proposed_tags,
                       display_rotation

# after
                f"""SELECT id, flickr_id, original_filename,
                       apple_unknown_faces, apple_named_faces, proposed_tags,
                       display_rotation, updated_at
```

- [ ] **Step 12: Run the `updated_at` test — must pass**

```
python -m pytest tests/test_core.py::TestReviewQueueColumns::test_review_queue_includes_updated_at -v
```

Expected: PASS.

- [ ] **Step 13: Run the full suite**

```
python -m pytest tests/ -q
```

Expected: all pass.

- [ ] **Step 14: Commit**

```bash
git add db/db.py reviewer/app.py tests/test_core.py
git commit -m "fix: add updated_at to review_queue and screenshot queries for cache-busting (GH #95)"
```

---

### Task 5: Templates — cache-bust thumbnail URLs + JS img src refresh

**Context:** Both `review.html` (grid) and `photo.html` (detail) render `<img src="{{ url_for('thumb', photo_id=photo.id) }}">`. Flask's `url_for` treats unknown kwargs as query params, so adding `v=photo.updated_at` produces `/thumb/1234?v=2026-05-20+12:34:56`. When `updated_at` changes on rotation, the URL changes and the browser fetches fresh.

In `photo.html`, after a rotation the JS must also update `img.src` to force a fresh fetch of the now-correct CDN redirect — otherwise the browser reuses the already-loaded URL.

**Files:**
- Modify: `reviewer/templates/review.html:266`
- Modify: `reviewer/templates/photo.html:212` (initial `<img>`)
- Modify: `reviewer/templates/photo.html:575-581` (`rotateFlickr()` JS)

- [ ] **Step 15: Add cache-bust param to review grid thumbnail**

In `reviewer/templates/review.html`, change line 266:

```html
<!-- before -->
      <img src="{{ url_for('thumb', photo_id=photo.id) }}"

<!-- after -->
      <img src="{{ url_for('thumb', photo_id=photo.id, v=photo.updated_at) }}"
```

- [ ] **Step 16: Add cache-bust param to photo detail initial image**

In `reviewer/templates/photo.html`, change line 212:

```html
<!-- before -->
    <img src="{{ url_for('thumb', photo_id=photo.id) }}"

<!-- after -->
    <img src="{{ url_for('thumb', photo_id=photo.id, v=photo.updated_at) }}"
```

- [ ] **Step 17: Update `rotateFlickr()` JS to force img refresh and handle `display_rotation=0`**

In `reviewer/templates/photo.html`, replace the `if (d.ok)` block in `rotateFlickr()` (lines 575–581):

```javascript
  if (d.ok) {
    const img = document.getElementById('main-image');
    if (img) {
      img.style.transform = d.display_rotation ? `rotate(${d.display_rotation}deg)` : '';
      img.src = `/thumb/{{ photo.id }}?r=${Date.now()}`;
    }
    toast(`Rotated ${degrees}° on Flickr`);
  }
```

- [ ] **Step 18: Run the full test suite**

```
python -m pytest tests/ -q
```

Expected: all tests pass (no template tests check these specific attrs, but nothing should break).

- [ ] **Step 19: Commit**

```bash
git add reviewer/templates/review.html reviewer/templates/photo.html
git commit -m "fix: cache-bust thumbnail URLs with updated_at; force img refresh after rotate (GH #95)"
```

---

### Task 6: Docs + README + close issue

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-05-20-rotation-fix-95-design.md`

- [ ] **Step 20: Update README test count**

Run `python -m pytest tests/ -q` and note the count. In `README.md`, find the line with the current test count (e.g. `811 tests`) and update it to match.

- [ ] **Step 21: Mark the spec done**

In `docs/superpowers/specs/2026-05-20-rotation-fix-95-design.md`, change the `**Status:**` line:

```markdown
**Status:** done
```

- [ ] **Step 22: Commit docs**

```bash
git add README.md docs/superpowers/specs/2026-05-20-rotation-fix-95-design.md
git commit -m "docs: update test count and mark GH #95 spec done"
```

- [ ] **Step 23: Push and close the issue**

```bash
git push
gh issue close 95 --comment "Fixed in this push. Two bugs resolved:
- display_rotation is now 0 after rotation when get_photo_info refreshes the Flickr secret (CDN URL already serves rotated image — no CSS correction needed)
- Thumbnail URLs in both grid and detail views include ?v=updated_at cache-bust param; rotateFlickr() JS also forces an immediate img.src refresh after rotation"
```
