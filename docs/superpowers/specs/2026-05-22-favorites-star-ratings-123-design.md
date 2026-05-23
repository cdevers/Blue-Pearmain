# Favorites / Star Ratings Design — Issue #123

**Date:** 2026-05-22
**Issue:** https://github.com/cdevers/Blue-Pearmain/issues/123

---

## Goal

Add a 0–5 star rating field (`bp_rating`) to the photos database, seeded from Apple Photos' heart/Favorites flag and the Flickr machine tag `bp:favorite=N`. Surface star controls in the reviewer UI, with keyboard shortcuts (0–5). Write rating changes back to Apple Photos' heart flag and to Flickr via machine tag, bidirectionally.

---

## Architecture

`bp_rating` (0–5 integer) is the canonical rating stored in the BP database. It is seeded from, and kept in sync with, two external signals:

- **Apple Photos** (`photo.favorite` boolean): heart = at least 1 star; no heart = 0 stars.
- **Flickr** (`bp:favorite=N` machine tag): numerical tag written and read by BP's poller.

```
Apple Photos (photo.favorite) ──scanner──► DB.bp_rating ◄──poller seed── Flickr (bp:favorite=N)
                                                  │
                                         Reviewer UI (star widget)
                                               │          │
                                  photoscript  ▼          ▼  Flickr tags API
                                Photos.favorite            bp:favorite=N tag
```

### Sync Rules

**Scanner (Apple Photos → DB), runs on every poll:**

| `photo.favorite` | `bp_rating` | Action |
|-----------------|-------------|--------|
| `True` | 0 | Set `bp_rating = 1` (seed from heart) |
| `True` | 1–5 | No change (rated at least 1; heart is satisfied) |
| `False` | 0 | No change |
| `False` | 1–5 | Set `bp_rating = 0` (user removed heart; propagate) |

**Poller (Flickr → DB), seed only:**

- If Flickr photo has `bp:favorite=N` tag and current `bp_rating == 0` → set `bp_rating = N`.
- If `bp_rating > 0`, Flickr tag is ignored during read (BP is authoritative).

**UI → Photos write-back (synchronous):**

- `bp_rating >= 1` → set `photo.favorite = True` in Photos via photoscript.
- `bp_rating == 0` → set `photo.favorite = False` in Photos via photoscript.
- Errors from photoscript are logged and do not fail the rating request.

**UI → Flickr write-back (queued, on next poller run):**

- Poller compares DB `bp_rating` to the current `bp:favorite=N` Flickr tag.
- If they differ: remove old `bp:favorite=*` tag (by tag ID), add new `bp:favorite=N`.
- If `bp_rating = 0`: remove the machine tag entirely (if present).
- If `bp_rating > 0` and no tag: add `bp:favorite=N`.
- Uses add/remove operations, NOT `flickr.photos.setTags`, to preserve the user's other Flickr tags.

---

## Database Layer

### Migration 022: `db/migrations/migrate_022_bp_rating.py`

- Adds `bp_rating INTEGER NOT NULL DEFAULT 0` to the `photos` table.
- Idempotent: guarded by the `schema_migrations` table (key `022_bp_rating`).
- No SQL backfill: the scanner will seed values on its next run from `photo.favorite`.

### `db/schema.sql`

Add `bp_rating INTEGER NOT NULL DEFAULT 0` to the `photos` table definition for fresh installs.

### `db/db.py` — upsert changes

The Apple Photos scanner passes a new field `apple_favorite` (int 0 or 1). The upsert must apply the sync table above using a SQL `CASE` expression:

```sql
bp_rating = CASE
  WHEN :apple_favorite = 1 AND bp_rating = 0 THEN 1
  WHEN :apple_favorite = 0 AND bp_rating > 0 THEN 0
  ELSE bp_rating
END
```

This is applied only in the scanner upsert path, not the Flickr poller upsert (which uses a different seed-only condition).

### `db/db.py` — new functions

