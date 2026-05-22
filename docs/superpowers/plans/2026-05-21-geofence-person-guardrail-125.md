# Geofence & Person-Policy Guardrail Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a ⚠️ warning badge and Override modal to the review UI so geofenced and always-private-person photos cannot be accidentally approved with the `p` key or a casual button click.

**Architecture:** Three layers — (1) DB: extend `review_queue()` to return `geofence_zone`, `apple_persons`, `privacy_reason`; (2) Route: annotate each photo dict with `is_protected` and `protected_reasons` before rendering; (3) Template: badge, conditional Override button, shared modal, JS keyboard guard. The override action calls the existing `/api/decide` endpoint with a new `override_note` field, which triggers an extra `operation_log` entry.

**Tech Stack:** Python/Flask, Jinja2, SQLite, vanilla JS, CSS. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-05-21-geofence-person-guardrail-design.md`

**GitHub issue:** #125

---

## File Map

| File | Change |
|------|--------|
| `db/db.py` | Add `geofence_zone`, `apple_persons`, `privacy_reason` to `review_queue()` SELECT; parse `apple_persons` JSON |
| `reviewer/app.py` | `review()`: compute `private_person_names` + annotate photo dicts; `api_decide()`: accept `override_note`, write override log entry |
| `reviewer/templates/review.html` | CSS (badge, override button, modal); Jinja (conditional badge, conditional buttons, data-attrs, modal HTML); JS (guard, modal, `decideWithOverride`) |
| `tests/test_geofence_guardrail.py` | New test file: `review_queue()` fields, route annotation, override log entry, template badge HTML |

---

## Task 1: Extend `review_queue()` to return geofence and person fields

**Files:**
- Modify: `db/db.py` (the `review_queue` method, around line 640)
- Create: `tests/test_geofence_guardrail.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_geofence_guardrail.py`:

```python
"""
tests/test_geofence_guardrail.py — tests for the geofence/person-policy guardrail

Run from repo root:
    python -m pytest tests/test_geofence_guardrail.py -v
"""

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.db import Database


def _make_db() -> Database:
    with tempfile.TemporaryDirectory() as tmp:
        return Database(Path(tmp) / "test.db")


@pytest.fixture()
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    yield d
    d.close()


class TestReviewQueueGeofenceFields:
    def test_review_queue_returns_geofence_zone(self, db):
        db.upsert_photo({
            "uuid": "uuid-geo-1",
            "original_filename": "IMG_001.JPG",
            "privacy_state": "candidate_public",
            "geofence_zone": "work",
            "apple_persons": [],
            "proposed_tags": [],
        })
        photos = db.review_queue(states=["candidate_public"])
        assert len(photos) == 1
        assert photos[0]["geofence_zone"] == "work"

    def test_review_queue_returns_none_geofence_zone_when_unset(self, db):
        db.upsert_photo({
            "uuid": "uuid-geo-2",
            "original_filename": "IMG_002.JPG",
            "privacy_state": "candidate_public",
            "apple_persons": [],
            "proposed_tags": [],
        })
        photos = db.review_queue(states=["candidate_public"])
        assert len(photos) == 1
        assert photos[0]["geofence_zone"] is None

    def test_review_queue_returns_apple_persons_as_list(self, db):
        db.upsert_photo({
            "uuid": "uuid-geo-3",
            "original_filename": "IMG_003.JPG",
            "privacy_state": "candidate_public",
            "apple_persons": ["Jane Smith", "Bob Jones"],
            "proposed_tags": [],
        })
        photos = db.review_queue(states=["candidate_public"])
        assert len(photos) == 1
        assert isinstance(photos[0]["apple_persons"], list)
        assert "Jane Smith" in photos[0]["apple_persons"]

    def test_review_queue_returns_empty_list_when_no_persons(self, db):
        db.upsert_photo({
            "uuid": "uuid-geo-4",
            "original_filename": "IMG_004.JPG",
            "privacy_state": "candidate_public",
            "apple_persons": [],
            "proposed_tags": [],
        })
        photos = db.review_queue(states=["candidate_public"])
        assert len(photos) == 1
        assert photos[0]["apple_persons"] == []

    def test_review_queue_returns_privacy_reason(self, db):
        db.upsert_photo({
            "uuid": "uuid-geo-5",
            "original_filename": "IMG_005.JPG",
            "privacy_state": "candidate_public",
            "privacy_reason": "no people detected",
            "apple_persons": [],
            "proposed_tags": [],
        })
        photos = db.review_queue(states=["candidate_public"])
        assert len(photos) == 1
        assert photos[0]["privacy_reason"] == "no people detected"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_geofence_guardrail.py -v
