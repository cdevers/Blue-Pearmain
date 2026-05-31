# Design — Legacy library indexer (GH #162, target 1.4.0)

## Problem & motivation

A large fraction of BP's review queue is family photos sitting on the wrong (public-leaning) side. Investigation of `data/curator.db`:

- The "general queue" is the `candidate_public` state: **34,045 photos, 34,035 of them Flickr-only** (`uuid IS NULL`).
- Every Flickr-only one has `privacy_reason = "no people detected"` and **zero `apple_persons`**.
- Flickr upload dates cluster **2008–2017** — the iPhoto era.

**Root cause:** BP's privacy classifier (`analyzer/privacy.py`) detects family/people *entirely* from Apple Photos ML metadata (named persons, face counts, people labels). These photos exist only on Flickr and were never matched to a current Apple Photos entry, so they carry none of that metadata. `classify()` falls through every people-check to the final step → `candidate_public`. They are **not** auto-published (confirmation is still required), but the queue is polluted and a bulk "confirm all" would be dangerous.

The metadata to fix this exists in the old, now-migrated library mounted (via AFP today) at a path such as `…/Photos Library.photoslibrary`: a **Photos 4** library (`ZGENERICASSET`, `LibrarySchemaVersion 5002`, `DatabaseVersion 112`) containing **1,451 named people** including the whole family (Isaac, Aidan, May, Chris, Don Devers, …).

## Goal & boundary

Build the **foundation** ("index now, match later"):

1. Index the old library's per-asset metadata + copied thumbnails into new `legacy_*` tables in `curator.db`.
2. Expose a `bp` CLI command to build/refresh the index from a **runtime-supplied path**.
3. Provide a **non-destructive matching-preview** report identifying which legacy assets likely correspond to the Flickr-only `candidate_public` photos.