- **`set_bp_rating(photo_id: int, rating: int) -> None`**: Sets `bp_rating` directly (from the reviewer UI endpoint). No CASE logic — this is an explicit override.
- **`get_photo_uuid(photo_id: int) -> str | None`**: Looks up the Apple Photos UUID for a given DB row ID (needed so photoscript can find the photo in Photos).
- **`review_queue()`**: Add `bp_rating` to the SELECT column list.

---

## Scanner Changes

### `poller/scanner.py` (`_build_apple_row`)

Add `apple_favorite`:

```python
row["apple_favorite"] = 1 if getattr(photo, "favorite", False) else 0
```

This field is passed through to the DB upsert, where the CASE expression applies the sync policy.

---

## Poller Changes

### `poller/poller.py` (`_build_flickr_row`)

Parse the `bp:favorite=N` machine tag from the photo's tag list:

```python
bp_rating_from_flickr = 0
for tag in photo.get("tags", {}).get("tag", []):
    raw = tag.get("raw", "")
    if raw.startswith("bp:favorite="):
        try:
            bp_rating_from_flickr = int(raw.split("=", 1)[1])
        except ValueError:
            pass
row["flickr_bp_rating"] = bp_rating_from_flickr  # used for seed-only upsert
```

DB upsert for Flickr path applies seed-only logic:

```sql
bp_rating = CASE
  WHEN :flickr_bp_rating > 0 AND bp_rating = 0 THEN :flickr_bp_rating
  ELSE bp_rating
END
```

### Flickr tag write-back

In the poller's sync loop, after updating a photo's metadata, compare `bp_rating` to the Flickr tags:

1. Fetch current Flickr photo info (tags included, already fetched during sync).
2. Find any existing `bp:favorite=*` tag (record its `id` attribute from the tags response).
3. Compare to DB `bp_rating`:
   - If `bp_rating == 0` and tag exists → call `flickr.photos.removeTag(tag_id=...)`.
   - If `bp_rating > 0` and no tag → call `flickr.photos.addTags(photo_id=..., tags="bp:favorite=N")`.
   - If `bp_rating > 0` and tag exists with wrong value → remove old, add new.
   - If already correct → no API call.

---

## Reviewer UI

### `reviewer/app.py` — new endpoint

```python
@app.route("/rate/<int:photo_id>", methods=["POST"])
def rate_photo(photo_id):
    data = request.get_json(silent=True) or {}
    rating = int(data.get("rating", 0))
    if not 0 <= rating <= 5:
        return jsonify({"error": "invalid rating"}), 400

    db.set_bp_rating(photo_id, rating)

    # Write-back to Apple Photos (macOS only)
    uuid = db.get_photo_uuid(photo_id)
    if uuid:
        try:
            import photoscript
            photo = photoscript.Photo(uuid)
            photo.favorite = (rating >= 1)
        except Exception as exc:
            app.logger.warning("photoscript write failed for %s: %s", uuid, exc)

    return jsonify({"ok": True, "bp_rating": rating})
```

### `reviewer/templates/review.html` — star widget

**CSS** (add to existing `<style>` block):

```css
.star-rating {
  margin: 6px 0 4px;
  cursor: pointer;
  font-size: 18px;
  line-height: 1;
  user-select: none;
}
.star-rating .star { color: #555; transition: color 0.1s; }
.star-rating .star.filled { color: #f5a623; }
```

**Jinja template** (add to each `.photo-card`, above the decision buttons):

```html
<div class="star-rating" data-id="{{ photo.id }}" data-rating="{{ photo.bp_rating }}">
  {% for n in [1, 2, 3, 4, 5] %}
    <span class="star{% if n <= photo.bp_rating %} filled{% endif %}"
          data-value="{{ n }}">★</span>
  {% endfor %}
</div>
```

**JavaScript** (add to existing `<script>` block):

