# Design: Geo Sync Hysteresis Band (#149)

**Status:** approved  
**Issue:** [#149](https://github.com/cdevers/Blue-Pearmain/issues/149)  
**Date:** 2026-05-27

---

## Summary

Replace the single `GEO_DIVERGENCE_THRESHOLD_M = 1_000` constant with two constants that create a hysteresis band, preventing proposal churn when GPS coords drift back and forth near the 1 km boundary.

---

## Background

`sync_geo()` currently uses a single threshold. A photo whose divergence oscillates near 1 km will on each sync run supersede the existing pending proposal and insert a new one (the `source_hash_at_creation` guard only protects against sub-11m jitter, since it rounds to 4 decimal places ≈ 11m). The hysteresis band eliminates this: once coords are below the create threshold, no new churn occurs until they clearly diverge again.

The pattern is a Schmitt trigger / thermostat: two separate thresholds prevent oscillation at the boundary.

---

## Design

### Constants (`flickr/geo_sync.py`)

Replace:
```python
GEO_DIVERGENCE_THRESHOLD_M: int = 1_000
```

With:
```python
GEO_CREATE_THRESHOLD_M: int = 1_000    # create a proposal when divergence exceeds this
GEO_SUPPRESS_THRESHOLD_M: int = 800    # suppress without touching existing proposals below this
```

`GEO_DIVERGENCE_THRESHOLD_M` is removed. Its only callers are `geo_sync.py` itself and `test_sync_geo.py` (import + 2 uses), both of which are updated.

### Logic change in `sync_geo()` (`flickr/geo_sync.py`)

The `elif has_flickr and has_photos:` branch changes from:

```python
dist = _haversine_m(flk_lat, flk_lon, pho_lat, pho_lon)
if dist > GEO_DIVERGENCE_THRESHOLD_M:
    proposals.extend(_make_divergence_pair(...))
else:
    totals["suppressed_under_threshold"] += 1
    continue
```

To:

```python
dist = _haversine_m(flk_lat, flk_lon, pho_lat, pho_lon)
if dist > GEO_CREATE_THRESHOLD_M:
    proposals.extend(_make_divergence_pair(...))
elif dist > GEO_SUPPRESS_THRESHOLD_M:
    # Hysteresis band: leave existing pending proposals untouched
    totals["suppressed_in_band"] += 1
    continue
else:
    totals["suppressed_under_threshold"] += 1
    continue
```

### Observability counter

Add `"suppressed_in_band": 0` to the `totals` dict initialisation and to the `log.debug(...)` call at the end of `sync_geo()`.

The three zones and their counters:
- `dist > 1 000m` → `proposals_created` (unchanged)
- `800m < dist ≤ 1 000m` → `suppressed_in_band` (new)
- `dist ≤ 800m` → `suppressed_under_threshold` (unchanged)

---

## Test changes (`tests/test_sync_geo.py`)

### Import update

```python
# before
from flickr.geo_sync import sync_geo, GEO_DIVERGENCE_THRESHOLD_M

# after
from flickr.geo_sync import sync_geo, GEO_CREATE_THRESHOLD_M, GEO_SUPPRESS_THRESHOLD_M
```

### Existing tests that need updating

**`test_threshold_boundary_below_no_proposal`** — uses `GEO_DIVERGENCE_THRESHOLD_M - 1` (999m, now in the band). Update to use `GEO_CREATE_THRESHOLD_M - 1`. The assertion (no proposal created) stays valid.

**`test_under_threshold_increments_suppressed_counter`** — uses 999m and asserts `suppressed_under_threshold == 1`. 999m is now in the band, so the counter is `suppressed_in_band`. Update the assertion to check `suppressed_in_band == 1` and rename the test to `test_in_band_increments_suppressed_in_band_counter`.

**`test_coords_agree_within_threshold_creates_no_proposal`** — uses 500m (~under the 800m suppress threshold). No change needed; still increments `suppressed_under_threshold`.

### New tests

**`test_band_creates_no_proposal`** — divergence of 900m (in band): verify no proposal is created.

**`test_below_suppress_threshold_increments_suppressed_under_threshold`** — divergence of 500m: verify `suppressed_under_threshold == 1` and `suppressed_in_band == 0`.

**`test_suppressed_in_band_counter_present_in_totals`** — verify `"suppressed_in_band"` key exists in the dict returned by `sync_geo()`.

---

## What is NOT changed

- `GEO_CREATE_THRESHOLD_M` value is 1 000m — no change to when proposals are created
- `upsert_proposal()` logic — unchanged
- Non-conflict proposals (flickr-only, photos-only) — unchanged; threshold only applies to divergence
- DB schema — unchanged
- Any UI — unchanged

---

## Scope

| Artifact | Change |
|---|---|
| `flickr/geo_sync.py` | 2 new constants; 1 removed; `suppressed_in_band` counter; 3-way branch |
| `tests/test_sync_geo.py` | Import update; 2 test updates; 3 new tests |

No other files touched.
