# Geo Sync Hysteresis Band Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single `GEO_DIVERGENCE_THRESHOLD_M` constant in `flickr/geo_sync.py` with two constants (`GEO_CREATE_THRESHOLD_M = 1_000` and `GEO_SUPPRESS_THRESHOLD_M = 800`) that form a hysteresis band, preventing proposal churn when GPS coordinates drift back and forth near the 1 km boundary.

**Architecture:** A Schmitt trigger / thermostat pattern: above 1 000 m creates proposals (unchanged), 800–1 000 m is a dead zone (existing proposals untouched, new `suppressed_in_band` counter), below 800 m stays `suppressed_under_threshold`. Two files change: `flickr/geo_sync.py` (constants + logic + counter) and `tests/test_sync_geo.py` (import update + 2 test edits + 3 new tests).

**Tech Stack:** Python, pytest, SQLite

---

## File Map

| File | Action | What changes |
|---|---|---|
| `flickr/geo_sync.py` | Modify | Replace constant; add `suppressed_in_band` counter; 3-way branch; updated log call |
| `tests/test_sync_geo.py` | Modify | Import update; 2 test edits; 1 rename; 3 new tests; granular-counters assertion |

No other files touched.

---

## Task 1: Write failing tests

**Files:**
- Modify: `tests/test_sync_geo.py`

**Context:** The current import at line 10 is `from flickr.geo_sync import sync_geo, GEO_DIVERGENCE_THRESHOLD_M`. After this task the import will use the two new names, which don't exist yet, so the entire test module will fail to import until Task 2 is done. That is the expected TDD state.

- [ ] **Step 1: Update the import**

In `tests/test_sync_geo.py`, replace line 10:

```python
# before
from flickr.geo_sync import sync_geo, GEO_DIVERGENCE_THRESHOLD_M

# after
from flickr.geo_sync import sync_geo, GEO_CREATE_THRESHOLD_M, GEO_SUPPRESS_THRESHOLD_M
```

- [ ] **Step 2: Update two existing tests that reference the old constant**

**`test_threshold_boundary_below_no_proposal`** (around line 120): change the constant used to compute `dlat`. The test name and the `count == 0` assertion stay the same (999 m is now in the hysteresis band, but still produces no proposal).

```python
    def test_threshold_boundary_below_no_proposal(self, db):
        lat1, lon1 = 42.3601, -71.0589
        dlat = (GEO_CREATE_THRESHOLD_M - 1) / 111_319.9
        lat2 = lat1 + dlat
        pid = db.upsert_photo(
            _photo(
                7,
                flickr_latitude=lat1,
                flickr_longitude=lon1,
                photos_latitude=lat2,
                photos_longitude=lon1,
            )
        )
        sync_geo(db, dry_run=False, photo_ids=[pid])
        count = db.conn.execute(
            "SELECT COUNT(*) AS n FROM metadata_proposals WHERE photo_id=?", (pid,)
        ).fetchone()["n"]
        assert count == 0
```

**`test_threshold_boundary_above_creates_proposal`** (around line 139): same constant swap. The `count > 0` assertion stays the same (1 001 m is above the create threshold).

```python
    def test_threshold_boundary_above_creates_proposal(self, db):
        lat1, lon1 = 42.3601, -71.0589
        dlat = (GEO_CREATE_THRESHOLD_M + 1) / 111_319.9
        lat2 = lat1 + dlat
        pid = db.upsert_photo(
            _photo(
                8,
                flickr_latitude=lat1,
                flickr_longitude=lon1,
                photos_latitude=lat2,
                photos_longitude=lon1,
            )
        )
        sync_geo(db, dry_run=False, photo_ids=[pid])
        count = db.conn.execute(
            "SELECT COUNT(*) AS n FROM metadata_proposals WHERE photo_id=?", (pid,)
        ).fetchone()["n"]
        assert count > 0
```

- [ ] **Step 3: Rename + update `test_under_threshold_increments_suppressed_counter`**

This test uses 999 m (one below the old single threshold). After the change 999 m is in the hysteresis band, so the counter incremented is `suppressed_in_band`, not `suppressed_under_threshold`. Rename the test and update the assertion:

```python
    def test_in_band_increments_suppressed_in_band_counter(self, db):
        lat1, lon1 = 42.3601, -71.0589
        dlat = (GEO_CREATE_THRESHOLD_M - 1) / 111_319.9
        pid = db.upsert_photo(
            _photo(
                16,
                flickr_latitude=lat1,
                flickr_longitude=lon1,
                photos_latitude=lat1 + dlat,
                photos_longitude=lon1,
            )
        )
        totals = sync_geo(db, dry_run=False, photo_ids=[pid])
        assert totals["suppressed_in_band"] == 1
        assert totals["proposals_created"] == 0
```

