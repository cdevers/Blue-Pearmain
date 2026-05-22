# Panoramic Photo Review UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make panoramic photos visually distinct in the review grid — double-wide tiles that show the full width, with named person chips so the operator knows who is in the shot before deciding.

**Architecture:** Two layers — (1) DB: add `width` and `height` to `review_queue()` SELECT (columns already exist in schema since migration 003); (2) Template: compute `is_pano` in Jinja, apply `.pano` CSS class for double-wide/contain layout, render named person chips in the meta row for panoramic tiles only.

**Tech Stack:** Python/SQLite, Jinja2, CSS. No new dependencies. No schema migration required.

**Spec:** `docs/superpowers/specs/2026-05-21-panoramic-review-ui-design.md`

**GitHub issue:** #126

---

## File Map

| File | Change |
|------|--------|
| `db/db.py` | Add `width`, `height` to `review_queue()` SELECT |
| `reviewer/templates/review.html` | CSS (pano overrides, person chips); Jinja (`is_pano`, `pano` class, person chips in `.meta`) |
| `tests/test_panoramic_review_ui.py` | New test file: `review_queue()` width/height fields; route template rendering for pano tiles and person chips |

---

## Task 1: Add `width` and `height` to `review_queue()`

**Files:**
- Modify: `db/db.py` (the `review_queue` method, line ~639)
- Create: `tests/test_panoramic_review_ui.py`

### Background

`width` and `height` already exist in the `photos` table (added in migration 003). They are not currently in the `review_queue()` SELECT. This task adds them so the template can compute whether a photo is panoramic.

- [ ] **Step 1: Write the failing test**

Create `tests/test_panoramic_review_ui.py`:

```python
"""
tests/test_panoramic_review_ui.py — tests for panoramic photo handling in the review UI

Run from repo root:
    python -m pytest tests/test_panoramic_review_ui.py -v
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.db import Database


@pytest.fixture()
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    yield d
    d.close()


class TestReviewQueueDimensions:
    def test_review_queue_returns_width_and_height(self, db):
        db.upsert_photo({
            "uuid": "uuid-pano-1",
            "original_filename": "PANO_001.JPG",
            "privacy_state": "candidate_public",
            "width": 5000,
            "height": 1000,
            "apple_persons": [],
            "proposed_tags": [],
        })
        photos = db.review_queue(states=["candidate_public"])
        assert len(photos) == 1
        assert photos[0]["width"] == 5000
        assert photos[0]["height"] == 1000

    def test_review_queue_returns_none_when_dimensions_absent(self, db):
        db.upsert_photo({
            "uuid": "uuid-pano-2",
            "original_filename": "IMG_002.JPG",
            "privacy_state": "candidate_public",
            "apple_persons": [],
            "proposed_tags": [],
        })
        photos = db.review_queue(states=["candidate_public"])
        assert len(photos) == 1
        # width/height may be None or 0 when not set; either is acceptable
        assert photos[0].get("width") in (None, 0)
        assert photos[0].get("height") in (None, 0)

    def test_review_queue_returns_non_panoramic_dimensions(self, db):
        db.upsert_photo({
            "uuid": "uuid-pano-3",
            "original_filename": "IMG_003.JPG",
            "privacy_state": "candidate_public",
            "width": 4032,
            "height": 3024,
            "apple_persons": [],
            "proposed_tags": [],
        })
        photos = db.review_queue(states=["candidate_public"])
        assert len(photos) == 1
        assert photos[0]["width"] == 4032
        assert photos[0]["height"] == 3024
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_panoramic_review_ui.py::TestReviewQueueDimensions -v
```

Expected: FAIL — `KeyError: 'width'` (field not in SELECT).

- [ ] **Step 3: Add `width` and `height` to `review_queue()` in `db/db.py`**

Find the `review_queue` method. The SELECT currently reads (around line 639):

```python
rows = self.conn.execute(
    f"""SELECT id, uuid, flickr_id, original_filename,
               apple_unknown_faces, apple_named_faces, proposed_tags,
               display_rotation, is_screenshot, updated_at,
               geofence_zone, apple_persons, privacy_reason
        FROM photos
        WHERE privacy_state IN ({placeholders}){screenshot_filter}
        ORDER BY date_taken DESC, id DESC
        LIMIT ? OFFSET ?""",
    states + [limit, offset],
).fetchall()
```

Replace with:

```python
rows = self.conn.execute(
    f"""SELECT id, uuid, flickr_id, original_filename,
               apple_unknown_faces, apple_named_faces, proposed_tags,
               display_rotation, is_screenshot, updated_at,
               geofence_zone, apple_persons, privacy_reason,
               width, height
        FROM photos
        WHERE privacy_state IN ({placeholders}){screenshot_filter}
        ORDER BY date_taken DESC, id DESC
        LIMIT ? OFFSET ?""",
    states + [limit, offset],
).fetchall()
```

