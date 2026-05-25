# Library Double-Click to Photo Detail — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Double-clicking a photo card in the library grid navigates to `/photo/<id>`, with a "← Back to Library" link returning the user to the library with filters intact.

**Architecture:** A `dblclick` JS handler on `.lib-thumb` cards reads `data-id` and navigates to `/photo/<id>?back=<encoded_url>`. The `photo.html` back link is made conditional — when a `back` query param is present it overrides the default review-queue link; otherwise existing behaviour is unchanged. No backend changes.

**Tech Stack:** Jinja2 templates, vanilla JS, Flask (existing), pytest

---

## Files

| Action | File | What changes |
|--------|------|-------------|
| Modify | `reviewer/templates/photo.html` | Back link uses `?back=` param when present |
| Modify | `reviewer/templates/library.html` | `dblclick` handler on `.lib-thumb` cards |
| Modify | `tests/test_review_ui.py` | Two new tests for `?back=` param behaviour |
| Modify | `README.md` | Note double-click navigation in library section |

---

## Task 1: Conditional back link in photo detail page

**Files:**
- Modify: `reviewer/templates/photo.html` (line ~217)
- Modify: `tests/test_review_ui.py`

- [ ] **Step 1: Write two failing tests**

Add this class to `tests/test_review_ui.py`. Place it after the existing `TestPhotoDetailAlbums` class. The fixture `client_with_albums` already exists in the file and provides a seeded photo — reuse it.

```python
class TestPhotoDetailBackLink:
    """Back link in photo detail page honours the ?back= query param."""

    def test_back_param_renders_library_link(self, client_with_albums):
        """?back=/library → back link points to /library, not the review queue."""
        c, photo_id = client_with_albums
        import urllib.parse
        back = urllib.parse.quote("/library?album_id=3", safe="")
        resp = c.get(f"/photo/{photo_id}?back={back}")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "Back to Library" in html
        assert "/library?album_id=3" in html
        # Should NOT contain the review-queue back link when ?back= is set
        assert "url_for" not in html  # rendered HTML never contains template syntax

    def test_no_back_param_renders_review_link(self, client_with_albums):
        """No ?back= → back link still points to the review queue."""
        c, photo_id = client_with_albums
        resp = c.get(f"/photo/{photo_id}?state=candidate_public")
        assert resp.status_code == 200
        html = resp.data.decode()
        # The review-queue back link uses url_for('review', ...) which renders as /review
        assert "/review" in html
        assert "Back to Library" not in html
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_review_ui.py::TestPhotoDetailBackLink -v
```

Expected: both tests FAIL — `"Back to Library"` not in response and `/review` check may vary.

- [ ] **Step 3: Modify the back link in `photo.html`**

Find this block in `reviewer/templates/photo.html` (around line 217, inside `<div class="detail-image">`):

```html
    <a href="{{ url_for('review', state=state, person=person_filter or '') }}" class="back-link">← Back{% if person_filter %} · {{ person_filter }}{% endif %}</a>
```

Replace it with:

```html
    {% if request.args.get('back') %}
    <a href="{{ request.args.get('back') }}" class="back-link">← Back to Library</a>
    {% else %}
    <a href="{{ url_for('review', state=state, person=person_filter or '') }}" class="back-link">← Back{% if person_filter %} · {{ person_filter }}{% endif %}</a>
    {% endif %}
```

No CSS changes needed — `.back-link` is already styled (top-left overlay, dark pill).

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_review_ui.py::TestPhotoDetailBackLink -v
```

Expected: both tests PASS.

- [ ] **Step 5: Run full test suite**

```bash
python -m pytest tests/ -q
```

Expected: all tests pass. If anything in `TestPhotoDetailAlbums` breaks, the existing back-link HTML changed — re-check that the `{% else %}` branch is verbatim identical to the original.

- [ ] **Step 6: Commit**

```bash
git add reviewer/templates/photo.html tests/test_review_ui.py
git commit -m "feat(#134): photo detail back link honours ?back= query param

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 2: Double-click handler in library grid

**Files:**
- Modify: `reviewer/templates/library.html`

- [ ] **Step 1: Locate the JS block**

Open `reviewer/templates/library.html`. Find the `<script>` block that contains the `thumbClick` function (around line 293). The function looks like:

```js
function thumbClick(evt, id) {
  if (evt.target.tagName === 'INPUT') return;
  togglePhoto(id);
}
```

- [ ] **Step 2: Add the dblclick wiring after the DOMContentLoaded / existing event setup**

Find the end of the `<script>` block (look for the closing `</script>` tag). Just before it, add:

```js
// ── Double-click → open photo detail ────────────────────────────────
document.querySelectorAll('.lib-thumb').forEach(function(card) {
  card.addEventListener('dblclick', function(e) {
    var photoId = card.dataset.id;
    var back = encodeURIComponent(location.pathname + location.search);
    window.location.href = '/photo/' + photoId + '?back=' + back;
  });
});
```

**Why no selection-state correction is needed:** a double-click fires two `click` events before `dblclick`. Each `click` calls `thumbClick` → `togglePhoto`, which toggles the photo in and out of `_selectedIds`. Two toggles cancel out — the selection state is identical before and after the double-click fires. No correction required.

- [ ] **Step 3: Manual smoke test**

```bash
python reviewer/app.py --config config/config.yml
```

Open the library view in a browser. Double-click any photo card. Confirm:
- Browser navigates to `/photo/<id>?back=%2Flibrary%3F...`
- "← Back to Library" link appears in the top-left of the photo image area
- Clicking "← Back to Library" returns to the library with the same filters
- Single-clicking a card still toggles selection normally (no spurious navigation)
- Double-clicking a card that was already selected leaves it selected after returning (net no change)

- [ ] **Step 4: Run full test suite**

```bash
python -m pytest tests/ -q
```

Expected: all tests pass (no test touches the library JS, so this is a sanity check).

- [ ] **Step 5: Commit**

```bash
git add reviewer/templates/library.html
git commit -m "feat(#134): double-click photo card in library opens photo detail

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 3: README + issue close

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Update README**

Find the section in `README.md` that describes the Library view (search for "Library" or "library view"). Add a line noting double-click navigation. Example:

```
- Double-click any photo in the library grid to open its detail page (title, description, tags, larger image); the detail page links back to the library with filters intact.
```

Place it with other library feature bullets. Match the existing style.

- [ ] **Step 2: Commit README**

```bash
git add README.md
git commit -m "docs(#134): README — note double-click to detail in library view

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

- [ ] **Step 3: Update the spec doc status**

In `docs/superpowers/specs/2026-05-25-library-dblclick-photo-detail-134.md`, change the status line from `Approved, awaiting implementation plan` to `✓ done`.

```bash
git add docs/superpowers/specs/2026-05-25-library-dblclick-photo-detail-134.md
git commit -m "docs(#134): mark spec done

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

- [ ] **Step 4: Push and close GH issue**

```bash
git push origin main
gh issue close 134 --comment "Implemented in two commits:
- photo.html: conditional back link using \`?back=\` query param
- library.html: \`dblclick\` handler navigates to \`/photo/<id>?back=<url>\`

No backend changes. Tests added in \`TestPhotoDetailBackLink\`."
```
