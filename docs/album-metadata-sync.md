# Album & Metadata Sync: Apple Photos ↔ Flickr

**Blue Pearmain — Architecture Plan**
*Status: Proposed | Author: cdevers | Date: 2026-04-13*

---

## Background

The Blue Pearmain database already cross-references Apple Photos records
(via `osxphotos`) with Flickr records (via the Flickr API), linking them
by a matched `uuid` / `flickr_id` pair in the `photos` table. The reviewer
UI and poller already push tags and privacy permissions to Flickr at review
time. This document specifies a two-phase extension to additionally sync:

- **Phase 1 (Apple Photos → Flickr):** album membership, so that Apple
  Photos albums are mirrored as Flickr photosets.
- **Phase 2 (Flickr → Apple Photos):** metadata (title, description, tags)
  written back to Photos, with mismatch detection and resolution.

---

## Phase 1: Apple Photos Albums → Flickr Photosets

### Goal

When a photo is in an Apple Photos album *and* has a linked `flickr_id`,
ensure it is also added to a corresponding Flickr photoset. This should
happen:

1. **At review time** — when the reviewer approves a photo and pushes it
   to Flickr, also add it to any relevant photosets.
2. **In batch** — for photos already approved and pushed, a
   `bp sync-albums` command reconciles album membership in arrears.

### Data Model Changes

Add two new tables to `db/schema.sql`:

```sql
-- Apple Photos album membership (populated by the scanner)
CREATE TABLE IF NOT EXISTS albums (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    apple_uuid      TEXT NOT NULL UNIQUE,   -- Photos album UUID
    name            TEXT NOT NULL,
    flickr_set_id   TEXT,                   -- NULL until created on Flickr
    flickr_set_url  TEXT,
    created_at      TEXT,
    updated_at      TEXT
);

-- Per-photo album membership
CREATE TABLE IF NOT EXISTS photo_albums (
    photo_id        INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    album_id        INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
    flickr_pushed   INTEGER DEFAULT 0,      -- boolean: added to Flickr photoset?
    pushed_at       TEXT,
    PRIMARY KEY (photo_id, album_id)
);

CREATE INDEX IF NOT EXISTS idx_photo_albums_photo   ON photo_albums(photo_id);
CREATE INDEX IF NOT EXISTS idx_photo_albums_album   ON photo_albums(album_id);
CREATE INDEX IF NOT EXISTS idx_photo_albums_pending ON photo_albums(flickr_pushed)
    WHERE flickr_pushed = 0;
```

Add a migration: `db/migrations/migrate_003_albums.py`.

### Scanner Changes (`poller/scanner.py`)

`osxphotos` already exposes album membership via `photo.albums` (a list of
`AlbumInfo` objects, each with `.uuid` and `.title`). The scanner should:

1. For each photo processed, collect its album list.
2. Upsert each album into the `albums` table (by `apple_uuid`).
3. Upsert rows into `photo_albums` for each (photo, album) pair, setting
   `flickr_pushed = 0` if the row is new.

This is additive — no change to existing scanner outputs or privacy
classification.

> **Note:** Filter out smart albums and system albums (e.g. "Recents",
> "Favourites", "All Photos"). Only user-created albums should be synced.
> `osxphotos` marks smart albums via `album.album_type`; filter to
> `album_type == "Album"` (not `"SmartAlbum"` or `"Folder"`).

### Flickr Client Changes (`flickr/flickr_client.py`)

Add three new methods:

```python
def create_photoset(self, title: str, primary_photo_id: str) -> str:
    """Create a Flickr photoset. Returns the new photoset ID."""
    # flickr.photosets.create

def add_photo_to_photoset(self, photoset_id: str, photo_id: str) -> None:
    """Add a photo to an existing Flickr photoset."""
    # flickr.photosets.addPhoto

def get_photosets(self) -> list[dict]:
    """Return all photosets for the authenticated user."""
    # flickr.photosets.getList
```

All three should use the existing retry/jitter/429-handling infrastructure.

### Push Logic (`flickr/album_pusher.py`) — new file

A new module responsible for the actual sync:

```python
def push_photo_to_albums(db: Database, flickr: FlickrClient, photo_id: int) -> int:
    """
    Given a DB photo row ID, push it to all Flickr photosets corresponding
    to its Apple Photos albums. Creates photosets if they don't exist yet.
    Returns the number of photosets updated.
    """
```

Key behaviour:

- Looks up the photo's `flickr_id` — skips if null (not yet on Flickr).
- Fetches the photo's album rows from `photo_albums` where `flickr_pushed = 0`.
- For each album, checks if `albums.flickr_set_id` is set:
  - If not, calls `create_photoset` using the album name and this photo as
    the primary (if this is the first pushed photo in the set), then
    updates `albums.flickr_set_id`.
  - If already set, calls `add_photo_to_photoset`.
- On success, sets `photo_albums.flickr_pushed = 1` and `pushed_at`.
- Logs failures per photo+album pair; does not abort the batch.

### Reviewer Integration (`reviewer/app.py`)