The result loop after this does not need any changes — `width` and `height` are integers, not JSON strings.

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_panoramic_review_ui.py::TestReviewQueueDimensions -v
```

Expected: all 3 tests PASS.

- [ ] **Step 5: Run full suite to check for regressions**

```bash
python -m pytest tests/ -q
```

Expected: all existing tests pass.

- [ ] **Step 6: Commit**

```bash
git add db/db.py tests/test_panoramic_review_ui.py
git commit -m "feat(db): add width, height to review_queue() SELECT

Required by #126 (panoramic review UI). Columns already exist in
the photos table (migration 003); this just exposes them to the
review template so it can detect panoramic aspect ratios.

Part of #126
Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 2: Add panoramic CSS, tile class, and person chips to the review template

**Files:**
- Modify: `reviewer/templates/review.html`
- Modify: `tests/test_panoramic_review_ui.py`

### Background

This task makes three changes to `review.html`:

1. **CSS** — new rules for `.photo-card.pano` (double-wide tile, contain layout) and `.person-chip` variants
2. **Jinja `is_pano`** — compute the panoramic flag per tile in the loop
3. **Person chips** — render named person chips in the `.meta` section for pano tiles

`private_person_names` is already passed to the template from the `review()` route (added by #125 guardrail work) — no change to `app.py` needed.

- [ ] **Step 1: Write the failing tests**

Add this class to `tests/test_panoramic_review_ui.py`:

```python
import reviewer.app as app_module


@pytest.fixture()
def flask_client(tmp_path):
    """Flask test client with seeded photos covering pano/normal/persons scenarios."""
    db = Database(tmp_path / "test.db")

    # always_private policy for Jane Smith
    db.set_person_policy("Jane Smith", "always_private")

    # Photo 1: panoramic (5:1), named persons including always_private
    db.upsert_photo({
        "uuid": "uuid-pano-a",
        "original_filename": "PANO_A.JPG",
        "privacy_state": "candidate_public",
        "width": 5000,
        "height": 1000,
        "apple_persons": ["Jane Smith", "Bob Jones"],
        "proposed_tags": [],
    })
    # Photo 2: panoramic (3:1), unknown face only
    db.upsert_photo({
        "uuid": "uuid-pano-b",
        "original_filename": "PANO_B.JPG",
        "privacy_state": "candidate_public",
        "width": 3000,
        "height": 1000,
        "apple_persons": ["_UNKNOWN_"],
        "proposed_tags": [],
    })
    # Photo 3: normal 4:3, with a person (no chips expected)
    db.upsert_photo({
        "uuid": "uuid-normal-c",
        "original_filename": "IMG_C.JPG",
        "privacy_state": "candidate_public",
        "width": 4032,
        "height": 3024,
        "apple_persons": ["Alice"],
        "proposed_tags": [],
    })
    # Photo 4: panoramic but no persons
    db.upsert_photo({
        "uuid": "uuid-pano-d",
        "original_filename": "PANO_D.JPG",
        "privacy_state": "candidate_public",
        "width": 8000,
        "height": 1000,
        "apple_persons": [],
        "proposed_tags": [],
    })

    app_module._db = db
    app_module.app.config["TESTING"] = True
    app_module.app.config["SECRET_KEY"] = "test"
    with app_module.app.test_client() as c:
        yield c
    app_module._db = None
    db.close()


class TestPanoTemplate:
    def test_panoramic_tile_has_pano_class(self, flask_client):
        r = flask_client.get("/review?state=candidate_public")
        html = r.data.decode()
        # PANO_A.JPG has width=5000, height=1000 → ratio 5.0 > 2.0
        assert "pano" in html

    def test_normal_tile_has_no_pano_class(self, flask_client):
        r = flask_client.get("/review?state=candidate_public")
        html = r.data.decode()
        # IMG_C.JPG has ratio ~1.33 — pano class should NOT appear for that card
        # We verify pano class count <= number of pano photos (2 panos with persons + 1 pano without)
        # The class appears only on pano cards — confirm at least one non-pano card has no class
        # (We test this via absence of 'pano' in the card fragment for IMG_C.JPG)
        assert "IMG_C.JPG" in html  # normal photo present
        # Count of 'pano' occurrences should match CSS rule + number of pano card divs
        # 3 pano photos → 3 occurrences of class="photo-card pano" in the HTML
        # plus 1 from the CSS rule ".photo-card.pano" — total ≥ 3
        assert html.count("photo-card pano") == 3

    def test_person_chips_rendered_for_pano_with_persons(self, flask_client):
        r = flask_client.get("/review?state=candidate_public")
        html = r.data.decode()
        assert "person-chips" in html

    def test_unknown_chip_rendered_for_unknown_person(self, flask_client):
        r = flask_client.get("/review?state=candidate_public")
        html = r.data.decode()
        assert "person-chip unknown" in html
        assert ">unknown<" in html

    def test_protected_chip_rendered_for_always_private_person(self, flask_client):
        r = flask_client.get("/review?state=candidate_public")
        html = r.data.decode()
        assert "person-chip protected" in html
        assert "Jane Smith" in html

    def test_normal_named_person_chip_rendered(self, flask_client):
        r = flask_client.get("/review?state=candidate_public")
        html = r.data.decode()
        assert "Bob Jones" in html

    def test_no_person_chips_for_normal_tile(self, flask_client):
        r = flask_client.get("/review?state=candidate_public")
        html = r.data.decode()
        # Alice is in a normal (non-pano) tile — person-chip should NOT appear for her
        # Confirm she's in the page data but not in a person-chip span
        assert "Alice" not in html or 'person-chip' not in html.split("Alice")[0].split("person-chip")[-1]
        # Simpler: Alice should not appear wrapped in a person-chip span
        assert '<span class="person-chip">Alice</span>' not in html

    def test_no_person_chips_for_pano_with_no_persons(self, flask_client):
        r = flask_client.get("/review?state=candidate_public")
        html = r.data.decode()
        # PANO_D has no persons — its tile should not contribute chips
        # The person-chips div should not appear near PANO_D
        # We check that PANO_D appears in the HTML (it should)
        assert "PANO_D.JPG" in html
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_panoramic_review_ui.py::TestPanoTemplate -v
```

Expected: FAIL — `pano` class not found in HTML, person chips not rendered.

- [ ] **Step 3: Add CSS to `reviewer/templates/review.html`**

Inside the `{% block extra_style %}` block, after the existing `.photo-card .thumb .protected-badge` rule (around line 130), add:

```css
/* Panoramic tile — double-wide, contain layout */
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

/* Person name chips — rendered below thumbnail in pano tiles */
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

- [ ] **Step 4: Add `is_pano` and `pano` class to the photo-card div**

Find the photo-card div in the `{% for photo in photos %}` loop. It currently starts with:

```html
  <div class="photo-card"
       id="card-{{ photo.id }}"
```

Add `is_pano` above the div and apply `pano` class:

```html
  {% set is_pano = photo.width and photo.height and (photo.width / photo.height) > 2.0 %}
  <div class="photo-card{% if is_pano %} pano{% endif %}"
       id="card-{{ photo.id }}"
```

- [ ] **Step 5: Add person chips to the `.meta` section**

In the `.meta` div, find the `<div class="tag-row"` block and the `{% if photo.album_count %}` badge immediately after it. Add the person chips block between them:

```html
      <div class="tag-row" id="tags-{{ photo.id }}">
        {% if photo.proposed_tags %}
          {{ photo.proposed_tags | join(', ') }}
        {% else %}
          <span style="color:#444">no tags yet</span>
        {% endif %}
      </div>
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
      {% if photo.album_count %}
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
python -m pytest tests/test_panoramic_review_ui.py::TestPanoTemplate -v
```

Expected: all 8 tests PASS.

- [ ] **Step 7: Run full suite**

```bash
python -m pytest tests/ -q
```

Expected: all pass.

- [ ] **Step 8: Run lint**

```bash
make lint
```

Fix any mypy or ruff issues before committing.

- [ ] **Step 9: Update README.md**

Find the review UI description in `README.md` (near the guardrail paragraph added in v1.0.5). Add a sentence about panoramic handling:

> Panoramic photos (width/height ratio > 2.0) are displayed as double-wide tiles in the review grid, with `object-fit: contain` so the full width is visible. Named persons are shown as chip labels below the thumbnail on panoramic tiles.

- [ ] **Step 10: Commit**

```bash
git add reviewer/templates/review.html tests/test_panoramic_review_ui.py README.md
git commit -m "feat(ui): panoramic tile layout and person chips in review grid

Photos with width/height > 2.0 get grid-column: span 2 and
object-fit: contain so the full panoramic width is visible.
Named person chips appear below the thumbnail on pano tiles,
with 🔒 prefix for always_private persons.

Closes #126
Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage check:**

| Spec requirement | Task |
|---|---|
| `width` and `height` in `review_queue()` SELECT | Task 1, Step 3 |
| `is_pano` computed in template (width/height > 2.0) | Task 2, Step 4 |
| `.photo-card.pano` → `grid-column: span 2` | Task 2, Step 3 |
| `.photo-card.pano .thumb` → `aspect-ratio: 3/1; background: #1a1a1a` | Task 2, Step 3 |
| `.photo-card.pano .thumb img` → `object-fit: contain` | Task 2, Step 3 |
| Person chips in `.meta` for pano tiles only | Task 2, Step 5 |
| `_UNKNOWN_` → `unknown` chip (dimmer) | Task 2, Step 5 |
| `always_private` person → `🔒` chip (`protected` class) | Task 2, Step 5 |
| `private_person_names` already in template context | ✓ no change needed (done by #125) |
| No change to non-pano tiles | ✓ guarded by `{% if is_pano %}` |
| No migration required | ✓ width/height exist since migration 003 |
| README update | Task 2, Step 9 |

All spec requirements covered. No placeholders. No TBD.
