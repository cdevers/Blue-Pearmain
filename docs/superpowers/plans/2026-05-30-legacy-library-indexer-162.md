# Legacy Library Indexer Implementation Plan (GH #162, target 1.4.0)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Index an old (migrated iPhoto/Photos 4) library's per-asset metadata + copied thumbnails into new `legacy_*` tables in `curator.db`, expose `bp index-legacy` to build/refresh it from a runtime-supplied path, and add a non-destructive `bp match-legacy-preview` report linking legacy assets to Flickr-only `candidate_public` photos.

**Architecture:** A new migration (`migrate_026`) adds two tables (`legacy_libraries`, `legacy_assets`). A new `poller/legacy_indexer.py` opens the library via `osxphotos.PhotosDB`, builds one normalized row per asset, copies an existing thumbnail derivative into BP's thumb cache (keyed by a path-independent hash), and upserts. Full runs are an *authoritative mirror* (reconcile/delete unseen rows + GC orphaned thumbnails) gated on successful completion; `--limit` runs are non-authoritative. A `poller/legacy_cache.py` mirrors the slow-over-AFP library DB to a local cache under `data/legacy-cache/<library_uuid>/`, validated by mtime+size+head-hash recorded in `legacy_libraries`. `bp match-legacy-preview` joins the index to Flickr-only photos via UTC-second timestamp + dimension matching (title as tiebreaker), emitting a deterministically-ordered tiered report + optional CSV, writing nothing to `photos`.

**Tech Stack:** Python 3, SQLite (stdlib `sqlite3`), `osxphotos` 0.75.6, `pytest`, `mypy` (via `make lint`). Reuses `poller/deduplicator.py:_normalise_to_utc_second`, `poller/thumbnailer.py:derivative_path`, `poller/bp_logging.py`.

**Spec:** `docs/superpowers/specs/2026-05-30-legacy-library-indexer-162-design.md`

**Conventions to follow (verified in codebase):**
- Migrations expose `MIGRATION_NAME`, `run_on_conn(conn)`, `run(db_path, dry_run)`, `main()`; idempotency via `schema_migrations`. Next free number is **026** (`db/migrations/` ends at `migrate_025_person_birthdays.py`).
- Migration tests build an in-memory DB and call `run_on_conn` (see `tests/test_migrate_024.py`).
- `db/db.py`: `class Database`, per-thread `self.conn`, `_now_iso()`, `_json_loads_safe()`, `import json` already present. Methods use `self.conn.execute(...)` + `self.conn.commit()`.
- `bp` CLI: register with `sub.add_parser(...)`, add a default-guard in the `if not hasattr(args, ...)` block, add a `cmd_*` function, and wire it into the `dispatch` dict. Config is loaded per-command via `yaml.safe_load(open(args.config))`; `ROOT` is the repo root path constant. Paths: `config["database"]["path"]`, `config["thumbnails"]["path"]`.
- **Never** read, print, or commit `config/config.yml` contents (it holds Flickr secrets).

---

## File Structure

- **Create** `db/migrations/migrate_026_legacy_index.py` — the two tables + indexes.
- **Create** `tests/test_migrate_026.py` — migration tests.
- **Modify** `db/db.py` — add legacy methods (upsert, count, iter, library get/set, reconcile-delete, thumbnail-key listing).
- **Create** `tests/test_db_legacy.py` — DB-method tests.
- **Create** `poller/legacy_normalize.py` — pure helpers (path canonicalization, JSON list normalization, thumbnail cache key + path, head-hash). No I/O beyond reading bytes for the hash.
- **Create** `tests/test_legacy_normalize.py` — helper tests.
- **Create** `poller/legacy_cache.py` — source-DB location, cache validity check, atomic cache build/refresh.
- **Create** `tests/test_legacy_cache.py` — cache tests.
- **Create** `poller/legacy_indexer.py` — `index_library(...)`, osxphotos adapter, thumbnail copy, authoritative reconcile + GC.
- **Create** `tests/test_legacy_indexer.py` — indexer tests with a mock PhotosDB.
- **Create** `poller/legacy_match.py` — tier computation + deterministic ordering (pure, no I/O on the photos table beyond the query the caller passes in).
- **Create** `tests/test_legacy_match.py` — match-tier tests.
- **Modify** `bp` — add `index-legacy` and `match-legacy-preview` subcommands + `cmd_index_legacy` / `cmd_match_legacy_preview`.
- **Create** `tests/test_cli_legacy.py` — CLI smoke tests (arg parsing + dispatch).
- **Modify** `README.md`, **modify** the spec status line, **add** `has-plan` label to #162.

---

## Task 1: Migration 026 — `legacy_libraries` + `legacy_assets`

**Files:**
- Create: `db/migrations/migrate_026_legacy_index.py`
- Test: `tests/test_migrate_026.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_migrate_026.py
"""Migration 026 — legacy_libraries + legacy_assets tables (#162)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


def _fresh_db_up_to_025() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE schema_migrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            applied_at TEXT NOT NULL
        );
    """)
    return conn


def _run_migration(conn: sqlite3.Connection) -> None:
    import sys

    sys.path.insert(0, str(Path(__file__).parent.parent))
    from db.migrations.migrate_026_legacy_index import run_on_conn

    run_on_conn(conn)


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


class TestMigrate026:
    def test_legacy_libraries_table_created(self):
        conn = _fresh_db_up_to_025()
        _run_migration(conn)
        assert "legacy_libraries" in _tables(conn)

    def test_legacy_assets_table_created(self):
        conn = _fresh_db_up_to_025()
        _run_migration(conn)
        assert "legacy_assets" in _tables(conn)

    def test_legacy_libraries_columns(self):
        conn = _fresh_db_up_to_025()
        _run_migration(conn)
        assert _cols(conn, "legacy_libraries") >= {
            "library_uuid", "display_name", "source_path_last_seen", "schema_version",
            "db_mtime", "db_size", "db_head_hash", "asset_count", "indexed_at",
        }

    def test_legacy_assets_columns(self):
        conn = _fresh_db_up_to_025()
        _run_migration(conn)
        assert _cols(conn, "legacy_assets") >= {
            "id", "library_uuid", "asset_uuid", "original_filename", "fingerprint",
            "date_taken", "width", "height", "latitude", "longitude", "title",
            "description", "keywords", "labels", "persons", "named_face_count",
            "unknown_face_count", "master_rel_path", "thumbnail_cache_key",
            "thumbnail_status", "indexed_at",
        }

    def test_legacy_assets_unique_identity(self):
        conn = _fresh_db_up_to_025()
        _run_migration(conn)
        conn.execute(
            "INSERT INTO legacy_assets (library_uuid, asset_uuid) VALUES ('L', 'A')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO legacy_assets (library_uuid, asset_uuid) VALUES ('L', 'A')"
            )

    def test_indexes_present(self):
        conn = _fresh_db_up_to_025()
        _run_migration(conn)
        idx = {r[1] for r in conn.execute("PRAGMA index_list(legacy_assets)").fetchall()}
        assert "idx_legacy_assets_date" in idx
        assert "idx_legacy_assets_dims" in idx

    def test_idempotent_second_run(self):
        conn = _fresh_db_up_to_025()
        _run_migration(conn)
        _run_migration(conn)
        rows = conn.execute(
            "SELECT name FROM schema_migrations WHERE name='migrate_026_legacy_index'"
        ).fetchall()
        assert len(rows) == 1

    def test_schema_migrations_entry_added(self):
        conn = _fresh_db_up_to_025()
        _run_migration(conn)
        row = conn.execute(
            "SELECT name FROM schema_migrations WHERE name='migrate_026_legacy_index'"
        ).fetchone()
        assert row is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_migrate_026.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'db.migrations.migrate_026_legacy_index'`.

- [ ] **Step 3: Write the migration**

```python
# db/migrations/migrate_026_legacy_index.py
"""
migrate_026_legacy_index.py

Adds two tables for the legacy (migrated iPhoto/Photos 4) library indexer (#162):

  legacy_libraries  one row per indexed source library; holds path-independent
                    identity (library_uuid) plus cache-validity metadata
                    (db_mtime, db_size, db_head_hash) used to decide whether the
                    local DB cache can be reused.

  legacy_assets     one row per old-library asset, keyed by the path-independent
                    identity (library_uuid, asset_uuid). Mirrors the existing
                    apple_persons JSON-array convention rather than normalized
                    person tables.

Safe to run multiple times (idempotent via schema_migrations).

Usage:
    python db/migrations/migrate_026_legacy_index.py --config config/config.yml
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import yaml

MIGRATION_NAME = "migrate_026_legacy_index"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_on_conn(conn: sqlite3.Connection) -> None:
    if conn.row_factory is None:
        conn.row_factory = sqlite3.Row

    try:
        row = conn.execute(
            "SELECT id FROM schema_migrations WHERE name = ?", (MIGRATION_NAME,)
        ).fetchone()
        if row is not None:
            return
    except Exception:
        pass

    conn.execute("BEGIN")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS legacy_libraries (
            library_uuid          TEXT PRIMARY KEY,
            display_name          TEXT,
            source_path_last_seen TEXT,
            schema_version        INTEGER,
            db_mtime              TEXT,
            db_size               INTEGER,
            db_head_hash          TEXT,
            asset_count           INTEGER NOT NULL DEFAULT 0,
            indexed_at            TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS legacy_assets (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            library_uuid        TEXT NOT NULL REFERENCES legacy_libraries(library_uuid) ON DELETE CASCADE,
            asset_uuid          TEXT NOT NULL,
            original_filename   TEXT,
            fingerprint         TEXT,
            date_taken          TEXT,
            width               INTEGER,
            height              INTEGER,
            latitude            REAL,
            longitude           REAL,
            title               TEXT,
            description         TEXT,
            keywords            TEXT,
            labels              TEXT,
            persons             TEXT,
            named_face_count    INTEGER NOT NULL DEFAULT 0,
            unknown_face_count  INTEGER NOT NULL DEFAULT 0,
            master_rel_path     TEXT,
            thumbnail_cache_key TEXT,
            thumbnail_status    TEXT,
            indexed_at          TEXT,
            UNIQUE(library_uuid, asset_uuid)
        )
    """)

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_legacy_assets_date ON legacy_assets(date_taken)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_legacy_assets_dims ON legacy_assets(width, height)"
    )

    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (name, applied_at) VALUES (?, ?)",
        (MIGRATION_NAME, _now_iso()),
    )
    conn.commit()


def run(db_path: str, dry_run: bool = False) -> None:
    if dry_run:
        print("  [dry-run] Would create legacy_libraries and legacy_assets tables")
        return
    conn = sqlite3.connect(db_path)
    run_on_conn(conn)
    conn.close()
    print("  Applied:  migrate_026_legacy_index")


def main() -> int:
    parser = argparse.ArgumentParser(description="Migration 026: legacy library index tables")
    parser.add_argument("--config", default="config/config.yml")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    with open(args.config) as f:
        config = yaml.safe_load(f)
    db_path = str(Path(config["database"]["path"]).expanduser())
    run(db_path, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_migrate_026.py -q`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add db/migrations/migrate_026_legacy_index.py tests/test_migrate_026.py