```

Expected: FAIL — `KeyError: 'geofence_zone'` (field not in `review_queue()` SELECT).

- [ ] **Step 3: Extend `review_queue()` in `db/db.py`**

Find the `review_queue` method. The SELECT currently reads:
```python
rows = self.conn.execute(
    f"""SELECT id, uuid, flickr_id, original_filename,
               apple_unknown_faces, apple_named_faces, proposed_tags,
               display_rotation, is_screenshot, updated_at
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
               geofence_zone, apple_persons, privacy_reason
        FROM photos
        WHERE privacy_state IN ({placeholders}){screenshot_filter}
        ORDER BY date_taken DESC, id DESC
        LIMIT ? OFFSET ?""",
    states + [limit, offset],
).fetchall()
```

In the loop immediately after, add `apple_persons` parsing alongside the existing `proposed_tags` parse:
```python
result = []
for row in rows:
    d = dict(row)
    d["proposed_tags"] = _json_loads_safe(d.get("proposed_tags"))
    d["apple_persons"] = _json_loads_safe(d.get("apple_persons"))  # add this line
    result.append(d)
return result
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_geofence_guardrail.py::TestReviewQueueGeofenceFields -v
```

Expected: all 5 tests PASS.

- [ ] **Step 5: Run full suite to check for regressions**

```bash
python -m pytest tests/ -q
```

Expected: all existing tests pass.

- [ ] **Step 6: Commit**

```bash
git add db/db.py tests/test_geofence_guardrail.py
git commit -m "feat(db): add geofence_zone, apple_persons, privacy_reason to review_queue()

Required by #125 (geofence guardrail) and #126 (panoramic UI).
Also parses apple_persons JSON string to list in review_queue result.

Closes part of #125
Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 2: Annotate photo dicts with `is_protected` / `protected_reasons` in the route

**Files:**
- Modify: `reviewer/app.py` (the `review()` function, around line 137)
- Modify: `tests/test_geofence_guardrail.py`

- [ ] **Step 1: Write failing tests**

Add this class to `tests/test_geofence_guardrail.py`:

```python
import json as _json
from unittest.mock import MagicMock
import reviewer.app as app_module


@pytest.fixture()
def flask_client(tmp_path):
    """Flask test client with a seeded in-memory-equivalent DB."""
    db = Database(tmp_path / "test.db")

    # Person policy: Jane Smith is always_private
    db.set_person_policy("Jane Smith", "always_private")

    # Photo 1: geofenced
    db.upsert_photo({
        "uuid": "uuid-p1",
        "original_filename": "IMG_001.JPG",
        "privacy_state": "candidate_public",
        "geofence_zone": "work",
        "apple_persons": [],
        "proposed_tags": [],
    })
    # Photo 2: private person (no zone)
    db.upsert_photo({
        "uuid": "uuid-p2",
        "original_filename": "IMG_002.JPG",
        "privacy_state": "candidate_public",
        "geofence_zone": None,
        "apple_persons": ["Jane Smith"],
        "proposed_tags": [],
    })
    # Photo 3: normal (not protected)
    db.upsert_photo({
        "uuid": "uuid-p3",
        "original_filename": "IMG_003.JPG",
        "privacy_state": "candidate_public",
        "geofence_zone": None,
        "apple_persons": ["Bob Jones"],
        "proposed_tags": [],
    })

    app_module._db = db
    app_module.app.config["TESTING"] = True
    app_module.app.config["SECRET_KEY"] = "test"
    with app_module.app.test_client() as c:
        yield c
    app_module._db = None
    db.close()


class TestRouteAnnotation:
    def test_protected_badge_present_for_geofenced_photo(self, flask_client):
        r = flask_client.get("/review?state=candidate_public")
        html = r.data.decode()
        assert "protected-badge" in html
        assert "Geofence: work" in html

    def test_protected_badge_present_for_private_person_photo(self, flask_client):
        r = flask_client.get("/review?state=candidate_public")
        html = r.data.decode()
        assert "Private person: Jane Smith" in html

    def test_override_button_present_for_protected_photo(self, flask_client):
        r = flask_client.get("/review?state=candidate_public")
        html = r.data.decode()
        assert "btn-override" in html
        assert "Override" in html

    def test_normal_button_absent_for_protected_photo(self, flask_client):
        """Verify geofenced photo does not render the normal btn-pub alongside override."""
        r = flask_client.get("/review?state=candidate_public")
        html = r.data.decode()
        # The HTML for a protected tile should not have btn-pub; count btn-pub occurrences
        # We have 1 protected (geofence) + 1 protected (person) + 1 normal = 1 btn-pub expected
        assert html.count("btn-pub") == 1

    def test_normal_photo_has_no_protected_badge(self, flask_client):
        r = flask_client.get("/review?state=candidate_public")
        html = r.data.decode()
        assert "Bob Jones" not in html or "Private person: Bob Jones" not in html

    def test_private_persons_js_set_embedded(self, flask_client):
        r = flask_client.get("/review?state=candidate_public")
        html = r.data.decode()
        assert "PRIVATE_PERSONS" in html
        assert "Jane Smith" in html
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_geofence_guardrail.py::TestRouteAnnotation -v
```

