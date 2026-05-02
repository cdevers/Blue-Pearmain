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

**Divergence direction rule (tags):** When one tag set is a strict superset of the other, always propose syncing *upward* — adding the missing tags to the smaller side. Never propose removing tags from the larger side to match the smaller. Tag sets only grow through the sync engine; removal requires explicit human action outside this system. Concretely: if Flickr has `[nature, landscape, travel]` and Photos has `[nature]`, the proposal is "add `landscape` and `travel` to Photos", not "remove `landscape` and `travel` from Flickr".

**Hash collision risk:** SHA-256 collisions are astronomically unlikely but theoretically possible. The hash is a fast-path filter only — if hashes match, the system skips full comparison. In `--verbose` mode, a sample of "hash-equal" records can be spot-checked against the full JSON to verify. No special handling is required in normal operation.

---

## Proposals table

The central new DB table. The sync engine writes proposals here; the apply step reads from here. Nothing is written to either Flickr or Photos without a proposal record.

```sql
CREATE TABLE IF NOT EXISTS metadata_proposals (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    photo_id                INTEGER NOT NULL REFERENCES photos(id),
    field                   TEXT NOT NULL,   -- 'title', 'description', 'tags'
    proposed_value          TEXT,            -- serialized (JSON for tags, plain text for others)
    source                  TEXT NOT NULL,   -- 'flickr' | 'photos' | 'manual'
    target                  TEXT NOT NULL,   -- 'flickr' | 'photos'
    conflict_type           TEXT NOT NULL,   -- 'non_conflict' | 'divergence' | 'collision'
    source_hash_at_creation TEXT,            -- hash of source field when proposal was created
    target_hash_at_creation TEXT,            -- hash of target field when proposal was created
    status                  TEXT NOT NULL DEFAULT 'pending',
                                             -- 'pending' | 'applied' | 'rejected' | 'superseded'
    created_at              TEXT NOT NULL,   -- ISO8601
    resolved_at             TEXT,            -- ISO8601; set when status leaves 'pending'
    resolution_note         TEXT             -- optional human note
);
```

**Proposal identity key:** `(photo_id, field, proposed_value, target, source)`. All five must match for two proposals to be considered duplicates. Including `source` prevents ambiguity if the same value is independently proposed from both sides.

**Idempotency and lifecycle rules:**

1. **Deduplication:** Before inserting, check for an existing `pending` proposal with the same identity key. If found, skip the insert.

2. **Staleness / supersession:** A proposal is stale when `source_hash_at_creation != current_source_hash` for that field. On this condition, mark the existing `pending` proposal `superseded` and generate a fresh one. Use hashes, not timestamps — timestamp changes (e.g. a re-fetch that returns the same value) must not trigger supersession.

3. **Rejection persistence:** A `rejected` proposal is not regenerated unless the source hash changes. The sync engine checks: if a `rejected` proposal exists for `(photo_id, field)` and `source_hash_at_creation == current_source_hash`, skip proposal generation entirely.

4. **Apply-time staleness re-check (required):** At the moment of applying a proposal, re-check `current_source_hash == proposal.source_hash_at_creation`. If they differ, refuse to apply and mark the proposal `superseded`. Do not rely solely on the proposal-generation check — the source may have changed between generation and apply.

5. **Apply-time target drift check (required):** Also at apply time, re-check `current_target_hash == proposal.target_hash_at_creation`. If they differ, the target has been independently edited since the proposal was created. Mark the proposal `superseded` and re-run the sync engine for that photo (it will reclassify, likely as a collision). This prevents silently overwriting newer user edits on the target side.

6. **No retroactive proposals on first migration:** When the schema migration runs, set `meta_last_harmonized_at = NOW()` for all existing rows. The sync engine treats this as "assumed in sync at migration time" — it is not a verified sync state, but it suppresses the initial noise burst. Future changes will generate proposals organically. Document this clearly in the migration script.