git commit -m "feat(#162): migration 026 — legacy_libraries + legacy_assets tables"
```

---

## Task 2: Normalization helpers (`poller/legacy_normalize.py`)

Pure functions shared by the indexer and matcher: deterministic JSON-list normalization, path canonicalization, thumbnail cache key/path, and the head-hash. Isolating them keeps the indexer focused and makes the fiddly string rules independently testable.

**Files:**
- Create: `poller/legacy_normalize.py`
- Test: `tests/test_legacy_normalize.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_legacy_normalize.py
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "poller"))

from legacy_normalize import (  # noqa: E402
    canonical_rel_path,
    head_hash,
    normalize_json_list,
    normalize_title,
    thumbnail_cache_key,
    thumbnail_path,
)


class TestNormalizeJsonList:
    def test_unique_and_sorted(self):
        assert json.loads(normalize_json_list(["b", "a", "b"])) == ["a", "b"]

    def test_empty_returns_empty_array(self):
        assert normalize_json_list([]) == "[]"
        assert normalize_json_list(None) == "[]"

    def test_reordered_input_is_identical(self):
        assert normalize_json_list(["x", "y"]) == normalize_json_list(["y", "x"])

    def test_strips_blank_entries(self):
        assert json.loads(normalize_json_list(["a", "", "  ", "b"])) == ["a", "b"]


class TestCanonicalRelPath:
    def test_backslashes_become_posix(self):
        assert canonical_rel_path("Masters\\2008\\img.jpg") == "Masters/2008/img.jpg"

    def test_duplicate_slashes_collapsed_after_separator_norm(self):
        assert canonical_rel_path("a\\\\b//c") == "a/b/c"

    def test_leading_dot_slash_stripped(self):
        assert canonical_rel_path("./a/b.jpg") == "a/b.jpg"

    def test_trailing_slash_stripped(self):
        assert canonical_rel_path("a/b/") == "a/b"

    def test_case_preserved(self):
        assert canonical_rel_path("Masters/IMG.JPG") == "Masters/IMG.JPG"

    def test_nfd_normalized_to_nfc(self):
        import unicodedata

        nfd = unicodedata.normalize("NFD", "café/photo.jpg")
        out = canonical_rel_path(nfd)
        assert out == unicodedata.normalize("NFC", "café/photo.jpg")

    def test_none_returns_none(self):
        assert canonical_rel_path(None) is None


class TestNormalizeTitle:
    def test_trim_casefold_nfc(self):
        assert normalize_title("  Hello ") == "hello"

    def test_empty_after_trim_is_none(self):
        assert normalize_title("   ") is None
        assert normalize_title("") is None
        assert normalize_title(None) is None


class TestThumbnailKeyAndPath:
    def test_key_is_stable_and_path_independent(self):
        k1 = thumbnail_cache_key("LIB", "ASSET")
        k2 = thumbnail_cache_key("LIB", "ASSET")
        assert k1 == k2 and len(k1) == 32

    def test_different_identity_different_key(self):
        assert thumbnail_cache_key("LIB", "A") != thumbnail_cache_key("LIB", "B")

    def test_path_built_from_root_at_read_time(self, tmp_path):
        key = thumbnail_cache_key("LIB", "ASSET")
        p = thumbnail_path(tmp_path, "LIB", key)
        assert p == tmp_path / "legacy" / "LIB" / f"{key}.jpg"


class TestHeadHash:
    def test_hashes_first_n_bytes_only(self, tmp_path):
        f = tmp_path / "db.sqlite"
        f.write_bytes(b"A" * 100 + b"B" * 100)
        h_all = head_hash(str(f))
        h_first = head_hash(str(f), n=100)
        assert h_first != h_all
        # Changing bytes beyond n does not change the n-byte head hash
        f.write_bytes(b"A" * 100 + b"C" * 100)
        assert head_hash(str(f), n=100) == h_first
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_legacy_normalize.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'legacy_normalize'`.

- [ ] **Step 3: Write the helpers**

```python
# poller/legacy_normalize.py
"""Pure normalization helpers for the legacy library indexer (#162).

No external dependencies; safe to import from indexer, matcher, and tests.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path

HEAD_HASH_BYTES = 16 * 1024 * 1024  # 16 MiB of raw file bytes
_DUP_SLASH = re.compile(r"/{2,}")


def normalize_json_list(values) -> str:
    """JSON array, unique + alphabetically sorted, blanks stripped.

    Deterministic: reordered or duplicated input yields identical output, so
    re-indexing never produces a noisy row update.
    """
    if not values:
        return "[]"
    cleaned = sorted({str(v).strip() for v in values if str(v).strip()})
    return json.dumps(cleaned, ensure_ascii=False)


def canonical_rel_path(path: str | None) -> str | None:
    """Canonical bundle-relative path. Fixed transform order (spec review pt 5):
    POSIX separators -> collapse duplicate slashes -> strip leading './' ->
    strip trailing '/' -> preserve case -> NFC.
    """
    if path is None:
        return None
    s = path.replace("\\", "/")          # 1. separators -> POSIX
    s = _DUP_SLASH.sub("/", s)           # 2. collapse dup slashes (after step 1)
    if s.startswith("./"):               # 3. strip leading ./
        s = s[2:]
    if len(s) > 1 and s.endswith("/"):   # 4. strip trailing slash
        s = s[:-1]
    s = unicodedata.normalize("NFC", s)  # 6. NFC (case preserved throughout)
    return s


def normalize_title(title: str | None) -> str | None:
    """Trim, casefold, NFC. Empty-after-trim counts as missing -> None."""
    if title is None:
        return None
    s = unicodedata.normalize("NFC", title).strip()
    if not s:
        return None
    return s.casefold()


def thumbnail_cache_key(library_uuid: str, asset_uuid: str) -> str:
    """Stable, path-independent 32-char hex key of the asset identity."""
    digest = hashlib.sha256(f"{library_uuid}:{asset_uuid}".encode()).hexdigest()
    return digest[:32]


def thumbnail_path(thumb_root: Path | str, library_uuid: str, cache_key: str) -> Path:
    """Absolute thumbnail path, resolved at read time against the cache root."""
    return Path(thumb_root) / "legacy" / library_uuid / f"{cache_key}.jpg"


def head_hash(path: str, n: int = HEAD_HASH_BYTES) -> str:
    """SHA256 of the first n raw file bytes (not SQLite pages)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read(n))
    return h.hexdigest()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_legacy_normalize.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add poller/legacy_normalize.py tests/test_legacy_normalize.py
git commit -m "feat(#162): legacy normalization helpers (paths, json lists, cache key, head-hash)"
```

---

## Task 3: DB access methods (`db/db.py`)

Add methods mirroring the existing `Database` style (per-thread `self.conn`, plain-dict returns). These cover: per-library cache-metadata get/set, asset upsert (idempotent on identity), count, iteration, and authoritative reconciliation (delete unseen rows for a library, returning the deleted cache keys so the indexer can GC their thumbnails).

**Files:**
- Modify: `db/db.py` (append methods inside `class Database`)
- Test: `tests/test_db_legacy.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_db_legacy.py
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.db import Database  # noqa: E402


def _migrate(db: Database) -> None:
    from db.migrations.migrate_026_legacy_index import run_on_conn

    run_on_conn(db.conn)


def _make_db(tmp_path) -> Database:
    db = Database(str(tmp_path / "curator.db"))
    _migrate(db)
    return db


def _asset(library_uuid="L", asset_uuid="A", **over) -> dict:
    row = {
        "library_uuid": library_uuid,
        "asset_uuid": asset_uuid,
        "original_filename": "img.jpg",
        "fingerprint": "fp",
        "date_taken": "2010-06-01 12:00:00",
        "width": 4000,
        "height": 3000,
        "latitude": None,
        "longitude": None,
        "title": "Birthday",
        "description": None,
        "keywords": "[]",
        "labels": "[]",
        "persons": '["Isaac"]',
        "named_face_count": 1,
        "unknown_face_count": 0,
        "master_rel_path": "Masters/2010/img.jpg",
        "thumbnail_cache_key": "deadbeef",
        "thumbnail_status": "ok",
    }
    row.update(over)
    return row