Expected: FAIL — `"protected-badge"` not found in HTML.

- [ ] **Step 3: Extend `review()` in `reviewer/app.py`**

At the end of the `review()` function, just before the `return render_template(...)` call, add:

```python
    # Compute protection annotation for the guardrail UI
    policies = db().get_person_policies()
    private_person_names = [n for n, p in policies.items() if p == "always_private"]
    private_person_set = set(private_person_names)
    for photo in photos:
        reasons: list[str] = []
        if photo.get("geofence_zone"):
            reasons.append(f"Geofence: {photo['geofence_zone']}")
        for person in (photo.get("apple_persons") or []):
            if person in private_person_set:
                reasons.append(f"Private person: {person}")
        photo["is_protected"] = bool(reasons)
        photo["protected_reasons"] = reasons
```

Update the `return render_template(...)` call to add `private_person_names`:

```python
    return render_template(
        "review.html",
        photos=photos,
        state_filter=state_filter,
        person_filter=person_filter,
        page=page,
        per_page=per_page,
        total=total,
        total_pages=total_pages,
        stats=db().stats(),
        private_person_names=private_person_names,
    )
```

Note: The `private_person_names` list and `private_person_set` are computed once per request. `get_person_policies()` is a single indexed DB read.

- [ ] **Step 4: Add protected badge CSS and HTML to `reviewer/templates/review.html`**

**CSS** — add inside the `<style>` block (after the existing `.photo-card .thumb .screenshot-badge` rule):

