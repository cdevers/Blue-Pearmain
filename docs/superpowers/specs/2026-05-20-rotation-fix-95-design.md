# Design: Rotation double-apply + stale grid thumbnail (GH #95)

**Date:** 2026-05-20
**Issue:** https://github.com/cdevers/Blue-Pearmain/issues/95
**Status:** done

## Problem

`bp rotate-flickr` has two bugs:

1. **Double-rotation (180° instead of 90°):** After rotating, Flickr re-encodes the image
   and returns a new secret. `api_rotate_flickr` refreshes `flickr_secret`/`flickr_server`
   in the DB, then *also* sets `display_rotation = degrees`. The `/thumb/` route (option 3)
   constructs a CDN URL from the new secret, which already serves the post-rotation image.
   The CSS `transform: rotate(Ndeg)` then rotates it again → 180° total. The bug is
   intermittent because it only triggers when `get_photo_info` returns a new secret quickly.

2. **Stale grid thumbnail:** After returning to the review grid, `<img src="/thumb/{id}">`
   may be served from the browser's HTTP cache. Flask's `send_file()` emits `ETag` /
   `Last-Modified` headers; CDN redirects are also cacheable. The grid page HTML is fresh
   (full page reload via `window.location.href`), but the thumbnail URL is unchanged so the
   browser can reuse the cached response.

## Root cause summary

`display_rotation` is a **temporary CSS correction** for the stale-thumbnail window. The
thumbnailer already clears it to 0 when it regenerates a thumbnail
(`UPDATE photos SET thumbnail_path = ?, display_rotation = 0`). But `api_rotate_flickr`
sets it unconditionally, even when the new Flickr secret means the CDN URL is already
correct.

## Changes

### 1. `reviewer/app.py` — `api_rotate_flickr`: conditional `display_rotation`

Track whether `get_photo_info` returned a fresh secret. Set `display_rotation = 0` when it
did; only set `display_rotation = degrees` when it failed (old CDN URL still serves
pre-rotation image, CSS correction needed).

```python
# before
new_secret = photo.get("flickr_secret") or ""
new_server = photo.get("flickr_server") or ""
try:
    info = c.get_photo_info(photo["flickr_id"])
    p = info.get("photo", {})
    new_secret = p.get("secret") or new_secret
    new_server = p.get("server") or new_server
except FlickrError:
    pass  # stale secret is better than crashing; thumbnailer will retry

# after
new_secret = photo.get("flickr_secret") or ""
new_server = photo.get("flickr_server") or ""
info_refreshed = False
try:
    info = c.get_photo_info(photo["flickr_id"])
    p = info.get("photo", {})
    fetched_secret = p.get("secret")
    if fetched_secret:
        new_secret = fetched_secret
        new_server = p.get("server") or new_server
        info_refreshed = True
except FlickrError:
    pass  # thumbnailer will retry

new_rotation = 0 if info_refreshed else (current + degrees) % 360
```

The response already returns `display_rotation`; clients already use the server value, so
this requires no API contract change.

### 2. `reviewer/templates/photo.html` — `rotateFlickr()`: JS update

After a successful rotation:
- Force an immediate thumbnail re-fetch by updating `img.src` with a timestamp query
  param. This ensures the browser fetches the CDN redirect fresh rather than using its
  cached response for the old `/thumb/` URL.
- Handle `display_rotation == 0` cleanly (clear the CSS transform instead of setting
  `rotate(0deg)`, though both are visually equivalent).

```javascript
// before
if (d.ok) {
  const img = document.getElementById('main-image');
  if (img) {
    img.style.transform = `rotate(${d.display_rotation}deg)`;
  }
  toast(`Rotated ${degrees}° on Flickr`);
}

// after
if (d.ok) {
  const img = document.getElementById('main-image');
  if (img) {
    img.style.transform = d.display_rotation ? `rotate(${d.display_rotation}deg)` : '';
    img.src = `/thumb/{{ photo.id }}?r=${Date.now()}`;
  }
  toast(`Rotated ${degrees}° on Flickr`);
}
```

### 3. `db/db.py` — `review_queue()`: add `updated_at` to SELECT

```python
# before
f"""SELECT id, uuid, flickr_id, original_filename,
           apple_unknown_faces, apple_named_faces, proposed_tags,
           display_rotation, is_screenshot

# after
f"""SELECT id, uuid, flickr_id, original_filename,
           apple_unknown_faces, apple_named_faces, proposed_tags,
           display_rotation, is_screenshot, updated_at
```

### 4. `reviewer/app.py` — screenshot query: add `updated_at` to SELECT

The screenshot-filter path uses a hand-written SELECT that omits `updated_at`. Add it:

```python
# before
f"""SELECT id, flickr_id, original_filename,
           apple_unknown_faces, apple_named_faces, proposed_tags,
           display_rotation

# after
f"""SELECT id, flickr_id, original_filename,
           apple_unknown_faces, apple_named_faces, proposed_tags,
           display_rotation, updated_at
```

### 5. `reviewer/templates/review.html` — thumbnail cache bust

```html
<!-- before -->
<img src="{{ url_for('thumb', photo_id=photo.id) }}"

<!-- after -->
<img src="{{ url_for('thumb', photo_id=photo.id, v=photo.updated_at) }}"
```

Flask treats unknown `url_for` kwargs as query params, producing
`/thumb/1234?v=2026-05-20+12:34:56`. Since `updated_at` is stamped on every rotation,
the URL changes and the browser fetches fresh.

### 6. `reviewer/templates/photo.html` — thumbnail cache bust (initial render)

The same cache-bust param on the initial `<img>` in `photo.html`:

```html
<!-- before -->
<img src="{{ url_for('thumb', photo_id=photo.id) }}"

<!-- after -->
<img src="{{ url_for('thumb', photo_id=photo.id, v=photo.updated_at) }}"
```

`photo` here comes from `db().get_photo()` which uses `SELECT *`, so `updated_at` is
always present.

## Tests

All in `tests/test_core.py`, extending the existing `TestRotateFlickr` class.

### Test A — info refresh clears `display_rotation`

Mock `client.rotate()` success, `client.get_photo_info()` returning `{"photo": {"secret":
"newsecret", "server": "65535"}}`. POST 90°. Assert:
- response `display_rotation == 0`
- DB row `display_rotation == 0`

### Test B — info failure preserves `display_rotation`

Mock `client.rotate()` success, `client.get_photo_info()` raising `FlickrError(1, "fail")`.
POST 90°. Assert:
- response `display_rotation == 90`
- DB row `display_rotation == 90`

### Test C — `updated_at` present in `review_queue` results

Seed a photo, call `db.review_queue()`, assert `"updated_at"` key is present in every
returned dict.

## Out of scope

- Redesigning the rotation UI (buttons, confirm dialog).
- Persisting display rotation across browser sessions for the `get_photo_info` failure
  case (the thumbnailer handles eventual cleanup already).
