# Confirmed-None Library Filter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `?confirmed_none=1` library filter that surfaces `geo_confirmed_none=1` photos, plus a contextual bulk undo button so mass "no location" marks are reversible.

**Architecture:** Mirror the existing `no_location` filter exactly — `confirmed_none` param flows through `_library_where()` → `library_photos()` / `library_photo_count()` / `library_photo_ids()`, a `confirmed_none_count()` method sits next to `no_location_count()`, the route parses the param and passes both count and active state to the template, and the template adds a filter chip plus a contextual "Undo" button that calls the existing `POST /api/geo_confirm_none` endpoint with `clear=true`. No new endpoint, no schema change.

**Tech Stack:** Python (SQLite, Flask), Jinja2, vanilla JS, unittest/pytest

---

## File Map

| File | Action | What changes |
|---|---|---|
| `db/db.py` | Modify | `confirmed_none` param in `_library_where`, `library_photos`, `library_photo_count`, `library_photo_ids`; new `confirmed_none_count()` |
| `reviewer/app.py` | Modify | Parse `?confirmed_none=1`; pass to DB calls; compute count; add to template context + filters dict |
| `reviewer/templates/library.html` | Modify | Filter chip; contextual undo button; `clearNoLocation()` JS; active-filter counter |
| `tests/test_geo_confirmed_none_filter.py` | Create | 7 new tests |

---

## Task 1: DB layer — confirmed_none filter + count

**Files:**
- Modify: `db/db.py`
- Create: `tests/test_geo_confirmed_none_filter.py`

### 1a — Write failing DB tests

- [ ] **Step 1: Create `tests/test_geo_confirmed_none_filter.py`**

```python
"""Library DB filter and count for geo_confirmed_none=1 photos (#148)."""

from __future__ import annotations
import tempfile
from pathlib import Path
import pytest
from db.db import Database


def _photo(i: int, **kwargs) -> dict:
    return {
        "uuid": f"gcn-u{i}",
        "original_filename": f"IMG_{i:04d}.JPG",
        "privacy_state": "needs_review",
        "apple_persons": [],
        "apple_labels": [],
        **kwargs,
    }


@pytest.fixture()
def gcn_db():
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "t.db")
        db.upsert_photo(_photo(1, latitude=42.3601, longitude=-71.0589))  # geotagged
        db.upsert_photo(_photo(2))                                         # unreviewed missing
        db.upsert_photo(_photo(3))                                         # unreviewed missing
        db.upsert_photo(_photo(4, geo_confirmed_none=1))                   # confirmed none
        db.upsert_photo(_photo(5, geo_confirmed_none=1))                   # confirmed none
        yield db


class TestConfirmedNoneFilter:
    def test_confirmed_none_filter_returns_only_confirmed_none_photos(self, gcn_db):
        photos = gcn_db.library_photos(confirmed_none=True)
        uuids = {p["uuid"] for p in photos}
        assert uuids == {"gcn-u4", "gcn-u5"}

    def test_confirmed_none_filter_excludes_geotagged_and_unreviewed(self, gcn_db):
        photos = gcn_db.library_photos(confirmed_none=True)
        uuids = {p["uuid"] for p in photos}
        assert "gcn-u1" not in uuids  # has coords
        assert "gcn-u2" not in uuids  # unreviewed missing
        assert "gcn-u3" not in uuids  # unreviewed missing

    def test_confirmed_none_count(self, gcn_db):
        assert gcn_db.confirmed_none_count() == 2

    def test_confirmed_none_count_excludes_deleted(self, gcn_db):
        # Mark photo 4 as flickr_deleted; it should drop out of the count
        gcn_db.conn.execute(
            "UPDATE photos SET flickr_deleted=1 WHERE uuid='gcn-u4'"
        )
        gcn_db.conn.commit()
        assert gcn_db.confirmed_none_count() == 1

    def test_confirmed_none_and_no_location_mutually_exclusive(self, gcn_db):
        with pytest.raises(ValueError, match="mutually exclusive"):
            gcn_db.library_photos(no_location=True, confirmed_none=True)

    def test_library_photo_count_confirmed_none(self, gcn_db):
        assert gcn_db.library_photo_count(confirmed_none=True) == 2

    def test_library_photo_ids_confirmed_none(self, gcn_db):
        ids = gcn_db.library_photo_ids(confirmed_none=True)
        assert len(ids) == 2
```

- [ ] **Step 2: Run the tests — confirm they fail**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/test_geo_confirmed_none_filter.py -v 2>&1 | tail -15
```

Expected: all 7 FAIL with `TypeError: _library_where() got an unexpected keyword argument 'confirmed_none'` or similar.

### 1b — Implement DB layer

- [ ] **Step 3: Add `confirmed_none` param to `_library_where()` signature**

Find `_library_where` in `db/db.py`. The last parameter in its signature is:

```python
        no_location: bool = False,  # #145 no_location filter
    ) -> tuple[str, list]:
