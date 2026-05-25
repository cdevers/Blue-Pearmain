# Design: Video Handling in Review UI

**Date:** 2026-05-21  
**Status:** Approved — ready for implementation planning  
**GitHub issue:** TBD (to be filed)

---

## Problem

Videos (`.MOV`, `.MP4`, `.M4V`) appear in the review grid with no visual distinction from still photos. The thumbnail is a JPEG (Flickr poster frame for the 99% that are already uploaded; grey placeholder for the rest), but there is no indicator that the tile represents a video rather than a still. The operator cannot tell at a glance that they are making a privacy decision about a moving image — which behaves differently from a still: longer duration, audio track, potentially more faces visible across frames.

There are currently 1,184 video records in the DB. 1,176 have Flickr-sourced JPEG thumbnails already; 8 are missing thumbnails (Photos-only, not yet uploaded to Flickr).

---

## What counts as a video

A photo record is a video when `is_video = 1` in the DB. HEIC/HEIF files are **not** treated as videos; they go through the standard still-image pipeline (Flickr does not support them natively and they are transcoded on upload).

---

## Schema change

Add one column to `photos`:

```sql
ALTER TABLE photos ADD COLUMN is_video INTEGER NOT NULL DEFAULT 0;
```

New migration file: `db/migrations/migrate_021_is_video.py` (following the existing migration pattern).

Backfill at migration time using the filename extension:

```sql
UPDATE photos
SET is_video = 1
WHERE lower(original_filename) LIKE '%.mov'
   OR lower(original_filename) LIKE '%.mp4'
   OR lower(original_filename) LIKE '%.m4v';
```

---

## Scanner changes (`poller/scanner.py`)

When building a photo row from osxphotos, set:

```python
photo_row["is_video"] = 1 if photo.ismovie else 0
```

`photo.ismovie` is the authoritative osxphotos field for video media type. HEIC Live Photos have `photo.live_photo = True` but `photo.ismovie = False` and are correctly excluded.

---

## Flickr poller changes (`poller/poller.py`)

The Flickr API returns a `media` field on each photo object (`"photo"` or `"video"`). Set:

```python
row["is_video"] = 1 if photo.get("media") == "video" else 0
```

This covers videos uploaded directly to Flickr that may not have a matching Photos record yet.

---

## Review UI changes

### `db.py` — `review_queue()` SQL

Add `is_video` to the SELECT:

```sql
SELECT id, uuid, flickr_id, original_filename,
       apple_unknown_faces, apple_named_faces, proposed_tags,
       display_rotation, is_screenshot, is_video, updated_at   -- is_video added
FROM photos ...
```

### `review.html` — thumbnail overlay

Add a ▶ play-button overlay centred on the thumbnail for video tiles, using the same overlay pattern as the existing `people-flag` and `screenshot-badge` elements:

```html
{% if photo.is_video %}
<span class="video-badge">▶</span>
{% endif %}
```

The overlay sits at the centre of the thumbnail (absolute-positioned), large enough to be unmistakeable at a glance.

### `review.html` — meta badge

Add a `video` label badge in the meta row (same visual style as the existing `screenshot` badge):

```html
{% if photo.is_video %}
<span class="video-label">video</span>
{% endif %}
```

### CSS additions

```css
/* Video overlay — centred play button on thumbnail */
.photo-card .thumb .video-badge {
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  font-size: 28px;
  color: rgba(255, 255, 255, 0.85);
  text-shadow: 0 1px 4px rgba(0, 0, 0, 0.7);
  pointer-events: none;
  line-height: 1;
}

/* Meta row label — same style as screenshot badge */
.video-label {
  display: inline-block;
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  color: #aaa;
  background: #2a2a2a;
  border-radius: 3px;
  padding: 1px 5px;
  margin-top: 3px;
}
```

---

## Thumbnailer — no change

Flickr generates JPEG poster frames for uploaded videos automatically; the existing download path handles these correctly. The 8 videos currently missing thumbnails are Photos-only records not yet uploaded to Flickr; they will gain thumbnails once the upload completes, with no thumbnailer changes required.

**Future enhancement (not in scope):** For Photos-only videos before Flickr upload, extract a poster frame via osxphotos or `ffmpeg`. This would close the 8-record gap immediately but adds a new dependency and complexity; deferred until there is demonstrated need.

---

## HEIC / Live Photo — explicitly excluded

HEIC files (`original_filename LIKE '%.heic'`) are still images in BP's model. `is_video` is never set for them. Live Photos embed a short video clip, but BP (and Flickr) treat the HEIC as the canonical still. No special handling.

---

## Interaction with panoramic handling (Issue #126)

If a video is also panoramic (unlikely but possible), both the `pano` CSS class and the `video-badge` overlay apply independently. The ▶ overlay is centred; the panoramic tile is double-wide. The two features compose without conflict.

---

## Explicit non-goals

- **No video playback in the review UI.** The ▶ badge is informational only; clicking through to the detail view or Flickr is the path to watch the clip.
- **No audio track warnings.** Out of scope for now.
- **No duration display.** Duration is not stored in the DB and fetching it would require either osxphotos or an EXIF read per video. Deferred.
- **No HEIC/Live Photo special casing.** These are treated as stills throughout.

---

## Testing

- Migration: `is_video = 1` set correctly for `.MOV`/`.MP4`/`.M4V` filenames; `is_video = 0` for `.jpg`, `.heic`, `.jpeg`
- Scanner: `photo.ismovie = True` → `is_video = 1`; Live Photo (`ismovie = False, live_photo = True`) → `is_video = 0`
- `review_queue()`: returns `is_video` field correctly
- Template: video tile renders ▶ overlay and `video` meta badge; non-video tile does not
- Template: video + panoramic tile renders both `pano` class and ▶ overlay without conflict
- Visual: ▶ badge is clearly visible over a typical Flickr thumbnail