The existing review approval path already calls tag-push and perm-push.
Extend it to also call `push_photo_to_albums` after a successful perm push:

```python
# Existing flow (simplified):
flickr.set_permissions(flickr_id, public=True)
db.mark_perms_pushed(photo_id)
flickr.add_tags(flickr_id, tags)
db.mark_tags_pushed(photo_id)

# New addition:
album_pusher.push_photo_to_albums(db, flickr, photo_id)
```

This means every photo approved through the reviewer is immediately placed
into its corresponding Flickr photosets as part of the same action.

### CLI: `bp sync-albums`

A new subcommand for backfill and reconciliation of already-reviewed photos:

```
bp sync-albums [--dry-run] [--album NAME] [--limit N]
```

Behaviour:

1. Queries `photo_albums JOIN photos` for rows where:
   - `photo_albums.flickr_pushed = 0`
   - `photos.flickr_id IS NOT NULL`
   - `photos.perms_pushed_flickr = 1` (already public on Flickr)
2. Calls `push_photo_to_albums` for each.
3. Prints a summary: `albums created=N  photos added=N  skipped=N  failed=N`.

Exit codes follow the same convention as `bp reconcile`:
- `0` — all pending entries pushed successfully (or nothing to do)
- `1` — some pushes failed
- `2` — operational error (DB or API unavailable)

### Migration Path

A migration script `db/migrations/migrate_003_albums.py` should:

1. Create the `albums` and `photo_albums` tables (idempotent, using
   `CREATE TABLE IF NOT EXISTS`).
2. Add indexes.
3. Record itself in `schema_migrations`.

No existing rows are modified. The scanner will populate the new tables on
its next run.

---

## Phase 2: Flickr Metadata → Apple Photos

> **Status:** Deferred. Design only — no implementation yet.

### Goal

For photos with a linked `flickr_id`, pull Flickr's title, description, and
tags back into Apple Photos (via `osxphotos`), so that both libraries stay
consistent in the other direction.

### Mapping

| Flickr field  | Apple Photos field | Notes                          |
|---------------|--------------------|--------------------------------|
| Title         | Title              | Stored in Photos DB, not EXIF  |
| Description   | Caption            | Shown in the Info panel        |
| Tags          | Keywords           | Searchable in Photos           |

### Mismatch Handling

Both systems may have independently evolved metadata. The policy is:

- **Flickr wins by default** — if Flickr has a value and Photos doesn't, or
  if they differ, prefer the Flickr value. Rationale: Flickr descriptions
  and tags are often manually curated and more complete than Photos titles.
- **Conflict flagging** — if both sides have non-empty, *different* values,
  record the conflict in the DB (`metadata_conflicts` table, see below) and
  surface it in the reviewer UI rather than writing blindly.
- **Photos-only values** — if Flickr has nothing but Photos does, preserve
  the Photos value and optionally offer to push it up to Flickr.

### Data Model Additions (Phase 2)

```sql
CREATE TABLE IF NOT EXISTS metadata_conflicts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    photo_id        INTEGER NOT NULL REFERENCES photos(id),
    field           TEXT NOT NULL,      -- 'title' | 'description' | 'tags'
    flickr_value    TEXT,
    photos_value    TEXT,
    resolved        INTEGER DEFAULT 0,  -- boolean
    resolution      TEXT,               -- 'flickr' | 'photos' | 'manual'
    resolved_at     TEXT,
    created_at      TEXT
);
```

### Write Path (Phase 2)

`osxphotos` supports writing metadata back to the Photos library via
`PhotoInfo.update()` (available in recent versions), or via the
`osxphotos batch-edit` CLI. The Phase 2 implementation should:

1. Query photos with `flickr_id IS NOT NULL` and unresolved conflicts (or
   no conflicts, ready to write).
2. For each photo, compare Flickr vs. Photos values.
3. Where Flickr wins with no conflict: call `osxphotos` write API to update
   title/caption/keywords.
4. Where there's a conflict: insert into `metadata_conflicts` and skip.
5. Expose conflicts in the reviewer UI as a separate "Conflicts" queue.

> **Risk:** `osxphotos` write support requires Photos to be running and may
> require user interaction on newer macOS versions due to privacy/automation
> permissions. This should be tested carefully before implementation.

---

## File Summary

### Phase 1 — new or modified files

| File | Change |
|------|--------|
| `db/schema.sql` | Add `albums`, `photo_albums` tables and indexes |
| `db/migrations/migrate_003_albums.py` | Migration script |
| `db/db.py` | Add album CRUD methods |
| `poller/scanner.py` | Populate `albums` / `photo_albums` during scan |
| `flickr/flickr_client.py` | Add `create_photoset`, `add_photo_to_photoset`, `get_photosets` |
| `flickr/album_pusher.py` | New: push logic for album sync |
| `reviewer/app.py` | Call `push_photo_to_albums` on approval |
| `bp` | Add `sync-albums` subcommand |
| `tests/test_core.py` | Tests for album DB methods, pusher, CLI |
| `README.md` | Document `sync-albums` command |

### Phase 2 — new or modified files (deferred)

