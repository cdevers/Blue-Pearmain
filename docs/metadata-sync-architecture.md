# Metadata Sync Architecture

**Goal:** Make the local SQLite database a cache of "last known state" for both Flickr and Apple Photos metadata (title, description, tags). Once both sides are in the DB, a lightweight sync engine can detect changes, surface conflicts for manual resolution, and push updates to either side — without hitting the Flickr API per-photo on every run.

**Ultimate vision:** An eventually-consistent bridge. When metadata changes on Flickr or in Apple Photos, the other side reflects it within a configurable window (hours to days via scheduled jobs), with manual conflict resolution for cases where both sides changed independently.

---

## Design principles

These are non-negotiable constraints that shape every implementation decision:

1. **No silent writes.** Nothing changes on either Flickr or Apple Photos without an explicit, logged operation. Background jobs detect and cache; they do not write.
2. **Explicit state only.** The DB reflects confirmed state. A field is only marked "synced" after the write is confirmed, not when it is queued.
3. **Manual conflict resolution.** When both sides have changed independently, the system surfaces the conflict in the reviewer UI; it never auto-resolves.
4. **Idempotent operations.** Applying the same sync twice does nothing. Each per-field push flag or timestamp makes this checkable.
5. **Separate validation from mutation.** `bp reconcile` (validate mode) detects mismatches. `bp reconcile --fix` (harmonize mode) applies them. Keep them separate.

---

## Field authority matrix

Explicit per-field rules prevent flip-flopping and silent overwrites:

| Field | Authority | Default direction | Notes |
|-------|-----------|-------------------|-------|
| `title` | Neither (manual) | Conflict → review queue | Either side may have been edited by the user |
| `description` | Neither (manual) | Conflict → review queue | Either side may have been edited by the user |
| `tags` | Merged / manual | Both sides contribute; conflicts reviewed | See tag model below |
| `date_taken` | Apple Photos (EXIF) | Photos → Flickr | EXIF is ground truth; Flickr's copy should match |
| `permissions` | DB / policy | DB → Flickr | Existing review → reconcile flow; unchanged |
| `albums` | Flickr (primary) | Flickr → Photos (read-only reflection) | Handled separately; see `docs/album-metadata-sync.md` |

*"Neither (manual)"* means: if both sides are non-empty and different, the system does not pick a winner — it writes a conflict record and waits for a human decision.

---

## Tag model

Tags are the most complex field because both sides legitimately contribute different values:

- **Flickr tags** are user-applied, human-curated, and the publishing target.
- **Photos keywords** may include Apple ML labels, geofence zone names, and manually added keywords.

**Canonical form rules:**
- Stored as JSON arrays in the DB.
- Comparison is case-insensitive and order-insensitive (set semantics).
- On write, original casing from the source is preserved.
- Deduplication: exact case-insensitive duplicates are removed; near-synonyms are not merged automatically.

**Sync policy:**
- Flickr has tags, Photos has none → write Flickr tags to Photos (clear win).
- Photos has tags, Flickr has none → write Photos tags to Flickr (clear win, respecting 75-tag limit).
- Both have tags and they differ → conflict record; human picks or merges manually.
- Both have the same tags (case-insensitive set equality) → no-op.

**Flickr 75-tag limit:** When a canonical tag set would exceed 75 tags when pushed to Flickr:
1. Truncate at 75, preferring shorter tags (fewer characters) as a proxy for specificity.
2. Log a warning with the full list of dropped tags.
3. Record the truncation in the DB (`tags_truncated_for_flickr = 1`) so the UI can surface it.
4. Never silently drop tags; always leave an audit trail.

---

## Change tracking

The system must distinguish *"we last fetched this"* from *"the remote side last changed this."* Without that distinction, a re-fetch looks identical to a new change.

**Timestamps stored per photo:**

| Column | Meaning |
|--------|---------|
| `meta_synced_flickr_at` | When we last successfully fetched Flickr metadata |
| `meta_synced_photos_at` | When we last successfully read Photos metadata |
| `flickr_last_updated` | `lastupdate` from Flickr API — when Flickr last modified this photo |
| `meta_last_harmonized_at` | When the sync engine last ran for this photo |

`flickr_last_updated` is the key: if it advances since our last fetch, something changed on Flickr. Apple Photos has no equivalent field-level change timestamp, so for Photos we rely on periodic re-scans and detect changes by comparing stored vs current values.

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
                                  sync engine (per-field)
                                        │
