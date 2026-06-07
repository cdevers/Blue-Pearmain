# Spec: Nominatim reverse geocoding for place enrichment (#217)

_Status: approved_

---

## Problem

BP stores `place_city`, `place_state`, `place_country`, `place_neighborhood`, and `place_address` columns for each photo. These feed directly into `tagger.py`'s location-tag generation, which in turn produces searchable tags pushed to Flickr and written back to Photos. The columns are populated from Apple Photos metadata or the Flickr API — but a photo can have GPS coordinates with any or all place columns empty when neither source provides complete place data. Gaps persist silently and result in missing location tags.

---

## Approach

A new `poller/geocoder.py` module provides a `reverse_geocode(lat, lon, db)` function backed by a local `nominatim_cache` table. During the scan cycle, `scanner.py` calls it to fill any missing place columns for photos that have coordinates. A `bp geocode` command handles retroactive backfill of the existing library. Because the same locations recur across many photos, the local cache makes the ongoing per-call rate negligible after the initial run.

The existing `tagger.py` location-tag logic and the full review → Flickr push → Photos writeback pipeline require no changes — they already consume the place columns.

---

## Scope

**In:**
- New `poller/geocoder.py` with `PlaceData`, `LookupResult`, `fetch_from_nominatim`, `reverse_geocode`
- New `nominatim_cache` table (schema.sql + migration 029)
- Gap-fill call in `scanner.py`'s `build_enriched_row()`
- `bp geocode [--dry-run] [--overwrite] [--limit N]` backfill command
- New `db.get_nominatim_cache` / `db.set_nominatim_cache` / `db.update_place_data` methods

**Out:**
- Geofence zone auto-naming from Nominatim (deferred; see `future-directions.md`)
- Any changes to `tagger.py` or the tag push pipeline
- UI changes (the review UI already displays place columns)
- Support for non-Nominatim geocoding backends

---

## Data model

### `nominatim_cache`

Keyed by coordinates rounded to 3 decimal places. At the equator, 0.001° ≈ 111 m of latitude but only ≈ 78–111 m of longitude depending on latitude. The cache key is therefore slightly asymmetric in physical distance. Photos taken within approximately one city block of each other share a cache entry. Occasional neighbourhood or address inaccuracies at zone boundaries are an intentional tradeoff for cache efficiency — they are acceptable for tagging purposes.

All place fields nullable — an all-null row records that Nominatim returned nothing for those coordinates, preventing repeated retries.

```sql
CREATE TABLE IF NOT EXISTS nominatim_cache (
    lat_rounded        REAL NOT NULL,
    lon_rounded        REAL NOT NULL,
    place_city         TEXT,
    place_state        TEXT,
    place_country      TEXT,
    place_country_code TEXT,
    place_neighborhood TEXT,
    place_address      TEXT,
    fetched_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    PRIMARY KEY (lat_rounded, lon_rounded)
);
```

Added to `db/schema.sql` for fresh installs. Migration 030 (following the existing pattern in `db/migrations/`, including an entry in `schema_migrations`) handles existing databases.

---

## Module: `poller/geocoder.py`

### `PlaceData`

```python
@dataclass
class PlaceData:
    city:         str | None
    state:        str | None
    country:      str | None
    country_code: str | None
    neighborhood: str | None
    address:      str | None
```

### `LookupResult`

```python
@dataclass
class LookupResult:
    place:     PlaceData | None  # None means a network/HTTP error — not cached
    cache_hit: bool
```

`cache_hit=True` means the result came from `nominatim_cache` (no API call was made). `cache_hit=False` means a live API call was made. A `LookupResult` with `place` being an all-null `PlaceData` and `cache_hit=True` means coordinates are known to have no Nominatim result — future lookups for the same location return immediately without calling the API.

### `fetch_from_nominatim(lat, lon) -> PlaceData | None`

Makes an HTTP GET to the Nominatim reverse geocoding endpoint. Injectable for testing — `reverse_geocode` accepts this as a parameter with `fetch_from_nominatim` as the default.

```
GET https://nominatim.openstreetmap.org/reverse
    ?lat=...&lon=...&zoom=14&addressdetails=1&format=json
User-Agent: BluePearmain/1.0 (https://github.com/cdevers/Blue-Pearmain; contact: 1642218+cdevers@users.noreply.github.com)
```

`zoom=14` returns neighbourhood-level granularity. Address field mapping:

