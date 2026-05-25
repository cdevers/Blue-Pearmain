# Design: Panoramic Photo Handling in Review UI

**Date:** 2026-05-21  
**Status:** Approved — ready for implementation planning  
**GitHub issue:** TBD (to be filed)

---

## Problem

Panoramic photos (very wide aspect ratio) are displayed in the review grid exactly like normal photos: cropped to a 4:3 thumbnail with `object-fit: cover`. This hides the margins of the image — exactly where faces often appear. The operator has approved panoramics believing them to be landscapes, only to notice people on the edges afterward. Two things need fixing:

1. **Visible full width** — the full panoramic should be visible in the tile, not centre-cropped
2. **Person signal visibility** — named persons should be shown by name in panoramic tiles, not just as a count badge, so the operator knows *who* is in the shot before deciding

---

## What counts as panoramic

A photo is panoramic when:
```
photo.width > 0 AND photo.height > 0 AND (photo.width / photo.height) > 2.0
```

This is computed in the template. Threshold of 2.0 is safely above 16:9 (1.78:1) and catches all iPhone panoramas (typically 3:1 to 8:1+). Photos without `width` or `height` fall through to normal tile behaviour.

---

## Tile layout changes

### Grid span

Panoramic photo-cards get `grid-column: span 2`, making them occupy two columns of the `repeat(auto-fill, minmax(220px, 1fr))` grid. Effective tile width is approximately 440–500px depending on viewport.

### Thumbnail container

Normal tiles: `aspect-ratio: 4/3; overflow: hidden;` + `object-fit: cover`

Panoramic tiles: `aspect-ratio: 3/1; overflow: hidden;` + `object-fit: contain` + dark background (`#1a1a1a`). At ~450px wide this gives a ~150px tall container. The full panoramic width is visible; narrow letterbox bands appear at top and bottom when the photo's ratio is taller than 3:1 (uncommon for true panoramas).

### Person name chips (panoramic tiles only)

Below the thumbnail, in the `.meta` section, add a `.person-chips` row. For each entry in `apple_persons`:

- Named person: `[Jane Smith]` chip (small, pill-shaped, neutral colour)
- `_UNKNOWN_`: `[unknown]` chip (slightly dimmer)
- If a named person has `always_private` policy: prefix with 🔒 — e.g. `[🔒 Jane Smith]`

Person chips are in addition to the existing `people-flag` count badge (which stays in place for consistency with non-panoramic tiles).

If `apple_persons` is empty or null: no `.person-chips` row rendered.

---

## Data changes

### `db.py` — `review_queue()` SQL

Add to SELECT:
```sql
width,
height,
apple_persons,
geofence_zone,
privacy_reason
```

Note: `apple_persons`, `geofence_zone`, and `privacy_reason` are also required by Issue #125 (geofence/person guardrail). These changes can land in a single migration of `review_queue()` and should be coordinated.

`apple_persons` is stored as a JSON string; it must be parsed the same way as in the person-filter path of `review()`:
```python
for field in ("apple_labels", "apple_persons", "proposed_tags"):
    if isinstance(d.get(field), str):
        try:
            d[field] = json.loads(d[field])
        except ...:
            d[field] = []
```

### `app.py` — `review()` route

Pass `private_person_names` to the template (set of names with `always_private` policy), needed for the 🔒 chip indicator. Same change as required by Issue #125; share the implementation.

---

## CSS additions

```css
/* Panoramic tile overrides */
.photo-card.pano {
  grid-column: span 2;
}
.photo-card.pano .thumb {
  aspect-ratio: 3/1;
  background: #1a1a1a;
}
.photo-card.pano .thumb img {
  object-fit: contain;
}

/* Person name chips */
.person-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 4px;
}
.person-chip {
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 10px;
  background: #2a2a2a;
  color: #ccc;
  white-space: nowrap;
}
.person-chip.unknown {
  color: #888;
}
.person-chip.protected {
  background: #3a2a1a;
  color: #e0a060;
}
```

---

## Template changes

In the Jinja loop over photos:

```html
{# Determine if panoramic #}
{% set is_pano = photo.width and photo.height and (photo.width / photo.height) > 2.0 %}

<div class="photo-card{% if is_pano %} pano{% endif %}" ...>
  <div class="thumb">
    <img ...>
    {# existing people-flag badge unchanged #}
  </div>
  <div class="meta">
    ...
    {% if is_pano and photo.apple_persons %}
    <div class="person-chips">
      {% for name in photo.apple_persons %}
        {% if name == '_UNKNOWN_' %}
          <span class="person-chip unknown">unknown</span>
        {% elif name in private_person_names %}
          <span class="person-chip protected">🔒 {{ name }}</span>
        {% else %}
          <span class="person-chip">{{ name }}</span>
        {% endif %}
      {% endfor %}
    </div>
    {% endif %}
    ...
  </div>
</div>
```

---

## Explicit non-goals

- **No change to non-panoramic tiles.** Person chips only appear on panoramic tiles; regular tiles keep the existing count badge.
- **No separate panoramic filter or queue.** Panoramics stay in the normal `candidate_public` / `needs_review` queues.
- **No thumbnail re-generation.** Existing thumbnails are used as-is. Flickr/local thumbnails may themselves be cropped; `object-fit: contain` shows whatever is available without distortion. Improving upstream thumbnails for panoramics is a separate concern.
- **No change to the detail view (`photo.html`).** Panoramic handling is grid-only for now.

---

## Dependencies

- **Issue #125 (geofence guardrail):** shares `review_queue()` column additions (`apple_persons`, `geofence_zone`, `privacy_reason`) and `private_person_names` in template context. Either issue can land first; the other picks up the already-added columns.

---

## Testing

- Unit: `review_queue()` returns `width`, `height`, `apple_persons` correctly
- Template: photo with width=5000, height=1000 renders with class `pano` and `grid-column: span 2`
- Template: photo with width=1000, height=800 does NOT get `pano` class
- Template: panoramic photo with named persons renders `.person-chips` row
- Template: panoramic with `_UNKNOWN_` renders `unknown` chip (dimmer style)
- Template: named person with `always_private` policy renders 🔒 chip
- Visual: panoramic tile is visibly wider than neighbours in the grid
