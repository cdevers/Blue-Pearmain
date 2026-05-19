# Design: bp reconcile resilience + progress logging (GH #103)

**Date:** 2026-05-19  
**Issue:** https://github.com/cdevers/Blue-Pearmain/issues/103  
**Status:** approved

## Problem

`bp reconcile` has two defects that make long runs unreliable:

1. **Silent loop-stop on permanent HTTP errors.** `flickr_client._call` raises
   `requests.HTTPError` for permanent HTTP responses (404, 401, 403, …). `check_photo`
   only catches `FlickrError`, so the first deleted or 4xx-returning photo propagates an
   uncaught exception to the outer `try/except` in `main()` and stops the entire loop.
   All remaining photos in the batch are silently abandoned.

2. **No progress feedback.** A run over 82 k photos takes 12+ hours. The only log output
   is occasional transient-error warnings; there is no indication of how far along the
   run is.

A secondary gap: photos already confirmed deleted from Flickr are not filtered from the
reconcile query, so they are re-checked on every run.

## Approach

Close the abstraction leak at its source (`_call`), then follow the existing
`flickr_deleted` pattern already used by `metadata_puller` and `sync_metadata`.

## Changes

### 1. `flickr/flickr_client.py` — `_call`: raise `FlickrError` for permanent HTTP codes

Replace `resp.raise_for_status()` (which raises `requests.HTTPError`) with a
`FlickrError` carrying the HTTP status code:

```python
# before
if resp.status_code in _PERMANENT_HTTP_CODES:
    resp.raise_for_status()

# after
if resp.status_code in _PERMANENT_HTTP_CODES:
    raise FlickrError(resp.status_code, resp.reason or f"HTTP {resp.status_code}")
```

`FlickrError.__init__` already accepts any integer code. HTTP codes like 404 are not in
`_TRANSIENT_FLICKR_CODES`, so they are correctly treated as permanent (no retry).

**Effect:** callers only ever see `FlickrError`; `requests.HTTPError` no longer escapes
the client boundary.

### 2. `poller/reconcile.py` — `check_photo`: handle deleted photos

Widen the existing `except FlickrError` block to detect "photo no longer exists" and
write it to the DB:

```python
except FlickrError as e:
    if e.code in (1, 404):          # Flickr app-level "not found" or HTTP 404
        db.mark_flickr_deleted(row["id"])
        result["status"] = "flickr_deleted"
    else:
        result["status"] = "flickr_error"
        result["errors"] = [str(e)]
    return result
```

`db.mark_flickr_deleted()` already exists (sets `flickr_deleted = 1`, updates
`updated_at`).

### 3. `poller/reconcile.py` — `main()`: filter deleted photos + progress logging

**WHERE clause** — add filter so already-deleted photos are skipped on future runs
(matches the pattern in `sync_metadata`):

```sql
AND (flickr_deleted IS NULL OR flickr_deleted = 0)
```

**New counter** — `flickr_deleted_count = 0`; incremented and printed in the result loop
for the `"flickr_deleted"` status.

**Summary line** — gains `flickr-deleted=N` field.

**Progress logging** — emitted every 500 photos:

```python
if (ok_count + mismatch_count + error_count + flickr_deleted_count) % 500 == 0:
    checked_so_far = ok_count + mismatch_count + error_count + flickr_deleted_count
    log.info(
        "progress: %d/%d checked  ok=%d  mismatch=%d  deleted=%d  errors=%d",
        checked_so_far, total, ok_count, mismatch_count, flickr_deleted_count, error_count,
    )
```

At 0.5 s/photo this fires roughly every 4 minutes; a 12-hour run produces ~144 lines.

### 4. `flickr/metadata_puller.py` — widen deleted-photo check

`metadata_puller` already handles `FlickrError` code 1. After the `_call` change, an
HTTP 404 arrives as `FlickrError(404, …)`. Widen the guard:

```python
# before
if e.code == 1:

# after
if e.code in (1, 404):
```

All downstream logic (log message, `db.mark_flickr_deleted()`, return) is unchanged.

## Tests

All in the existing `tests/` suite.

1. **`_call` permanent HTTP → `FlickrError`** — mock HTTP 404 and HTTP 403 responses;
   assert `FlickrError` is raised (not `requests.HTTPError`). Confirm HTTP 200 with
   `stat=fail, code=1` still raises `FlickrError(1, …)` (no regression).

2. **`check_photo` marks deleted photos** — mock client raising `FlickrError(1, …)` and
   `FlickrError(404, …)` separately; assert `db.mark_flickr_deleted` is called, status
   is `"flickr_deleted"`, and `check_photo` returns rather than raises.

3. **Reconcile WHERE skips `flickr_deleted` photos** — seed test DB with a photo where
   `flickr_deleted = 1`; assert it is absent from the query result set.

## Out of scope

- Redesigning the one-photo-at-a-time API call pattern (separate issue if needed).
- Investigating why the run sometimes checks the full library instead of the 500-photo
  default (tracked in GH #103 for follow-up).
