# Spec: Nominatim reverse geocoding for place enrichment (#217)

_Status: draft_

---

## Problem

BP stores `place_city`, `place_state`, `place_country`, `place_neighborhood`, and `place_address` columns for each photo. These feed directly into `tagger.py`'s location-tag generation, which in turn produces searchable tags pushed to Flickr and written back to Photos. The columns are populated from Apple Photos metadata or the Flickr API — but a photo can have GPS coordinates with all place columns empty when neither source provides place data. Gaps persist silently and result in missing location tags.

---

## Approach

A new `poller/geocoder.py` module provides a `reverse_geocode(lat, lon, db)` function backed by a local `nominatim_cache` table. During the scan cycle, `scanner.py` calls it to fill empty place columns for any photo that has coordinates. A `bp geocode` command handles retroactive backfill of the existing library. Because the same locations recur across many photos, the local cache makes the ongoing per-call rate negligible after the initial run.

The existing `tagger.py` location-tag logic and the full review → Flickr push → Photos writeback pipeline require no changes — they already consume the place columns.

---

## Scope

**In:**
- New `poller/geocoder.py` with `PlaceData`, `fetch_from_nominatim`, `reverse_geocode`
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

Keyed by coordinates rounded to 3 decimal places (~111 m precision). Photos taken in the same neighbourhood share a cache entry. All place fields nullable — an all-null row records that Nominatim returned nothing for those coordinates, preventing repeated retries.

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

Added to `db/schema.sql` for fresh installs; migration 029 handles existing databases.

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

### `fetch_from_nominatim(lat, lon) -> PlaceData | None`

Makes an HTTP GET to the Nominatim reverse geocoding endpoint. Injectable for testing — `reverse_geocode` accepts this as a parameter with `fetch_from_nominatim` as the default.

```
GET https://nominatim.openstreetmap.org/reverse
    ?lat=...&lon=...&zoom=14&addressdetails=1&format=json
User-Agent: BluePearmain/1.0 (https://github.com/cdevers/Blue-Pearmain)
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

Returns `None` on network error or non-200 response (not cached — will retry next cycle).
Returns a `PlaceData` with all-`None` fields if the API responds but has no address data (this is cached to prevent retrying unmapped coordinates).

**Rate limiting:** A module-level timestamp enforces a minimum 1-second gap between calls, per Nominatim's usage policy. If the last call was less than 1 second ago, the function sleeps the remainder before calling.

### `reverse_geocode(lat, lon, db, fetcher=fetch_from_nominatim) -> PlaceData | None`

1. Round `lat` and `lon` to 3 decimal places.
2. Check `nominatim_cache` via `db.get_nominatim_cache(lat_r, lon_r)`.
3. Cache hit: return the cached `PlaceData` (may be all-`None`).
4. Cache miss: call `fetcher(lat, lon)`. If it returns `None` (network error), return `None` without caching. Otherwise store the result in `nominatim_cache` and return it.

---

## Scanner integration

In `scanner.py`'s `build_enriched_row()`, after the existing place extraction from `photo.place`, add:

```python
if row["latitude"] and not row["place_city"]:
    place = reverse_geocode(row["latitude"], row["longitude"], db)
    if place:
        row["place_city"]          = row["place_city"]         or place.city
        row["place_state"]         = row["place_state"]        or place.state
        row["place_country"]       = row["place_country"]      or place.country
        row["place_country_code"]  = row["place_country_code"] or place.country_code
        row["place_neighborhood"]  = row["place_neighborhood"] or place.neighborhood
        row["place_address"]       = row["place_address"]      or place.address