| Nominatim `address` key | `PlaceData` field |
|---|---|
| `neighbourhood` or `suburb` (first non-null) | `neighborhood` |
| `city`, `town`, or `village` (first non-null) | `city` |
| `state` | `state` |
| `country` | `country` |
| `country_code` | `country_code` |
| `display_name` (top-level) | `address` |

**Return values:**

- Returns `None` on network error or a 4xx/5xx response. These are **not cached** and are logged at WARNING level so persistent rejections (429, 403) are visible. The next scan cycle will retry.
- Returns a `PlaceData` with all-`None` fields if the API responds 200 but returns no address data for those coordinates. This **is cached** to prevent retrying unmapped coordinates.

**Rate limiting:** A module-level timestamp enforces a minimum 1-second gap between calls, per Nominatim's usage policy. If the last call was less than 1 second ago, the function sleeps the remainder before calling. BP's scanner and `bp geocode` are single-threaded, so no lock is needed around the timestamp.

### `reverse_geocode(lat, lon, db, fetcher=fetch_from_nominatim) -> LookupResult`

1. Round `lat` and `lon` to 3 decimal places.
2. Check `nominatim_cache` via `db.get_nominatim_cache(lat_r, lon_r)`.
3. Cache hit: return `LookupResult(place=cached_place_data, cache_hit=True)`.
4. Cache miss: call `fetcher(lat, lon)`.
   - If fetcher returns `None` (network/HTTP error): return `LookupResult(place=None, cache_hit=False)` without caching.
   - Otherwise: store the result in `nominatim_cache` and return `LookupResult(place=result, cache_hit=False)`.

---

## Scanner integration

In `scanner.py`'s `build_enriched_row()`, after the existing place extraction from `photo.place`, add:

```python
_PLACE_FIELDS = ("place_city", "place_state", "place_country", "place_neighborhood")

if row["latitude"] is not None and row["longitude"] is not None:
    if any(row[f] is None for f in _PLACE_FIELDS):
        result = reverse_geocode(row["latitude"], row["longitude"], db)
        if result.place:
            row["place_city"]         = row["place_city"]         or result.place.city
            row["place_state"]        = row["place_state"]        or result.place.state
            row["place_country"]      = row["place_country"]      or result.place.country
            row["place_country_code"] = row["place_country_code"] or result.place.country_code
            row["place_neighborhood"] = row["place_neighborhood"] or result.place.neighborhood
            row["place_address"]      = row["place_address"]      or result.place.address
```

The `is not None` check on coordinates is correct — latitude 0.0 is a valid coordinate. The `any(... is None ...)` guard triggers geocoding when any of the four key place fields is missing, not just `place_city`. The `or` pattern inside preserves Photos-sourced values — Nominatim fills only what is absent.

The scanner updates the in-memory `row` dict; the standard DB upsert downstream writes the final values. The `db.update_place_data` method is used only by `bp geocode`, not by the scanner.

`build_enriched_row()` must receive a `db` argument (verify during implementation; thread through from the caller if not already present).

---

## CLI command: `bp geocode`

Retroactive backfill for existing photos. After the first run, ongoing enrichment is handled automatically by the scan cycle.

**Performance note:** On a large library the first run may require many API calls at 1/sec. Running `bp geocode` before the first full scan is recommended — it populates `nominatim_cache` so that when `build_enriched_row()` runs, the geocoder path returns from cache immediately rather than blocking the scan for each photo.

**Flags:**
- `--dry-run` — report counts, write nothing
- `--overwrite` — replace existing place data with Nominatim results (default: fill gaps only)
- `--limit N` — stop after N **API call attempts** (cache hits do not count; failed network calls do count — a network attempt was made and consuming the slot prevents `--limit` from spinning indefinitely on persistent errors; `LookupResult.cache_hit` distinguishes cache hits from API attempts)

**Query:** `SELECT id, latitude, longitude, place_city, place_state, place_country, place_neighborhood FROM photos WHERE latitude IS NOT NULL AND (place_city IS NULL OR place_state IS NULL OR place_country IS NULL OR place_neighborhood IS NULL OR <overwrite>)`.

For each photo, calls `reverse_geocode()`. On a result with `place` data, writes via `db.update_place_data(photo_id, place_data, overwrite=args.overwrite)`. Increments `api_calls` when `not result.cache_hit`; stops when `api_calls >= limit` if `--limit` was passed.

**Output:**
```
Geocoded: 48   Cached: 312   No result: 7   Skipped (already set): 203
(dry run — nothing written)
```

