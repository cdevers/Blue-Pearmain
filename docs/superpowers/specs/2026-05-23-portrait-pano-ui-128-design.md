# Portrait Panoramic UI Design — Issue #128

**Date:** 2026-05-23
**Issue:** https://github.com/cdevers/Blue-Pearmain/issues/128
**Depends on:** #126 (panoramic review UI — already shipped)

---

## Goal

Extend the panoramic tile logic to handle portrait-orientation panoramics: photos where `height/width > 2.0` (after correcting for `display_rotation`). Portrait panos get a double-tall tile (`grid-row: span 2`) with a `1/3` aspect-ratio thumbnail, mirroring what landscape panos do for the horizontal axis.

---

## Background

The existing panoramic detection in `review.html`:

```jinja2
{% set is_pano = photo.width and photo.height and (photo.width / photo.height) > 2.0 %}
```

Only catches landscape panos. Portrait panos (vertical cliffs, tall buildings) have `height > width × 2` and currently render as normal square-ish tiles, hiding most of the image.

**Data as of 2026-05-23:** 1,654 portrait panos, 4,117 landscape panos, 185,201 photos with known dimensions.

---

## Rotation-Aware Detection

`display_rotation` values in the DB: `0` (normal), `90`, `180`, `270`. A photo with `display_rotation = 90` or `270` is stored "sideways" — the raw `width` and `height` are swapped relative to how it displays.

Effective dimensions:

```
eff_w = height if display_rotation ∈ {90, 270} else width
eff_h = width  if display_rotation ∈ {90, 270} else height
```

Detection:
- **Landscape pano:** `eff_w / eff_h > 2.0`
- **Portrait pano:**  `eff_h / eff_w > 2.0`
- **Normal:**         neither

**Data note:** Only 3 landscape panos at 90° and 2 portrait panos at 180° exist in the live DB — the rotation correction barely matters in practice but is the correct thing to do.

---

## Template Changes (`reviewer/templates/review.html`)

### Replace the single `is_pano` variable

**Before:**
```jinja2
{% set is_pano = photo.width and photo.height and (photo.width / photo.height) > 2.0 %}
<div class="photo-card{% if is_pano %} pano{% endif %}" ...>
...
{% if is_pano and photo.apple_persons %}
```

**After:**
```jinja2
{% set eff_w = photo.height if photo.display_rotation in (90, 270) else photo.width %}
{% set eff_h = photo.width  if photo.display_rotation in (90, 270) else photo.height %}
{% set is_landscape_pano = eff_w and eff_h and (eff_w / eff_h) > 2.0 %}
{% set is_portrait_pano  = eff_w and eff_h and (eff_h / eff_w) > 2.0 %}
<div class="photo-card{% if is_landscape_pano %} pano{% elif is_portrait_pano %} pano-portrait{% endif %}" ...>
...
{% if (is_landscape_pano or is_portrait_pano) and photo.apple_persons %}
```

### Add `.pano-portrait` CSS

```css
.photo-card.pano-portrait {
  grid-row: span 2;
}
.photo-card.pano-portrait .thumb {
  aspect-ratio: 1/3;
  background: #1a1a1a;
}
.photo-card.pano-portrait .thumb img {
  object-fit: contain;
}
```

The existing `.pano` rules are unchanged.

---

## No DB Changes

`display_rotation`, `width`, and `height` are already in the `review_queue()` SELECT in `db/db.py`. No migration needed.

---

## File Summary

| File | Change |
|---|---|
| `reviewer/templates/review.html` | Replace `is_pano` with `is_landscape_pano`/`is_portrait_pano`; add `.pano-portrait` CSS |
| `tests/test_portrait_pano_ui.py` | New — rotation-aware detection tests + template rendering tests |

---

## Testing Plan

- **Detection logic (unit):** all combinations of raw dimensions × display_rotation → correct pano classification
  - Normal photo (4:3): not pano
  - Landscape pano (3:1 ratio, rotation 0): landscape
  - Portrait pano (1:3 ratio, rotation 0): portrait
  - Landscape-dimensioned photo with rotation 90: treated as portrait pano
  - Portrait-dimensioned photo with rotation 90: treated as landscape pano
  - Null width/height: not pano (no division)
- **Template rendering:** seed DB with portrait pano photo; check rendered HTML for `pano-portrait` class, `1/3` aspect-ratio, no `pano` class
- **Regression:** existing landscape pano photos still get `pano` class, not `pano-portrait`
