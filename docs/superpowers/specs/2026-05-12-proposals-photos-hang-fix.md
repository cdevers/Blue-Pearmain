# Proposals: Photos.app Hang Fix — Design

**GitHub issues:** #61 (umbrella), #78 (server), #79 (client), #80 (tests)

---

## Root cause

When Photos.app is running but unresponsive (hung, being force-quit, restarting), `_photos_is_running()` returns `True` because it only checks process existence via `osascript`. The subsequent `photoscript` calls — `photoscript.Photo(uuid)`, `photo.keywords = tags`, `photo.title = value` — issue AppleScript commands that block indefinitely waiting for a response from the hung process.

This hangs the Flask request thread. From the user's perspective: the proposal button shows `…` and never changes. On reload the proposal is still pending, because `_mark_applied` and `db.conn.commit()` never ran.

Batch approve compounds the problem: one hung proposal blocks the entire loop, so all proposals in the batch appear to fail.

A secondary issue: the async JS handlers have no try-catch and no request timeout. If the server hangs, `await r.json()` never resolves — the button is stuck at `…` with no way to retry.

---

## Fix A — Server-side: responsiveness check + thread timeout (#78)

### `_photos_is_responsive()` replaces `_photos_is_running()`

```python
def _photos_is_responsive(timeout: int = 3) -> bool:
    """
    Return True only if Photos.app is running AND responds to a test AppleScript
    command within `timeout` seconds. Catches the case where the process exists
    but is hung (which _photos_is_running() cannot detect).
    """
    try:
        result = subprocess.run(
            ["osascript", "-e", 'tell application "Photos" to name'],
            capture_output=True, text=True, timeout=timeout,
        )
        return result.returncode == 0
    except Exception:
        return False
```

Replace every call to `_photos_is_running()` in `proposal_applier.py` with `_photos_is_responsive()`. Remove `_photos_is_running()`.

### Thread-with-timeout wrapper

Wrap each `photoscript` block (the actual write operations) in a helper that runs in a `ThreadPoolExecutor` with a 45-second timeout:

```python
import concurrent.futures

_PHOTOS_WRITE_TIMEOUT = 45  # seconds

def _run_with_timeout(fn, *args, timeout=_PHOTOS_WRITE_TIMEOUT):
    """
    Run fn(*args) in a thread. If it does not complete within `timeout` seconds,
    return {"ok": False, "reason": "Photos not responding"}.
    The thread itself cannot be killed (OS limitation), but the Flask handler
    is unblocked and the user gets a clear error.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(fn, *args)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            return {"ok": False, "reason": "Photos not responding"}
        except Exception as exc:
            return {"ok": False, "reason": str(exc)}
```

Apply to the three internal write functions that call `photoscript`:

| Function | Wraps |
|----------|-------|
| `_write_tags_to_photos` | `photo.keywords = new_tags` block |
| `_apply_text_to_photos` | `photo.title = ...` / `photo.description = ...` block |
| `_write_text_to_photos_both` | `photo.title = ...` + `photo.description = ...` block |

The restructuring: extract the `photoscript`-calling portion of each function into a nested `_do_*` callable, then call it via `_run_with_timeout`. The DB update portion (after `photo.keywords` etc.) stays outside the thread.

### Why both?

The pre-check catches the common case (Photos visibly unresponsive) quickly and cheaply, before any `photoscript` overhead. The thread timeout is the belt-and-suspenders guard for the race window where Photos becomes unresponsive *between* the check and the write.

### Existing mock references

All tests that currently patch `flickr.proposal_applier._photos_is_running` must be updated to patch `flickr.proposal_applier._photos_is_responsive`.

### New tests (test_core.py)

- `test_photos_not_responsive_returns_error` — pre-check returns False → `{"ok": False, "reason": "Photos not responding"}`
- `test_photos_timeout_during_write_returns_error` — pre-check passes, photoscript write times out → `{"ok": False, "reason": "Photos not responding"}`
- Existing `test_photos_not_running_returns_error` updated to use new mock name

---

## Fix B — Client-side: JS defensive error handling (#79)

### try-catch + AbortController timeout

