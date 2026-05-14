# Flickr Rate-Limit Retry Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent sustained HTTP 429 rate-limiting from exhausting all retries during large overnight runs by giving 429s their own extended retry schedule (8 retries, 60s backoff cap) while leaving timeout/connection-error retries unchanged.

**Architecture:** Two changes to `FlickrClient` in `flickr/flickr_client.py`: (1) `_retry` selects retry count and backoff cap based on whether the error is a 429; (2) `_call` checks for a `Retry-After` header on 429 responses and honors it (clamped to 0–120s) before delegating to `_retry`. No schema changes, no new flags.

**Tech Stack:** Python, `requests`, `unittest.mock`, pytest.

**Spec:** `docs/superpowers/specs/2026-05-13-rate-limit-retry-54-design.md`

---

## File map

| Action | File | Change |
|--------|------|--------|
| Modify | `flickr/flickr_client.py:89–169` | Retry-After handling in `_call`; differentiated policy in `_retry` |
| Modify | `tests/test_core.py:1281–1290` | Update `_mock_response` to support `retry_after` param |
| Modify | `tests/test_core.py` (class `TestFlickrClientRetry`) | Add 5 new tests |

---

## Task 1: Write failing tests

**Files:**
- Modify: `tests/test_core.py` (class `TestFlickrClientRetry`, starting around line 1265)

- [ ] **Step 1: Update `_mock_response` to support `Retry-After`**

The existing `_mock_response` helper (around line 1281) leaves `resp.headers` as an uncontrolled `MagicMock`, which means `resp.headers.get("Retry-After")` returns a truthy `MagicMock` by default. That would incorrectly trigger the Retry-After path in every 429 test. Add an explicit `retry_after` parameter so the default is `None`:

Replace the existing `_mock_response` method:

```python
def _mock_response(self, status_code=200, json_data=None, retry_after=None):
    from unittest.mock import MagicMock
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {"stat": "ok"}
    resp.raise_for_status = MagicMock()
    resp.headers.get.return_value = retry_after  # None by default — no Retry-After header
    if status_code >= 400:
        import requests as req
        resp.raise_for_status.side_effect = req.HTTPError(response=resp)
    return resp
```

- [ ] **Step 2: Add 5 new tests to `TestFlickrClientRetry`**

Add these tests after the existing `test_429_is_retried_not_treated_as_permanent` test (around line 1434):

