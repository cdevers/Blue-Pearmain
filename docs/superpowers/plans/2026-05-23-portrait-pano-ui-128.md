# Portrait Panoramic UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the panoramic tile system to handle portrait-orientation panoramics with a double-tall tile (`grid-row: span 2`, `aspect-ratio: 1/3`), using rotation-aware dimension detection.

**Architecture:** Template-only change. `display_rotation`, `width`, and `height` are already in `review_queue()`. Replace the single `is_pano` Jinja variable with `is_landscape_pano` and `is_portrait_pano`, computed from effective (rotation-corrected) dimensions. Add `.pano-portrait` CSS.

**Tech Stack:** Jinja2, CSS Grid. No Python changes, no DB changes.

---

## File Map

| File | Change |
|---|---|
| `reviewer/templates/review.html` | Replace `is_pano` detection; add `.pano-portrait` CSS |
| `tests/test_portrait_pano_ui.py` | New — 8 tests: detection logic + template rendering + regressions |

---

### Task 1: Portrait pano UI — TDD

**Files:**
- Create: `tests/test_portrait_pano_ui.py`
- Modify: `reviewer/templates/review.html`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_portrait_pano_ui.py`:

```python
"""
tests/test_portrait_pano_ui.py — tests for portrait panoramic tile support (#128)

Run from repo root:
    python -m pytest tests/test_portrait_pano_ui.py -v
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.db import Database
import reviewer.app as app_module


@pytest.fixture()
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    yield d
    d.close()


@pytest.fixture()
def flask_client(tmp_path):
    """Flask test client seeded with landscape pano, portrait pano, and normal photos."""
    db = Database(tmp_path / "test.db")

    # Ensure person_policies table exists (migration_019, not in schema.sql)
    db.conn.execute("""
        CREATE TABLE IF NOT EXISTS person_policies (
            id          INTEGER PRIMARY KEY,
            person_name TEXT NOT NULL UNIQUE,
            policy      TEXT NOT NULL CHECK(policy IN ('always_private')),
            created_at  TEXT NOT NULL
        )
    """)
    db.conn.commit()

    # Photo 1: landscape pano (3:1 ratio, no rotation) → expects class "pano"
    db.upsert_photo({
        "uuid": "uuid-land-pano",
        "original_filename": "PANO_LAND.JPG",
        "privacy_state": "candidate_public",
        "width": 3000, "height": 1000,
        "apple_persons": [], "proposed_tags": [],
    })

    # Photo 2: portrait pano (1:3 ratio, no rotation) → expects class "pano-portrait"
    db.upsert_photo({
        "uuid": "uuid-port-pano",
        "original_filename": "PANO_PORT.JPG",
        "privacy_state": "candidate_public",
        "width": 1000, "height": 3000,
        "apple_persons": [], "proposed_tags": [],
    })

    # Photo 3: normal 4:3 → expects neither class
    db.upsert_photo({
        "uuid": "uuid-normal",
        "original_filename": "IMG_NORM.JPG",
        "privacy_state": "candidate_public",
        "width": 4032, "height": 3024,
        "apple_persons": [], "proposed_tags": [],
    })

    # Photo 4: stored sideways — raw dims are 3000×1000 (landscape) but display_rotation=90
    #   eff_w = height = 1000, eff_h = width = 3000 → portrait pano after rotation
    db.upsert_photo({
        "uuid": "uuid-rotated-port",
        "original_filename": "PANO_ROT_PORT.JPG",
        "privacy_state": "candidate_public",
        "width": 3000, "height": 1000,
        "display_rotation": 90,
        "apple_persons": [], "proposed_tags": [],
    })

    app_module.DATABASE_PATH = str(tmp_path / "test.db")
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as client:
        yield client

    db.close()


# ---------------------------------------------------------------------------
# review_queue: display_rotation is returned
# ---------------------------------------------------------------------------

class TestReviewQueueRotation:
    def test_display_rotation_returned_in_review_queue(self, db):
        """review_queue must include display_rotation so the template can correct dimensions."""
        db.upsert_photo({
            "uuid": "uuid-rot-check",
            "original_filename": "IMG_ROT.JPG",
            "privacy_state": "candidate_public",
            "width": 3000, "height": 1000,
            "display_rotation": 90,
            "apple_persons": [], "proposed_tags": [],
        })
        photos = db.review_queue(states=["candidate_public"])
        assert len(photos) == 1
        assert photos[0]["display_rotation"] == 90


# ---------------------------------------------------------------------------
# Template rendering: CSS classes
# ---------------------------------------------------------------------------

class TestPortraitPanoTemplate:
    def test_landscape_pano_gets_pano_class(self, flask_client):
        r = flask_client.get("/review?state=candidate_public")
        html = r.data.decode()
        # Landscape pano: card element must have class="... pano ..."
        assert 'class="photo-card pano"' in html or 'photo-card pano ' in html or ' pano"' in html

    def test_portrait_pano_gets_pano_portrait_class(self, flask_client):
        r = flask_client.get("/review?state=candidate_public")
        html = r.data.decode()
        assert "pano-portrait" in html

    def test_normal_photo_gets_no_pano_class(self, flask_client):
        r = flask_client.get("/review?state=candidate_public")
        html = r.data.decode()
        # At least one card should have neither pano class — IMG_NORM.JPG
        assert 'class="photo-card "' in html or 'class="photo-card"' in html

    def test_rotated_landscape_stored_sideways_is_portrait_pano(self, flask_client):
        """Photo with raw width=3000,height=1000,rotation=90 has eff dims 1000×3000 → portrait pano."""
        r = flask_client.get("/review?state=candidate_public")
        html = r.data.decode()
        # Two photos should now be portrait panos: uuid-port-pano and uuid-rotated-port
        assert html.count("pano-portrait") >= 2

    def test_pano_portrait_css_present(self, flask_client):
        """The .pano-portrait CSS rule must be present in the page."""
        r = flask_client.get("/review?state=candidate_public")
        html = r.data.decode()
        assert "pano-portrait" in html
        assert "grid-row: span 2" in html

    def test_landscape_pano_css_still_present(self, flask_client):
        """Regression: .pano (landscape) CSS must not be removed."""
        r = flask_client.get("/review?state=candidate_public")
        html = r.data.decode()
        assert "grid-column: span 2" in html

    def test_portrait_pano_aspect_ratio_css(self, flask_client):
        """The portrait pano thumb should use aspect-ratio: 1/3."""
        r = flask_client.get("/review?state=candidate_public")
        html = r.data.decode()
        assert "aspect-ratio: 1/3" in html

    def test_no_is_pano_variable_in_template(self, flask_client):
        """Regression guard: the old single is_pano variable must be replaced."""
        template_path = (
            Path(__file__).parent.parent / "reviewer" / "templates" / "review.html"
        )
        source = template_path.read_text()
        # The variable name 'is_pano' (without _landscape or _portrait suffix) must not appear
        assert "is_pano " not in source
        assert "is_pano}" not in source
        assert "{% set is_pano " not in source
```

- [ ] **Step 2: Run tests — expect failures**

```bash
python -m pytest tests/test_portrait_pano_ui.py -v 2>&1 | head -40
```

Expected: several FAIL — `pano-portrait` CSS not present, `grid-row: span 2` not in page, `is_pano` still in template.

- [ ] **Step 3: Update `reviewer/templates/review.html`**

**3a. Add `.pano-portrait` CSS** — insert immediately after the existing `.pano` block (after line `135: object-fit: contain;` / `136: }`):

```css
/* Portrait panoramic tile — double-tall, contain layout */
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

**3b. Replace the `is_pano` detection block** — find:

```jinja2
  {% set is_pano = photo.width and photo.height and (photo.width / photo.height) > 2.0 %}
  <div class="photo-card{% if is_pano %} pano{% endif %}"
```

Replace with:

```jinja2
  {% set eff_w = photo.height if photo.display_rotation in (90, 270) else photo.width %}
  {% set eff_h = photo.width  if photo.display_rotation in (90, 270) else photo.height %}
  {% set is_landscape_pano = eff_w and eff_h and (eff_w / eff_h) > 2.0 %}
  {% set is_portrait_pano  = eff_w and eff_h and (eff_h / eff_w) > 2.0 %}
  <div class="photo-card{% if is_landscape_pano %} pano{% elif is_portrait_pano %} pano-portrait{% endif %}"
```

**3c. Update person chips condition** — find:

```jinja2
      {% if is_pano and photo.apple_persons %}
```

Replace with:

```jinja2
      {% if (is_landscape_pano or is_portrait_pano) and photo.apple_persons %}
```

- [ ] **Step 4: Run tests — expect PASS**

```bash
python -m pytest tests/test_portrait_pano_ui.py -v
```

Expected: 8 tests PASS.

- [ ] **Step 5: Run full test suite**

```bash
python -m pytest tests/ -q
```

Expected: all tests PASS (including existing panoramic tests in `test_panoramic_review_ui.py`).

- [ ] **Step 6: Run lint**

```bash
make lint
```

- [ ] **Step 7: Commit**

```bash
git add reviewer/templates/review.html tests/test_portrait_pano_ui.py
git commit -m "feat: portrait panoramic tile support with rotation-aware detection (#128)

- Replace is_pano with is_landscape_pano / is_portrait_pano using effective
  dimensions (swapped when display_rotation is 90° or 270°)
- Add .pano-portrait CSS: grid-row: span 2, aspect-ratio: 1/3
- Person chips shown for both landscape and portrait panos

Closes #128

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 2: Wrap up

- [ ] **Step 1: Bump version to 1.0.10 in `pyproject.toml`**

Change `version = "1.0.9"` to `version = "1.0.10"`.

- [ ] **Step 2: Update README.md**

Find the test count line (e.g. `NNN tests passing`) in README and increment it by 8 (the new portrait pano tests).

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml README.md
git commit -m "Bump version to 1.0.10"
```

- [ ] **Step 4: Push to origin**

```bash
git push origin main
```
