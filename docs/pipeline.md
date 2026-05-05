# Blue Pearmain Pipeline

`bp all` runs the full maintenance sequence. This document explains what each stage does, what it reads and writes, whether it's safe to run in isolation, and what happens when something goes wrong.

---

## Stage order

```
bp all
  1. scan --all        Read Apple Photos → DB
  2. poll              Read Flickr API   → DB
  3. thumbs            Download thumbnails → disk
  4. pipeline          Diff DB caches    → proposals → apply non-conflicts
  5. reconcile --fix   Validate DB state → push corrections to Flickr
  6. sync-albums       Sync album memberships → Flickr
  7. checkpoint        Trim WAL file
```

The order matters because later stages depend on data written by earlier ones (`pipeline` needs the Flickr and Photos caches that `scan` and `poll` populate). Stages 5–7 are independent of each other once stage 4 has run.

---

## Fault tolerance

`bp all` runs every stage in an independent try/except block. **A failure in any stage is logged and the sequence continues.** No stage can block a later one from running. This means:

- A network error during `poll` doesn't prevent `pipeline` from running with the existing cache.
- A photoscript error during `pipeline` doesn't prevent `reconcile` from running.

Partial failures are reported at the end: `all: completed with 2 error(s): poll, pipeline`.

---

## Idempotency

**`bp all` is safe to run multiple times.** Every stage is designed to be idempotent:

| Stage | Idempotency | Notes |
|-------|-------------|-------|
| `scan` | ✓ Safe to repeat | Re-reads Photos; overwrites cache columns with current values |
| `poll` | ✓ Safe to repeat | Re-fetches Flickr; overwrites cache columns; deduplicates photos by `flickr_id` |
| `thumbs` | ✓ Safe to repeat | Skips photos that already have thumbnail paths |
| `pipeline` | ✓ Safe to repeat | Proposals deduplicated by identity key; applied proposals skipped; staleness check supersedes stale ones |
| `reconcile` | ✓ Safe to repeat | Reads current Flickr state and corrects; re-running on a corrected photo is a no-op |
| `sync-albums` | ✓ Safe to repeat | Compares current Flickr membership against DB; pushes only differences |
| `checkpoint` | ✓ Safe to repeat | WAL trim; harmless if WAL is already empty |

---

## Stages in detail

### 1. `bp scan [--all] [--days N]`

**What it does:** Reads every photo from the Apple Photos library via osxphotos and upserts records into the `photos` table. Captures UUID, filename, date taken, GPS, Apple ML labels, face detections, privacy state classification, and — since Phase 3 — title, description, and tags from the Photos library.

**Reads:** Apple Photos library (local, via photoscript/osxphotos)  
**Writes:** `photos` table (all columns), sets `meta_synced_photos_at`  
**External writes:** None  
**Idempotent:** Yes — upserts by UUID; re-running overwrites with current Photos state  

`--all` rescans every photo; default (incremental) scans only photos modified recently. Use `--all` when in doubt.

---

### 2. `bp poll [--backfill] [--days N]`

**What it does:** Fetches photo metadata from the Flickr API and upserts records into the `photos` table. Captures Flickr ID, title, description, tags, upload date, permissions, and last-updated timestamp.

**Reads:** Flickr API  
**Writes:** `photos` table (flickr columns), sets `meta_synced_flickr_at`, `flickr_last_updated`  
**External writes:** None — this is read-only from Flickr's perspective  
**Idempotent:** Yes — upserts by `flickr_id`; re-running refreshes the cache  

`--backfill` fetches all photos ever uploaded (slow, ~18h for 71k photos). Default fetches only recent uploads. Run backfill once after initial setup.

---

### 3. `bp thumbs`

**What it does:** Populates `thumb_path` for photos that don't have one. Tries Photos derivatives first (fast, local), then downloads from Flickr's `url_l` thumbnail URL (slower, requires network).

**Reads:** Local filesystem, Flickr CDN  
**Writes:** `photos.thumb_path`; thumbnail files in `thumbnails.path` (from config)  
**External writes:** None  
**Idempotent:** Yes — skips photos already with `thumb_path`  

---

### 4. `bp pipeline` (and `bp sync-metadata`)

**What it does:** Two sub-steps chained together:

1. **Sync engine** — reads the cached Flickr and Photos metadata from the DB (no API calls), runs a drift filter to find photos where the caches have diverged, classifies differences as `non_conflict` / `divergence` / `collision`, and writes proposals to `metadata_proposals`.

2. **Auto-apply** — applies `non_conflict` proposals immediately (both sides had the same field empty on one side; safe to fill in). `collision` and `divergence` proposals are left pending for human review in the `/proposals` UI.

