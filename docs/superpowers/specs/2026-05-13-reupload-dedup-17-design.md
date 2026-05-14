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

Important constraints that shape the design:

1. **Resolution is not a reliable primary signal for candidate loading.** Snapbridge
   explicitly generates low-res proxies with different dimensions. Dimension data may
   also be absent for many records until the scanner backfill runs.

2. **Nikon firmware bug.** Still images captured during a video recording session all
   receive the same `date_taken` timestamp as the first frame of the video. A
   timestamp-only match could therefore produce false-positive groups from unrelated
   photos sharing a coincident timestamp.

3. **The linked record is not always the high-res original.** For photos taken during
   the iPhoto era (before iCloud Photo Library), the full-res image may still live on
   the old computer and have never been imported into iCloud Photos. In those cases the
   record with a `uuid` in BP may actually be the Snapbridge *low-res* proxy — the one
   that made it into iCloud — while the high-res original is a Flickr-only record.
   Issue #12 (iPhoto merge) will surface more of these cases. Neither side should be
   presumed canonical without evidence.

---

## Architecture

### New function: `_fetch_reupload_candidates()`

Added to `poller/deduplicator.py` alongside the existing
`_fetch_duplicate_candidates()`. The two functions are independent; `--flickr` mode
calls only the new function.

**Load strategy:** consistent with `link_orphans.py` — both sides loaded into memory
and matched in Python (dict lookups), avoiding a full SQL cross-join across hundreds
of thousands of rows.

Explicit indexing structure to avoid accidental O(n²) behaviour:

```python
# keyed by (normalised_filename, utc_second)
by_filename_ts: dict[tuple[str | None, str], list[PhotoRow]]
# keyed by utc_second alone (fallback)
by_ts_only: dict[str, list[PhotoRow]]
```

**Timestamp normalisation:** `date_taken` strings are parsed and converted to UTC,
then truncated to whole-second precision (not rounded — truncation matches the existing
behaviour in `normalise_dt()` in `scanner.py`). The resulting key format is
`"YYYY-MM-DD HH:MM:SS"` in UTC. Both sides of the join must use identical
normalisation to avoid false misses from sub-second or timezone differences.

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

**Timestamp-only fallback:** when `original_filename IS NULL` on either record, a
timestamp-only match is attempted but the result is **always classified
`reupload_uncertain`** regardless of ID gap. A NULL filename removes the primary
false-positive guard; auto-grouping is too risky.

### Upload-session gap classification

```python
CROSS_SESSION_THRESHOLD = 100_000  # Flickr IDs

upload_session_gap = abs(int(flickr_only.flickr_id) - int(linked.flickr_id))
```

The gap is evidence of **separate upload sessions**, not of duplication itself. A
large gap combined with a filename+timestamp match produces a confident pair; the gap
alone means nothing.

| Condition | Classification |
|-----------|---------------|
| filename+timestamp match AND gap > `CROSS_SESSION_THRESHOLD` | `reupload` — auto-grouped |
| filename+timestamp match AND gap ≤ `CROSS_SESSION_THRESHOLD` | `reupload_uncertain` |
| timestamp-only match (NULL filename), any gap | `reupload_uncertain` |

Note: session gaps of 6–12 months produce gaps in the hundreds of millions; 100 K is
conservative by design.

### Keeper determination

Resolution-first with a bias toward the linked record within a threshold:

```python
REUPLOAD_KEEPER_PIXEL_RATIO = 1.5
```

1. **Both records have valid dimensions** (`width > 0` and `height > 0` on both):
   - Compute `ratio = max_pixels / min_pixels` (both non-zero by construction)
   - If ratio ≥ `REUPLOAD_KEEPER_PIXEL_RATIO`: the dramatically-larger record is the
     keeper (strong signal it is the full-res original), regardless of which side is
     linked
   - If ratio < `REUPLOAD_KEEPER_PIXEL_RATIO`: linked record wins conservatively;
     group classified `reupload_uncertain`
2. **Only the linked record has valid dimensions:** linked record is tentatively the
   keeper; group classified `reupload_uncertain`
3. **Only the orphan has valid dimensions:** linked record is still the tentative
   keeper — a Flickr-only orphan is never auto-promoted solely from its own unilateral
   dimension data (a garbage metadata row with bogus dimensions should not steal
   keeper status); group classified `reupload_uncertain`
4. **Neither record has valid dimensions, or either has `width=0` / `height=0`:**
   linked record is the keeper (conservative default); group classified
   `reupload_uncertain`; `keeper_assumed: true` in notes

`REUPLOAD_KEEPER_PIXEL_RATIO = 1.5` is the initial threshold. Snapbridge proxies are
often dramatically smaller than full-res Nikon originals, so this may prove
conservative — expect tuning after real-world runs.

