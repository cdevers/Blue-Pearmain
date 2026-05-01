# Metadata Sync Architecture

**Goal:** Make the local SQLite database a cache of "last known state" for both Flickr and Apple Photos metadata (title, description, tags). Once both sides are in the DB, a lightweight sync engine can detect changes, generate reviewable proposals, and push confirmed values to either side — without hitting the Flickr API per-photo on every run.

**Ultimate vision:** An eventually-consistent bridge. When metadata changes on Flickr or in Apple Photos, the other side reflects it within a configurable window (hours to days via scheduled jobs), with manual conflict resolution for cases where both sides changed independently.

**Implementation order:** Start with tags only. Tags are the highest-volume field; getting tag sync right validates the whole proposal/apply pipeline. Expand to title and description once the pipeline is proven.

---

## Design principles

Non-negotiable constraints that shape every implementation decision:

1. **No silent writes.** Nothing changes on either Flickr or Apple Photos without an explicit, logged operation. Background jobs detect and cache; they do not write.
2. **Explicit state only.** The DB reflects confirmed state. A field is only marked "applied" after the write is confirmed, not when it is queued.
3. **Proposals, not direct writes.** The sync engine produces proposals (`pending` records in `metadata_proposals`). Proposals are reviewed and applied separately. No change bypasses the proposal lifecycle.
4. **Manual conflict resolution.** When both sides have changed independently, the system surfaces the conflict for human decision; it never auto-resolves.
5. **Idempotent operations.** Applying the same proposal twice does nothing. Rejecting a proposal suppresses it until state genuinely changes.
6. **Separate validation from mutation.** `bp reconcile` (validate mode) detects mismatches. `bp reconcile --fix` (harmonize mode) applies confirmed proposals. Keep them separate.
7. **Verify after Photos writes.** Apple Photos APIs are less reliable than Flickr's. After writing via photoscript, re-read the value to confirm it was applied. Mark as applied only on confirmed success.

---

## Field authority matrix

| Field | Authority | Default direction | Notes |
|-------|-----------|-------------------|-------|
| `tags` | Merged / manual | Both sides contribute; conflicts reviewed | Highest priority; implement first |
| `title` | Neither (manual) | Conflict → review queue | Either side may have been edited by the user |
| `description` | Neither (manual) | Conflict → review queue | Either side may have been edited by the user |
| `date_taken` | Apple Photos (EXIF) | Photos → Flickr | EXIF is ground truth; Flickr's copy should match |
| `permissions` | DB / policy | DB → Flickr | Existing review → reconcile flow; unchanged |
| `albums` | Flickr (primary) | Flickr → Photos (read-only reflection) | Handled separately; see `docs/album-metadata-sync.md` |

*"Neither (manual)"* means: if both sides are non-empty and different, the system records a conflict and waits for a human decision.

---

## Change detection semantics

Precise per-field rules prevent unnecessary churn and noisy proposals.

### Tags
Compare as **normalized sets** (see tag normalization below). Two tag sets are considered equal if their normalized forms are identical sets.

- Tag order change only → **no change**
- Case change only (e.g. `Nature` → `nature`) → **no change** (normalized away)
- Whitespace-only difference → **no change** (normalized away)
- Tags added on one side → **non-conflict proposal** (see conflict classification)
- Tags removed on one side → **divergence** (requires review)
- Tags completely different → **collision** (requires review)

### Title
Compare after trimming leading/trailing whitespace. Any remaining difference is a detected change. Case-only changes are **not** normalized away for titles (a user may have intentionally changed `"my photo"` to `"My Photo"`).

### Description
Compare after trimming leading/trailing whitespace. Any remaining difference is a detected change.

---

## Tag normalization

Canonical form applied before all comparisons and before storing to DB:

1. **Trim** leading and trailing whitespace.
2. **Lowercase** (Unicode-aware: `"Ñoño".lower()`).
3. **NFC normalize** (Python: `unicodedata.normalize("NFC", tag)`).
4. **Deduplicate** within a set (case-insensitive exact matches only; near-synonyms are not merged).
5. **No delimiter splitting** — tags are stored as discrete items; commas or spaces within a tag string are preserved as part of the tag (Flickr and Photos both use discrete tag objects, not delimiter-separated strings in their APIs).

Original casing from the source is preserved in storage (`flickr_tags`, `photos_tags`). Normalization is applied only for comparison, and to the `canonical_tags` column when writing the resolved value.