| File | Change |
|------|--------|
| `db/schema.sql` | Add `metadata_conflicts` table |
| `db/migrations/migrate_004_metadata_conflicts.py` | Migration |
| `db/db.py` | Conflict CRUD methods |
| `flickr/metadata_puller.py` | New: compare and write Flickr → Photos |
| `reviewer/app.py` | Conflicts queue UI |
| `bp` | Add `sync-metadata` subcommand |

---

## Open Questions

1. **Folder hierarchy:** Apple Photos supports nested folders containing
   albums. Should these be mirrored as Flickr collections (which can contain
   photosets)? Flickr collections are a Pro feature — leave for later.

2. **Album deletion:** if a photo is removed from an Apple Photos album,
   should it be removed from the Flickr photoset? The safe default is *no*
   — only additive sync in Phase 1. Removal can be added later with an
   explicit `--remove` flag.

3. **Primary photo for new photosets:** `flickr.photosets.create` requires
   a primary photo. Use the oldest photo in the album (by `date_taken`) as
   the primary, since that's the most natural ordering.

4. **osxphotos write permissions (Phase 2):** macOS Automation permissions
   may require a one-time user prompt. Document this clearly in the Phase 2
   implementation.

---

## Instructions for `claude` CLI — Phase 1 Implementation

The following is a prompt you can give to the `claude` CLI tool to begin
implementing Phase 1. Run it from the root of the Blue Pearmain repo:

```
claude
```

Then paste:

---

**Prompt for `claude`:**

> We are implementing Phase 1 of the album sync feature for Blue Pearmain,
> as described in `docs/album-metadata-sync.md`. Please implement the
> following in order, with tests after each step:
>
> **Step 1 — Schema & migration**
> - Add `albums` and `photo_albums` tables and their indexes to
>   `db/schema.sql` exactly as specified in the plan.
> - Write `db/migrations/migrate_003_albums.py` (idempotent, registers in
>   `schema_migrations`).
> - Add album CRUD methods to `db/db.py`:
>   - `upsert_album(apple_uuid, name) -> int` (returns album row id)
>   - `upsert_photo_album(photo_id, album_id)` (no-op if already exists)
>   - `get_pending_album_pushes(limit=500) -> list[dict]` (rows where
>     `flickr_pushed=0` and photo has a `flickr_id` and
>     `perms_pushed_flickr=1`)
>   - `mark_album_pushed(photo_id, album_id)`
>
> **Step 2 — Scanner**
> - In `poller/scanner.py`, after the existing per-photo upsert, collect
>   `photo.albums` from the `osxphotos` `PhotoInfo` object.
> - Filter to user-created albums only: `album_type == "Album"` (skip smart
>   albums, system albums).
> - Upsert each album, then upsert a `photo_albums` row for each
>   (photo, album) pair.
> - Do not change any existing scanner output or privacy classification.
>
> **Step 3 — Flickr client**
> - Add `create_photoset(title, primary_photo_id) -> str` to
>   `flickr/flickr_client.py`. Returns the new photoset ID.
> - Add `add_photo_to_photoset(photoset_id, photo_id) -> None`.
> - Add `get_photosets() -> list[dict]`.
> - All three must use the existing retry/jitter/429 infrastructure.
>
> **Step 4 — Album pusher**
> - Create `flickr/album_pusher.py` with
>   `push_photo_to_albums(db, flickr, photo_id) -> int`.
> - Creates the Flickr photoset if `albums.flickr_set_id` is null (using
>   the photo being pushed as the primary).
> - Adds the photo to the photoset.
> - Marks `photo_albums.flickr_pushed = 1` only on confirmed API success.
> - Returns the count of photosets updated.
>
> **Step 5 — Reviewer integration**
> - In `reviewer/app.py`, after the existing tag-push on approval, call
>   `push_photo_to_albums`. Import from `flickr.album_pusher`.
> - Log how many photosets were updated (or zero if the photo has no albums).
>
> **Step 6 — CLI**
> - Add a `sync-albums` subcommand to `bp`.
> - Accepts `--dry-run`, `--album NAME` (filter to one album), `--limit N`.
> - Uses `db.get_pending_album_pushes()` and calls `push_photo_to_albums`.
> - Prints summary: `albums created=N  photos added=N  skipped=N  failed=N`.
> - Exit codes: 0 = success, 1 = partial failure, 2 = operational error.
>
> **Step 7 — Tests & docs**
> - Add tests to `tests/test_core.py` covering:
>   - Album DB CRUD methods
>   - `push_photo_to_albums` (mock Flickr client)
>   - `sync-albums` CLI exit codes (0/1/2)
>   - Reviewer approval path now calls album pusher
> - Update `README.md`: add `sync-albums` to the CLI command table and
>   document the feature in a new "Album Sync" section.
> - Run `python tests/test_core.py` and confirm all tests pass before
>   committing.
>
> Work through all steps. Ask if anything is ambiguous before writing code.
> Do not change existing behavior of the scanner, poller, reconciler, or
> reviewer beyond what is specified above.
