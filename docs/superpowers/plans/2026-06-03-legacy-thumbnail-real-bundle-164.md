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
| `poller/legacy_indexer.py` | Add `_load_model_ids`, `_derivatives_dir_photos4`; extend `_copy_thumbnail`; two targeted edits inside `index_library` |
| `tests/test_legacy_indexer.py` | New tests for helpers, fallback path, fast-path regression, and integration wiring |

No schema changes. No new files.

---

### Task 1: Add `_load_model_ids` helper

**Files:**
- Modify: `poller/legacy_indexer.py` (after the `log = ...` line, around line 26)
- Test: `tests/test_legacy_indexer.py`

This fetches `uuid → model_id` for every asset in a Photos 4 DB in one query.

- [ ] **Step 1: Write failing tests**

Add to `tests/test_legacy_indexer.py` after the existing imports (also add `import sqlite3`):

```python
import sqlite3


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

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain"
python -m pytest tests/test_legacy_indexer.py::test_load_model_ids_returns_uuid_to_modelid tests/test_legacy_indexer.py::test_load_model_ids_returns_empty_on_bad_path -v
```

Expected: `AttributeError: module 'legacy_indexer' has no attribute '_load_model_ids'`

- [ ] **Step 3: Implement `_load_model_ids`**

In `poller/legacy_indexer.py`, add after `log = logging.getLogger(...)`:

```python
def _load_model_ids(source_db_path: str) -> dict[str, int]:
    """Map asset UUID → RKVersion.modelId. Single query; safe on missing/corrupt DB.

    Duplicate UUIDs in RKVersion are unexpected; if they occur, the dict
    comprehension keeps the last-seen modelId (last-row-wins).
    """
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

- [ ] **Step 4: Run tests to verify they pass**

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

When `path_derivatives` is empty and `(real_library_path, model_id)` are provided, fall back
to the computed derivatives directory and pick the largest file (best-effort heuristic: larger
files are higher-quality previews in Photos 4; mirrors osxphotos' own sort order but is not a
Photos API guarantee).

- [ ] **Step 1: Write failing tests**

Add to `tests/test_legacy_indexer.py`:

```python
def test_copy_thumbnail_photos4_fallback_uses_real_bundle(tmp_path):
    """When path_derivatives is empty, falls back to _derivatives_dir_photos4."""
    # model_id=1 → dir: <real_lib>/resources/proxies/derivatives/00/00/1/
    real_lib = tmp_path / "Real.photoslibrary"
    deriv_dir = real_lib / "resources" / "proxies" / "derivatives" / "00" / "00" / "1"
    deriv_dir.mkdir(parents=True)
    (deriv_dir / "preview.jpg").write_bytes(b"X" * 200)

    photo = FakePhoto("uuid-1", derivatives=[])
    status = legacy_indexer._copy_thumbnail(
        photo, "LIB-UUID", tmp_path / "thumbs",
        real_library_path=str(real_lib),
        model_id=1,
    )
    assert status == "ok"
    key = thumbnail_cache_key("LIB-UUID", "uuid-1")
    assert thumbnail_path(tmp_path / "thumbs", "LIB-UUID", key).exists()


def test_copy_thumbnail_photos4_fallback_picks_largest_file(tmp_path):
    """Best-effort heuristic: largest file in derivatives dir is copied (highest quality)."""
    real_lib = tmp_path / "Real.photoslibrary"
    deriv_dir = real_lib / "resources" / "proxies" / "derivatives" / "00" / "00" / "1"
    deriv_dir.mkdir(parents=True)
    (deriv_dir / "thumb.jpg").write_bytes(b"X" * 10)
    (deriv_dir / "preview.jpg").write_bytes(b"X" * 500)

    photo = FakePhoto("uuid-1", derivatives=[])
    legacy_indexer._copy_thumbnail(
        photo, "LIB-UUID", tmp_path / "thumbs",
        real_library_path=str(real_lib),
        model_id=1,
    )
    key = thumbnail_cache_key("LIB-UUID", "uuid-1")
    dest = thumbnail_path(tmp_path / "thumbs", "LIB-UUID", key)
    assert dest.stat().st_size == 500  # largest file was copied