Apple Photos ──scan──► photos.photos_title / photos_description / photos_tags
```

The sync engine compares the two cached columns on a per-field basis, applies the field authority policy, writes to `canonical_*` columns where unambiguous, and surfaces conflicts for manual resolution. All this happens without live API calls once the cache is warm.

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
| `flickr_last_updated` | TEXT | ISO8601 — Flickr's `lastupdate` timestamp for this photo |
| `photos_title` | TEXT | Last title read from Apple Photos |
| `photos_description` | TEXT | Last description read from Apple Photos |
| `photos_tags` | TEXT | JSON array — last keywords read from Apple Photos |
| `meta_synced_flickr_at` | TEXT | ISO8601 — when we last fetched from Flickr |
| `meta_synced_photos_at` | TEXT | ISO8601 — when we last read from Photos |
| `meta_last_harmonized_at` | TEXT | ISO8601 — when sync engine last ran for this photo |
| `tags_truncated_for_flickr` | INTEGER | Boolean — canonical tags exceeded 75 and were truncated on last push |

**Files to change:**
- `db/schema.sql` — add columns to `CREATE TABLE IF NOT EXISTS photos`
- `db/migrations/migrate_008_metadata_cache.py` — ALTER TABLE for existing DBs

**Completion criteria:** columns exist in schema and migration; no other behaviour changes yet.

---

### Phase 2 — Poller writes Flickr metadata to DB
*Depends on Phase 1. Makes `bp poll` populate the Flickr cache.*

Stop discarding `flickr_title`, `flickr_description`, `flickr_tags` in `poller.py` before `upsert_photo()`. Write them into the new columns instead. Also capture `lastupdate` from the Flickr API response into `flickr_last_updated`. Set `meta_synced_flickr_at` on every upsert.

`bp poll --backfill --days N` then becomes the initial bulk population pass and the recurring "refresh Flickr cache" job.

**Incremental refresh:** Flickr's `people.getPhotos` can be sorted by `date-updated`. Adding `--sort updated` to `bp poll` would let a daily job fetch only recently-changed photos, making incremental refreshes much faster than a full backfill. This is a nice-to-have, not required for Phase 2 to be complete.

**Files to change:**
- `poller/poller.py` — remove the three `.pop()` lines; map fields to new column names; capture `lastupdate`

**Completion criteria:** after `bp poll --backfill`, `flickr_title`/`flickr_description`/`flickr_tags`/`flickr_last_updated` are populated for all photos that have a `flickr_id`.

---

### Phase 3 — Fast sync-metadata reads from DB
*Depends on Phases 1 & 2. The main performance win.*

Rewrite `pull_batch()` / `pull_photo_metadata()` in `flickr/metadata_puller.py` to:
1. Read `flickr_*` columns from the DB instead of calling `flickr.get_photo_info()` per photo.
2. Read `photos_*` columns from the DB instead of opening osxphotos per photo (when the cache is warm).
3. Compare per-field using the field authority policy above.
4. Set `meta_last_harmonized_at` on completion.

**Flags:**
- `bp sync-metadata --refresh-flickr` — re-fetches all from Flickr API and updates cache before comparing (the current default behaviour, now opt-in).
- `bp sync-metadata --refresh-photos` — re-reads Photos library before comparing.
- `bp sync-metadata` with no flags — reads from DB cache only; fast path.

**Files to change:**
- `flickr/metadata_puller.py` — replace live-fetch with DB-read; keep live-fetch behind `--refresh-*` flags
- `flickr/sync_metadata.py` — wire up `--refresh-flickr` / `--refresh-photos` flags

**Completion criteria:** `bp sync-metadata` with a warm cache completes in under 60 seconds for 71k photos.

---

### Phase 4 — Scanner writes Photos metadata to DB
*Depends on Phase 1. Independent of Phases 2 & 3.*

Add a Photos metadata pass to `bp scan` that reads `title`, `description`, and `keywords` from the Photos library for every photo with a `uuid` and writes them to `photos_title`, `photos_description`, `photos_tags`. Sets `meta_synced_photos_at`.

This is the Apple Photos analogue of Phase 2. Because Apple Photos has no `lastupdate` equivalent, the scanner detects changes by comparing incoming values against the stored `photos_*` columns and logging differences.

**Files to change:**
- `poller/scanner.py` (or a new `poller/metadata_scanner.py`) — add keywords/title/description to the per-photo scan pass
- `bp` — wire up if a new sub-command is added

**Completion criteria:** after `bp scan`, `photos_title`/`photos_description`/`photos_tags` are populated for all photos with a `uuid`.

---

### Phase 5 — Canonical columns and conflict resolution UI
*Depends on Phases 1–4.*

Add `canonical_title`, `canonical_description`, `canonical_tags` columns to the DB. These hold the resolved "what should be on both sides" value after conflict resolution.

**Do not introduce canonical columns until Phases 1–4 are complete.** Introducing them earlier risks collapsing the source-distinct representation before the sync engine is trustworthy.

The sync engine (run as part of `bp sync-metadata`) sets canonical values per the field authority matrix:
- Clear win (one side empty, other has value) → canonical = the non-empty value
- Both agree → canonical = either (they're equal)
- Both differ → write to `metadata_conflicts`; canonical stays NULL until resolved

Extend the reviewer UI `/conflicts` page to:
- Show `flickr_*` vs `photos_*` side-by-side per field
- "Use Flickr" / "Use Photos" / "Skip for now" buttons per field (not per photo — fields are resolved individually)
- Resolving a field writes the chosen value to `canonical_*` and marks that conflict record resolved

**Per-field push tracking:** Add `canonical_pushed_to_flickr_at` and `canonical_pushed_to_photos_at` timestamps (or per-field boolean flags) so the reconcile step knows exactly what still needs pushing. This ensures idempotency: re-running reconcile after a partial failure pushes only unconfirmed fields.

**Files to change:**
- `db/schema.sql` + new migration (`migrate_009_canonical_metadata.py`) — add `canonical_*` columns and push-tracking fields
- `flickr/metadata_puller.py` — write canonical values as part of sync
- `reviewer/app.py` — extend `/conflicts` API routes
- `reviewer/templates/conflicts.html` — extend UI (or new page)

---

### Phase 6 — bp reconcile pushes metadata bidirectionally
*Depends on Phase 5.*

Extend `bp reconcile` with two modes:

**Validate mode** (`bp reconcile`, existing behaviour extended):
- Detect photos where `canonical_*` differs from `flickr_*` or `photos_*`.
- Report mismatches; no writes.

**Harmonize mode** (`bp reconcile --fix`, extended):
- Push `canonical_title`/`canonical_description`/`canonical_tags` to Flickr where `canonical_pushed_to_flickr_at` is NULL.
- Push to Apple Photos via photoscript where `canonical_pushed_to_photos_at` is NULL.
- On confirmed success, set the push timestamp.
- On failure, leave timestamp NULL so the next run retries.

**Tag limit (Flickr → 75 tags):** See tag model section above.

**Files to change:**
- `poller/reconcile.py` — add metadata validation and harmonize steps
- `flickr/flickr_client.py` — add `set_photo_meta(flickr_id, title, description)` if not present

---

### Phase 7 — Scheduled sync (cron / launchd)
*Depends on Phases 2–4.*

Add a `bp cron` command (or a `launchd` plist installer) that schedules:
- `bp poll` — daily (refreshes Flickr cache, picks up new uploads)
- `bp scan` — weekly (refreshes Photos cache; slower due to library open)
- `bp sync-metadata` — after each poll completes (fast once cache is warm)
- `bp reconcile --fix` — after each sync-metadata (pushes resolved canonical values)

The reviewer dashboard should show "Flickr cache: N hours old" and "Photos cache: N hours old" using `meta_synced_flickr_at` / `meta_synced_photos_at`.

**Files to change:**
- New `bp cron` sub-command or `launchd/com.bluepearmain.sync.plist`
- `reviewer/app.py` — expose last-sync timestamps to dashboard template
- `reviewer/templates/dashboard.html` — show staleness indicators

---

## Data flow summary

```
bp poll (daily)
  └─► flickr_title, flickr_description, flickr_tags,
      flickr_last_updated, meta_synced_flickr_at