```

The `or` pattern preserves Photos-sourced values — Nominatim only fills what is missing. The `not row["place_city"]` guard is the fast-path exit: if the city is already known, no cache lookup is needed.

`build_enriched_row()` must receive a `db` argument (verify during implementation; thread through from the caller if not already present).

---

## CLI command: `bp geocode`

Retroactive backfill for existing photos. After the first run, ongoing enrichment is handled automatically by the scan cycle.

**Flags:**
- `--dry-run` — report counts, write nothing
- `--overwrite` — replace existing place data with Nominatim results (default: fill gaps only)
- `--limit N` — stop after N API calls (cache hits do not count toward the limit)

**Query:** `SELECT id, latitude, longitude, place_city FROM photos WHERE latitude IS NOT NULL AND (place_city IS NULL OR <overwrite>)`.

For each photo, calls `reverse_geocode()`. Writes results via `db.update_place_data(photo_id, place_data)`.

**Output:**
```
Geocoded: 48   Cached: 312   No result: 7   Skipped (already set): 203
(dry run — nothing written)
```

- **Geocoded**: API calls that returned a result and were written
- **Cached**: lookups satisfied from `nominatim_cache` without an API call
- **No result**: Nominatim returned no address data (stored as empty cache entry)
- **Skipped**: already had place data and `--overwrite` not passed

---

## `db/db.py` additions

- `get_nominatim_cache(lat_r: float, lon_r: float) -> PlaceData | None`
- `set_nominatim_cache(lat_r: float, lon_r: float, place: PlaceData) -> None`
- `update_place_data(photo_id: int, place: PlaceData, overwrite: bool = False) -> None` — UPDATE place columns; when `overwrite=False`, only writes fields where the existing DB value is NULL

---

## Tests: `tests/test_geocoder.py`

All tests use an injectable `fetcher` — no real HTTP calls.

| Test | Platform |
|---|---|
| `test_reverse_geocode_cache_hit` — returns cached result; fetcher not called | any |
| `test_reverse_geocode_cache_miss_stores_result` — on miss, calls fetcher and writes to cache | any |
| `test_reverse_geocode_cache_miss_null_result` — all-None result stored; coordinates not retried | any |
| `test_reverse_geocode_rounds_coordinates` — two coords within 111 m resolve to same cache entry | any |
| `test_reverse_geocode_network_error` — fetcher raises; returns None, nothing cached | any |
| `test_parse_nominatim_response_full` — all address fields present; correct mapping to PlaceData | any |
| `test_parse_nominatim_response_town_fallback` — no `city`; falls back to `town`, then `village` | any |
| `test_parse_nominatim_response_suburb_fallback` — no `neighbourhood`; falls back to `suburb` | any |
| `test_parse_nominatim_response_missing_fields` — sparse response; missing fields are None, no crash | any |
| `test_bp_geocode_fills_gaps` — fills place data for photo with coords and empty place columns | any |
| `test_bp_geocode_skips_existing` — photo has place_city; not overwritten without --overwrite | any |
| `test_bp_geocode_overwrite_flag` — --overwrite replaces existing place data | any |
| `test_bp_geocode_dry_run` — DB unchanged; counts reported correctly | any |
| `test_scanner_fills_place_from_geocoder` — build_enriched_row() calls geocoder when coords present, place empty | any |
| `test_scanner_skips_geocoder_when_place_set` — build_enriched_row() skips geocoder when place_city populated | any |

---

## Implementation checklist

- [ ] Add `nominatim_cache` DDL to `db/schema.sql`
- [ ] Write migration 029 (`CREATE TABLE IF NOT EXISTS nominatim_cache`)
- [ ] Write `tests/test_geocoder.py` (15 tests); confirm they fail; implement module; confirm pass
- [ ] Create `poller/geocoder.py` with `PlaceData`, `fetch_from_nominatim`, `reverse_geocode`
- [ ] Add `get_nominatim_cache`, `set_nominatim_cache`, `update_place_data` to `db/db.py`
- [ ] Wire `reverse_geocode()` into `scanner.py`'s `build_enriched_row()`
- [ ] Add `cmd_geocode` to `bp` and wire subparser + dispatch
- [ ] `make lint` — mypy clean
- [ ] `python -m pytest tests/ -q` — all pass
- [ ] Commit referencing #217