class TestLibraryMetadata:
    def test_set_then_get(self, tmp_path):
        db = _make_db(tmp_path)
        db.set_legacy_library({
            "library_uuid": "L", "display_name": "Old", "source_path_last_seen": "/mnt/x",
            "schema_version": 5002, "db_mtime": "2026-01-01T00:00:00", "db_size": 123,
            "db_head_hash": "abc", "asset_count": 0,
        })
        lib = db.get_legacy_library("L")
        assert lib["db_head_hash"] == "abc"
        assert lib["schema_version"] == 5002

    def test_get_missing_returns_none(self, tmp_path):
        db = _make_db(tmp_path)
        assert db.get_legacy_library("nope") is None

    def test_set_is_upsert(self, tmp_path):
        db = _make_db(tmp_path)
        db.set_legacy_library({"library_uuid": "L", "db_head_hash": "a", "asset_count": 0})
        db.set_legacy_library({"library_uuid": "L", "db_head_hash": "b", "asset_count": 5})
        lib = db.get_legacy_library("L")
        assert lib["db_head_hash"] == "b"
        assert lib["asset_count"] == 5


class TestUpsertAsset:
    def test_insert(self, tmp_path):
        db = _make_db(tmp_path)
        db.set_legacy_library({"library_uuid": "L", "asset_count": 0})
        db.upsert_legacy_asset(_asset())
        assert db.legacy_asset_count("L") == 1

    def test_update_same_identity_no_duplicate(self, tmp_path):
        db = _make_db(tmp_path)
        db.set_legacy_library({"library_uuid": "L", "asset_count": 0})
        db.upsert_legacy_asset(_asset(title="Birthday"))
        db.upsert_legacy_asset(_asset(title="Party"))
        assert db.legacy_asset_count("L") == 1
        rows = list(db.iter_legacy_assets("L"))
        assert rows[0]["title"] == "Party"

    def test_reindex_from_different_path_updates_same_row(self, tmp_path):
        db = _make_db(tmp_path)
        db.set_legacy_library({"library_uuid": "L", "asset_count": 0})
        db.upsert_legacy_asset(_asset(master_rel_path="Masters/a.jpg"))
        db.upsert_legacy_asset(_asset(master_rel_path="originals/a.jpg"))
        assert db.legacy_asset_count("L") == 1


class TestIterAssets:
    def test_iter_filtered_by_library(self, tmp_path):
        db = _make_db(tmp_path)
        db.set_legacy_library({"library_uuid": "L", "asset_count": 0})
        db.set_legacy_library({"library_uuid": "M", "asset_count": 0})
        db.upsert_legacy_asset(_asset("L", "A"))
        db.upsert_legacy_asset(_asset("M", "B"))
        uuids = {r["asset_uuid"] for r in db.iter_legacy_assets("L")}
        assert uuids == {"A"}


class TestReconcileDelete:
    def test_deletes_unseen_returns_cache_keys(self, tmp_path):
        db = _make_db(tmp_path)
        db.set_legacy_library({"library_uuid": "L", "asset_count": 0})
        db.upsert_legacy_asset(_asset("L", "A", thumbnail_cache_key="key-a"))
        db.upsert_legacy_asset(_asset("L", "B", thumbnail_cache_key="key-b"))
        # Only A was seen this run; B must be deleted.
        removed = db.delete_legacy_assets_not_in("L", {"A"})
        assert removed == ["key-b"]
        assert db.legacy_asset_count("L") == 1

    def test_empty_seen_set_deletes_all(self, tmp_path):
        db = _make_db(tmp_path)
        db.set_legacy_library({"library_uuid": "L", "asset_count": 0})
        db.upsert_legacy_asset(_asset("L", "A", thumbnail_cache_key="key-a"))
        removed = db.delete_legacy_assets_not_in("L", set())
        assert removed == ["key-a"]
        assert db.legacy_asset_count("L") == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_db_legacy.py -q`
Expected: FAIL — `AttributeError: 'Database' object has no attribute 'set_legacy_library'`.

- [ ] **Step 3: Add the methods**

Append inside `class Database` in `db/db.py` (e.g. after the merge methods). The columns list is the single source of truth for the upsert.

```python
    # -----------------------------------------------------------------------
    # Legacy library index (#162)
    # -----------------------------------------------------------------------

    _LEGACY_LIBRARY_COLS = (
        "library_uuid", "display_name", "source_path_last_seen", "schema_version",
        "db_mtime", "db_size", "db_head_hash", "asset_count", "indexed_at",
    )

    _LEGACY_ASSET_COLS = (
        "library_uuid", "asset_uuid", "original_filename", "fingerprint",
        "date_taken", "width", "height", "latitude", "longitude", "title",
        "description", "keywords", "labels", "persons", "named_face_count",
        "unknown_face_count", "master_rel_path", "thumbnail_cache_key",
        "thumbnail_status", "indexed_at",
    )

    def set_legacy_library(self, rec: dict) -> None:
        """Upsert a legacy_libraries row by library_uuid. Missing keys default
        to NULL; indexed_at defaults to now if absent."""
        rec = dict(rec)
        rec.setdefault("indexed_at", _now_iso())
        cols = [c for c in self._LEGACY_LIBRARY_COLS if c in rec]
        placeholders = ", ".join("?" for _ in cols)
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "library_uuid")
        self.conn.execute(
            f"INSERT INTO legacy_libraries ({', '.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT(library_uuid) DO UPDATE SET {updates}",
            [rec[c] for c in cols],
        )
        self.conn.commit()

    def get_legacy_library(self, library_uuid: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM legacy_libraries WHERE library_uuid = ?", (library_uuid,)
        ).fetchone()
        return _row_to_dict(row) if row else None

    def upsert_legacy_asset(self, rec: dict) -> None:
        """Upsert one legacy_assets row, idempotent on (library_uuid, asset_uuid)."""
        rec = dict(rec)
        rec.setdefault("indexed_at", _now_iso())
        cols = list(self._LEGACY_ASSET_COLS)
        placeholders = ", ".join("?" for _ in cols)
        updates = ", ".join(
            f"{c}=excluded.{c}" for c in cols
            if c not in ("library_uuid", "asset_uuid")
        )
        self.conn.execute(
            f"INSERT INTO legacy_assets ({', '.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT(library_uuid, asset_uuid) DO UPDATE SET {updates}",
            [rec.get(c) for c in cols],
        )
        self.conn.commit()

    def legacy_asset_count(self, library_uuid: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM legacy_assets WHERE library_uuid = ?", (library_uuid,)
        ).fetchone()
        return int(row[0])

    def iter_legacy_assets(self, library_uuid: str):
        """Yield legacy_assets rows for a library as dicts, ordered by asset_uuid."""
        for row in self.conn.execute(
            "SELECT * FROM legacy_assets WHERE library_uuid = ? ORDER BY asset_uuid",
            (library_uuid,),
        ):
            yield _row_to_dict(row)

    def delete_legacy_assets_not_in(
        self, library_uuid: str, seen_asset_uuids: set[str]
    ) -> list[str]:
        """Hard-delete rows for this library whose asset_uuid was NOT seen this run.
        Returns the thumbnail_cache_keys of deleted rows (for thumbnail GC).
        Authoritative reconciliation — callers must only invoke after a FULL run
        completes successfully (never for --limit / interrupted runs)."""
        rows = self.conn.execute(
            "SELECT asset_uuid, thumbnail_cache_key FROM legacy_assets WHERE library_uuid = ?",
            (library_uuid,),
        ).fetchall()
        to_delete = [r for r in rows if r["asset_uuid"] not in seen_asset_uuids]
        removed_keys = [
            r["thumbnail_cache_key"] for r in to_delete if r["thumbnail_cache_key"]
        ]
        for r in to_delete:
            self.conn.execute(
                "DELETE FROM legacy_assets WHERE library_uuid = ? AND asset_uuid = ?",
                (library_uuid, r["asset_uuid"]),
            )
        self.conn.commit()
        return removed_keys
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_db_legacy.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add db/db.py tests/test_db_legacy.py
git commit -m "feat(#162): Database methods for legacy library + asset upsert/reconcile"
```

---

## Task 4: Local DB cache (`poller/legacy_cache.py`)

Mirror the slow-over-AFP library DB to a local cache under `data/legacy-cache/<library_uuid>/`, validated against `legacy_libraries` (mtime + size + head-hash). Build is atomic (temp dir + rename) so a partial copy is never treated as valid.

**Files:**
- Create: `poller/legacy_cache.py`
- Test: `tests/test_legacy_cache.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_legacy_cache.py
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "poller"))

from legacy_cache import (  # noqa: E402
    cache_dir,
    cache_root,
    locate_source_db,
    source_db_stats,
    is_cache_valid,
)


def _fake_library(tmp_path) -> Path:
    """Build a minimal Photos-4-shaped bundle with a database/ dir."""
    lib = tmp_path / "Old.photoslibrary"
    (lib / "database").mkdir(parents=True)
    db = lib / "database" / "photos.db"
    db.write_bytes(b"SQLITEHEADER" + b"\x00" * 500)
    return lib


class TestLocateSourceDb:
    def test_finds_photos4_db(self, tmp_path):
        lib = _fake_library(tmp_path)
        assert locate_source_db(str(lib)) == str(lib / "database" / "photos.db")

    def test_missing_returns_none(self, tmp_path):
        assert locate_source_db(str(tmp_path / "nope.photoslibrary")) is None


