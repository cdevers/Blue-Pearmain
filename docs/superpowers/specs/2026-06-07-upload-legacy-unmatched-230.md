# Upload legacy-only assets to Flickr — spec (#230)

## Problem

`bp legacy-report` (#229) identifies legacy assets (iPhoto/Photos 4 library) whose wall-clock timestamp matches no photo in BP's `photos` table. These are photos that have never been on Flickr and are unknown to BP. This spec covers uploading them directly from their iPhoto file path, bypassing Apple Photos and iCloud entirely.

## Scope

One-shot manual command. Not wired into `bp all`. If that integration makes sense later it can be added separately.

## Architecture overview

Three layers, consistent with the existing `legacy_apply.py` pattern:

1. **`FlickrClient.upload_photo()`** — new method on the existing Flickr client. Handles the multipart POST and XML response specific to the upload endpoint.
2. **`poller/legacy_uploader.py`** — pure orchestration: iterate unmatched assets, classify, upload, write DB records, log. No Flickr or file I/O concerns leak into the tests.
3. **`bp upload-legacy-unmatched`** — CLI command that reads config, wires all components together, prints a summary.

## FlickrClient.upload_photo()

Flickr uploads use a different endpoint and response format from all other API calls:

- **Endpoint:** `https://up.flickr.com/services/upload/` (multipart POST)
- **Auth:** same `OAuth1Session` already on the client — no new credentials
- **Response:** XML, not JSON. Success: `<rsp stat="ok"><photoid>XXXXXX</photoid></rsp>`

The method does **not** go through `_call()`. It posts directly via `self._session.post()` with `files=` for the photo binary and `data=` for the metadata fields. It parses `<photoid>` from the XML response using `xml.etree.ElementTree`. On failure (non-ok `stat` or HTTP error) it raises `FlickrApiError` with the error message from the XML.

Signature:
```python
def upload_photo(
    self,
    path: Path,
    title: str = "",
    description: str = "",
    tags: str = "",          # space-separated
    is_public: int = 0,
    is_friend: int = 0,
    is_family: int = 0,
) -> str:                    # returns flickr_id
```

**All uploads are private** (`is_public=0, is_friend=0, is_family=0`). Privacy is determined by BP's privacy pipeline, not by the upload call.

After upload, the method calls `flickr.photos.setDates` (via `_call()`) to set `date_taken` from the legacy asset. If `setDates` fails, log a warning but do not roll back — the photo exists on Flickr with correct content; `bp sync-metadata` can fix the date later.

## DB schema changes — migration 031

Two new columns on `legacy_assets` to support idempotent re-runs:

```sql
ALTER TABLE legacy_assets ADD COLUMN uploaded_flickr_id TEXT;
ALTER TABLE legacy_assets ADD COLUMN uploaded_at TEXT;
```

`uploaded_flickr_id` is set immediately after a successful Flickr upload, before the `photos` row is created. This makes the idempotency key the `asset_uuid` rather than the wall-clock timestamp, avoiding false matches when multiple assets share a timestamp (burst shots) or when a `photos` row write fails after upload.

## legacy_uploader.py — orchestration

```python
def upload_unmatched_assets(
    db,
    library_uuid: str,
    library_path: Path,
    flickr_client,
    *,
    self_name: str,
    zones: list[dict],
    person_policies: dict[str, str],
    classifier_version: int,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict:
```

Returns:
```
{
    eligible, uploaded, skipped_already_uploaded, skipped_missing_file,
    needs_review, auto_private, candidate_public,
    date_set_failed, db_write_failed, upload_failed,
}
```

**Per-asset flow:**

1. Check `legacy_assets.uploaded_flickr_id` — if set, increment `skipped_already_uploaded` and continue. This is the primary idempotency guard.
2. Resolve `library_path / master_rel_path`. If path doesn't exist, increment `skipped_missing_file` and continue.
3. Call `shape_legacy_for_classify()` then `classify(shaped, zones, self_name, person_policies)` → `(privacy_state, privacy_reason)`.
4. If `dry_run`, record the would-be classification and continue without uploading.
5. Upload via `flickr_client.upload_photo(path, title, description, tags)` → `flickr_id`.
6. **Immediately** update `legacy_assets SET uploaded_flickr_id = flickr_id, uploaded_at = now` and commit. If this write fails, log a warning with the `flickr_id` (see orphan handling below) and increment `db_write_failed`.
7. In a single DB transaction: `db.upsert_photo(...)` + `db.log_operation("upload_legacy_asset", ...)`. If this transaction fails, the `uploaded_flickr_id` is already set in `legacy_assets`, so a re-run will skip the upload and retry only the `photos` row creation.

**Recovery path for partial failures:**

On re-run, before the main loop, `legacy_uploader` queries for assets where `uploaded_flickr_id IS NOT NULL` but no `photos` row exists for that `flickr_id`. For each, it retries only step 7 (the DB write). This self-heals the most common failure mode without requiring manual intervention.

**Orphan risk (acknowledged):**

If both step 6 and the Flickr upload fail in a specific order — upload succeeds, `legacy_assets` update fails before committing — the asset has no `uploaded_flickr_id` and no `photos` row, so a re-run would upload it again, creating a Flickr duplicate. This is a narrow failure window. For a one-shot command on an expected small set, the mitigation is: the `uploaded_flickr_id` write uses a minimal separate transaction immediately after upload, minimising the window; and `bp deduplicator` can catch any resulting duplicates. The `flickr_id` is always logged to stdout for manual recovery if needed.

## photos row fields

```python
{
    "flickr_id": flickr_id,
    "uuid": None,
    "privacy_state": privacy_state,       # from classify()
    "privacy_reason": privacy_reason,     # from classify()
    "date_taken": asset["date_taken"],
    "width": asset["width"],
    "height": asset["height"],
    "flickr_title": asset["title"] or "",
    "flickr_tags": asset["keywords"],     # JSON array from legacy_assets
    "flickr_description": asset["description"] or "",
}
```

After the `photos` row is created, the new record feeds into BP's normal pipeline:
- `needs_review` → appears in the review queue
- `candidate_public` → picked up by the proposal pipeline
- `auto_private` → stays private

## operation_log entry

```
operation:  "upload_legacy_asset"
photo_id:   the new photos.id
target:     "flickr_id"
old_value:  NULL
new_value:  flickr_id
trigger:    "legacy:{asset_uuid} clf={classifier_version}"
actor:      "bp"
```

## CLI command

```
bp upload-legacy-unmatched [--dry-run] [--limit N] [--library-uuid UUID] [--library PATH]
```

- `--dry-run` — classify and report without uploading or writing to DB
- `--limit N` — upload at most N assets (for incremental rollout)
- `--library-uuid` — which indexed library to draw from (default: most recently indexed)
- `--library` — path to the `.photoslibrary` bundle (overrides `config.legacy_library.path`)

Dry-run output:
```
Legacy upload dry run
  library_uuid      : <uuid>
  eligible          : 245
  would upload      : 243
  skipped (no file) : 2

Would-be privacy states:
  auto_private      : 180
  needs_review      : 50
  candidate_public  : 13
```

Live run output:
```
Legacy upload
  library_uuid       : <uuid>
  eligible           : 245
  uploaded           : 241
  already uploaded   : 0
  skipped (no file)  : 2
  upload failed      : 2
  db write failed    : 0

Privacy states applied:
  auto_private       : 178
  needs_review       : 50
  candidate_public   : 13
```

## Error handling summary

| Failure | Behaviour |
|---------|-----------|
| File missing | Skip, increment `skipped_missing_file`, continue |
| Upload fails (network/5xx) | Log error, increment `upload_failed`, continue |
| `setDates` fails | Log warning, count as uploaded, continue |
| `legacy_assets` update fails after upload | Log flickr_id to stdout, increment `db_write_failed` |
| `photos` row write fails | Detected on re-run via `uploaded_flickr_id`; auto-retried |
| Unsupported file format (Flickr rejects) | Treated as upload failure |

## Testing

**`FlickrClient.upload_photo()`** — mock `self._session.post()`. Verify: correct endpoint URL, multipart form fields, XML success path returns flickr_id, XML error path raises `FlickrApiError`, `setDates` is called with correct date_taken.

**`legacy_uploader.py`** — real SQLite DB, stub Flickr client that records calls and returns fake flickr_ids. Tests: successful upload creates photos row + sets `uploaded_flickr_id`; dry-run makes no writes; asset with `uploaded_flickr_id` already set is skipped; missing file is skipped and counted; upload failure is isolated (other assets continue); recovery path creates photos row for asset with `uploaded_flickr_id` but no photos row; classify result (`auto_private`, `needs_review`, `candidate_public`) is correctly stored.

## What this does not do

- Does not import photos into Apple Photos (#231)
- Does not upload photos that are already in `photos` (any privacy state)
- Does not run periodically or as part of `bp all`
- Does not support format conversion (HEIC → JPEG, RAW → JPEG)
- Does not update Flickr album membership after upload (albums can be managed separately)