```css
/* Geofence / person-policy protection badge */
.photo-card .thumb .protected-badge {
  position: absolute;
  top: 4px;
  left: 4px;
  background: rgba(180, 120, 0, 0.92);
  color: #fff;
  font-size: 10px;
  font-weight: 600;
  padding: 2px 6px;
  border-radius: 3px;
  line-height: 1.4;
  pointer-events: none;
  max-width: calc(100% - 8px);
}

/* Override button — amber outlined, replaces btn-pub for protected photos */
.btn-override {
  background: transparent;
  border: 1px solid #b87800;
  color: #b87800;
  border-radius: 3px;
  font-size: 11px;
  font-weight: 600;
  padding: 3px 8px;
  cursor: pointer;
  white-space: nowrap;
}
.btn-override:hover { background: rgba(184, 120, 0, 0.12); }

/* Badge pulse animation — triggered by JS when p key is suppressed */
@keyframes protected-pulse {
  0%   { opacity: 1; transform: scale(1); }
  30%  { opacity: 1; transform: scale(1.08); }
  100% { opacity: 1; transform: scale(1); }
}
.protected-badge.pulse {
  animation: protected-pulse 0.4s ease-out;
}

/* Override modal */
#override-modal {
  display: none;
  position: fixed;
  inset: 0;
  z-index: 1000;
  align-items: center;
  justify-content: center;
}
#override-modal-backdrop {
  position: absolute;
  inset: 0;
  background: rgba(0, 0, 0, 0.65);
}
#override-modal-box {
  position: relative;
  background: #1e1e1e;
  border: 1px solid #444;
  border-radius: 8px;
  padding: 24px;
  max-width: 420px;
  width: 90%;
  z-index: 1;
}
#override-modal-box h3 {
  margin: 0 0 10px;
  font-size: 16px;
  color: #e0a060;
}
#override-modal-box p {
  margin: 0 0 14px;
  font-size: 13px;
  color: #ccc;
  line-height: 1.5;
}
#override-modal-box label {
  display: block;
  font-size: 11px;
  color: #888;
  margin-bottom: 4px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}
#override-note-field {
  width: 100%;
  box-sizing: border-box;
  background: #2a2a2a;
  border: 1px solid #444;
  border-radius: 4px;
  color: #eee;
  font-size: 13px;
  padding: 6px 8px;
  resize: vertical;
  min-height: 48px;
}
#override-modal-actions {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-top: 16px;
}
#override-modal-cancel {
  background: #333;
  border: 1px solid #555;
  color: #ccc;
  border-radius: 4px;
  padding: 6px 18px;
  font-size: 13px;
  cursor: pointer;
  font-weight: 600;
}
#override-modal-cancel:hover { background: #3a3a3a; }
#override-modal-confirm {
  background: transparent;
  border: 1px solid #666;
  color: #888;
  border-radius: 4px;
  padding: 4px 12px;
  font-size: 11px;
  cursor: pointer;
}
#override-modal-confirm:hover { color: #aaa; border-color: #888; }
```

**Badge HTML** — inside the `{% for photo in photos %}` loop, in the `.thumb` div, add after the screenshot badge:

```html
      {% if photo.is_protected %}
      <span class="protected-badge">
        {{ photo.protected_reasons | join(' · ') }}
      </span>
      {% endif %}
```

**data-* attributes** — on the `.photo-card` div, add two attributes:

```html
  <div class="photo-card"
       id="card-{{ photo.id }}"
       data-id="{{ photo.id }}"
       data-flickr="{{ photo.flickr_id or '' }}"
       data-protected="{{ '1' if photo.is_protected else '0' }}"
       data-geofence-zone="{{ photo.geofence_zone or '' }}"
       data-persons="{{ photo.apple_persons | tojson if photo.apple_persons else '[]' }}"
       tabindex="0"
       onclick="selectCard(this)"
       ondblclick="openDetail({{ photo.id }})">
```

**Conditional button** — replace the existing `btn-pub` button with:

```html
      <div class="actions">
        {% if photo.is_protected %}
        <button class="btn-override"
                onclick="event.stopPropagation(); openOverrideModal(this.closest('.photo-card'))">
          ⚠️ Override →
        </button>
        {% else %}
        <button class="btn-pub"
                onclick="event.stopPropagation(); quickDecide({{ photo.id }}, '{{ 'confirm_public' if state_filter == 'screenshot_public' else 'make_public' }}', this.closest('.photo-card'))">
          {% if state_filter == 'screenshot_public' %}✓ Confirm public{% else %}✓ Public{% endif %}
        </button>
        {% endif %}
        <button class="{% if state_filter.startswith('screenshot_') %}btn-pub{% else %}btn-prv{% endif %}"
```

(Keep the rest of the buttons — `btn-prv`, `btn-skp`, `btn-more`, and the restricted row — unchanged.)

**Modal HTML** — add once, just before `{% endblock %}`, outside the grid loop:

```html
<div id="override-modal" role="dialog" aria-modal="true" aria-labelledby="override-modal-title">
  <div id="override-modal-backdrop"></div>
  <div id="override-modal-box">
    <h3 id="override-modal-title">⚠️ Protected photo</h3>
    <p id="override-modal-reason"></p>
    <label for="override-note-field">Reason for override (optional)</label>
    <textarea id="override-note-field" rows="2"
              placeholder="e.g. parking lot — nothing sensitive"></textarea>
    <div id="override-modal-actions">
      <button id="override-modal-cancel">Cancel</button>
      <button id="override-modal-confirm">Make public anyway</button>
    </div>
  </div>
</div>
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
python -m pytest tests/test_geofence_guardrail.py::TestRouteAnnotation -v
```