class TestCacheRoot:
    def test_root_is_beside_curator_db(self, tmp_path):
        db_path = tmp_path / "data" / "curator.db"
        assert cache_root(str(db_path)) == tmp_path / "data" / "legacy-cache"

    def test_dir_keyed_by_library_uuid(self, tmp_path):
        db_path = tmp_path / "data" / "curator.db"
        assert cache_dir(str(db_path), "LIB") == tmp_path / "data" / "legacy-cache" / "LIB"


class TestValidity:
    def test_valid_when_all_match(self, tmp_path):
        lib = _fake_library(tmp_path)
        src = locate_source_db(str(lib))
        stats = source_db_stats(src)
        lib_rec = {
            "db_mtime": stats["db_mtime"],
            "db_size": stats["db_size"],
            "db_head_hash": stats["db_head_hash"],
        }
        assert is_cache_valid(lib_rec, src) is True

    def test_invalid_when_hash_differs_same_mtime_size(self, tmp_path):
        lib = _fake_library(tmp_path)
        src = locate_source_db(str(lib))
        stats = source_db_stats(src)
        lib_rec = {
            "db_mtime": stats["db_mtime"],
            "db_size": stats["db_size"],
            "db_head_hash": "different",
        }
        assert is_cache_valid(lib_rec, src) is False

    def test_invalid_when_record_missing(self, tmp_path):
        lib = _fake_library(tmp_path)
        src = locate_source_db(str(lib))
        assert is_cache_valid(None, src) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_legacy_cache.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'legacy_cache'`.

- [ ] **Step 3: Write the cache module**

```python
# poller/legacy_cache.py
"""Local DB cache for the legacy library indexer (#162).

Mirrors the library's database/ directory to data/legacy-cache/<library_uuid>/
so osxphotos can open a local copy instead of the slow AFP mount. Validity is
judged against legacy_libraries metadata (mtime + size + 16 MiB head-hash);
there is no filesystem sidecar. Build is atomic (temp dir + rename).
"""

from __future__ import annotations

import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from legacy_normalize import head_hash  # noqa: E402

# Candidate locations of the primary asset DB, newest schema first.
_DB_CANDIDATES = (
    "database/Photos.sqlite",   # Photos 5+
    "database/photos.db",       # Photos 4 (our target)
)


def locate_source_db(library_path: str) -> str | None:
    lib = Path(library_path)
    for rel in _DB_CANDIDATES:
        p = lib / rel
        if p.exists():
            return str(p)
    return None


def source_db_stats(db_path: str) -> dict:
    st = Path(db_path).stat()
    return {
        "db_mtime": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
        "db_size": st.st_size,
        "db_head_hash": head_hash(db_path),
    }


def cache_root(curator_db_path: str) -> Path:
    """legacy-cache dir beside curator.db (under data/, which is git-ignored)."""
    return Path(curator_db_path).expanduser().resolve().parent / "legacy-cache"


def cache_dir(curator_db_path: str, library_uuid: str) -> Path:
    return cache_root(curator_db_path) / library_uuid


def is_cache_valid(library_rec: dict | None, source_db_path: str) -> bool:
    """True iff the recorded mtime + size + head-hash all match the live source."""
    if not library_rec:
        return False
    try:
        stats = source_db_stats(source_db_path)
    except OSError:
        return False
    return (
        library_rec.get("db_mtime") == stats["db_mtime"]
        and library_rec.get("db_size") == stats["db_size"]
        and library_rec.get("db_head_hash") == stats["db_head_hash"]
    )


def build_cache(library_path: str, library_uuid: str, curator_db_path: str) -> str:
    """Copy the library's database/ dir + top-level plists into a local skeleton
    bundle, atomically. Returns the path to the cached bundle (suitable for
    osxphotos.PhotosDB). Replaces any existing/partial cache."""
    lib = Path(library_path)
    dest = cache_dir(curator_db_path, library_uuid)
    tmp = dest.parent / f".{library_uuid}.tmp"
    if tmp.exists():
        shutil.rmtree(tmp)
    (tmp / "database").parent.mkdir(parents=True, exist_ok=True)

    shutil.copytree(lib / "database", tmp / "database")
    # osxphotos needs the bundle plists to recognize the library version.
    for plist in lib.glob("*.plist"):
        shutil.copy2(plist, tmp / plist.name)

    if dest.exists():
        shutil.rmtree(dest)
    tmp.rename(dest)  # atomic: a partial copy never lands at dest
    return str(dest)


def ensure_cache(
    db, library_path: str, library_uuid: str, curator_db_path: str, force: bool = False
) -> str:
    """Return a local bundle path osxphotos can open, building/refreshing the
    cache when invalid or forced. db is the Database (for legacy_libraries)."""
    source_db = locate_source_db(library_path)
    if source_db is None:
        raise FileNotFoundError(f"No Photos DB found under {library_path}")
    dest = cache_dir(curator_db_path, library_uuid)
    rec = db.get_legacy_library(library_uuid)
    if not force and dest.exists() and is_cache_valid(rec, source_db):
        return str(dest)
    return build_cache(library_path, library_uuid, curator_db_path)
```

> **Note:** `ensure_cache`/`build_cache` perform real filesystem copies and are exercised by the indexer integration step, not unit-tested against a 6.5 GB library. The unit tests cover location, root derivation, and validity logic — the parts with branching behavior.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_legacy_cache.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add poller/legacy_cache.py tests/test_legacy_cache.py
git commit -m "feat(#162): local DB cache with mtime+size+head-hash validation"
```

---

## Task 5: Indexer (`poller/legacy_indexer.py`)

Opens the library via `osxphotos.PhotosDB`, builds one normalized row per asset, copies an existing thumbnail derivative into BP's cache, and upserts. Full runs reconcile (delete unseen rows + GC orphaned thumbnails) **only after the iteration completes successfully**; `--limit` runs skip reconciliation entirely. A `photosdb_factory` parameter lets tests inject a fake PhotosDB so no real multi-GB read happens in CI.