**Tag hash:** Store `flickr_tags_hash` and `photos_tags_hash` (SHA-256 of the sorted normalized tag set, as a hex string). The sync engine filters on `flickr_tags_hash != photos_tags_hash` before doing the full set comparison, avoiding per-photo JSON parsing at scale.

---

## Conflict classification

Three distinct categories, each with a defined action:

| Type | Definition | Example | Action |
|------|-----------|---------|--------|
| **Non-conflict** | One side has a value; the other is empty | Photos has no tags; Flickr has tags | Auto-generate proposal; no human review required |
| **Divergence** | Both sides have values; one side's value is a strict extension of the other (tags: one set is a superset) | Flickr has `[nature, landscape]`; Photos has `[nature]` | Generate proposal; recommend auto-apply but allow review |
| **Collision** | Both sides have values and neither is a superset of the other | Flickr title: `"Sunset"`, Photos title: `"Golden Hour"` | Generate conflict proposal; **requires human resolution** |

In the proposals table, `conflict_type` is always one of `non_conflict`, `divergence`, or `collision`. The UI surfaces collisions prominently; non-conflicts can be batch-approved.

---

## Proposals table

The central new DB table. The sync engine writes proposals here; the apply step reads from here. Nothing is written to either Flickr or Photos without a proposal record.

```sql
CREATE TABLE IF NOT EXISTS metadata_proposals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    photo_id        INTEGER NOT NULL REFERENCES photos(id),
    field           TEXT NOT NULL,         -- 'title', 'description', 'tags'
    proposed_value  TEXT,                  -- serialized (JSON for tags, plain text for title/description)
    source          TEXT NOT NULL,         -- 'flickr' | 'photos' | 'manual'
    target          TEXT NOT NULL,         -- 'flickr' | 'photos' | 'both'
    conflict_type   TEXT NOT NULL,         -- 'non_conflict' | 'divergence' | 'collision'
    status          TEXT NOT NULL DEFAULT 'pending',
                                           -- 'pending' | 'applied' | 'rejected' | 'superseded'
    created_at      TEXT NOT NULL,         -- ISO8601
    resolved_at     TEXT,                  -- ISO8601; set when status leaves 'pending'
    resolution_note TEXT                   -- optional human note
);
```

**Idempotency rules:**
- Before inserting a new proposal, check if a `pending` proposal already exists for `(photo_id, field, proposed_value, target)`. If so, do not insert a duplicate.
- If state changes (e.g. `flickr_tags` is updated by a new poll), any existing `pending` proposal for that `(photo_id, field)` is marked `superseded` and a fresh proposal is generated.
- A `rejected` proposal is not regenerated unless the underlying source value changes (compare against `proposed_value` at rejection time).

**UX / workflow:**
- The reviewer UI `/proposals` page (new) lists pending proposals grouped by conflict type.
- **Non-conflicts** can be bulk-approved ("apply all tag additions to Photos").
- **Divergences** are listed with a recommended action pre-selected; one click to confirm.
- **Collisions** require explicit side-selection per field.
- CLI: `bp sync-metadata --list-proposals` prints pending proposals in tabular form for headless review.

---

## Drift detection

A photo is considered **in sync** when:

```
meta_last_harmonized_at >= max(flickr_last_updated, meta_synced_photos_at)
```

If `flickr_last_updated` advances (a new Flickr poll fetched a newer `lastupdate`), the photo needs re-harmonization. If `meta_synced_photos_at` advances (Photos was re-scanned), same. The sync engine processes only photos where this condition is false — avoiding redundant work on every run.

---

## Change tracking columns

| Column | Meaning |
|--------|---------|
| `meta_synced_flickr_at` | When we last successfully fetched Flickr metadata |
| `flickr_last_updated` | `lastupdate` from Flickr API — when Flickr last modified this photo |
| `meta_synced_photos_at` | When we last successfully read Photos metadata |
| `meta_last_harmonized_at` | When the sync engine last processed this photo |
| `flickr_tags_hash` | SHA-256 of sorted normalized Flickr tag set |
| `photos_tags_hash` | SHA-256 of sorted normalized Photos tag set |

---

## Current state (before this work)

- `bp sync-metadata` fetches title/description/tags from Flickr **live** for every photo on every run (~18 hours for 71k photos).
- The poller fetches Flickr title/description/tags but discards them before writing to the DB (`poller.py` lines 425–432).
- Apple Photos metadata is read live via osxphotos during sync, not cached.
- The DB has no stored representation of either side's current metadata state.
- Conflicts are detected and stored in `metadata_conflicts` but only relative to the live values at the time of the run, with no proposal lifecycle.