```python
def test_429_uses_8_retries_not_4(self):
    """HTTP 429 must use 8 retries, not the default 4, to outlast Flickr's rate-limit window."""
    from unittest.mock import patch
    from flickr.flickr_client import FlickrError
    c = self._make_client()
    rate_limited = self._mock_response(429)
    call_count = 0

    def counting_get(*a, **kw):
        nonlocal call_count
        call_count += 1
        return rate_limited

    with patch.object(c._session, 'get', side_effect=counting_get):
        with patch('time.sleep'):
            with self.assertRaises(FlickrError):
                c._call("flickr.photosets.addPhoto")
    # 1 initial attempt + 8 retries = 9 total calls
    self.assertEqual(call_count, 9)

def test_timeout_still_uses_4_retries(self):
    """Timeout errors must keep the existing 4-retry schedule, not the 429 extended schedule."""
    from unittest.mock import patch
    import requests as req
    from flickr.flickr_client import FlickrError
    c = self._make_client()
    call_count = 0

    def counting_get(*a, **kw):
        nonlocal call_count
        call_count += 1
        raise req.Timeout()

    with patch.object(c._session, 'get', side_effect=counting_get):
        with patch('time.sleep'):
            with self.assertRaises(FlickrError):
                c._call("flickr.test.login")
    # 1 initial attempt + 4 retries = 5 total calls
    self.assertEqual(call_count, 5)

def test_429_backoff_capped_at_60s(self):
    """429 retry delays must be capped at 60s — attempt 6+ should not exceed 60s."""
    from unittest.mock import patch
    from flickr.flickr_client import FlickrError
    c = self._make_client()
    rate_limited = self._mock_response(429)
    sleep_calls = []

    with patch.object(c._session, 'get', return_value=rate_limited):
        with patch('time.sleep', side_effect=lambda d: sleep_calls.append(d)):
            with patch('flickr.flickr_client.random.uniform', return_value=0.0):
                with self.assertRaises(FlickrError):
                    c._call("flickr.photosets.addPhoto")

    # rate_limit_delay=0 in tests, so all non-zero sleeps are retry backoffs
    retry_sleeps = [d for d in sleep_calls if d > 0]
    self.assertTrue(all(d <= 60.5 for d in retry_sleeps),
                    f"All retry delays must be <= 60.5s, got: {retry_sleeps}")
    # Attempts 6 and 7 (2^6=64, 2^7=128) must be capped at exactly 60s (jitter=0)
    self.assertEqual(retry_sleeps.count(60.0), 2,
                     f"Expected two 60s delays (attempts 6 and 7), got: {retry_sleeps}")

def test_retry_after_header_honored(self):
    """When Flickr sends Retry-After, sleep that duration instead of exponential backoff."""
    from unittest.mock import patch
    c = self._make_client()
    rate_limited = self._mock_response(429, retry_after="30")
    ok_resp = self._mock_response(200, {"stat": "ok"})
    sleep_calls = []

    with patch.object(c._session, 'get', side_effect=[rate_limited, ok_resp]):
        with patch('time.sleep', side_effect=lambda d: sleep_calls.append(d)):
            result = c._call("flickr.test.login")

    self.assertEqual(result["stat"], "ok")
    self.assertIn(30.0, sleep_calls, "Retry-After value of 30 must be used as sleep duration")

def test_retry_after_validation(self):
    """Retry-After header: non-numeric ignored; negative clamped to 0; >120 capped at 120."""
    from unittest.mock import patch
    c = self._make_client()

    # Non-numeric: should fall through to normal backoff (no sleep of "bad-value")
    bad_header = self._mock_response(429, retry_after="bad-value")
    ok_resp = self._mock_response(200, {"stat": "ok"})
    sleep_calls = []
    with patch.object(c._session, 'get', side_effect=[bad_header, ok_resp]):
        with patch('time.sleep', side_effect=lambda d: sleep_calls.append(d)):
            with patch('flickr.flickr_client.random.uniform', return_value=0.0):
                c._call("flickr.test.login")
    # Should have slept 1.0s (2^0 + 0.0 jitter) from exponential backoff, not from the header
    self.assertIn(1.0, sleep_calls)

    # Absurd value: capped at 120
    huge_header = self._mock_response(429, retry_after="86400")
    ok_resp2 = self._mock_response(200, {"stat": "ok"})
    sleep_calls2 = []
    with patch.object(c._session, 'get', side_effect=[huge_header, ok_resp2]):
        with patch('time.sleep', side_effect=lambda d: sleep_calls2.append(d)):
            c._call("flickr.test.login")
    self.assertIn(120.0, sleep_calls2, "Retry-After of 86400 must be capped at 120")
    self.assertNotIn(86400.0, sleep_calls2)

    # Negative value: clamped to 0
    neg_header = self._mock_response(429, retry_after="-5")
    ok_resp3 = self._mock_response(200, {"stat": "ok"})
    sleep_calls3 = []
    with patch.object(c._session, 'get', side_effect=[neg_header, ok_resp3]):
        with patch('time.sleep', side_effect=lambda d: sleep_calls3.append(d)):
            c._call("flickr.test.login")
    self.assertIn(0.0, sleep_calls3, "Negative Retry-After must be clamped to 0")
    self.assertNotIn(-5.0, sleep_calls3)
```

- [ ] **Step 3: Run new tests to confirm they fail**

```bash
python -m pytest tests/test_core.py::TestFlickrClientRetry::test_429_uses_8_retries_not_4 tests/test_core.py::TestFlickrClientRetry::test_timeout_still_uses_4_retries tests/test_core.py::TestFlickrClientRetry::test_429_backoff_capped_at_60s tests/test_core.py::TestFlickrClientRetry::test_retry_after_header_honored tests/test_core.py::TestFlickrClientRetry::test_retry_after_validation -v
```

Expected: all 5 FAIL (implementation not changed yet).

- [ ] **Step 4: Run existing retry tests to confirm they still pass**

```bash
python -m pytest tests/test_core.py::TestFlickrClientRetry -v
```

Expected: existing tests PASS (updating `_mock_response` must not break them).

---

## Task 2: Implement the changes

**Files:**
- Modify: `flickr/flickr_client.py:89–169`

- [ ] **Step 1: Update `_call` to handle `Retry-After` header on 429 responses**

Replace the existing transient-error block in `_call` (around line 129):

```python
        # Permanent client errors — raise immediately, no retry
        if resp.status_code in _PERMANENT_HTTP_CODES:
            resp.raise_for_status()  # raises requests.HTTPError

        # Transient server errors — retry with backoff
        if resp.status_code in _TRANSIENT_HTTP_CODES:
            return self._retry(method, params, http_method, max_retries, _attempt,
                               reason=f"HTTP {resp.status_code}")
```

With:

```python
        # Permanent client errors — raise immediately, no retry
        if resp.status_code in _PERMANENT_HTTP_CODES:
            resp.raise_for_status()  # raises requests.HTTPError

        # Transient server errors — retry with backoff
        if resp.status_code in _TRANSIENT_HTTP_CODES:
            if resp.status_code == 429:
                # Honor Retry-After if present and valid; fall through to exponential on bad values
                retry_after = resp.headers.get("Retry-After")
                if retry_after:
                    try:
                        delay = float(retry_after)
                        delay = max(0.0, min(delay, 120.0))  # clamp: negative→0, absurd→2 min cap
                        time.sleep(delay)
                    except ValueError:
                        pass  # non-numeric header — exponential backoff will run via _retry
            return self._retry(method, params, http_method, max_retries, _attempt,
                               reason=f"HTTP {resp.status_code}")
```

- [ ] **Step 2: Update `_retry` to use differentiated policy for 429 vs other errors**

Replace the entire `_retry` method (lines 148–169):

```python
    def _retry(
        self,
        method: str,
        params: dict | None,
        http_method: str,
        max_retries: int,
        attempt: int,
        reason: str,
    ) -> dict:
        """Sleep with exponential backoff and retry, or raise if exhausted.

        Policy by error type:
          HTTP 429 (rate limit): 8 retries, 60s backoff ceiling.
              Outlasts Flickr's ~1-minute rate-limit window before giving up.
          All other transient errors (timeout, 500, 502, etc.): caller's max_retries
              (default 4), 8s backoff ceiling. Network hiccups typically recover quickly.
        """
        photo_id = (params or {}).get("photo_id", "")
        context  = f" photo_id={photo_id}" if photo_id else ""

        if "429" in reason:
            effective_max_retries = 8
            backoff_cap = 60
        else:
            effective_max_retries = max_retries  # caller's value, default 4
            backoff_cap = 8

        if attempt >= effective_max_retries:
            log.error(
                f"Flickr {method}{context} failed after {effective_max_retries} retries ({reason})"
            )
            raise FlickrError(-1, f"Flickr call failed after {effective_max_retries} retries ({reason})")

        delay = min(2 ** attempt, backoff_cap) + random.uniform(0, 0.5)
        log.warning(
            f"Flickr {method}{context} failed ({reason}), "
            f"retry {attempt + 1}/{effective_max_retries} in {delay:.1f}s"
        )
        time.sleep(delay)
        return self._call(method, params, http_method, max_retries, attempt + 1)
```

- [ ] **Step 3: Update the docstring in `_call`**

Replace the existing docstring (around line 97):

```python
        """
        Make a signed Flickr API call with exponential backoff on transient errors.
        Returns the parsed JSON response body. Raises FlickrError on persistent failure.

        Retry schedule (seconds): 1, 2, 4, 8 — then give up.
        """
```

With:

```python
        """
        Make a signed Flickr API call with exponential backoff on transient errors.
        Returns the parsed JSON response body. Raises FlickrError on persistent failure.

        Retry policy by error type:
          HTTP 429: 8 retries, backoff capped at 60s (~3 min total).
                    Honors Retry-After header when present (clamped to 0–120s).
          Other transient errors (timeout, 5xx): max_retries attempts (default 4),
                    backoff capped at 8s (~15s total).
          Permanent errors (4xx, non-transient Flickr codes): raise immediately.
        """
```

- [ ] **Step 4: Run the new tests to confirm they pass**

```bash
python -m pytest tests/test_core.py::TestFlickrClientRetry::test_429_uses_8_retries_not_4 tests/test_core.py::TestFlickrClientRetry::test_timeout_still_uses_4_retries tests/test_core.py::TestFlickrClientRetry::test_429_backoff_capped_at_60s tests/test_core.py::TestFlickrClientRetry::test_retry_after_header_honored tests/test_core.py::TestFlickrClientRetry::test_retry_after_validation -v
```

Expected: all 5 PASS.

- [ ] **Step 5: Run the full `TestFlickrClientRetry` suite to confirm no regressions**

```bash
python -m pytest tests/test_core.py::TestFlickrClientRetry -v
```

Expected: all tests PASS.

- [ ] **Step 6: Run the full test suite**

```bash
python -m pytest tests/ -q
```

Expected: all pass.

- [ ] **Step 7: Close issue and commit**

```bash
git add flickr/flickr_client.py tests/test_core.py
git commit -m "fix: extend 429 retry schedule to 8 attempts with 60s backoff cap (Closes #54)"
```

After committing, add a comment to GH #54:

> Fixed. HTTP 429 now uses 8 retries with a 60s backoff ceiling (~3 minutes total), giving Flickr's rate-limit window time to reset before giving up. Other transient errors (timeouts, 5xx) keep the existing 4-retry / 8s schedule. `Retry-After` headers are honored when present, clamped to 0–120s. The `photo_albums` row was already left pending on failure (not cleared), so any pairs that failed in the original overnight run will be retried automatically.

Then add the `has-plan` label to #54 on GitHub:

```bash
gh issue edit 54 --add-label "has-plan"
```
