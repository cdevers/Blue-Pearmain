# Album & Metadata Sync: Apple Photos ↔ Flickr

**Blue Pearmain — Architecture Plan**
*Status: Phase 1 complete, Phase 2 deferred | Author: cdevers | Last updated: 2026-04-15*

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

## Implementation Notes

These notes were added after `claude` reviewed the actual codebase before
implementing Phase 1. They refine details that the original design left
underspecified.

### Gap 1 — Scanner early-continue bypasses album sync

`poller/scanner.py` skips further processing for photos whose
`date_analyzed` hasn't changed:

```python
if existing_by_uuid.get("date_analyzed") == photo_row.get("date_analyzed"):
    continue  # nothing new from Apple
```

Album membership can change without the ML analysis date changing — for
example, when a user adds an existing photo to a new album days after it was
taken. Album upserts must therefore happen **before** this `continue`, not
after it. The scanner implementation must call `sync_photo_albums()` in
three places: before the early-continue, after a changed-analysis upsert,
and after a new-photo insert.

### Gap 2 — Reviewer album push belongs inside `_push()`

`reviewer/app.py` runs permission and tag pushes inside a `_push()`
background thread. The album push call must live **inside** `_push()` —
gated on `perms_ok` and `_decision == "make_public"` — so it shares the
same thread, error handling, and DB commit path, rather than being spawned
separately. The pseudocode in the Phase 1 section below reflects this.

### Gap 3 — `db/migrations/` directory does not exist yet

The migration for Phase 1 must create this directory. No `__init__.py` is
needed since migration scripts are run directly.

### Gap 4 — Primary photo for new photosets

`flickr.photosets.create` requires a primary photo ID. The implementation
uses the photo currently being pushed as the primary (not the oldest in the
album), which avoids an extra DB query and is sufficient in practice.

### Confirmed patterns to follow

- `FlickrClient._call(method, params, http_method="POST")` — all write
  methods use POST.
- DB upserts: `INSERT OR IGNORE`, then `UPDATE` if needed; always
  `conn.commit()`.
- `bp` CLI: thin `cmd_*` dispatch functions, logic lives in separate
  modules (e.g. `flickr/sync_albums.py`).
- Tests: `unittest.TestCase` with `tempfile.TemporaryDirectory()` + real
  SQLite; Flickr mocked via `unittest.mock.MagicMock`.

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
`AlbumInfo` objects, each with `.uuid` and `.title`). Add a helper:

```python
def sync_photo_albums(photo, photo_db_id: int, db: Database, dry_run: bool) -> None:
    albums = getattr(photo, "albums", []) or []
    for album in albums:
        if getattr(album, "album_type", None) != "Album":
            continue  # skip SmartAlbum, Folder, system albums
        album_id = db.upsert_album(album.uuid, album.title)
        db.upsert_photo_album(photo_db_id, album_id)
```

Call this in **three places** inside `scan()`:

1. **Before the early-continue** (photo matched, analysis date unchanged) —
   album membership changes even when ML data doesn't; this is the critical
   fix relative to the naive implementation.
2. **After upsert** for matched photos with changed analysis data.
3. **After upsert** for newly inserted Photos-only photos.

> **Note:** Filter to `album_type == "Album"` only. Skip `"SmartAlbum"`,
> `"Folder"`, and system albums (e.g. "Recents", "Favourites").

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

Album push lives **inside** the existing `_push()` background thread,
after the permission and tag push blocks. It is triggered for both
`make_public` and `keep_private` decisions — private photos should still
appear in photosets so the full archive is organised by album on Flickr
regardless of visibility. For `make_public`, the album push waits until
`perms_ok` is confirmed (i.e. the permission change succeeded) before
adding the photo to any photoset. For `keep_private`, it fires
unconditionally (there is no permission change to wait on).