def test_copy_thumbnail_photos4_fallback_missing_when_dir_absent(tmp_path):
    """Returns 'missing' when real bundle derivatives dir does not exist (NAS unmounted)."""
    photo = FakePhoto("uuid-1", derivatives=[])
    status = legacy_indexer._copy_thumbnail(
        photo, "LIB-UUID", tmp_path / "thumbs",
        real_library_path=str(tmp_path / "NotMounted.photoslibrary"),
        model_id=1,
    )
    assert status == "missing"


def test_copy_thumbnail_photos4_fallback_missing_when_no_model_id(tmp_path):
    """Returns 'missing' when model_id is None (UUID not in lookup map)."""
    real_lib = tmp_path / "Real.photoslibrary"
    deriv_dir = real_lib / "resources" / "proxies" / "derivatives" / "00" / "00" / "1"
    deriv_dir.mkdir(parents=True)
    (deriv_dir / "preview.jpg").write_bytes(b"DATA")

    photo = FakePhoto("uuid-1", derivatives=[])
    status = legacy_indexer._copy_thumbnail(
        photo, "LIB-UUID", tmp_path / "thumbs",
        real_library_path=str(real_lib),
        model_id=None,  # UUID not found in map
    )
    assert status == "missing"


def test_copy_thumbnail_fast_path_wins_when_derivatives_present(tmp_path):
    """Regression: when path_derivatives is already populated, it is used without fallback."""
    # Derivative provided by osxphotos (real-bundle open or --no-cache)
    deriv = tmp_path / "existing_deriv.jpg"
    deriv.write_bytes(b"FAST_PATH")

    # Fallback dir also exists, but should NOT be touched
    real_lib = tmp_path / "Real.photoslibrary"
    fallback_dir = real_lib / "resources" / "proxies" / "derivatives" / "00" / "00" / "1"
    fallback_dir.mkdir(parents=True)
    (fallback_dir / "fallback.jpg").write_bytes(b"FALLBACK_SHOULD_NOT_BE_USED" * 10)

    photo = FakePhoto("uuid-1", derivatives=[str(deriv)])
    legacy_indexer._copy_thumbnail(
        photo, "LIB-UUID", tmp_path / "thumbs",
        real_library_path=str(real_lib),
        model_id=1,
    )
    key = thumbnail_cache_key("LIB-UUID", "uuid-1")
    dest = thumbnail_path(tmp_path / "thumbs", "LIB-UUID", key)
    assert dest.read_bytes() == b"FAST_PATH"  # fast path, not fallback


def test_copy_thumbnail_photos4_fallback_handles_stat_failure(tmp_path):
    """stat() failure on one file in the derivatives dir is skipped; others still work."""
    from unittest.mock import patch

    real_lib = tmp_path / "Real.photoslibrary"
    deriv_dir = real_lib / "resources" / "proxies" / "derivatives" / "00" / "00" / "1"
    deriv_dir.mkdir(parents=True)
    good = deriv_dir / "good.jpg"
    good.write_bytes(b"GOOD" * 100)
    bad = deriv_dir / "bad.jpg"
    bad.write_bytes(b"BAD")

    original_stat = Path.stat

    def stat_raises_for_bad(self, **kwargs):
        if self.name == "bad.jpg":
            raise OSError("simulated stat failure")
        return original_stat(self, **kwargs)

    photo = FakePhoto("uuid-1", derivatives=[])
    with patch.object(Path, "stat", stat_raises_for_bad):
        status = legacy_indexer._copy_thumbnail(
            photo, "LIB-UUID", tmp_path / "thumbs",
            real_library_path=str(real_lib),
            model_id=1,
        )
    assert status == "ok"  # bad.jpg skipped; good.jpg copied
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest \
  tests/test_legacy_indexer.py::test_copy_thumbnail_photos4_fallback_uses_real_bundle \
  tests/test_legacy_indexer.py::test_copy_thumbnail_photos4_fallback_picks_largest_file \
  tests/test_legacy_indexer.py::test_copy_thumbnail_photos4_fallback_missing_when_dir_absent \
  tests/test_legacy_indexer.py::test_copy_thumbnail_photos4_fallback_missing_when_no_model_id \
  tests/test_legacy_indexer.py::test_copy_thumbnail_fast_path_wins_when_derivatives_present \
  tests/test_legacy_indexer.py::test_copy_thumbnail_photos4_fallback_handles_stat_failure \
  -v
