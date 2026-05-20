# Re-upload Dedup Phase 3: Metadata Sync from Keeper to Linked Record — Design (#105)

**GitHub issue:** #105
**Status:** ✓ done
**Depends on:** #17 (Phase 1 — detection + DB grouping), #104 (Phase 2 — mark/delete discards)

---

## Scope

Phase 3 handles the case where a `reupload` group's keeper is the Flickr-only orphan (higher-res,
no `uuid`). After Phase 2 deletes the lower-res linked record's Flickr photo, Phase 3 transfers
the orphan's entire Flickr presence to the linked record and soft-deletes the orphan via
`merged_into_id`. The linked record emerges as the canonical record holding both `uuid` and
`flickr_id`.

This is a DB-only operation — no Flickr API calls.

`reupload_uncertain` groups are excluded; those wait for human review.

---

## Architecture

One new function `_sync_keeper_metadata(conn, dry_run, verbose)` in `poller/deduplicator.py`.
One new CLI flag `--sync-metadata` under `bp dedup --flickr`.

| Step | Flag | Function | Reversible? |
|------|------|----------|-------------|
| Sync metadata | `--sync-metadata [--apply]` | `_sync_keeper_metadata()` (new) | No — `merged_into_id` soft-delete is permanent |

Intended workflow:
```
bp dedup --flickr --write                        # Phase 1: detect + group
bp dedup --flickr --mark-discards --apply        # Phase 2a: mark discards
bp dedup --flickr --delete-discards --apply      # Phase 2b: delete from Flickr
bp dedup --flickr --sync-metadata                # Phase 3: dry-run preview
bp dedup --flickr --sync-metadata --apply        # Phase 3: transfer + soft-delete
```

---

## Query

```sql
SELECT
    k.id AS keeper_id,
    k.flickr_id AS keeper_flickr_id,
    k.flickr_secret, k.flickr_server, k.flickr_farm,
    k.flickr_title, k.flickr_description,
    k.flickr_tags, k.flickr_tags_hash, k.flickr_last_updated,
    k.width AS keeper_width, k.height AS keeper_height,
    k.thumbnail_path AS keeper_thumb,
    d.id AS linked_id,
    d.original_filename AS linked_filename,
    dg.id AS group_id
FROM photos k
JOIN duplicate_groups dg ON k.duplicate_group_id = dg.id
JOIN photos d ON d.duplicate_group_id = dg.id
WHERE k.duplicate_role = 'keeper'
  AND k.uuid IS NULL
  AND d.duplicate_role = 'discard'
  AND d.uuid IS NOT NULL
  AND d.flickr_deleted = 1
  AND dg.group_type = 'reupload'
  AND dg.resolved = 0
```

---

## Action (when `--apply`)

Three writes per group in a single transaction:

```sql
-- 1. Transfer orphan's Flickr presence to linked record
UPDATE photos
SET flickr_id         = ?,
    flickr_secret     = ?,
    flickr_server     = ?,
    flickr_farm       = ?,
    flickr_title      = ?,
    flickr_description = ?,
    flickr_tags       = ?,
    flickr_tags_hash  = ?,
    flickr_last_updated = ?,
    width             = ?,
    height            = ?,
    thumbnail_path    = ?,
    flickr_deleted    = 0,
    updated_at        = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
WHERE id = ?   -- linked (discard) record

-- 2. Soft-delete the orphan
UPDATE photos
SET merged_into_id = ?,
    updated_at     = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
WHERE id = ?   -- orphan (keeper) record

-- 3. Resolve the group
UPDATE duplicate_groups
SET resolved    = 1,
    resolved_at = datetime('now')
WHERE id = ?
```

`flickr_deleted = 0` is set on the linked record because it now owns a live Flickr photo
(the orphan's high-res copy). The orphan's old `flickr_id` column is not cleared — the
`merged_into_id` soft-delete marker is sufficient to exclude it from normal queries.

---

## CLI Surface

```bash
# Dry-run (default)
bp dedup --flickr --sync-metadata

# Execute
bp dedup --flickr --sync-metadata --apply
```

### Argument guards

```python
if args.sync_metadata and not args.flickr:
    log.error("--sync-metadata requires --flickr")
    sys.exit(1)
```

`--sync-metadata` is mutually exclusive with `--mark-discards` and `--delete-discards`.

`--apply` guard extended: requires `--delete-discards`, `--mark-discards`, or `--sync-metadata`.

---

## Report Format

Dry-run:
```
Reupload groups eligible for metadata sync: 12

  group_id=45  flickr_id=48910000 → linked id=1234 (IMG_001.JPG)
  group_id=46  flickr_id=48920001 → linked id=1235 (IMG_002.JPG)
  ... (10 shown, use --verbose to see all)

Dry run — no changes written. Use --apply to persist.
```

After `--apply`: `Synced metadata for 12 reupload groups.`

Return value: `int` — count of groups synced (or eligible in dry-run).

---

## DB State Transitions

| Field | linked record (before) | linked record (after) |
|-------|----------------------|-----------------------|
| `flickr_id` | old deleted Flickr ID | orphan's flickr_id |
| `flickr_deleted` | 1 | 0 |
| `flickr_title/description/tags` | stale or NULL | orphan's values |
| `width` / `height` | lower-res | orphan's (higher-res) |
| `thumbnail_path` | old or NULL | orphan's thumbnail |
| orphan `merged_into_id` | NULL | linked record's id |
| group `resolved` | 0 | 1 |

---

## Edge Cases

| Case | Handling |
|------|----------|
| Keeper has `uuid` (linked is keeper) | Skipped — sync only needed when orphan is keeper |
| Discard `flickr_deleted = 0` | Skipped — old Flickr photo still live; unsafe to transfer `flickr_id` |
| `reupload_uncertain` group | Skipped — only confirmed `reupload` groups |
| `resolved = 1` group | Skipped by WHERE clause |
| Orphan has NULL `flickr_title` / tags | Written as NULL; metadata sync pipeline can fill later |
| No eligible groups | Reports 0; exits cleanly |

---

## Tests

New class `TestSyncKeeperMetadata` in `tests/test_deduplicator.py`, using the existing
`_make_db_with_groups()` helper.

| Test | Scenario | Expected |
|------|----------|----------|
| `test_syncs_all_fields_to_linked` | reupload group, orphan keeper (uuid=NULL), linked discard (uuid set, flickr_deleted=1) | All 12 Flickr fields on linked match orphan's; `flickr_deleted=0` on linked; `merged_into_id=linked.id` on orphan; group `resolved=1`; returns 1 |
| `test_skips_when_keeper_is_linked` | reupload group, linked is keeper (uuid set) | No DB change; returns 0 |
| `test_skips_when_discard_not_deleted` | orphan keeper, linked discard with `flickr_deleted=0` | No DB change; returns 0 |
| `test_skips_uncertain_groups` | `reupload_uncertain` group | No DB change; returns 0 |
| `test_skips_resolved_groups` | `resolved=1` on group | No DB change; returns 0 |
| `test_dry_run_no_changes` | eligible group, `dry_run=True` | DB unchanged; returns 1 |

---

## Files Touched

| File | Change |
|------|--------|
| `poller/deduplicator.py` | Add `_sync_keeper_metadata()`; wire `--sync-metadata` in `main()` |
| `tests/test_deduplicator.py` | Add `TestSyncKeeperMetadata` (6 tests) |
