# Re-upload Dedup Phase 4: UI Cross-Linking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface `reupload` and `reupload_uncertain` duplicate groups in the existing `/duplicates` reviewer UI by fixing match-key parsing, adding notes JSON parsing, extending the sections loop, adding badge CSS, and rendering a structured evidence block with appropriate action buttons.

**Architecture:** The `/duplicates` route in `reviewer/app.py` already fetches all unresolved groups but silently drops reupload types because the sections loop only handles three types. This plan adds two new sections entries, fixes the match-key parser to handle the `reupload:{id1}:{id2}` format, parses the notes JSON blob in Python, and updates the template to render the evidence block and extended action buttons. No DB schema changes, no new routes, no Flickr API calls.

**Tech Stack:** Python 3, Flask, Jinja2, SQLite, pytest, Flask test client

---

## File Map

| File | Change |
|------|--------|
| `reviewer/app.py` | Fix match-key parsing (lines 441–453); add notes JSON parsing; add two section entries (lines 498–529) |
| `reviewer/templates/duplicates.html` | Add 2 badge CSS rules (after line 140); add evidence block (after line 241); extend action button gate (line 294) |
| `tests/test_review_ui.py` | Add `TestReuploadDuplicatesUI` class with 5 tests |

---

## Task 1: Tests for the `/duplicates` route — reupload group rendering

Write the failing tests first. These tests seed an in-memory DB with reupload groups and assert
the route renders them correctly. All tests will fail until Tasks 2 and 3 are complete.

**Files:**
- Modify: `tests/test_review_ui.py` (append new class at end of file)

---

- [ ] **Step 1: Locate the bottom of the test file**

Open `tests/test_review_ui.py`. The last class is `TestProposalJsDefensiveHandling` ending
around line 880. You will append the new test class after it.

The file imports at the top include `pytest`, `tempfile`, `Path`, and the `Database` class.
Check the existing `client_with_merge_group` fixture (around line 750) — it runs
`migrate_003` to create the `duplicate_groups` table. Use the same pattern.

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_review_ui.py`:

```python
# ---------------------------------------------------------------------------
# TestReuploadDuplicatesUI — GH #106
# ---------------------------------------------------------------------------

import json as _json


@pytest.fixture
def client_with_reupload_group():
    """DB with one unresolved reupload group: keeper + discard with notes JSON."""
    import tempfile as _tempfile
    with _tempfile.TemporaryDirectory() as tmp:
        from db.migrations.migrate_003_dimensions_and_dedup import run as migrate_003

        db_path = Path(tmp) / "test.db"
        test_db = Database(db_path)
        migrate_003(str(db_path))

        notes = _json.dumps({
            "summary": "Higher-res Flickr copy of a local photo",
            "upload_session_gap": "14 days",
            "filename_match": True,
            "dimension_ratio": 0.85,
            "keeper_flickr_id": "48910000",
            "discard_flickr_id": "48900000",
        })

        # Keeper (higher-res Flickr-only)
        keeper_id = test_db.upsert_photo({
            "flickr_id": "48910000",
            "flickr_secret": "sec1",
            "flickr_server": "65535",
            "original_filename": "IMG_001.JPG",
            "date_taken": "2024-06-01 12:00:00",
            "privacy_state": "candidate_public",
            "width": 4000,
            "height": 3000,
        })

        # Discard (lower-res, already marked)
        discard_id = test_db.upsert_photo({
            "flickr_id": "48900000",
            "flickr_secret": "sec2",
            "flickr_server": "65535",
            "original_filename": "IMG_001.JPG",
            "date_taken": "2024-06-01 12:00:00",
            "privacy_state": "duplicate_flickr",
        })

        test_db.conn.execute(
            "INSERT INTO duplicate_groups (match_key, group_type, photo_count, notes)"
            " VALUES (?,?,?,?)",
            ("reupload:48900000:48910000", "reupload", 2, notes),
        )
        group_id = test_db.conn.execute(
            "SELECT last_insert_rowid() AS id"
        ).fetchone()["id"]
        test_db.conn.execute(
            "UPDATE photos SET duplicate_group_id=?, duplicate_role='keeper' WHERE id=?",
            (group_id, keeper_id),
        )
        test_db.conn.execute(
            "UPDATE photos SET duplicate_group_id=?, duplicate_role='discard' WHERE id=?",
            (group_id, discard_id),
        )
        test_db.conn.commit()

        import reviewer.app as app_module
        app_module._db = test_db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test-secret"

        with app_module.app.test_client() as c:
            yield c, test_db, group_id

        app_module._db = None


