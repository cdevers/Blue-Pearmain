# Duplicate Detection Bug Fixes — `edit_pair` + `--prune` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix Snapbridge mislabelling of iPhone edit pairs, add an `edit_pair` category, and clean up the 290 stale duplicate groups (zombie groups + orphaned photos).

**Architecture:** All classifier changes are in `poller/deduplicator.py`. UI changes are in `reviewer/app.py` and `reviewer/templates/duplicates.html`. Tests live in `tests/test_deduplicator.py`. Two operational `--write`/`--prune` runs against the live DB close out the backlog at the end.

**Tech Stack:** Python 3, SQLite, Flask/Jinja2, pytest; no new dependencies.

**GH issue:** [#129](https://github.com/cdevers/Blue-Pearmain/issues/129)
**Spec:** `docs/superpowers/specs/2026-05-23-dedup-bugs-edit-pair-prune-design.md`

---

## Files

| File | Change |
|------|--------|
| `poller/deduplicator.py` | Add DSC_ guard to `_is_snapbridge_pair`; add `_is_edit_pair`; update `_classify_group`; update `_write_groups` ON CONFLICT + counts; add `_prune_stale_groups`; update `_print_report`; update `main()` CLI |
| `reviewer/app.py` | Add `edit_pair` to ORDER BY, add section entry |
| `reviewer/templates/duplicates.html` | Add `.badge-edit_pair` CSS, add `edit_pair` to action button conditional |
| `tests/test_deduplicator.py` | 15 new tests across 4 new test classes |
| `README.md` | Update test count |
| `docs/testing.md` | Add `edit_pair` + `--prune` to coverage inventory |

---

## Task 1: DSC_ prefix guard on `_is_snapbridge_pair`

**Files:**
- Modify: `tests/test_deduplicator.py` (add to `TestIsSnapbridgePair`)
- Modify: `poller/deduplicator.py:192-217` (`_is_snapbridge_pair`)

- [ ] **Step 1.1: Verify the baseline**

```bash
python -m pytest tests/test_deduplicator.py -q
```
Expected: `62 passed`

- [ ] **Step 1.2: Write the failing test**

In `tests/test_deduplicator.py`, add to the `TestIsSnapbridgePair` class (after the last existing test method):

```python
def test_dsc_prefix_required(self):
    # IMG_* files must not be classified as Snapbridge even if fingerprints
    # differ and dimensions differ — Snapbridge is Nikon-only (DSC_* naming).
    a = make_photo(id=1, original_filename="IMG_3199.HEIC",
                   fingerprint="FP-A", width=3260, height=2059)
    b = make_photo(id=2, original_filename="IMG_3199.HEIC",
                   fingerprint="FP-B", width=4032, height=3024)
    self.assertFalse(_is_snapbridge_pair([a, b]))
```

- [ ] **Step 1.3: Run to confirm it fails**

```bash
python -m pytest tests/test_deduplicator.py::TestIsSnapbridgePair::test_dsc_prefix_required -v
```
Expected: `FAILED` — `_is_snapbridge_pair` returns `True` for IMG_* files before the fix.

- [ ] **Step 1.4: Add the DSC_ guard to `_is_snapbridge_pair`**

In `poller/deduplicator.py`, locate `_is_snapbridge_pair` (around line 192). Replace the function body with:

```python
def _is_snapbridge_pair(photos: list[PhotoRow]) -> bool:
    """
    True if exactly two photos match the Snapbridge low-res/high-res pattern:
    same filename + timestamp (guaranteed by the caller), different fingerprints
    (different file content), and — when available — different pixel dimensions.

    Requires both photos to have a DSC_-prefixed filename: Snapbridge is a
    Nikon-specific feature and only applies to files named DSC_*.  This is a
    pragmatic heuristic (see spec for caveats), not a semantic guarantee.

    Timing (date_added_photos) is intentionally NOT used here. Snapbridge
    previews sometimes arrive days or weeks after capture, and full-res card
    imports can be delayed by months. The reliable signals are fingerprint
    divergence (proves different files) and resolution difference (proves
    one is the low-res preview). If dimensions are not yet populated, we
    return False and let the group stay 'uncertain' until the scanner
    backfill provides them.
    """
    if len(photos) != 2:
        return False
    a, b = photos
    # Snapbridge only applies to Nikon camera files (DSC_* filename convention)
    if not all(
        p.original_filename and p.original_filename.upper().startswith("DSC_")
        for p in photos
    ):
        return False
    if not a.fingerprint or not b.fingerprint:
        return False
    if a.fingerprint == b.fingerprint:
        return False
    # Dimensions available: must differ to confirm low-res/high-res split
    if a.pixels is not None and b.pixels is not None:
        return a.pixels != b.pixels
    # Dimensions not yet populated — stay uncertain until scanner backfill runs
    return False
```

- [ ] **Step 1.5: Run all Snapbridge tests**

```bash
python -m pytest tests/test_deduplicator.py::TestIsSnapbridgePair -v
```
Expected: all 7 pass (6 original + 1 new).

- [ ] **Step 1.6: Run full suite to check for regressions**

```bash
python -m pytest tests/ -q
```
Expected: 1072 passed (same as baseline — no regressions).

- [ ] **Step 1.7: Commit**

```bash
git add poller/deduplicator.py tests/test_deduplicator.py
git commit -m "fix: _is_snapbridge_pair requires DSC_* filename prefix (#129)

Snapbridge is Nikon-specific; IMG_* edit pairs were incorrectly
classified as snapbridge. The DSC_ prefix guard narrows the classifier
to Nikon camera files only.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 2: `_is_edit_pair` function and `_classify_group` update

**Files:**
- Modify: `tests/test_deduplicator.py` (new classes `TestIsEditPair`, `TestClassifyGroupEditPair`)
- Modify: `poller/deduplicator.py` (new `_is_edit_pair`; update `_classify_group`, `_write_groups` counts, `_print_report`)

- [ ] **Step 2.1: Write failing tests**

Add the following two new test classes to `tests/test_deduplicator.py` (after `TestIsSnapbridgePair`):

```python
# ---------------------------------------------------------------------------
# _is_edit_pair
# ---------------------------------------------------------------------------


class TestIsEditPair(unittest.TestCase):
    def test_iphone_pair_different_fingerprints_different_dims(self):
        from poller.deduplicator import _is_edit_pair

        # Original IMG_*.HEIC + colour-corrected crop — should be edit_pair
        a = make_photo(id=1, original_filename="IMG_3199.HEIC",
                       fingerprint="FP-ORIG", width=3260, height=2059)
        b = make_photo(id=2, original_filename="IMG_3199.HEIC",
                       fingerprint="FP-EDIT", width=4032, height=3024)
        self.assertTrue(_is_edit_pair([a, b]))

    def test_dsc_files_excluded(self):
        from poller.deduplicator import _is_edit_pair

        # DSC_* files belong to _is_snapbridge_pair, not _is_edit_pair
        a = make_photo(id=1, original_filename="DSC_0001.JPG",
                       fingerprint="FP-LO", width=1620, height=1080)
        b = make_photo(id=2, original_filename="DSC_0001.JPG",
                       fingerprint="FP-HI", width=6048, height=4024)
        self.assertFalse(_is_edit_pair([a, b]))


# ---------------------------------------------------------------------------
# _classify_group — edit_pair cases
# ---------------------------------------------------------------------------


class TestClassifyGroupEditPair(unittest.TestCase):
    def _iphone_edit_pair(self):
        original = make_photo(
            id=1, original_filename="IMG_3199.HEIC",
            uuid="UUID-ORIG", fingerprint="FP-ORIG",
            width=3260, height=2059,
        )
        edited = make_photo(
            id=2, original_filename="IMG_3199.HEIC",
            uuid="UUID-EDIT", fingerprint="FP-EDIT",
            width=4032, height=3024,
        )
        return [original, edited]

    def test_edit_pair_classification(self):
        group = _classify_group(self._iphone_edit_pair())
        self.assertEqual(group.group_type, "edit_pair")

    def test_edit_pair_all_photos_in_review_not_discards(self):
        group = _classify_group(self._iphone_edit_pair())
        self.assertEqual(len(group.discards), 0)
        self.assertEqual(len(group.review), 2)

    def test_edit_pair_keeper_is_higher_res(self):
        group = _classify_group(self._iphone_edit_pair())
        self.assertIsNotNone(group.keeper)
        # edited (4032×3024 = 12,192,768 px) > original (3260×2059 = 6,712,340 px)
        self.assertEqual(group.keeper.width, 4032)
```

- [ ] **Step 2.2: Run to confirm they fail**

```bash
python -m pytest tests/test_deduplicator.py::TestIsEditPair tests/test_deduplicator.py::TestClassifyGroupEditPair -v
```
Expected: all 5 fail with `ImportError` or `AssertionError` — `_is_edit_pair` doesn't exist yet.

- [ ] **Step 2.3: Add `_is_edit_pair` to `poller/deduplicator.py`**

Insert the new function immediately after `_is_snapbridge_pair`:

```python
def _is_edit_pair(photos: list[PhotoRow]) -> bool:
    """
    True if exactly two non-DSC_ photos share filename+timestamp with different
    fingerprints and different pixel dimensions — likely an original + edited,
    cropped, or colour-corrected version.

    DSC_* files are handled exclusively by _is_snapbridge_pair.
    If dimensions are not yet populated, returns False (stays uncertain).
    """
    if len(photos) != 2:
        return False
    a, b = photos
    # DSC_* pairs belong to _is_snapbridge_pair
    if any(
        p.original_filename and p.original_filename.upper().startswith("DSC_")
        for p in photos
    ):
        return False
    if not a.fingerprint or not b.fingerprint:
        return False
    if a.fingerprint == b.fingerprint:
        return False
    if a.pixels is not None and b.pixels is not None:
        return a.pixels != b.pixels
    return False
```

- [ ] **Step 2.4: Update `_classify_group` to call `_is_edit_pair`**

In `_classify_group`, after the `_is_snapbridge_pair` block and before the `device_upload` check, insert:

```python
    if _is_edit_pair(photos):
        # Keeper = higher resolution (suggestion only — all photos go to review).
        # Typical action for the user is "Not a duplicate" to keep both.
        ranked = sorted(photos, key=lambda p: p.pixels or 0, reverse=True)
        keeper = ranked[0]
        notes = (
            f"Edit pair: {ranked[0].width}×{ranked[0].height}px "
            f"({ranked[0].uuid or ranked[0].flickr_id}) vs "
            f"{ranked[1].width}×{ranked[1].height}px "
            f"({ranked[1].uuid or ranked[1].flickr_id}) — "
            f"likely original + edited version; use 'Not a duplicate' to keep both"
        )
        return DuplicateGroup(match_key, "edit_pair", photos, keeper, [], photos, notes)
```

- [ ] **Step 2.5: Add `edit_pair` to `_write_groups` counts dict**

In `_write_groups`, find the `counts` dict initialisation and add the new key:

```python
    counts: dict[str, int] = {
        "snapbridge": 0,
        "edit_pair": 0,
        "device_upload": 0,
        "uncertain": 0,
        "not_duplicate": 0,
    }
```

- [ ] **Step 2.6: Add `edit_pair` to `_print_report`**

Find the line:
```python
    for gtype in ("snapbridge", "device_upload", "uncertain"):
```
Replace with:
```python
    for gtype in ("snapbridge", "edit_pair", "device_upload", "uncertain"):
```

- [ ] **Step 2.7: Run the new tests**

```bash
python -m pytest tests/test_deduplicator.py::TestIsEditPair tests/test_deduplicator.py::TestClassifyGroupEditPair -v
```
Expected: all 5 pass.

- [ ] **Step 2.8: Run full suite**

```bash
python -m pytest tests/ -q
```
Expected: 1077 passed (1072 + 5 new).

- [ ] **Step 2.9: Commit**

```bash
git add poller/deduplicator.py tests/test_deduplicator.py
git commit -m "feat: add edit_pair duplicate category for iPhone original+edit pairs (#129)

_is_edit_pair fires for non-DSC_* photos with different fingerprints
and different dimensions. All photos placed in review (no auto-delete).
Keeper is assigned by pixel count as a suggestion only.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 3: `edit_pair` UI — app.py and template

**Files:**
- Modify: `reviewer/app.py` (ORDER BY, sections list)
- Modify: `reviewer/templates/duplicates.html` (badge CSS, button conditional)

No new tests — UI is covered by existing `test_review_ui.py` smoke tests. Verify manually that `/duplicates` renders after the change.

- [ ] **Step 3.1: Update the ORDER BY in `reviewer/app.py`**

Find the `ORDER BY` clause in the `/duplicates` route query (around line 473). Replace:

```python
            ORDER BY
                CASE dg.group_type
                    WHEN 'snapbridge'    THEN 0
                    WHEN 'device_upload' THEN 1
                    ELSE 2
                END,
```

With:

```python
            ORDER BY
                CASE dg.group_type
                    WHEN 'snapbridge'    THEN 0
                    WHEN 'edit_pair'     THEN 1
                    WHEN 'device_upload' THEN 2
                    ELSE 3
                END,
```

- [ ] **Step 3.2: Add `edit_pair` to the sections list in `reviewer/app.py`**

Find the `sections = []` loop (around line 566). Insert the `edit_pair` entry between `snapbridge` and `device_upload`:

```python
    for gtype, label, description in (
        (
            "snapbridge",
            "Snapbridge",
            "Low-res phone preview vs. full-res card import — keeper is the higher-resolution copy",
        ),
        (
            "edit_pair",
            "Edit pair",
            "Same filename and timestamp, different content — typically an original and an edited, "
            "cropped, or colour-corrected version. Use ‘Not a duplicate’ if you want to keep both.",
        ),
        (
            "device_upload",
            "Device upload",
            "Same file uploaded from multiple devices — keeper is the earlier Flickr upload",
        ),
        ...  # rest of the list unchanged
```

(Keep all existing entries; only add the `edit_pair` tuple between `snapbridge` and `device_upload`.)

- [ ] **Step 3.3: Add the badge CSS to `reviewer/templates/duplicates.html`**

Find the existing badge definitions (around line 138):

```css
.badge-reupload_uncertain { background: #fd7e14; color: #fff; }
```

Add immediately after:

```css
.badge-edit_pair          { background: #4a2800; color: #f5a623; }
```

- [ ] **Step 3.4: Add `edit_pair` to the action-button conditional in the template**

Find (around line 307):

```html
        {% if section.type in ('snapbridge', 'device_upload', 'reupload') %}
        <button class="btn btn-primary"
                onclick="resolveGroup({{ group.id }}, this)">
          ✓ Confirm resolution
        </button>
```

Replace with:

```html
        {% if section.type in ('snapbridge', 'edit_pair', 'device_upload', 'reupload') %}
        <button class="btn btn-primary"
                onclick="resolveGroup({{ group.id }}, this)">
          ✓ Confirm resolution
        </button>
```

- [ ] **Step 3.5: Run the test suite**

```bash
python -m pytest tests/ -q
```
Expected: 1077 passed (unchanged — no regressions from UI edits).

- [ ] **Step 3.6: Commit**

```bash
git add reviewer/app.py reviewer/templates/duplicates.html
git commit -m "feat: edit_pair section in /duplicates UI (#129)

Add ORDER BY slot, sections list entry, amber badge CSS, and action
button support for the new edit_pair group type.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 4: Preserve `resolved=1` on `--write` re-run

**Files:**
- Modify: `tests/test_deduplicator.py` (new class `TestWriteGroupsPreservesResolved`)
- Modify: `poller/deduplicator.py:630-650` (`_write_groups` ON CONFLICT clause)

- [ ] **Step 4.1: Write the failing test**

Add after `TestClassifyGroupEditPair`:

```python
# ---------------------------------------------------------------------------
# _write_groups — preserve resolved=1 on re-run
# ---------------------------------------------------------------------------


class TestWriteGroupsPreservesResolved(unittest.TestCase):
    def test_re_run_does_not_reset_resolved_group(self):
        from poller.deduplicator import _write_groups, DuplicateGroup

        conn = _make_dedup_db()
        # Simulate a group that the user has already resolved
        conn.execute(
            "INSERT INTO duplicate_groups"
            " (id, match_key, group_type, photo_count, resolved, notes)"
            " VALUES (1, 'DSC_0001.JPG|2024-09-28T14:12:43', 'uncertain', 2, 1,"
            " 'upload gap=unknown')"
        )
        conn.execute(
            "INSERT INTO photos"
            " (id, flickr_id, uuid, duplicate_group_id, duplicate_role)"
            " VALUES (10, '11111', 'UUID-A', 1, 'review')"
        )
        conn.execute(
            "INSERT INTO photos"
            " (id, flickr_id, uuid, duplicate_group_id, duplicate_role)"
            " VALUES (20, '22222', 'UUID-B', 1, 'review')"
        )
        conn.commit()

        p1 = make_photo(id=10, flickr_id="11111", uuid="UUID-A")
        p2 = make_photo(id=20, flickr_id="22222", uuid="UUID-B")
        group = DuplicateGroup(
            match_key="DSC_0001.JPG|2024-09-28T14:12:43",
            group_type="uncertain",
            photos=[p1, p2],
            keeper=None,
            discards=[],
            review=[p1, p2],
            notes="upload gap=unknown",
        )

        _write_groups(conn, [group])

        row = conn.execute(
            "SELECT resolved FROM duplicate_groups"
            " WHERE match_key = 'DSC_0001.JPG|2024-09-28T14:12:43'"
        ).fetchone()
        self.assertEqual(row["resolved"], 1,
                         "resolved=1 must be preserved when --write re-runs")
```

- [ ] **Step 4.2: Run to confirm it fails**

```bash
python -m pytest tests/test_deduplicator.py::TestWriteGroupsPreservesResolved -v
```
Expected: `FAILED` — current code sets `resolved=0` unconditionally.

- [ ] **Step 4.3: Fix the ON CONFLICT clause in `_write_groups`**

In `poller/deduplicator.py`, locate the `INSERT INTO duplicate_groups ... ON CONFLICT` statement inside `_write_groups`. Replace the `DO UPDATE SET` block:

```python
        conn.execute(
            """
            INSERT INTO duplicate_groups (match_key, group_type, photo_count, notes, resolved)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(match_key) DO UPDATE SET
                group_type  = excluded.group_type,
                photo_count = excluded.photo_count,
                notes       = excluded.notes,
                resolved    = CASE WHEN duplicate_groups.resolved = 1
                                   THEN 1
                                   ELSE excluded.resolved
                              END,
                updated_at  = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
        """,
            (
                group.match_key,
                group.group_type,
                len(group.photos),
                group.notes,
                1 if is_not_duplicate else 0,
            ),
        )
```

- [ ] **Step 4.4: Run the new test**

```bash
python -m pytest tests/test_deduplicator.py::TestWriteGroupsPreservesResolved -v
```
Expected: `PASSED`.

- [ ] **Step 4.5: Run full suite**

```bash
python -m pytest tests/ -q
```
Expected: 1078 passed (1077 + 1 new).

- [ ] **Step 4.6: Commit**

```bash
git add poller/deduplicator.py tests/test_deduplicator.py
git commit -m "fix: preserve resolved=1 in _write_groups ON CONFLICT upsert (#129)

Re-running --write was resetting user-reviewed groups back to
unresolved. The CASE guard preserves resolved=1 permanently.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 5: `_prune_stale_groups` function and `--prune` CLI

**Files:**
- Modify: `tests/test_deduplicator.py` (new class `TestPruneStaleGroups`)
- Modify: `poller/deduplicator.py` (new `_prune_stale_groups`; update `main()`)

- [ ] **Step 5.1: Write failing tests**

Add after `TestWriteGroupsPreservesResolved`:

```python
# ---------------------------------------------------------------------------
# _prune_stale_groups
# ---------------------------------------------------------------------------


class TestPruneStaleGroups(unittest.TestCase):
    """Tests for _prune_stale_groups().

    Uses _make_dedup_db() which has the duplicate_groups + photos schema.
    _prune_stale_groups only looks at unresolved groups (resolved=0).
    """

    def test_zombie_group_zero_linked_is_deleted(self):
        from poller.deduplicator import _prune_stale_groups

        conn = _make_dedup_db()
        conn.execute(
            "INSERT INTO duplicate_groups (id, match_key, group_type, photo_count, resolved)"
            " VALUES (1, 'DSC_0001.JPG|2024-01-01', 'uncertain', 2, 0)"
        )
        conn.commit()

        counts = _prune_stale_groups(conn, dry_run=False)

        self.assertEqual(counts["groups_deleted"], 1)
        self.assertEqual(counts["links_cleared"], 0)
        row = conn.execute(
            "SELECT id FROM duplicate_groups WHERE id = 1"
        ).fetchone()
        self.assertIsNone(row)

    def test_zombie_group_one_linked_is_deleted_and_link_cleared(self):
        from poller.deduplicator import _prune_stale_groups

        conn = _make_dedup_db()
        conn.execute(
            "INSERT INTO duplicate_groups (id, match_key, group_type, photo_count, resolved)"
            " VALUES (1, 'DSC_0001.JPG|2024-01-01', 'uncertain', 2, 0)"
        )
        conn.execute(
            "INSERT INTO photos"
            " (id, flickr_id, duplicate_group_id, duplicate_role)"
            " VALUES (10, '11111', 1, 'review')"
        )
        conn.commit()

        counts = _prune_stale_groups(conn, dry_run=False)

        self.assertEqual(counts["groups_deleted"], 1)
        self.assertEqual(counts["links_cleared"], 1)
        row = conn.execute(
            "SELECT id FROM duplicate_groups WHERE id = 1"
        ).fetchone()
        self.assertIsNone(row)
        photo = conn.execute(
            "SELECT duplicate_group_id FROM photos WHERE id = 10"
        ).fetchone()
        self.assertIsNone(photo["duplicate_group_id"])

    def test_stale_count_repaired_for_healthy_group(self):
        from poller.deduplicator import _prune_stale_groups

        conn = _make_dedup_db()
        conn.execute(
            "INSERT INTO duplicate_groups (id, match_key, group_type, photo_count, resolved)"
            " VALUES (1, 'DSC_0001.JPG|2024-01-01', 'device_upload', 3, 0)"
        )
        conn.execute(
            "INSERT INTO photos (id, flickr_id, duplicate_group_id, duplicate_role)"
            " VALUES (10, '11111', 1, 'keeper')"
        )
        conn.execute(
            "INSERT INTO photos (id, flickr_id, duplicate_group_id, duplicate_role)"
            " VALUES (20, '22222', 1, 'discard')"
        )
        conn.commit()

        counts = _prune_stale_groups(conn, dry_run=False)

        self.assertEqual(counts["counts_repaired"], 1)
        row = conn.execute(
            "SELECT photo_count FROM duplicate_groups WHERE id = 1"
        ).fetchone()
        self.assertEqual(row["photo_count"], 2)

    def test_dry_run_reports_eligible_without_writing(self):
        from poller.deduplicator import _prune_stale_groups

        conn = _make_dedup_db()
        conn.execute(
            "INSERT INTO duplicate_groups (id, match_key, group_type, photo_count, resolved)"
            " VALUES (1, 'DSC_0001.JPG|2024-01-01', 'uncertain', 2, 0)"
        )
        conn.commit()

        counts = _prune_stale_groups(conn, dry_run=True)

        self.assertEqual(counts["groups_deleted"], 1)  # eligible count
        row = conn.execute(
            "SELECT id FROM duplicate_groups WHERE id = 1"
        ).fetchone()
        self.assertIsNotNone(row)  # not actually deleted

    def test_resolved_groups_are_not_touched(self):
        from poller.deduplicator import _prune_stale_groups

        conn = _make_dedup_db()
        conn.execute(
            "INSERT INTO duplicate_groups (id, match_key, group_type, photo_count, resolved)"
            " VALUES (1, 'DSC_0001.JPG|2024-01-01', 'uncertain', 2, 1)"  # resolved=1
        )
        conn.commit()

        counts = _prune_stale_groups(conn, dry_run=False)

        self.assertEqual(counts["groups_deleted"], 0)
        self.assertEqual(counts["counts_repaired"], 0)
        row = conn.execute(
            "SELECT id FROM duplicate_groups WHERE id = 1"
        ).fetchone()
        self.assertIsNotNone(row)

    def test_invariant_no_zombie_groups_after_prune(self):
        from poller.deduplicator import _prune_stale_groups

        conn = _make_dedup_db()
        # zombie (0 linked)
        conn.execute(
            "INSERT INTO duplicate_groups (id, match_key, group_type, photo_count, resolved)"
            " VALUES (1, 'DSC_0001.JPG|2024-01-01', 'uncertain', 2, 0)"
        )
        # healthy (2 linked)
        conn.execute(
            "INSERT INTO duplicate_groups (id, match_key, group_type, photo_count, resolved)"
            " VALUES (2, 'DSC_0002.JPG|2024-01-02', 'device_upload', 2, 0)"
        )
        conn.execute(
            "INSERT INTO photos (id, flickr_id, duplicate_group_id, duplicate_role)"
            " VALUES (10, '11111', 2, 'keeper')"
        )
        conn.execute(
            "INSERT INTO photos (id, flickr_id, duplicate_group_id, duplicate_role)"
            " VALUES (20, '22222', 2, 'discard')"
        )
        conn.commit()

        _prune_stale_groups(conn, dry_run=False)

        rows = conn.execute("""
            SELECT dg.id, COUNT(p.id) AS linked
            FROM duplicate_groups dg
            LEFT JOIN photos p ON p.duplicate_group_id = dg.id
            WHERE dg.resolved = 0
            GROUP BY dg.id
            HAVING linked < 2
        """).fetchall()
        self.assertEqual(len(rows), 0,
                         "No unresolved group should have < 2 linked photos after prune")

    def test_invariant_photo_count_matches_linked_after_prune(self):
        from poller.deduplicator import _prune_stale_groups

        conn = _make_dedup_db()
        conn.execute(
            "INSERT INTO duplicate_groups (id, match_key, group_type, photo_count, resolved)"
            " VALUES (1, 'DSC_0001.JPG|2024-01-01', 'device_upload', 3, 0)"
        )
        conn.execute(
            "INSERT INTO photos (id, flickr_id, duplicate_group_id, duplicate_role)"
            " VALUES (10, '11111', 1, 'keeper')"
        )
        conn.execute(
            "INSERT INTO photos (id, flickr_id, duplicate_group_id, duplicate_role)"
            " VALUES (20, '22222', 1, 'discard')"
        )
        conn.commit()

        _prune_stale_groups(conn, dry_run=False)

        mismatched = conn.execute("""
            SELECT dg.id, dg.photo_count, COUNT(p.id) AS linked
            FROM duplicate_groups dg
            LEFT JOIN photos p ON p.duplicate_group_id = dg.id
            WHERE dg.resolved = 0
            GROUP BY dg.id
            HAVING dg.photo_count != linked
        """).fetchall()
        self.assertEqual(len(mismatched), 0,
                         "photo_count must equal actual linked count after prune")

    def test_invariant_no_dangling_duplicate_group_id_after_prune(self):
        from poller.deduplicator import _prune_stale_groups

        conn = _make_dedup_db()
        conn.execute(
            "INSERT INTO duplicate_groups (id, match_key, group_type, photo_count, resolved)"
            " VALUES (1, 'DSC_0001.JPG|2024-01-01', 'uncertain', 2, 0)"
        )
        conn.execute(
            "INSERT INTO photos (id, flickr_id, duplicate_group_id, duplicate_role)"
            " VALUES (10, '11111', 1, 'review')"
        )
        conn.commit()

        _prune_stale_groups(conn, dry_run=False)

        dangling = conn.execute("""
            SELECT p.id FROM photos p
            WHERE p.duplicate_group_id IS NOT NULL
              AND NOT EXISTS (
                SELECT 1 FROM duplicate_groups dg
                WHERE dg.id = p.duplicate_group_id
              )
        """).fetchall()
        self.assertEqual(len(dangling), 0,
                         "No photo should have a duplicate_group_id pointing to a deleted group")
```

- [ ] **Step 5.2: Run to confirm they fail**

```bash
python -m pytest tests/test_deduplicator.py::TestPruneStaleGroups -v
```
Expected: all 8 fail with `ImportError` — `_prune_stale_groups` not defined yet.

- [ ] **Step 5.3: Implement `_prune_stale_groups` in `poller/deduplicator.py`**

Add the function in the "Delete discards" section (before `_print_report`):

```python
def _prune_stale_groups(
    conn: sqlite3.Connection, dry_run: bool = True
) -> dict[str, int]:
    """Clean up duplicate groups that have become stale since the last --write run.

    Class A — zombie groups (0 or 1 linked photos): nothing meaningful to compare.
      Action: delete the group row; clear duplicate_group_id/duplicate_role on any
      remaining linked photo. If the underlying key still has ≥ 2 photos in the DB,
      the next --write run will recreate the group correctly.

    Class B — stale photo_count: ≥ 2 photos still linked but photo_count is wrong.
      Action: update photo_count to actual linked count.
      (Notes text is regenerated by --write; --prune only fixes the integer field.)

    Only operates on unresolved groups (resolved=0). Does not reclassify groups.

    Args:
        conn:     SQLite connection (row_factory must be set to sqlite3.Row).
        dry_run:  If True (default), report counts without writing anything.

    Returns:
        dict with keys: groups_deleted, links_cleared, counts_repaired
    """
    rows = conn.execute("""
        SELECT dg.id, dg.match_key, dg.photo_count,
               COUNT(p.id) AS linked_count
        FROM duplicate_groups dg
        LEFT JOIN photos p ON p.duplicate_group_id = dg.id
        WHERE dg.resolved = 0
        GROUP BY dg.id
        HAVING linked_count != dg.photo_count
    """).fetchall()

    groups_deleted = 0
    links_cleared = 0
    counts_repaired = 0

    for r in rows:
        gid = r["id"]
        linked = r["linked_count"]

        if linked <= 1:
            # Class A: zombie group — nothing meaningful left to compare
            groups_deleted += 1
            links_cleared += linked  # 0 or 1 photo to unlink
            if not dry_run:
                conn.execute(
                    "UPDATE photos SET duplicate_group_id = NULL, duplicate_role = NULL"
                    " WHERE duplicate_group_id = ?",
                    (gid,),
                )
                conn.execute("DELETE FROM duplicate_groups WHERE id = ?", (gid,))
        else:
            # Class B: still a valid group, but photo_count is stale
            counts_repaired += 1
            if not dry_run:
                conn.execute(
                    "UPDATE duplicate_groups"
                    " SET photo_count = ?,"
                    "     updated_at  = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
                    " WHERE id = ?",
                    (linked, gid),
                )

    if not dry_run:
        conn.commit()

    return {
        "groups_deleted": groups_deleted,
        "links_cleared": links_cleared,
        "counts_repaired": counts_repaired,
    }
```

- [ ] **Step 5.4: Add `--prune` to `main()` CLI argument parser**

In `main()`, find the existing `--apply` argument definition:

```python
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Execute deletions (default is dry-run). Requires --delete-discards.",
    )
```

Replace with:

```python
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Execute changes (default is dry-run). Requires --delete-discards, --mark-discards, or --prune.",
    )
```

Then add the new `--prune` argument immediately after `--apply`:

```python
    parser.add_argument(
        "--prune",
        action="store_true",
        help=(
            "Clean up zombie groups (0–1 linked photos) and stale photo_count values. "
            "Default is dry-run; use --apply to execute."
        ),
    )
```

- [ ] **Step 5.5: Update the `--apply` validation guard**

Find:

```python
    if args.apply and not args.delete_discards and not args.mark_discards:
        log.error("--apply requires --delete-discards or --mark-discards")
        sys.exit(1)
```

Replace with:

```python
    if args.apply and not args.delete_discards and not args.mark_discards and not args.prune:
        log.error("--apply requires --delete-discards, --mark-discards, or --prune")
        sys.exit(1)
```

- [ ] **Step 5.6: Add `--prune` execution block to `main()`**

Add the following block **before** the `if args.flickr:` block (so it's a top-level mode that doesn't require `--flickr`):

```python
    if args.prune:
        dry_run = not args.apply
        log.info("Pruning stale duplicate groups in %s (dry_run=%s) …", db_path, dry_run)
        counts = _prune_stale_groups(conn, dry_run=dry_run)
        label = "Eligible for pruning (dry run):" if dry_run else "Pruned:"
        print(f"\n{label}")
        print(f"  Groups deleted:     {counts['groups_deleted']}")
        print(f"  Photo links cleared:{counts['links_cleared']}")
        print(f"  Counts repaired:    {counts['counts_repaired']}")
        if dry_run:
            print("\nDry run — no changes written. Use --apply to execute.")
        conn.close()
        return
```

- [ ] **Step 5.7: Run the prune tests**

```bash
python -m pytest tests/test_deduplicator.py::TestPruneStaleGroups -v
```
Expected: all 8 pass.

- [ ] **Step 5.8: Run full suite**

```bash
python -m pytest tests/ -q
```
Expected: 1087 passed (1078 + 8 + 1 already counted = 1087).

- [ ] **Step 5.9: Commit**

```bash
git add poller/deduplicator.py tests/test_deduplicator.py
git commit -m "feat: --prune subcommand cleans zombie groups and stale photo_count (#129)

_prune_stale_groups():
  Class A (0-1 linked): delete group, clear dangling FK
  Class B (≥2 linked, wrong count): update photo_count
Dry-run by default; --apply to execute.
Reports groups_deleted / links_cleared / counts_repaired.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 6: Run `--write` against the live database

This is the first half of the one-time recovery sequence. It links 206 orphaned `candidate_public`/`needs_review` photos to their groups and regenerates notes text with correct counts.

- [ ] **Step 6.1: Dry-run first to see what will change**

```bash
python poller/deduplicator.py --config config/config.yml --dry-run
```
Note the group counts before writing.

- [ ] **Step 6.2: Run `--write`**

```bash
python poller/deduplicator.py --config config/config.yml --write
```
Expected: output reports groups written; the 206 orphaned photos are now linked; no previously-resolved groups are reset (preserved by the ON CONFLICT fix from Task 4).

- [ ] **Step 6.3: Verify no resolved groups were reset**

```bash
sqlite3 data/curator.db "
SELECT COUNT(*) FROM duplicate_groups WHERE resolved = 1;
"
```
Count should be the same as before `--write` (resolved groups untouched).

---

## Task 7: Run `--prune --apply` against the live database

Second half of the one-time recovery sequence. Removes zombie groups and corrects stale `photo_count` values.

- [ ] **Step 7.1: Dry-run first**

```bash
python poller/deduplicator.py --config config/config.yml --prune
```
Expected: reports counts of groups_deleted, links_cleared, counts_repaired without writing.

- [ ] **Step 7.2: Apply**

```bash
python poller/deduplicator.py --config config/config.yml --prune --apply
```
Expected: non-zero groups_deleted (zombie cleanup), counts_repaired for stale counts.

- [ ] **Step 7.3: Verify the invariants hold**

```bash
sqlite3 data/curator.db "
-- Invariant 1: no unresolved group with < 2 linked photos
SELECT COUNT(*) AS zombie_groups
FROM (
  SELECT dg.id, COUNT(p.id) AS linked
  FROM duplicate_groups dg
  LEFT JOIN photos p ON p.duplicate_group_id = dg.id
  WHERE dg.resolved = 0
  GROUP BY dg.id
  HAVING linked < 2
);

-- Invariant 2: no photo_count mismatch
SELECT COUNT(*) AS stale_counts
FROM (
  SELECT dg.id, dg.photo_count, COUNT(p.id) AS linked
  FROM duplicate_groups dg
  LEFT JOIN photos p ON p.duplicate_group_id = dg.id
  WHERE dg.resolved = 0
  GROUP BY dg.id
  HAVING dg.photo_count != linked
);

-- Invariant 3: no dangling duplicate_group_id
SELECT COUNT(*) AS dangling
FROM photos p
WHERE p.duplicate_group_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM duplicate_groups dg WHERE dg.id = p.duplicate_group_id
  );
