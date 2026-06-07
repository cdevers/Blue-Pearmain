# Local Nominatim Docker Support (#225) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow `bp geocode` to use a local Nominatim Docker instance for bulk geocoding, bypassing the public API's rate limits.

**Architecture:** A new `make_fetcher(url, *, min_delay=None)` factory in `geocoder.py` returns a closure bound to an arbitrary Nominatim URL and auto-detected delay (1s for the public endpoint, 0s for anything else). A companion `check_nominatim_status(url)` function confirms a local instance is up. `cmd_geocode` in `bp` resolves the URL from CLI flag or config, constructs the fetcher, and passes it into the existing `run_geocode` machinery unchanged.

**Tech Stack:** Python 3.11+, `requests`, `urllib.parse`, `pytest`, `unittest.mock`

---

## File map

| File | Change |
|------|--------|
| `poller/geocoder.py` | Rename `_NOMINATIM_URL` → `_PUBLIC_NOMINATIM_URL`; add `urlparse` import; add `make_fetcher`; add `check_nominatim_status` |
| `bp` | Add `--nominatim-url` and `--check-nominatim` args to `p_geo` (after `--limit`, ~line 1709); update `cmd_geocode` body (line 1109) |
| `config/config.example.yml` | Add commented `geocoding:` section (after `tag_protection:` block) |
| `tests/test_geocoder.py` | Add `make_fetcher`, `check_nominatim_status` to import block; add `TestMakeFetcher` and `TestCheckNominatimStatus` |
| `tests/test_bp_cli.py` | Add `test_geocode_check_nominatim_flag_accepted` |

---

## Task 1: `make_fetcher` — factory function + tests

**Files:**
- Modify: `poller/geocoder.py` (lines 1–26, 102, 115, then new code after line 124)
- Test: `tests/test_geocoder.py` (new class `TestMakeFetcher`)

- [ ] **Step 1a: Rename the constant (refactor, no test needed)**

In `poller/geocoder.py`, make three substitutions:

Line 25 — rename definition:
```python
# before
_NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"

# after
_PUBLIC_NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
```

Line 102 — rename in first `requests.get` call inside `fetch_from_nominatim`:
```python
# before
        resp = requests.get(_NOMINATIM_URL, params=_params, headers=_headers, timeout=10)

# after
        resp = requests.get(_PUBLIC_NOMINATIM_URL, params=_params, headers=_headers, timeout=10)
```

Line 115 — rename in retry `requests.get` call inside `fetch_from_nominatim`:
```python
# before
            resp = requests.get(_NOMINATIM_URL, params=_params, headers=_headers, timeout=10)

# after
            resp = requests.get(_PUBLIC_NOMINATIM_URL, params=_params, headers=_headers, timeout=10)
```

- [ ] **Step 1b: Add `urlparse` import to `geocoder.py`**

After `from typing import Any, Callable` (line 17), add:
```python
from urllib.parse import urlparse
```

- [ ] **Step 1c: Write `TestMakeFetcher` tests (they must fail — `make_fetcher` does not exist yet)**

