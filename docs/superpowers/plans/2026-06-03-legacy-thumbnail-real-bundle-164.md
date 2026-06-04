# Legacy Thumbnail Copy: Resolve Against Real Bundle (#164) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix `bp index-legacy` so thumbnail copying succeeds for Photos 4 libraries opened via the local DB cache, by resolving derivative file paths against the real NAS bundle instead of the cache.

**Architecture:** The indexer currently opens a local bundle cache (DB only, no media) via osxphotos. When osxphotos resolves `path_derivatives` for Photos 4, it constructs a path under the bundle and checks whether the directory exists — the cache has no `resources/proxies/derivatives/`, so it returns `[]`. The fix: when `path_derivatives` is empty and we have a real library path + a `model_id`, compute the derivatives directory ourselves using the same formula as osxphotos (`_get_resource_loc`), then glob it against the real NAS mount. The `model_id` is fetched in a single batch query from the local cache DB before the iteration loop, so no per-photo network calls are needed.

**Tech Stack:** Python, sqlite3, osxphotos (read-only; we replicate its `_get_resource_loc` formula without importing it), pytest

---

## Background: How osxphotos finds Photos 4 derivatives

From `osxphotos/utils.py::_get_resource_loc(model_id)`:

```
hex_id    = hex(model_id)[2:]                              # e.g. "186a0"
folder_id = hex_id.zfill(4)[-4:-2]                        # e.g. "86"
nn_id     = hex_id[:len(hex_id)-4].zfill(2) if len(hex_id) > 4 else "00"  # e.g. "01"
file_id   = hex_id                                         # e.g. "186a0"
path      = <library>/resources/proxies/derivatives/<folder_id>/<nn_id>/<file_id>/
```

`model_id` is `RKVersion.modelId` (INTEGER primary key). It's in the cache DB at
`data/legacy-cache/<uuid>/database/photos.db`.

---

## File Map

| File | Change |
|------|--------|
| `poller/legacy_indexer.py` | Add `_load_model_ids`, `_derivatives_dir_photos4`; extend `_copy_thumbnail`; pass map in `index_library` |
| `tests/test_legacy_indexer.py` | Add tests for photos4 fallback path; extend `FakePhoto` with `model_id` if needed |

No schema changes. No new files.

---

### Task 1: Add `_load_model_ids` helper

**Files:**
- Modify: `poller/legacy_indexer.py` (after the `log = ...` line, around line 26)
- Test: `tests/test_legacy_indexer.py`

This fetches `uuid → model_id` for every asset in a Photos 4 DB in one query.

- [ ] **Step 1: Write failing test**

Add to `tests/test_legacy_indexer.py` after the existing imports:

```python
import sqlite3
import tempfile

def _make_photos4_db(tmp_path, rows: list[tuple[str, int]]) -> str:
    """Minimal photos.db with just enough RKVersion rows for model_id lookup."""
    db_path = str(tmp_path / "photos.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE RKVersion (modelId INTEGER PRIMARY KEY, uuid VARCHAR)")
    conn.executemany("INSERT INTO RKVersion VALUES (?, ?)", [(mid, uid) for uid, mid in rows])
    conn.commit()
    conn.close()
    return db_path


def test_load_model_ids_returns_uuid_to_modelid(tmp_path):
    db = _make_photos4_db(tmp_path, [("uuid-A", 10), ("uuid-B", 255)])
    result = legacy_indexer._load_model_ids(db)
    assert result == {"uuid-A": 10, "uuid-B": 255}


def test_load_model_ids_returns_empty_on_bad_path():
    result = legacy_indexer._load_model_ids("/nonexistent/photos.db")
    assert result == {}
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain"
python -m pytest tests/test_legacy_indexer.py::test_load_model_ids_returns_uuid_to_modelid tests/test_legacy_indexer.py::test_load_model_ids_returns_empty_on_bad_path -v
```

Expected: `AttributeError: module 'legacy_indexer' has no attribute '_load_model_ids'`

- [ ] **Step 3: Implement `_load_model_ids`**

In `poller/legacy_indexer.py`, add after `log = logging.getLogger(...)`:

```python
def _load_model_ids(source_db_path: str) -> dict[str, int]:
    """Map asset UUID → RKVersion.modelId. Single query; safe on missing/corrupt DB."""
    import sqlite3 as _sqlite3

    try:
        conn = _sqlite3.connect(f"file:{source_db_path}?immutable=1", uri=True)
    except _sqlite3.Error:
        return {}
    try:
        rows = conn.execute(
            "SELECT uuid, modelId FROM RKVersion "
            "WHERE uuid IS NOT NULL AND modelId IS NOT NULL"
        ).fetchall()
        return {uuid: int(model_id) for uuid, model_id in rows}
    except _sqlite3.Error:
        return {}
    finally:
        conn.close()
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m pytest tests/test_legacy_indexer.py::test_load_model_ids_returns_uuid_to_modelid tests/test_legacy_indexer.py::test_load_model_ids_returns_empty_on_bad_path -v
```

Expected: PASS (both)

---

### Task 2: Add `_derivatives_dir_photos4` helper

**Files:**
- Modify: `poller/legacy_indexer.py`
- Test: `tests/test_legacy_indexer.py`

Replicates `osxphotos.utils._get_resource_loc` without importing osxphotos.

- [ ] **Step 1: Write failing tests**

Add to `tests/test_legacy_indexer.py`:

```python
def test_derivatives_dir_photos4_small_model_id():
    # model_id=1 → hex="1" → folder_id="00", nn_id="00", file_id="1"
    result = legacy_indexer._derivatives_dir_photos4(1, "/fake/lib")
    assert str(result) == "/fake/lib/resources/proxies/derivatives/00/00/1"


def test_derivatives_dir_photos4_typical_model_id():
    # model_id=100000 → hex="186a0" → folder_id="86", nn_id="01", file_id="186a0"
    result = legacy_indexer._derivatives_dir_photos4(100000, "/fake/lib")
    assert str(result) == "/fake/lib/resources/proxies/derivatives/86/01/186a0"


def test_derivatives_dir_photos4_returns_path_object():
    result = legacy_indexer._derivatives_dir_photos4(42, "/fake/lib")
    assert isinstance(result, Path)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_legacy_indexer.py::test_derivatives_dir_photos4_small_model_id tests/test_legacy_indexer.py::test_derivatives_dir_photos4_typical_model_id tests/test_legacy_indexer.py::test_derivatives_dir_photos4_returns_path_object -v
```

Expected: `AttributeError: module 'legacy_indexer' has no attribute '_derivatives_dir_photos4'`

- [ ] **Step 3: Implement `_derivatives_dir_photos4`**

In `poller/legacy_indexer.py`, add after `_load_model_ids`:

```python
def _derivatives_dir_photos4(model_id: int, library_path: str | Path) -> Path:
    """Photos 4 derivative directory for a given RKVersion.modelId.

    Replicates osxphotos.utils._get_resource_loc without importing it.
    Path = <library>/resources/proxies/derivatives/<folder_id>/<nn_id>/<file_id>
    """
    hex_id = hex(model_id)[2:]
    folder_id = hex_id.zfill(4)[-4:-2]
    nn_id = hex_id[: len(hex_id) - 4].zfill(2) if len(hex_id) > 4 else "00"
    return (
        Path(library_path)
        / "resources" / "proxies" / "derivatives"
        / folder_id / nn_id / hex_id
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_legacy_indexer.py::test_derivatives_dir_photos4_small_model_id tests/test_legacy_indexer.py::test_derivatives_dir_photos4_typical_model_id tests/test_legacy_indexer.py::test_derivatives_dir_photos4_returns_path_object -v
```

Expected: PASS (all three)

---

### Task 3: Extend `_copy_thumbnail` with Photos 4 fallback

**Files:**
- Modify: `poller/legacy_indexer.py:84-100`
- Test: `tests/test_legacy_indexer.py`

When `path_derivatives` is empty and `(real_library_path, model_id)` are provided, fall back to the computed derivatives directory.

- [ ] **Step 1: Write failing tests**

Add to `tests/test_legacy_indexer.py`:

```python
def test_copy_thumbnail_photos4_fallback_uses_real_bundle(tmp_path):
    """When path_derivatives is empty, falls back to _derivatives_dir_photos4."""
    # model_id=1 → dir: <real_lib>/resources/proxies/derivatives/00/00/1/
    real_lib = tmp_path / "Real.photoslibrary"
    deriv_dir = real_lib / "resources" / "proxies" / "derivatives" / "00" / "00" / "1"
    deriv_dir.mkdir(parents=True)
    (deriv_dir / "preview.jpg").write_bytes(b"BIGPREVIEW" * 100)
    (deriv_dir / "thumb.jpg").write_bytes(b"SMALL")

    db = _db(tmp_path)
    # FakePhoto with no path_derivatives
    photos = [FakePhoto("asset-uuid-1", derivatives=[])]
    stats = legacy_indexer.index_library(
        str(real_lib),
        db,
        curator_db_path=str(tmp_path / "curator.db"),
        thumb_root=tmp_path / "thumbs",
        copy_thumbnails=True,
        use_cache=False,
        photosdb_factory=_factory(photos),
        model_id_override={"asset-uuid-1": 1},
    )
    assert stats["thumb_ok"] == 1
    assert stats["thumb_missing"] == 0


def test_copy_thumbnail_photos4_fallback_picks_largest_file(tmp_path):
    """When multiple files exist in the derivatives dir, picks largest (best quality)."""
    real_lib = tmp_path / "Real.photoslibrary"
    deriv_dir = real_lib / "resources" / "proxies" / "derivatives" / "00" / "00" / "1"
    deriv_dir.mkdir(parents=True)
    small = deriv_dir / "thumb.jpg"
    small.write_bytes(b"X" * 10)
    big = deriv_dir / "preview.jpg"
    big.write_bytes(b"X" * 500)

    db = _db(tmp_path)
    photos = [FakePhoto("uuid-1", derivatives=[])]
    legacy_indexer.index_library(
        str(real_lib),
        db,
        curator_db_path=str(tmp_path / "curator.db"),
        thumb_root=tmp_path / "thumbs",
        copy_thumbnails=True,
        use_cache=False,
        photosdb_factory=_factory(photos),
        model_id_override={"uuid-1": 1},
    )
    from legacy_normalize import thumbnail_cache_key, thumbnail_path
    key = thumbnail_cache_key("LIB-UUID", "uuid-1")
    dest = thumbnail_path(tmp_path / "thumbs", "LIB-UUID", key)
    assert dest.stat().st_size == 500  # largest file was copied


def test_copy_thumbnail_photos4_fallback_missing_when_dir_absent(tmp_path):
    """Returns 'missing' when real bundle derivatives dir does not exist."""
    real_lib = tmp_path / "NotMounted.photoslibrary"
    # No derivatives dir created → NAS not available

    db = _db(tmp_path)
    photos = [FakePhoto("uuid-1", derivatives=[])]
    stats = legacy_indexer.index_library(
        str(real_lib),
        db,
        curator_db_path=str(tmp_path / "curator.db"),
        thumb_root=tmp_path / "thumbs",
        copy_thumbnails=True,
        use_cache=False,
        photosdb_factory=_factory(photos),
        model_id_override={"uuid-1": 1},
    )
    assert stats["thumb_missing"] == 1


def test_copy_thumbnail_photos4_fallback_missing_when_no_model_id(tmp_path):
    """Returns 'missing' when model_id not available (UUID not in lookup)."""
    real_lib = tmp_path / "Real.photoslibrary"
    deriv_dir = real_lib / "resources" / "proxies" / "derivatives" / "00" / "00" / "1"
    deriv_dir.mkdir(parents=True)
    (deriv_dir / "preview.jpg").write_bytes(b"DATA")

    db = _db(tmp_path)
    photos = [FakePhoto("uuid-1", derivatives=[])]
    stats = legacy_indexer.index_library(
        str(real_lib),
        db,
        curator_db_path=str(tmp_path / "curator.db"),
        thumb_root=tmp_path / "thumbs",
        copy_thumbnails=True,
        use_cache=False,
        photosdb_factory=_factory(photos),
        # No model_id_override → uuid-1 won't be in model_id_map
    )
    assert stats["thumb_missing"] == 1
```

