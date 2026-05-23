# Duplicate Detection Bug Fixes — `edit_pair` category and stale group cleanup

**Date:** 2026-05-23  
**Status:** Approved — ready for implementation plan  
**Related issues:** [#129](https://github.com/cdevers/Blue-Pearmain/issues/129)

---

## Background

Two bugs were discovered during manual review of the `/duplicates` UI:

1. **Snapbridge mislabel on iPhone photos** — the `snapbridge` classifier was firing on `IMG_*.HEIC` files (iPhone) that happen to be an original + edited pair. Snapbridge is a Nikon-specific feature and should only apply to `DSC_*`-named files.

2. **Duplicate groups showing only one photo** — 290 unresolved groups have a mismatch between their stored `photo_count` and the number of photos actually linked to them. Root causes: (A) 206 photos added by the scanner *after* the deduplicator last ran — they share a key with an existing group but were never linked; (B) some photos have been deleted from the DB since their group was created, leaving groups with stale counts and nothing meaningful to review.

A related latent bug was also identified: `_write_groups` unconditionally overwrites `resolved` on upsert, meaning a re-run of `--write` would un-resolve groups the user has already reviewed.

---

## Bug 1: `edit_pair` category

### New classification logic

The classifier currently tests `_is_snapbridge_pair` before `device_upload` and `uncertain`. Two changes are made:

**`_is_snapbridge_pair` gains a filename guard:**  
Both photos must have a `DSC_`-prefixed `original_filename`. Without the prefix, the function returns `False` regardless of fingerprint or dimension differences. This correctly limits Snapbridge classification to Nikon camera files.

**New `_is_edit_pair` function:**  
Fires when:
- Exactly 2 photos
- Neither photo has a `DSC_`-prefixed filename
- Both fingerprints are present and differ (different file content)
- Both pixel counts are present and differ (one is a resized/cropped edit)

Structurally identical to `_is_snapbridge_pair` except for the filename test. The two functions are intentionally parallel, not merged, to keep each concept self-contained.

**Updated classification order in `_classify_group`:**

```
1. _is_snapbridge_pair?   → "snapbridge"    (DSC_*, diff fingerprints, diff dims)
2. _is_edit_pair?         → "edit_pair"     (non-DSC_*, diff fingerprints, diff dims)
3. gap > 5 min?           → "device_upload"
4. pixel_ratio > 1.1?     → "not_duplicate" (auto-dismiss)
5. otherwise              → "uncertain"
```

### Keeper assignment for `edit_pair`

Higher-pixel photo is designated keeper (matching Snapbridge logic), but **all photos are placed in the `review` list, not `discards`**. No photo is automatically marked for deletion. Notes string: *"Edit pair: X×Ypx vs A×Bpx (uuid-or-flickr-id) — likely original + edited version. Use 'Not a duplicate' to keep both."*

### UI changes

- New CSS badge `.badge-edit_pair`: amber (`background: #4a2800; color: #f5a623`) to match `uncertain`.
- New section on `/duplicates` between Snapbridge and Device Upload.
  - Section label: `EDIT PAIR`
  - Section description: *"Same filename and timestamp, different content — typically an original and an edited, cropped, or colour-corrected version. Use 'Not a duplicate' if you want to keep both."*
- Section actions: **"✓ Confirm resolution"** + **"Not a duplicate"** (same pair as Snapbridge; description sets the expectation that "Not a duplicate" is the common choice here).
- `app.py` duplicates route: add `edit_pair` to the sections list with appropriate label/description/type.

### What this does NOT change

- Stills taken during Nikon video recording (`DSC_*`, `fingerprints=same`) are unaffected — they already route to `uncertain`, which is correct. They need human "Not a duplicate" clicks.
- `IMG_3199.HEIC` and similar previously-mislabeled Snapbridge cases will be reclassified on the next `--write` run: most will become `device_upload` (large Flickr upload gap) or `uncertain`.

---

## Bug 2: Stale groups and orphaned photos

### Fix A — Preserve `resolved=1` on re-run (`_write_groups`)

The `ON CONFLICT` clause in `_write_groups` must not overwrite `resolved=1`:

```sql
ON CONFLICT(match_key) DO UPDATE SET
    group_type  = excluded.group_type,
    photo_count = excluded.photo_count,
    notes       = excluded.notes,
    resolved    = CASE WHEN duplicate_groups.resolved = 1 THEN 1
                       ELSE excluded.resolved END,
    updated_at  = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
```

This is a prerequisite for any safe re-run of `--write`. Without it, re-running the deduplicator would un-resolve groups the user has already reviewed.

### Fix B — `--prune` subcommand

New `--prune` subcommand (added to existing CLI alongside `--write`, `--flickr`, etc.). Scans all **unresolved** groups and cleans up two classes of staleness:

**Class A — zombie groups (0 or 1 linked photos)**  
The group is unresolvable — no meaningful comparison is possible. Action: delete the group row; clear `duplicate_group_id` and `duplicate_role` on any remaining linked photo. If the underlying key still has ≥ 2 photos in the DB, the next `--write` run will recreate the group correctly.

**Class B — stale `photo_count`**  
Where ≥ 2 photos are still linked but `photo_count` doesn't match reality. Action: update `photo_count` to the actual linked count. (The notes text — e.g. "3 copies" — is a denormalised string regenerated by `--write`; running `--write` first handles that. `--prune` fixes only the integer field.)

`--prune` defaults to **dry-run** (reports what would change). Requires `--apply` to execute. Reports counts by class. Does **not** re-classify groups or touch `resolved=1` groups.

### One-time recovery sequence

After both fixes are implemented and tested:

```bash
python poller/deduplicator.py --write          # links 206 orphaned photos, updates counts
python poller/deduplicator.py --prune --apply  # deletes zombie groups, fixes stale counts
```

Running `--write` first ensures any group that can have its orphaned siblings re-linked does so before `--prune` evaluates its count.

**Expected outcome:** zero groups in the `photo_count != linked_count` state; 206 previously-orphaned `candidate_public`/`needs_review` photos linked to their groups and visible in the UI.

---

## Testing

- `test_deduplicator.py`:
  - `test_is_snapbridge_pair_requires_dsc_prefix` — IMG_* pair with different fingerprints + dims → not Snapbridge
  - `test_is_edit_pair_iphone` — IMG_* pair with different fingerprints + dims → `edit_pair`
  - `test_is_edit_pair_dsc_excluded` — DSC_* pair falls through to Snapbridge, not edit_pair
  - `test_edit_pair_all_photos_in_review` — no photo placed in discards for edit_pair group
  - `test_write_groups_preserves_resolved` — re-running `_write_groups` on a resolved group keeps `resolved=1`
  - `test_prune_removes_zombie_groups` — groups with 0 or 1 linked photos are deleted
  - `test_prune_updates_stale_photo_count` — groups with ≥ 2 photos get corrected count
  - `test_prune_dry_run_makes_no_changes` — `--prune` without `--apply` writes nothing

---

## Out of scope

- **Embedding deduplicator in the poller cycle** — would self-heal orphaned photos automatically going forward. Deferred; see `docs/future-directions.md` and a dedicated GitHub issue.
- **Video group handling** — video duplicates (DSC_*.MP4/MOV from multi-import hardware issues) need separate treatment; not addressed here.
- **Stills-during-video auto-detection** — Nikon firmware assigns the video-start timestamp to stills captured during recording; these show as `uncertain` with `fingerprints=same`. Visual analysis would be required to auto-classify; currently routes to human review (correct behaviour).
- **Perceptual hashing / visual similarity** — already considered and declined; see `docs/future-directions.md`.