@pytest.fixture
def client_with_reupload_uncertain_group():
    """DB with one unresolved reupload_uncertain group (no notes JSON)."""
    import tempfile as _tempfile
    with _tempfile.TemporaryDirectory() as tmp:
        from db.migrations.migrate_003_dimensions_and_dedup import run as migrate_003

        db_path = Path(tmp) / "test.db"
        test_db = Database(db_path)
        migrate_003(str(db_path))

        keeper_id = test_db.upsert_photo({
            "flickr_id": "48920000",
            "flickr_secret": "sec3",
            "flickr_server": "65535",
            "original_filename": "IMG_002.JPG",
            "date_taken": "2024-07-01 10:00:00",
            "privacy_state": "candidate_public",
        })
        discard_id = test_db.upsert_photo({
            "flickr_id": "48910001",
            "flickr_secret": "sec4",
            "flickr_server": "65535",
            "original_filename": "IMG_002.JPG",
            "date_taken": "2024-07-01 10:00:00",
            "privacy_state": "candidate_public",
        })

        test_db.conn.execute(
            "INSERT INTO duplicate_groups (match_key, group_type, photo_count)"
            " VALUES (?,?,?)",
            ("reupload:48910001:48920000", "reupload_uncertain", 2),
        )
        group_id = test_db.conn.execute(
            "SELECT last_insert_rowid() AS id"
        ).fetchone()["id"]
        test_db.conn.execute(
            "UPDATE photos SET duplicate_group_id=?, duplicate_role='keeper' WHERE id=?",
            (group_id, keeper_id),
        )
        test_db.conn.execute(
            "UPDATE photos SET duplicate_group_id=?, duplicate_role='discard' WHERE id=?",
            (group_id, discard_id),
        )
        test_db.conn.commit()

        import reviewer.app as app_module
        app_module._db = test_db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test-secret"

        with app_module.app.test_client() as c:
            yield c, test_db, group_id

        app_module._db = None


class TestReuploadDuplicatesUI:
    """GH #106 — reupload/reupload_uncertain groups appear in /duplicates."""

    def test_reupload_group_appears_in_duplicates_page(
        self, client_with_reupload_group
    ):
        c, _, _ = client_with_reupload_group
        resp = c.get("/duplicates")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "Re-upload duplicate" in html
        assert "48910000" in html
        assert "48900000" in html

    def test_reupload_uncertain_group_appears_in_duplicates_page(
        self, client_with_reupload_uncertain_group
    ):
        c, _, _ = client_with_reupload_uncertain_group
        resp = c.get("/duplicates")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "Possible re-upload" in html
        assert "Mark reviewed" in html

    def test_reupload_notes_fields_rendered(self, client_with_reupload_group):
        c, _, _ = client_with_reupload_group
        resp = c.get("/duplicates")
        html = resp.data.decode()
        assert "14 days" in html
        assert "Yes" in html       # filename_match=True → "Yes"
        assert "0.85" in html

    def test_reupload_match_key_no_crash(self, client_with_reupload_group):
        """match_key 'reupload:{id1}:{id2}' must not cause a 500."""
        c, _, _ = client_with_reupload_group
        resp = c.get("/duplicates")
        assert resp.status_code == 200

    def test_reupload_null_notes_no_crash(self, client_with_reupload_uncertain_group):
        """NULL notes must not cause a 500."""
        c, _, _ = client_with_reupload_uncertain_group
        resp = c.get("/duplicates")
        assert resp.status_code == 200
```

- [ ] **Step 3: Run the tests to confirm they fail**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain"
python -m pytest tests/test_review_ui.py::TestReuploadDuplicatesUI -v 2>&1 | tail -20
```

Expected: all 5 tests FAIL. The first failure will be something like `assert "Re-upload duplicate" in html` — the section is not rendered because `reupload` is not in the sections loop. If any test errors (not fails), read the traceback before proceeding.