**Files:**
- Create: `poller/legacy_indexer.py`
- Test: `tests/test_legacy_indexer.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_legacy_indexer.py
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "poller"))

from db.db import Database  # noqa: E402
import legacy_indexer  # noqa: E402
from legacy_normalize import thumbnail_cache_key, thumbnail_path  # noqa: E402


class FakeFace:
    def __init__(self, name):
        self.name = name


class FakePhoto:
    def __init__(self, uuid, *, persons=(), faces=(), path=None, derivatives=(), **kw):
        self.uuid = uuid
        self.original_filename = kw.get("original_filename", f"{uuid}.jpg")
        self.fingerprint = kw.get("fingerprint", "fp")
        self.date = kw.get("date")
        self.width = kw.get("width", 4000)
        self.height = kw.get("height", 3000)
        self.location = kw.get("location", (None, None))
        self.title = kw.get("title")
        self.description = kw.get("description")
        self.keywords = list(kw.get("keywords", []))
        self.labels = list(kw.get("labels", []))
        self.persons = list(persons)
        self.face_info = [FakeFace(n) for n in faces]
        self.path = path
        self.path_derivatives = list(derivatives)


class FakePhotosDB:
    def __init__(self, photos, db_version="5002", uuid="LIB-UUID"):
        self._photos = photos
        self.db_version = db_version
        self.library_path = "/fake/Old.photoslibrary"
        self._uuid = uuid

    def photos(self):
        return list(self._photos)


def _factory(photos, **kw):
    def make(_path):
        return FakePhotosDB(photos, **kw)
    return make


def _db(tmp_path) -> Database:
    db = Database(str(tmp_path / "curator.db"))
    from db.migrations.migrate_026_legacy_index import run_on_conn

    run_on_conn(db.conn)
    return db


def test_indexes_persons_and_face_counts(tmp_path):
    db = _db(tmp_path)
    photos = [FakePhoto("A", persons=["Isaac", "May"],
                        faces=["Isaac", "May", "_UNKNOWN_"])]
    stats = legacy_indexer.index_library(
        "/fake/Old.photoslibrary", db,
        curator_db_path=str(tmp_path / "curator.db"),
        thumb_root=tmp_path / "thumbs",
        copy_thumbnails=False, use_cache=False,
        photosdb_factory=_factory(photos),
    )
    assert stats["indexed"] == 1
    row = next(db.iter_legacy_assets("LIB-UUID"))
    assert json.loads(row["persons"]) == ["Isaac", "May"]
    assert row["named_face_count"] == 2
    assert row["unknown_face_count"] == 1


def test_persons_sorted_unique_deterministic(tmp_path):
    db = _db(tmp_path)
    photos = [FakePhoto("A", persons=["May", "Isaac", "May"])]
    legacy_indexer.index_library(
        "/fake/Old.photoslibrary", db, curator_db_path=str(tmp_path / "curator.db"),
        thumb_root=tmp_path / "thumbs", copy_thumbnails=False, use_cache=False,
        photosdb_factory=_factory(photos),
    )
    row = next(db.iter_legacy_assets("LIB-UUID"))
    assert json.loads(row["persons"]) == ["Isaac", "May"]


def test_master_rel_path_is_bundle_relative_posix(tmp_path):
    db = _db(tmp_path)
    photos = [FakePhoto("A", path="/fake/Old.photoslibrary/Masters/2010/A.jpg")]
    legacy_indexer.index_library(
        "/fake/Old.photoslibrary", db, curator_db_path=str(tmp_path / "curator.db"),
        thumb_root=tmp_path / "thumbs", copy_thumbnails=False, use_cache=False,
        photosdb_factory=_factory(photos),
    )
    row = next(db.iter_legacy_assets("LIB-UUID"))
    assert row["master_rel_path"] == "Masters/2010/A.jpg"


def test_thumbnail_copied_when_derivative_exists(tmp_path):
    db = _db(tmp_path)
    deriv = tmp_path / "deriv.jpg"
    deriv.write_bytes(b"JPEGDATA")
    photos = [FakePhoto("A", derivatives=[str(deriv)])]
    legacy_indexer.index_library(
        "/fake/Old.photoslibrary", db, curator_db_path=str(tmp_path / "curator.db"),
        thumb_root=tmp_path / "thumbs", copy_thumbnails=True, use_cache=False,
        photosdb_factory=_factory(photos),
    )
    row = next(db.iter_legacy_assets("LIB-UUID"))
    assert row["thumbnail_status"] == "ok"
    key = thumbnail_cache_key("LIB-UUID", "A")
    assert thumbnail_path(tmp_path / "thumbs", "LIB-UUID", key).exists()


def test_thumbnail_miss_records_status_does_not_fail(tmp_path):
    db = _db(tmp_path)
    photos = [FakePhoto("A", derivatives=[])]  # no derivative on disk
    stats = legacy_indexer.index_library(
        "/fake/Old.photoslibrary", db, curator_db_path=str(tmp_path / "curator.db"),
        thumb_root=tmp_path / "thumbs", copy_thumbnails=True, use_cache=False,
        photosdb_factory=_factory(photos),
    )
    assert stats["indexed"] == 1
    row = next(db.iter_legacy_assets("LIB-UUID"))
    assert row["thumbnail_status"] == "missing"


def test_full_run_reconciles_deleted_assets(tmp_path):
    db = _db(tmp_path)
    # First run: A and B present.
    legacy_indexer.index_library(
        "/fake/Old.photoslibrary", db, curator_db_path=str(tmp_path / "curator.db"),
        thumb_root=tmp_path / "thumbs", copy_thumbnails=False, use_cache=False,
        photosdb_factory=_factory([FakePhoto("A"), FakePhoto("B")]),
    )
    assert db.legacy_asset_count("LIB-UUID") == 2
    # Second full run: only A present -> B reconciled away.
    legacy_indexer.index_library(
        "/fake/Old.photoslibrary", db, curator_db_path=str(tmp_path / "curator.db"),
        thumb_root=tmp_path / "thumbs", copy_thumbnails=False, use_cache=False,
        photosdb_factory=_factory([FakePhoto("A")]),
    )
    uuids = {r["asset_uuid"] for r in db.iter_legacy_assets("LIB-UUID")}
    assert uuids == {"A"}


def test_limit_run_does_not_reconcile(tmp_path):
    db = _db(tmp_path)
    legacy_indexer.index_library(
        "/fake/Old.photoslibrary", db, curator_db_path=str(tmp_path / "curator.db"),
        thumb_root=tmp_path / "thumbs", copy_thumbnails=False, use_cache=False,
        photosdb_factory=_factory([FakePhoto("A"), FakePhoto("B")]),
    )
    # limit=1: non-authoritative, deletes nothing even though only 1 seen.
    legacy_indexer.index_library(
        "/fake/Old.photoslibrary", db, curator_db_path=str(tmp_path / "curator.db"),
        thumb_root=tmp_path / "thumbs", copy_thumbnails=False, use_cache=False, limit=1,
        photosdb_factory=_factory([FakePhoto("A"), FakePhoto("B")]),
    )
    assert db.legacy_asset_count("LIB-UUID") == 2


def test_interrupted_full_run_does_not_reconcile(tmp_path):
    db = _db(tmp_path)
    legacy_indexer.index_library(
        "/fake/Old.photoslibrary", db, curator_db_path=str(tmp_path / "curator.db"),
        thumb_root=tmp_path / "thumbs", copy_thumbnails=False, use_cache=False,
        photosdb_factory=_factory([FakePhoto("A"), FakePhoto("B")]),
    )

    class Boom(FakePhotosDB):
        def photos(self):
            yield FakePhoto("A")
            raise RuntimeError("share unmounted mid-iteration")

    def boom_factory(_path):
        return Boom([])

    import pytest

    with pytest.raises(RuntimeError):
        legacy_indexer.index_library(
            "/fake/Old.photoslibrary", db, curator_db_path=str(tmp_path / "curator.db"),
            thumb_root=tmp_path / "thumbs", copy_thumbnails=False, use_cache=False,
            photosdb_factory=boom_factory,
        )
    # B must survive: an interrupted full run reconciles nothing.
    assert db.legacy_asset_count("LIB-UUID") == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_legacy_indexer.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'legacy_indexer'`.

- [ ] **Step 3: Write the indexer**

```python
# poller/legacy_indexer.py
"""Legacy (migrated iPhoto/Photos 4) library indexer (#162).

Opens the library via osxphotos, builds one normalized row per asset in
legacy_assets, and copies an existing thumbnail derivative into BP's thumb
cache (path-independent identity). Full runs are an authoritative mirror:
after a successful full iteration they reconcile (delete unseen rows) and GC
orphaned thumbnails. --limit runs are non-authoritative (no deletions).
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Callable

from legacy_cache import ensure_cache
from legacy_normalize import (
    canonical_rel_path,
    normalize_json_list,
    thumbnail_cache_key,
    thumbnail_path,
)

log = logging.getLogger("blue-pearmain.legacy-indexer")

_UNKNOWN = {"", "_UNKNOWN_", None}


def _library_uuid(photosdb) -> str:
    """Path-independent identity of the source bundle."""
    for attr in ("library_uuid", "_uuid"):
        val = getattr(photosdb, attr, None)
        if val:
            return str(val)
    raise ValueError("PhotosDB exposed no library UUID")


def _face_counts(photo) -> tuple[int, int]:
    faces = getattr(photo, "face_info", None) or []
    named = sum(1 for f in faces if getattr(f, "name", None) not in _UNKNOWN)
    unknown = sum(1 for f in faces if getattr(f, "name", None) in _UNKNOWN)
    return named, unknown


def _persons(photo) -> list[str]:
    return [p for p in (getattr(photo, "persons", None) or []) if p not in _UNKNOWN]


def _rel_master(photo, library_path: str) -> str | None:
    p = getattr(photo, "path", None)
    if not p:
        return None
    try:
        rel = str(Path(p).relative_to(Path(library_path)))
    except ValueError:
        rel = p
    return canonical_rel_path(rel)


def _copy_thumbnail(photo, library_uuid: str, thumb_root: Path) -> str:
    """Copy the first existing derivative into the cache. Returns ok/missing/error."""
    derivs = getattr(photo, "path_derivatives", None) or []
    src = next((d for d in derivs if d and Path(d).exists()), None)
    if src is None:
        return "missing"
    try:
        key = thumbnail_cache_key(library_uuid, photo.uuid)
        dest = thumbnail_path(thumb_root, library_uuid, key)
        if dest.exists():
            return "ok"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        return "ok"
    except OSError as exc:
        log.warning("thumbnail copy failed for %s: %s", photo.uuid, exc)
        return "error"


def _build_row(photo, library_uuid: str, library_path: str) -> dict:
    lat, lon = getattr(photo, "location", (None, None)) or (None, None)
    date = getattr(photo, "date", None)
    named, unknown = _face_counts(photo)
    return {
        "library_uuid": library_uuid,
        "asset_uuid": photo.uuid,
        "original_filename": getattr(photo, "original_filename", None),
        "fingerprint": getattr(photo, "fingerprint", None),
        "date_taken": date.isoformat() if date else None,
        "width": getattr(photo, "width", None),
        "height": getattr(photo, "height", None),
        "latitude": lat,
        "longitude": lon,
        "title": getattr(photo, "title", None),
        "description": getattr(photo, "description", None),
        "keywords": normalize_json_list(getattr(photo, "keywords", [])),
        "labels": normalize_json_list(getattr(photo, "labels", [])),
        "persons": normalize_json_list(_persons(photo)),
        "named_face_count": named,
        "unknown_face_count": unknown,
        "master_rel_path": _rel_master(photo, library_path),
    }


def _open_photosdb(library_path, db, library_uuid_hint, curator_db_path,
                   use_cache, refresh_cache, photosdb_factory):
    import osxphotos

    factory: Callable = photosdb_factory or (lambda p: osxphotos.PhotosDB(p))
    if not use_cache:
        return factory(library_path)
    # Open once to learn the library_uuid, then cache by it.
    probe = factory(library_path)
    uuid = _library_uuid(probe)
    bundle = ensure_cache(db, library_path, uuid, curator_db_path, force=refresh_cache)
    return factory(bundle)


def index_library(
    library_path: str,
    db,
    *,
    curator_db_path: str,
    thumb_root: Path | str,
    copy_thumbnails: bool = True,
    limit: int | None = None,
    use_cache: bool = True,
    refresh_cache: bool = False,
    photosdb_factory: Callable | None = None,
) -> dict:
    """Index the legacy library into curator.db. Returns a stats dict."""
    thumb_root = Path(thumb_root)
    photosdb = _open_photosdb(
        library_path, db, None, curator_db_path,
        use_cache, refresh_cache, photosdb_factory,
    )
    library_uuid = _library_uuid(photosdb)
    schema_version = getattr(photosdb, "db_version", None)

    seen: set[str] = set()
    indexed = thumb_ok = thumb_missing = thumb_error = 0

    for photo in photosdb.photos():
        if limit is not None and indexed >= limit:
            break
        row = _build_row(photo, library_uuid, library_path)
        if copy_thumbnails:
            status = _copy_thumbnail(photo, library_uuid, thumb_root)
        else:
            status = "skipped"
        row["thumbnail_cache_key"] = thumbnail_cache_key(library_uuid, photo.uuid)
        row["thumbnail_status"] = status
        db.upsert_legacy_asset(row)
        seen.add(photo.uuid)
        indexed += 1
        thumb_ok += status == "ok"
        thumb_missing += status == "missing"
        thumb_error += status == "error"

    # Authoritative reconciliation — only after a successful FULL iteration.
    # An exception above propagates and skips this block (interrupted == no delete).
    reconciled = 0
    if limit is None:
        removed_keys = db.delete_legacy_assets_not_in(library_uuid, seen)
        reconciled = len(removed_keys)
        for key in removed_keys:
            stale = thumbnail_path(thumb_root, library_uuid, key)
            try:
                stale.unlink(missing_ok=True)
            except OSError:
                pass

    try:
        schema_int = int(schema_version) if schema_version is not None else None
    except (TypeError, ValueError):
        schema_int = None

    from legacy_cache import locate_source_db, source_db_stats

    lib_rec = {
        "library_uuid": library_uuid,
        "source_path_last_seen": library_path,
        "schema_version": schema_int,
        "asset_count": db.legacy_asset_count(library_uuid),
    }
    src = locate_source_db(library_path)
    if src:
        try:
            lib_rec.update(source_db_stats(src))
        except OSError:
            pass
    db.set_legacy_library(lib_rec)

    stats = {
        "library_uuid": library_uuid,
        "indexed": indexed,
        "reconciled": reconciled,
        "thumb_ok": thumb_ok,
        "thumb_missing": thumb_missing,
        "thumb_error": thumb_error,
        "authoritative": limit is None,
    }
    log.info("legacy index complete: %s", stats)
    return stats
```

