# Nominatim Reverse Geocoding Implementation Plan (#217)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a local-caching Nominatim geocoder that fills missing `place_*` columns from GPS coordinates during scan and via a `bp geocode` backfill command.

**Architecture:** A new `poller/geocoder.py` module wraps Nominatim reverse geocoding behind a local `nominatim_cache` SQLite table keyed by coordinates rounded to 3 decimal places. The scanner calls it inline to fill gaps; `bp geocode` handles retroactive backfill. `tagger.py` and the Flickr push pipeline require no changes.

**Tech Stack:** Python 3.11+, SQLite (existing), `requests` (already a project dependency), `dataclasses` (stdlib)

---

## Critical: the cache/miss/null three-way distinction

This is the most error-prone part of the implementation. **Do not collapse it.**

`db.get_nominatim_cache(lat_r, lon_r)` returns:
- Python `None` — no row in `nominatim_cache` for these coordinates (cache **miss**)
- A `PlaceData` instance — a row exists (cache **hit**); all fields may be `None` if Nominatim returned nothing

These two cases are semantically different. A cache miss means "we haven't asked Nominatim yet." A hit with all-null `PlaceData` means "we asked and got nothing — don't ask again." Collapsing both to `None` would cause infinite retries on coordinates outside Nominatim's coverage.

`LookupResult.cache_hit` preserves this distinction for callers:
- `cache_hit=True` → result came from `nominatim_cache` (no API call made)
- `cache_hit=False` → a live API call was made (or attempted)

The `--limit N` flag counts API call *attempts* (not successes); `LookupResult.cache_hit` provides that count.

---

## File map

| File | Action | Responsibility |
|------|--------|----------------|
| `db/schema.sql` | Modify | Add `nominatim_cache` DDL for fresh installs |
| `db/migrations/migrate_030_nominatim_cache.py` | Create | Upgrade existing DBs; record in `schema_migrations` |
| `db/db.py` | Modify | Add `get_nominatim_cache`, `set_nominatim_cache`, `update_place_data` |
| `poller/geocoder.py` | Create | `PlaceData`, `LookupResult`, `_parse_nominatim_response`, `fetch_from_nominatim`, `reverse_geocode` |
| `poller/scanner.py` | Modify | Add optional `db` param to `build_enriched_row`; call geocoder |
| `bp` | Modify | Add `cmd_geocode`, subparser wiring, dispatch entry |
| `tests/test_migrate_030.py` | Create | Migration correctness and idempotency |
| `tests/test_geocoder.py` | Create | 18 tests from spec (injectable fetcher — no real HTTP) |

---

## Task 1: Schema + Migration 030

**Files:**
- Modify: `db/schema.sql` (append at end)
- Create: `db/migrations/migrate_030_nominatim_cache.py`
- Create: `tests/test_migrate_030.py`

- [ ] **Step 1: Write failing migration tests**

Create `tests/test_migrate_030.py`:

```python
"""Migration 030 — add nominatim_cache table (#217)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))


def _fresh_db() -> sqlite3.Connection:
    """Minimal in-memory DB without nominatim_cache."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE schema_migrations (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT UNIQUE NOT NULL,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE photos (
            id        INTEGER PRIMARY KEY,
            latitude  REAL,
            longitude REAL
        );
    """)
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _run(conn: sqlite3.Connection) -> None:
    from db.migrations.migrate_030_nominatim_cache import run_on_conn
    run_on_conn(conn)


class TestMigrate030:
    def test_creates_nominatim_cache_table(self):
        conn = _fresh_db()
        _run(conn)
        assert "nominatim_cache" in _tables(conn)

    def test_table_has_expected_columns(self):
        conn = _fresh_db()
        _run(conn)
        cols = _cols(conn, "nominatim_cache")
        assert "lat_rounded" in cols
        assert "lon_rounded" in cols
        assert "place_city" in cols
        assert "place_state" in cols
        assert "place_country" in cols
        assert "place_country_code" in cols
        assert "place_neighborhood" in cols
        assert "place_address" in cols
        assert "fetched_at" in cols

    def test_idempotent(self):
        conn = _fresh_db()
        _run(conn)
        _run(conn)  # must not raise
        assert "nominatim_cache" in _tables(conn)

    def test_recorded_in_schema_migrations(self):
        conn = _fresh_db()
        _run(conn)
        row = conn.execute(
            "SELECT name FROM schema_migrations WHERE name = 'migrate_030_nominatim_cache'"
        ).fetchone()
        assert row is not None

    def test_place_fields_nullable(self):
        conn = _fresh_db()
        _run(conn)
        # All place fields should allow NULL (for caching "no result" entries)
        conn.execute(
            "INSERT INTO nominatim_cache (lat_rounded, lon_rounded, fetched_at) VALUES (1.0, 2.0, '2026-01-01T00:00:00Z')"
        )
        conn.commit()
        row = conn.execute("SELECT place_city FROM nominatim_cache").fetchone()
        assert row["place_city"] is None
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd /Users/cdevers/Documents/GitHub/Blue\ Pearmain
python -m pytest tests/test_migrate_030.py -v
```

Expected: `ModuleNotFoundError` or `ImportError` — `migrate_030_nominatim_cache` does not exist yet.

- [ ] **Step 3: Add nominatim_cache DDL to db/schema.sql**

Append to the end of `db/schema.sql` (after the `person_birthdays` table block):