```javascript
// Star rating widget
function initStarWidgets() {
  document.querySelectorAll('.star-rating').forEach(container => {
    const stars = [...container.querySelectorAll('.star')];
    const current = () => parseInt(container.dataset.rating) || 0;

    // Hover preview
    stars.forEach((star, idx) => {
      star.addEventListener('mouseover', () => {
        stars.forEach((s, i) => s.classList.toggle('filled', i <= idx));
      });
    });
    container.addEventListener('mouseleave', () => {
      const c = current();
      stars.forEach((s, i) => s.classList.toggle('filled', i < c));
    });

    // Click to rate (clicking current star again clears to 0)
    stars.forEach(star => {
      star.addEventListener('click', e => {
        e.stopPropagation();
        const val = parseInt(star.dataset.value);
        const newRating = val === current() ? 0 : val;
        setRating(parseInt(container.dataset.id), newRating, container);
      });
    });
  });
}

async function setRating(id, rating, container) {
  const r = await apiFetch(`/rate/${id}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ rating }),
  });
  if (!r.ok) return;
  const d = await r.json();
  if (d.ok) {
    container.dataset.rating = d.bp_rating;
    const stars = [...container.querySelectorAll('.star')];
    stars.forEach((s, i) => s.classList.toggle('filled', i < d.bp_rating));
  }
}

document.addEventListener('DOMContentLoaded', initStarWidgets);

// Keyboard shortcuts: 0–5 to rate selected card (no auto-advance)
document.addEventListener('keydown', e => {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
  if (!selected) return;
  const digit = parseInt(e.key);
  if (!isNaN(digit) && digit >= 0 && digit <= 5) {
    e.preventDefault();
    const container = selected.querySelector('.star-rating');
    if (container) setRating(+selected.dataset.id, digit, container);
  }
});
```

---

## Existing Keyboard Shortcuts (unchanged)

| Key | Action | Advances? |
|-----|--------|-----------|
| `j` / ↓ | Select next card | — |
| `k` / ↑ | Select prev card | — |
| `p` / `P` | Make public | Yes |
| `x` / `X` | Keep private | Yes |
| `Space` | Skip | Yes |
| `Enter` | Open detail | — |
| `z` / `Z` | Undo | — |
| **`0`** | **Clear rating** | **No** |
| **`1`–`5`** | **Set 1–5 stars** | **No** |

---

## File Summary

| File | Change |
|------|--------|
| `db/migrations/migrate_022_bp_rating.py` | New — adds `bp_rating` column |
| `db/schema.sql` | Add `bp_rating` column for fresh installs |
| `db/db.py` | `_ensure_schema()` guard for `bp_rating`; `review_queue()` SELECT; upsert CASE; new `set_bp_rating()`, `get_photo_uuid()` |
| `poller/scanner.py` | Add `apple_favorite` to row |
| `poller/poller.py` | Parse Flickr machine tag; write `bp:favorite=N` tag on sync |
| `reviewer/app.py` | New `POST /rate/<id>` endpoint |
| `reviewer/templates/review.html` | Star widget CSS, Jinja, JS; keyboard 0–5 handler |
| `tests/test_bp_rating.py` | New — migration, scanner sync rules, poller seed, endpoint, UI |

---

## Testing Plan

- **Migration**: idempotent, column exists after run, default value is 0.
- **Scanner sync rules**: all four cases in the table above, via unit tests with mock `photo.favorite` and current `bp_rating` values.
- **Poller seed**: `bp:favorite=3` tag on Flickr photo with `bp_rating=0` → seeds to 3; with `bp_rating=2` → no change.
- **`/rate/<id>` endpoint**: valid ratings 0–5 accepted; invalid (6, -1) rejected with 400; DB updated; photoscript write called (mocked).
- **UI template**: star widget appears on cards; pre-fills from `bp_rating`; `setRating()` JS updates widget.
- **Flickr tag write-back**: mock Flickr API to verify add/remove calls fire correctly when `bp_rating` changes.
