# Flickr Rate-Limit Retry Hardening — Design Spec (GH #54)

**Goal:** Prevent sustained HTTP 429 rate-limiting from permanently failing Flickr API calls during large overnight runs by extending the retry count and backoff ceiling in `FlickrClient._retry`.

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

---

## Change

**File:** `flickr/flickr_client.py`

**`_retry` method** — two changes:

1. **`max_retries` default: 4 → 8**
2. **Backoff formula: add 60-second ceiling**
   - Current: `2 ** attempt + random.uniform(0, 0.5)`
   - New: `min(2 ** attempt, 60) + random.uniform(0, 0.5)`

New retry schedule:

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

Total max wait before giving up: ~183s (~3 minutes). This outlasts Flickr's ~1-minute rate-limit window by 3× before exhausting retries.

The 60s cap prevents runaway waits at higher attempt numbers without needing to increase `max_retries` further.

The `max_retries` parameter remains a keyword argument on `_call` so individual call sites can still override it if needed.

---

## Scope

- One method (`_retry`), one formula change, one default change
- Fixes all Flickr commands (sync-albums, poller, metadata sync, etc.) — not scoped to a single command
- No schema changes, no new flags, no config changes

---

## Testing

Two unit tests, no live Flickr calls:

- Backoff values respect the 60s ceiling at high attempt numbers
- `_retry` raises `FlickrError` after exactly `max_retries` attempts