> **Note on `source_db_stats` in tests:** the fake library path `/fake/Old.photoslibrary` has no DB on disk, so `locate_source_db` returns `None` and the stats update is skipped — `set_legacy_library` still records identity + count. The cache path (`use_cache=False` in every unit test) avoids opening a real bundle.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_legacy_indexer.py -q`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add poller/legacy_indexer.py tests/test_legacy_indexer.py
git commit -m "feat(#162): legacy indexer — osxphotos read, thumbnail copy, authoritative reconcile"
```

---

## Task 6: Match tiers (`poller/legacy_match.py`)

Pure tier logic + deterministic ordering, reusing the existing `_normalise_to_utc_second` so legacy matching is consistent with reupload/orphan matching. No DB access — the caller passes a photo dict and its candidate legacy rows.

**Files:**
- Create: `poller/legacy_match.py`
- Test: `tests/test_legacy_match.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_legacy_match.py
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "poller"))

from legacy_match import classify_match, order_rows, preview_rows  # noqa: E402


def _photo(**kw):
    base = {"flickr_id": "1", "date_taken": "2010-06-01 12:00:00",
            "width": 4000, "height": 3000, "flickr_title": "Birthday"}
    base.update(kw)
    return base


def _cand(asset_uuid="A", **kw):
    base = {"asset_uuid": asset_uuid, "date_taken": "2010-06-01T12:00:00-00:00",
            "width": 4000, "height": 3000, "title": "Birthday"}
    base.update(kw)
    return base


class TestClassify:
    def test_confident_single_dims_and_title(self):
        tier, matches = classify_match(_photo(), [_cand("A")])
        assert tier == "confident"
        assert [m["asset_uuid"] for m in matches] == ["A"]

    def test_naive_flickr_vs_tzaware_apple_same_utc_second(self):
        # Flickr naive (UTC) vs Apple tz-aware that normalize to the same second.
        photo = _photo(date_taken="2010-06-01 16:00:00")
        cand = _cand("A", date_taken="2010-06-01T12:00:00-04:00")
        tier, _ = classify_match(photo, [cand])
        assert tier == "confident"

    def test_no_match_when_no_timestamp_candidate(self):
        tier, matches = classify_match(_photo(), [_cand("A", date_taken="2011-01-01 00:00:00")])
        assert tier == "no-match"
        assert matches == []

    def test_ambiguous_two_timestamp_matches(self):
        tier, matches = classify_match(_photo(), [_cand("A"), _cand("B")])
        assert tier == "ambiguous"
        assert {m["asset_uuid"] for m in matches} == {"A", "B"}

    def test_ambiguous_single_but_dims_conflict(self):
        tier, _ = classify_match(_photo(), [_cand("A", width=100, height=100)])
        assert tier == "ambiguous"

    def test_empty_title_one_side_never_demotes(self):
        # Confident even though Flickr title is missing.
        tier, _ = classify_match(_photo(flickr_title=""), [_cand("A", title="Birthday")])
        assert tier == "confident"

    def test_both_titles_nonempty_and_differ_is_ambiguous(self):
        tier, _ = classify_match(_photo(flickr_title="Party"), [_cand("A", title="Birthday")])
        assert tier == "ambiguous"

    def test_title_whitespace_only_counts_missing(self):
        tier, _ = classify_match(_photo(flickr_title="   "), [_cand("A", title="Birthday")])
        assert tier == "confident"


class TestPreviewRowsAndOrdering:
    def test_preview_emits_one_row_per_no_match(self):
        rows = preview_rows([(_photo(flickr_id="9"), [])])
        assert len(rows) == 1
        assert rows[0]["tier"] == "no-match"
        assert rows[0]["asset_uuid"] == ""

    def test_preview_emits_row_per_candidate_for_ambiguous(self):
        rows = preview_rows([(_photo(), [_cand("B"), _cand("A")])])
        assert {r["asset_uuid"] for r in rows} == {"A", "B"}
        assert all(r["tier"] == "ambiguous" for r in rows)

    def test_order_is_tier_then_date_then_flickr_then_asset(self):
        rows = [
            {"tier": "no-match", "date_norm": "2010-01-01 00:00:00", "flickr_id": "5", "asset_uuid": ""},
            {"tier": "confident", "date_norm": "2010-01-01 00:00:00", "flickr_id": "2", "asset_uuid": "Z"},
            {"tier": "ambiguous", "date_norm": "2009-01-01 00:00:00", "flickr_id": "1", "asset_uuid": "B"},
            {"tier": "ambiguous", "date_norm": "2009-01-01 00:00:00", "flickr_id": "1", "asset_uuid": "A"},
        ]
        out = order_rows(rows)
        assert [r["tier"] for r in out] == ["confident", "ambiguous", "ambiguous", "no-match"]
        # Within the two ambiguous rows: same date+flickr_id, so asset_uuid breaks the tie.
        assert [r["asset_uuid"] for r in out[1:3]] == ["A", "B"]

    def test_order_is_stable_across_runs(self):
        rows = [
            {"tier": "confident", "date_norm": "2010", "flickr_id": "2", "asset_uuid": "A"},
            {"tier": "confident", "date_norm": "2010", "flickr_id": "1", "asset_uuid": "A"},
        ]
        assert order_rows(list(rows)) == order_rows(list(reversed(rows)))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_legacy_match.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'legacy_match'`.

- [ ] **Step 3: Write the matcher**

```python
# poller/legacy_match.py
"""Non-destructive match-preview tiers for the legacy indexer (#162).

Pure logic: given a Flickr-only photo dict and candidate legacy_assets rows,
classify into confident / ambiguous / no-match and emit deterministically
ordered rows for the report/CSV. Reuses the existing UTC-second normalizer so
legacy matching is consistent with reupload/orphan matching.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from deduplicator import _normalise_to_utc_second  # noqa: E402
from legacy_normalize import normalize_title  # noqa: E402

CONFIDENT = "confident"
AMBIGUOUS = "ambiguous"
NO_MATCH = "no-match"

_TIER_ORDER = {CONFIDENT: 0, AMBIGUOUS: 1, NO_MATCH: 2}


def _norm_dt(value) -> str | None:
    return _normalise_to_utc_second(value) if value else None


def _dims_match(photo: dict, cand: dict) -> bool:
    return photo.get("width") == cand.get("width") and photo.get("height") == cand.get("height")


def _title_conflict(photo: dict, cand: dict) -> bool:
    """Conflict only when BOTH titles are non-empty after normalization and differ."""
    a = normalize_title(photo.get("flickr_title"))
    b = normalize_title(cand.get("title"))
    if a is None or b is None:
        return False
    return a != b


def classify_match(photo: dict, candidates: list[dict]) -> tuple[str, list[dict]]:
    """Return (tier, matched_candidates). matched_candidates are the timestamp
    matches (the rows the report should show); empty for no-match."""
    pd = _norm_dt(photo.get("date_taken"))
    if pd is None:
        return NO_MATCH, []
    time_matches = [c for c in candidates if _norm_dt(c.get("date_taken")) == pd]
    if not time_matches:
        return NO_MATCH, []
    if len(time_matches) == 1:
        c = time_matches[0]
        if _dims_match(photo, c) and not _title_conflict(photo, c):
            return CONFIDENT, time_matches
        return AMBIGUOUS, time_matches
    return AMBIGUOUS, time_matches


def preview_rows(photo_candidate_pairs) -> list[dict]:
    """Build (unordered) report rows from (photo, candidates) pairs.

    confident -> one row (the match); ambiguous -> one row per timestamp
    candidate; no-match -> one row with empty asset_uuid.
    """
    rows: list[dict] = []
    for photo, candidates in photo_candidate_pairs:
        tier, matches = classify_match(photo, candidates)
        date_norm = _norm_dt(photo.get("date_taken")) or ""
        flickr_id = str(photo.get("flickr_id", ""))
        if tier == NO_MATCH:
            rows.append({
                "tier": tier, "date_norm": date_norm, "flickr_id": flickr_id,
                "asset_uuid": "", "width": photo.get("width"), "height": photo.get("height"),
                "flickr_title": photo.get("flickr_title") or "",
            })
            continue
        for c in matches:
            rows.append({
                "tier": tier, "date_norm": date_norm, "flickr_id": flickr_id,
                "asset_uuid": c.get("asset_uuid", ""),
                "width": photo.get("width"), "height": photo.get("height"),
                "flickr_title": photo.get("flickr_title") or "",
                "legacy_persons": c.get("persons", "[]"),
                "legacy_title": c.get("title") or "",
            })
    return rows


def order_rows(rows: list[dict]) -> list[dict]:
    """Deterministic order: tier -> date_norm -> flickr_id -> asset_uuid."""
    return sorted(
        rows,
        key=lambda r: (
            _TIER_ORDER.get(r["tier"], 99),
            r.get("date_norm", ""),
            r.get("flickr_id", ""),
            r.get("asset_uuid", ""),
        ),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_legacy_match.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add poller/legacy_match.py tests/test_legacy_match.py
git commit -m "feat(#162): match-preview tiers + deterministic ordering"
```