The linked record (Photos-imported) is *usually* the full-res import, but this is not
guaranteed — see the iPhoto background note above. Resolution data wins when the
evidence is clear (ratio ≥ 1.5×); within that band the linked record is preferred.

### Collision handling

Collisions in either direction are classified `reupload_uncertain` regardless of gap
or resolution evidence. No auto-grouping fires for any ambiguous match.

- **One linked record → multiple Flickr-only records:** Nikon firmware scenario —
  multiple stills share the same timestamp. All candidate orphans flagged uncertain.
- **One Flickr-only record → multiple linked records:** rare (photo imported into
  Photos more than once). The orphan is flagged uncertain; correct counterpart
  requires human review.

Even if one candidate appears to win clearly on resolution, `linked_match_count > 1`
or `orphan_match_count > 1` forces `reupload_uncertain`. Multi-candidate situations
are where unexpected edge cases cluster; human confirmation is required.

A future `reupload_multi` group type could handle the case where one linked record has
two clearly low-res orphans (both large gap, both smaller resolution) — but this is
explicitly out of scope for Phase 1.

---

## DB state after `--write`

The existing `duplicate_groups` table and `photos` columns (`duplicate_group_id`,
`duplicate_role`) are reused without schema changes.

### `duplicate_groups` row

| Column | Value |
|--------|-------|
| `match_key` | `"reupload:{smaller_flickr_id}:{larger_flickr_id}"` — always ordered smallest-first, independent of which record is keeper, to prevent reversed-key collisions across runs |
| `group_type` | `'reupload'` or `'reupload_uncertain'` |
| `keeper_id` | `photos.id` of the keeper record |
| `photo_count` | `2` |
| `notes` | JSON evidence blob + human-readable summary (see below) |
| `resolved` | `0` (Phase 2 flips this when it acts on privacy) |

Using Flickr IDs in the match key (rather than filename+timestamp strings) makes
idempotency deterministic and avoids timezone-normalisation instability.

### Structured evidence in `notes`

```json
{
  "keeper_flickr_id": "48922xxxxxx",
  "discard_flickr_id": "54060xxxxxx",
  "filename_match": true,
  "timestamp_delta_s": 1,
  "upload_session_gap": 512345678,
  "dimension_ratio": 4.2,
  "linked_match_count": 1,
  "orphan_match_count": 1,
  "keeper_assumed": false,
  "summary": "DSC_0042.JPG | 2022-08-14T10:23:11 | linked flickr_id=48922xxxxxx → orphan flickr_id=54060xxxxxx | gap=512345678 | ratio=4.2×"
}
```

- `keeper_flickr_id` / `discard_flickr_id`: explicit copies of the Flickr IDs even
  though they appear in the match key, to make later analysis and export easier.
- `linked_match_count`: number of linked records that matched this orphan's key
  (> 1 means a collision on the linked side).
- `orphan_match_count`: number of orphans that matched this linked record's key
  (> 1 means a Nikon-firmware-style timestamp collision).
- `keeper_assumed`: `true` when keeper was chosen by fallback (no usable dimensions)
  rather than by resolution evidence.
- `summary`: human-readable log line for the report and `--verbose` output. Downstream tooling must not parse this field — use the structured fields above instead.

### `photos` updates

| Record | `duplicate_group_id` | `duplicate_role` | `privacy_state` |
|--------|----------------------|-----------------|-----------------|
| Keeper | X | `'keeper'` | unchanged |
| Discard | X | `'discard'` | unchanged |

`privacy_state` is **not** modified in Phase 1. The discard stays `candidate_public`
until Phase 2 acts on it.

### Already-grouped records

If either record already has a `duplicate_group_id`, the pair is **skipped** — no
existing group is overwritten. These conflicts are collected and printed as a dedicated
**Conflicts** section in the report so they are visible rather than silently dropped.
A future `--reconcile-groups` flag could re-evaluate them.

### Idempotency

The existing `ON CONFLICT(match_key) DO UPDATE` upsert in `_write_groups()` handles
re-runs. The Flickr-ID-based match key makes this deterministic across runs even if
timestamps are re-normalised.

### Transaction

All pairs written atomically in a single `BEGIN` / `COMMIT` block, with `ROLLBACK` on
any error — consistent with the existing deduplicator.

---

## CLI surface

New `--flickr` flag added to the existing `deduplicator.py` entry point:

```bash
bp dedup --flickr --dry-run           # find pairs, print report, no writes (default)
bp dedup --flickr --write             # find pairs and write to duplicate_groups
bp dedup --flickr --write --limit 200 # write at most 200 pairs (safe for first runs)
bp dedup --flickr --write --verbose   # also log each matched pair individually
bp dedup                              # existing filename+timestamp dedup (unchanged)
```