```

Expected: `TypeError: _copy_thumbnail() got an unexpected keyword argument 'real_library_path'`

- [ ] **Step 3: Implement the fallback in `_copy_thumbnail`**

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
    Fallback sort is largest-first: a best-effort heuristic matching osxphotos'
    own sort order (larger files are higher-quality previews), not a Photos API guarantee.
    """
    derivs = getattr(photo, "path_derivatives", None) or []
    src = next((d for d in derivs if d and Path(d).exists()), None)

    if src is None and real_library_path is not None and model_id is not None:
        deriv_dir = _derivatives_dir_photos4(model_id, real_library_path)
        if deriv_dir.is_dir():
            # Collect (size, path) pairs, skipping any entry whose stat() fails
            # (e.g. a broken symlink or a file deleted between glob and stat).
            # Largest-first: best-effort heuristic, not a Photos API guarantee.
            candidates: list[tuple[int, Path]] = []
            for f in deriv_dir.glob("*"):
                try:
                    if f.is_file():
                        candidates.append((f.stat().st_size, f))
                except OSError:
                    pass
            candidates.sort(reverse=True)
            src = str(candidates[0][1]) if candidates else None
            if src is not None:
                log.debug("fallback thumbnail copy for %s from %s", photo.uuid, src)

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

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest \
  tests/test_legacy_indexer.py::test_copy_thumbnail_photos4_fallback_uses_real_bundle \
  tests/test_legacy_indexer.py::test_copy_thumbnail_photos4_fallback_picks_largest_file \
  tests/test_legacy_indexer.py::test_copy_thumbnail_photos4_fallback_missing_when_dir_absent \
  tests/test_legacy_indexer.py::test_copy_thumbnail_photos4_fallback_missing_when_no_model_id \
  tests/test_legacy_indexer.py::test_copy_thumbnail_fast_path_wins_when_derivatives_present \
  tests/test_legacy_indexer.py::test_copy_thumbnail_photos4_fallback_handles_stat_failure \
  -v
```

Expected: PASS (all six)

---

### Task 4: Wire model_id map into `index_library` — minimal edits

**Files:**
- Modify: `poller/legacy_indexer.py:154-249` (two targeted additions)
- Test: `tests/test_legacy_indexer.py`

Two changes only — nothing else in `index_library` moves:

**Change A** — add after `db.set_legacy_library(...)` (around line 181), before `seen: set[str] = set()`:

```python
    # Batch-load uuid→model_id from the local cache DB so _copy_thumbnail can
    # compute Photos 4 derivative paths without per-asset NAS queries.
    # Always reads from the cache (not the NAS) for speed; falls back to an empty
    # map if the cache DB hasn't been built yet.
    model_id_map: dict[str, int] = {}
    if copy_thumbnails:
        from legacy_cache import cache_dir as _cache_dir
        cached_db = locate_source_db(str(_cache_dir(curator_db_path, library_uuid)))
        if cached_db:
            model_id_map = _load_model_ids(cached_db)
```

**Change B** — inside the `for photo in photosdb.photos()` loop, replace the existing `_copy_thumbnail` call:

```python
        # Before:
        status = _copy_thumbnail(photo, library_uuid, thumb_root)

        # After:
        status = _copy_thumbnail(
            photo,
            library_uuid,
            thumb_root,
            real_library_path=library_path if model_id_map else None,
            model_id=model_id_map.get(photo.uuid),
        )
```

No other lines in `index_library` change.

- [ ] **Step 1: Write failing integration test**

This test creates a minimal fake cache DB (the `photos.db` file `_load_model_ids` reads from)
at the path `index_library` will compute from `curator_db_path` and `library_uuid`.
No monkeypatching needed — it exercises the real code path end-to-end.

**Note:** This test intentionally encodes the current cache-path contract
(`cache_dir(curator_db_path, library_uuid) / "database" / "photos.db"`). If the
cache layout changes, update this test along with the cache module.

Add to `tests/test_legacy_indexer.py`:

