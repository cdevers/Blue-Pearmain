# Metadata Sync Architecture

**Goal:** Make the local SQLite database a cache of "last known state" for both Flickr and Apple Photos metadata (title, description, tags). Once both sides are in the DB, a lightweight sync engine can detect changes, surface conflicts for manual resolution, and push updates to either side — without hitting the Flickr API per-photo on every run.

**Ultimate vision:** An eventually-consistent bridge. When metadata changes on Flickr or in Apple Photos, the other side reflects it within a configurable window (hours to days via scheduled jobs), with manual conflict resolution for cases where both sides changed independently.

---

## Current state (before this work)

- `bp sync-metadata` fetches title/description/tags from Flickr **live** for every photo on every run (~18 hours for 71k photos).
- The poller fetches Flickr title/description/tags but discards them before writing to the DB (`poller.py` lines 425–432).
- Apple Photos metadata is read live via osxphotos during sync, not cached.
- The DB has no stored representation of either side's current metadata state.
- Conflicts are detected and stored in `metadata_conflicts` but only relative to the live values at the time of the run.

---

## Target state

```
Flickr API  ──poll──►  photos.flickr_title / flickr_description / flickr_tags
                                        │
                                  sync engine
                                        │
Apple Photos ──scan──► photos.photos_title / photos_description / photos_tags
```

The sync engine compares the two cached columns, applies policy (Flickr wins when Photos is empty; conflict when both differ), writes to `canonical_*` columns, and queues pushes to either side. All this happens without live API calls.

---

## Phases

### Phase 1 — DB schema: cache both sides
*Prerequisite for everything else. Safe to ship alone.*

Add columns to the `photos` table via a new migration:

| Column | Type | Description |
|--------|------|-------------|
| `flickr_title` | TEXT | Last title fetched from Flickr |
| `flickr_description` | TEXT | Last description fetched from Flickr |
| `flickr_tags` | TEXT | JSON array — last tags fetched from Flickr |
| `photos_title` | TEXT | Last title read from Apple Photos |
| `photos_description` | TEXT | Last description read from Apple Photos |
| `photos_tags` | TEXT | JSON array — last keywords read from Apple Photos |
| `meta_synced_flickr_at` | TEXT | ISO8601 — when Flickr side was last fetched |
| `meta_synced_photos_at` | TEXT | ISO8601 — when Photos side was last read |

**Files to change:**
- `db/schema.sql` — add columns to `CREATE TABLE IF NOT EXISTS photos`
- `db/migrations/migrate_008_metadata_cache.py` — ALTER TABLE for existing DBs

**Completion criteria:** columns exist in schema and migration; no other behaviour changes yet.

---

### Phase 2 — Poller writes Flickr metadata to DB
*Depends on Phase 1. Makes `bp poll` populate the Flickr cache.*

Stop discarding `flickr_title`, `flickr_description`, `flickr_tags` in `poller.py` before `upsert_photo()`. Write them into the new columns instead. Set `meta_synced_flickr_at` on every upsert.

`bp poll --backfill --days N` then becomes the initial bulk population pass and the recurring "refresh Flickr cache" job.

**Note on "just the changes" API:** Flickr's `people.getPhotos` sorted by `date-updated` (not `date-taken`) gives photos most recently edited first. This isn't exposed via `bp poll` today but could be added as `--sort updated` to make incremental refreshes much faster than a full backfill.

**Files to change:**
- `poller/poller.py` — remove the three `.pop()` lines; map fields to new column names

**Completion criteria:** after `bp poll --backfill`, `flickr_title`/`flickr_description`/`flickr_tags` are populated for all photos that have a `flickr_id`.

---

### Phase 3 — Fast sync-metadata reads from DB
*Depends on Phases 1 & 2. The main performance win.*

Rewrite `pull_batch()` / `pull_photo_metadata()` in `flickr/metadata_puller.py` to:
1. Read `flickr_*` columns from the DB instead of calling `flickr.get_photo_info()` per photo.
2. Read `photos_*` columns from the DB instead of opening osxphotos per photo (when the cache is warm).
3. Compare and apply the same policy as today (Flickr wins when Photos empty; conflict when both differ).

The live Flickr API path becomes `bp sync-metadata --refresh-flickr`, which re-fetches from the API and updates the cache before comparing. The live Photos read path becomes `--refresh-photos`.

**Files to change:**
- `flickr/metadata_puller.py` — replace live-fetch with DB-read; keep live-fetch behind `--refresh-*` flags
- `flickr/sync_metadata.py` — wire up `--refresh-flickr` / `--refresh-photos` flags

**Completion criteria:** `bp sync-metadata` with a warm cache completes in under 60 seconds for 71k photos.

---

### Phase 4 — Scanner writes Photos metadata to DB
*Depends on Phase 1. Independent of Phases 2 & 3.*

