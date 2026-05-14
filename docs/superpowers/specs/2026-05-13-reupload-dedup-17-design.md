# Re-upload Duplicate Detection — Design (Phase 1)

**GitHub issue:** #17

---

## Problem

The review queue contains ~58,600 Flickr-only `candidate_public` records. A significant
subset are photos uploaded to Flickr **twice in separate sessions**: the older upload was
linked to an Apple Photos record (has both `uuid` and `flickr_id`); the newer upload is a
Flickr-only orphan (`uuid IS NULL`) that sits in the review queue indefinitely.

`bp link-orphans` correctly skips these — it only merges Photos-only + Flickr-only pairs
where the Photos record has no `flickr_id`. Re-uploads are a third case (linked record +
extra Flickr-only record) and need separate handling.

---

## Scope: Phase 1 only

This design covers **detection and DB grouping** only. No Flickr API writes occur in
Phase 1.

Deferred to separate issues:
- **Phase 2:** Privacy enforcement — make the low-res copy private; flag exceptions
  (has Flickr comments, has Flickr group memberships, both halves already public).
- **Phase 3:** Metadata sync between the pair (weakly-held; may be unnecessary if
  duplicates are later purged manually).
- **Phase 4:** Local UI cross-linking in the reviewer (weakly-held).

---

## Background: upload pattern

The typical re-upload pattern is Snapbridge: low-res proxy images (~1 MB, reduced
dimensions) stream to Flickr in real time via the Nikon phone app. The full-res originals
may be imported to a computer, synced to iCloud Photos, and uploaded to Flickr months
later — gaps of 6–12 months are expected, producing Flickr ID gaps in the hundreds of
millions.

Two important constraints that shape the design:

1. **Resolution is not a reliable primary signal.** Snapbridge explicitly generates
   low-res proxies with different dimensions. Dimension data may also be absent for
   many records until the scanner backfill runs.

2. **Nikon firmware bug.** Still images captured during a video recording session all
   receive the same `date_taken` timestamp as the first frame of the video. A
   timestamp-only match could therefore produce false-positive groups from unrelated
   photos sharing a coincident timestamp.

---

## Architecture

### New function: `_fetch_reupload_candidates()`

Added to `poller/deduplicator.py` alongside the existing
`_fetch_duplicate_candidates()`. The two functions are independent; `--flickr` mode
calls only the new function.

**Load strategy:** consistent with `link_orphans.py` — both sides loaded into memory
and matched in Python (dict lookups), avoiding a full SQL cross-join across hundreds
of thousands of rows.

**Left side (candidates):**
```sql
SELECT id, flickr_id, uuid, original_filename, date_taken,
       date_uploaded_flickr, width, height, duplicate_group_id
FROM photos
WHERE uuid IS NULL
  AND flickr_id IS NOT NULL
  AND privacy_state = 'candidate_public'
```

**Right side (linked records):**
```sql
SELECT id, flickr_id, uuid, original_filename, date_taken,
       date_uploaded_flickr, width, height, duplicate_group_id
FROM photos
WHERE uuid IS NOT NULL
  AND flickr_id IS NOT NULL
```

### Primary match key: filename + timestamp (Approach B)

Match on `original_filename` (exact) AND `date_taken` within ±2 seconds.

When `original_filename IS NULL` on either record, fall back to timestamp-only matching.

This approach is chosen over timestamp-only (too many false positives from the Nikon
firmware bug) and dimension-based matching (dimensions unavailable or unreliable for
Snapbridge proxies).

### ID gap classification

```
gap = abs(int(flickr_only.flickr_id) - int(linked.flickr_id))
```

| Gap | Classification |
|-----|---------------|
| > 100,000 | `reupload` — auto-grouped |
| ≤ 100,000 | `reupload_uncertain` — flagged for manual review |

Note: the gap threshold distinguishes upload *sessions* from burst-shot *batches*.
Actual session gaps of 6–12 months produce gaps in the hundreds of millions; the
100 K threshold is conservative by design.

### Keeper determination

Resolution-first, with fallback:

1. **Both records have dimensions** → keeper = record with higher `width × height`
2. **Only one record has dimensions** → keeper = the record with dimensions
3. **Neither has dimensions** → keeper = the linked record (has `uuid`); a flag is
   added to `notes` indicating this assumption could not be verified

The linked record (Photos-imported) is usually the full-res original, but this is
treated as an assumption, not a guarantee — resolution data always wins when available.

### Collision handling

Collisions in either direction are classified `reupload_uncertain` regardless of ID gap.
No auto-grouping fires for any ambiguous match.

- **One linked record → multiple Flickr-only records:** Nikon firmware scenario — multiple
  stills share the same timestamp. All candidate orphans are flagged uncertain.
- **One Flickr-only record → multiple linked records:** rare, but possible if the same
  photo was imported into Photos more than once. The orphan is flagged uncertain; the
  correct linked counterpart requires human review.

---

## DB state after `--write`