```sql


-- ============================================================
-- Nominatim reverse geocoding cache
-- Keyed by coordinates rounded to 3 decimal places (~111 m).
-- All place fields nullable — an all-null row records that
-- Nominatim returned nothing, preventing repeated retries.
-- ============================================================

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

- [ ] **Step 4: Create db/migrations/migrate_030_nominatim_cache.py**

```python
"""
migrate_030_nominatim_cache.py

Create the nominatim_cache table for reverse geocoding results (#217).

Idempotent: skips if already applied.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import yaml

MIGRATION_NAME = "migrate_030_nominatim_cache"


def run_on_conn(conn: sqlite3.Connection) -> None:
    if conn.row_factory is None:
        conn.row_factory = sqlite3.Row

    try:
        row = conn.execute(
            "SELECT id FROM schema_migrations WHERE name = ?", (MIGRATION_NAME,)
        ).fetchone()
        if row is not None:
            return
    except sqlite3.OperationalError:
        # schema_migrations table doesn't exist yet — proceed with migration
        pass

    conn.execute("BEGIN")

    conn.execute("""
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
        )
    """)

    conn.execute(
        "INSERT INTO schema_migrations (name, applied_at) "
        "VALUES (?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))",
        (MIGRATION_NAME,),
    )
    conn.execute("COMMIT")


def run(db_path: str, dry_run: bool = False) -> None:
    if dry_run:
        print("  [dry-run] Would create nominatim_cache table")
        return
    conn = sqlite3.connect(db_path)
    run_on_conn(conn)
    conn.close()
    print("  Applied:  migrate_030_nominatim_cache")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migration 030: create nominatim_cache table"
    )
    parser.add_argument("--config", default="config/config.yml")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    with open(args.config) as f:
        config = yaml.safe_load(f)
    db_path = str(Path(config["database"]["path"]).expanduser())
    run(db_path, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Run migration tests to confirm they pass**

```bash
python -m pytest tests/test_migrate_030.py -v
```

Expected: 5 tests PASS.

- [ ] **Step 6: Run full suite to check for regressions**

```bash
python -m pytest tests/ -q
```

Expected: all existing tests pass.

- [ ] **Step 7: Commit**

```bash
git add db/schema.sql db/migrations/migrate_030_nominatim_cache.py tests/test_migrate_030.py
git commit -m "feat(#217): migration 030 — create nominatim_cache table"
```

---

## Task 2: DB methods — get_nominatim_cache, set_nominatim_cache, update_place_data

**Files:**
- Modify: `db/db.py` (append after the `# Person birthdays` section, around line 1027)

The three-way distinction note applies here: `get_nominatim_cache` returns Python `None` for a cache miss (no row), and a `PlaceData` instance for a cache hit (row exists, fields may be all `None`). Callers must not treat a hit with all-null fields the same as a miss.

These methods are tested implicitly by the geocoder tests in Task 4. No separate test file for DB methods.

- [ ] **Step 1: Add imports to db/db.py**

At the top of `db/db.py`, find the existing `from __future__ import annotations` line. Check whether `PlaceData` needs to be imported. It does — but that would create a circular import (db imports from geocoder, geocoder imports from db). Instead, use a forward reference or accept `Any` typed dict. 

**The right approach:** DB methods work with raw column values, not `PlaceData` objects. `PlaceData` lives in `geocoder.py`. The DB methods accept/return raw primitives. The `geocoder.py` module handles conversion. So:

- `get_nominatim_cache(lat_r, lon_r) -> dict[str, Any] | None` — returns raw dict from the row, or `None` if no row. The `geocoder.py` caller converts to `PlaceData`.
- `set_nominatim_cache(lat_r, lon_r, place_dict: dict[str, Any]) -> None` — accepts raw dict.
- `update_place_data(photo_id, place_dict: dict[str, Any], overwrite: bool = False) -> None`

This avoids any circular import.

- [ ] **Step 2: Add the three DB methods**

In `db/db.py`, after the `delete_person_birthday` method (around line 1026), add:

```python
    # -----------------------------------------------------------------------
    # Nominatim geocoding cache (#217)
    # -----------------------------------------------------------------------

    def get_nominatim_cache(self, lat_r: float, lon_r: float) -> "dict[str, Any] | None":
        """Return the cached row for (lat_r, lon_r) as a plain dict, or None if no row.

        A return value of None means cache miss — no API call has been made for
        these coordinates. A returned dict (even with all place fields None) means
        cache hit — Nominatim was queried and returned no address data. Callers
        MUST NOT conflate the two.
        """
        row = self.conn.execute(
            "SELECT place_city, place_state, place_country, place_country_code, "
            "place_neighborhood, place_address "
            "FROM nominatim_cache WHERE lat_rounded = ? AND lon_rounded = ?",
            (lat_r, lon_r),
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    def set_nominatim_cache(
        self, lat_r: float, lon_r: float, place_dict: "dict[str, Any]"
    ) -> None:
        """Insert or replace a nominatim_cache row for (lat_r, lon_r).

        place_dict keys: place_city, place_state, place_country, place_country_code,
        place_neighborhood, place_address. Any key may be None.
        """
        self.conn.execute(
            """INSERT INTO nominatim_cache
               (lat_rounded, lon_rounded, place_city, place_state, place_country,
                place_country_code, place_neighborhood, place_address)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(lat_rounded, lon_rounded) DO UPDATE SET
                   place_city=excluded.place_city,
                   place_state=excluded.place_state,
                   place_country=excluded.place_country,
                   place_country_code=excluded.place_country_code,
                   place_neighborhood=excluded.place_neighborhood,
                   place_address=excluded.place_address,
                   fetched_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')""",
            (
                lat_r,
                lon_r,
                place_dict.get("place_city"),
                place_dict.get("place_state"),
                place_dict.get("place_country"),
                place_dict.get("place_country_code"),
                place_dict.get("place_neighborhood"),
                place_dict.get("place_address"),
            ),
        )
        self.conn.commit()

    def update_place_data(
        self,
        photo_id: int,
        place_dict: "dict[str, Any]",
        overwrite: bool = False,
    ) -> None:
        """Write place data for photo_id from place_dict.

        overwrite=False: fill gaps only (COALESCE semantics — only writes fields
            where the DB value is currently NULL).
        overwrite=True: unconditionally set all six place columns.

        Only used by bp geocode. The scanner updates the in-memory row dict instead.
        """
        if overwrite:
            self.conn.execute(
                """UPDATE photos SET
                       place_city=?, place_state=?, place_country=?,
                       place_country_code=?, place_neighborhood=?, place_address=?
                   WHERE id=?""",
                (
                    place_dict.get("place_city"),
                    place_dict.get("place_state"),
                    place_dict.get("place_country"),
                    place_dict.get("place_country_code"),
                    place_dict.get("place_neighborhood"),
                    place_dict.get("place_address"),
                    photo_id,
                ),
            )
        else:
            self.conn.execute(
                """UPDATE photos SET
                       place_city=COALESCE(place_city, ?),
                       place_state=COALESCE(place_state, ?),
                       place_country=COALESCE(place_country, ?),
                       place_country_code=COALESCE(place_country_code, ?),
                       place_neighborhood=COALESCE(place_neighborhood, ?),
                       place_address=COALESCE(place_address, ?)
                   WHERE id=?""",
                (
                    place_dict.get("place_city"),
                    place_dict.get("place_state"),
                    place_dict.get("place_country"),
                    place_dict.get("place_country_code"),
                    place_dict.get("place_neighborhood"),
                    place_dict.get("place_address"),
                    photo_id,
                ),
            )
        self.conn.commit()