**Out of scope (deliberately deferred):**
- Reviewer UI browse page for legacy assets.
- Actual reclassification / merge of matched photos — that is the iPhoto migration (#12).
- Re-generating high-resolution thumbnails.
- Normalized person/face tables for the legacy data.

## Path independence (explicit constraint)

The library is mounted via AFP today but may move to SMB, a different IP, or an external HDD. Therefore:

- Asset identity is **`(library_uuid, asset_uuid)`** — the bundle's `databaseUuid` plus the asset's stable ZUUID. The **absolute mount path is never part of the identity** and is never persisted as such.
- `master_rel_path` is stored **relative to the library bundle root**.
- Thumbnails are **copied into BP's thumb cache**, keyed by a stable hash of the identity, so a transport/path change requires **no reindex**.
- The library root path is supplied at index time (`--library` flag, or optional config default).

## Optional local DB cache (performance & resilience)

Opening the library directly over AFP is slow: a verified open-test took **243 s (~4 min)** to load 237,309 photos. The library's `database/` directory is **6.5 GB** — small enough to cache locally (86 GB free).

- Optional `--cache-db` behaviour: before opening, mirror the library's `database/` plus the small bundle plists osxphotos needs (e.g. `DataModelVersion.plist`) into a local skeleton bundle at `<dir of curator.db>/legacy-cache/<library_uuid>/` (i.e. under `data/`, derived from the configured DB path), then point osxphotos at the local copy. `data/` is git-ignored, so this stays **out of the repo** while living alongside `curator.db`.
- Keyed by `library_uuid`, so multiple libraries and refreshes coexist; the cache is purely a performance/resilience aid, never part of asset identity, and is safe to delete and rebuild.
- Thumbnails are read from the live mount during the single index run (then copied into BP's thumb cache as the durable artifact), so the cache only needs the database files, not the originals/derivatives.

**Cache invalidation (review point 3).** The cache is validated against `legacy_libraries` metadata before reuse:
- **Reuse** the local cache if it exists and the source `Photos.sqlite` matches the recorded `db_mtime` + `db_size` **and** `db_head_hash` (SHA256 of the first 16 MiB of raw file bytes). The hash is the extra discriminator (review point 1): it catches restores/rsync copies that preserve mtime, and same-size replacements that bare mtime+size would miss. Reading 16 MiB is cheap relative to the 1.8 GB full DB.

**Cache-validity metadata lives in the DB.** `db_mtime`, `db_size`, and `db_head_hash` are columns on `legacy_libraries` in `curator.db` — there is no filesystem sidecar. The local cache directory under `data/legacy-cache/<library_uuid>/` holds only the copied bundle files; its validity is judged solely by comparing the live source `Photos.sqlite` against these DB-recorded values.
- **Rebuild** if the cache is absent, any discriminator differs (library upgraded/restored/replaced), or a prior copy was incomplete (a copy completes by atomic rename of a temp dir, so a partial copy is never treated as valid).
- `--refresh-cache` forces a rebuild regardless. Stale/partial caches are replaced, not appended to.

## Data model (new migration)

Two new tables.

### `legacy_libraries` — one row per indexed source library

Houses per-library identity, provenance, and cache-validity metadata (the latter drives cache invalidation, below).

| Column | Notes |
|---|---|
| `library_uuid` | TEXT PK — source bundle `databaseUuid` |
| `display_name` | TEXT — friendly label |
| `source_path_last_seen` | TEXT — last mount path indexed from (advisory only; not identity) |
| `schema_version` | INTEGER — `LibrarySchemaVersion` from the bundle |
| `db_mtime` | TEXT — mtime of the source `Photos.sqlite` at last index/cache |
| `db_size` | INTEGER — size of the source `Photos.sqlite` at last index/cache |
| `db_head_hash` | TEXT — SHA256 of the **first 16 MiB (16,777,216 raw file bytes)** of source `Photos.sqlite` — file bytes, not SQLite pages or logical DB content (cheap content discriminator) |
| `asset_count` | INTEGER |
| `indexed_at` | TEXT ISO8601 |

### `legacy_assets` — one row per old-library asset

| Column | Notes |
|---|---|
| `id` | INTEGER PK |
| `library_uuid` | TEXT — FK → `legacy_libraries.library_uuid` |
| `asset_uuid` | TEXT — asset stable ZUUID |
| `original_filename` | TEXT |
| `fingerprint` | TEXT — **advisory matching signal only, never identity** (may be absent or regenerated) |
| `date_taken` | TEXT ISO8601 (as osxphotos reports; normalized only at compare time — see Matching) |
| `width`, `height` | INTEGER |
| `latitude`, `longitude` | REAL |
| `title`, `description` | TEXT |
| `keywords` | TEXT — JSON array, unique + alphabetically sorted |
| `labels` | TEXT — JSON array, unique + alphabetically sorted |
| `persons` | TEXT — JSON array of names, **unique + alphabetically sorted** (deterministic, to avoid noisy re-index updates) |
| `named_face_count` | INTEGER |
| `unknown_face_count` | INTEGER |
| `master_rel_path` | TEXT — path **relative to bundle root**, POSIX separators, original case preserved |
| `thumbnail_cache_key` | TEXT — stable cache key (hash of `library_uuid`+`asset_uuid`); absolute path is **resolved at read time** against the configured thumb-cache root, never persisted |
| `thumbnail_status` | TEXT — `ok` / `missing` / `error`; a thumbnail miss records status and continues, it never fails the index run |
| `indexed_at` | TEXT ISO8601 |

Constraint: `UNIQUE(library_uuid, asset_uuid)`. Indexes on `date_taken` and `(width, height)` to support the matching preview.

**Identity vs. advisory fields:** asset identity is strictly `(library_uuid, asset_uuid)`. `fingerprint`, `original_filename`, `source_path_last_seen`, and the thumbnail cache location are all **advisory** — never identity, never the source of truth for "where is this asset."

**Path canonicalization (review point 5):** `master_rel_path` is produced by a single canonical transform, in this fixed order, so the same asset never yields two spellings: (1) convert separators to POSIX `/`; (2) collapse duplicate slashes — **after** separator normalization, so backslash-derived separators are also collapsed; (3) strip leading `./`; (4) strip trailing slash; (5) preserve original case; (6) normalize Unicode to **NFC** (macOS surfaces NFD). This is for stable storage/comparison and display; it is advisory, so a later phase that needs to *open* the file should re-derive the path from the live bundle rather than trust this string.

**Thumbnail path (review point 1):** only the cache *key* is durable. The absolute path is derived at read time from the configured thumb-cache root, so moving the repo / `data/` / restoring backups never strands the DB on stale absolute paths. This deliberately diverges from the older `photos.thumbnail_path` (absolute) convention because path-independence is a first-class goal for the legacy index.

**Design choice:** mirror the existing `photos.apple_persons` JSON-array convention rather than introducing normalized person/face tables. Consistent with the codebase; YAGNI for this phase.

Migration follows the existing `migrate_019_*` idempotent pattern. Next free migration number to be confirmed at implementation time by inspecting `db/migrations/`.

### Reindex semantics (review point 2)

`legacy_assets` is an **authoritative mirror** of the source library, not an append-only archive. A **full** index run reconciles per `library_uuid`:
- assets present in the source are upserted;
- rows for that `library_uuid` **not seen during the run are hard-deleted** (the source library is the sole authority; the index is rebuildable, so no soft-delete/tombstones).

This prevents removed assets from lingering and producing false-positive candidates in the matching preview.

A **`--limit N`** run is explicitly **non-authoritative**: it upserts only the sampled assets and performs **no deletions** (a partial run cannot speak for the whole library). Reconciliation/GC happen only on full runs.

**Completion marker required before any deletion (review point 2).** Reconciliation (row hard-delete) and thumbnail GC run **only after the full run has iterated the entire source library to successful completion**. The indexer records the set of `asset_uuid`s seen during the run and performs deletions in a single final step gated on that successful completion. An **interrupted full run** (exception, kill, unmounted share mid-iteration) deletes nothing — it behaves exactly like `--limit`: upserts what it saw, reconciles nothing. This prevents a partial iteration from being mistaken for "the library no longer contains these assets" and mass-deleting valid rows.

**Thumbnail orphan cleanup (review point 3):** the thumbnail cache is reconciled on full runs — cache entries whose key no longer maps to a live `legacy_assets` row for that library are pruned, so deleted assets don't leak thumbnails forever. (`--limit` runs prune nothing.)

## Components

- **`poller/legacy_indexer.py`** (new module)
  - `index_library(library_path: str, db, copy_thumbnails: bool = True, limit: int | None = None) -> dict` (stats).
  - Opens `osxphotos.PhotosDB(library_path)`, iterates photos, builds a row per asset, upserts (idempotent on identity), and copies an existing derivative/preview thumbnail into BP's thumb cache (no regeneration). Uses `poller/bp_logging.py`.
- **`db/db.py`**: `upsert_legacy_asset(...)`, `legacy_asset_count()`, `iter_legacy_assets()`.
- **CLI (`bp`)**: `bp index-legacy --library <path> [--no-thumbnails] [--limit N] [--no-cache] [--refresh-cache]`. Path from `--library`, falling back to optional `legacy_library.path` in config; the flag always wins. Identity is path-independent, so re-pointing requires no reindex. **Local DB cache is on by default** (given the ~4-min AFP open); `--no-cache` reads the library in place, `--refresh-cache` forces a cache rebuild.
- **Matching preview (`bp match-legacy-preview`)**: joins `legacy_assets` to Flickr-only `candidate_public` photos. Emits a deterministic tiered report to the console plus an optional CSV (`--csv <path>`). **No new table, and writes nothing to `photos`** — persisting proposed links is deferred to the #12 migration phase that will actually consume them (YAGNI here).

**Timestamp normalization (review point 2).** The two sources store `date_taken` differently — Flickr-only is naive (`2026-04-08 14:42:08`), Apple/osxphotos is tz-aware (`2023-05-06T16:34:28-04:00`, sometimes with microseconds). Both sides are normalized through the **existing** `poller/deduplicator.py:_normalise_to_utc_second()` helper before comparison: it parses the space-separated Flickr variant, treats naive timestamps as UTC, converts tz-aware to UTC, and truncates to whole seconds (`YYYY-MM-DD HH:MM:SS`). Reusing it keeps legacy matching consistent with the existing reupload/orphan matching semantics rather than inventing a parallel scheme. The naive=UTC assumption can introduce a systematic offset for genuinely local Flickr times; because this command is a **non-destructive preview**, any such drift surfaces in the ambiguous/no-match tiers for inspection before any future migration acts on it.

**Deterministic match tiers** (for durable tests):
- **confident** — exactly one legacy asset whose normalized-UTC-second `date_taken` equals the photo's *and* `(width, height)` match, with no title conflict.
- **ambiguous** — more than one legacy candidate at the matching timestamp, or a timestamp match with conflicting dimensions or title.
- **no-match** — zero legacy candidates at the normalized timestamp.

**Deterministic output ordering (review point 6).** Both the console report and the CSV emit rows in a fixed sort order so re-running over an unchanged DB produces byte-identical output (stable diffs): primary key **tier** (confident, then ambiguous, then no-match), then **normalized `date_taken`** ascending, then the Flickr photo's **`flickr_id`** ascending, then (for the candidate legacy rows within a photo) **`asset_uuid`** ascending. Ties are fully broken by this composite key.

**Title comparison (review point 4).** Title is a weak signal and is treated as a *tiebreaker only*, never a primary key. A title "conflict" exists **only when both titles are non-empty after normalization** (trim whitespace, casefold, NFC) and still differ. **A title that is empty after trimming counts as missing** (identical to `NULL`/absent). An empty/missing title on either side is *not* a conflict and never demotes an otherwise-confident match — this keeps the ambiguous rate from climbing on the 28% of photos lacking a Flickr title.

Matching feasibility (verified against the live DB): Flickr-only `candidate_public` rows have `date_taken` 100% (34035/34035), dimensions ~100% (34034), `flickr_title` 72% (24555). The legacy assets carry all of these plus fingerprint (advisory).

## Data flow

```
mount (AFP/SMB/HDD)
   └─ bp index-legacy --library <path>
        └─ osxphotos reads metadata + copies thumbnails
             └─ legacy_assets rows in curator.db

bp match-legacy-preview
   └─ reads legacy_assets + photos → tiered match report (no writes)
```

The live review queue and `photos` table are never modified by either command.

## Error handling & safety

- **Read-only on the library bundle**: we only read it (osxphotos copies the DB to a temp dir on open); thumbnails are copied *out*. The bundle is never written.
- Missing / unmounted library path → clear error message, non-zero exit.
- Idempotent re-runs: upsert by `(library_uuid, asset_uuid)`; already-cached thumbnails are skipped. `--limit` enables quick test runs over slow AFP/SMB links.
- **osxphotos open — verified:** a feasibility open-test confirmed `osxphotos.PhotosDB` opens this Photos 4 library successfully (237,309 photos, persons present), in ~243 s over AFP (hence the optional local DB cache above). Remaining contingency: if osxphotos later proves unreliable on this library, fall back to a **direct read-only SQL reader** against `Photos.sqlite` (`ZGENERICASSET ⋈ ZPERSON ⋈ ZDETECTEDFACE`) behind the same `index_library` interface — the data model, CLI, and matching preview are unaffected.

## Testing (TDD)

- **Migration test**: both tables + columns exist; idempotent re-run (mirrors `migrate_019` tests).
- **DB methods**: `upsert_legacy_asset` insert + update; idempotency on duplicate identity; `persons`/`keywords`/`labels` stored unique + alphabetically sorted (re-index with reordered input produces an identical row — no noisy update).
- **Indexer unit test**: mock `osxphotos.PhotosDB` with fake photo objects → assert rows built correctly (persons, face counts, bundle-relative POSIX paths) and thumbnail-copy invoked. No real 1.8 GB read in CI.
- **Thumbnail-miss test**: a derivative that can't be copied records `thumbnail_status='missing'` and does **not** fail the run.
- **Matching tiers test**: seed cases that exercise each deterministic tier (confident / ambiguous / no-match), including a Flickr naive timestamp vs an Apple tz-aware timestamp that normalize to the same UTC second → confident; assert **zero writes** to `photos`.
- **Cache invalidation test**: matching `db_mtime`+`db_size`+`db_head_hash` → cache reused (no rebuild); changed hash with identical mtime+size (simulated restore) → rebuild; `--refresh-cache` → rebuild; partial cache (no atomic-rename marker) treated as invalid.
- **Reindex reconciliation test**: full run that no longer sees a previously-indexed asset hard-deletes its row; a `--limit` run deletes nothing.
- **Thumbnail GC test**: full run prunes cache entries orphaned by deleted assets; `--limit` run prunes nothing.
- **Title-conflict test**: empty title on one side never demotes a confident match; two non-empty titles differing after trim/casefold/NFC → ambiguous.
- **Path-canonicalization test**: inputs with `./`, duplicate slashes, trailing slash, and NFD Unicode all collapse to one canonical NFC POSIX string.
- **Path-independence test**: re-index the same identity from a different `library_path` → updates the same row, no duplicate; `thumbnail_cache_key` resolves correctly after the configured cache root changes.

Run `python -m pytest tests/ -q` and `make lint` (mypy-clean, no bare `# type: ignore`).

## Release

Branch + green PR (no direct commits to `main`). Reference **#162** in commits; relates to **#12**. Version bump to **1.4.0** on merge (via branch + PR per branch-protection policy). Add the `has-plan` label to #162 once the implementation plan is written.

✓ plan written — `docs/superpowers/plans/2026-05-30-legacy-library-indexer-162.md`. Implemented on branch `feat/legacy-library-indexer-162` (Tasks 1–7 complete; migration 026, `legacy_normalize`/`legacy_cache`/`legacy_indexer`/`legacy_match`, and the `bp index-legacy` / `bp match-legacy-preview` subcommands).

### Implementation notes (from live verification)

- **`library_uuid` derivation.** osxphotos exposes no `library_uuid` for Photos-4 bundles (its `_uuid` attr is bound to an unrelated function). The intrinsic identity is read directly from the source DB: `RKAdminData.databaseUuid` (e.g. `MY%X00uFQayV48ecM+9I2A`). Because that raw value contains path-hostile chars (`%`, `+`), it is hashed into a filesystem-safe id — `p4-<sha256(value)[:24]>` — used as both the `legacy_libraries` PK and the cache directory name. `read_library_uuid()` reads it without opening the (slow) bundle. A migrated Photos-4 bundle can carry a leftover `Photos.sqlite`; the source-DB locator prefers `photos.db`, which is the DB osxphotos reads and the only one holding `databaseUuid`.
- **Thumbnails deferred → #164.** The local DB cache copies only `database/` + bundle plists, not the originals/derivatives, so osxphotos (pointed at the cache) cannot resolve derivative paths and every thumbnail reports `missing`. Thumbnail copying is therefore deferred to follow-up **#164**; metadata indexing (the part that unblocks #162's queue-pollution goal) works on its own. Use `--no-thumbnails` until #164 lands.
- **Match on wall-clock, NOT UTC (supersedes "Timestamp normalization" above).** The spec planned to reuse `_normalise_to_utc_second()`. Live verification disproved its naive=UTC assumption: against the real library that scheme matched only 44 of 34,041 candidates. Flickr `date_taken` is naive *local* capture time (EXIF `DateTimeOriginal`), while legacy Apple dates are tz-aware in the photo's local zone — so the same shot shares wall-clock digits but differs by the local offset (~4–5h Eastern) once converted to UTC. The matcher now uses `legacy_match.normalise_wall_clock()`, which strips the tz offset and keeps the local wall-clock. Result on the live library: **24,562 confident + 3,821 ambiguous = 28,383 matched**, 5,658 no-match. `_normalise_to_utc_second()` is left untouched because it remains correct for Flickr↔Flickr reupload/orphan matching, where both sides are naive and the offset cancels.