```

Change it to:

```python
        no_location: bool = False,  # #145 no_location filter
        confirmed_none: bool = False,  # #148 confirmed-none filter
    ) -> tuple[str, list]:
```

- [ ] **Step 4: Add the mutual-exclusivity guard and confirmed_none clause to `_library_where()` body**

Find this comment block in `_library_where`:

```python
        # #145 — "No location" filter: untagged + not confirmed-none
        if no_location:
            clauses.append(
                "p.latitude IS NULL AND p.longitude IS NULL AND p.geo_confirmed_none = 0"
            )
            # Mutually exclusive with bbox — suppress it
            lat_min = lat_max = lon_min = lon_max = None
```

Replace it with:

```python
        # #145/#148 — no_location and confirmed_none are complementary but mutually exclusive
        if no_location and confirmed_none:
            raise ValueError("confirmed_none and no_location are mutually exclusive")

        # #145 — "No location" filter: untagged + not confirmed-none
        if no_location:
            clauses.append(
                "p.latitude IS NULL AND p.longitude IS NULL AND p.geo_confirmed_none = 0"
            )
            # Mutually exclusive with bbox — suppress it
            lat_min = lat_max = lon_min = lon_max = None

        # #148 — "Reviewed: no location" filter: confirmed-none photos
        if confirmed_none:
            clauses.append("p.geo_confirmed_none = 1")
```

- [ ] **Step 5: Add `confirmed_none` param to `library_photos()` signature and pass-through**

Find `library_photos`. Its last keyword param (before `limit`) is:

```python
        no_location: bool = False,
        limit: int = 120,
```

Change to:

```python
        no_location: bool = False,
        confirmed_none: bool = False,
        limit: int = 120,
```

In the same function, find the `_library_where(...)` call block. It currently ends with:

```python
            no_location=no_location,
        )
```

Change to:

```python
            no_location=no_location,
            confirmed_none=confirmed_none,
        )
```

- [ ] **Step 6: Add `confirmed_none` param to `library_photo_count()` signature and pass-through**

Find `library_photo_count`. Its last keyword param is:

```python
        no_location: bool = False,
    ) -> int:
```

Change to:

```python
        no_location: bool = False,
        confirmed_none: bool = False,
    ) -> int:
```

In the same function, find the `_library_where(...)` call block. It ends with:

```python
            no_location=no_location,
        )
```

Change to:

```python
            no_location=no_location,
            confirmed_none=confirmed_none,
        )
```

- [ ] **Step 7: Add `confirmed_none` param to `library_photo_ids()` signature and pass-through**

Find `library_photo_ids`. Its last keyword param is:

```python
        no_location: bool = False,
    ) -> list[int]:
```

Change to:

```python
        no_location: bool = False,
        confirmed_none: bool = False,
    ) -> list[int]:
```

In the same function, find the `_library_where(...)` call block. It ends with:

```python
            no_location=no_location,
        )
```

Change to:

```python
            no_location=no_location,
            confirmed_none=confirmed_none,
        )
```

- [ ] **Step 8: Add `confirmed_none_count()` method**

Find `no_location_count` in `db/db.py`:

```python
    def no_location_count(self) -> int:
        """Count photos with no geotag that have not been confirmed as intentionally-none."""
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM photos"
            " WHERE latitude IS NULL AND longitude IS NULL"
            "   AND geo_confirmed_none = 0"
            "   AND (flickr_deleted IS NULL OR flickr_deleted = 0)"
        ).fetchone()
        return row["n"] if row else 0
```

Add this method immediately after it:

```python
    def confirmed_none_count(self) -> int:
        """Count photos marked as intentionally having no location (geo_confirmed_none=1)."""
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM photos"
            " WHERE geo_confirmed_none = 1"
            "   AND (flickr_deleted IS NULL OR flickr_deleted = 0)"
        ).fetchone()
        return row["n"] if row else 0
```

- [ ] **Step 9: Run DB tests — confirm they pass**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/test_geo_confirmed_none_filter.py -v 2>&1 | tail -15
```

Expected: all 7 PASS.

- [ ] **Step 10: Run full suite — confirm no regressions**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/ -q 2>&1 | tail -5
```

Expected: all tests pass.

- [ ] **Step 11: Run lint**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && make lint 2>&1 | tail -10
```

Expected: no errors.

- [ ] **Step 12: Commit DB layer**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && git config user.email "1642218+cdevers@users.noreply.github.com" && git add db/db.py tests/test_geo_confirmed_none_filter.py && git commit -m "feat(#148): confirmed_none filter in DB layer

