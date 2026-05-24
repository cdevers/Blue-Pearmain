# Test coverage inventory

Run the suite:

```bash
python -m pytest tests/ -q
```

1102 tests. Coverage is grouped below by area.

---

## Privacy classification

- Privacy classifier logic (scene labels, GPS, face detection → privacy state)
- Tagger (tag proposal generation from Apple ML labels and location data)
- Screenshot classification: migration 013, scanner persistence, `stats()` screenshot counts, review filter pseudo-states
- Screenshot review UI: `candidate_public` excludes screenshots, screenshot badge on cards, confirm-private button and hint in screenshot queues

## Database

- Database access layer (`db.py`)
- Schema migrations 001–013: idempotency, dry-run support, CHECK constraints, index creation, new columns
- `schema_migrations` tracking table
- WAL checkpoint: `wal_autocheckpoint` pragma, TRUNCATE and PASSIVE modes
- `privacy_state` CHECK constraint enforcement

## Scanner (Apple Photos)

- Scanner matching: Photos record upsert, capture-timestamp linking
- Scanner Photos metadata cache writes: `photos_title`, `photos_description`, `photos_tags`/hash, `meta_synced_photos_at`, skip-condition update
- Screenshot classification persistence

## Flickr client

- Retry/backoff/jitter on transient failures (HTTP 429, 5xx, timeouts)
- 4xx permanent-failure classification (no retry)
- Max-tags (75) handling: tag push silently skipped, permission change proceeds
- Rotation API
- Photoset/collection title methods: `get_photosets_titled`, `get_collections_flat`, `edit_photoset_meta`, `edit_collection_meta`

## Polling and cache writes

- Flickr metadata cache writes: `flickr_title`, `flickr_tags` JSON/hash, `flickr_last_updated`, `meta_synced_flickr_at`
- DB-cache-first reads in sync-metadata: cache hit/miss logic, API call avoidance

## Metadata sync pipeline

- Sync engine: `classify_tags`, `classify_text_field`, `run_sync_engine`, `upsert_proposal`, hash-match supersede
- Drift filter with `--force` bypass
- Metadata-sync batch behaviour: PhotosDB caching, progress logging, `flickr_deleted` detection
- Proposal applier: `apply_proposal`, `apply_batch`, `_count_pending`, `apply_collision_reverse`, `set_photo_text`, stale-uuid termination, staleness/drift re-checks, title/description apply
- Proposal API routes: approve returns ok/not-ok based on Photos responsiveness; approve resolves collision sibling; approve-reverse writes Photos value to Flickr; bulk-approve for non-conflict and divergence batches
- Collision reverse: works even when the Photos→Flickr sibling has been superseded by a sync run
- Prune-proposals: supersede spurious managed-tag proposals, delete old resolved proposals, dry-run mode

## Deduplication

- Duplicate detection logic: Snapbridge (DSC_* prefix required), edit_pair (non-DSC_* original+edit), device-upload, and uncertain classification
- `_is_snapbridge_pair` DSC_ filename guard: IMG_* pairs are not Snapbridge even with different fingerprints/dimensions
- `_is_edit_pair`: non-DSC_* pairs with differing fingerprints and pixel counts → `edit_pair`; all photos placed in review (no auto-discard)
- `_write_groups` preserve-resolved invariant: re-running `--write` does not reset `resolved=1` groups
- `_prune_stale_groups`: zombie groups (0–1 linked photos) deleted + dangling FKs cleared; stale `photo_count` repaired; dry-run by default; safety-net sweep for dangling `duplicate_group_id` refs
- Post-prune invariants: no unresolved group with < 2 linked photos; `photo_count` matches actual linked count; no photo with dangling `duplicate_group_id`
- Duplicates UI merge action: merging Flickr-only donor records into Photos-linked recipients to reconcile split records
- `local_duplicate` classifier: same-fingerprint groups (same image imported into Apple Photos multiple times, null/empty fingerprints excluded); all photos placed in review, no keeper assigned; waterfall invariant (local_duplicate fires before device_upload)

## Orphan linking

- Photos/Flickr record merging (including `tag_events` migration)
- Orphan-linking by capture timestamp
- Link-orphans cross-timezone matching: integer hour offsets ±1–12 h to catch camera-timezone ≠ machine-timezone gaps

## Album and collection sync

- Sync-album-collections: folder tree reading, Collection creation, `editSets` API calls, dry-run mode, `--remove` with confirmation
- Name-sync baseline tracking: `flickr_name` column, `set_album_flickr_name`, `set_folder_flickr_name`
- Sync-names-from-flickr: rename detection, Photos-wins conflict policy, dry-run, AppleScript rename, folder/collection renames

## Thumbnail serving

- `derivative_path`: tries three candidate paths in order — `resources/derivatives/masters/{shard}/`, `resources/derivatives/{shard}/`, and `scopes/momentshared/resources/derivatives/masters/{shard}/`; returns first that exists on disk, or None
- `/thumb/<id>` live fallback: Photos-only records with no `thumbnail_path` resolved at request time via `derivative_path`; real path written back to DB on hit; `"__none__"` sentinel written on miss so future requests skip filesystem probing; OSError guard prevents library-inaccessibility crashes

## Review UI

- Batch person actions (All private / All public)
- Reviewer "Open in Photos" API endpoint
- Proposal API routes (see Metadata sync pipeline above)

## CLI and infrastructure

- `bp` CLI entry point: subcommand dispatch, flag forwarding
- `bp all` step sequencing and error isolation
- Daemon install/uninstall: plist generation, path substitution, dry-run
- Thumbnail URL preference: `url_m` preferred over `url_l` to reduce cache size
- mDNS/Bonjour registration: `_start_mdns` registers `_http._tcp.local.`; skips on localhost binding or no LAN IP; handles missing zeroconf gracefully
- `bp ui --host` flag: forwarded from `bp` subparser to `reviewer/app.py`
- Reconcile exit codes and precedence

## Reliability

- Background-thread file-descriptor lifecycle: SQLite connection opened and closed in `finally` block per push thread
- Photos hang prevention: `_photos_is_responsive` liveness probe with `pgrep` guard; `_run_with_timeout` ThreadPoolExecutor wrapper with non-blocking shutdown; JS `AbortController` + try/catch with `origText` restoration
- Metadata-sync hang prevention: `metadata_puller._photos_is_responsive` migrated to `pgrep` + `osascript` probe pattern, replacing old System Events AppleScript check