Note: `model_id_override` is a new test-only parameter added to `index_library` for injecting the map without a real DB. Implement it in the next step.

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_legacy_indexer.py::test_copy_thumbnail_photos4_fallback_uses_real_bundle tests/test_legacy_indexer.py::test_copy_thumbnail_photos4_fallback_picks_largest_file tests/test_legacy_indexer.py::test_copy_thumbnail_photos4_fallback_missing_when_dir_absent tests/test_legacy_indexer.py::test_copy_thumbnail_photos4_fallback_missing_when_no_model_id -v
```

Expected: all fail — `model_id_override` parameter not accepted, fallback logic absent.

- [ ] **Step 3: Implement the fallback in `_copy_thumbnail` and wire it in `index_library`**

Replace `_copy_thumbnail` in `poller/legacy_indexer.py` with:

```python
def _copy_thumbnail(
    photo,
    library_uuid: str,
    thumb_root: Path,
    *,
    real_library_path: str | None = None,
    model_id: int | None = None,
) -> str:
    """Copy the best available derivative into the cache. Returns ok/missing/error.

    Primary: photo.path_derivatives (populated when osxphotos opens the real bundle).
    Fallback: compute Photos 4 derivatives dir from model_id + real_library_path.
    """
    derivs = getattr(photo, "path_derivatives", None) or []
    src = next((d for d in derivs if d and Path(d).exists()), None)

    if src is None and real_library_path is not None and model_id is not None:
        deriv_dir = _derivatives_dir_photos4(model_id, real_library_path)
        if deriv_dir.is_dir():
            files = sorted(
                (f for f in deriv_dir.glob("*") if f.is_file()),
                key=lambda f: f.stat().st_size,
                reverse=True,
            )
            src = str(files[0]) if files else None

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
```

- [ ] **Step 4: Run tests to verify they pass (or that the only failure is the missing `model_id_override` wiring)**

```bash
python -m pytest tests/test_legacy_indexer.py::test_copy_thumbnail_photos4_fallback_uses_real_bundle tests/test_legacy_indexer.py::test_copy_thumbnail_photos4_fallback_picks_largest_file tests/test_legacy_indexer.py::test_copy_thumbnail_photos4_fallback_missing_when_dir_absent tests/test_legacy_indexer.py::test_copy_thumbnail_photos4_fallback_missing_when_no_model_id -v
```

These will still fail if `index_library` doesn't pass `model_id` and `real_library_path` through. Proceed to Task 4 to wire it up, then run the full suite.

---

### Task 4: Wire model_id map into `index_library`

**Files:**
- Modify: `poller/legacy_indexer.py:154-249` (the `index_library` function)

Build the `uuid → model_id` map from the local cache DB and pass it through to `_copy_thumbnail`.

- [ ] **Step 1: Modify `index_library` signature and body**

The signature gains one test-only parameter (`model_id_override`). The body builds a model_id map from the cache DB, then passes `real_library_path` and per-photo `model_id` to `_copy_thumbnail`.

Replace `index_library` in `poller/legacy_indexer.py` with:

```python
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
    model_id_override: dict[str, int] | None = None,
) -> dict:
    """Index the legacy library into curator.db. Returns a stats dict."""
    thumb_root = Path(thumb_root)
    photosdb = _open_photosdb(
        library_path,
        db,
        None,
        curator_db_path,
        use_cache,
        refresh_cache,
        photosdb_factory,
    )
    library_uuid = _resolve_library_uuid(photosdb, library_path)
    schema_version = getattr(photosdb, "db_version", None)

    # Ensure the library row exists before inserting assets (FK requires it).
    db.set_legacy_library({"library_uuid": library_uuid, "source_path_last_seen": library_path})

    # Build uuid→model_id map from the local cache DB so _copy_thumbnail can
    # resolve Photos 4 derivative paths without querying the NAS per asset.
    if model_id_override is not None:
        model_id_map: dict[str, int] = model_id_override
    elif copy_thumbnails and use_cache:
        from legacy_cache import cache_dir as _cache_dir, locate_source_db as _lsd
        cached_db = _lsd(str(_cache_dir(curator_db_path, library_uuid)))
        model_id_map = _load_model_ids(cached_db) if cached_db else {}
    else:
        model_id_map = {}

    seen: set[str] = set()
    indexed = thumb_ok = thumb_missing = thumb_error = 0

    for photo in photosdb.photos():
        if limit is not None and indexed >= limit:
            break
        row = _build_row(photo, library_uuid, library_path)
        if copy_thumbnails:
            status = _copy_thumbnail(
                photo,
                library_uuid,
                thumb_root,
                real_library_path=library_path if model_id_map else None,
                model_id=model_id_map.get(photo.uuid),
            )
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
        if indexed % PROGRESS_INTERVAL == 0:
            log.info("indexed %d assets so far...", indexed)

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

    from legacy_cache import source_db_stats

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

