# Maintenance backlog

Small improvements noted during development; none are urgent.

---

## 1. Shrink thumbnail cache (30 GB → ~7 GB) ([GH #4](https://github.com/cdevers/Blue-Pearmain/issues/4))

**Problem:** The thumbs directory is ~30 GB because `download_thumb` prefers `url_l`
(1024 px). For review-grid use the 500 px `url_m` size is plenty, and the detail view
can link out to Flickr directly for full resolution.

**Change:** In `poller/poller.py`, `download_thumb` and `poll`, swap the preference so
`url_m` is tried first and `url_l` is the fallback. Also update `EXTRA_FIELDS` to keep
requesting both sizes.

**Migration:** Existing 1024 px files can be left in place or deleted and re-downloaded
at the smaller size on the next backfill poll. A one-time script that deletes files
larger than, say, 200 KB from `data/thumbs/` and re-queues the downloads would work,
or just let natural re-polls repopulate them over time.

---

## 2. ~~"Open in Photos" link in the per-photo review UI~~ ✓ done

**Problem:** The review UI runs on `localhost`, so the Mac's URL-scheme handler is
available. Currently there's no way to jump from a photo's detail screen into
Photos.app.

**Implemented:** `POST /api/open-in-photos/<photo_id>` runs AppleScript via `osascript`
to activate Photos.app and spotlight the photo by UUID. The template renders a
**Photos ↗** overlay and a Details row link (both `onclick="openInPhotos(...)"`) only
when `uuid` is non-NULL. `x-apple-photos://` is not a valid macOS URL scheme.

---

## 4. ~~Reviewer UI page load performance regression~~ ✓ done

**Root cause:** Two queries were sorting all 120k queue rows on every page load:
1. `review_queue()` used `SELECT *` + `ORDER BY COALESCE(...)` → full temp B-tree sort.
2. `photo_detail()` nav used `LAG`/`LEAD` window functions over the full queue.

**Fixed:**
- Added `idx_photos_review_queue ON photos(privacy_state, date_taken DESC, id DESC)`;
  SQLite now serves both queries from the covering index with no temp sort.
- Narrowed `review_queue()` `SELECT *` to the 6 columns the grid actually renders.
- Replaced window-function nav with `get_photo_nav()`: two single-row indexed lookups.
- Migration 009 creates the index on existing databases.

---

## 3. WAL checkpoint maintenance ([GH #5](https://github.com/cdevers/Blue-Pearmain/issues/5)) ✓ Implemented

**Problem:** The SQLite WAL file grew to 6.5 GB without being checkpointed (observed
2026-04-30). This happens when the process crashes or is killed mid-write, leaving
readers that block automatic checkpoints.

**Implemented (2026-05-02):**
- `PRAGMA wal_autocheckpoint = 500` added to `db/db.py` `_connect()` — passive
  checkpoints now trigger every 500 pages (half the SQLite default), keeping the WAL
  from growing large under normal operation.
- `Database.checkpoint(mode)` method added to `db/db.py`.
- `bp checkpoint [--mode MODE]` CLI command added — runs TRUNCATE by default,
  reports busy/log/checkpointed frame counts. Use `--mode passive` when the UI or
  other readers are active.

**Manual emergency fix (if WAL grows large):**

```bash
# Stop the UI daemon first, then:
bp checkpoint
# If busy > 0, readers are still active; try:
bp checkpoint --mode passive
```

---

## 4. Automate bp-all maintenance operations ([GH #13](https://github.com/cdevers/Blue-Pearmain/issues/13)) ✓ Partially implemented

**`bp all` command (implemented 2026-05-02):** Runs the full periodic maintenance
sequence: `scan --all` → `poll` → `thumbs` → `pipeline` → `reconcile --fix` →
`sync-albums` → `checkpoint`. Each step runs independently; a failure is logged and
the sequence continues.

**`config/com.blue-pearmain.pipeline.plist`** (renamed from `com.cdevers.*`) updated to call `bp all` instead
of `bp pipeline`. The 6h daemon now runs the full maintenance sequence.

**Not yet automated:**
- `bp poll --backfill --days 100000` — must stay manual until the iCloud→Flickr
  upload backlog (~29k photos as of 2026-05-02) is confirmed cleared.
- `bp scan --all` now runs in the 6h daemon via `bp all`.