7. **Proposal table pruning:** Applied, rejected, and superseded proposals accumulate indefinitely. A periodic `bp db prune-proposals --older-than 90d` command (or equivalent) will eventually be needed. Not required now. ([GH #6](https://github.com/cdevers/Blue-Pearmain/issues/6))

**UX / workflow:**
- The reviewer UI `/proposals` page (new) lists pending proposals grouped by conflict type.
- **Non-conflicts** can be bulk-approved ("apply all tag additions to Photos").
- **Divergences** are listed with recommended action pre-selected; one click to confirm.
- **Collisions** require explicit side-selection per field.
- Proposals share a grouping key of `(field, target)` for batch operations — e.g. "apply all pending tag additions to Photos" in one action.
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

### Phase 1 — DB schema: cache both sides ✓ done
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
- `db/migrations/migrate_007_metadata_cache.py`

**Completion criteria:** columns and table exist; no behaviour changes yet.

---

### Phase 2 — Poller writes Flickr metadata to DB ✓ done
*Depends on Phase 1.*

Stop discarding `flickr_title`, `flickr_description`, `flickr_tags` in `poller.py`. Write them into the new columns. Capture `lastupdate` into `flickr_last_updated`. Compute and store `flickr_tags_hash`. Set `meta_synced_flickr_at`.

**Incremental refresh (nice-to-have):** `bp poll --sort updated` fetches photos sorted by `date-updated` descending, enabling a daily job to skip unchanged photos. Not required for Phase 2 completion.

**Files to change:**
- `poller/poller.py`

**Completion criteria:** after `bp poll --backfill`, `flickr_tags`/`flickr_tags_hash`/`flickr_last_updated` populated for all photos with `flickr_id`.

---

### Phase 3 — Scanner writes Photos metadata to DB ✓ done
*Depends on Phase 1. Independent of Phase 2.*

Add a Photos metadata pass to `bp scan` that reads title, description, and keywords from the Photos library for every photo with a `uuid`. Compute and store `photos_tags_hash`. Set `meta_synced_photos_at`. Detect changes by comparing incoming values against stored `photos_*` columns; log differences.

**Files to change:**
- `poller/scanner.py` (or new `poller/metadata_scanner.py`)
- `bp` if a new sub-command

**Completion criteria:** after `bp scan`, `photos_tags`/`photos_tags_hash` populated for all photos with `uuid`.

---

### Phase 4 — Sync engine: diff and generate proposals (tags first) ✓ done
*Depends on Phases 1–3. Tags only in this phase.*

`bp sync-metadata` runs the drift filter, compares tag hashes, classifies
divergences, and writes to `metadata_proposals`. No API calls. 70,946 photos
processed in ~2.5 s; 60,220 hash matches (already in sync), ~11,226 proposals.

- **Drift filter:** selects photos where both caches populated and
  `meta_last_harmonized_at < MAX(COALESCE(flickr_last_updated, meta_synced_flickr_at), meta_synced_photos_at)` (or NULL).
- **Fast path:** hash match → set `meta_last_harmonized_at`, skip.
- **Classification:** non_conflict / divergence / collision per architecture rules.
- **Idempotency:** pending dup → skip; stale hash → supersede + new; rejected same hash → skip.
- **`--refresh-flickr`:** re-fetches Flickr cache via API before syncing (opt-in).
- Batch commits every 500 photos.

**Files to change:**
- `flickr/metadata_puller.py`
- `flickr/sync_metadata.py`

**Completion criteria:** `bp sync-metadata` with warm cache completes in under 60 seconds for 71k photos; proposals appear in `metadata_proposals`.

---

### Phase 5 — Proposal review UI and apply step (tags) ✓ done
*Depends on Phase 4.*

`/proposals` page in the reviewer UI groups by conflict type (collisions first, then
divergences, then non-conflicts). 11,226 proposals generated on first run across 70,946
photos (10,226 non-conflicts, 1,000 collisions, 0 divergences).

- **Non-conflicts:** bulk-approve button applies all in one step.
- **Divergences:** single Approve/Reject per card.
- **Collisions:** "Use Flickr" / "Use Photos" buttons per card; approving either side
  auto-rejects the sibling proposal. Collision pairs are deduplicated in the display
  (only flickr→photos direction shown; photos→flickr sibling resolved automatically).
- **Keyboard shortcuts:** `p` = approve / use Photos, `f` = use Flickr (collisions only),
  `x` = reject / skip, `j`/`k` = navigate.
- `bp reconcile --apply-proposals` applies pending non-conflict proposals from the CLI.
- `flickr/proposal_applier.py`: apply-time staleness and target-drift re-checks, photoscript
  verify-after-write, 75-tag truncation for Flickr target.

**Files changed:**
- `flickr/proposal_applier.py` (new)
- `reviewer/app.py` + `reviewer/templates/proposals.html` (new)
- `poller/reconcile.py`

---

### Phase 6 — Manual merge editor for collision resolution ([GH #1](https://github.com/cdevers/Blue-Pearmain/issues/1))
*Depends on Phase 5.*

Collisions currently require picking one side wholesale ("Use Flickr" or "Use Photos").
This phase adds an inline merge editor so users can construct a custom tag set from
elements of both sides — or add entirely new tags — without accepting either side entirely.

**UI on the `/proposals` page, collision cards only:**
- Expand/collapse inline editor triggered by an **Edit ✎** button (or `m` keyboard shortcut).
- Both tag sets rendered as toggleable chips: checked = include in merged result.
- All tags from both sides pre-checked by default; user unchecks to exclude.
- Free-text input to add new tags not present on either side.
- "Apply merged →" button sends the custom value to both Flickr and Photos simultaneously.
- Merged result stored with `source = 'manual'` in `metadata_proposals` (already supported
  by the schema).

**Backend:**
- New endpoint `POST /api/proposals/<id>/apply-manual` accepting `{"value": [...tags...]}`.
- Applies the custom tag list to both targets; marks proposal `applied` and
  auto-rejects the collision sibling.
- Same staleness and drift re-checks as the regular apply path.

**Keyboard shortcut:** `m` on a selected collision card opens the inline editor.
Non-collision cards ignore `m`.

**Files to change:**
- `reviewer/app.py`
- `reviewer/templates/proposals.html`
- `flickr/proposal_applier.py` (extend to accept an explicit value override)

**Completion criteria:** user can resolve any tag collision without accepting either side
wholesale; merged result is applied to both Flickr and Photos in a single action.

---

### Phase 7 — Expand to title and description ✓ done
*Depends on Phase 5. Same pipeline, new fields.*

Extended the sync engine, apply step, and proposals UI to cover
`flickr_title`/`photos_title` and `flickr_description`/`photos_description`.
Title/description use string equality (no hash fast-path needed); text fields
produce only `non_conflict` or `collision` proposals (no divergence — there is
no superset relationship for plain text). After first `--force` run: 31 title
non-conflicts, 163 description non-conflicts, 1,295 Photos→Flickr description
non-conflicts, 28 description collision pairs.

- `_classify_text_field` parallels `_classify_tags` for string values.
- `_harmonise_one` now fetches and diffs all four text columns on every run.
- Bulk end-of-run supersede extended with string-equality checks for title/description.
- `apply_proposal` dispatch extended; `_apply_text_to_photos` (photoscript
  `photo.title`/`photo.description`) and `_apply_text_to_flickr` (`set_meta`)
  added to `proposal_applier.py`.
- `/proposals` UI: text-diff card layout for title/description, field badge
  (TITLE/DESCRIPTION) in card header, sub-sorted within conflict-type groups.

**`canonical_*` columns deferred** — not needed until Phase 6 (manual merge
editor). Will be added in `migrate_009_canonical_metadata.py` when Phase 6 ships.

**Files changed:**
- `flickr/metadata_puller.py`, `flickr/proposal_applier.py`
- `db/db.py`
- `reviewer/templates/proposals.html`

---

### Phase 8 — Scheduled sync (launchd) ✓ done
*Depends on Phases 2–5.*

`bp pipeline` chains sync-metadata + apply in a single Python-import call (no subprocess
overhead): runs `_select_drift_filtered` + `run_sync_engine` to generate proposals, then
`apply_batch(conflict_types=["non_conflict"])` to auto-apply them. Collision proposals
remain pending for human review.

`config/com.cdevers.blue-pearmain.pipeline.plist` runs `bp all` every 6 hours via launchd
([GH #13](https://github.com/cdevers/Blue-Pearmain/issues/13)). `bp all` is the full
maintenance sequence: `scan --all`, `poll`, `thumbs`, `pipeline`, `reconcile --fix`,
`sync-albums`, `checkpoint`. Each step runs independently; failures are logged and the
sequence continues. The existing `com.cdevers.blue-pearmain.poller.plist` continues to
run `bp poll` every hour for faster Flickr catch-up between the 6-hour cycles.

Dashboard freshness indicators: `db.stats()` now returns `flickr_cache_age_hours` and
`photos_cache_age_hours` (computed from `MAX(meta_synced_flickr_at/photos_at)`).
Dashboard shows "Flickr cache: Xh old" / "Photos cache: Xh old" with color coding
(green < 12h, amber < 48h, red ≥ 48h).

**Files changed:**
- `bp` (`cmd_pipeline`, `pipeline` subparser)
- `flickr/proposal_applier.py` (`apply_batch` limit=0 support)
- `db/db.py` (`stats()` cache-age fields)
- `config/com.cdevers.blue-pearmain.pipeline.plist` (new)
- `reviewer/templates/dashboard.html` (freshness row)

---

## Data flow summary

```
bp poll (daily)
  └─► flickr_title, flickr_description, flickr_tags, flickr_tags_hash,
      flickr_last_updated, meta_synced_flickr_at

bp scan (weekly)
  └─► photos_title, photos_description, photos_tags, photos_tags_hash,
      meta_synced_photos_at

bp sync-metadata (after poll — reads DB only, no API calls)
  ├─► drift filter: photos where harmonized < max(flickr_last_updated, photos_synced)
  ├─► hash comparison → skip unchanged photos
  ├─► full set diff → classify non_conflict / divergence / collision
  ├─► write metadata_proposals (deduplicated)
  └─► set meta_last_harmonized_at

/proposals UI (human, on demand)
  ├─► bulk-approve non-conflicts
  ├─► confirm divergences
  ├─► pick a side for collisions (Use Flickr / Use Photos)
  └─► manual merge editor for collisions (Phase 6) — custom tag set → both targets

bp reconcile --fix (after sync-metadata)
  ├─► apply approved proposals → Flickr (tags ≤75) → mark applied on confirmed success
  └─► apply approved proposals → Photos (via photoscript, verify after write)
```

---

### Phase 9 — Deferred cleanup (non-urgent)

Two small housekeeping items identified during post-migration review. Neither affects
correctness of the current pipeline; both are quality-of-life for future maintainability.

**1. Normalize `_normalise_tags` in the legacy path.** ([GH #7](https://github.com/cdevers/Blue-Pearmain/issues/7))
`pull_photo_metadata` (reachable only via `bp sync-metadata --refresh-flickr`) calls
`_normalise_tags` which uses `.lower()` instead of `.casefold()` and keeps punctuation
(hyphens, spaces). The active pipeline (`run_sync_engine` / `_classify_tags`) uses
`casefold()` + NFC + `isalnum()`, matching the hash functions in `poller.py` and
`scanner.py`. The legacy function should be updated to match, or the dead code removed
if `--refresh-flickr` is eventually retired. Not a current bug (that path is dry-run
only); just a latent inconsistency.

**2. Implement `bp db prune-proposals`.** ([GH #6](https://github.com/cdevers/Blue-Pearmain/issues/6))
Applied, superseded, and rejected proposals accumulate indefinitely. A periodic prune
command (e.g. `bp db prune-proposals --older-than 90d`) will eventually be needed to
prevent unbounded table growth and keep query performance stable. Not urgent at current
data volumes, but should be done before the table grows into the millions of rows.

**Files to change:**
- `flickr/metadata_puller.py` (`_normalise_tags` cleanup)
- `bp` + `db/db.py` (new `db prune-proposals` subcommand)

---

## What is NOT in scope

- Real-time change detection (webhooks, file-system watchers). Everything is pull-based on a schedule.
- Auto-resolution of collisions. All ambiguous cases go to the review queue.
- Album sync (handled separately; see `docs/album-metadata-sync.md`).
- Privacy/visibility changes (handled by the existing review → reconcile flow).
- Tag synonym merging or semantic deduplication.

---

## Migration numbering

- `migrate_001` through `migrate_007` — pre-existing
- `migrate_007_metadata_cache.py` — Phase 1 (`flickr_*`/`photos_*` columns, `metadata_proposals` table) ✓
- `migrate_008_review_queue_index.py` — performance: covering index on `photos(privacy_state, date_taken DESC, id DESC)` ✓
- `migrate_009_canonical_metadata.py` — Phase 7 (`canonical_*` columns, push-tracking timestamps)