bp scan (weekly)
  └─► photos_title, photos_description, photos_tags,
      meta_synced_photos_at

bp sync-metadata (after poll, reads from DB — no API calls)
  ├─► writes canonical_* where unambiguous (per field authority matrix)
  ├─► writes metadata_conflicts where both sides differ
  └─► sets meta_last_harmonized_at

/conflicts UI (human, on demand)
  └─► resolves per-field conflicts → writes canonical_*

bp reconcile --fix (after sync-metadata)
  ├─► pushes canonical_* → Flickr (title, description, tags ≤75)
  │     └─► sets canonical_pushed_to_flickr_at on confirmed success
  └─► pushes canonical_* → Apple Photos (via photoscript)
        └─► sets canonical_pushed_to_photos_at on confirmed success
```

---

## What is NOT in scope

- Real-time change detection (webhooks, file-system watchers). Everything is pull-based on a schedule.
- Auto-resolution of conflicts. All ambiguous cases go to the review queue.
- Album sync (handled separately; see `docs/album-metadata-sync.md`).
- Privacy/visibility changes (handled by the existing review → reconcile flow).

---

## Migration numbering

As of writing, migrations 001–007 exist. This work will consume:
- `migrate_008_metadata_cache.py` — Phase 1 (`flickr_*`/`photos_*` columns + timestamps)
- `migrate_009_canonical_metadata.py` — Phase 5 (`canonical_*` columns + push-tracking fields)