- _library_where/library_photos/library_photo_count/library_photo_ids:
  add confirmed_none param; raises ValueError if used with no_location
- confirmed_none_count(): count geo_confirmed_none=1 non-deleted photos
- 7 new tests

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 2: Route + Template — chip, undo button, JS

**Files:**
- Modify: `reviewer/app.py`
- Modify: `reviewer/templates/library.html`
- Modify: `tests/test_geo_confirmed_none_filter.py` (add route + UI tests)

### 2a — Write failing route + UI tests

- [ ] **Step 1: Add route and UI tests to `tests/test_geo_confirmed_none_filter.py`**

Append this class to the end of the file:

```python
import reviewer.app as app_module


@pytest.fixture()
def client_gcn():
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "t.db")
        db.upsert_photo(_photo(10, latitude=42.3601, longitude=-71.0589))
        db.upsert_photo(_photo(11))                          # unreviewed missing
        db.upsert_photo(_photo(12, geo_confirmed_none=1))    # confirmed none
        db.upsert_photo(_photo(13, geo_confirmed_none=1))    # confirmed none
        app_module._db = db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test"
        with app_module.app.test_client() as c:
            yield c
        app_module._db = None


class TestConfirmedNoneUI:
    def test_library_route_confirmed_none_param(self, client_gcn):
        resp = client_gcn.get("/library?confirmed_none=1")
        assert resp.status_code == 200
        html = resp.data.decode()
        # Only confirmed-none photos shown: gcn-u12, gcn-u13
        assert "gcn-u12" in html or "gcn-u13" in html
        # Geotagged photo must NOT appear
        assert "42.3601" not in html

    def test_confirmed_none_chip_visible_in_template(self, client_gcn):
        html = client_gcn.get("/library").data.decode()
        assert "Reviewed: no location" in html

    def test_confirmed_none_badge_count_shown(self, client_gcn):
        html = client_gcn.get("/library").data.decode()
        # 2 confirmed-none photos
        assert "confirmed_none_count" not in html  # variable name must not leak
        # count badge appears somewhere (the value "2" is present)
        assert "2" in html

    def test_undo_button_visible_only_when_filter_active(self, client_gcn):
        html_active = client_gcn.get("/library?confirmed_none=1").data.decode()
        html_inactive = client_gcn.get("/library").data.decode()
        assert "clearNoLocation" in html_active
        assert "Undo: no location" in html_active
        assert "Undo: no location" not in html_inactive
```

- [ ] **Step 2: Run the new tests — confirm they fail**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/test_geo_confirmed_none_filter.py::TestConfirmedNoneUI -v 2>&1 | tail -15
```

Expected: all 4 FAIL (route doesn't parse the param, template lacks chip and button).

### 2b — Implement route changes

- [ ] **Step 3: Parse `confirmed_none` in the `library()` route**

In `reviewer/app.py`, find the `library()` function. The line:

```python
    no_location = request.args.get("no_location") == "1"
```

Add the next line immediately after:

```python
    confirmed_none = request.args.get("confirmed_none") == "1"
```

- [ ] **Step 4: Pass `confirmed_none` to all three DB calls in `library()`**

The `photos = db().library_photos(...)` call currently includes:

```python
        no_location=no_location,
```

Add `confirmed_none=confirmed_none,` on the line immediately after, in all three DB calls (`library_photos`, `library_photo_count`, and `library_photo_ids` if present). Find each `no_location=no_location,` and add the new kwarg after it:

For `photos = db().library_photos(...)`:
```python
        no_location=no_location,
        confirmed_none=confirmed_none,
```

For `total = db().library_photo_count(...)`:
```python
        no_location=no_location,
        confirmed_none=confirmed_none,
```

- [ ] **Step 5: Compute `confirmed_none_count` and pass to template**

Find this line in `library()`:

```python
    no_location_count = db().no_location_count()
```

Add the next line immediately after:

```python
    confirmed_none_count = db().confirmed_none_count()
```

- [ ] **Step 6: Add `confirmed_none_count` and `confirmed_none` to `render_template` call**

Find the `render_template(...)` call. It currently includes:

```python
        no_location_count=no_location_count,
```

Add the new argument immediately after:

```python
        no_location_count=no_location_count,
        confirmed_none_count=confirmed_none_count,
```

Find the `filters={...}` dict inside the same `render_template` call. It currently includes:

```python
            "no_location": "1" if no_location else "",
```

Add the new entry immediately after:

```python
            "no_location": "1" if no_location else "",
            "confirmed_none": "1" if confirmed_none else "",
```

### 2c — Implement template changes

- [ ] **Step 7: Add `confirmed_none` to the active-filter counter**

In `reviewer/templates/library.html`, find the `filter_count` block (around line 234). It currently includes:

```jinja
  (1 if filters.no_location else 0) +
