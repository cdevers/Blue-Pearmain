# Local Photos thumbnails + `local_duplicate` classifier — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix blank thumbnails for Apple Photos-only records in the duplicate reviewer, then add a `local_duplicate` classifier to surface same-fingerprint groups as a distinct, actionable category.

**Architecture:** Two independent parts. Part A: `derivative_path()` in `thumbnailer.py` now tries three candidate filesystem paths instead of one; the `/thumb/<id>` route gets a live fallback that calls `derivative_path()` at request time for Photos-only records with no stored path, and writes the result back to the DB. Part B: a new `_is_local_duplicate()` classifier slots into the deduplicator waterfall after `edit_pair`; UI wires it up with its own badge and section.

**Tech Stack:** Python 3, SQLite, Flask/Jinja2, pytest; no new dependencies.

**GH issue:** [#130](https://github.com/cdevers/Blue-Pearmain/issues/130)
**Spec:** `docs/superpowers/specs/2026-05-23-local-duplicate-thumbnails-design.md`

---

## Files

| File | Change |
|------|--------|
| `poller/thumbnailer.py:51-67` | Replace single-path `derivative_path` with 3-candidate version |
| `reviewer/app.py:1419-1453` | Add `uuid` to SELECT; insert live-fallback step 3 |
| `reviewer/app.py:475-481` | Add `local_duplicate` to ORDER BY CASE |
| `reviewer/app.py:569-604` | Add `local_duplicate` tuple to sections list |
| `reviewer/templates/duplicates.html:143` | Add `.badge-local_duplicate` CSS |
| `reviewer/templates/duplicates.html:308` | Add `local_duplicate` to button conditional |
| `poller/deduplicator.py` | Add `_is_local_duplicate`; update waterfall, counts dict, print report |
| `tests/test_thumbnailer.py` | Create; 4 new tests for `derivative_path` |
| `tests/test_review_ui.py` | 1 new test for thumb live fallback |
| `tests/test_deduplicator.py` | 6 new tests for `_is_local_duplicate` + classify |
| `README.md` | Update test count (1089 → 1100) |
| `docs/testing.md` | Coverage inventory update |

---

## Task 1: Fix `derivative_path` in `thumbnailer.py`

**Files:**
- Create: `tests/test_thumbnailer.py`
- Modify: `poller/thumbnailer.py:51-67`

- [ ] **Step 1.1: Verify baseline**

```bash
python -m pytest tests/ -q 2>&1 | tail -1
```
Expected: `1089 passed`

- [ ] **Step 1.2: Create `tests/test_thumbnailer.py` with 4 failing tests**

```python
"""
tests/test_thumbnailer.py — unit tests for derivative_path()
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from poller.thumbnailer import derivative_path


class TestDerivativePath:
    def test_masters_path(self, tmp_path):
        """derivative found at resources/derivatives/masters/{shard}/"""
        uuid = "AAAA1234-0000-0000-0000-000000000000"
        shard = "a"
        d = tmp_path / "resources" / "derivatives" / "masters" / shard
        d.mkdir(parents=True)
        deriv = d / f"{uuid}_4_5005_c.jpeg"
        deriv.write_bytes(b"fake-jpeg")

        result = derivative_path(uuid, str(tmp_path))

        assert result == str(deriv)

    def test_shard_path_when_masters_missing(self, tmp_path):
        """masters/ missing → falls back to resources/derivatives/{shard}/"""
        uuid = "BBBB1234-0000-0000-0000-000000000000"
        shard = "b"
        d = tmp_path / "resources" / "derivatives" / shard
        d.mkdir(parents=True)
        deriv = d / f"{uuid}_1_105_c.jpeg"
        deriv.write_bytes(b"fake-jpeg")

        result = derivative_path(uuid, str(tmp_path))

        assert result == str(deriv)

    def test_momentshared_path_when_others_missing(self, tmp_path):
        """masters/ and shard/ missing → falls back to scopes/momentshared/"""
        uuid = "CCCC1234-0000-0000-0000-000000000000"
        shard = "c"
        d = (
            tmp_path
            / "scopes"
            / "momentshared"
            / "resources"
            / "derivatives"
            / "masters"
            / shard
        )
        d.mkdir(parents=True)
        deriv = d / f"{uuid}_4_5005_c.jpeg"
        deriv.write_bytes(b"fake-jpeg")

        result = derivative_path(uuid, str(tmp_path))

        assert result == str(deriv)

    def test_returns_none_when_no_candidate_exists(self, tmp_path):
        """No derivative file present → returns None."""
        uuid = "DDDD1234-0000-0000-0000-000000000000"

        result = derivative_path(uuid, str(tmp_path))

        assert result is None
```

- [ ] **Step 1.3: Run to confirm all 4 fail**

```bash
python -m pytest tests/test_thumbnailer.py -v
```
Expected: 4 failures — `derivative_path` only checks masters path, not shard or momentshared.

- [ ] **Step 1.4: Replace `derivative_path` in `poller/thumbnailer.py`**

Locate the existing `derivative_path` function (lines 51–67) and replace it entirely:

```python
def derivative_path(uuid: str, library_path: str) -> str | None:
    """
    Return the path to a Photos pre-generated JPEG derivative for this UUID,
    or None if no derivative is found on disk.

    Tries three candidate locations in order:
      1. resources/derivatives/masters/{shard}/{uuid}_4_5005_c.jpeg
         — standard card-import derivative
      2. resources/derivatives/{shard}/{uuid}_1_105_c.jpeg
         — smaller derivative used for some import paths
      3. scopes/momentshared/resources/derivatives/masters/{shard}/{uuid}_4_5005_c.jpeg
         — Shared Moments scope
    """
    if not uuid:
        return None
    shard = uuid[0].lower()
    lib = Path(library_path)
    candidates = [
        lib / "resources" / "derivatives" / "masters" / shard / f"{uuid}_4_5005_c.jpeg",
        lib / "resources" / "derivatives" / shard / f"{uuid}_1_105_c.jpeg",
        lib
        / "scopes"
        / "momentshared"
        / "resources"
        / "derivatives"
        / "masters"
        / shard
        / f"{uuid}_4_5005_c.jpeg",
    ]
    for p in candidates:
        if p.exists():
            return str(p)
    return None
```

- [ ] **Step 1.5: Run the new tests**

```bash
python -m pytest tests/test_thumbnailer.py -v
```
Expected: 4 passed.

- [ ] **Step 1.6: Run full suite**

```bash
python -m pytest tests/ -q
```
Expected: 1093 passed (1089 + 4 new).

- [ ] **Step 1.7: Commit**

```bash
git add poller/thumbnailer.py tests/test_thumbnailer.py
git commit -m "fix: derivative_path tries 3 candidate paths for Photos thumbnails (#130)

Adds shard/ and scopes/momentshared/ fallback paths so Photos-only
records whose derivatives aren't in the masters/ tree get
thumbnail_path populated by the thumbnailer.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 2: Live fallback in `/thumb/<id>` route

**Files:**
- Modify: `reviewer/app.py:1419-1453` (the `thumb` function)
- Modify: `tests/test_review_ui.py` (add one test)

- [ ] **Step 2.1: Add two failing tests to `tests/test_review_ui.py`**

Add these two standalone test functions (not inside any class) near the bottom of the file, before the final blank line:

```python
# ---------------------------------------------------------------------------
# Thumb route — live derivative fallback
# ---------------------------------------------------------------------------


def _make_thumb_test_db(tmp_path, uuid):
    """Helper: create an isolated DB with one Photos-only record."""
    test_db = Database(Path(tmp_path) / "thumb_test.db")
    test_db.upsert_photo(
        {
            "uuid": uuid,
            "original_filename": "IMG_0001.JPG",
            "privacy_state": "candidate_public",
            "apple_persons": [],
            "apple_labels": [],
        }
    )
    photo_id = test_db.conn.execute(
        "SELECT id FROM photos WHERE uuid = ?", (uuid,)
    ).fetchone()["id"]
    return test_db, photo_id


def test_thumb_live_fallback_writes_thumbnail_path(tmp_path):
    """
    Photo with uuid but no thumbnail_path: if a derivative file exists in the
    Photos library, /thumb/<id> serves it and writes thumbnail_path to the DB.
    """
    import reviewer.app as _app

    uuid = "FFFF1234-0000-0000-0000-000000000000"
    shard = "f"

    # Create a minimal JPEG derivative on disk
    deriv_dir = tmp_path / "resources" / "derivatives" / "masters" / shard
    deriv_dir.mkdir(parents=True)
    deriv = deriv_dir / f"{uuid}_4_5005_c.jpeg"
    # Minimal valid JPEG bytes (SOI marker + EOI marker)
    deriv.write_bytes(b"\xff\xd8\xff\xd9")

    test_db, photo_id = _make_thumb_test_db(tmp_path, uuid)

    old_db = _app._db
    old_config = _app._config.copy()
    _app._db = test_db
    _app._config = {"photos_library": {"path": str(tmp_path)}}
    _app.app.config["TESTING"] = True
    _app.app.config["SECRET_KEY"] = "test-secret"

    try:
        with _app.app.test_client() as c:
            resp = c.get(f"/thumb/{photo_id}")
        assert resp.status_code == 200
        assert resp.content_type == "image/jpeg"
        # thumbnail_path written back to DB
        row = test_db.conn.execute(
            "SELECT thumbnail_path FROM photos WHERE id = ?", (photo_id,)
        ).fetchone()
        assert row["thumbnail_path"] == str(deriv)
    finally:
        _app._db = old_db
        _app._config = old_config


def test_thumb_live_fallback_writes_sentinel_on_miss(tmp_path):
    """
    Photo with uuid but no thumbnail_path and no derivative on disk:
    /thumb/<id> writes the '__none__' sentinel so future requests skip
    the filesystem probe entirely.
    """
    import reviewer.app as _app

    uuid = "EEEE1234-0000-0000-0000-000000000000"

    # No derivative created on disk — tmp_path is an empty Photos library.
    test_db, photo_id = _make_thumb_test_db(tmp_path, uuid)

    old_db = _app._db
    old_config = _app._config.copy()
    _app._db = test_db
    _app._config = {"photos_library": {"path": str(tmp_path)}}
    _app.app.config["TESTING"] = True
    _app.app.config["SECRET_KEY"] = "test-secret"

    try:
        with _app.app.test_client() as c:
            resp = c.get(f"/thumb/{photo_id}")
        # Falls through to placeholder SVG (no derivative, no Flickr metadata)
        assert resp.status_code == 200
        # Sentinel written to DB so future requests skip probing
        row = test_db.conn.execute(
            "SELECT thumbnail_path FROM photos WHERE id = ?", (photo_id,)
        ).fetchone()
        assert row["thumbnail_path"] == "__none__"
    finally:
        _app._db = old_db
        _app._config = old_config
```

- [ ] **Step 2.2: Run to confirm both tests fail**

```bash
python -m pytest tests/test_review_ui.py::test_thumb_live_fallback_writes_thumbnail_path tests/test_review_ui.py::test_thumb_live_fallback_writes_sentinel_on_miss -v
```
Expected: `FAILED` — thumb route doesn't yet do a live lookup.

- [ ] **Step 2.3: Update the `thumb` function in `reviewer/app.py`**

Replace the entire `thumb` function (lines 1410–1453):

```python
# Sentinel written to thumbnail_path when no derivative exists on disk.
# Prevents repeated filesystem probing for permanently-missing derivatives.
# Clear this value manually to force re-probing (e.g. after Photos regenerates
# derivatives for an import).
_SENTINEL_NO_DERIVATIVE = "__none__"


@app.route("/thumb/<int:photo_id>")
def thumb(photo_id: int) -> ResponseReturnValue:
    """
    Serve a thumbnail. Priority order:
      1. Stored URL (redirect to CDN)
      2. Local file (thumbnail_path on disk)
      3. Live derivative lookup (uuid → Photos library):
         - Hit: writes real path to DB, serves file.
         - Miss: writes '__none__' sentinel to DB so future requests
           skip filesystem probing without probing all three paths again.
      4. Flickr URL constructed on the fly from flickr_id/secret/server
      5. Placeholder SVG
    """
    row = (
        db()
        .conn.execute(
            "SELECT thumbnail_path, flickr_id, flickr_secret, flickr_server, uuid"
            " FROM photos WHERE id = ?",
            (photo_id,),
        )
        .fetchone()
    )

    if not row:
        return _placeholder_svg("no preview")

    path = row["thumbnail_path"] or ""

    # 1. Stored URL — redirect to CDN
    if path.startswith("http"):
        return redirect(path)

    # 2. Local file (skip sentinel value)
    if path and path != _SENTINEL_NO_DERIVATIVE:
        p = Path(path)
        if p.exists():
            return send_file(str(p), mimetype="image/jpeg")

    # 3. Live derivative lookup from Photos library.
    #    Skipped when path == _SENTINEL_NO_DERIVATIVE (known miss).
    uuid = row["uuid"] or ""
    if uuid and path != _SENTINEL_NO_DERIVATIVE:
        try:
            library_path = str(
                Path(_config.get("photos_library", {}).get("path", "")).expanduser()
            )
            if library_path and library_path != ".":
                from poller.thumbnailer import derivative_path as _derivative_path

                deriv = _derivative_path(uuid, library_path)
                if deriv:
                    db().conn.execute(
                        "UPDATE photos SET thumbnail_path = ? WHERE id = ?",
                        (deriv, photo_id),
                    )
                    db().conn.commit()
                    return send_file(deriv, mimetype="image/jpeg")
                else:
                    # Write sentinel: no derivative found; skip probing next time.
                    db().conn.execute(
                        "UPDATE photos SET thumbnail_path = ? WHERE id = ?",
                        (_SENTINEL_NO_DERIVATIVE, photo_id),
                    )
                    db().conn.commit()
        except OSError:
            pass  # Photos library inaccessible; fall through to Flickr/placeholder

    # 4. Construct Flickr URL on the fly if we have the pieces
    flickr_id = row["flickr_id"] or ""
    secret = row["flickr_secret"] or ""
    server = row["flickr_server"] or ""
    if flickr_id and secret and server:
        url = f"https://live.staticflickr.com/{server}/{flickr_id}_{secret}_b.jpg"
        return redirect(url)

    # 5. Placeholder
    label = "no preview"
    return _placeholder_svg(label)
```

- [ ] **Step 2.4: Run both new tests**

```bash
python -m pytest tests/test_review_ui.py::test_thumb_live_fallback_writes_thumbnail_path tests/test_review_ui.py::test_thumb_live_fallback_writes_sentinel_on_miss -v
```
Expected: both `PASSED`.

- [ ] **Step 2.5: Run full suite**

```bash
python -m pytest tests/ -q
```
Expected: 1095 passed (1093 + 2 new).

- [ ] **Step 2.6: Commit**

```bash
git add reviewer/app.py tests/test_review_ui.py
git commit -m "feat: /thumb/<id> live fallback for Photos-only records (#130)

If thumbnail_path is empty and uuid is present, derive the path from
the Photos library at request time and write it back to the DB.
On miss, writes '__none__' sentinel to skip future filesystem probing.
Wrap probe in OSError guard so a bad mount never crashes the thumb route.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 3: Backfill `thumbnail_path` on the live database

This is a one-time operational step. It populates `thumbnail_path` for the ~218 Photos-only records in uncertain groups (and ~300 more across the rest of the DB) that the old `derivative_path` missed.

- [ ] **Step 3.1: Run thumbnailer dry-run to confirm scope**

```bash
python poller/thumbnailer.py --config config/config.yml --limit 1000 2>&1 | grep -E "Found|local|skipped"
```
Expected: reports how many records need thumbnails.

- [ ] **Step 3.2: Run thumbnailer to backfill**

```bash
python poller/thumbnailer.py --config config/config.yml
```
Expected: significant number of `local_count` hits from the newly-reachable derivative paths.

- [ ] **Step 3.3: Verify improvement**

```bash
sqlite3 data/curator.db "
SELECT COUNT(*) AS still_missing
FROM photos
WHERE uuid IS NOT NULL
  AND flickr_id IS NULL
  AND (thumbnail_path IS NULL OR thumbnail_path = '');
"
```
Expected: noticeably lower than 521 (the pre-fix count). Some photos may still be missing if their derivatives haven't been generated by Photos yet — the live fallback in Task 2 will handle those at request time.

---

## Task 4: `_is_local_duplicate` classifier

**Files:**
- Modify: `tests/test_deduplicator.py` (add 2 new test classes, 6 tests total)
- Modify: `poller/deduplicator.py` (new function; update waterfall, counts dict, print report)

- [ ] **Step 4.1: Write 6 failing tests**

Add the following two new test classes at the end of `tests/test_deduplicator.py` (after `TestPruneStaleGroups`):

```python
# ---------------------------------------------------------------------------
# _is_local_duplicate
# ---------------------------------------------------------------------------


class TestIsLocalDuplicate(unittest.TestCase):
    def test_same_fingerprint_two_photos(self):
        from poller.deduplicator import _is_local_duplicate

        a = make_photo(id=1, fingerprint="FP-SAME")
        b = make_photo(id=2, fingerprint="FP-SAME")
        self.assertTrue(_is_local_duplicate([a, b]))

    def test_different_fingerprints(self):
        from poller.deduplicator import _is_local_duplicate

        a = make_photo(id=1, fingerprint="FP-A")
        b = make_photo(id=2, fingerprint="FP-B")
        self.assertFalse(_is_local_duplicate([a, b]))

    def test_missing_fingerprint(self):
        from poller.deduplicator import _is_local_duplicate

        a = make_photo(id=1, fingerprint="FP-A")
        b = make_photo(id=2, fingerprint=None)
        self.assertFalse(_is_local_duplicate([a, b]))

    def test_single_photo(self):
        from poller.deduplicator import _is_local_duplicate

        a = make_photo(id=1, fingerprint="FP-A")
        self.assertFalse(_is_local_duplicate([a]))


# ---------------------------------------------------------------------------
# _classify_group — local_duplicate cases
# ---------------------------------------------------------------------------


class TestClassifyGroupLocalDuplicate(unittest.TestCase):
    def _pair(self):
        a = make_photo(
            id=1,
            original_filename="DSC_0001.JPG",
            uuid="UUID-A",
            fingerprint="FP-SAME",
            width=6048,
            height=4024,
        )
        b = make_photo(
            id=2,
            original_filename="DSC_0001.JPG",
            uuid="UUID-B",
            fingerprint="FP-SAME",
            width=6048,
            height=4024,
        )
        return [a, b]

    def test_local_duplicate_classification(self):
        group = _classify_group(self._pair())
        self.assertEqual(group.group_type, "local_duplicate")

    def test_local_duplicate_all_photos_in_review_not_discards(self):
        group = _classify_group(self._pair())
        self.assertEqual(len(group.discards), 0)
        self.assertEqual(len(group.review), 2)


class TestLocalDuplicateWaterfallInvariant(unittest.TestCase):
    def test_local_duplicate_beats_device_upload(self):
        """
        Same-fingerprint group with a capture-time gap > 5 min must classify as
        local_duplicate, not device_upload. Verifies waterfall ordering: step 3
        (_is_local_duplicate) fires before step 4 (gap > 5 min check).

        Note: make_photo must support a 'capture_date' parameter (datetime) for
        this test. If it doesn't yet, add it — the deduplicator's device_upload
        classifier reads the capture_date gap, so the field must be settable.
        """
        from datetime import datetime

        a = make_photo(
            id=1,
            original_filename="IMG_0001.HEIC",
            fingerprint="FP-SAME",
            width=3024,
            height=4032,
            capture_date=datetime(2024, 1, 1, 0, 0, 0),
        )
        b = make_photo(
            id=2,
            original_filename="IMG_0001.HEIC",
            fingerprint="FP-SAME",
            width=3024,
            height=4032,
            capture_date=datetime(2024, 1, 1, 2, 0, 0),  # 2-hour gap → device_upload without guard
        )
        group = _classify_group([a, b])
        # Must be local_duplicate, not device_upload
        self.assertEqual(group.group_type, "local_duplicate")
        self.assertNotEqual(group.group_type, "device_upload")
```

- [ ] **Step 4.2: Run to confirm all 7 fail**

```bash
python -m pytest tests/test_deduplicator.py::TestIsLocalDuplicate tests/test_deduplicator.py::TestClassifyGroupLocalDuplicate tests/test_deduplicator.py::TestLocalDuplicateWaterfallInvariant -v
```
Expected: all 7 fail — `_is_local_duplicate` doesn't exist yet (ImportError or NameError).

- [ ] **Step 4.3: Add `_is_local_duplicate` to `poller/deduplicator.py`**

Insert the new function immediately after `_is_edit_pair` (around line 250):

```python
def _is_local_duplicate(photos: list[PhotoRow]) -> bool:
    """
    True if all photos in the group share the same non-null fingerprint.

    This pattern indicates the same image was imported into Apple Photos
    multiple times (e.g., card import + iCloud sync, each producing a
    separate UUID record for identical file content). One copy is typically
    matched to a Flickr record; the others were never uploaded.

    **Semantic commitment:** fingerprint equality is treated as authoritative
    evidence of content identity. Two Photos records with the same fingerprint
    are the same image regardless of UUID, filename, or timestamp. Groups with
    any null or differing fingerprint are not classified here — they route to
    device_upload, not_duplicate, or uncertain instead.
    """
    if len(photos) < 2:
        return False
    fingerprints = {p.fingerprint for p in photos if p.fingerprint}
    if len(fingerprints) != 1:
        return False  # missing or differing fingerprints
    return True
```

- [ ] **Step 4.4: Insert `_is_local_duplicate` into `_classify_group` waterfall**

In `_classify_group`, locate the `if _is_edit_pair(photos):` block and its `return` statement. Immediately after that `return`, insert:

```python
    if _is_local_duplicate(photos):
        fp = next(p.fingerprint for p in photos if p.fingerprint) or ""
        notes = (
            f"Local duplicate: {len(photos)} copies share fingerprint {fp[:12]}… "
            f"— same image imported multiple times into Apple Photos"
        )
        return DuplicateGroup(match_key, "local_duplicate", photos, None, [], photos, notes)
```

- [ ] **Step 4.5: Add `local_duplicate` to `_write_groups` counts dict**

Find the `counts` dict initialisation in `_write_groups`. Add the new key:

```python
    counts: dict[str, int] = {
        "snapbridge": 0,
        "edit_pair": 0,
        "local_duplicate": 0,
        "device_upload": 0,
        "uncertain": 0,
        "not_duplicate": 0,
    }
```

- [ ] **Step 4.6: Add `local_duplicate` to `_print_report`**

Find:
```python
    for gtype in ("snapbridge", "edit_pair", "device_upload", "uncertain"):
```
Replace with:
```python
    for gtype in ("snapbridge", "edit_pair", "local_duplicate", "device_upload", "uncertain"):
```

- [ ] **Step 4.7: Run the 7 new tests**

```bash
python -m pytest tests/test_deduplicator.py::TestIsLocalDuplicate tests/test_deduplicator.py::TestClassifyGroupLocalDuplicate tests/test_deduplicator.py::TestLocalDuplicateWaterfallInvariant -v
```
Expected: all 7 passed.

- [ ] **Step 4.8: Run full suite**

```bash
python -m pytest tests/ -q
```
Expected: 1102 passed (1095 + 7 new).

- [ ] **Step 4.9: Commit**

```bash
git add poller/deduplicator.py tests/test_deduplicator.py
git commit -m "feat: local_duplicate classifier for same-fingerprint groups (#130)

_is_local_duplicate fires when all members share the same non-null
fingerprint — the same image imported into Apple Photos multiple times.
Fingerprint equality treated as authoritative content identity.
All photos placed in review; no keeper/discard assigned.
Waterfall invariant: local_duplicate takes precedence over device_upload.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 5: `local_duplicate` UI — app.py and template

**Files:**
- Modify: `reviewer/app.py` (ORDER BY, sections list)
- Modify: `reviewer/templates/duplicates.html` (badge CSS, button conditional)

No new tests — UI is exercised by existing smoke tests.

- [ ] **Step 5.1: Update ORDER BY CASE in `reviewer/app.py`**

Find (around line 475):
```python
                CASE dg.group_type
                    WHEN 'snapbridge'    THEN 0
                    WHEN 'edit_pair'     THEN 1
                    WHEN 'device_upload' THEN 2
                    ELSE 3
                END,
```
Replace with:
```python
                CASE dg.group_type
                    WHEN 'snapbridge'      THEN 0
                    WHEN 'edit_pair'       THEN 1
                    WHEN 'local_duplicate' THEN 2
                    WHEN 'device_upload'   THEN 3
                    ELSE 4
                END,
```

- [ ] **Step 5.2: Add `local_duplicate` to the sections list in `reviewer/app.py`**

Find the `for gtype, label, description in (` loop (around line 570). Insert the new tuple between the `edit_pair` entry and the `device_upload` entry:

```python
        (
            "local_duplicate",
            "Local duplicate",
            "Same image imported multiple times into your Photos library. "
            "One copy is already on Flickr; the others were never uploaded. "
            "Use ‘Not a duplicate’ to dismiss from review.",
        ),
```

- [ ] **Step 5.3: Add badge CSS to `reviewer/templates/duplicates.html`**

Find line 143:
```css
.badge-edit_pair          { background: #4a2800; color: #f5a623; }
```
Add immediately after:
```css
.badge-local_duplicate    { background: #1e1030; color: #c084fc; }
```

- [ ] **Step 5.4: Add `local_duplicate` to the action-button conditional**

Find (around line 308):
```html
        {% if section.type in ('snapbridge', 'edit_pair', 'device_upload', 'reupload') %}
```
Replace with:
```html
        {% if section.type in ('snapbridge', 'edit_pair', 'local_duplicate', 'device_upload', 'reupload') %}
```

- [ ] **Step 5.5: Run full suite**

```bash
python -m pytest tests/ -q
```
Expected: 1100 passed (unchanged — UI edits add no tests).

- [ ] **Step 5.6: Commit**

```bash
git add reviewer/app.py reviewer/templates/duplicates.html
git commit -m "feat: local_duplicate section in /duplicates UI (#130)

ORDER BY slot, sections list entry, muted-purple badge, and action-button
support for the new local_duplicate group type.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 6: Run `--write` to reclassify live groups, then docs and close

- [ ] **Step 6.1: Re-run `--write` to reclassify uncertain groups as local_duplicate**

```bash
python -m poller.deduplicator --config config/config.yml --write
```
Expected: output now shows a non-zero `local_duplicate` count. Previously-uncertain groups with same fingerprints are reclassified.

- [ ] **Step 6.2: Verify reclassification**

```bash
sqlite3 data/curator.db "
SELECT group_type, resolved, COUNT(*) AS n
FROM duplicate_groups
GROUP BY group_type, resolved
ORDER BY group_type, resolved;
"
```
Expected: a `local_duplicate|0|...` row now appears. `uncertain|0|...` count should be lower than 346.

- [ ] **Step 6.3: Update README test count**

```bash
python -m pytest tests/ -q 2>&1 | tail -1
```

In `README.md`, update both occurrences of the test count (`1089` → `1102`). Also append to the coverage sentence: `local Photos derivative thumbnails (multi-path fallback + live reviewer lookup with negative-miss sentinel), and local_duplicate classifier (same-fingerprint Apple Photos imports).`

- [ ] **Step 6.4: Update `docs/testing.md`**

Add to the **Deduplication** section:
```
- `local_duplicate` classifier: same-fingerprint groups (same image imported multiple times); all in review, no keeper
```

Add a new **Thumbnail serving** section:
```
## Thumbnail serving

- `derivative_path`: tries masters/, shard/, and scopes/momentshared/ candidate paths; returns first that exists on disk
- `/thumb/<id>` live fallback: Photos-only records with no `thumbnail_path` resolved at request time via `derivative_path`; path written back to DB on hit
```

- [ ] **Step 6.5: Mark spec done**

In `docs/superpowers/specs/2026-05-23-local-duplicate-thumbnails-design.md`, change:
```
**Status:** Approved — ready for implementation plan
```
to:
```
**Status:** ✓ Done — implemented in commits on 2026-05-23
```

- [ ] **Step 6.6: Commit docs**

```bash
git add README.md docs/testing.md docs/superpowers/specs/2026-05-23-local-duplicate-thumbnails-design.md
git commit -m "docs: update test count and coverage for #130

1100 tests. Adds local_duplicate classifier and thumbnail derivative
path coverage to docs/testing.md.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

- [ ] **Step 6.7: Close the GitHub issue**

```bash
gh issue close 130 --comment "Implemented in this session:
- derivative_path now tries 3 candidate paths (masters/, shard/, momentshared/)
- /thumb/<id> live fallback for Photos-only records; writes path back to DB on hit
- '__none__' sentinel written on miss to skip future filesystem probing
- OSError guard so Photos library inaccessibility never crashes the thumb route
- Thumbnailer backfill run to populate existing missing records
- _is_local_duplicate classifier: same fingerprint across all members (fingerprint equality treated as authoritative content identity)
- Waterfall invariant: local_duplicate fires before device_upload
- local_duplicate UI section (muted-purple badge, confirm + not-a-duplicate actions)
- --write re-run reclassified same-fingerprint uncertain groups
- 13 new tests (1089 → 1102)"
```

- [ ] **Step 6.8: Push branch to origin**

```bash
git push origin feature/130-local-duplicate-thumbnails
```
