# Album Rename and Delete — Design Spec

**Issues:** #136 (rename), #137 (delete)  
**Date:** 2026-05-25  
**Status:** ✓ done

---

## Problem

Albums can be viewed and filtered in BP but their names cannot be changed, and there is no way to mark an album deleted, without going to Photos.app or Flickr's web UI.

## Scope

- Rename an album from the `/albums` page — updates `albums.name` in the DB; queued automatically for Flickr title sync on next `bp sync-albums` run
- Delete an album from the `/albums` page — sets `deleted_at`; Flickr photoset removal handled by next `bp sync-albums --remove --apply` run, same as scanner-triggered deletions

**Out of scope:**
- Renaming or deleting inside Apple Photos (BP does not write back to Photos.app)
- Bulk rename or bulk delete
- A dedicated per-album detail page

---

## Architecture & Data Flow

### Rename

```
User clicks ✏ on an album row
    → name cell becomes inline <input> with Save / Cancel
    → PATCH /api/albums/<id>  { "name": "New Name" }
    → UPDATE albums SET name=?, updated_at=? WHERE id=?
    → returns { "ok": true, "name": "New Name" }
    → UI updates cell in place; no reload
    → next bp sync-albums run calls sync_album_titles()
          which pushes albums.name → Flickr photoset title
          for all albums with flickr_set_id IS NOT NULL
```

No new DB method needed. `sync_album_titles()` (already in `flickr/sync_albums.py`) runs unconditionally at the end of every `bp sync-albums` invocation and pushes `albums.name` to Flickr for all albums that have a photoset. A rename in the DB is therefore automatically picked up on the next sync — no extra flag or queue entry required.

### Delete

```
User clicks 🗑 on an album row
    → inline confirm appears: "Delete [Album]? [Confirm] [Cancel]"
    → Confirm → DELETE /api/albums/<id>
    → db.mark_album_deleted(album_id)  — sets deleted_at = now()
    → returns { "ok": true }
    → UI removes the row without reload
    → next bp sync-albums --remove --apply deletes the Flickr photoset
          (same path as scanner-triggered album deletion)
```

`mark_album_deleted()` and the full Flickr deletion pipeline already exist. The route only needs to call the existing method. Album membership rows (`photo_albums`) are cleaned up by `ON DELETE CASCADE` when `delete_album()` is called during sync.

---

## Backend

### New routes — `reviewer/app.py`

| Method | Path | Purpose |
|--------|------|---------|
| `PATCH` | `/api/albums/<int:album_id>` | Rename an album |
| `DELETE` | `/api/albums/<int:album_id>` | Mark an album deleted |

**`PATCH /api/albums/<id>`**
```
Body:    { "name": "New Name" }
Returns: { "ok": true, "name": "New Name" }
Errors:
  400  name missing or empty string
  404  album_id not found (or already deleted)
```

Implementation: validate name is a non-empty string, look up album (excluding deleted), execute `UPDATE albums SET name=?, updated_at=? WHERE id=?`, commit, return new name.

**`DELETE /api/albums/<id>`**
```
Body:    (none)
Returns: { "ok": true }
Errors:
  404  album_id not found (or already deleted)
```

Implementation: look up album (excluding deleted — double-delete is a 404, not a no-op), call `db().mark_album_deleted(album_id)`, return ok.

No new DB methods. Both routes use only existing methods and direct SQL.

---

## Frontend

### `reviewer/templates/albums.html`

**Action column** — the existing third column currently holds only "View in library →". Extend it to hold three controls:

```
[✏]  View in library →  [🗑]
```

`✏` is a small icon button that triggers rename mode.  
`🗑` is a small icon button that triggers the delete confirmation.

**Rename mode** (triggered by ✏):
- The name cell (`<td>`) is replaced by an `<input>` pre-filled with the current album name, plus Save and Cancel buttons inline
- Enter key → Save; Escape key → Cancel
- Save → `PATCH /api/albums/<id>` → on success update the cell text in place; on error show a toast
- Cancel → revert the cell to the original name text

**Delete confirmation** (triggered by 🗑, inline — no modal):
- The row's action cell is replaced by: *"Delete [name]? [Confirm] [Cancel]"*
- Cancel → restores the normal action buttons
- Confirm → `DELETE /api/albums/<id>` → on success the row slides out / is removed from the DOM; on error show a toast

Both flows use the existing `toast(msg, kind)` helper from `base.html`.

---

## Testing

### `tests/test_album_management_api.py` *(new)*

**PATCH rename:**
- Valid rename → 200, `{ "ok": true, "name": "..." }`, DB `albums.name` updated
- Empty name → 400
- Whitespace-only name → 400
- Unknown album id → 404
- Already-deleted album id → 404

**DELETE:**
- Valid delete → 200, `{ "ok": true }`, `deleted_at` is set in DB
- Album absent from `GET /albums` response after delete
- Unknown album id → 404
- Already-deleted album id → 404 (not a silent no-op)

### Manual verification

Run `bp ui` and confirm:
- ✏ button appears on each row; clicking it activates the inline input
- Enter saves, Esc cancels; name updates without page reload
- 🗑 button reveals inline confirm; Cancel restores buttons; Confirm removes the row
- After a rename: `bp sync-albums --dry-run` logs the new title for the album's photoset
- After a delete: album is absent from `/albums`; `bp sync-albums --remove` (preview) shows the photoset queued for deletion
