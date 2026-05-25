# Library Double-Click to Photo Detail — Design Spec

**Date:** 2026-05-25  
**Status:** ✓ done

---

## Problem

The library view shows a photo grid but offers no way to open a photo's full detail page from it. Viewing a photo larger or editing its title, description, or tags requires navigating to the reviewer queue separately.

## Scope

- Double-clicking a photo card in the library grid navigates to `/photo/<id>`
- A "← Back to Library" link on the photo detail page returns the user to the library with filters intact
- Single-click-to-select behaviour is unchanged

**Out of scope:**
- Modal/lightbox within the library page
- Keyboard navigation (arrow keys) within library grid
- Touch-specific affordances (discoverable via double-tap on iOS)

---

## Design

### `reviewer/templates/library.html` — JS only

Add a `dblclick` listener to each photo card. Because a double-click fires two `click` events first, the checkbox ends up net-unchanged — the handler corrects this by undoing the toggle before navigating:

```js
card.addEventListener('dblclick', (e) => {
    e.preventDefault();
    // Undo the net checkbox toggle caused by the two preceding single-clicks
    const cb = card.querySelector('input[type=checkbox]');
    if (cb) cb.checked = !cb.checked;
    updateSelectionState();  // existing function — keeps action bar in sync
    const photoId = card.dataset.photoId;
    const back = encodeURIComponent(location.pathname + location.search);
    window.location.href = `/photo/${photoId}?back=${back}`;
});
```

`data-photo-id` is already present on card elements. No template structure changes required.

### `reviewer/templates/photo.html` — one addition

At the top of `.detail-panel`, render a back link when the `back` query param is present:

```html
{% if request.args.get('back') %}
<a href="{{ request.args.get('back') }}" class="back-link">← Back to Library</a>
{% endif %}
```

One CSS rule added to `photo.html`'s `<style>` block:

```css
.back-link {
    font-size: 12px;
    color: var(--muted);
    text-decoration: none;
}
.back-link:hover { color: var(--text); }
```

### `reviewer/app.py` — no changes

The `back` param is passed through via `request.args` in the template; the route needs no modification.

---

## Testing

**`tests/test_photo_detail.py`** (new or existing):
- `GET /photo/<id>?back=/library` returns 200 — confirms the back param doesn't break the route

**Manual verification via `bp ui`:**
- Double-clicking a card navigates to `/photo/<id>`
- The "← Back to Library" link appears on the detail page
- Clicking it returns to the library with the same filters active
- Single-clicking a card still toggles selection normally
- Double-clicking a card that was unselected does not leave it selected after navigation