Wrap each async handler body in try-catch. Pass an `AbortSignal` to `apiFetch` with a 45-second timeout so that a hung server eventually unblocks the UI rather than leaving the button stuck at `…`.

```javascript
async function approveProposal(id, btn) {
  const origText = btn.textContent;
  btn.disabled = true;
  btn.textContent = '…';
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 45000);
  try {
    const r = await apiFetch('/api/proposals/' + id + '/approve',
                             {method: 'POST', signal: controller.signal});
    clearTimeout(timer);
    const d = await r.json();
    if (d.ok) {
      const card = document.getElementById('card-' + id);
      card.style.opacity = '0.3';
      btn.textContent = 'Applied ✓';
      _resolveCard(card);
    } else if (d.reason === 'stale_uuid') {
      const card = document.getElementById('card-' + id);
      card.style.opacity = '0.3';
      btn.textContent = 'Stale UUID';
      _resolveCard(card);
      toast('Photo no longer in Photos library — proposal marked failed', 'err');
    } else {
      btn.disabled = false;
      btn.textContent = origText;
      toast('Could not apply: ' + (d.reason || 'unknown error'), 'err');
    }
  } catch (err) {
    clearTimeout(timer);
    btn.disabled = false;
    btn.textContent = origText;
    toast(err.name === 'AbortError' ? 'Timed out — Photos may be unresponsive' :
          'Could not apply: network error', 'err');
  }
}
```

Apply the same pattern to `approveReverse` and `bulkApprove`.

### Button-text fix

Capture `origText = btn.textContent` at the start of each handler and restore it on any error path. This fixes the collision-proposal bug where the error path was restoring `'Approve ✓'` for buttons that were labelled `'Use Flickr ✓'` or `'Use Photos ✓'`.

### `apiFetch` signal support

`apiFetch` already passes `opts` through to `fetch`, so `signal: controller.signal` works without any changes to `base.html`.

---

## Fix C — Route-level tests (#80)

### Fixture: `client_with_proposals`

Creates a DB with:
- One photo (`flickr_id='F1'`, `uuid='U1'`, both tag sets populated)
- A non-conflict proposal (source=flickr, target=photos, field=tags)
- A divergence proposal (source=flickr, target=photos, field=tags)
- A collision proposal pair (source=flickr/photos, target=photos/flickr, field=tags)

### `TestProposalRoutes` tests

| Test | Endpoint | Mock | Expected |
|------|----------|------|----------|
| approve non-conflict, Photos responsive | `POST /api/proposals/<id>/approve` | `_photos_is_responsive=True`, `_write_tags_to_photos` mocked ok | `{"ok": true}` |
| approve, Photos not responding | same | `_photos_is_responsive=False` | `{"ok": false, "reason": "Photos not responding"}` |
| approve-reverse, Flickr ok | `POST /api/proposals/<id>/approve-reverse` | mock Flickr client | `{"ok": true}` |
| approve-reverse, no Flickr client | same | client=None | `{"ok": false}` |
| bulk-approve non-conflict | `POST /api/proposals/bulk-approve` | Photos mocked ok | `{"ok": true, "applied": N}` |
| bulk-approve divergence | same with `conflict_type=divergence` | Photos mocked ok | `{"ok": true, "applied": N}` |

---

## Files touched

| File | Change |
|------|--------|
| `flickr/proposal_applier.py` | Replace `_photos_is_running` with `_photos_is_responsive`; add `_run_with_timeout`; wrap photoscript blocks |
| `reviewer/templates/proposals.html` | try-catch + AbortController + button-text fix in JS handlers |
| `tests/test_core.py` | Update mock patch names; add responsiveness + timeout tests |
| `tests/test_review_ui.py` | Add `client_with_proposals` fixture + `TestProposalRoutes` |

---

## Implementation order

1. **#78 first** — fixes the root cause. All three JS handlers immediately get cleaner responses (`"Photos not responding"` instead of hanging).
2. **#79 second** — adds client-side resilience. Now if anything server-side still misbehaves (timeout, crash, network hiccup), the button re-enables and the user can retry.
3. **#80 third** — closes the test gap. Validates both fixes are wired correctly end-to-end and prevents future regressions.
