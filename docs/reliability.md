# Reliability

## Flickr API client

The Flickr API client uses exponential backoff with jitter on transient failures (HTTP 429/5xx, timeouts, connection errors), retrying up to 4 times with delays of approximately 1, 2, 4, and 8 seconds. HTTP 4xx errors (400, 401, 403, 404, etc.) are treated as permanent and raise immediately without retrying. Flickr application-level errors are classified as transient (codes 0, 106) or permanent, and handled accordingly. Every API call has a 30-second timeout.

## Write atomicity

Write operations (permissions and tags) only update the DB push flags after each operation succeeds individually — a failed tag push does not mark tags as pushed, and a failed permission change does not mark the photo as public. Both failures are logged with the Flickr photo ID for traceability. If Flickr's 75-tag limit is reached, the tag push is silently skipped and the permission change still proceeds normally.

If a photo has been manually deleted from Flickr since it was approved, the batch push logs a warning and marks the photo as done (so it is not retried on subsequent pushes) rather than counting it as a failure. The rest of the batch continues unaffected. The dashboard toast shows a `skipped` count for these cases alongside the usual `pushed` and `failed` counts.

`updated_at` is stamped on every write path — `upsert_photo`, `set_privacy_state`, `record_review`, and `undo_decision` — so the modification time of any row always reflects its true last-changed time regardless of how the change was made.

## File descriptor management

Each review decision with `push=True` spawns a background thread that opens its own SQLite connection (Flask's `teardown_appcontext` only runs on the request thread, not on background threads). The background thread closes its connection in a `finally` block so the file descriptor is released promptly when the thread exits. Without this, reviewing several dozen photos in quick succession would exhaust the OS file-descriptor limit (macOS default: 256) and crash the server with `OSError: [Errno 24] Too many open files`.

## Reconciliation

If you suspect a push operation failed silently, the reconciliation script checks your DB's expected state against what Flickr actually has:

```bash
bp reconcile          # Report mismatches (exit 1 if mismatches, 2 if API errors)
bp reconcile --fix    # Repair mismatches (exit 2 if any fix fails, 0 if all resolved)
bp reconcile --explain  # Show why each photo has pending drift (DB-only, no Flickr calls)
```

The structured summary output distinguishes checked, mismatched, fixed, and failed counts. Exit codes are differentiated: 0 = clean, 1 = mismatches found (without `--fix`), 2 = operational errors (API failures or fix failures). `bp poll` also exits non-zero if any auto-push Flickr write fails.

## Operation journal

All significant mutations are appended to the `operation_log` table: review decisions, proposal auto-applies, reconcile `--fix` writes, and tag writebacks. Each entry records the photo ID, operation type, old and new values, trigger, and actor.

**Fire-and-forget guarantee:** The journal write is always attempted after the main operation succeeds, in a separate `conn.commit()`. A failed journal INSERT (e.g. pre-migration DB, transient error) is silently swallowed — the main operation is never rolled back due to a journal failure. This means the audit trail is best-effort rather than authoritative: every logged entry reflects a mutation that actually happened, but not every mutation is guaranteed to appear in the log.

The log is queryable via `db.get_operation_log(photo_id=…, operation=…, limit=…)`.

## Config validation

Both the poller and reviewer validate required config fields at startup and exit immediately with a readable error message if anything is missing, rather than failing deep in a request.

## Database integrity

`privacy_state` has a SQL `CHECK` constraint enforcing valid values. Applied migrations are tracked in the `schema_migrations` table. All migration scripts live in `db/migrations/` and are idempotent — safe to re-run.

If you are upgrading an existing installation, run all migrations in order:

```bash
python db/migrations/migrate_001_privacy_state_check.py --config config/config.yml
python db/migrations/migrate_002_updated_at_and_indexes.py --config config/config.yml
python db/migrations/migrate_003_dimensions_and_dedup.py --config config/config.yml
python db/migrations/migrate_004_albums.py --config config/config.yml
python db/migrations/migrate_005_metadata_conflicts.py --config config/config.yml
python db/migrations/migrate_006_flickr_deleted.py --config config/config.yml
python db/migrations/migrate_007_metadata_cache.py --config config/config.yml
python db/migrations/migrate_008_review_queue_index.py --config config/config.yml
python db/migrations/migrate_009_placeholder.py --config config/config.yml
python db/migrations/migrate_010_stale_uuid.py --config config/config.yml
python db/migrations/migrate_011_folders.py --config config/config.yml
python db/migrations/migrate_012_flickr_name.py --config config/config.yml
python db/migrations/migrate_013_screenshot_flag.py --config config/config.yml
python db/migrations/migrate_014_merged_into_id.py --config config/config.yml
python db/migrations/migrate_015_album_removal.py --config config/config.yml
python db/migrations/migrate_016_friends_family.py --config config/config.yml
python db/migrations/migrate_017_tag_events_cascade.py --config config/config.yml
python db/migrations/migrate_018_pushed_tags.py --config config/config.yml
python db/migrations/migrate_019_person_policies.py --config config/config.yml
python db/migrations/migrate_020_operation_log.py --config config/config.yml
```

> **Note (migration 001):** If any photos have an unrecognised `privacy_state` value (e.g. from manual DB edits or a future code change), the migration will reset them to `needs_review` before adding the constraint. Check the output for any rows that are reset.