Add after the `TestFetchFromNominatim` class in `tests/test_geocoder.py`:
```python
# ---------------------------------------------------------------------------
# make_fetcher — URL-bound fetcher factory (#225)
# ---------------------------------------------------------------------------


class TestMakeFetcher:
    def _mock_resp_200(self) -> "MagicMock":
        from unittest.mock import MagicMock

        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"display_name": "X", "address": {}}
        return resp

    def test_make_fetcher_default_url_uses_public_endpoint(self):
        """make_fetcher() with no URL argument hits the public Nominatim endpoint."""
        from unittest.mock import patch

        from geocoder import _PUBLIC_NOMINATIM_URL, make_fetcher

        fetcher = make_fetcher()
        with patch("requests.get", return_value=self._mock_resp_200()) as mock_get:
            fetcher(42.361, -71.057)

        assert mock_get.call_args[0][0] == _PUBLIC_NOMINATIM_URL

    def test_make_fetcher_public_url_delay_is_1s(self):
        """Auto-detected delay is 1.0s when URL matches the public Nominatim endpoint."""
        from unittest.mock import patch

        from geocoder import _PUBLIC_NOMINATIM_URL, make_fetcher

        fetcher = make_fetcher(_PUBLIC_NOMINATIM_URL)
        resp = self._mock_resp_200()

        with (
            patch("requests.get", return_value=resp),
            patch("time.sleep") as mock_sleep,
        ):
            # First call sets last_call_time to ~now.
            # Second call finds elapsed < 1.0 and calls sleep.
            fetcher(42.0, -71.0)
            fetcher(42.0, -71.0)

        assert mock_sleep.called
        sleep_amount = mock_sleep.call_args[0][0]
        assert 0.9 < sleep_amount <= 1.0

    def test_make_fetcher_local_url_delay_is_0(self):
        """Auto-detected delay is 0.0s for any URL other than the public endpoint."""
        from unittest.mock import patch

        from geocoder import make_fetcher

        fetcher = make_fetcher("http://localhost:8080/reverse")
        resp = self._mock_resp_200()

        with (
            patch("requests.get", return_value=resp),
            patch("time.sleep") as mock_sleep,
        ):
            fetcher(42.0, -71.0)
            fetcher(42.0, -71.0)  # elapsed ≈ 0, min_delay=0.0 → no sleep

        mock_sleep.assert_not_called()

    def test_make_fetcher_explicit_min_delay_overrides_auto(self):
        """Explicit min_delay overrides auto-detection."""
        from unittest.mock import patch

        from geocoder import _PUBLIC_NOMINATIM_URL, make_fetcher

        # Public URL auto-detects to 1.0s; explicit 0.0 suppresses the sleep.
        fetcher = make_fetcher(_PUBLIC_NOMINATIM_URL, min_delay=0.0)
        resp = self._mock_resp_200()

        with (
            patch("requests.get", return_value=resp),
            patch("time.sleep") as mock_sleep,
        ):
            fetcher(42.0, -71.0)
            fetcher(42.0, -71.0)

        mock_sleep.assert_not_called()
```

Also update the import block at the top of `tests/test_geocoder.py` to include `make_fetcher`:

```python
from geocoder import (
    PlaceData,
    _parse_nominatim_response,
    make_fetcher,
    reverse_geocode,
)
```

- [ ] **Step 1d: Run tests to confirm `TestMakeFetcher` fails**

```bash
python -m pytest tests/test_geocoder.py::TestMakeFetcher -v
```

Expected: 4 failures mentioning `ImportError: cannot import name 'make_fetcher'`

- [ ] **Step 1e: Implement `make_fetcher` in `geocoder.py`**

Insert the following function after `fetch_from_nominatim` (after line 124) and before `reverse_geocode`:

```python
def make_fetcher(
    url: str | None = None,
    *,
    min_delay: float | None = None,
) -> Callable[[float, float], "PlaceData | None"]:
    """Return a Nominatim fetcher closure bound to url and an auto-detected delay.

    url=None uses the public endpoint. min_delay=None auto-detects: 1.0s for
    the public endpoint (Nominatim usage policy), 0.0s for any other host.
    The closure tracks its own last_call_time, independent of the module-level
    _last_call_time used by fetch_from_nominatim.
    """
    effective_url = url or _PUBLIC_NOMINATIM_URL
    if min_delay is None:
        netloc = urlparse(effective_url).netloc
        min_delay = 1.0 if netloc == "nominatim.openstreetmap.org" else 0.0

    last_call_time = 0.0

    def fetcher(lat: float, lon: float) -> "PlaceData | None":
        import requests  # deferred import — not needed if geocoder isn't used

        nonlocal last_call_time
        elapsed = time.monotonic() - last_call_time
        if elapsed < min_delay:
            time.sleep(min_delay - elapsed)

        _params = {"lat": lat, "lon": lon, "zoom": 14, "addressdetails": 1, "format": "json"}
        _headers = {"User-Agent": _USER_AGENT}

        try:
            resp = requests.get(effective_url, params=_params, headers=_headers, timeout=10)
            last_call_time = time.monotonic()
            for delay in _RETRY_DELAYS:
                if resp.status_code != 429:
                    break
                wait = max(int(resp.headers.get("Retry-After", delay)), delay)
                log.warning(
                    "Nominatim rate-limited (429) for (%.6f, %.6f); backing off %ds",
                    lat,
                    lon,
                    wait,
                )
                time.sleep(wait)
                resp = requests.get(effective_url, params=_params, headers=_headers, timeout=10)
                last_call_time = time.monotonic()
            if resp.status_code != 200:
                log.warning(
                    "Nominatim returned HTTP %s for (%.6f, %.6f)", resp.status_code, lat, lon
                )
                return None
            return _parse_nominatim_response(resp.json())
        except Exception as exc:
            last_call_time = time.monotonic()
            log.warning("Nominatim request failed for (%.6f, %.6f): %s", lat, lon, exc)
            return None

    return fetcher
```

