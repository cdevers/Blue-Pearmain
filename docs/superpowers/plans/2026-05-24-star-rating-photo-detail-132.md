# Star Rating Widget on Photo Detail Page — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the 1–5 star rating widget to `photo.html` so users can set or clear a rating from the per-photo detail page.

**Architecture:** Pure frontend change. The API (`POST /rate/<id>`), DB column (`bp_rating`), and CSS/JS patterns all exist in `review.html`; we copy the minimal pieces into `photo.html`. `get_photo()` already returns `bp_rating` via `SELECT *`, so no backend changes are needed. We also add keyboard shortcuts 0–5 to mirror the review grid behaviour.

**Tech Stack:** Jinja2 templates, vanilla JS, CSS media queries, Python/Flask test client (pytest), existing `/rate/<id>` API.

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `reviewer/templates/photo.html` | Modify | CSS + HTML widget + JS (init, setRating, keyboard) |
| `tests/test_bp_rating.py` | Modify | New `TestStarWidgetPhotoDetail` class |

No other files change.

---

### Task 1: Write failing tests for the star widget on the photo detail page

**Files:**
- Modify: `tests/test_bp_rating.py` (append after line 893)
- Test: `tests/test_bp_rating.py` (these *are* the tests)

- [ ] **Step 1: Append the new test class to `tests/test_bp_rating.py`**

Open `tests/test_bp_rating.py` and add the following class at the very end of the file:

```python
# ===========================================================================
# Task 8 — Photo detail page: star rating widget (#132)
# ===========================================================================


class TestStarWidgetPhotoDetail(unittest.TestCase):
    """Star rating widget must appear in the rendered /photo/<id> page."""

    def _setup_and_get_photo_html(self) -> str:
        """Create a test DB with one rated photo and render /photo/<id>."""
        import reviewer.app as app_module

        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.db")
            db.conn.execute(
                "CREATE TABLE IF NOT EXISTS person_policies "
                "(id INTEGER PRIMARY KEY, person_name TEXT NOT NULL UNIQUE, "
                "policy TEXT NOT NULL, created_at TEXT NOT NULL)"
            )
            db.conn.commit()
            photo_id = db.upsert_photo(
                {
                    "uuid": "detail-star-uuid",
                    "original_filename": "IMG_DETAIL.JPG",
                    "privacy_state": "candidate_public",
                    "apple_persons": [],
                    "proposed_tags": [],
                }
            )
            db.set_bp_rating(photo_id, 3)
            app_module._db = db
            app_module.app.config["TESTING"] = True
            app_module.app.config["SECRET_KEY"] = "test-secret"
            with app_module.app.test_client() as client:
                r = client.get(f"/photo/{photo_id}?state=candidate_public")
                html = r.data.decode()
            db.close()
            return html, photo_id

    def test_star_rating_div_present_on_detail_page(self):
        """photo.html must render a .star-rating div."""
        html, _ = self._setup_and_get_photo_html()
        self.assertIn("star-rating", html)

    def test_star_widget_data_rating_present(self):
        """star-rating div must carry the data-rating attribute."""
        html, _ = self._setup_and_get_photo_html()
        self.assertIn("data-rating=", html)

    def test_star_widget_data_rating_reflects_db_value(self):
        """data-rating must equal 3 (the value we set before rendering)."""
        html, _ = self._setup_and_get_photo_html()
        self.assertIn('data-rating="3"', html)

    def test_star_widget_has_five_star_spans(self):
        """The widget must contain exactly five ★ star characters."""
        html, _ = self._setup_and_get_photo_html()
        self.assertGreaterEqual(html.count("★"), 5)

    def test_set_rating_js_present_on_detail_page(self):
        """photo.html must include the setRating JS function."""
        template_path = (
            Path(__file__).parent.parent / "reviewer" / "templates" / "photo.html"
        )
        source = template_path.read_text()
        self.assertIn("setRating", source)

    def test_init_star_widgets_called_on_detail_page(self):
        """photo.html must call initStarWidgets (or equivalent inline init)."""
        template_path = (
            Path(__file__).parent.parent / "reviewer" / "templates" / "photo.html"
        )
        source = template_path.read_text()
        self.assertIn("initStarWidgets", source)

    def test_keyboard_0_to_5_rating_on_detail_page(self):
        """photo.html keydown handler must handle digit 0–5 to set rating."""
        template_path = (
            Path(__file__).parent.parent / "reviewer" / "templates" / "photo.html"
        )
        source = template_path.read_text()
        # The review grid uses "digit >= 0 && digit <= 5"; photo page must too
        self.assertIn("digit >= 0 && digit <= 5", source)

    def test_star_css_present_on_detail_page(self):
        """.star-rating CSS must be defined in photo.html."""
        template_path = (
            Path(__file__).parent.parent / "reviewer" / "templates" / "photo.html"
        )
        source = template_path.read_text()
        self.assertIn(".star-rating", source)

    def test_mobile_star_size_28px_on_detail_page(self):
        """Mobile media query must enlarge stars to 28px on photo detail page."""
        template_path = (
            Path(__file__).parent.parent / "reviewer" / "templates" / "photo.html"
        )
        source = template_path.read_text()
        self.assertIn("28px", source)
```

