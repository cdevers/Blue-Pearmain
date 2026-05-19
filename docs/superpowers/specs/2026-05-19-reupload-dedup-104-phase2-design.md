# Re-upload Dedup Phase 2: Privacy Enforcement — Design (#104)

**GitHub issue:** #104
**Depends on:** #17 (Phase 1 — detection + DB grouping)

---

## Scope

Phase 2 adds two enforcement operations to `bp dedup --flickr`:

1. **Mark** — set `privacy_state = 'duplicate_flickr'` on confirmed `reupload` discard records (DB only, no API calls, reversible).
2. **Delete** — call the Flickr API to permanently delete the discard photo, then mark the group resolved (irreversible).

Both operations act only on `group_type = 'reupload'` groups. `reupload_uncertain` groups are excluded; those wait for human review via the Phase 4 UI (#106).

Deferred to separate issues:
- **Phase 3:** Metadata sync from higher-res keeper to linked record (#105)
- **Phase 4:** UI cross-linking for `reupload_uncertain` groups (#106)

---

## Architecture

Two operations, two functions, one CLI flag each — all under `bp dedup --flickr`:

| Step | Flag | Function | Reversible? |
|------|------|----------|-------------|
| Mark | `--mark-discards [--apply]` | `_mark_reupload_discards()` (new) | Yes — DB only |
| Delete | `--delete-discards [--apply]` | `_delete_discards()` (fix existing) | No |

Both default to dry-run. `--apply` makes them live. `--flickr` is required for both.

The intended workflow is sequential:

```
bp dedup --flickr --write              # Phase 1: detect + group
bp dedup --flickr --mark-discards      # Phase 2a: dry-run preview
bp dedup --flickr --mark-discards --apply  # Phase 2a: mark DB records
# (review, spot-check)
bp dedup --flickr --delete-discards    # Phase 2b: dry-run preview
bp dedup --flickr --delete-discards --apply  # Phase 2b: delete from Flickr
```

---

## New function: `_mark_reupload_discards(conn, dry_run)`

### Query

```sql
SELECT p.id, p.flickr_id, p.privacy_state,
       dg.id AS group_id, dg.group_type, dg.notes
FROM photos p
JOIN duplicate_groups dg ON p.duplicate_group_id = dg.id
WHERE p.duplicate_role = 'discard'
  AND dg.group_type = 'reupload'
  AND p.privacy_state != 'duplicate_flickr'
  AND p.flickr_deleted = 0
  AND dg.resolved = 0
```

### Action (when not dry-run)

For each row, update `privacy_state` and `updated_at` in a single transaction:

```sql
UPDATE photos
SET privacy_state = 'duplicate_flickr',
    updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
WHERE id = ?
```

Does **not** set `resolved = 1` on the group — that is reserved for after Flickr deletion.

### Report

```
Reupload discards to mark: 541

  flickr_id=48922xxxxxx  candidate_public → duplicate_flickr
  flickr_id=48918xxxxxx  candidate_public → duplicate_flickr
  ... (10 shown, use --verbose to see all)

Dry run — no changes written. Use --apply to persist.
```

When `--apply` is active: `Marked 541 reupload discards as duplicate_flickr.`

### Return value

`int` — count of records marked (or eligible in dry-run).

---

## Fix to existing `_delete_discards(conn, client, dry_run)`

### Current bug

The query filters `privacy_state = 'approved_public'`. Reupload discards are
`candidate_public` before marking and `duplicate_flickr` after — so the current
query never finds them regardless of phase.

### Fix

Change the `WHERE` clause to:

```sql
WHERE p.duplicate_role = 'discard'
  AND dg.group_type = 'reupload'
  AND p.privacy_state = 'duplicate_flickr'
  AND (p.flickr_deleted IS NULL OR p.flickr_deleted = 0)
  AND dg.resolved = 0
```

Everything else in `_delete_discards()` is correct:
- `FlickrError(code=1)` (photo not found) treated as success
- `flickr_deleted = 1` set on the photo after deletion
- `resolved = 1` set on the group after deletion
- Per-row commit for durability

---

## CLI surface

```bash
# Mark reupload discards as duplicate_flickr (dry-run by default)
bp dedup --flickr --mark-discards
bp dedup --flickr --mark-discards --apply     # actually writes

# Delete from Flickr (now fixed to find duplicate_flickr discards)
bp dedup --flickr --delete-discards
bp dedup --flickr --delete-discards --apply
```

### Argument guards

New guard (mirrors existing `--delete-discards` guard):

```python
if args.mark_discards and not args.flickr:
    log.error("--mark-discards requires --flickr")
    sys.exit(1)
```

Existing guards preserved:
- `--delete-discards requires --flickr`
- `--apply requires --delete-discards or --mark-discards` (relax the existing single-flag check)

`--mark-discards` and `--delete-discards` are mutually exclusive in a single
invocation — running both in one command is an error.

---

## DB state transitions

| Phase | `privacy_state` | `flickr_deleted` | `resolved` |
|-------|----------------|-----------------|-----------|
| After Phase 1 `--write` | `candidate_public` | 0 | 0 |
| After `--mark-discards --apply` | `duplicate_flickr` | 0 | 0 |
| After `--delete-discards --apply` | `duplicate_flickr` | 1 | 1 |

---

## Edge cases

| Case | Handling |
|------|----------|
| `reupload_uncertain` group | Skipped by both operations |
| Already `duplicate_flickr` | Skipped by `--mark-discards` (idempotent) |
| `flickr_deleted = 1` already | Skipped by `--mark-discards`; treated as "already gone" by `--delete-discards` |
| `resolved = 1` group | Skipped by both |
| Flickr API error code 1 (not found) | Treat as success: `flickr_deleted = 1`, `resolved = 1` |
| Flickr API error code ≠ 1 | Log error, increment error count, continue |
| No `duplicate_group_id` on photo | Cannot happen — guard is the join |

---

## Files touched

| File | Change |
|------|--------|
| `poller/deduplicator.py` | Add `_mark_reupload_discards()`; fix `_delete_discards()` query; wire `--mark-discards` in `main()` |
| `tests/test_deduplicator.py` | Add `TestMarkReuploaDiscards`; add one test for fixed `_delete_discards()` query |

---

## Tests

### `TestMarkReuploaDiscards` (in-memory SQLite)

The existing `_make_db()` helper only creates `photos`. These tests also need `duplicate_groups`, so a new `_make_db_with_groups()` helper is added that creates both tables with the minimal columns needed (`id`, `match_key`, `group_type`, `resolved`). The existing `_insert()` helper is reused.

| Test | Scenario | Expected |
|------|----------|----------|
| `test_marks_reupload_discards` | `reupload` group, discard `candidate_public`, `flickr_deleted=0`, `resolved=0` | `privacy_state = 'duplicate_flickr'`; returns 1 |
| `test_skips_uncertain_groups` | `reupload_uncertain` group | no DB change; returns 0 |
| `test_skips_already_marked` | discard already `duplicate_flickr` | no DB change; returns 0 |
| `test_skips_flickr_deleted` | `flickr_deleted = 1` | no DB change; returns 0 |
| `test_dry_run_no_changes` | `dry_run=True`, eligible record | DB unchanged; returns 1 (eligible count) |
| `test_skips_resolved_groups` | `resolved = 1` on group | no DB change; returns 0 |

### Fix verification for `_delete_discards()`

| Test | Scenario | Expected |
|------|----------|----------|
| `test_delete_discards_finds_duplicate_flickr` | discard `privacy_state='duplicate_flickr'`, `flickr_deleted=0`, `group_type='reupload'` | the SELECT query inside `_delete_discards()` (extracted as a helper or reproduced inline in the test) returns the row |

The test calls the query directly against an in-memory DB (not `_delete_discards()` itself, which would require a live Flickr client). This confirms the WHERE clause fix without mocking the API.