**Reads:** `photos` table (flickr and photos cache columns)  
**Writes:** `metadata_proposals` table; applies non-conflict proposals to Flickr (API) and Apple Photos (photoscript)  
**External writes:** Yes — non-conflict proposals write to both Flickr and Apple Photos  
**Idempotent:** Yes — proposals are deduplicated by `(photo_id, field, proposed_value, source, target)`; applied proposals are never re-applied; stale proposals are superseded rather than duplicated  

See [`docs/metadata-sync-architecture.md`](metadata-sync-architecture.md) for the full design.

---

### 5. `bp reconcile [--fix]`

**What it does:** Validates that Flickr's actual state matches what the DB says was pushed. Detects drift caused by external edits, failed pushes, or permission changes. With `--fix`, corrects the mismatches by re-pushing the DB's authoritative state to Flickr.

**Reads:** Flickr API (to verify current state), `photos` table  
**Writes (--fix):** Flickr permissions, Flickr tags  
**External writes:** Yes, with `--fix`  
**Idempotent:** Yes — correcting an already-correct photo is a no-op  

Without `--fix`, reconcile is read-only and reports mismatches without changing anything. Safe to run for diagnosis at any time.

---

### 6. `bp sync-albums`

**What it does:** Syncs album memberships from the DB to Flickr photosets. Creates missing photosets, adds photos to the correct sets, removes photos from sets they no longer belong to.

**Reads:** `albums` and `photo_albums` tables, Flickr API  
**Writes:** Flickr photosets  
**External writes:** Yes  
**Idempotent:** Yes — computes the desired membership state and pushes only differences  

---

### 7. `bp checkpoint`

**What it does:** Runs `PRAGMA wal_checkpoint(TRUNCATE)` on the SQLite database. Moves committed WAL frames into the main DB file and truncates the WAL to zero bytes. Prevents the WAL file from growing without bound during long-running sessions.

**Reads:** SQLite WAL  
**Writes:** SQLite main DB file and WAL  
**External writes:** None  
**Idempotent:** Yes  

---

## What writes to external systems

Only these stages write outside the local DB:

| Stage | Writes to |
|-------|-----------|
| `pipeline` | Flickr (tags, title, description) via API; Apple Photos (tags, title, description) via photoscript |
| `reconcile --fix` | Flickr (permissions, tags) via API |
| `sync-albums` | Flickr (photoset membership) via API |
| `thumbs` | Flickr CDN read (download only, no write) |

All writes are **logged** and **conditional** — a write only happens if the DB's proposal or authoritative state disagrees with the current live state. No stage writes blindly.

---

## Partial failure recovery

If `bp all` fails mid-run (network outage, Photos not running, machine sleep), re-run `bp all`. All stages resume cleanly from the DB state left behind:

- Stages that completed leave their output in the DB — subsequent stages read it without re-running the completed work.
- Stages that partially completed (e.g. `pipeline` applied 200 of 400 proposals before network failure) leave applied proposals marked `applied` in the DB — re-running skips them and continues with the remaining `pending` ones.
- Stages that failed entirely are simply re-run from scratch — all are idempotent.

**Exception:** If a Photos write (via photoscript) succeeds but the DB update fails (rare, would require a crash between the two), the proposal will remain `pending` and `bp pipeline` will attempt to write the same value to Photos again on the next run. Photos writes are idempotent (writing the same title twice is harmless), so this resolves cleanly.

---

## Running stages individually

Every stage can be run independently. Useful for:

- **Debugging:** `bp scan` or `bp poll` alone to refresh one side of the cache
- **Diagnosis:** `bp reconcile` (no `--fix`) to see what's out of sync without changing anything
- **After a Photos library repair:** `bp scan --all` to rebuild the UUID cache
- **After Flickr edits:** `bp poll` then `bp pipeline` to pick up changes and generate proposals

```bash
bp scan --all          # full Photos re-index
bp poll --backfill     # full Flickr re-fetch (slow)
bp sync-metadata       # generate proposals from current cache (no API calls)
bp reconcile           # diagnose mismatches (read-only)
bp reconcile --fix     # correct mismatches
```

---

## DB lifecycle

**Fresh install:**

```bash
# The DB is created and all migrations applied automatically on first run:
bp scan --all
# or directly:
python -m bp db migrate   # (planned — see bp doctor, issue #43)
```

**Upgrading:**

Migrations in `db/migrations/` are applied automatically when the DB is opened. Applied migrations are tracked in the `schema_migrations` table and are never re-applied. The migration sequence is idempotent: running it twice is safe.

**Schema source of truth:** `db/schema.sql` reflects the fully-migrated schema. Use it to understand the current data model. Migrations exist to upgrade existing installations; `schema.sql` is what a fresh install creates.