---

## Target state

```
Flickr API  ──poll──►  flickr_title / flickr_description / flickr_tags / flickr_tags_hash
                                        │
                                  sync engine (per-field, drift-filtered)
                                  generates metadata_proposals
                                        │
Apple Photos ──scan──► photos_title / photos_description / photos_tags / photos_tags_hash
```

The sync engine runs the drift filter, classifies each out-of-sync field, and writes proposals. The reviewer UI and/or `bp reconcile --fix` applies confirmed proposals. No API calls during sync once the cache is warm.

---

## Phases

### Phase 1 — DB schema: cache both sides
*Prerequisite for everything else. Safe to ship alone.*

Add columns to the `photos` table and create the `metadata_proposals` table:

**`photos` table additions:**

| Column | Type | Description |
|--------|------|-------------|
| `flickr_title` | TEXT | Last title fetched from Flickr |
| `flickr_description` | TEXT | Last description fetched from Flickr |
| `flickr_tags` | TEXT | JSON array — last tags fetched from Flickr (original casing) |
| `flickr_tags_hash` | TEXT | SHA-256 of sorted normalized Flickr tag set |
| `flickr_last_updated` | TEXT | ISO8601 — Flickr's `lastupdate` for this photo |
| `photos_title` | TEXT | Last title read from Apple Photos |
| `photos_description` | TEXT | Last description read from Apple Photos |
| `photos_tags` | TEXT | JSON array — last keywords read from Apple Photos (original casing) |
| `photos_tags_hash` | TEXT | SHA-256 of sorted normalized Photos tag set |
| `meta_synced_flickr_at` | TEXT | ISO8601 — when we last fetched from Flickr |
| `meta_synced_photos_at` | TEXT | ISO8601 — when we last read from Photos |
| `meta_last_harmonized_at` | TEXT | ISO8601 — when sync engine last ran for this photo |
| `tags_truncated_for_flickr` | INTEGER | Boolean — canonical tags exceeded 75 on last push |

**New `metadata_proposals` table:** as defined in the proposals section above.

**Files to change:**
- `db/schema.sql`
- `db/migrations/migrate_008_metadata_cache.py`

**Completion criteria:** columns and table exist; no behaviour changes yet.

---

### Phase 2 — Poller writes Flickr metadata to DB
*Depends on Phase 1.*

Stop discarding `flickr_title`, `flickr_description`, `flickr_tags` in `poller.py`. Write them into the new columns. Capture `lastupdate` into `flickr_last_updated`. Compute and store `flickr_tags_hash`. Set `meta_synced_flickr_at`.

**Incremental refresh (nice-to-have):** `bp poll --sort updated` fetches photos sorted by `date-updated` descending, enabling a daily job to skip unchanged photos. Not required for Phase 2 completion.

**Files to change:**
- `poller/poller.py`

**Completion criteria:** after `bp poll --backfill`, `flickr_tags`/`flickr_tags_hash`/`flickr_last_updated` populated for all photos with `flickr_id`.

---

### Phase 3 — Scanner writes Photos metadata to DB
*Depends on Phase 1. Independent of Phase 2.*

Add a Photos metadata pass to `bp scan` that reads title, description, and keywords from the Photos library for every photo with a `uuid`. Compute and store `photos_tags_hash`. Set `meta_synced_photos_at`. Detect changes by comparing incoming values against stored `photos_*` columns; log differences.

**Files to change:**
- `poller/scanner.py` (or new `poller/metadata_scanner.py`)
- `bp` if a new sub-command

**Completion criteria:** after `bp scan`, `photos_tags`/`photos_tags_hash` populated for all photos with `uuid`.

---

### Phase 4 — Sync engine: diff and generate proposals (tags first)
*Depends on Phases 1–3. Tags only in this phase.*

Rewrite `bp sync-metadata` to:
1. Run the drift filter: select photos where `meta_last_harmonized_at < max(flickr_last_updated, meta_synced_photos_at)` (or NULL).
2. For each such photo, compare `flickr_tags_hash` vs `photos_tags_hash`.
3. If hashes differ, expand to full set comparison and classify (`non_conflict`, `divergence`, `collision`).
4. Write a proposal to `metadata_proposals` (deduplicated per idempotency rules).
5. Set `meta_last_harmonized_at`.

No Flickr API calls. No writes to Photos or Flickr. Pure DB reads → proposal writes.

`bp sync-metadata --refresh-flickr` re-fetches from the Flickr API and updates the cache before running the sync engine (the old default behaviour, now opt-in).