```python
# Album push: for make_public, wait until perms are confirmed;
# for keep_private, push immediately (private photos still belong in photosets).
do_album_push = (perms_ok and _decision == "make_public") or _decision == "keep_private"
if do_album_push:
    try:
        from flickr.album_pusher import push_photo_to_albums
        n = push_photo_to_albums(db(), c, _photo_id)
        if n:
            log.info("background push: added to %d photoset(s) photo_id=%s", n, _photo_id)
    except Exception as album_err:
        log.error("background push: album sync failed photo_id=%s: %s", _photo_id, album_err)
```

Do **not** spawn a separate thread for album push — it must run inside
`_push()` so failures are logged consistently and don't leave the DB in a
partial state relative to the permission push.

### CLI: `bp sync-albums`

A new subcommand for backfill and reconciliation of already-reviewed photos:

```
bp sync-albums [--dry-run] [--album NAME] [--limit N]
```

Behaviour:

1. Queries `photo_albums JOIN photos` for rows where:
   - `photo_albums.flickr_pushed = 0`
   - `photos.flickr_id IS NOT NULL`
   - `photos.perms_pushed_flickr = 1` (already public on Flickr) **or**
     `photos.review_decision = 'keep_private'` (private photos still belong in photosets)
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
| `db/migrations/migrate_003_albums.py` | New: idempotent migration + `schema_migrations` record; also creates `db/migrations/` directory |
| `db/db.py` | Add `upsert_album`, `upsert_photo_album`, `get_pending_album_pushes`, `mark_album_pushed`, `set_album_flickr_set_id`, `get_photo_albums`, `get_album_counts_for_photos` |
| `poller/scanner.py` | Add `sync_photo_albums()` helper; call in 3 places in `scan()` |
| `flickr/flickr_client.py` | Add `create_photoset`, `add_photo_to_photoset`, `get_photosets` |
| `flickr/album_pusher.py` | New: `push_photo_to_albums()` |
| `flickr/sync_albums.py` | New: `bp sync-albums` subcommand logic (CLI stays thin) |
| `reviewer/app.py` | Add album push inside existing `_push()` background thread |
| `bp` | Add `sync-albums` subparser + `cmd_sync_albums` |
| `tests/test_core.py` | Add `TestAlbumDB`, `TestAlbumPusher`, `TestSyncAlbumsCLI` |
| `README.md` | Add `sync-albums` to CLI command table; add "Album Sync" section |

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

2. **Album deletion:** *(decision made)* If a photo is removed from an
   Apple Photos album, it is **not** removed from the Flickr photoset.
   Phase 1 is additive-only. Removal can be added later with an explicit
   `--remove` flag on `bp sync-albums`.

3. **Primary photo for new photosets:** *(resolved)* `flickr.photosets.create`
   requires a primary photo. The implementation uses the photo currently
   being pushed as the primary, avoiding an extra DB query. This is
   sufficient in practice.

4. **Private photos in photosets:** *(resolved)* Private photos (`keep_private`
   decisions) are included in the album push. Both the reviewer integration
   and `get_pending_album_pushes` treat `review_decision = 'keep_private'`
   the same as a fully-pushed public photo for the purpose of photoset
   membership, so the full archive is organised on Flickr regardless of
   visibility.

5. **osxphotos write permissions (Phase 2):** macOS Automation permissions
   may require a one-time user prompt. Document this clearly in the Phase 2
   implementation.

---

## Verification

After implementation, confirm with:

```bash
# Schema applies cleanly to a fresh DB
python -c "
from db.db import Database; import tempfile, pathlib
d = Database(pathlib.Path(tempfile.mkdtemp()) / 't.db')
print(d.conn.execute(\"SELECT name FROM sqlite_master WHERE type='table'\").fetchall())
"

# Migration runs against the live DB
python db/migrations/migrate_003_albums.py --config config/config.yml

# Full test suite
python tests/test_core.py

# Dry-run batch sync (after a scanner run to populate photo_albums)
bp sync-albums --dry-run --verbose
```