The existing `duplicate_groups` table and `photos` columns (`duplicate_group_id`,
`duplicate_role`) are reused without schema changes.

### `duplicate_groups` row

| Column | Value |
|--------|-------|
| `match_key` | `"{filename}\|{date_taken_sec}\|reupload"` |
| `group_type` | `'reupload'` or `'reupload_uncertain'` |
| `keeper_id` | `photos.id` of the keeper record |
| `photo_count` | `2` |
| `notes` | Human-readable: filenames, Flickr IDs, ID gap, upload date delta, any flags |
| `resolved` | `0` (Phase 2 flips this when it acts on privacy) |

The match key includes `\|reupload` suffix to avoid colliding with existing
filename+timestamp dedup groups.

### `photos` updates

| Record | `duplicate_group_id` | `duplicate_role` | `privacy_state` |
|--------|----------------------|-----------------|-----------------|
| Keeper | X | `'keeper'` | unchanged |
| Discard | X | `'discard'` | unchanged |

`privacy_state` is **not** modified in Phase 1. The discard stays `candidate_public`
until Phase 2 acts on it.

### Already-grouped records

If either record already has a `duplicate_group_id` (from a prior run or the existing
filename+timestamp deduplicator), the pair is **skipped** and a warning is logged. No
existing group is overwritten.

### Idempotency

The existing `ON CONFLICT(match_key) DO UPDATE` upsert in `_write_groups()` handles
re-runs without duplication.

### Transaction

All pairs written atomically in a single `BEGIN` / `COMMIT` block, with `ROLLBACK` on
any error — consistent with the existing deduplicator.

---

## CLI surface

New `--flickr` flag added to the existing `deduplicator.py` entry point:

```bash
bp dedup --flickr --dry-run     # find pairs, print report, no writes (default)
bp dedup --flickr --write       # find pairs and write to duplicate_groups
bp dedup --flickr --write --verbose  # also log each matched pair individually
bp dedup                        # existing filename+timestamp dedup (unchanged)
```

No `--confirm` flag in Phase 1 — there are no Flickr API calls.

### Report format

```
Reupload pairs found: 583

  reupload            541 pairs   (auto-grouped)
  reupload_uncertain   42 pairs   (flagged — small ID gap or 1-to-many match)

── REUPLOAD_UNCERTAIN (42 pairs) ──────────────────────────────────────
  DSC_0042.JPG | 2022-08-14T10:23:11
    linked:  flickr_id=48922xxxxxx  uuid=XXXX-...
    orphan:  flickr_id=48922xxxxxx+80  id_gap=80  (small gap — possible burst shot)
  ...

Dry run — no changes written. Use --write to persist.
```

Confirmed `reupload` pairs are not listed individually at default verbosity (there may
be hundreds). `--verbose` dumps each one.

---

## Edge cases

| Case | Handling |
|------|----------|
| Negative gap (Flickr-only ID < linked ID) | `abs()` for threshold; keeper by pixels as usual |
| Already has `duplicate_group_id` | Skip pair, log warning |
| NULL `flickr_id` on either side | Skip (shouldn't pass the query filters, but guarded) |
| Multiple linked records at same timestamp | All matches → `reupload_uncertain` |
| No dimensions on either record | Keeper = linked record; note flag added |

---

## Files touched

| File | Change |
|------|--------|
| `poller/deduplicator.py` | Add `_fetch_reupload_candidates()`; add `--flickr` CLI flag; extend `main()` to dispatch to new function |
| `tests/test_deduplicator.py` | Add `TestReuploadCandidates` test class (8 tests) |

---

## Tests

New class `TestReuploadCandidates` in `tests/test_deduplicator.py`, using the existing
`make_photo()` fixture:

| Test | Scenario | Expected |
|------|----------|----------|
| `test_exact_match_reupload` | Same filename + timestamp, gap > 100 K | `group_type='reupload'`, keeper=linked |
| `test_keeper_by_pixels` | Both have dimensions; linked is low-res | keeper = Flickr-only |
| `test_small_gap_uncertain` | ID gap ≤ 100 K | `group_type='reupload_uncertain'` |
| `test_one_to_many_uncertain` | Two Flickr-only records match same linked record | both `reupload_uncertain` |
| `test_already_grouped_skipped` | Flickr-only already has `duplicate_group_id` | pair skipped |
| `test_null_filename_fallback` | `original_filename` NULL on Flickr-only | matches on timestamp only |
| `test_negative_gap_abs` | Flickr-only has lower Flickr ID than linked | classified correctly; keeper by pixels |
| `test_no_dimensions_defaults_to_linked` | Neither record has dimensions | keeper=linked, note flag set |

---

## Implementation order

Phase 1 is self-contained and unblocks everything else:

1. Add `_fetch_reupload_candidates()` + classification logic (TDD: tests first)
2. Extend `main()` with `--flickr` flag + report
3. Update README test count

Phase 2 (privacy enforcement) and Phases 3–4 are tracked as separate issues.
