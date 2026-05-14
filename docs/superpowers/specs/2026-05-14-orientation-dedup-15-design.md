# Orientation Duplicate Resolution — Design Spec (GH #15)

**GitHub issue:** #15

**Depends on:** #17 Phase 1 (`bp dedup --flickr` detection infrastructure)

---

## Problem

Approximately 55 `approved_public` records are orientation duplicates: the same photo
exists on Flickr twice — once as a Snapbridge upload (landscape, as-shot) and once as
a full-res upload from Photos (rotated to portrait). Issue #17 Phase 1 detects
re-upload pairs among `candidate_public` records only. Orientation duplicates sitting
in `approved_public` are invisible to that pass.

Additionally, `bp dedup --flickr` can detect a pair and write it to `duplicate_groups`,
but it never acts on the discard — there is currently no path from "grouped" to
"deleted from Flickr."

This issue adds two independent capabilities to `bp dedup --flickr`:

1. **`--include-approved`** — extends the detection query to include `approved_public`
   Flickr-only records, catching orientation duplicates.
2. **`--delete-discards`** — acts on already-grouped pairs (from any prior `--write`
   run) by calling `flickr.photos.delete` on the discard record.

---

## Scope

Both additions are surgical extensions to existing `--flickr` code paths. No new DB
schema. No new tables. `duplicate_groups`, `duplicate_role`, `duplicate_group_id`, and
`flickr_deleted` all exist.

---

## Addition 1 — Extended detection (`--include-approved`)

### CLI

```bash
bp dedup --flickr --include-approved --dry-run   # detect only (default)
bp dedup --flickr --include-approved --write      # detect and write to duplicate_groups
```

`--include-approved` is only meaningful with `--flickr`. Combining it with the
existing plain `bp dedup` path is an error (print message, exit 1).

### Query change

Current left side (candidates):

```sql
WHERE uuid IS NULL
  AND flickr_id IS NOT NULL
  AND privacy_state = 'candidate_public'
```

With `--include-approved`:

```sql
WHERE uuid IS NULL
  AND flickr_id IS NOT NULL
  AND privacy_state IN ('candidate_public', 'approved_public')
```

The right side (linked records: `uuid IS NOT NULL AND flickr_id IS NOT NULL`) is
unchanged.

### Classification

Orientation duplicates are Snapbridge uploads — `original_filename` is typically NULL
on the Snapbridge side. The existing timestamp-only fallback applies and always
classifies as `reupload_uncertain`. This is correct: human review is appropriate
before any deletion action.

No new classification logic is required. The existing `reupload_uncertain` path handles
these records exactly as intended.

### Report addition

When `--include-approved` is active, the report header gains a line:

```
Detection scope: candidate_public + approved_public (--include-approved)
```

Everything else in the existing report format is unchanged.

---

## Addition 2 — Flickr deletion (`--delete-discards`)

### CLI

```bash
bp dedup --flickr --delete-discards               # dry run (default): report what would be deleted
bp dedup --flickr --delete-discards --apply       # execute: call flickr.photos.delete
```

`--delete-discards` is independent of detection — it reads `duplicate_groups` and acts
on groups that were written by any prior `--write` run. No detection pass runs when
`--delete-discards` is given; the two operations are separate invocations.

`--dry-run` is the default; `--apply` is required to make any Flickr API calls. This
is intentional: users are expected to run `--dry-run` first and inspect the report.

### Candidate query

```sql
SELECT p.id, p.flickr_id, p.privacy_state,
       dg.id AS group_id, dg.group_type, dg.notes
FROM photos p
JOIN duplicate_groups dg ON p.duplicate_group_id = dg.id
WHERE p.duplicate_role = 'discard'
  AND p.privacy_state = 'approved_public'
  AND (p.flickr_deleted IS NULL OR p.flickr_deleted = 0)
  AND dg.resolved = 0
```