```python
def test_index_library_loads_model_id_map_from_cache_db(tmp_path):
    """index_library wires model_id_map → _copy_thumbnail when the cache DB exists.

    Exercises the current cache-path contract. Update if cache layout changes.
    """
    # FakePhotosDB uses library_uuid = "LIB-UUID" (from _uuid attribute).
    library_uuid = "LIB-UUID"
    curator_db = str(tmp_path / "curator.db")

    # Create minimal cache DB at the path index_library will look up.
    from legacy_cache import cache_dir as _cache_dir
    cache_db_dir = _cache_dir(curator_db, library_uuid) / "database"
    cache_db_dir.mkdir(parents=True)
    cache_conn = sqlite3.connect(str(cache_db_dir / "photos.db"))
    cache_conn.execute("CREATE TABLE RKVersion (modelId INTEGER PRIMARY KEY, uuid VARCHAR)")
    cache_conn.execute("INSERT INTO RKVersion VALUES (1, 'uuid-A')")
    cache_conn.commit()
    cache_conn.close()

    # Create derivative file at the path _derivatives_dir_photos4(1, real_lib) resolves to.
    real_lib = tmp_path / "Real.photoslibrary"
    deriv_dir = real_lib / "resources" / "proxies" / "derivatives" / "00" / "00" / "1"
    deriv_dir.mkdir(parents=True)
    (deriv_dir / "preview.jpg").write_bytes(b"PREVIEW_DATA")

    db = _db(tmp_path)
    photos = [FakePhoto("uuid-A", derivatives=[])]
    stats = legacy_indexer.index_library(
        str(real_lib),
        db,
        curator_db_path=curator_db,
        thumb_root=tmp_path / "thumbs",
        copy_thumbnails=True,
        use_cache=False,
        photosdb_factory=_factory(photos),
    )
    assert stats["thumb_ok"] == 1
    assert stats["thumb_missing"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_legacy_indexer.py::test_index_library_loads_model_id_map_from_cache_db -v
```

Expected: FAIL — `_copy_thumbnail` doesn't receive `model_id` yet, so derivative dir isn't checked.

- [ ] **Step 3: Apply the two targeted edits to `index_library`**

Open `poller/legacy_indexer.py`. Find the block that starts with:

```python
    db.set_legacy_library({"library_uuid": library_uuid, "source_path_last_seen": library_path})

    seen: set[str] = set()
```

Change it to:

```python
    db.set_legacy_library({"library_uuid": library_uuid, "source_path_last_seen": library_path})

    # Batch-load uuid→model_id from the local cache DB so _copy_thumbnail can
    # compute Photos 4 derivative paths without per-asset NAS queries.
    # Always reads from the cache (not the NAS) for speed; falls back to an empty
    # map if the cache DB hasn't been built yet.
    model_id_map: dict[str, int] = {}
    if copy_thumbnails:
        from legacy_cache import cache_dir as _cache_dir
        cached_db = locate_source_db(str(_cache_dir(curator_db_path, library_uuid)))
        if cached_db:
            model_id_map = _load_model_ids(cached_db)

    seen: set[str] = set()
```

Then find the existing `_copy_thumbnail` call inside the loop:

```python
        if copy_thumbnails:
            status = _copy_thumbnail(photo, library_uuid, thumb_root)
```

Change it to:

```python
        if copy_thumbnails:
            status = _copy_thumbnail(
                photo,
                library_uuid,
                thumb_root,
                real_library_path=library_path if model_id_map else None,
                model_id=model_id_map.get(photo.uuid),
            )
```

- [ ] **Step 4: Run the full test suite**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain"
python -m pytest tests/ -q
```

Expected: all tests pass. Pay attention to existing `test_legacy_indexer.py` tests — they use `copy_thumbnails=False` or no cache DB, so `model_id_map` stays `{}` and behaviour is unchanged.

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
unknown. Largest-file-first selection matches osxphotos' sort order;
documented as a best-effort heuristic in code and tests.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Manual verification + push

- [ ] **Step 1: Verify against the real library with `--limit`**

With the NAS mounted and `legacy_library.path` set in `config/config.yml`:

```bash
bp index-legacy --limit 50 --verbose 2>&1 | tee /tmp/index_legacy_test.log
```

Two things to confirm:

**a) The fallback code path actually ran.** The `log.debug` line in `_copy_thumbnail` fires
when a fallback copy succeeds. `--verbose` enables DEBUG level; without it the line is
suppressed and the grep will always come up empty:

```bash
grep "fallback thumbnail copy" /tmp/index_legacy_test.log | head -5
```

Expected: at least one line like:
`DEBUG blue-pearmain.legacy-indexer fallback thumbnail copy for <uuid> from <path>`

If nothing appears but `thumb_ok > 0`, re-run with `--verbose` — a copy may have succeeded
via the fast path (`path_derivatives` was non-empty) rather than the fallback.

**b) The count is material.** Zero `thumb_ok` with a non-empty library means something is wrong.
Check that `resources/proxies/derivatives/` exists under the NAS mount and that the cache DB
has `RKVersion` rows.

- [ ] **Step 2: Push branch and open PR**

```bash
git push -u origin fix/legacy-thumbnail-real-bundle-164
gh pr create \
  --title "fix(#164): resolve Photos 4 thumbnails against real bundle" \
  --body "$(cat <<'EOF'