Expected: all 6 tests PASS.

- [ ] **Step 6: Run full suite**

```bash
python -m pytest tests/ -q
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add reviewer/app.py reviewer/templates/review.html tests/test_geofence_guardrail.py
git commit -m "feat(ui): add protected badge and Override button for geofenced/private-person photos

Annotates photo dicts with is_protected + protected_reasons in the review()
route. Protected tiles show a ⚠️ badge and 'Override →' button instead
of the normal Approve button. Modal HTML and CSS added; JS in next commit.

Part of #125
Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 3: Add JS guardrail logic

**Files:**
- Modify: `reviewer/templates/review.html` (inside `<script>` block)

- [ ] **Step 1: Add `PRIVATE_PERSONS` JS set and guardrail functions**

Inside the `<script>` block in `review.html`, add near the top (after the existing `const _stateFilter = ...` lines):

```javascript
// Geofence / person-policy guardrail
const PRIVATE_PERSONS = new Set({{ private_person_names | tojson }});

function isProtected(card) {
  return card.dataset.protected === '1';
}

function pulseProtectedBadge(card) {
  const badge = card.querySelector('.protected-badge');
  if (!badge) return;
  badge.classList.remove('pulse');
  // Force reflow so re-adding the class re-triggers the animation
  void badge.offsetWidth;
  badge.classList.add('pulse');
  setTimeout(() => badge.classList.remove('pulse'), 500);
}

let _overrideCard = null;

function closeOverrideModal() {
  document.getElementById('override-modal').style.display = 'none';
  _overrideCard = null;
}

function openOverrideModal(card) {
  _overrideCard = card;
  const zone = card.dataset.geofenceZone || '';
  const persons = JSON.parse(card.dataset.persons || '[]');
  const privatePersons = persons.filter(p => PRIVATE_PERSONS.has(p));

  const reasons = [];
  if (zone) reasons.push(`taken in the '${zone}' geofence zone`);
  if (privatePersons.length) {
    reasons.push(`contains private person: ${privatePersons.join(', ')}`);
  }

  document.getElementById('override-modal-reason').textContent =
    `This photo was ${reasons.join(' and ')} and would normally be kept private.`;
  document.getElementById('override-note-field').value = '';
  document.getElementById('override-modal').style.display = 'flex';
  // Focus note field so keyboard entry is ready, but don't let Enter submit
  document.getElementById('override-note-field').focus();
}

async function decideWithOverride(id, note, card) {
  const r = await apiFetch('/api/decide', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      photo_id: id,
      decision: 'make_public',
      push: true,
      override_note: note,
    }),
  });
  const data = await r.json();
  closeOverrideModal();
  if (data.ok) {
    toast('✓ Approved (override)');
    card.classList.add('decided-pub');
    _updateReviewCounts('make_public');
    const cards = [...document.querySelectorAll('.photo-card')];
    const idx = cards.indexOf(card);
    if (idx >= 0 && idx < cards.length - 1) selectCard(cards[idx + 1]);
  } else {
    toast('Error: ' + (data.error || 'unknown'), 'err');
  }
}

// Modal button handlers — wired once on page load
document.getElementById('override-modal-cancel')
  .addEventListener('click', closeOverrideModal);

document.getElementById('override-modal-backdrop')
  .addEventListener('click', closeOverrideModal);

