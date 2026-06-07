# Local Nominatim Docker Support — Spec (#225)

## Problem

The public Nominatim API rate-limits bulk geocoding and will ban IPs that make systematic
requests. The 429 backoff added in #219 handles transient throttling but does not solve the
underlying policy conflict: `bp geocode` on a fresh library makes thousands of sequential
requests, which is exactly the pattern Nominatim's terms of service prohibit.

The long-term solution is a local Nominatim instance loaded from an OpenStreetMap extract.
After one bulk `bp geocode` run against the local instance, `nominatim_cache` covers every
coordinate in the library. All future runs hit the cache and make zero API calls. The Docker
container is then disposable.

---

## Design

### URL configuration

The Nominatim endpoint is made configurable at two levels:

- **`config.yml`** under `geocoding.nominatim_url` — persists for repeated use
- **`bp geocode --nominatim-url URL`** — one-shot CLI override (takes precedence over config)

If neither is set, the default public endpoint
(`https://nominatim.openstreetmap.org/reverse`) is used unchanged.

### Rate-limiter auto-detect

The 1 req/sec rate limiter is only required by the public Nominatim policy. When the
effective URL is anything other than `https://nominatim.openstreetmap.org/reverse`, the
delay is set to 0. This is auto-detected — no config knob needed.

### Fetcher factory

A new function `make_fetcher(url, *, min_delay=None)` in `poller/geocoder.py` returns a
closure that binds the effective URL and delay. `cmd_geocode` resolves the URL (CLI > config
> None), calls `make_fetcher(url)` when a non-default URL is configured, and passes the
closure to `run_geocode` as `fetcher=`. When no URL is configured, `fetch_from_nominatim`
is used directly (no change to the existing default path).

`make_fetcher` signature:

```python
def make_fetcher(
    url: str | None = None,
    *,
    min_delay: float | None = None,
) -> Callable[[float, float], "PlaceData | None"]:
```

- `url=None` uses the public endpoint.
- `min_delay=None` auto-detects: 1.0s for the public endpoint, 0.0s for anything else.
- `min_delay` can be passed explicitly to override (kept private; not exposed in config or CLI).

The closure contains the full fetch logic (same as `fetch_from_nominatim`) using the bound
URL and delay. `fetch_from_nominatim` is preserved as-is for the default case and for all
existing tests.

### Readiness check

`bp geocode --check-nominatim` hits the configured endpoint's `/status.php` path (derived
from the effective URL via `urllib.parse.urlparse` — strip path, append `/status.php`) and
prints a single line:

```
Nominatim OK — http://localhost:8080 (data updated: 2024-01-15T12:00:00+00:00)
```

or on failure:

```
Nominatim unreachable — http://localhost:8080 (connection refused)
```

The command exits 0 on success, 1 on failure. No database is opened; `--check-nominatim`
is a pre-flight check and exits before the geocoding loop runs.

A helper `check_nominatim_status(url: str) -> tuple[bool, str]` in `geocoder.py` handles
the HTTP call and message formatting; `cmd_geocode` calls it and prints the result.

---

## Files changed

| File | Change |
|------|--------|
| `poller/geocoder.py` | Add `make_fetcher`, add `check_nominatim_status` |
| `bp` | Add `--nominatim-url` and `--check-nominatim` args; update `cmd_geocode` |
| `config/config.example.yml` | Add commented `geocoding:` section |
| `tests/test_geocoder.py` | Add `TestMakeFetcher`, `TestCheckNominatimStatus` |
| `tests/test_bp_cli.py` | Smoke-test new flags |

---

## Behaviour details

### `cmd_geocode` URL resolution

```python
nominatim_url = args.nominatim_url or config.get("geocoding", {}).get("nominatim_url")

if args.check_nominatim:
    ok, msg = check_nominatim_status(nominatim_url or _PUBLIC_NOMINATIM_URL)
    print(msg)
    sys.exit(0 if ok else 1)

fetcher = make_fetcher(nominatim_url) if nominatim_url else fetch_from_nominatim
counts = run_geocode(db, dry_run=args.dry_run, overwrite=args.overwrite,
                     limit=args.limit, fetcher=fetcher)
```

### `config.example.yml` addition

```yaml
# geocoding:
#   nominatim_url: "http://localhost:8080/reverse"  # local Docker instance; omit for public API
```

### Rate limiter in the closure

The closure returned by `make_fetcher` uses its own `last_call_time` (local variable, not
the module-level `_last_call_time`). This prevents a local-instance run from polluting the
shared rate-limiter state for any subsequent default-fetcher call in the same process.

---

## Tests

**`TestMakeFetcher`** (4 tests):
- `test_make_fetcher_default_url_uses_public_endpoint` — closure passes correct URL to requests
- `test_make_fetcher_public_url_delay_is_1s` — delay is 1.0 when URL is the public endpoint
- `test_make_fetcher_local_url_delay_is_0` — delay is 0.0 for any other URL
- `test_make_fetcher_explicit_min_delay_overrides_auto` — `min_delay=0.5` is respected

**`TestCheckNominatimStatus`** (3 tests):
- `test_check_status_ok` — 200 with `{"status": 0, "message": "OK", "data_updated": "..."}` → returns `(True, "Nominatim OK — ...")`
- `test_check_status_http_error` — non-200 response → returns `(False, "Nominatim unreachable — ...")`
- `test_check_status_connection_error` — `requests.ConnectionError` → returns `(False, "Nominatim unreachable — ...")`

**`test_bp_cli.py`** additions:
- `--check-nominatim` is not in `DRY_RUN_SUBCOMMANDS` (it exits before the DB; no `--dry-run` needed)
- The `--help` smoke test covers `geocode` already; no new subcommand entries needed
- Add one test: `test_geocode_check_nominatim_flag_accepted` — `bp geocode --check-nominatim --config /dev/null/nonexistent.yml` exits without "unrecognized arguments"

---

## Typical Docker workflow (documentation only — no BP changes needed)

```bash
# 1. Start local Nominatim with a regional OSM extract
docker run -d --name nominatim -p 8080:8080 \
  -e PBF_URL=https://download.geofabrik.de/north-america/us-northeast-latest.osm.pbf \
  mediagis/nominatim:4.4

# 2. Wait for data load (30 min – a few hours)
bp geocode --check-nominatim --nominatim-url http://localhost:8080/reverse

# 3. Bulk-geocode with no rate concern
bp geocode --nominatim-url http://localhost:8080/reverse

# 4. Remove the container — cache covers all known coordinates
docker stop nominatim && docker rm nominatim
```

Region selection: a US Northeast extract (~2–5 GB) covers a geographically concentrated
library. For a globe-spanning library, the full planet extract (~70 GB) is available from
Geofabrik but takes significantly longer to load.

---

## Out of scope

- Automatic Docker lifecycle management (spin up / spin down from within BP)
- Multi-region extract merging
- Scheduled re-geocoding of new photos against the local instance