## Summary

- `_load_model_ids` batch-queries `RKVersion.modelId` from the local cache DB (one sqlite3 query, no NAS traffic)
- `_derivatives_dir_photos4` computes the Photos 4 derivatives path using the same `_get_resource_loc` formula as osxphotos
- `_copy_thumbnail` gains a Photos 4 fallback: when `path_derivatives` is empty, globs the computed dir against the real library path; largest file first (best-effort heuristic documented in code)
- `index_library` gains two targeted edits: build map before loop, pass `model_id` per asset to `_copy_thumbnail`
- Fast path (path_derivatives populated) is regression-tested: it wins over the fallback

## Test plan
- [ ] Unit tests for `_load_model_ids`, `_derivatives_dir_photos4`, `_copy_thumbnail` fallback and fast-path regression
- [ ] Integration test: minimal fake cache DB + derivative file → `thumb_ok == 1` through `index_library`
- [ ] Run `bp index-legacy --limit 50` with NAS mounted and confirm `thumb_ok > 0`
- [ ] Full test suite green

Closes #164

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 3: Post retrospective comment on GH#164**

```bash
gh issue comment 164 --repo cdevers/Blue-Pearmain --body "$(cat <<'EOF'
Implemented in PR #<number>.

Size estimate: S ✓ (~40 LOC production, ~100 LOC test)
Files changed: poller/legacy_indexer.py, tests/test_legacy_indexer.py
Plan tasks: 6 / 6 complete

The `model_id_override` parameter from the first plan draft was dropped following
code review; tests now use direct `_copy_thumbnail` unit calls and a minimal fake
cache DB for integration coverage.
EOF
)"
```

---

## Self-Review Notes

**Spec coverage:**
- ✓ Resolve derivative paths against real bundle → Tasks 3+4
- ✓ Confirm osxphotos `path_derivatives` for Photos 4 → documented in Architecture (empty from cache; confirmed by reading osxphotos source)
- ✓ Fall back to `Thumbnails/` construction → implemented as `resources/proxies/derivatives/` (the actual Photos 4 path; "Thumbnails/" is iPhoto's older layout)
- ✓ Re-run `--limit` pass and confirm `thumb_ok > 0` → Task 6 step 1

**No placeholders present.**

**Review feedback addressed (rounds 1 and 2):**
- ✓ No `model_id_override` in production API — tests use direct `_copy_thumbnail` calls or a real fake cache DB
- ✓ `index_library` edits are minimal (two additions) not a full replacement
- ✓ Largest-file heuristic documented in docstring, code comment, and test name
- ✓ Fast-path regression test added (`test_copy_thumbnail_fast_path_wins_when_derivatives_present`)
- ✓ Integration test annotated as intentionally encoding the cache-path contract
- ✓ `_load_model_ids` duplicate-UUID behavior documented (last-row-wins, intentional)
- ✓ `stat()` failure in fallback sort fixed — per-file try/except, unreadable entries skipped
- ✓ `stat()` failure test added (`test_copy_thumbnail_photos4_fallback_handles_stat_failure`)
- ✓ `log.debug` emitted when fallback copy succeeds; verification runs with `--verbose` so DEBUG messages are visible (default level is INFO)

**Type consistency:** `_derivatives_dir_photos4` returns `Path`; `_load_model_ids` returns `dict[str, int]`; `model_id_map.get(photo.uuid)` returns `int | None`; `_copy_thumbnail` accepts `model_id: int | None`. All consistent across tasks.