"
```
Expected: all three counts are `0`.

---

## Task 8: Update docs, README, and close the issue

**Files:**
- Modify: `README.md` (test count)
- Modify: `docs/testing.md` (coverage inventory)
- Modify: `docs/superpowers/specs/2026-05-23-dedup-bugs-edit-pair-prune-design.md` (mark done)

- [ ] **Step 8.1: Update README test count**

Run the full suite to get the final count:

```bash
python -m pytest tests/ -q 2>&1 | tail -1
```

In `README.md`, update both occurrences of the test count:
- Line ~229: `| \`tests/\` | Unit tests (NNNN tests) |`
- Line ~571: the prose sentence beginning `NNNN tests covering...`

Add `edit_pair duplicate category` and `dedup stale-group cleanup (--prune)` to the coverage list in the prose sentence.

- [ ] **Step 8.2: Update `docs/testing.md`**

Add a line to the coverage inventory for:
- `edit_pair` duplicate category (`_is_edit_pair`, `_classify_group`, UI)
- `_write_groups` preserve-resolved invariant
- `_prune_stale_groups` (zombie groups, stale counts, invariant assertions)

- [ ] **Step 8.3: Mark spec as done**

In `docs/superpowers/specs/2026-05-23-dedup-bugs-edit-pair-prune-design.md`, change:

```
**Status:** Approved — ready for implementation plan
```
to:
```
**Status:** ✓ Done — implemented in commits on 2026-05-23
```

- [ ] **Step 8.4: Commit docs**

```bash
git add README.md docs/testing.md docs/superpowers/specs/2026-05-23-dedup-bugs-edit-pair-prune-design.md
git commit -m "docs: update test count and coverage inventory for #129

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

- [ ] **Step 8.5: Close the GitHub issue**

```bash
gh issue close 129 --comment "Implemented in this session:
- _is_snapbridge_pair requires DSC_* prefix
- New edit_pair category for iPhone original+edit pairs
- _write_groups ON CONFLICT preserves resolved=1
- New --prune subcommand (zombie group cleanup + stale photo_count repair)
- One-time --write + --prune --apply recovery run completed
- 15 new tests (1072 → 1087)"
```

- [ ] **Step 8.6: Push to origin**

```bash
git push origin main
```