- [ ] **Step 2: Run the new thumbnail fallback tests**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain"
python -m pytest tests/test_legacy_indexer.py::test_copy_thumbnail_photos4_fallback_uses_real_bundle tests/test_legacy_indexer.py::test_copy_thumbnail_photos4_fallback_picks_largest_file tests/test_legacy_indexer.py::test_copy_thumbnail_photos4_fallback_missing_when_dir_absent tests/test_legacy_indexer.py::test_copy_thumbnail_photos4_fallback_missing_when_no_model_id -v
```

Expected: PASS (all four)

- [ ] **Step 3: Run the full test suite**

```bash
python -m pytest tests/ -q
```

Expected: all tests pass. Pay attention to existing `test_legacy_indexer.py` tests — they must continue to pass unchanged (they use `use_cache=False` and no `model_id_override`, so `model_id_map` is `{}` and behaviour is identical to before).

---

### Task 5: Commit

- [ ] **Step 1: Review the diff**

```bash
git diff --stat
```

Expected: only `poller/legacy_indexer.py` and `tests/test_legacy_indexer.py` changed.

- [ ] **Step 2: Stage and commit**

```bash
git checkout -b fix/legacy-thumbnail-real-bundle-164
git add poller/legacy_indexer.py tests/test_legacy_indexer.py
git commit -m "$(cat <<'EOF'
fix(#164): resolve Photos 4 thumbnail derivatives against real bundle

When use_cache=True, osxphotos opens the local DB cache which has no
resources/proxies/derivatives/ tree, so path_derivatives returns [].

Fix: batch-load uuid→model_id from the cached photos.db once per run,
then compute the derivatives directory via the same formula osxphotos
uses (_get_resource_loc) and glob for files against the real library
path. Falls back to missing if the NAS is unmounted or model_id is
unknown.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Manual verification + docs + push

- [ ] **Step 1: Verify against the real library with `--limit`**

With the NAS mounted and `legacy_library.path` set in `config/config.yml`:

```bash
bp index-legacy --limit 50
```

Expected output includes `thumb_ok > 0` (e.g. `"thumb_ok": 42, "thumb_missing": 8`). Any missing is fine — some assets genuinely have no derivatives. Zero `thumb_ok` means the fallback isn't firing.

If `thumb_ok` is 0, investigate: check that
`<NAS>/resources/proxies/derivatives/` exists and is readable:

```bash
ls "/Volumes/homes/cdevers/Pictures/Photos Library.photoslibrary/resources/proxies/derivatives/" | head -5
```

- [ ] **Step 2: Update CLAUDE.md / docs**

No spec file was created for #164 (the GitHub issue itself is the spec). No docs update required.

- [ ] **Step 3: Push branch and open PR**

```bash
git push -u origin fix/legacy-thumbnail-real-bundle-164
gh pr create \
  --title "fix(#164): resolve Photos 4 thumbnails against real bundle" \
  --body "$(cat <<'EOF'
## Summary

- `index_library` now batch-loads `uuid → model_id` from the local cache DB before the iteration loop (one sqlite3 query, no NAS traffic)
- `_copy_thumbnail` falls back to computing the Photos 4 derivatives directory path via the `_get_resource_loc` formula when `path_derivatives` is empty
- Gracefully handles NAS-unmounted case: returns `missing` without error
- Existing behaviour unchanged when `use_cache=False` or `copy_thumbnails=False`

## Test plan
- [ ] New unit tests for `_load_model_ids`, `_derivatives_dir_photos4`, and the fallback path in `_copy_thumbnail`
- [ ] Run `bp index-legacy --limit 50` with NAS mounted and confirm `thumb_ok > 0`
- [ ] Full test suite green

Closes #164

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 4: Post retrospective comment on GH#164**

```bash
gh issue comment 164 --repo cdevers/Blue-Pearmain --body "$(cat <<'EOF'
Implemented in PR #<number>. 

Size estimate: S ✓ (3 new helpers, ~40 LOC production, ~80 LOC test)
Files changed: poller/legacy_indexer.py, tests/test_legacy_indexer.py
Plan tasks: 6 / 6 complete

The fix stays entirely within the indexer — no schema changes, no new files. The `model_id_override` test parameter avoids coupling tests to a real DB file.
EOF
)"
```

---

## Self-Review Notes

**Spec coverage:**
- ✓ Resolve derivative paths against real bundle → Task 3+4
- ✓ Confirm osxphotos `path_derivatives` for Photos 4 → documented in Architecture (empty from cache; confirmed by reading osxphotos source)
- ✓ Fall back to `Thumbnails/` construction → implemented as `resources/proxies/derivatives/` (the actual Photos 4 path; "Thumbnails/" is iPhoto's older layout)
- ✓ Re-run `--limit` pass and confirm `thumb_ok > 0` → Task 6 step 1

**No placeholders present.**

**Type consistency:** `_derivatives_dir_photos4` returns `Path`; tests import `Path` at top; `_load_model_ids` returns `dict[str, int]`; `model_id_map.get(photo.uuid)` returns `int | None`; `_copy_thumbnail` accepts `model_id: int | None`. All consistent.