- [ ] **Step 1f: Run the full test suite and confirm it passes**

```bash
python -m pytest tests/ -q
```

Expected: all tests pass, including the 4 new `TestMakeFetcher` tests.

- [ ] **Step 1g: Commit**

```bash
git add poller/geocoder.py tests/test_geocoder.py
git commit -m "$(cat <<'EOF'
feat(#225): add make_fetcher — URL-bound fetcher factory with auto-detected delay

Renames _NOMINATIM_URL → _PUBLIC_NOMINATIM_URL. make_fetcher(url, *,
min_delay=None) returns a closure that binds the effective URL and
auto-detected rate-limit delay (1s for the public endpoint, 0s for any
other host). The closure tracks its own last_call_time so a local-instance
run does not pollute the module-level rate-limiter state.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `check_nominatim_status` — readiness check + tests

**Files:**
- Modify: `poller/geocoder.py` (new function after `make_fetcher`)
- Test: `tests/test_geocoder.py` (new class `TestCheckNominatimStatus`)

- [ ] **Step 2a: Write `TestCheckNominatimStatus` tests (must fail — function does not exist yet)**

Add after `TestMakeFetcher` in `tests/test_geocoder.py`:

```python
# ---------------------------------------------------------------------------
# check_nominatim_status — readiness check (#225)
# ---------------------------------------------------------------------------


class TestCheckNominatimStatus:
    def test_check_status_ok(self):
        """HTTP 200 from /status.php → (True, 'Nominatim OK — <base_url>')."""
        from unittest.mock import MagicMock, patch

        from geocoder import check_nominatim_status

        resp = MagicMock()
        resp.status_code = 200

        with patch("requests.get", return_value=resp):
            ok, msg = check_nominatim_status("http://localhost:8080/reverse")

        assert ok is True
        assert "Nominatim OK" in msg
        assert "http://localhost:8080" in msg

    def test_check_status_http_error(self):
        """Non-200 response → (False, 'Nominatim unreachable — ...')."""
        from unittest.mock import MagicMock, patch

        from geocoder import check_nominatim_status

        resp = MagicMock()
        resp.status_code = 503

        with patch("requests.get", return_value=resp):
            ok, msg = check_nominatim_status("http://localhost:8080/reverse")

        assert ok is False
        assert "unreachable" in msg
        assert "http://localhost:8080" in msg

    def test_check_status_connection_error(self):
        """Network error → (False, 'Nominatim unreachable — ...')."""
        import requests as _requests
        from unittest.mock import patch

        from geocoder import check_nominatim_status

        with patch("requests.get", side_effect=_requests.ConnectionError("refused")):
            ok, msg = check_nominatim_status("http://localhost:1/reverse")

        assert ok is False
        assert "unreachable" in msg
