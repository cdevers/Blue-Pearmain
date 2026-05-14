# Thumbnail Rotation Glitch Fix — Design Spec (GH #21)

**Goal:** Fix the CSS over-rotation glitch that appears after using "Rotate on Flickr" — the thumbnail is correctly oriented after `bp thumbs` runs, but the stored `display_rotation` value still applies a redundant CSS transform on top of it.

---

## Root cause

The rotate endpoint (`POST /api/photos/:id/rotate`) sets `thumbnail_path = NULL` and stores the rotation delta in `display_rotation`. Templates apply `transform:rotate(Ndeg)` whenever `display_rotation != 0`. This is correct while the thumbnail is stale.

The thumbnailer refetches the thumbnail (the correctly-oriented Flickr CDN image) and writes `thumbnail_path`, but it never clears `display_rotation`. After that run, the template applies a CSS rotation to an image that is already correctly oriented — rotating it again.

---

## Fix

**File:** `poller/thumbnailer.py`

When the thumbnailer successfully resolves a `thumbnail_path` (any source: local derivative, Flickr URL, or downloaded file), the DB update that writes `thumbnail_path` also sets `display_rotation = 0`:

```python
db.conn.execute(
    "UPDATE photos SET thumbnail_path = ?, display_rotation = 0 WHERE id = ?",
    (thumb, row_id),
)
```

`display_rotation = 0` means "thumbnail is correctly oriented, no CSS correction needed." This is true once the thumbnailer has fetched the post-rotation Flickr image.

---

## What does not change

- Templates: unchanged. The CSS transform fires when `display_rotation != 0`, which is the correct condition — it just becomes 0 sooner (immediately after `bp thumbs` runs rather than never).
- The rotate endpoint's accumulation logic: `current = photo.get("display_rotation") or 0` reads the value before the thumbnailer clears it, so subsequent rotations still accumulate correctly.
- Schema: no migration. `display_rotation INTEGER NOT NULL DEFAULT 0` already exists.

---

## Testing

One new test in `tests/test_core.py` (thumbnailer suite): after a successful thumbnail write, verify `display_rotation = 0` in the DB. Existing thumbnailer tests verify path-setting and can be updated to assert the new column.