---

## Task 7: CLI subcommands (`bp`)

Add `index-legacy` and `match-legacy-preview`. Wire each in three places (matching the existing pattern): `sub.add_parser(...)`, an `args` default-guard in the `if not hasattr(...)` block, and the `dispatch` dict. Both load config per-command and never print config contents.

**Files:**
- Modify: `bp` (add two `cmd_*` functions + registration)
- Test: `tests/test_cli_legacy.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli_legacy.py
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
BP = ROOT / "bp"


def _run(*args):
    return subprocess.run(
        [sys.executable, str(BP), *args],
        capture_output=True, text=True, cwd=str(ROOT),
    )


def test_index_legacy_registered():
    r = _run("index-legacy", "--help")
    assert r.returncode == 0
    assert "--library" in r.stdout
    assert "--no-thumbnails" in r.stdout
    assert "--refresh-cache" in r.stdout


def test_match_legacy_preview_registered():
    r = _run("match-legacy-preview", "--help")
    assert r.returncode == 0
    assert "--csv" in r.stdout


def test_top_level_help_lists_commands():
    r = _run("--help")
    assert r.returncode == 0
    assert "index-legacy" in r.stdout
    assert "match-legacy-preview" in r.stdout
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cli_legacy.py -q`
Expected: FAIL — the subcommands are not registered (`index-legacy` invalid choice / not in `--help`).

- [ ] **Step 3a: Add the two command functions to `bp`**

Add near the other `cmd_*` functions (e.g. after `cmd_migrate`). Imports are local to match the file's style.

```python
def _resolve_legacy_library_path(args, config) -> str:
    """--library wins; else fall back to optional config legacy_library.path."""
    if getattr(args, "library", None):
        return str(Path(args.library).expanduser())
    cfg = (config.get("legacy_library") or {}).get("path")
    if cfg:
        return str(Path(cfg).expanduser())
    print("Error: no library path. Pass --library <path> or set legacy_library.path in config.",
          file=sys.stderr)
    sys.exit(2)


def cmd_index_legacy(args: argparse.Namespace) -> None:
    """Index a legacy (migrated iPhoto/Photos 4) library into curator.db."""
    import yaml
    sys.path.insert(0, str(ROOT / "poller"))
    from db.db import Database
    from legacy_indexer import index_library

    with open(args.config) as f:
        config = yaml.safe_load(f)

    db_path = str(Path(config["database"]["path"]).expanduser())
    thumb_root = Path(config["thumbnails"]["path"]).expanduser()
    library_path = _resolve_legacy_library_path(args, config)

    if not Path(library_path).exists():
        print(f"Error: library not found / not mounted: {library_path}", file=sys.stderr)
        sys.exit(1)

    db = Database(db_path)
    try:
        stats = index_library(
            library_path, db,
            curator_db_path=db_path,
            thumb_root=thumb_root,
            copy_thumbnails=not args.no_thumbnails,
            limit=args.limit,
            use_cache=not args.no_cache,
            refresh_cache=args.refresh_cache,
        )
    finally:
        db.close()

    print(
        f"Indexed {stats['indexed']} assets from library {stats['library_uuid']} "
        f"(thumbs ok={stats['thumb_ok']} missing={stats['thumb_missing']} "
        f"error={stats['thumb_error']}; "
        f"{'authoritative, reconciled ' + str(stats['reconciled']) if stats['authoritative'] else 'non-authoritative (--limit), no deletions'})."
    )


def cmd_match_legacy_preview(args: argparse.Namespace) -> None:
    """Non-destructive report: which legacy assets likely match Flickr-only
    candidate_public photos. Writes nothing to photos."""
    import csv
    import yaml
    from collections import defaultdict
    sys.path.insert(0, str(ROOT / "poller"))
    from db.db import Database
    from deduplicator import _normalise_to_utc_second
    from legacy_match import order_rows, preview_rows

    with open(args.config) as f:
        config = yaml.safe_load(f)
    db_path = str(Path(config["database"]["path"]).expanduser())
    db = Database(db_path)

    try:
        # Determine library_uuid: explicit flag, else the single indexed library.
        if getattr(args, "library_uuid", None):
            library_uuid = args.library_uuid
        else:
            libs = db.conn.execute(
                "SELECT library_uuid FROM legacy_libraries ORDER BY indexed_at DESC"
            ).fetchall()
            if not libs:
                print("No indexed legacy library found. Run 'bp index-legacy' first.",
                      file=sys.stderr)
                sys.exit(1)
            if len(libs) > 1 and not args.library_uuid:
                print("Multiple legacy libraries indexed; pass --library-uuid <uuid>.",
                      file=sys.stderr)
                sys.exit(2)
            library_uuid = libs[0]["library_uuid"]

        # Index legacy assets by normalized UTC-second timestamp.
        by_date: dict[str, list[dict]] = defaultdict(list)
        for asset in db.iter_legacy_assets(library_uuid):
            norm = _normalise_to_utc_second(asset.get("date_taken")) if asset.get("date_taken") else None
            if norm:
                by_date[norm].append(asset)

        # Flickr-only candidate_public photos.
        photos = db.conn.execute(
            "SELECT flickr_id, date_taken, width, height, flickr_title "
            "FROM photos WHERE uuid IS NULL AND privacy_state = 'candidate_public'"
        ).fetchall()

        pairs = []
        for p in photos:
            norm = _normalise_to_utc_second(p["date_taken"]) if p["date_taken"] else None
            candidates = by_date.get(norm, []) if norm else []
            pairs.append((dict(p), candidates))

        rows = order_rows(preview_rows(pairs))

        counts = {"confident": 0, "ambiguous": 0, "no-match": 0}
        seen_photos = {"confident": set(), "ambiguous": set(), "no-match": set()}
        for r in rows:
            seen_photos[r["tier"]].add(r["flickr_id"])
        for tier in counts:
            counts[tier] = len(seen_photos[tier])

        print("Legacy match preview (non-destructive — no writes to photos)")
        print(f"  library_uuid : {library_uuid}")
        print(f"  photos       : {len(photos)} Flickr-only candidate_public")
        print(f"  confident    : {counts['confident']}")
        print(f"  ambiguous    : {counts['ambiguous']}")
        print(f"  no-match     : {counts['no-match']}")

        if args.csv:
            fields = ["tier", "date_norm", "flickr_id", "asset_uuid", "width", "height",
                      "flickr_title", "legacy_title", "legacy_persons"]
            with open(args.csv, "w", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
                w.writeheader()
                for r in rows:
                    w.writerow(r)
            print(f"  CSV written  : {args.csv} ({len(rows)} rows)")
    finally:
        db.close()
```

- [ ] **Step 3b: Register the subparsers**

In `main()`, after the `export` parser block (before `args = parser.parse_args()`):

```python
    # index-legacy
    p_idxleg = sub.add_parser(
        "index-legacy",
        help="Index a legacy (migrated iPhoto/Photos 4) library into curator.db",
    )
    p_idxleg.add_argument("--library", default=None,
                          help="Path to the .photoslibrary bundle (overrides config legacy_library.path)")
    p_idxleg.add_argument("--no-thumbnails", action="store_true",
                          help="Skip copying thumbnails into BP's cache")
    p_idxleg.add_argument("--limit", type=int, default=None, metavar="N",
                          help="Index only the first N assets (non-authoritative: no deletions)")
    p_idxleg.add_argument("--no-cache", action="store_true",
                          help="Read the library in place instead of the local DB cache")
    p_idxleg.add_argument("--refresh-cache", action="store_true",
                          help="Force a rebuild of the local DB cache")

    # match-legacy-preview
    p_mlp = sub.add_parser(
        "match-legacy-preview",
        help="Report likely matches between legacy assets and Flickr-only candidate_public photos (no writes)",
    )
    p_mlp.add_argument("--library-uuid", default=None,
                       help="Which indexed library to match against (default: the most recently indexed)")
    p_mlp.add_argument("--csv", default=None, metavar="PATH",
                       help="Also write the full tiered report to a CSV file")
```

Add the `args` default-guards in the `if not hasattr(args, ...)` block:

```python
    if not hasattr(args, "library"):        args.library = None
    if not hasattr(args, "no_thumbnails"):  args.no_thumbnails = False
    if not hasattr(args, "no_cache"):       args.no_cache = False
    if not hasattr(args, "refresh_cache"):  args.refresh_cache = False
    if not hasattr(args, "library_uuid"):   args.library_uuid = None
    if not hasattr(args, "csv"):            args.csv = None
```

Add to the `dispatch` dict:

```python
        "index-legacy":          cmd_index_legacy,
        "match-legacy-preview":  cmd_match_legacy_preview,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_cli_legacy.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bp tests/test_cli_legacy.py
git commit -m "feat(#162): bp index-legacy and match-legacy-preview subcommands"
```

---