- [ ] **Step 4: Add `"suppressed_in_band"` assertion to `test_return_dict_has_granular_counters`**

Find `test_return_dict_has_granular_counters` (around line 182). Add one assertion:

```python
    def test_return_dict_has_granular_counters(self, db):
        """sync_geo() return dict must include all observability counters."""
        totals = sync_geo(db, dry_run=False, photo_ids=[])
        assert "proposals_created" in totals
        assert "suppressed_confirmed_none" in totals
        assert "suppressed_in_band" in totals
        assert "suppressed_under_threshold" in totals
        assert "suppressed_both_absent" in totals
        assert "suppressed_not_linked" in totals
        assert "failed" in totals
```

- [ ] **Step 5: Add three new tests to `TestSyncGeo`**

Add all three immediately before the `class TestSupersedeIsolation:` line (after `test_photo_missing_flickr_id_skipped`):

```python
    def test_band_creates_no_proposal(self, db):
        # 900m — in the hysteresis band (800m < dist <= 1000m)
        lat1, lon1 = 42.3601, -71.0589
        dlat = 900 / 111_319.9
        pid = db.upsert_photo(
            _photo(
                17,
                flickr_latitude=lat1,
                flickr_longitude=lon1,
                photos_latitude=lat1 + dlat,
                photos_longitude=lon1,
            )
        )
        sync_geo(db, dry_run=False, photo_ids=[pid])
        count = db.conn.execute(
            "SELECT COUNT(*) AS n FROM metadata_proposals WHERE photo_id=?", (pid,)
        ).fetchone()["n"]
        assert count == 0

    def test_below_suppress_threshold_increments_suppressed_under_threshold(self, db):
        # 500m — below the suppress threshold (800m)
        lat1, lon1 = 42.3601, -71.0589
        dlat = 500 / 111_319.9
        pid = db.upsert_photo(
            _photo(
                18,
                flickr_latitude=lat1,
                flickr_longitude=lon1,
                photos_latitude=lat1 + dlat,
                photos_longitude=lon1,
            )
        )
        totals = sync_geo(db, dry_run=False, photo_ids=[pid])
        assert totals["suppressed_under_threshold"] == 1
        assert totals["suppressed_in_band"] == 0

    def test_suppressed_in_band_counter_present_in_totals(self, db):
        totals = sync_geo(db, dry_run=False, photo_ids=[])
        assert "suppressed_in_band" in totals
```

- [ ] **Step 6: Run the tests to confirm they fail for the right reason**

```bash
python -m pytest tests/test_sync_geo.py -v 2>&1 | head -30
```

Expected: The module fails to import because `GEO_CREATE_THRESHOLD_M` and `GEO_SUPPRESS_THRESHOLD_M` don't exist yet in `flickr/geo_sync.py`. You will see something like:

```
ImportError: cannot import name 'GEO_CREATE_THRESHOLD_M' from 'flickr.geo_sync'
```

All tests in the file are collected as errors (not failures). This is correct — the implementation doesn't exist yet.

Do NOT commit yet.

---

## Task 2: Implement hysteresis band in `flickr/geo_sync.py`

**Files:**
- Modify: `flickr/geo_sync.py`

**Context:** `flickr/geo_sync.py` currently has:
- Line 24: `GEO_DIVERGENCE_THRESHOLD_M: int = 1_000`
- Lines 138–154: the `elif has_flickr and has_photos:` branch uses `GEO_DIVERGENCE_THRESHOLD_M`
- Lines 165–174: `log.debug()` call with counter format string

- [ ] **Step 1: Replace the single constant with two**

In `flickr/geo_sync.py`, replace line 24:

```python
# before
GEO_DIVERGENCE_THRESHOLD_M: int = 1_000
```

```python
# after
GEO_CREATE_THRESHOLD_M: int = 1_000    # create a proposal when divergence exceeds this
GEO_SUPPRESS_THRESHOLD_M: int = 800    # hysteresis band lower edge; below this → suppressed_under_threshold
```

- [ ] **Step 2: Add `suppressed_in_band` to the `totals` dict initialisation**

In `sync_geo()`, the `totals` dict (around line 59) becomes:

```python
    totals: dict[str, int] = {
        "proposals_created": 0,
        "suppressed_confirmed_none": 0,
        "suppressed_in_band": 0,
        "suppressed_under_threshold": 0,
        "suppressed_both_absent": 0,
        "suppressed_not_linked": 0,
        "failed": 0,
    }
```

- [ ] **Step 3: Replace the two-way branch with a three-way branch**