document.getElementById('override-modal-confirm').addEventListener('click', () => {
  if (!_overrideCard) return;
  const note = document.getElementById('override-note-field').value.trim();
  decideWithOverride(+_overrideCard.dataset.id, note, _overrideCard);
});
```

- [ ] **Step 2: Guard the `p` key in the existing keyboard handler**

Find the existing keyboard handler block containing `e.key === 'p'`:

```javascript
  } else if (e.key === 'p' || e.key === 'P') {
    const _pubDecision = _stateFilter === 'screenshot_public' ? 'confirm_public' : 'make_public';
    if (selected) quickDecide(+selected.dataset.id, _pubDecision, selected);
```

Replace with:

```javascript
  } else if (e.key === 'p' || e.key === 'P') {
    if (selected) {
      if (isProtected(selected)) {
        pulseProtectedBadge(selected);
      } else {
        const _pubDecision = _stateFilter === 'screenshot_public' ? 'confirm_public' : 'make_public';
        quickDecide(+selected.dataset.id, _pubDecision, selected);
      }
    }
```

- [ ] **Step 3: Add Escape key handler for modal**

Add to the second `keydown` event listener (the one for `z`/undo), or add a new one:

```javascript
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    const modal = document.getElementById('override-modal');
    if (modal && modal.style.display !== 'none') {
      closeOverrideModal();
    }
  }
});
```

- [ ] **Step 4: Manual verification**

Start the dev server:
```bash
python reviewer/app.py --config config/config.yml
```

Open the review page. Find or create a photo with `geofence_zone = 'work'` (use `sqlite3 data/curator.db "UPDATE photos SET geofence_zone='work', privacy_state='candidate_public' WHERE id=<id>"`).

Verify:
- [ ] The tile shows a ⚠️ amber badge with text "Geofence: work"
- [ ] The tile shows "⚠️ Override →" button, not "✓ Public"
- [ ] Pressing `p` with that tile selected pulses the badge and does nothing else
- [ ] Clicking "⚠️ Override →" opens the modal with correct reason text
- [ ] Pressing Escape closes the modal
- [ ] Clicking "Cancel" closes the modal
- [ ] Clicking "Make public anyway" approves the photo and closes the modal
- [ ] A normal tile still approves instantly with `p`

- [ ] **Step 5: Commit**

```bash
git add reviewer/templates/review.html
git commit -m "feat(ui): add JS guardrail — p key suppressed, Override modal for protected photos

isProtected() checks data-protected attr. Pressing 'p' on a protected
card pulses the warning badge. Override modal collects optional note and
calls /api/decide with override_note field.

Part of #125
Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 4: Log override to `operation_log` in `api_decide`

**Files:**
- Modify: `reviewer/app.py` (the `api_decide` function, around line 709)
- Modify: `tests/test_geofence_guardrail.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_geofence_guardrail.py`:

```python
class TestOverrideLogging:
    @pytest.fixture()
    def decide_client(self, tmp_path):
        db = Database(tmp_path / "test.db")
        db.set_person_policy("Jane Smith", "always_private")
        db.upsert_photo({
            "uuid": "uuid-ov-1",
            "original_filename": "IMG_OV1.JPG",
            "privacy_state": "candidate_public",
            "geofence_zone": "work",
            "apple_persons": [],
            "proposed_tags": [],
        })
        # get the photo id
        row = db.conn.execute(
            "SELECT id FROM photos WHERE uuid = 'uuid-ov-1'"
        ).fetchone()
        self._photo_id = row["id"]

        app_module._db = db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test"
        with app_module.app.test_client() as c:
            yield c
        app_module._db = None
        db.close()

    def test_override_note_writes_geofence_override_log_entry(self, decide_client):
        import json as _json
        r = decide_client.post(
            "/api/decide",
            json={
                "photo_id": self._photo_id,
                "decision": "make_public",
                "push": False,
                "override_note": "parking lot, nothing sensitive",
            },
        )
        assert r.status_code == 200
        data = r.get_json()
        assert data["ok"] is True

        # Check operation_log for the override entry
        db_entries = app_module._db.get_operation_log(
            photo_id=self._photo_id, operation="geofence_override"
        )
        assert len(db_entries) == 1
        entry = db_entries[0]
        assert entry["operation"] == "geofence_override"
        assert entry["target"] == "privacy_state"
        assert entry["new_value"] == "approved_public"
        assert entry["actor"] == "manual"
        trigger = _json.loads(entry["trigger"])
        assert trigger["zone"] == "work"
        assert trigger["note"] == "parking lot, nothing sensitive"

    def test_empty_override_note_still_writes_log_entry(self, decide_client):
        r = decide_client.post(
            "/api/decide",
            json={
                "photo_id": self._photo_id,
                "decision": "make_public",
                "push": False,
                "override_note": "",
            },
        )
        assert r.get_json()["ok"] is True
        db_entries = app_module._db.get_operation_log(
            photo_id=self._photo_id, operation="geofence_override"
        )
        assert len(db_entries) == 1

    def test_no_override_note_does_not_write_override_log_entry(self, decide_client):
        """Normal make_public without override_note must NOT write a geofence_override entry."""
        r = decide_client.post(
            "/api/decide",
            json={
                "photo_id": self._photo_id,
                "decision": "make_public",
                "push": False,
                # no override_note key
            },
        )
        assert r.get_json()["ok"] is True
        db_entries = app_module._db.get_operation_log(
            photo_id=self._photo_id, operation="geofence_override"
        )
        assert len(db_entries) == 0
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_geofence_guardrail.py::TestOverrideLogging -v
```

