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

## Data model (new migration)

New table `legacy_assets` (one row per old-library asset):

| Column | Notes |
|---|---|
| `id` | INTEGER PK |
| `library_uuid` | TEXT — source bundle `databaseUuid` |
| `asset_uuid` | TEXT — asset stable ZUUID |
| `original_filename` | TEXT |
| `fingerprint` | TEXT — osxphotos fingerprint / cloud guid |
| `date_taken` | TEXT ISO8601 |
| `width`, `height` | INTEGER |
| `latitude`, `longitude` | REAL |
| `title`, `description` | TEXT |
| `keywords` | TEXT — JSON array |
| `labels` | TEXT — JSON array |
| `persons` | TEXT — JSON array of names (mirrors `photos.apple_persons`) |
| `named_face_count` | INTEGER |
| `unknown_face_count` | INTEGER |
| `master_rel_path` | TEXT — path relative to bundle root |
| `thumbnail_path` | TEXT — absolute path in BP thumb cache (copied) |
| `indexed_at` | TEXT ISO8601 |

Constraint: `UNIQUE(library_uuid, asset_uuid)`. Indexes on `date_taken` and `(width, height)` to support the matching preview.

**Design choice:** mirror the existing `photos.apple_persons` JSON-array convention rather than introducing normalized `legacy_persons` / join tables. Consistent with the codebase; YAGNI for this phase.

Migration follows the existing `migrate_019_*` idempotent pattern. Next free migration number to be confirmed at implementation time by inspecting `db/migrations/`.

## Components

- **`poller/legacy_indexer.py`** (new module)
  - `index_library(library_path: str, db, copy_thumbnails: bool = True, limit: int | None = None) -> dict` (stats).
  - Opens `osxphotos.PhotosDB(library_path)`, iterates photos, builds a row per asset, upserts (idempotent on identity), and copies an existing derivative/preview thumbnail into BP's thumb cache (no regeneration). Uses `poller/bp_logging.py`.
- **`db/db.py`**: `upsert_legacy_asset(...)`, `legacy_asset_count()`, `iter_legacy_assets()`.
- **CLI (`bp`)**: `bp index-legacy --library <path> [--no-thumbnails] [--limit N]`. Path from `--library`, falling back to optional `legacy_library.path` in config; the flag always wins. Identity is path-independent, so re-pointing requires no reindex.
- **Matching preview (`bp match-legacy-preview`)**: joins `legacy_assets` to Flickr-only `candidate_public` photos. Match key is `date_taken`, with dimensions and title as disambiguators. Emits a tiered report (confident / ambiguous / no-match) and optional CSV. **Writes nothing to `photos`.**

Matching feasibility (verified against the live DB): Flickr-only `candidate_public` rows have `date_taken` 100% (34035/34035), dimensions ~100% (34034), `flickr_title` 72% (24555). The legacy assets carry all of these plus fingerprint.

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
- **osxphotos open contingency:** opening this Photos 4 library over AFP copies a multi-GB SQLite first and is slow; Photos 4 support, while real, is less battle-tested than Photos 5+. A feasibility open-test is run during implementation. If osxphotos cannot open the library, fall back to a **direct read-only SQL reader** against `Photos.sqlite` (`ZGENERICASSET ⋈ ZPERSON ⋈ ZDETECTEDFACE`) behind the same `index_library` interface — the data model, CLI, and matching preview are unaffected.

## Testing (TDD)

- **Migration test**: table + columns exist; idempotent re-run (mirrors `migrate_019` tests).
- **DB methods**: `upsert_legacy_asset` insert + update; idempotency on duplicate identity.
- **Indexer unit test**: mock `osxphotos.PhotosDB` with fake photo objects → assert rows built correctly (persons, face counts, bundle-relative paths) and thumbnail-copy invoked. No real 1.8 GB read in CI.
- **Matching-preview test**: seed `legacy_assets` + Flickr-only `candidate_public` photos with overlapping date/dimensions → assert the report identifies the match and asserts **zero writes** to `photos`.
- **Path-independence test**: re-index the same identity from a different `library_path` → updates the same row, no duplicate.

Run `python -m pytest tests/ -q` and `make lint` (mypy-clean, no bare `# type: ignore`).

## Release

Branch + green PR (no direct commits to `main`). Reference **#162** in commits; relates to **#12**. Version bump to **1.4.0** on merge (via branch + PR per branch-protection policy). Add the `has-plan` label to #162 once the implementation plan is written.