No `--confirm` flag in Phase 1 — there are no Flickr API calls.

`--limit N` caps the number of pairs written in a single run. Recommended for the
first few live runs to allow spot-checking before committing to the full dataset.

### Report format

```
Reupload pairs found: 583

  reupload            541 pairs   92.8%   (auto-grouped)
  reupload_uncertain   42 pairs    7.2%   (flagged — small gap, timestamp-only, or collision)

── REUPLOAD_UNCERTAIN (42 pairs) ──────────────────────────────────────
  DSC_0042.JPG | 2022-08-14T10:23:11
    linked:  flickr_id=48922xxxxxx  uuid=XXXX-...
    orphan:  flickr_id=48922xxxxxx+80  upload_session_gap=80  (small gap — possible burst)
  ...

── CONFLICTS (3 records already in a group) ───────────────────────────
  flickr_id=54060xxxxxx  already in duplicate_group_id=17  — skipped
  ...

Dry run — no changes written. Use --write to persist.
```

Confirmed `reupload` pairs are not listed individually at default verbosity.
`--verbose` dumps each one. Percentages are included to aid threshold tuning.

---

## Edge cases

| Case | Handling |
|------|----------|
| Negative gap (orphan ID < linked ID) | `abs()` for threshold; keeper by pixel ratio as usual |
| Already has `duplicate_group_id` | Skip, surface in Conflicts report section |
| NULL `flickr_id` on either side | Skip (filtered by query, but guarded defensively) |
| One linked → many orphans | All `reupload_uncertain` |
| One orphan → many linked | All `reupload_uncertain` |
| NULL filename on either side | Timestamp-only match → always `reupload_uncertain` |
| `width=0` or `height=0` on either record | Treated as no dimensions; keeper = linked, `reupload_uncertain` |
| No dimensions on either record | Keeper = linked record; `keeper_assumed: true` in notes |
| Only orphan has dimensions | Linked still wins; orphan never auto-promoted from unilateral data |
| Pixel ratio < 1.5× (similar sizes) | Linked wins conservatively; `reupload_uncertain` |

---

## Files touched

| File | Change |
|------|--------|
| `poller/deduplicator.py` | Add `_fetch_reupload_candidates()` + classification helpers; add `--flickr` CLI flag; extend `main()` to dispatch to new function |
| `tests/test_deduplicator.py` | Add `TestReuploadCandidates` test class (8 tests) |

---

## Tests

New class `TestReuploadCandidates` in `tests/test_deduplicator.py`, using the existing
`make_photo()` fixture:

| Test | Scenario | Expected |
|------|----------|----------|
| `test_exact_match_reupload` | Same filename + timestamp, gap > 100 K | `group_type='reupload'`, keeper=linked |
| `test_keeper_by_pixels_orphan_wins` | Both have valid dimensions; orphan > 1.5× pixels | keeper = orphan |
| `test_keeper_within_ratio_linked_wins` | Both have dimensions; ratio < 1.5× | keeper = linked; `reupload_uncertain` |
| `test_keeper_only_orphan_has_dims` | Only orphan has dimensions | keeper = linked; `reupload_uncertain` |
| `test_keeper_zero_dimension_treated_as_none` | One record has `width=0` | treated as no dimensions; keeper = linked |
| `test_small_gap_uncertain` | ID gap ≤ 100 K | `group_type='reupload_uncertain'` |
| `test_one_to_many_uncertain` | Two orphans match same linked record | both `reupload_uncertain` |
| `test_already_grouped_skipped` | Orphan already has `duplicate_group_id` | pair skipped, appears in conflicts |
| `test_null_filename_fallback_uncertain` | `original_filename` NULL on orphan | timestamp-only match → `reupload_uncertain` regardless of gap |
| `test_no_dimensions_defaults_to_linked` | Neither record has dimensions | keeper=linked, `keeper_assumed=true` in notes |

---

## Implementation notes

- Constants `CROSS_SESSION_THRESHOLD` and `REUPLOAD_KEEPER_PIXEL_RATIO` should be
  module-level named constants (not magic numbers inline) so they are easy to tune.
- Variable names in implementation should use `upload_session_gap` (not bare `gap`) to
  preserve the semantic meaning of the threshold.
- Index structures (`by_filename_ts`, `by_ts_only`) should be built explicitly before
  the match loop to guarantee O(n + m) behaviour.

---

## Implementation order

Phase 1 is self-contained and unblocks everything else:

1. Add `_fetch_reupload_candidates()` + classification logic (TDD: tests first)
2. Extend `main()` with `--flickr` flag + report
3. Update README test count

Phase 2 (privacy enforcement) and Phases 3–4 are tracked as separate issues.