```

- [ ] **Step 3: Run full test suite to confirm nothing broken**

```bash
python -m pytest tests/ -q
```

Expected: all existing tests pass. (DB method tests come in Task 4 via geocoder tests.)

- [ ] **Step 4: Commit**

```bash
git add db/db.py
git commit -m "feat(#217): add get_nominatim_cache, set_nominatim_cache, update_place_data to db.py"
```

---

## Task 3: poller/geocoder.py — PlaceData, LookupResult, _parse_nominatim_response

**Files:**
- Create: `tests/test_geocoder.py` (parse tests only in this task)
- Create: `poller/geocoder.py` (partial — dataclasses and parse function only)

- [ ] **Step 1: Write failing parse tests**

Create `tests/test_geocoder.py`:

```python
"""Tests for poller/geocoder.py — Nominatim reverse geocoding (#217).

All tests use an injectable fetcher — no real HTTP calls are made.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "poller"))

from db.db import Database
from geocoder import (
    LookupResult,
    PlaceData,
    _parse_nominatim_response,
    reverse_geocode,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _db(tmp_path: Path) -> Database:
    return Database(str(tmp_path / "curator.db"))


def _place(**kwargs) -> PlaceData:
    defaults = dict(city=None, state=None, country=None,
                    country_code=None, neighborhood=None, address=None)
    defaults.update(kwargs)
    return PlaceData(**defaults)


# ---------------------------------------------------------------------------
# _parse_nominatim_response
# ---------------------------------------------------------------------------


class TestParseNominatimResponse:
    def test_parse_nominatim_response_full(self):
        data = {
            "display_name": "14 High Street, Somerville, Massachusetts, United States",
            "address": {
                "neighbourhood": "Winter Hill",
                "city": "Somerville",
                "state": "Massachusetts",
                "country": "United States",
                "country_code": "us",
            },
        }
        result = _parse_nominatim_response(data)
        assert result.neighborhood == "Winter Hill"
        assert result.city == "Somerville"
        assert result.state == "Massachusetts"
        assert result.country == "United States"
        assert result.country_code == "us"
        assert result.address == "14 High Street, Somerville, Massachusetts, United States"

    def test_parse_nominatim_response_town_fallback(self):
        # No 'city' key — should fall back to 'town', then 'village'
        data = {
            "display_name": "Some Town, MA, US",
            "address": {
                "town": "Acton",
                "state": "Massachusetts",
                "country": "United States",
                "country_code": "us",
            },
        }
        result = _parse_nominatim_response(data)
        assert result.city == "Acton"

    def test_parse_nominatim_response_village_fallback(self):
        # No 'city' or 'town' — fall back to 'village'
        data = {
            "display_name": "Someplace, rural",
            "address": {
                "village": "Podunk",
                "state": "Maine",
                "country": "United States",
                "country_code": "us",
            },
        }
        result = _parse_nominatim_response(data)
        assert result.city == "Podunk"

    def test_parse_nominatim_response_suburb_fallback(self):
        # No 'neighbourhood' key — fall back to 'suburb'
        data = {
            "display_name": "Some area",
            "address": {
                "suburb": "Davis Square",
                "city": "Somerville",
                "state": "Massachusetts",
                "country": "United States",
                "country_code": "us",
            },
        }
        result = _parse_nominatim_response(data)
        assert result.neighborhood == "Davis Square"
        assert result.city == "Somerville"

    def test_parse_nominatim_response_missing_fields(self):
        # Sparse response — only country present
        data = {
            "display_name": "Somewhere",
            "address": {
                "country": "France",
                "country_code": "fr",
            },
        }
        result = _parse_nominatim_response(data)
        assert result.country == "France"
        assert result.country_code == "fr"
        assert result.city is None
        assert result.state is None
        assert result.neighborhood is None
```

- [ ] **Step 2: Run parse tests to confirm they fail**

```bash
python -m pytest tests/test_geocoder.py::TestParseNominatimResponse -v
```

Expected: `ModuleNotFoundError` — `geocoder` does not exist yet.

- [ ] **Step 3: Create poller/geocoder.py with dataclasses and parse function**

```python
"""Nominatim reverse geocoding for place enrichment (#217).

Provides:
  PlaceData           — six place fields extracted from a Nominatim response
  LookupResult        — wraps PlaceData | None with a cache_hit flag
  _parse_nominatim_response — parse a raw Nominatim JSON dict into PlaceData
  fetch_from_nominatim — HTTP call to Nominatim (injectable for testing)
  reverse_geocode     — cache-first lookup; calls fetcher on miss
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger("blue-pearmain.geocoder")

_USER_AGENT = (
    "BluePearmain/1.0 "
    "(https://github.com/cdevers/Blue-Pearmain; "
    "contact: 1642218+cdevers@users.noreply.github.com)"
)
_NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
_last_call_time: float = 0.0  # module-level rate limiter (single-threaded)


@dataclass
class PlaceData:
    city:         str | None
    state:        str | None
    country:      str | None
    country_code: str | None
    neighborhood: str | None
    address:      str | None


@dataclass
class LookupResult:
    """Wraps a geocoder result with cache provenance.

    place=None means a network/HTTP error occurred — not cached.
    place=PlaceData(all None) + cache_hit=True means coordinates are known
        to have no Nominatim result — no API call will be retried.
    cache_hit=True  → result came from nominatim_cache; no API call was made.
    cache_hit=False → a live API call was made (or attempted).
    """
    place:     PlaceData | None
    cache_hit: bool


def _parse_nominatim_response(data: dict[str, Any]) -> PlaceData:
    """Parse a raw Nominatim JSON response dict into a PlaceData.

    Returns a PlaceData with all-None fields if address data is absent.
    Address field mapping:
      neighbourhood or suburb (first non-null) → neighborhood
      city, town, or village (first non-null)  → city
      state                                    → state
      country                                  → country
      country_code                             → country_code
      display_name (top-level)                 → address
    """
    addr = data.get("address") or {}
    neighborhood = addr.get("neighbourhood") or addr.get("suburb")
    city = addr.get("city") or addr.get("town") or addr.get("village")
    return PlaceData(
        city=city or None,
        state=addr.get("state") or None,
        country=addr.get("country") or None,
        country_code=addr.get("country_code") or None,
        neighborhood=neighborhood or None,
        address=data.get("display_name") or None,
    )
```

- [ ] **Step 4: Run parse tests to confirm they pass**

```bash
python -m pytest tests/test_geocoder.py::TestParseNominatimResponse -v
```

Expected: 5 tests PASS.

- [ ] **Step 5: Run full suite**

```bash
python -m pytest tests/ -q
```

Expected: all existing tests pass.

- [ ] **Step 6: Commit**

```bash
git add poller/geocoder.py tests/test_geocoder.py
git commit -m "feat(#217): geocoder.py — PlaceData, LookupResult, _parse_nominatim_response"
```

---

## Task 4: poller/geocoder.py — fetch_from_nominatim + reverse_geocode

**Files:**
- Modify: `poller/geocoder.py` (add `fetch_from_nominatim` and `reverse_geocode`)
- Modify: `tests/test_geocoder.py` (add cache logic tests)

- [ ] **Step 1: Add cache logic tests to tests/test_geocoder.py**

Append to `tests/test_geocoder.py` after `TestParseNominatimResponse`:

```python

# ---------------------------------------------------------------------------
# reverse_geocode — cache logic
# ---------------------------------------------------------------------------


class TestReverseGeocode:
    def test_reverse_geocode_cache_hit(self, tmp_path: Path):
        db = _db(tmp_path)
        db.set_nominatim_cache(42.361, -71.057, {
            "place_city": "Somerville",
            "place_state": "Massachusetts",
            "place_country": "United States",
            "place_country_code": "us",
            "place_neighborhood": "Winter Hill",
            "place_address": "Somerville, MA, US",
        })
        fetcher_called = []

        def fake_fetcher(lat: float, lon: float) -> PlaceData | None:
            fetcher_called.append((lat, lon))
            return None

        result = reverse_geocode(42.3614, -71.0572, db, fetcher=fake_fetcher)
        assert result.cache_hit is True
        assert result.place is not None
        assert result.place.city == "Somerville"
        assert fetcher_called == []  # fetcher must NOT be called on cache hit

    def test_reverse_geocode_null_cache_hit_suppresses_api_call(self, tmp_path: Path):
        # All-null cached entry → cache hit, no API call, PlaceData with all None fields
        db = _db(tmp_path)
        db.set_nominatim_cache(10.0, 20.0, {
            "place_city": None, "place_state": None, "place_country": None,
            "place_country_code": None, "place_neighborhood": None, "place_address": None,
        })
        fetcher_called = []

        def fake_fetcher(lat: float, lon: float) -> PlaceData | None:
            fetcher_called.append((lat, lon))
            return None

        result = reverse_geocode(10.0, 20.0, db, fetcher=fake_fetcher)
        assert result.cache_hit is True
        assert result.place is not None  # PlaceData instance, not Python None
        assert result.place.city is None
        assert fetcher_called == []

    def test_reverse_geocode_cache_miss_stores_result(self, tmp_path: Path):
        db = _db(tmp_path)

        def fake_fetcher(lat: float, lon: float) -> PlaceData | None:
            return _place(city="Cambridge", state="Massachusetts",
                          country="United States", country_code="us")

        result = reverse_geocode(42.374, -71.106, db, fetcher=fake_fetcher)
        assert result.cache_hit is False
        assert result.place is not None
        assert result.place.city == "Cambridge"
        # Verify it was stored in cache
        cached = db.get_nominatim_cache(42.374, -71.106)
        assert cached is not None
        assert cached["place_city"] == "Cambridge"

    def test_reverse_geocode_cache_miss_null_result(self, tmp_path: Path):
        # Fetcher returns PlaceData(all None) — should be cached (prevents future retries)
        db = _db(tmp_path)

        def fake_fetcher(lat: float, lon: float) -> PlaceData | None:
            return _place()  # all None

        result = reverse_geocode(0.0, 0.0, db, fetcher=fake_fetcher)
        assert result.cache_hit is False
        assert result.place is not None
        assert result.place.city is None
        # Must be stored in cache so future calls skip the API
        cached = db.get_nominatim_cache(0.0, 0.0)
        assert cached is not None  # row exists (cache hit next time)

    def test_reverse_geocode_rounds_coordinates(self, tmp_path: Path):
        # Two coordinates within ~111 m should share the same cache entry
        db = _db(tmp_path)
        fetcher_calls = []

        def fake_fetcher(lat: float, lon: float) -> PlaceData | None:
            fetcher_calls.append((lat, lon))
            return _place(city="Somerville")

        result1 = reverse_geocode(42.3614, -71.0572, db, fetcher=fake_fetcher)
        result2 = reverse_geocode(42.3619, -71.0578, db, fetcher=fake_fetcher)
        assert len(fetcher_calls) == 1  # second call hits cache
        assert result2.cache_hit is True
        assert result2.place is not None
        assert result2.place.city == "Somerville"

    def test_reverse_geocode_network_error(self, tmp_path: Path):
        # Fetcher returns None (network/HTTP error) — not cached
        db = _db(tmp_path)

        def fake_fetcher(lat: float, lon: float) -> PlaceData | None:
            return None  # simulate network error

        result = reverse_geocode(42.0, -71.0, db, fetcher=fake_fetcher)
        assert result.cache_hit is False
        assert result.place is None
        # Must NOT be stored in cache — next scan should retry
        cached = db.get_nominatim_cache(42.0, -71.0)
        assert cached is None
```

- [ ] **Step 2: Run cache logic tests to confirm they fail**

```bash
python -m pytest tests/test_geocoder.py::TestReverseGeocode -v
```

Expected: `ImportError` — `reverse_geocode` not yet defined in `geocoder.py`.

- [ ] **Step 3: Add fetch_from_nominatim and reverse_geocode to poller/geocoder.py**

Append to the end of `poller/geocoder.py`:

```python

def fetch_from_nominatim(lat: float, lon: float) -> PlaceData | None:
    """Make a live HTTP GET to Nominatim and return parsed PlaceData, or None on error.

    Returns None on network error or 4xx/5xx response (not cached; logged at WARNING).
    Returns PlaceData (possibly all-None fields) on a 200 response — including when
    Nominatim returns no address data. A PlaceData return IS cached.

    Rate-limited to 1 request/second per Nominatim usage policy.
    """
    import requests  # deferred import — not needed if geocoder isn't used

    global _last_call_time
    elapsed = time.monotonic() - _last_call_time
    if elapsed < 1.0:
        time.sleep(1.0 - elapsed)

    try:
        resp = requests.get(
            _NOMINATIM_URL,
            params={"lat": lat, "lon": lon, "zoom": 14, "addressdetails": 1, "format": "json"},
            headers={"User-Agent": _USER_AGENT},
            timeout=10,
        )
        _last_call_time = time.monotonic()
        if resp.status_code != 200:
            log.warning("Nominatim returned HTTP %s for (%.6f, %.6f)", resp.status_code, lat, lon)
            return None
        data = resp.json()
        return _parse_nominatim_response(data)
    except Exception as exc:
        _last_call_time = time.monotonic()
        log.warning("Nominatim request failed for (%.6f, %.6f): %s", lat, lon, exc)
        return None


def reverse_geocode(
    lat: float,
    lon: float,
    db: Any,
    fetcher: Callable[[float, float], PlaceData | None] = fetch_from_nominatim,
) -> LookupResult:
    """Cache-first reverse geocode for (lat, lon).

    1. Round lat/lon to 3 decimal places.
    2. Check nominatim_cache via db.get_nominatim_cache(lat_r, lon_r).
    3. Cache hit  → return LookupResult(place=PlaceData(...), cache_hit=True).
       (place may have all-None fields if coordinates are known to return nothing)
    4. Cache miss → call fetcher(lat, lon).
       - fetcher returns None (error) → return LookupResult(place=None, cache_hit=False)
         without caching; next scan will retry.
       - fetcher returns PlaceData → store in cache, return LookupResult(place=result,
         cache_hit=False).

    The db argument accepts any object with get_nominatim_cache and set_nominatim_cache
    methods (duck-typed for testability).
    """
    lat_r = round(lat, 3)
    lon_r = round(lon, 3)

    cached = db.get_nominatim_cache(lat_r, lon_r)
    if cached is not None:
        # Cache hit — convert raw dict back to PlaceData
        place = PlaceData(
            city=cached.get("place_city"),
            state=cached.get("place_state"),
            country=cached.get("place_country"),
            country_code=cached.get("place_country_code"),
            neighborhood=cached.get("place_neighborhood"),
            address=cached.get("place_address"),
        )
        return LookupResult(place=place, cache_hit=True)

    # Cache miss — call the fetcher
    result = fetcher(lat, lon)
    if result is None:
        # Network/HTTP error — do not cache; allow retry on next scan
        return LookupResult(place=None, cache_hit=False)

    # Store result (including all-None PlaceData, which suppresses future retries)
    db.set_nominatim_cache(lat_r, lon_r, {
        "place_city":         result.city,
        "place_state":        result.state,
        "place_country":      result.country,
        "place_country_code": result.country_code,
        "place_neighborhood": result.neighborhood,
        "place_address":      result.address,
    })
    return LookupResult(place=result, cache_hit=False)
```

- [ ] **Step 4: Run cache logic tests to confirm they pass**

```bash
python -m pytest tests/test_geocoder.py::TestReverseGeocode -v
```

Expected: 6 tests PASS.

- [ ] **Step 5: Run all geocoder tests so far**

```bash
python -m pytest tests/test_geocoder.py -v
```

Expected: 11 tests PASS (5 parse + 6 cache).

- [ ] **Step 6: Run full suite**

```bash
python -m pytest tests/ -q
```

Expected: all existing tests pass.

- [ ] **Step 7: Commit**

```bash
git add poller/geocoder.py tests/test_geocoder.py
git commit -m "feat(#217): geocoder.py — fetch_from_nominatim, reverse_geocode with cache logic"
```

---

## Task 5: Scanner integration

**Files:**
- Modify: `poller/scanner.py` — add `db` param to `build_enriched_row`; call geocoder
- Modify: `tests/test_geocoder.py` — add scanner integration tests

The `db` parameter is optional (default `None`) so all existing `test_core.py` tests calling `build_enriched_row(photo_row, existing, zones, name)` continue to pass without changes.

- [ ] **Step 1: Add scanner integration tests to tests/test_geocoder.py**

Append to `tests/test_geocoder.py` after `TestReverseGeocode`:

```python

# ---------------------------------------------------------------------------
# Scanner integration
# ---------------------------------------------------------------------------


class TestScannerIntegration:
    """Test that build_enriched_row calls reverse_geocode correctly."""

    EXISTING: dict = {
        "uuid": "test-uuid",
        "flickr_id": "12345",
        "privacy_state": "candidate_public",
        "privacy_reason": "",
        "proposed_tags": [],
        "place_city": None,
        "place_state": None,
        "place_country": None,
        "place_country_code": None,
        "place_neighborhood": None,
        "place_address": None,
        "place_ishome": 0,
        "apple_persons": [],
        "apple_named_faces": 0,
        "apple_unknown_faces": 0,
        "apple_labels": [],
        "apple_human_count": 0,
        "apple_ai_caption": "",
        "apple_ai_caption_conf": 0.0,
        "geofenced": 0,
    }

    def _photo_row_with_coords(
        self,
        lat: float,
        lon: float,
        place_city: str | None = None,
    ) -> dict:
        return {
            "uuid": "test-uuid",
            "latitude": lat,
            "longitude": lon,
            "place_city": place_city,
            "place_state": None,
            "place_country": None,
            "place_country_code": None,
            "place_neighborhood": None,
            "place_address": None,
            "place_ishome": 0,
            "apple_persons": [],
            "apple_named_faces": 0,
            "apple_unknown_faces": 0,
            "apple_labels": [],
            "apple_human_count": 0,
            "apple_ai_caption": "",
            "apple_ai_caption_conf": 0.0,
            "date_analyzed": None,
            "meta_synced_photos_at": None,
            "photos_tags_hash": None,
            "photos_title": None,
            "photos_description": None,
            "photos_tags": [],
            "_is_screenshot": False,
            "_is_selfie": False,
            "_is_live": False,
            "is_video": 0,
        }

    def test_scanner_fills_place_from_geocoder(self, tmp_path: Path):
        from geocoder import LookupResult, PlaceData
        from poller.scanner import build_enriched_row

        db = _db(tmp_path)
        db.set_nominatim_cache(42.361, -71.057, {
            "place_city": "Somerville",
            "place_state": "Massachusetts",
            "place_country": "United States",
            "place_country_code": "us",
            "place_neighborhood": "Winter Hill",
            "place_address": "Somerville, MA, US",
        })

        photo_row = self._photo_row_with_coords(42.3614, -71.0572)
        result = build_enriched_row(photo_row, self.EXISTING, [], "Chris Devers", db=db)
        assert result["place_city"] == "Somerville"
        assert result["place_state"] == "Massachusetts"

    def test_scanner_skips_geocoder_when_all_place_set(self, tmp_path: Path):
        from poller.scanner import build_enriched_row

        db = _db(tmp_path)
        # Photo row already has all four key place fields populated
        photo_row = self._photo_row_with_coords(42.361, -71.057)
        photo_row["place_city"] = "Somerville"
        photo_row["place_state"] = "Massachusetts"
        photo_row["place_country"] = "United States"
        photo_row["place_neighborhood"] = "Winter Hill"

        # No entry in nominatim_cache — if geocoder is called, it would find nothing
        result = build_enriched_row(photo_row, self.EXISTING, [], "Chris Devers", db=db)
        # place_city should still be Somerville (from photo_row), not overwritten
        assert result["place_city"] == "Somerville"
        # Verify cache was NOT written (geocoder skipped)
        cached = db.get_nominatim_cache(42.361, -71.057)
        assert cached is None

    def test_scanner_zero_zero_coordinates_not_skipped(self, tmp_path: Path):
        # (lat=0.0, lon=0.0) is a valid coordinate pair (null island).
        # Neither value should be treated as falsy — geocoder must be called.
        from poller.scanner import build_enriched_row

        db = _db(tmp_path)
        db.set_nominatim_cache(0.0, 0.0, {
            "place_city": "Gulf of Guinea",
            "place_state": None, "place_country": None,
            "place_country_code": None, "place_neighborhood": None, "place_address": None,
        })

        photo_row = self._photo_row_with_coords(0.0, 0.0)
        result = build_enriched_row(photo_row, self.EXISTING, [], "Chris Devers", db=db)
        assert result["place_city"] == "Gulf of Guinea"
```

- [ ] **Step 2: Run scanner tests to confirm they fail**

```bash
python -m pytest tests/test_geocoder.py::TestScannerIntegration -v
```

Expected: `TypeError` or `AssertionError` — `build_enriched_row` doesn't yet accept `db`.

- [ ] **Step 3: Modify build_enriched_row in poller/scanner.py**

Find the `build_enriched_row` function signature (line ~463) and change it to:

```python
def build_enriched_row(
    photo_row: dict,
    existing: dict,
    zones: list[dict],
    self_name: str,
    person_policies: dict[str, str] | None = None,
    db: "Database | None" = None,
) -> dict:
```

Then, after the `# Place fields` block (around line 522, where place fields are copied from `photo_row`), add the geocoder call. The location is after this block:

```python
    for field in (
        "place_city",
        "place_state",
        "place_country",
        "place_country_code",
        "place_address",
        "place_neighborhood",
        "place_ishome",
    ):
        if photo_row.get(field) is not None:
            merged[field] = photo_row[field]
```

Add immediately after that block (before the `# Screenshot / selfie` block):

```python
    # Geocoder fill-in: use Nominatim to fill any missing place fields from GPS coordinates
    _PLACE_FIELDS = ("place_city", "place_state", "place_country", "place_neighborhood")
    if (
        db is not None
        and merged.get("latitude") is not None
        and merged.get("longitude") is not None
        and any(merged.get(f) is None for f in _PLACE_FIELDS)
    ):
        from geocoder import reverse_geocode  # deferred import — poller path
        result = reverse_geocode(merged["latitude"], merged["longitude"], db)
        if result.place:
            merged["place_city"]         = merged.get("place_city")         or result.place.city
            merged["place_state"]        = merged.get("place_state")        or result.place.state
            merged["place_country"]      = merged.get("place_country")      or result.place.country
            merged["place_country_code"] = merged.get("place_country_code") or result.place.country_code
            merged["place_neighborhood"] = merged.get("place_neighborhood") or result.place.neighborhood
            merged["place_address"]      = merged.get("place_address")      or result.place.address
```

Also update the two call sites in `scan()` (lines ~663 and ~679) to pass `db`:

Line ~663:
```python
            enriched_row = build_enriched_row(
                photo_row, existing_by_uuid, zones, self_name,
                person_policies=person_policies, db=db
            )
```

Line ~679:
```python
            enriched_row = build_enriched_row(
                photo_row, primary, zones, self_name,
                person_policies=person_policies, db=db
            )
```

There is a third call site further down (for Photos-only records with no Flickr match) — search for all occurrences of `build_enriched_row(` in `scanner.py` and update all of them to pass `db=db`.

- [ ] **Step 4: Run scanner integration tests to confirm they pass**

```bash
python -m pytest tests/test_geocoder.py::TestScannerIntegration -v
```

Expected: 3 tests PASS.

- [ ] **Step 5: Run full test suite**

```bash
python -m pytest tests/ -q
```

Expected: all existing tests pass (existing `build_enriched_row` calls without `db` default to `None` and skip geocoding).

- [ ] **Step 6: Commit**

```bash
git add poller/scanner.py tests/test_geocoder.py
git commit -m "feat(#217): wire reverse_geocode into scanner.py build_enriched_row"
```

---

## Task 6: bp geocode CLI command

**Files:**
- Modify: `bp` — add `cmd_geocode` function, subparser, dispatch entry
- Modify: `tests/test_geocoder.py` — add CLI tests

- [ ] **Step 1: Add CLI tests to tests/test_geocoder.py**

Append to `tests/test_geocoder.py` after `TestScannerIntegration`:

```python

# ---------------------------------------------------------------------------
# bp geocode command
# ---------------------------------------------------------------------------


class TestBpGeocode:
    """Tests for the cmd_geocode function (extracted run_geocode for testability)."""

    def _make_db_with_photo(
        self,
        tmp_path: Path,
        *,
        lat: float = 42.361,
        lon: float = -71.057,
        place_city: str | None = None,
        place_state: str | None = None,
        place_country: str | None = None,
        place_neighborhood: str | None = None,
    ) -> tuple[Database, int]:
        """Insert a test photo with the given place fields; return (db, photo_id)."""
        db = _db(tmp_path)
        row_id = db.upsert_photo({
            "uuid": "test-geocode-uuid",
            "flickr_id": None,
            "latitude": lat,
            "longitude": lon,
            "place_city": place_city,
            "place_state": place_state,
            "place_country": place_country,
            "place_neighborhood": place_neighborhood,
            "privacy_state": "candidate_public",
            "privacy_reason": "",
            "proposed_tags": [],
        })
        return db, row_id

    def test_bp_geocode_fills_gaps(self, tmp_path: Path):
        from run_geocode import run_geocode

        db, photo_id = self._make_db_with_photo(tmp_path)

        def fake_fetcher(lat: float, lon: float) -> PlaceData | None:
            return _place(city="Somerville", state="Massachusetts",
                          country="United States", country_code="us")

        counts = run_geocode(db, dry_run=False, overwrite=False, limit=None, fetcher=fake_fetcher)
        assert counts["geocoded"] == 1
        row = db.get_photo(photo_id)
        assert row["place_city"] == "Somerville"

    def test_bp_geocode_skips_existing(self, tmp_path: Path):
        from run_geocode import run_geocode

        db, photo_id = self._make_db_with_photo(
            tmp_path,
            place_city="Cambridge",
            place_state="Massachusetts",
            place_country="United States",
            place_neighborhood="Harvard Square",
        )

        fetcher_calls = []

        def fake_fetcher(lat: float, lon: float) -> PlaceData | None:
            fetcher_calls.append((lat, lon))
            return _place(city="Somerville")

        counts = run_geocode(db, dry_run=False, overwrite=False, limit=None, fetcher=fake_fetcher)
        assert counts["skipped"] == 1
        assert counts["geocoded"] == 0
        assert fetcher_calls == []  # no API call needed
        row = db.get_photo(photo_id)
        assert row["place_city"] == "Cambridge"  # unchanged

    def test_bp_geocode_overwrite_flag(self, tmp_path: Path):
        from run_geocode import run_geocode

        db, photo_id = self._make_db_with_photo(
            tmp_path,
            place_city="Old City",
            place_state="Old State",
            place_country="Old Country",
            place_neighborhood="Old Neighborhood",
        )

        def fake_fetcher(lat: float, lon: float) -> PlaceData | None:
            return _place(city="New City", state="New State",
                          country="New Country", country_code="nc")

        counts = run_geocode(db, dry_run=False, overwrite=True, limit=None, fetcher=fake_fetcher)
        assert counts["geocoded"] >= 1
        row = db.get_photo(photo_id)
        assert row["place_city"] == "New City"

    def test_bp_geocode_dry_run(self, tmp_path: Path):
        from run_geocode import run_geocode

        db, photo_id = self._make_db_with_photo(tmp_path)

        def fake_fetcher(lat: float, lon: float) -> PlaceData | None:
            return _place(city="Somerville")

        counts = run_geocode(db, dry_run=True, overwrite=False, limit=None, fetcher=fake_fetcher)
        assert counts["geocoded"] == 1  # counted
        row = db.get_photo(photo_id)
        assert row["place_city"] is None  # DB unchanged

    def test_bp_geocode_limit(self, tmp_path: Path):
        from run_geocode import run_geocode

        # Insert two photos with missing place data
        db = _db(tmp_path)
        db.upsert_photo({
            "uuid": "uuid-a", "flickr_id": None, "latitude": 42.0, "longitude": -71.0,
            "place_city": None, "place_state": None, "place_country": None,
            "place_neighborhood": None, "privacy_state": "candidate_public",
            "privacy_reason": "", "proposed_tags": [],
        })
        db.upsert_photo({
            "uuid": "uuid-b", "flickr_id": None, "latitude": 43.0, "longitude": -72.0,
            "place_city": None, "place_state": None, "place_country": None,
            "place_neighborhood": None, "privacy_state": "candidate_public",
            "privacy_reason": "", "proposed_tags": [],
        })

        fetcher_calls = []

        def fake_fetcher(lat: float, lon: float) -> PlaceData | None:
            fetcher_calls.append((lat, lon))
            return _place(city="Somewhere")

        counts = run_geocode(db, dry_run=False, overwrite=False, limit=1, fetcher=fake_fetcher)
        assert len(fetcher_calls) == 1  # stopped after limit

    def test_bp_geocode_limit_counts_failed_calls(self, tmp_path: Path):
        # Network errors count toward --limit to prevent spinning on persistent failures
        from run_geocode import run_geocode

        db = _db(tmp_path)
        for i in range(3):
            db.upsert_photo({
                "uuid": f"uuid-{i}", "flickr_id": None,
                "latitude": float(40 + i), "longitude": -71.0,
                "place_city": None, "place_state": None, "place_country": None,
                "place_neighborhood": None, "privacy_state": "candidate_public",
                "privacy_reason": "", "proposed_tags": [],
            })

        fetcher_calls = []

        def fake_fetcher(lat: float, lon: float) -> PlaceData | None:
            fetcher_calls.append((lat, lon))
            return None  # persistent network error

        counts = run_geocode(db, dry_run=False, overwrite=False, limit=2, fetcher=fake_fetcher)
        assert len(fetcher_calls) == 2  # limited even with errors
```

- [ ] **Step 2: Run CLI tests to confirm they fail**

```bash
python -m pytest tests/test_geocoder.py::TestBpGeocode -v
```

Expected: `ModuleNotFoundError` — `poller.run_geocode` does not exist.

- [ ] **Step 3: Create poller/run_geocode.py**

The `run_geocode` function is extracted into its own module (analogous to how `run_import` lives in `contacts_importer.py`) so it can be tested without invoking the CLI arg parser.

```python
"""bp geocode — retroactive place data backfill via Nominatim (#217).

Provides run_geocode(), which is injectable for testing and called by cmd_geocode
in the bp CLI script.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

log = logging.getLogger("blue-pearmain.geocoder")


def run_geocode(
    db: Any,
    *,
    dry_run: bool,
    overwrite: bool,
    limit: int | None,
    fetcher: "Callable[[float, float], Any] | None" = None,
) -> dict[str, int]:
    """Backfill place data for photos that have GPS coordinates but missing place fields.

    Returns a counts dict:
      geocoded  — API calls (not cache_hit) that returned a result and were/would be written
      cached    — lookups satisfied from nominatim_cache (cache_hit=True)
      no_result — Nominatim returned no address data (whether from cache or fresh call)
      skipped   — already had complete place data and --overwrite not passed
      errors    — API calls that returned None (network/HTTP errors)

    --limit N counts API call *attempts*. Cache hits do not count. Failed calls DO
    count (prevents spinning on persistent errors).
    """
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent))
    from geocoder import PlaceData, reverse_geocode as _reverse_geocode, fetch_from_nominatim

    if fetcher is None:
        fetcher = fetch_from_nominatim

    _PLACE_FIELDS = ("place_city", "place_state", "place_country", "place_neighborhood")

    if overwrite:
        query = (
            "SELECT id, latitude, longitude, place_city, place_state, place_country, "
            "place_neighborhood FROM photos WHERE latitude IS NOT NULL"
        )
        params: tuple = ()
    else:
        query = (
            "SELECT id, latitude, longitude, place_city, place_state, place_country, "
            "place_neighborhood FROM photos WHERE latitude IS NOT NULL "
            "AND (place_city IS NULL OR place_state IS NULL "
            "OR place_country IS NULL OR place_neighborhood IS NULL)"
        )
        params = ()

    rows = db.conn.execute(query, params).fetchall()

    counts = {"geocoded": 0, "cached": 0, "no_result": 0, "skipped": 0, "errors": 0}
    api_calls = 0

    for row in rows:
        photo_id = row["id"]
        lat = row["latitude"]
        lon = row["longitude"]

        # Check if already complete (handles overwrite=False case where query may still
        # return rows with some fields set, relying on COALESCE in update_place_data)
        if not overwrite and all(row[f] is not None for f in _PLACE_FIELDS):
            counts["skipped"] += 1
            continue

        if limit is not None and api_calls >= limit:
            break

        result = _reverse_geocode(lat, lon, db, fetcher=fetcher)

        if result.cache_hit:
            if result.place and any(
                getattr(result.place, f.replace("place_", "")) for f in _PLACE_FIELDS
            ):
                if not dry_run:
                    db.update_place_data(photo_id, {
                        "place_city":         result.place.city,
                        "place_state":        result.place.state,
                        "place_country":      result.place.country,
                        "place_country_code": result.place.country_code,
                        "place_neighborhood": result.place.neighborhood,
                        "place_address":      result.place.address,
                    }, overwrite=overwrite)
                counts["cached"] += 1
            else:
                counts["no_result"] += 1
        else:
            api_calls += 1
            if result.place is None:
                counts["errors"] += 1
            elif any(
                getattr(result.place, f.replace("place_", ""), None)
                for f in _PLACE_FIELDS
            ):
                if not dry_run:
                    db.update_place_data(photo_id, {
                        "place_city":         result.place.city,
                        "place_state":        result.place.state,
                        "place_country":      result.place.country,
                        "place_country_code": result.place.country_code,
                        "place_neighborhood": result.place.neighborhood,
                        "place_address":      result.place.address,
                    }, overwrite=overwrite)
                counts["geocoded"] += 1
            else:
                counts["no_result"] += 1

    return counts
```

**Note on `_PLACE_FIELDS` field name stripping:** The `run_geocode` function checks `PlaceData` attributes (e.g. `place.city`) while the query rows use column names (e.g. `place_city`). The expression `f.replace("place_", "")` converts `"place_city"` → `"city"` to match `PlaceData` field names.

- [ ] **Step 4: Run CLI tests to confirm they pass**

```bash
python -m pytest tests/test_geocoder.py::TestBpGeocode -v
```

Expected: 6 tests PASS.

- [ ] **Step 5: Add cmd_geocode and CLI wiring to bp**

Find `cmd_import_contacts_birthdays` in `bp` (around line 993). Add a new function before it:

```python
def cmd_geocode(args: argparse.Namespace) -> None:
    """Backfill place data from Nominatim for photos with GPS coordinates."""
    import yaml

    sys.path.insert(0, str(ROOT / "poller"))
    from db.db import Database
    from run_geocode import run_geocode

    with open(args.config) as f:
        config = yaml.safe_load(f)

    db_path = str(Path(config["database"]["path"]).expanduser())
    db = Database(db_path)
    try:
        counts = run_geocode(
            db,
            dry_run=args.dry_run,
            overwrite=args.overwrite,
            limit=args.limit,
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
```

Find the subparser block (around line 1417, near `p_icb = sub.add_parser`). Add before the `p_icb` block:

```python
    p_geo = sub.add_parser(
        "geocode",
        help="Backfill place data from Nominatim for photos that have GPS coordinates",
    )
    p_geo.add_argument(
        "--dry-run",
        action="store_true",
        help="Report counts without writing anything",
    )
    p_geo.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace existing place data with Nominatim results (default: fill gaps only)",
    )
    p_geo.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Stop after N API call attempts (cache hits do not count; errors do count)",
    )
```

Find the `dispatch = {` dict (around line 1467). Add before the closing `}`:

```python
        "geocode":               cmd_geocode,
```

- [ ] **Step 6: Run all geocoder tests**

```bash
python -m pytest tests/test_geocoder.py -v
```

Expected: all 17 tests PASS (5 parse + 6 cache + 3 scanner + 6 CLI).

Wait — the spec lists 18 tests. Check the count: `test_parse_nominatim_response_village_fallback` is an extra test not in the original spec list (added above). The spec has 18; count actual tests in the file and reconcile. The exact number doesn't matter as long as all spec scenarios are covered.

- [ ] **Step 7: Run full suite**

```bash
python -m pytest tests/ -q
```

Expected: all tests pass.

- [ ] **Step 8: Commit**

```bash
git add poller/run_geocode.py bp tests/test_geocoder.py
git commit -m "feat(#217): add bp geocode command and run_geocode module"
```

---

## Task 7: Lint, final checks, and closing commit

**Files:**
- No new code — verification only.

- [ ] **Step 1: Run mypy (make lint)**

```bash
cd /Users/cdevers/Documents/GitHub/Blue\ Pearmain && make lint
```

Fix any type errors before proceeding. Common issues to watch for:
- `reverse_geocode`'s `db: Any` parameter — annotate more precisely if mypy complains
- `_last_call_time: float` module variable accessed via `global` — verify no mypy complaint
- `dict[str, Any]` return type on `get_nominatim_cache` — ensure `Any` is imported in `db/db.py`

- [ ] **Step 2: Run full test suite one final time**

```bash
python -m pytest tests/ -q
```

Expected: all tests pass with no warnings.

- [ ] **Step 3: Verify migration is registered**

```bash
python -m pytest tests/test_migrate_030.py -v
```

Expected: all 5 migration tests PASS.

- [ ] **Step 4: Smoke-test the CLI help**

```bash
python bp geocode --help
```

Expected output includes `--dry-run`, `--overwrite`, `--limit N`.

- [ ] **Step 5: Update MEMORY.md and issue**

Update the memory file to reflect #217 as done. Close or comment on GH #217.

- [ ] **Step 6: Final commit referencing #217**

```bash
git add -p  # stage any lint fixes
git commit -m "chore(#217): type cleanup and lint pass for Nominatim geocoder

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Implementation checklist (from spec)

- [ ] `nominatim_cache` DDL in `db/schema.sql`
- [ ] Migration 030 (`db/migrations/migrate_030_nominatim_cache.py`) with `schema_migrations` entry
- [ ] `poller/geocoder.py`: `PlaceData`, `LookupResult`, `_parse_nominatim_response`, `fetch_from_nominatim`, `reverse_geocode`
- [ ] `poller/run_geocode.py`: `run_geocode()`
- [ ] `db/db.py`: `get_nominatim_cache`, `set_nominatim_cache`, `update_place_data`
- [ ] `poller/scanner.py`: optional `db` param on `build_enriched_row`; geocoder call
- [ ] `bp`: `cmd_geocode`, subparser, dispatch entry
- [ ] `tests/test_migrate_030.py`: 5 migration tests
- [ ] `tests/test_geocoder.py`: 18+ tests (all injectable, no real HTTP)
- [ ] `make lint` — mypy clean
- [ ] `python -m pytest tests/ -q` — all pass
- [ ] Commit referencing `#217`