- **Geocoded**: API calls (`not cache_hit`) that returned a result and were written
- **Cached**: lookups satisfied from `nominatim_cache` without an API call (`cache_hit=True`)
- **No result**: Nominatim returned no address data (stored as empty cache entry; counted whether from cache or fresh API call)
- **Skipped**: already had complete place data and `--overwrite` not passed

---

## `db/db.py` additions

- `get_nominatim_cache(lat_r: float, lon_r: float) -> dict[str, Any] | None` — returns `None` if **no row exists** (cache miss); returns a plain dict of the row's place columns if a row exists, even if all values are `None` (cached empty result). The distinction is Python `None` vs a dict instance — callers must not conflate the two. The DB layer returns raw dicts to avoid a circular import (`db/db.py` cannot import `PlaceData` from `poller/geocoder.py`); the geocoder converts the dict to `PlaceData`.
- `set_nominatim_cache(lat_r: float, lon_r: float, place_dict: dict[str, Any]) -> None` — `place_dict` keys: `place_city`, `place_state`, `place_country`, `place_country_code`, `place_neighborhood`, `place_address`.
- `update_place_data(photo_id: int, place_dict: dict[str, Any], overwrite: bool = False) -> None` — when `overwrite=False`, uses `COALESCE(existing, new)` semantics (only writes fields where the DB value is currently NULL); when `overwrite=True`, unconditionally sets all six place columns.

---

## Tests: `tests/test_geocoder.py`

All tests use an injectable `fetcher` — no real HTTP calls.

| Test | Platform |
|---|---|
| `test_reverse_geocode_cache_hit` — returns cached result with `cache_hit=True`; fetcher not called | any |
| `test_reverse_geocode_null_cache_hit_suppresses_api_call` — all-null cached entry returns `LookupResult(place=PlaceData(all None), cache_hit=True)` without calling fetcher | any |
| `test_reverse_geocode_cache_miss_stores_result` — on miss, calls fetcher, writes to cache, returns `cache_hit=False` | any |
| `test_reverse_geocode_cache_miss_null_result` — all-None result stored in cache; returned as `LookupResult(place=PlaceData(all None), cache_hit=False)` | any |
| `test_reverse_geocode_rounds_coordinates` — two coords within ~111 m resolve to same cache entry | any |
| `test_reverse_geocode_network_error` — fetcher raises; returns `LookupResult(place=None, cache_hit=False)`, nothing cached | any |
| `test_parse_nominatim_response_full` — all address fields present; correct mapping to PlaceData | any |
| `test_parse_nominatim_response_town_fallback` — no `city`; falls back to `town`, then `village` | any |
| `test_parse_nominatim_response_suburb_fallback` — no `neighbourhood`; falls back to `suburb` | any |
| `test_parse_nominatim_response_missing_fields` — sparse response; missing fields are None, no crash | any |
| `test_bp_geocode_fills_gaps` — fills place data for photo with coords and empty place columns | any |
| `test_bp_geocode_skips_existing` — photo has all place fields set; not overwritten without --overwrite | any |
| `test_bp_geocode_overwrite_flag` — --overwrite replaces existing place data | any |
| `test_bp_geocode_dry_run` — DB unchanged; counts reported correctly | any |
| `test_bp_geocode_limit` — stops after N API call attempts; cache hits do not count; failed network calls do count | any |
| `test_scanner_fills_place_from_geocoder` — `build_enriched_row()` calls geocoder when coords present and any place field is None | any |
| `test_scanner_skips_geocoder_when_all_place_set` — `build_enriched_row()` skips geocoder when all four key place fields are populated | any |
| `test_scanner_zero_zero_coordinates_not_skipped` — `(lat=0.0, lon=0.0)` (null island) is a valid coordinate pair; geocoder is called and neither value is treated as falsy | any |

---

## Implementation checklist

- [ ] Add `nominatim_cache` DDL to `db/schema.sql`
- [ ] Write migration 029 following existing pattern in `db/migrations/` (including `schema_migrations` entry)
- [ ] Write `tests/test_geocoder.py` (18 tests); confirm they fail; implement module; confirm pass
- [ ] Create `poller/geocoder.py` with `PlaceData`, `LookupResult`, `fetch_from_nominatim`, `reverse_geocode`
- [ ] Add `get_nominatim_cache`, `set_nominatim_cache`, `update_place_data` to `db/db.py`
- [ ] Wire `reverse_geocode()` into `scanner.py`'s `build_enriched_row()`
- [ ] Add `cmd_geocode` to `bp` and wire subparser + dispatch
- [ ] `make lint` — mypy clean
- [ ] `python -m pytest tests/ -q` — all pass
- [ ] Commit referencing #217