```

Add the next line immediately after:

```jinja
  (1 if filters.no_location else 0) +
  (1 if filters.confirmed_none else 0) +
```

- [ ] **Step 8: Add the "Reviewed: no location" filter chip**

Find this block in `library.html`:

```html
    <label style="display:flex;align-items:center;gap:5px">
      <input type="checkbox" name="no_location" value="1" {% if filters.no_location %}checked{% endif %}>
      No location
      {% if no_location_count %}
      <span style="background:var(--border);border-radius:10px;padding:1px 6px;font-size:10px;margin-left:4px">{{ no_location_count }}</span>
      {% endif %}
    </label>
```

Add this new `<label>` block immediately after the closing `</label>`:

```html
    <label style="display:flex;align-items:center;gap:5px">
      <input type="checkbox" name="confirmed_none" value="1"
             {% if filters.confirmed_none %}checked{% endif %}>
      Reviewed: no location
      {% if confirmed_none_count %}
      <span style="background:var(--border);border-radius:10px;padding:1px 6px;font-size:10px;margin-left:4px">{{ confirmed_none_count }}</span>
      {% endif %}
    </label>
```

- [ ] **Step 9: Add the contextual "Undo: no location" bulk action button**

Find this block in `library.html`:

```html
  <span class="sep">│</span>
  <button onclick="markNoLocation()">Mark: no location ✓</button>
  <button class="clear-btn" onclick="clearSelection()">✕ Clear</button>
```

Change it to:

```html
  <span class="sep">│</span>
  <button onclick="markNoLocation()">Mark: no location ✓</button>
  {% if filters.confirmed_none %}
  <span class="sep">│</span>
  <button onclick="clearNoLocation()">Undo: no location</button>
  {% endif %}
  <button class="clear-btn" onclick="clearSelection()">✕ Clear</button>
```

- [ ] **Step 10: Add `clearNoLocation()` JS function**

Find the `markNoLocation` JS function in `library.html`:

```javascript
// ── Mark: no location (#145) ─────────────────────────────────────────
async function markNoLocation() {
  const n = _selectionCount();
  if (n === 0) return;
  if (n > 10 && !confirm(
    `Mark ${n} photos as having no location? This will suppress future location sync proposals for all of them.`
  )) return;
  const ids = _selectAllFilter ? null : Array.from(_selectedIds);
  const body = ids ? {photo_ids: ids} : {};
  const r = await fetch('/api/geo_confirm_none', {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest'},
    body: JSON.stringify(body),
  });
  if ((await r.json()).ok) location.reload();
}
```

Add this function immediately after it:

```javascript
// ── Undo: no location (#148) ─────────────────────────────────────────
async function clearNoLocation() {
  const n = _selectionCount();
  if (n === 0) return;
  if (n > 10 && !confirm(
    `Undo 'no location' for ${n} photo${n === 1 ? '' : 's'}? They will re-enter the unreviewed missing-location queue.`
  )) return;
  const ids = _selectAllFilter ? null : Array.from(_selectedIds);
  const body = ids ? {photo_ids: ids, clear: true} : {clear: true};
  const r = await fetch('/api/geo_confirm_none', {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest'},
    body: JSON.stringify(body),
  });
  if ((await r.json()).ok) location.reload();
}
```

- [ ] **Step 11: Run all new tests**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/test_geo_confirmed_none_filter.py -v 2>&1 | tail -20
```

Expected: all 11 tests PASS.

- [ ] **Step 12: Run full suite**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/ -q 2>&1 | tail -5
```

Expected: all tests pass, no regressions.

- [ ] **Step 13: Run lint**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && make lint 2>&1 | tail -10
```

Expected: no errors.

- [ ] **Step 14: Add `has-plan` label, commit, post retrospective, push**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && gh issue edit 148 --add-label "has-plan"
```

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && git add reviewer/app.py reviewer/templates/library.html tests/test_geo_confirmed_none_filter.py && git commit -m "feat(#148): confirmed-none library filter + bulk undo

- app.py: parse ?confirmed_none=1; compute confirmed_none_count;
  pass both to template + filters dict
- library.html: 'Reviewed: no location' chip with badge count;
  contextual 'Undo: no location' button (visible only when filter
  active); clearNoLocation() JS mirrors markNoLocation()
- 4 new route/UI tests

Closes #148

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && gh issue comment 148 --body "## Retrospective

Size estimate: S ✓

**Files changed:** 4 (db/db.py, reviewer/app.py, reviewer/templates/library.html, tests/test_geo_confirmed_none_filter.py)
**Plan tasks:** 2
**Commits:** 2
**Tests added:** 11

No scope changes. Entirely analogous to the existing no_location filter — same pattern applied to the complementary confirmed_none state." && gh issue close 148
```

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && git push origin main
```