**Files to change:**
- `flickr/metadata_puller.py`
- `flickr/sync_metadata.py`

**Completion criteria:** `bp sync-metadata` with warm cache completes in under 60 seconds for 71k photos; proposals appear in `metadata_proposals`.

---

### Phase 5 — Proposal review UI and apply step (tags)
*Depends on Phase 4.*

Add `/proposals` page to the reviewer UI:
- Group by conflict type (collisions first, then divergences, then non-conflicts).
- Non-conflicts: bulk-approve button ("apply all tag additions to Photos").
- Divergences: recommended action pre-selected; one click to confirm.
- Collisions: explicit side-selection per field.

Add `bp reconcile --fix` apply step for tags:
- For each `pending` proposal with `target = 'photos'`: write via photoscript, verify by re-reading, mark `applied` only on confirmed success.
- For each `pending` proposal with `target = 'flickr'`: call Flickr API, mark `applied` on success.
- Failures leave status as `pending`; the next run retries automatically.
- Respects 75-tag limit (see truncation policy above).

**Files to change:**
- `reviewer/app.py` + `reviewer/templates/proposals.html`
- `poller/reconcile.py`

**Completion criteria:** tag proposals can be reviewed and applied end-to-end; full pipeline tested.

---

### Phase 6 — Expand to title and description
*Depends on Phase 5. Same pipeline, new fields.*

Extend Phases 2–5 to cover `flickr_title`/`photos_title` and `flickr_description`/`photos_description`. These fields have no hash optimization needed (short strings). Collision handling in the UI becomes the main addition.

**Add `canonical_*` columns here** (not before):
- `canonical_title`, `canonical_description`, `canonical_tags` — the resolved value after a collision is manually resolved.
- `canonical_pushed_to_flickr_at`, `canonical_pushed_to_photos_at` — per-field push confirmation timestamps.

**Files to change:**
- `db/schema.sql` + `db/migrations/migrate_009_canonical_metadata.py`
- All files touched in Phases 2–5, extended for title/description

---

### Phase 7 — Scheduled sync (cron / launchd)
*Depends on Phases 2–5.*

Add a `bp cron` command or `launchd` plist that schedules:
- `bp poll` — daily (refreshes Flickr cache)
- `bp scan` — weekly (refreshes Photos cache)
- `bp sync-metadata` — after each poll (fast drift detection, generates proposals)
- `bp reconcile --fix` — after sync-metadata (applies non-conflict proposals automatically; leaves collisions for the UI)

Reviewer dashboard: show "Flickr cache: N hours old" and "Photos cache: N hours old".

**Files to change:**
- `bp cron` sub-command or `launchd/com.bluepearmain.sync.plist`
- `reviewer/app.py` + `reviewer/templates/dashboard.html`

---

## Data flow summary

```
bp poll (daily)
  └─► flickr_tags, flickr_tags_hash, flickr_last_updated, meta_synced_flickr_at

bp scan (weekly)
  └─► photos_tags, photos_tags_hash, meta_synced_photos_at

bp sync-metadata (after poll — reads DB only, no API calls)
  ├─► drift filter: photos where harmonized < max(flickr_last_updated, photos_synced)
  ├─► hash comparison → skip unchanged photos
  ├─► full set diff → classify non_conflict / divergence / collision
  ├─► write metadata_proposals (deduplicated)
  └─► set meta_last_harmonized_at

/proposals UI (human, on demand)
  ├─► bulk-approve non-conflicts
  ├─► confirm divergences
  └─► manually resolve collisions

bp reconcile --fix (after sync-metadata)
  ├─► apply approved proposals → Flickr (tags ≤75) → mark applied on confirmed success
  └─► apply approved proposals → Photos (via photoscript, verify after write)
```

---

## What is NOT in scope

- Real-time change detection (webhooks, file-system watchers). Everything is pull-based on a schedule.
- Auto-resolution of collisions. All ambiguous cases go to the review queue.
- Album sync (handled separately; see `docs/album-metadata-sync.md`).
- Privacy/visibility changes (handled by the existing review → reconcile flow).
- Tag synonym merging or semantic deduplication.

---

## Migration numbering

As of writing, migrations 001–007 exist. This work will consume:
- `migrate_008_metadata_cache.py` — Phase 1 (`flickr_*`/`photos_*` columns, `metadata_proposals` table)
- `migrate_009_canonical_metadata.py` — Phase 6 (`canonical_*` columns, push-tracking timestamps)