- [ ] **Step 4: Commit the failing tests**

```bash
git add tests/test_review_ui.py
git commit -m "test: add failing tests for reupload duplicate UI (#106)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 2: Fix `app.py` — match-key parsing, notes JSON, and sections entries

Make the 5 tests pass by changing `reviewer/app.py`.

**Files:**
- Modify: `reviewer/app.py` (lines ~441–529)

---

- [ ] **Step 1: Add `import json` at the top of `app.py`**

Open `reviewer/app.py`. Find the imports block at the top. Add `import json` if it is not
already present (grep first: `grep -n "^import json" reviewer/app.py`).

- [ ] **Step 2: Fix the match-key parsing and add notes parsing**

Find this block (around line 441):

```python
        if gid not in groups:
            key = r["match_key"] or ""
            filename, _, date_key = key.partition("|")
            groups[gid] = {
                "id": gid,
                "match_key": key,
                "group_type": r["group_type"],
                "photo_count": r["photo_count"],
                "keeper_id": r["keeper_id"],
                "resolved": r["resolved"],
                "notes": r["notes"],
                "filename": filename,
                "date_key": date_key,
                "photos": [],
            }
```

Replace it with:

```python
        if gid not in groups:
            key = r["match_key"] or ""
            gtype = r["group_type"]
            if gtype in ("reupload", "reupload_uncertain"):
                parts = key.split(":")
                filename = f"{parts[1]} → {parts[2]}" if len(parts) == 3 else key
                date_key = ""
                try:
                    notes_parsed = json.loads(r["notes"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    notes_parsed = {}
            else:
                filename, _, date_key = key.partition("|")
                notes_parsed = {}
            groups[gid] = {
                "id": gid,
                "match_key": key,
                "group_type": gtype,
                "photo_count": r["photo_count"],
                "keeper_id": r["keeper_id"],
                "resolved": r["resolved"],
                "notes": r["notes"],
                "notes_parsed": notes_parsed,
                "filename": filename,
                "date_key": date_key,
                "photos": [],
            }
```

- [ ] **Step 3: Add reupload entries to the sections loop**

Find this block (around line 499):

```python
    sections = []
    for gtype, label, description in (
        (
            "snapbridge",
            ...
        ),
        (
            "device_upload",
            ...
        ),
        (
            "uncertain",
            ...
        ),
    ):
```

Add two more tuples after `"uncertain"` (inside the same tuple list, before the closing `)`):

```python
        (
            "reupload",
            "Re-upload duplicate",
            "Higher-res Flickr copy of a local photo — discard has been marked duplicate_flickr.",
        ),
        (
            "reupload_uncertain",
            "Possible re-upload",
            "Probable re-upload — needs human review before marking or deleting.",
        ),
```

- [ ] **Step 4: Run the tests — expect most to pass**

```bash
python -m pytest tests/test_review_ui.py::TestReuploadDuplicatesUI -v 2>&1 | tail -20
```

Expected: `test_reupload_match_key_no_crash` and `test_reupload_null_notes_no_crash` should
PASS now. The remaining 3 tests (`test_reupload_group_appears`, `test_reupload_uncertain_appears`,
`test_reupload_notes_fields_rendered`) will still fail because the template hasn't been updated
yet. That is expected — continue to Task 3.

- [ ] **Step 5: Run the full test suite to check for regressions**

```bash
python -m pytest tests/ -q 2>&1 | tail -10
```

Expected: all previously passing tests still pass; the 3 new template-dependent tests still fail.
If any previously passing test is now failing, fix the regression before continuing.

- [ ] **Step 6: Commit**

```bash
git add reviewer/app.py
git commit -m "feat: fix match-key parsing and add reupload sections to /duplicates (#106)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 3: Update `duplicates.html` — badge CSS, evidence block, action buttons

Make the remaining 3 failing tests pass.

**Files:**
- Modify: `reviewer/templates/duplicates.html`

---

- [ ] **Step 1: Add badge CSS for reupload types**

Find this block (around line 138):

```css
/* Type badges */
.badge-snapbridge    { background: #0d3a5c; color: #6ab4f5; }
.badge-device_upload { background: #1e1e1e; color: #aaa; }
.badge-uncertain     { background: #4a2800; color: #f5a623; }
```

Add two lines immediately after `.badge-uncertain`:

```css
.badge-reupload           { background: #6f42c1; color: #fff; }
.badge-reupload_uncertain { background: #fd7e14; color: #fff; }
```

- [ ] **Step 2: Add the evidence block after the existing notes div**

Find this block (around line 239):

```html
      {% if group.notes %}
      <div class="dup-notes">{{ group.notes }}</div>
      {% endif %}
```

Replace it with:

```html
      {% if section.type in ('reupload', 'reupload_uncertain') and group.notes_parsed %}
      <div class="dup-notes">
        <span>Gap: {{ group.notes_parsed.upload_session_gap or '—' }}</span>
        &nbsp;·&nbsp;
        <span>Filename match: {{ 'Yes' if group.notes_parsed.filename_match else 'No' }}</span>
        &nbsp;·&nbsp;
        <span>Dimension ratio: {{ group.notes_parsed.dimension_ratio or '—' }}</span>
        {% if group.notes_parsed.summary %}
        <div style="margin-top:4px">{{ group.notes_parsed.summary }}</div>
        {% endif %}
      </div>
      {% elif group.notes %}
      <div class="dup-notes">{{ group.notes }}</div>
      {% endif %}
```

- [ ] **Step 3: Extend the action button gate**

Find this block (around line 294):

```html
      <div class="dup-actions">
        {% if section.type in ('snapbridge', 'device_upload') %}
        <button class="btn btn-primary"
                onclick="resolveGroup({{ group.id }}, this)">
          ✓ Confirm resolution
        </button>
        {% endif %}
```

Replace it with:

```html
      <div class="dup-actions">
        {% if section.type in ('snapbridge', 'device_upload', 'reupload') %}
        <button class="btn btn-primary"
                onclick="resolveGroup({{ group.id }}, this)">
          ✓ Confirm resolution
        </button>
        {% elif section.type == 'reupload_uncertain' %}
        <button class="btn btn-primary"
                onclick="resolveGroup({{ group.id }}, this)">
          ✓ Mark reviewed
        </button>
        {% endif %}
```

Both buttons call the existing `resolveGroup()` JS function which POSTs to
`/api/duplicates/{groupId}/resolve` — no JS changes needed.

- [ ] **Step 4: Run the full test suite**

```bash
python -m pytest tests/ -q 2>&1 | tail -10
```

Expected: all 5 `TestReuploadDuplicatesUI` tests PASS; all previously passing tests still pass.
If the count shown matches the previous total + 5, the implementation is complete.

- [ ] **Step 5: Commit**

```bash
git add reviewer/templates/duplicates.html
git commit -m "feat: add reupload duplicate sections to /duplicates UI (#106)

Adds badge CSS, evidence block (gap / filename match / dimension ratio),
and Confirm resolution / Mark reviewed buttons for reupload group types.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 4: Update README and docs, close issue

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-05-19-reupload-dedup-106-phase4-design.md`

---

- [ ] **Step 1: Update the test count in README.md**

Run `python -m pytest tests/ -q 2>&1 | tail -3` to get the new count. Find the two places in
`README.md` where the test count appears (search: `grep -n "775\|780\|test" README.md | head -20`)
and update both to the new number.

- [ ] **Step 2: Update the spec doc status**

Open `docs/superpowers/specs/2026-05-19-reupload-dedup-106-phase4-design.md`. Add a one-line
status note at the top of the file, immediately after the `**GitHub issue:** #106` line:

```markdown
**Status:** ✓ done
```

- [ ] **Step 3: Commit README and docs**

```bash
git add README.md docs/superpowers/specs/2026-05-19-reupload-dedup-106-phase4-design.md
git commit -m "docs: update test count and mark Phase 4 spec done (#106)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

- [ ] **Step 4: Push to origin**

```bash
git push
```

- [ ] **Step 5: Close the GitHub issue**

```bash
gh issue close 106 --comment "Phase 4 complete. Added reupload/reupload_uncertain sections to /duplicates UI: fixed match-key parsing, notes JSON evidence block (gap, filename match, dimension ratio), badge CSS, and Confirm resolution / Mark reviewed action buttons. 5 new tests added."
```