In `sync_geo()`, replace the entire `elif has_flickr and has_photos:` block (currently lines 138–154):

```python
        # before
        elif has_flickr and has_photos:
            dist = _haversine_m(flk_lat, flk_lon, pho_lat, pho_lon)
            if dist > GEO_DIVERGENCE_THRESHOLD_M:
                proposals.extend(
                    _make_divergence_pair(
                        photo_id,
                        flk_lat=flk_lat,
                        flk_lon=flk_lon,
                        pho_lat=pho_lat,
                        pho_lon=pho_lon,
                        dist=dist,
                        now=now,
                    )
                )
            else:
                totals["suppressed_under_threshold"] += 1
                continue
```

```python
        # after
        elif has_flickr and has_photos:
            dist = _haversine_m(flk_lat, flk_lon, pho_lat, pho_lon)
            if dist > GEO_CREATE_THRESHOLD_M:
                proposals.extend(
                    _make_divergence_pair(
                        photo_id,
                        flk_lat=flk_lat,
                        flk_lon=flk_lon,
                        pho_lat=pho_lat,
                        pho_lon=pho_lon,
                        dist=dist,
                        now=now,
                    )
                )
            elif dist > GEO_SUPPRESS_THRESHOLD_M:
                # Hysteresis band: leave existing pending proposals untouched
                totals["suppressed_in_band"] += 1
                continue
            else:
                totals["suppressed_under_threshold"] += 1
                continue
```

- [ ] **Step 4: Update the `log.debug()` call to include the new counter**

In `sync_geo()`, replace the `log.debug(...)` call (around lines 165–174):

```python
    # before
    log.debug(
        "sync_geo done: created=%d  confirmed_none=%d  under_threshold=%d"
        "  both_absent=%d  not_linked=%d  failed=%d",
        totals["proposals_created"],
        totals["suppressed_confirmed_none"],
        totals["suppressed_under_threshold"],
        totals["suppressed_both_absent"],
        totals["suppressed_not_linked"],
        totals["failed"],
    )
```

```python
    # after
    log.debug(
        "sync_geo done: created=%d  confirmed_none=%d  in_band=%d"
        "  under_threshold=%d  both_absent=%d  not_linked=%d  failed=%d",
        totals["proposals_created"],
        totals["suppressed_confirmed_none"],
        totals["suppressed_in_band"],
        totals["suppressed_under_threshold"],
        totals["suppressed_both_absent"],
        totals["suppressed_not_linked"],
        totals["failed"],
    )
```

- [ ] **Step 5: Run `test_sync_geo.py` to confirm all tests pass**

```bash
python -m pytest tests/test_sync_geo.py -v
```

Expected: All tests PASS (including the 3 new ones and the renamed test). No import errors.

- [ ] **Step 6: Run the full test suite to catch regressions**

```bash
python -m pytest tests/ -q
```

Expected: All tests pass, no regressions.

- [ ] **Step 7: Run lint**

```bash
make lint
```

Expected: no new type errors. `GEO_CREATE_THRESHOLD_M` and `GEO_SUPPRESS_THRESHOLD_M` are both `int`, consistent with the removed constant.

- [ ] **Step 8: Update the GH issue label and commit**

```bash
gh issue edit 149 --add-label "has-plan"
```

```bash
git add flickr/geo_sync.py tests/test_sync_geo.py
git commit -m "feat(#149): geo sync hysteresis band (800–1000m dead zone)

Replace GEO_DIVERGENCE_THRESHOLD_M with two constants:
- GEO_CREATE_THRESHOLD_M = 1_000m (unchanged: proposals created above this)
- GEO_SUPPRESS_THRESHOLD_M = 800m (new: band lower edge)

Three-zone logic in sync_geo():
- dist > 1000m → proposals_created (unchanged)
- 800m < dist ≤ 1000m → suppressed_in_band (new dead zone)
- dist ≤ 800m → suppressed_under_threshold (unchanged)

Tests: import update; test_in_band_increments_suppressed_in_band_counter
(renamed + updated); test_threshold_boundary_* updated to GEO_CREATE_THRESHOLD_M;
3 new tests for band behaviour; granular-counters assertion updated.

Closes #149

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

- [ ] **Step 9: Post retrospective on GH issue and push**

```bash
gh issue comment 149 --body "Size estimate: S ✓

Files changed: 2 (flickr/geo_sync.py, tests/test_sync_geo.py)
Lines: ~35 added (constants: 2, branch: 4, counter init: 1, log format: 2; tests: ~26)
Plan tasks: 2

No scope changes. Purely mechanical — replace one constant with two, add a middle branch, update tests."
```

```bash
gh issue close 149
git push origin main
```