Add a Photos metadata pass to `bp scan` (or a new `bp scan-metadata` sub-command) that reads `title`, `description`, and `keywords` from the Photos library for every photo with a `uuid` and writes them to `photos_title`, `photos_description`, `photos_tags`. Sets `meta_synced_photos_at`.

This is the Apple Photos analogue of Phase 2's poller changes.

**Files to change:**
- `poller/scanner.py` (or a new `poller/metadata_scanner.py`) — add keywords/title/description to the per-photo scan pass
- `bp` — wire up if a new sub-command is added

**Completion criteria:** after `bp scan`, `photos_title`/`photos_description`/`photos_tags` are populated for all photos with a `uuid`.

---

### Phase 5 — Canonical columns and conflict resolution UI
*Depends on Phases 1–4.*

Add `canonical_title`, `canonical_description`, `canonical_tags` columns to the DB. These hold the resolved "what should be on both sides" value after conflict resolution.

The sync engine (run as part of `bp sync-metadata`) sets canonical values:
- Flickr has value, Photos empty → canonical = Flickr value
- Photos has value, Flickr empty → canonical = Photos value
- Both agree → canonical = either (they're equal)
- Both differ → write to `metadata_conflicts` for manual resolution; canonical stays NULL until resolved

Extend the reviewer UI `/conflicts` page to:
- Show `flickr_*` vs `photos_*` side by side
- "Use Flickr" / "Use Photos" / "Skip for now" buttons
- Resolving writes the chosen value to `canonical_*` and marks the conflict resolved

**Files to change:**
- `db/schema.sql` + new migration — add `canonical_*` columns
- `flickr/metadata_puller.py` — write canonical values as part of sync
- `reviewer/app.py` — extend `/conflicts` API routes
- `reviewer/templates/conflicts.html` — extend UI (or new page)

---

### Phase 6 — bp reconcile pushes metadata to Flickr
*Depends on Phase 5.*

Extend `bp reconcile` to push `canonical_title`, `canonical_description`, `canonical_tags` to Flickr for photos where the canonical value differs from the cached `flickr_*` value.

**Tag limit handling:** Flickr enforces a 75-tag limit. When `canonical_tags` would exceed this:
1. Log a warning listing which tags were dropped.
2. Prefer shorter/simpler tags (or use a configurable priority list).
3. Record the truncation in the DB so the UI can surface it.

**Files to change:**
- `poller/reconcile.py` — add metadata push step
- `flickr/flickr_client.py` — add `set_photo_meta(flickr_id, title, description)` method if not present

---

### Phase 7 — Scheduled sync (cron / launchd)
*Depends on Phases 2–4.*

Add a `bp cron` command (or a `launchd` plist installer) that schedules:
- `bp poll` — daily (refreshes Flickr cache, picks up new uploads)
- `bp scan` — weekly (refreshes Photos cache; slower due to library open)
- `bp sync-metadata` — after each poll completes (fast once cache is warm)
- `bp reconcile` — after each sync-metadata (pushes resolved values)

The reviewer dashboard should show "Flickr last synced N hours ago" and "Photos last synced N hours ago" using `meta_synced_flickr_at` / `meta_synced_photos_at`.

**Files to change:**
- New `bp cron` sub-command or `launchd/com.bluepearmain.sync.plist`
- `reviewer/app.py` — expose last-sync timestamps to dashboard template
- `reviewer/templates/dashboard.html` — show staleness indicators

---

## Data flow summary

```
bp poll (daily)
  └─► flickr_title, flickr_description, flickr_tags, meta_synced_flickr_at

bp scan (weekly)
  └─► photos_title, photos_description, photos_tags, meta_synced_photos_at

bp sync-metadata (after poll)
  ├─► reads flickr_* and photos_* from DB (no API calls)
  ├─► writes canonical_* where unambiguous
  └─► writes metadata_conflicts where both sides differ

/conflicts UI (human, on demand)
  └─► resolves conflicts → writes canonical_*

bp reconcile (after sync-metadata)
  ├─► pushes canonical_* → Flickr (title, description, tags ≤75)
  └─► pushes canonical_* → Apple Photos (via photoscript)
```

---

## What is NOT in scope

- Real-time change detection (webhooks, file-system watchers). Everything is pull-based on a schedule.
- Album sync is handled separately (`bp sync-albums`; see `docs/album-metadata-sync.md`).
- Privacy/visibility changes are handled by the existing review → reconcile flow, not this system.
- Flickr `date-updated` incremental polling (noted as a nice-to-have in Phase 2 but not required).

---

## Migration numbering

As of writing, migrations 001–007 exist. This work will consume:
- `migrate_008_metadata_cache.py` — Phase 1 (flickr_*/photos_* columns)
- `migrate_009_canonical_metadata.py` — Phase 5 (canonical_* columns)