```

Also update the import block at the top of `tests/test_geocoder.py` to include `check_nominatim_status`:

```python
from geocoder import (
    PlaceData,
    _parse_nominatim_response,
    check_nominatim_status,
    make_fetcher,
    reverse_geocode,
)
```

- [ ] **Step 2b: Run tests to confirm `TestCheckNominatimStatus` fails**

```bash
python -m pytest tests/test_geocoder.py::TestCheckNominatimStatus -v
```

Expected: 3 failures mentioning `ImportError: cannot import name 'check_nominatim_status'`

- [ ] **Step 2c: Implement `check_nominatim_status` in `geocoder.py`**

Insert the following function immediately after `make_fetcher` (and before `reverse_geocode`):

```python
def check_nominatim_status(url: str) -> tuple[bool, str]:
    """Check if a Nominatim instance is reachable by hitting its /status.php endpoint.

    Derives the base URL from url by stripping the path. Returns (True, message)
    on HTTP 200; (False, message) on any non-200 or network error. The response
    body is not parsed — deployment variants differ in JSON structure.
    """
    import requests  # deferred import — not needed if geocoder isn't used

    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    status_url = f"{base_url}/status.php"

    try:
        resp = requests.get(status_url, headers={"User-Agent": _USER_AGENT}, timeout=10)
        if resp.status_code == 200:
            return (True, f"Nominatim OK — {base_url}")
        return (False, f"Nominatim unreachable — {base_url} (HTTP {resp.status_code})")
    except Exception as exc:
        return (False, f"Nominatim unreachable — {base_url} ({exc})")