`privacy_state = 'approved_public'` is the safety filter: only act on discards whose
Flickr presence was deliberately approved. `candidate_public` discards have not been
approved for public visibility and should not be deleted via this path (they belong to
Phase 2 of #17).

### Per-record action (`--apply`)

For each candidate:

1. Call `client.delete_photo(flickr_id)` (new method, see below).
2. On success: call `db.mark_flickr_deleted(photo_id)` (existing method).
3. On Flickr error 1 (photo not found — already gone): treat as success; call
   `db.mark_flickr_deleted(photo_id)` and log a note.
4. On any other Flickr error: log the error, continue to next record (do not abort
   the whole batch).

After all records are processed, mark the `duplicate_groups` row as resolved:

```sql
UPDATE duplicate_groups SET resolved = 1 WHERE id = ?
```

Only mark resolved when the group's discard has been successfully flagged
`flickr_deleted`. If the delete call fails, leave `resolved = 0`.

### New `FlickrClient.delete_photo()`

```python
def delete_photo(self, photo_id: str) -> None:
    """Call flickr.photos.delete. Raises FlickrError on failure."""
    self._call("flickr.photos.delete", photo_id=photo_id)
```

Uses the existing `_call()` / `_retry()` infrastructure, identical in pattern to
`set_permissions()`, `rotate()`, etc.

### Dry-run report format

```
Discards eligible for deletion: 47

  flickr_id=54060xxxxxx  group_type=reupload_uncertain  privacy=approved_public
    group notes: DSC_0042.JPG | 2022-08-14T10:23:11 | ...
  ...

Dry run — no Flickr API calls made. Use --apply to delete.
```

### Apply report format

```
Discards to delete: 47

  ✓ deleted  flickr_id=54060xxxxxx
  ✓ deleted  flickr_id=...
  ! already gone (Flickr error 1)  flickr_id=...
  ✗ error (code 99 — Flickr permission error)  flickr_id=...

Done: 45 deleted, 1 already gone, 1 error
```

---

## Files touched

| File | Change |
|------|--------|
| `poller/deduplicator.py` | Add `--include-approved` flag + query extension; add `--delete-discards` + `--apply` flags + new `_delete_discards()` function |
| `flickr/flickr_client.py` | Add `delete_photo(photo_id: str)` method |
| `tests/test_deduplicator.py` | New tests for `--include-approved` scope extension; new `TestDeleteDiscards` class |
| `tests/test_core.py` | New test for `delete_photo()` (in existing `TestFlickrCollectionsClient` or a new sibling class) |

---

## Tests

### `--include-approved` tests (add to existing `TestReuploadCandidates`)

| Test | Scenario | Expected |
|------|----------|----------|
| `test_include_approved_adds_approved_public` | One `candidate_public` + one `approved_public` orphan; `--include-approved` active | both appear in detection results |
| `test_include_approved_off_excludes_approved_public` | Same records; `--include-approved` NOT active | `approved_public` orphan excluded |
| `test_include_approved_null_filename_uncertain` | `approved_public` orphan with NULL filename matches by timestamp | classified `reupload_uncertain` |

### `TestDeleteDiscards` (new class)

| Test | Scenario | Expected |
|------|----------|----------|
| `test_dry_run_no_api_calls` | Two eligible discards; `--apply` not set | report lists both; `FlickrClient.delete_photo` never called |
| `test_apply_success` | One eligible discard; `--apply` set; `delete_photo` succeeds | `flickr_deleted = 1` in DB; `duplicate_groups.resolved = 1` |
| `test_apply_error_1_treated_as_success` | `delete_photo` raises `FlickrError(1, ...)` | treated as already gone; `flickr_deleted = 1`; `resolved = 1` |
| `test_apply_other_error_leaves_record` | `delete_photo` raises `FlickrError(99, ...)` | `flickr_deleted` unchanged; `resolved = 0`; logged as error |
| `test_candidate_public_discard_excluded` | Discard has `privacy_state = 'candidate_public'` | not included in delete-discards query |
| `test_already_deleted_excluded` | Discard has `flickr_deleted = 1` | excluded from query |
| `test_already_resolved_excluded` | Group has `resolved = 1` | excluded from query |

### `FlickrClient.delete_photo()` test (add to `tests/test_core.py`)

| Test | Scenario | Expected |
|------|----------|----------|
| `test_delete_photo_calls_api` | Mock `_call`; call `delete_photo("123")` | `_call` invoked with `"flickr.photos.delete"`, `photo_id="123"` |

---

## Edge cases

| Case | Handling |
|------|----------|
| `--include-approved` + plain `bp dedup` (no `--flickr`) | Print error, exit 1 |
| `--delete-discards` + `--write` in same invocation | Print error, exit 1 — these are separate operations |
| Discard is already `flickr_deleted = 1` | Excluded by query; not double-processed |
| Group already `resolved = 1` | Excluded by query |
| Flickr photo not found (error 1) | Treated as success — photo is already gone |
| Any other Flickr error during `--apply` | Log, continue; record left undeleted |
| No eligible discards found | Print "No discards eligible for deletion" and exit 0 |
