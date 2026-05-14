# Flickr Rate-Limit Retry Hardening — Design Spec (GH #54)

**Goal:** Prevent sustained HTTP 429 rate-limiting from permanently failing Flickr API calls during large overnight runs by extending the retry count and backoff ceiling for 429s, and honoring `Retry-After` headers when present.

---

## Problem

The current retry schedule exhausts all attempts in ~15 seconds:

```
attempt 0:  ~1s
attempt 1:  ~2s
attempt 2:  ~4s
attempt 3:  ~8s   ← gives up here (max_retries=4)
```

Flickr's rate-limit window is ~1 minute. During sustained 429s (observed during `bp sync-albums --limit 1000000` overnight runs), all 4 retries exhaust before the window resets, causing permanent failure for that call in that run. The `photo_albums` row stays pending (it is not cleared on failure), so subsequent runs will retry — but sustained rate-limiting means they fail too.

The same problem affects `flickr.photos.recentlyUpdated` in the poller (confirmed in logs).

Exhausted retries are not treated as permanent failure — the pending DB row is preserved for the next run. This is the correct recovery model and is unchanged by this fix.

---

## Change

**File:** `flickr/flickr_client.py`

### 1. Differentiated retry policy by error type

429s and other transient errors have different recovery profiles:

- **HTTP 429 (rate limit):** needs long backoff to outlast Flickr's ~1-minute window. Gets 8 retries with a 60s ceiling.
- **Timeouts and connection errors:** transient network issues that typically recover in seconds. Keep the existing 4-retry / 8s-ceiling schedule.
- **Permanent HTTP errors (400, 401, 403, etc.) and non-transient Flickr API errors:** raise immediately, no retry. Unchanged.

`_retry` receives the `reason` string already. Use it to select the policy:

```python
if "429" in reason:
    max_retries_effective = 8
    backoff_cap = 60
else:
    max_retries_effective = max_retries  # caller's value (default 4)
    backoff_cap = 8
```

### 2. Honor `Retry-After` header on 429 responses

When Flickr sends a `Retry-After` header on a 429 response, sleep that duration instead of computing exponential backoff. Flickr does not send this header reliably, so it is best-effort with fallback to exponential.

In `_call`, before calling `_retry` on a 429:

```python
if resp.status_code == 429:
    retry_after = resp.headers.get("Retry-After")
    if retry_after:
        try:
            delay = float(retry_after)
            delay = max(0, min(delay, 120))  # clamp: negative → 0, absurd → 2 min cap
            sleep(delay)
        except ValueError:
            pass  # non-numeric header — fall through to exponential backoff
```

Validation rules:
- Non-numeric value: ignored, fall through to exponential backoff
- Negative value: clamped to 0 (sleep immediately, then retry)
- Values > 120s: capped at 120s — prevents a bad upstream header (e.g. `Retry-After: 86400`) from stalling a bulk run for hours

### 3. New 429 retry schedule

| Attempt | Delay |
|---------|-------|
| 0 | ~1s |
| 1 | ~2s |
| 2 | ~4s |
| 3 | ~8s |
| 4 | ~16s |
| 5 | ~32s |
| 6 | ~60s ← rate-limit window resets here |
| 7 | ~60s |

Total max wait before giving up: ~183s (~3 minutes). Outlasts Flickr's ~1-minute rate-limit window by 3× before exhausting retries.

Timeout/connection errors retain the current schedule (attempts 0–3, ~15s total) — appropriate for transient network issues.

### Not in scope

- **Process-wide cooldown after repeated 429s** — valid future consideration if the per-call fix proves insufficient. Would require shared mutable state across concurrent calls. Deferred.
- **Summary output changes** (immediate / retried / exhausted counts) — a monitoring enhancement, tracked separately from this fix.

---

## Scope

- One method (`_retry`), one call site in `_call` for `Retry-After`
- Fixes all Flickr commands (sync-albums, poller, metadata sync, etc.) — not scoped to a single command
- No schema changes, no new flags, no config changes

---

## Testing

Three unit tests, no live Flickr calls:

- 429 errors use 8-retry schedule with 60s backoff cap
- Timeout/connection errors use 4-retry schedule with 8s cap
- `_retry` raises `FlickrError` after the correct number of attempts for each error type