```

- [ ] **Step 2d: Run the full test suite and confirm it passes**

```bash
python -m pytest tests/ -q
```

Expected: all tests pass, including the 3 new `TestCheckNominatimStatus` tests.

- [ ] **Step 2e: Commit**

```bash
git add poller/geocoder.py tests/test_geocoder.py
git commit -m "$(cat <<'EOF'
feat(#225): add check_nominatim_status — /status.php readiness check

Hits <base_url>/status.php (derived from url by stripping the path).
Any HTTP 200 is treated as OK; non-200 and network errors return
unreachable. Body not parsed — deployment variants differ in JSON shape.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: CLI wiring + config docs + smoke test

**Files:**
- Modify: `bp` (arg parser ~line 1709; `cmd_geocode` body starting line 1109)
- Modify: `config/config.example.yml` (add commented `geocoding:` section)
- Test: `tests/test_bp_cli.py` (new function `test_geocode_check_nominatim_flag_accepted`)

- [ ] **Step 3a: Write the CLI smoke test (must fail — flags don't exist yet)**

Add at the end of `tests/test_bp_cli.py`:

```python
def test_geocode_check_nominatim_flag_accepted() -> None:
    """--check-nominatim exits before opening config; argparse must not reject the flag.

    http://localhost:1/reverse will refuse the connection → exit 1.
    No --config is passed because --check-nominatim must run before config is opened.
    The test guards against 'unrecognized arguments' in stderr (argparse parse failure).
    """
    result = subprocess.run(
        [
            sys.executable,
            BP,
            "geocode",
            "--check-nominatim",
            "--nominatim-url",
            "http://localhost:1/reverse",
        ],
        capture_output=True,
        cwd=ROOT,
    )
    stderr = result.stderr.decode()
    assert result.returncode == 1  # connection refused → unreachable → exit 1
    assert "unrecognized arguments" not in stderr
```

- [ ] **Step 3b: Run the test to confirm it fails for the right reason**

```bash
python -m pytest tests/test_bp_cli.py::test_geocode_check_nominatim_flag_accepted -v
```

Expected: FAIL. Exit code is likely 2 (argparse rejects `--check-nominatim` as unrecognized) and `"unrecognized arguments"` appears in stderr.

- [ ] **Step 3c: Add `--nominatim-url` and `--check-nominatim` args to the `geocode` subparser in `bp`**

After the `--limit` argument block (after line 1709, before `# import-contacts-birthdays`), insert:

```python
    p_geo.add_argument(
        "--nominatim-url",
        default=None,
        metavar="URL",
        help=(
            "Nominatim reverse geocoding endpoint "
            "(default: https://nominatim.openstreetmap.org/reverse). "
            "Override to use a local Docker instance, e.g. http://localhost:8080/reverse"
        ),
    )
    p_geo.add_argument(
        "--check-nominatim",
        action="store_true",
        help=(
            "Check whether the Nominatim endpoint is reachable and exit. "
            "Exits 0 if reachable, 1 if not. No --config required."
        ),
    )
```

- [ ] **Step 3d: Replace the body of `cmd_geocode` in `bp`**

Replace the entire `cmd_geocode` function body (lines 1109–1152) with:

```python
def cmd_geocode(args: argparse.Namespace) -> None:
    """Backfill place data from Nominatim for photos with GPS coordinates."""
    import yaml

    sys.path.insert(0, str(ROOT / "poller"))
    from bp_logging import configure
    from db.db import Database
    from geocoder import _PUBLIC_NOMINATIM_URL, check_nominatim_status, fetch_from_nominatim, make_fetcher
    from run_geocode import run_geocode

    # --check-nominatim exits before the config file is opened (no DB path needed).
    if args.check_nominatim:
        url = args.nominatim_url or _PUBLIC_NOMINATIM_URL
        ok, msg = check_nominatim_status(url)
        print(msg)
        sys.exit(0 if ok else 1)

    configure("geocode", verbose=args.verbose)

    with open(args.config) as f:
        config = yaml.safe_load(f)

    db_path = str(Path(config["database"]["path"]).expanduser())
    db = Database(db_path)

    nominatim_url = args.nominatim_url or config.get("geocoding", {}).get("nominatim_url")
    fetcher = make_fetcher(nominatim_url) if nominatim_url else fetch_from_nominatim

    try:
        counts = run_geocode(
            db,
            dry_run=args.dry_run,
            overwrite=args.overwrite,
            limit=args.limit,
            fetcher=fetcher,
        )
    finally:
        db.close()

    parts = [
        f"Geocoded: {counts['geocoded']}",
        f"Cached: {counts['cached']}",
        f"No result: {counts['no_result']}",
        f"Skipped (already set): {counts['skipped']}",
    ]
    if counts.get("errors"):
        parts.append(f"Errors: {counts['errors']}")
    print("   ".join(parts))
    if args.dry_run:
        print("(dry run — nothing written)")
    if counts.get("stopped_early"):
        print(
            "\nStopped early: 3 consecutive Nominatim errors — the service is likely "
            "rate-limiting this IP.\nWait a few hours before retrying. "
            "For large libraries, a local Nominatim instance (Docker) is more reliable."
        )
```

- [ ] **Step 3e: Add commented `geocoding:` section to `config/config.example.yml`**

After the `tag_protection:` block at the end of the file, append:

```yaml

# geocoding:
#   nominatim_url: "http://localhost:8080/reverse"  # local Docker instance; omit for public API
```

- [ ] **Step 3f: Run the full test suite and confirm it passes**

```bash
python -m pytest tests/ -q
```

Expected: all tests pass. The new `test_geocode_check_nominatim_flag_accepted` should now pass (connection refused → exit 1, no "unrecognized arguments").

- [ ] **Step 3g: Commit**

```bash
git add bp config/config.example.yml tests/test_bp_cli.py
git commit -m "$(cat <<'EOF'
feat(#225): wire --nominatim-url and --check-nominatim CLI flags

--check-nominatim exits before opening the config file, so no --config
is needed for Docker pre-flight checks. --nominatim-url takes precedence
over geocoding.nominatim_url in config.yml. When no URL is configured,
fetch_from_nominatim (the existing public-endpoint path) is used unchanged.
Closes #225

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Self-review notes

Spec coverage check:

| Spec requirement | Task covering it |
|---|---|
| `--nominatim-url` CLI flag | Task 3 |
| `geocoding.nominatim_url` in config | Task 3 |
| Rate-limiter auto-detect via `urlparse().netloc` | Task 1 (`make_fetcher`) |
| `make_fetcher` factory signature | Task 1 |
| `fetch_from_nominatim` preserved for default case | Task 3 (fetcher selection logic) |
| `check_nominatim_status` + `/status.php` | Task 2 |
| `--check-nominatim` early exit before config | Task 3 |
| `TestMakeFetcher` (4 tests) | Task 1 |
| `TestCheckNominatimStatus` (3 tests) | Task 2 |
| CLI smoke test for new flags | Task 3 |
| `config.example.yml` commented `geocoding:` section | Task 3 |
| Closure uses own `last_call_time`, not module-level | Task 1 (via `nonlocal`) |