## Task 8: Full verification, docs, and a real smoke run

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-05-30-legacy-library-indexer-162-design.md` (status line)
- (no new source)

- [ ] **Step 1: Run the full suite + lint**

Run: `python -m pytest tests/ -q`
Expected: PASS (all pre-existing tests + the new legacy tests).

Run: `make lint`
Expected: mypy-clean. If new code trips mypy, fix the types properly (no bare `# type: ignore`). Likely touch-ups: annotate dict params as `dict`, the `photosdb_factory: Callable | None`, and the generator return on `iter_legacy_assets`.

- [ ] **Step 2: Apply the migration to the live DB**

Run: `python bp migrate --config config/config.yml`
Expected: `Applying: migrate_026_legacy_index…` then `1 migration(s) applied.` (Re-run → `All migrations already applied.`)

- [ ] **Step 3: Real smoke run against the mounted library (`--limit`, non-authoritative)**

This validates osxphotos attribute access against the real Photos 4 bundle without a full 237k-asset / ~4-min pass.

Run: `python bp index-legacy --library "/Volumes/homes/cdevers/Pictures/Photos Library.photoslibrary" --limit 200 --config config/config.yml`
Expected: prints `Indexed 200 assets from library <uuid> (... non-authoritative (--limit), no deletions).` and populates `legacy_assets`. If osxphotos attribute names differ from the adapter's assumptions (e.g. `path_derivatives`), fix `_build_row`/`_copy_thumbnail` in `poller/legacy_indexer.py` here — the unit tests pin the contract, this step pins reality.

> If osxphotos proves unreliable on this library, the spec's documented fallback is a direct read-only SQL reader behind the same `index_library` interface; that is a separate task, not part of this plan.

- [ ] **Step 4: Cache reuse vs. rebuild expectations**

The unit tests cover the cache *validity logic*; this step proves the live build/reuse/refresh contract end-to-end. Use `-v` so the indexer logs which path it took.

1. **First run builds the cache** (default cache on). After Step 3's run, confirm the cache exists:
   Run: `ls "$(dirname $(python -c 'import yaml;print(yaml.safe_load(open("config/config.yml"))["database"]["path"])'))/legacy-cache"`
   Expected: a `<library_uuid>/database/` directory is present (the freshly built cache).
2. **Second identical run reuses the cache** (source `Photos.sqlite` unchanged → mtime+size+head-hash match):
   Run: `python bp index-legacy --library "/Volumes/homes/cdevers/Pictures/Photos Library.photoslibrary" --limit 200 -v --config config/config.yml`
   Expected: logs indicate the existing cache was reused (no rebuild); run completes noticeably faster than the first (no multi-GB copy).
3. **`--refresh-cache` forces a rebuild** even though the source is unchanged:
   Run: `python bp index-legacy --library "/Volumes/homes/cdevers/Pictures/Photos Library.photoslibrary" --limit 200 --refresh-cache -v --config config/config.yml`
   Expected: logs/timing show the cache directory was rebuilt (the multi-GB copy runs again).

> If the indexer doesn't log enough to distinguish reuse from rebuild, add an `INFO` log line in `ensure_cache`/`build_cache` (`poller/legacy_cache.py`) here — reused vs. rebuilt — so the contract is observable. This is a legitimate part of making the cache layer verifiable, not scope creep.

- [ ] **Step 5: Interruption safety — no authoritative reconcile or thumbnail GC on a killed full run**

Proves the completion-marker gate from Task 5 on the real library. Run a **non-limited** (authoritative) index and interrupt it mid-iteration:

1. Note current state: `python bp stats --config config/config.yml` is unrelated; instead capture the legacy row count and a sample of thumbnail files:
   Run: `python -c "import sys; sys.path.insert(0,'.'); from db.db import Database; import yaml; c=yaml.safe_load(open('config/config.yml')); d=Database(c['database']['path']); rows=d.conn.execute('SELECT library_uuid, COUNT(*) FROM legacy_assets GROUP BY library_uuid').fetchall(); print([dict(r) for r in [ {0:r[0],1:r[1]} for r in rows]])"`
   (Simpler: just record `SELECT COUNT(*) FROM legacy_assets` and `ls` of the `legacy/<uuid>/` thumb dir.)
2. Start a full run, then interrupt it with Ctrl-C (or `kill`) after a few seconds, before it finishes iterating:
   Run: `python bp index-legacy --library "/Volumes/homes/cdevers/Pictures/Photos Library.photoslibrary" --config config/config.yml`  → press **Ctrl-C** partway through.
   Expected: the process exits non-zero with a traceback/KeyboardInterrupt; **no** "reconciled N" summary line is printed (the summary only prints on clean completion).
3. Verify nothing was deleted:
   Run the same count + thumb-dir checks as (1).
   Expected: `legacy_assets` row count is **unchanged or higher** (interrupted run may have upserted some rows, but deleted none), and previously-present thumbnail files still exist. No row that existed before the interrupted run is missing afterward.

> This confirms an interrupted full run behaves like `--limit`: upserts what it saw, reconciles nothing, GCs nothing.

- [ ] **Step 6: Real smoke run of the preview (deterministic output)**

Run: `python bp match-legacy-preview --csv /tmp/legacy-preview-1.csv --config config/config.yml`
Then re-run to a second file: `python bp match-legacy-preview --csv /tmp/legacy-preview-2.csv --config config/config.yml`
Expected: prints tier counts; confident matches look plausible on inspection; and the two CSVs are identical:
Run: `diff /tmp/legacy-preview-1.csv /tmp/legacy-preview-2.csv && echo IDENTICAL`
Expected: no diff output, prints `IDENTICAL` (proves the deterministic ordering produces stable, reviewable diffs).

- [ ] **Step 7: Update README**

Add the two commands to the command reference in `README.md` (follow the existing list style; do not cite a specific test count — the README uses a general coverage statement). Suggested wording:

```markdown
- `bp index-legacy --library <path>` — index a legacy (migrated iPhoto/Photos 4)
  library's metadata + thumbnails into `curator.db`. Path-independent identity;
  local DB cache on by default (`--no-cache`, `--refresh-cache`). `--limit N`
  does a quick non-authoritative sample (no deletions).
- `bp match-legacy-preview [--csv <path>]` — non-destructive report of likely
  matches between legacy assets and Flickr-only `candidate_public` photos.
  Writes nothing to `photos`.
```

- [ ] **Step 8: Mark the spec done**

Edit the spec's `## Release` section to note the plan is written and link this plan file; add `✓ plan written` to the status. (Leave the design body unchanged.)

- [ ] **Step 9: Commit**

```bash
git add README.md docs/superpowers/specs/2026-05-30-legacy-library-indexer-162-design.md
git commit -m "docs(#162): README commands + spec status for legacy indexer"
```

- [ ] **Step 10: Label the issue**

Run: `gh issue edit 162 --add-label has-plan`
Expected: confirms the label was added.

---

## Self-Review (completed by plan author)

**1. Spec coverage** — every spec section maps to a task:
- Data model (two tables, indexes, UNIQUE identity) → Task 1.
- Path canonicalization, JSON-list determinism, thumbnail cache key/path, 16 MiB head-hash → Task 2.
- DB upsert/count/iter/reconcile-delete → Task 3.
- Optional local DB cache + mtime/size/head-hash validity + atomic build + `--refresh-cache` → Task 4 (validity/location unit-tested; real copy exercised in Task 8).
- Indexer (osxphotos read, persons/keywords sorted-unique, bundle-relative POSIX master path, thumbnail copy + status, authoritative reconcile gated on successful completion, thumbnail GC, `--limit` non-authoritative) → Task 5.
- Match tiers (confident/ambiguous/no-match), UTC-second normalization reuse, title-as-tiebreaker (empty-after-trim = missing), deterministic ordering → Task 6.
- CLI `index-legacy` (`--library`/`--no-thumbnails`/`--limit`/`--no-cache`/`--refresh-cache`, flag-over-config) + `match-legacy-preview` (`--csv`, no writes) → Task 7.
- Migration test, DB-method tests, indexer mock test, thumbnail-miss, reconciliation, title-conflict, path-canon, path-independence, cache-invalidation → Tasks 1–6 tests. Release (branch+PR, 1.4.0, has-plan, relates #12) → Task 8 + handoff.

**2. Placeholder scan** — no `TBD`/`TODO`/"handle edge cases"; every code step shows complete code.

**3. Type/name consistency** — the upsert column tuples (`_LEGACY_ASSET_COLS`) match the migration DDL and the row dict keys built in `_build_row` (note: `_build_row` omits `thumbnail_cache_key`/`thumbnail_status`, which the indexer sets explicitly before upsert — verified). `thumbnail_cache_key`/`thumbnail_path` signatures are consistent across `legacy_normalize`, indexer, and tests. `classify_match`/`preview_rows`/`order_rows` names match between module and tests. `_normalise_to_utc_second` is the real symbol in `poller/deduplicator.py`. `index_library` keyword args match every call site (tests + CLI).

**Two cross-task contracts to honor during execution:**
- `index_library` requires the migration (Task 1) already applied to the DB it's given — all indexer/CLI usages run `run_on_conn` or `bp migrate` first.
- The matcher reads `photos.flickr_title`, `date_taken`, `width`, `height`, `uuid`, `privacy_state` — all confirmed present on the live `photos` table (per spec feasibility check). No new photos columns are introduced.

---

## Execution Handoff

Plan complete. Per the standing plan-review-gate, this is presented for your review before any execution begins. Once you approve, the default is subagent-driven execution (a fresh subagent per task, two-stage review between tasks) on branch `feat/legacy-library-indexer-162`, ending with the full suite green, `make lint` clean, a PR, and the 1.4.0 bump on merge.