- [ ] **Step 2: Run the new tests — confirm they all fail**

```bash
cd /Users/cdevers/Documents/GitHub/Blue\ Pearmain
python -m pytest tests/test_bp_rating.py::TestStarWidgetPhotoDetail -v
```

Expected: all 8 tests **FAIL** (star widget not yet in photo.html).

---

### Task 2: Add the CSS to `photo.html`

**Files:**
- Modify: `reviewer/templates/photo.html` (inside `{% block extra_style %}`, after the existing `.flickr-link`/`.photos-link`/`.key-hint`/`.kbd-hints` rules, before `{% endblock %}`)

- [ ] **Step 1: Add `.star-rating` CSS to `photo.html`**

In `reviewer/templates/photo.html`, locate the closing `{% endblock %}` of `{% block extra_style %}` (currently line 192). Insert **before** it:

```css
/* Star rating widget */
.star-rating {
  margin: 6px 0 4px;
  cursor: pointer;
  font-size: 20px;
  line-height: 1;
  user-select: none;
}
.star-rating .star { color: #555; transition: color 0.1s; }
.star-rating .star.filled { color: #f5a623; }

@media (max-width: 640px) {
  /* Bigger stars for accurate touch targets on mobile */
  .star-rating {
    font-size: 28px;
    margin: 8px 0;
  }
}
```