Expected: FAIL — no `geofence_override` entry in the log.

- [ ] **Step 3: Extend `api_decide` in `reviewer/app.py`**

At the top of `api_decide`, add `override_note` extraction alongside the existing fields:

```python
    override_note = data.get("override_note")  # None if key absent; "" if blank
```

After the existing `db().log_operation(...)` call for `review_decision`, add:

```python
    # If an override_note was provided, log the guardrail override separately.
    # override_note of None means a normal (non-override) decision — skip.
    # override_note of "" means the user chose not to write a note — still log.
    if override_note is not None and decision in ("make_public", "confirm_public"):
        _zone = photo.get("geofence_zone") or ""
        _raw_persons = photo.get("apple_persons") or "[]"
        if isinstance(_raw_persons, str):
            import json as _json_inner
            try:
                _persons_list = _json_inner.loads(_raw_persons)
            except Exception:
                _persons_list = []
        else:
            _persons_list = list(_raw_persons)
        _all_policies = db().get_person_policies()
        _private_lower = {k.lower() for k, v in _all_policies.items() if v == "always_private"}
        _private_in_photo = [p for p in _persons_list if p.lower() in _private_lower]

        _has_zone = bool(_zone)
        _has_person = bool(_private_in_photo)
        if _has_zone and _has_person:
            _op = "geofence_and_policy_override"
        elif _has_zone:
            _op = "geofence_override"
        else:
            _op = "policy_override"

        _trigger: dict[str, str] = {}
        if _zone:
            _trigger["zone"] = _zone
        if _private_in_photo:
            _trigger["person"] = ", ".join(_private_in_photo)
        if override_note:
            _trigger["note"] = override_note

        db().log_operation(
            photo_id=photo_id,
            operation=_op,
            target="privacy_state",
            old_value=old["privacy_state"] if old else None,
            new_value="approved_public",
            trigger=json.dumps(_trigger),
            actor="manual",
        )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_geofence_guardrail.py::TestOverrideLogging -v
```

Expected: all 3 tests PASS.

- [ ] **Step 5: Run full suite**

```bash
python -m pytest tests/ -q
```

Expected: all pass.

- [ ] **Step 6: Run lint**

```bash
make lint
```

Fix any mypy or ruff issues before committing.

- [ ] **Step 7: Commit**

```bash
git add reviewer/app.py tests/test_geofence_guardrail.py
git commit -m "feat(api): log geofence_override / policy_override to operation_log

When api_decide receives override_note (even blank), writes a second
log entry with operation=geofence_override|policy_override|geofence_and_
policy_override, trigger JSON with zone/person/note, actor=manual.

Closes #125
Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 5: Final check and README update

- [ ] **Step 1: Run full test suite one last time**

```bash
python -m pytest tests/ -q
```

Expected: all pass, no warnings.

- [ ] **Step 2: Update README**

In `README.md`, find the review UI section and add a note about the guardrail. Look for a paragraph describing the review grid and append:

> Photos taken within a geofence zone or containing a person with an `always_private` policy are flagged with a ⚠️ warning badge. The `p` keyboard shortcut is suppressed for these photos; approving them requires clicking "Override →" and confirming in a modal. Overrides are recorded in the operation log.

- [ ] **Step 3: Final commit**

```bash
git add README.md
git commit -m "docs: document geofence/person-policy guardrail in README (refs #125)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

- [ ] **Step 4: Push**

```bash
git push origin main
```