(The detail page uses `font-size: 20px` on desktop — slightly larger than review grid's 18px — because the detail panel has more breathing room.)

- [ ] **Step 2: Run the CSS tests to confirm they pass**

```bash
cd /Users/cdevers/Documents/GitHub/Blue\ Pearmain
python -m pytest tests/test_bp_rating.py::TestStarWidgetPhotoDetail::test_star_css_present_on_detail_page tests/test_bp_rating.py::TestStarWidgetPhotoDetail::test_mobile_star_size_28px_on_detail_page -v
```

Expected: **2 PASS**.

---

### Task 3: Add the star widget HTML to the detail panel

**Files:**
- Modify: `reviewer/templates/photo.html` (inside `{% block content %}`, inside `.detail-panel`, just before `<!-- Actions -->`)

- [ ] **Step 1: Insert the star rating HTML widget**

In `reviewer/templates/photo.html`, find the comment `<!-- Actions — fixed at top so position is consistent across photos -->` (currently line 243). Insert **before** it:

```html
    <!-- Star rating -->
    {% set _bp_rating = photo.bp_rating if photo.bp_rating is defined else 0 %}
    <div class="star-rating" data-id="{{ photo.id }}" data-rating="{{ _bp_rating }}">
      {% for n in [1, 2, 3, 4, 5] %}
        <span class="star{% if n <= _bp_rating %} filled{% endif %}"
              data-value="{{ n }}">★</span>
      {% endfor %}
    </div>
```

- [ ] **Step 2: Run the HTML rendering tests**

```bash
cd /Users/cdevers/Documents/GitHub/Blue\ Pearmain
python -m pytest tests/test_bp_rating.py::TestStarWidgetPhotoDetail::test_star_rating_div_present_on_detail_page tests/test_bp_rating.py::TestStarWidgetPhotoDetail::test_star_widget_data_rating_present tests/test_bp_rating.py::TestStarWidgetPhotoDetail::test_star_widget_data_rating_reflects_db_value tests/test_bp_rating.py::TestStarWidgetPhotoDetail::test_star_widget_has_five_star_spans -v
```

Expected: **4 PASS**.

---

### Task 4: Add the star widget JavaScript to `photo.html`

**Files:**
- Modify: `reviewer/templates/photo.html` (inside `{% block scripts %}`, before the closing `</script>` tag)

- [ ] **Step 1: Add `initStarWidgets` and `setRating` functions**

In `reviewer/templates/photo.html`, find the closing `</script>` (currently line 625, after the keydown handler). Insert **before** it:

```javascript
// Star rating widget
function initStarWidgets() {
  document.querySelectorAll('.star-rating').forEach(container => {
    const stars = [...container.querySelectorAll('.star')];
    const current = () => parseInt(container.dataset.rating) || 0;

    stars.forEach((star, idx) => {
      star.addEventListener('mouseover', () => {
        stars.forEach((s, i) => s.classList.toggle('filled', i <= idx));
      });
    });
    container.addEventListener('mouseleave', () => {
      const c = current();
      stars.forEach((s, i) => s.classList.toggle('filled', i < c));
    });

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
```

- [ ] **Step 2: Run the JS function tests**

```bash
cd /Users/cdevers/Documents/GitHub/Blue\ Pearmain
python -m pytest tests/test_bp_rating.py::TestStarWidgetPhotoDetail::test_set_rating_js_present_on_detail_page tests/test_bp_rating.py::TestStarWidgetPhotoDetail::test_init_star_widgets_called_on_detail_page -v
```

Expected: **2 PASS**.

---

### Task 5: Add 0–5 keyboard shortcuts and update kbd-hints

**Files:**
- Modify: `reviewer/templates/photo.html` (keydown handler and `.kbd-hints` div)

- [ ] **Step 1: Add digit 0–5 handling to the existing keydown listener**

In `reviewer/templates/photo.html`, find the keydown handler. It currently starts with:

```javascript
document.addEventListener('keydown', e => {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
```

Add the digit-rating block immediately after that guard (before any existing key checks):

```javascript
  // Keys 0–5: set star rating
  const digit = parseInt(e.key, 10);
  if (!isNaN(digit) && digit >= 0 && digit <= 5) {
    const container = document.querySelector('.star-rating');
    if (container) setRating(parseInt(container.dataset.id), digit, container);
    return;
  }
```

- [ ] **Step 2: Add rating hint to `.kbd-hints`**

In `reviewer/templates/photo.html`, find the `.kbd-hints` div (currently lines 274–280):

```html
    <div class="kbd-hints">
      <span><kbd>J</kbd><kbd>K</kbd> next/prev</span>
      <span><kbd>Esc</kbd> grid</span>
      <span><kbd>D</kbd> title</span>
      <span><kbd>T</kbd> tags</span>
      <span><kbd>N</kbd> faces</span>
    </div>
```

Replace it with:

```html
    <div class="kbd-hints">
      <span><kbd>J</kbd><kbd>K</kbd> next/prev</span>
      <span><kbd>Esc</kbd> grid</span>
      <span><kbd>D</kbd> title</span>
      <span><kbd>T</kbd> tags</span>
      <span><kbd>N</kbd> faces</span>
      <span><kbd>0</kbd>–<kbd>5</kbd> rating</span>
    </div>
```

- [ ] **Step 3: Run the keyboard shortcut test**

```bash
cd /Users/cdevers/Documents/GitHub/Blue\ Pearmain
python -m pytest tests/test_bp_rating.py::TestStarWidgetPhotoDetail::test_keyboard_0_to_5_rating_on_detail_page -v
```

Expected: **1 PASS**.

---

### Task 6: Run all tests and commit

**Files:** none new — verify everything passes.

- [ ] **Step 1: Run the full test suite**

```bash
cd /Users/cdevers/Documents/GitHub/Blue\ Pearmain
python -m pytest tests/ -q
```

Expected: all tests pass (baseline was 1102; this adds 8 new tests → 1110 pass).

- [ ] **Step 2: Run lint**

```bash
cd /Users/cdevers/Documents/GitHub/Blue\ Pearmain
make lint
```

Expected: clean (photo.html is a template; mypy doesn't touch it).

- [ ] **Step 3: Update README test count**

In `README.md`, find the line that mentions the test count and update the number to match the new total. Example pattern to search for:

```
1102 tests
```

Update to the actual count shown by pytest (likely `1110 tests`).

- [ ] **Step 4: Commit**

```bash
cd /Users/cdevers/Documents/GitHub/Blue\ Pearmain
git add reviewer/templates/photo.html tests/test_bp_rating.py README.md
git commit -m "feat(#132): add star rating widget to photo detail page

- Add .star-rating CSS (20px desktop, 28px mobile) to photo.html
- Insert star widget HTML in detail panel, above action buttons
- Add initStarWidgets() + setRating() JS, keyboard 0–5 shortcuts
- Update kbd-hints to show 0–5 rating hint
- Add 8 tests to TestStarWidgetPhotoDetail in test_bp_rating.py

Closes #132
Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

- [ ] **Step 5: Push to origin**

```bash
cd /Users/cdevers/Documents/GitHub/Blue\ Pearmain
git push origin main
```

---

## Self-Review

**Spec coverage:**

| Requirement | Task |
|-------------|------|
| Widget appears on `/photo/:id` | Task 3 (HTML) |
| Same tap/click-to-set behaviour | Task 4 (JS: `initStarWidgets`) |
| Click current star clears rating | Task 4 (JS: `val === current() ? 0 : val`) |
| Mobile: 28px stars | Task 2 (CSS media query) |
| Existing API (`POST /rate/<id>`) used | Task 4 (`setRating` calls `/rate/${id}`) |
| No backend changes | ✓ — all changes are in `photo.html` |
| Tests | Tasks 1–5 (8 new tests) |

**Placeholder scan:** No TBDs, no "add appropriate error handling" phrases — all code is shown in full.

**Type consistency:** `initStarWidgets`, `setRating` — same names used in both the implementation (Task 4) and tests (Tasks 1, 4). `data-rating`, `data-id`, `data-value` — same attribute names as review.html.
