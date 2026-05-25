# Album Rename and Delete — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add inline rename and delete controls to the `/albums` page, backed by `PATCH` and `DELETE` API routes that write to the DB and queue changes for the next `bp sync-albums` run.

**Architecture:** One new DB method (`rename_album`) + two new Flask routes (`PATCH /api/albums/<id>`, `DELETE /api/albums/<id>`) + inline HTML/JS on the albums page — no new templates, no new sync machinery. Rename is picked up automatically by the existing `sync_album_titles()` step in `bp sync-albums`. Delete reuses the existing `mark_album_deleted()` and the existing Flickr removal pipeline.

**Tech Stack:** SQLite / Python sqlite3, Flask, Jinja2, vanilla JS, pytest

---

## Files

| Action | File | What changes |
|--------|------|-------------|
| Modify | `db/db.py` | Add `rename_album(album_id, name)` after the existing `get_all_albums_with_counts` block |
| Modify | `reviewer/app.py` | Add `PATCH /api/albums/<id>` and `DELETE /api/albums/<id>` near the existing `GET /albums` route |
| Modify | `reviewer/templates/albums.html` | Inline rename UI, inline delete confirm, JS functions, CSS |
| Create | `tests/test_album_management_api.py` | Route integration tests |
| Modify | `README.md` | Note rename + delete |

---

## Task 1: DB method + backend routes + tests

**Files:**
- Modify: `db/db.py`
- Modify: `reviewer/app.py`
- Create: `tests/test_album_management_api.py`

### Background

**`rename_album` is the only new DB method.** It updates `albums.name` and `updated_at`. The timestamp helper `_now_iso()` is already defined at module level in `db/db.py` — do not redefine it.

`mark_album_deleted(album_id)` **already exists** in `db/db.py` — the DELETE route just calls it.

**Album lookup pattern:** Both routes need to confirm the album exists and is not already deleted. Use a direct SQL query — there is no `get_album_by_id` helper:
```python
row = db().conn.execute(
    "SELECT id, name FROM albums WHERE id = ? AND deleted_at IS NULL", (album_id,)
).fetchone()
if not row:
    return jsonify({"ok": False, "error": "album not found"}), 404
```

**Route conventions:** All API routes in this codebase use `{"ok": False, "error": "..."}` for errors and `{"ok": True, ...}` for success. The return type alias is `_JsonResp = Response | tuple[Response, int]` (already defined in `reviewer/app.py`).

**Test fixture:** Use `scope="function"` (not `scope="module"`) — both rename and delete mutate state irreversibly in the same DB, and function scope gives each test a clean slate.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_album_management_api.py`:

```python
"""
tests/test_album_management_api.py — integration tests for album rename and delete (#136, #137)

Run from repo root:
    python -m pytest tests/test_album_management_api.py -v
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

import reviewer.app as app_module
from db.db import Database


@pytest.fixture()
def client_and_albums():
    """Fresh DB + test client per test function — both operations mutate state."""
    with tempfile.TemporaryDirectory() as tmp:
        test_db = Database(Path(tmp) / "test.db")
        a1 = test_db.upsert_album("album-uuid-1", "Summer 2024")
        a2 = test_db.upsert_album("album-uuid-2", "Trips")
        app_module._db = test_db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test-secret"
        with app_module.app.test_client() as c:
            yield c, a1, a2, test_db
        app_module._db = None


class TestAlbumRename:
    def test_rename_valid(self, client_and_albums):
        c, a1, _, db = client_and_albums
        resp = c.patch(
            f"/api/albums/{a1}",
            data=json.dumps({"name": "Winter 2024"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["name"] == "Winter 2024"
        # Verify DB
        row = db.conn.execute("SELECT name FROM albums WHERE id = ?", (a1,)).fetchone()
        assert row["name"] == "Winter 2024"

    def test_rename_strips_whitespace(self, client_and_albums):
        c, a1, _, db = client_and_albums
        resp = c.patch(
            f"/api/albums/{a1}",
            data=json.dumps({"name": "  Padded Name  "}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert resp.get_json()["name"] == "Padded Name"

    def test_rename_empty_name_returns_400(self, client_and_albums):
        c, a1, _, _ = client_and_albums
        resp = c.patch(
            f"/api/albums/{a1}",
            data=json.dumps({"name": ""}),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert resp.get_json()["ok"] is False

    def test_rename_whitespace_only_returns_400(self, client_and_albums):
        c, a1, _, _ = client_and_albums
        resp = c.patch(
            f"/api/albums/{a1}",
            data=json.dumps({"name": "   "}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_rename_unknown_album_returns_404(self, client_and_albums):
        c, _, _, _ = client_and_albums
        resp = c.patch(
            "/api/albums/99999",
            data=json.dumps({"name": "Ghost"}),
            content_type="application/json",
        )
        assert resp.status_code == 404
        assert resp.get_json()["ok"] is False

    def test_rename_deleted_album_returns_404(self, client_and_albums):
        c, a1, _, db = client_and_albums
        db.mark_album_deleted(a1)
        resp = c.patch(
            f"/api/albums/{a1}",
            data=json.dumps({"name": "New Name"}),
            content_type="application/json",
        )
        assert resp.status_code == 404


class TestAlbumDelete:
    def test_delete_valid(self, client_and_albums):
        c, a1, _, db = client_and_albums
        resp = c.delete(f"/api/albums/{a1}")
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True
        # Verify deleted_at is set
        row = db.conn.execute("SELECT deleted_at FROM albums WHERE id = ?", (a1,)).fetchone()
        assert row["deleted_at"] is not None

    def test_delete_removes_from_albums_page(self, client_and_albums):
        c, a1, _, _ = client_and_albums
        c.delete(f"/api/albums/{a1}")
        resp = c.get("/albums")
        assert resp.status_code == 200
        assert "Summer 2024" not in resp.data.decode()

    def test_delete_unknown_album_returns_404(self, client_and_albums):
        c, _, _, _ = client_and_albums
        resp = c.delete("/api/albums/99999")
        assert resp.status_code == 404
        assert resp.get_json()["ok"] is False

    def test_delete_already_deleted_returns_404(self, client_and_albums):
        c, a1, _, db = client_and_albums
        db.mark_album_deleted(a1)
        resp = c.delete(f"/api/albums/{a1}")
        assert resp.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/test_album_management_api.py -v 2>&1 | head -30
```

Expected: failures with `405 Method Not Allowed` (routes don't exist yet).

- [ ] **Step 3: Add `rename_album` to `db/db.py`**

Find the `get_all_albums_with_counts` method (around line 1005). Add `rename_album` immediately after it:

```python
    def rename_album(self, album_id: int, name: str) -> None:
        """Update the album's display name and timestamp.

        The next bp sync-albums run calls sync_album_titles() which pushes
        albums.name to the Flickr photoset title for all albums with a
        flickr_set_id — no extra flag needed.
        """
        self.conn.execute(
            "UPDATE albums SET name = ?, updated_at = ? WHERE id = ?",
            (name, _now_iso(), album_id),
        )
        self.conn.commit()
```

- [ ] **Step 4: Add the two routes to `reviewer/app.py`**

Find the `albums_index` route (around line 735):

```python
@app.route("/albums")
def albums_index() -> str:
    albums = db().get_all_albums_with_counts()
    return render_template("albums.html", albums=albums)
```

Add the two new routes immediately after it:

```python
@app.route("/api/albums/<int:album_id>", methods=["PATCH"])
def api_album_rename(album_id: int) -> _JsonResp:
    row = db().conn.execute(
        "SELECT id, name FROM albums WHERE id = ? AND deleted_at IS NULL", (album_id,)
    ).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "album not found"}), 404

    data = request.get_json(silent=True) or {}
    name = data.get("name", "")
    if not isinstance(name, str) or not name.strip():
        return jsonify({"ok": False, "error": "name must be a non-empty string"}), 400

    name = name.strip()
    db().rename_album(album_id, name)
    return jsonify({"ok": True, "name": name})


@app.route("/api/albums/<int:album_id>", methods=["DELETE"])
def api_album_delete(album_id: int) -> _JsonResp:
    row = db().conn.execute(
        "SELECT id, name FROM albums WHERE id = ? AND deleted_at IS NULL", (album_id,)
    ).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "album not found"}), 404

    db().mark_album_deleted(album_id)
    return jsonify({"ok": True})
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/test_album_management_api.py -v
```

Expected: all 10 tests PASS.

- [ ] **Step 6: Run full suite**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/ -q
```

Expected: all tests pass (1195 + 10 new = 1205 passing).

- [ ] **Step 7: Run lint**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && make lint 2>&1 | tail -20
```

Fix any mypy errors before committing.

- [ ] **Step 8: Commit**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && git add db/db.py reviewer/app.py tests/test_album_management_api.py && git commit -m "feat(#136,#137): PATCH/DELETE /api/albums/<id> — rename and delete

- rename_album() DB method (queues Flickr title sync automatically)
- PATCH /api/albums/<id> — validate, strip, rename
- DELETE /api/albums/<id> — mark deleted; Flickr removal via bp sync-albums --remove

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 2: albums.html — inline rename and delete UI

**Files:**
- Modify: `reviewer/templates/albums.html`

### Background

The existing table has three columns: `Album | Photos | (link)`. This task extends the third column into a proper actions column and adds a hidden rename-mode section to the name column.

**Key JS convention:** All JS functions find their context via `el.closest('tr')` — no IDs on individual rows, no global state. Album ID and name are stored in `data-album-id` and `data-album-name` on the `<tr>`.

**`toast(msg, kind)`** is globally available from `base.html` (kind is `'ok'` or `'err'`).

- [ ] **Step 1: Replace `reviewer/templates/albums.html` entirely**

```html
{% extends "base.html" %}
{% block title %}Albums — Blue Pearmain{% endblock %}

{% block extra_style %}
<style>
.albums-page { max-width: 860px; margin: 32px auto; padding: 0 16px; }
.albums-page h1 { font-size: 20px; font-weight: 600; margin-bottom: 20px; color: var(--text); }
.albums-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.albums-table th {
  text-align: left; padding: 8px 12px;
  border-bottom: 1px solid var(--border);
  color: var(--muted); font-size: 11px;
  text-transform: uppercase; letter-spacing: .06em;
}
.albums-table td { padding: 10px 12px; border-bottom: 1px solid var(--border); color: var(--text); }
.albums-table tr:last-child td { border-bottom: none; }
.albums-table tr:hover td { background: var(--surface); }
.albums-table .count { color: var(--muted); text-align: right; }
.albums-table a { color: var(--accent); text-decoration: none; font-size: 12px; }
.albums-table a:hover { text-decoration: underline; }
.albums-empty { color: var(--muted); padding: 40px 0; text-align: center; font-size: 14px; }

/* ── Rename / delete controls ─────────────────────────── */
.btn-icon {
  background: none; border: none; cursor: pointer;
  color: var(--muted); font-size: 14px; padding: 2px 4px; border-radius: 3px;
}
.btn-icon:hover { color: var(--text); }
.btn-icon.btn-destructive:hover { color: #ff7a7a; }
.rename-input {
  font-size: 13px; padding: 2px 6px;
  border: 1px solid var(--border); border-radius: 3px;
  background: var(--surface); color: var(--text);
  width: 200px;
}
.btn-save {
  padding: 2px 8px; border-radius: 3px; font-size: 12px;
  background: var(--accent); color: #fff; border: none; cursor: pointer;
}
.btn-cancel-sm {
  padding: 2px 8px; border-radius: 3px; font-size: 12px;
  background: none; border: 1px solid var(--border); color: var(--muted); cursor: pointer;
}
.confirm-yes {
  padding: 2px 8px; border-radius: 3px; font-size: 12px;
  background: #aa2222; color: #fff; border: none; cursor: pointer;
}
.album-actions-normal { display: flex; align-items: center; gap: 8px; }
.album-actions-confirm { display: none; align-items: center; gap: 8px; }
.album-actions-confirm span { color: var(--muted); font-size: 12px; }
</style>
{% endblock %}

{% block content %}
<div class="albums-page">
  <h1>Albums</h1>
  {% if albums %}
  <table class="albums-table">
    <thead>
      <tr>
        <th>Album</th>
        <th class="count">Photos</th>
        <th></th>
      </tr>
    </thead>
    <tbody>
      {% for album in albums %}
      <tr data-album-id="{{ album.id }}" data-album-name="{{ album.name | e }}">
        <td class="album-name-cell">
          <span class="album-name-display">{{ album.name }}</span>
          <span class="album-name-edit" style="display:none">
            <input type="text" class="rename-input">
            <button class="btn-save" onclick="saveRename(this)">Save</button>
            <button class="btn-cancel-sm" onclick="cancelRename(this)">Cancel</button>
          </span>
        </td>
        <td class="count">{{ album.photo_count }}</td>
        <td>
          <span class="album-actions-normal">
            <button class="btn-icon" onclick="startRename(this)" title="Rename album">✏</button>
            <a href="{{ url_for('library', album_id=album.id) }}">View in library →</a>
            <button class="btn-icon btn-destructive" onclick="startDelete(this)" title="Delete album">🗑</button>
          </span>
          <span class="album-actions-confirm">
            <span>Delete?</span>
            <button class="confirm-yes" onclick="confirmDelete(this)">Confirm</button>
            <button class="btn-cancel-sm" onclick="cancelDelete(this)">Cancel</button>
          </span>
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  {% else %}
  <p class="albums-empty">No albums yet. Albums are imported from Apple Photos automatically.</p>
  {% endif %}
</div>

<script>
// ── Album rename ──────────────────────────────────────────────────────
function startRename(btn) {
  const row = btn.closest('tr');
  const input = row.querySelector('.rename-input');
  input.value = row.dataset.albumName;
  input.onkeydown = function(e) {
    if (e.key === 'Enter')  { e.preventDefault(); saveRename(input); }
    if (e.key === 'Escape') { cancelRename(input); }
  };
  row.querySelector('.album-name-display').style.display = 'none';
  row.querySelector('.album-name-edit').style.display = '';
  row.querySelector('.album-actions-normal').style.display = 'none';
  input.focus();
  input.select();
}

function cancelRename(el) {
  const row = el.closest('tr');
  row.querySelector('.album-name-edit').style.display = 'none';
  row.querySelector('.album-name-display').style.display = '';
  row.querySelector('.album-actions-normal').style.display = 'flex';
}

async function saveRename(el) {
  const row = el.closest('tr');
  const albumId = parseInt(row.dataset.albumId);
  const input = row.querySelector('.rename-input');
  const name = input.value.trim();
  if (!name) { toast('Album name cannot be empty', 'err'); return; }
  const saveBtn = row.querySelector('.btn-save');
  saveBtn.disabled = true;
  try {
    const r = await fetch(`/api/albums/${albumId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    });
    const data = await r.json();
    if (r.ok) {
      row.dataset.albumName = data.name;
      row.querySelector('.album-name-display').textContent = data.name;
      cancelRename(el);
      toast('Album renamed', 'ok');
    } else {
      toast('Error: ' + (data.error || 'unknown'), 'err');
      saveBtn.disabled = false;
    }
  } catch (e) {
    toast('Network error — try again', 'err');
    saveBtn.disabled = false;
  }
}

// ── Album delete ──────────────────────────────────────────────────────
function startDelete(btn) {
  const row = btn.closest('tr');
  row.querySelector('.album-actions-normal').style.display = 'none';
  row.querySelector('.album-actions-confirm').style.display = 'flex';
}

function cancelDelete(btn) {
  const row = btn.closest('tr');
  row.querySelector('.album-actions-confirm').style.display = 'none';
  row.querySelector('.album-actions-normal').style.display = 'flex';
}

async function confirmDelete(btn) {
  const row = btn.closest('tr');
  const albumId = parseInt(row.dataset.albumId);
  btn.disabled = true;
  try {
    const r = await fetch(`/api/albums/${albumId}`, { method: 'DELETE' });
    const data = await r.json();
    if (r.ok) {
      row.remove();
      toast('Album deleted — Flickr photoset will be removed on next sync', 'ok');
    } else {
      toast('Error: ' + (data.error || 'unknown'), 'err');
      cancelDelete(btn);
    }
  } catch (e) {
    toast('Network error — try again', 'err');
    cancelDelete(btn);
  }
}
</script>
{% endblock %}
```

- [ ] **Step 2: Run full test suite**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/ -q
```

Expected: all tests pass (template-only change, existing tests unaffected).

- [ ] **Step 3: Run lint**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && make lint 2>&1 | tail -10
```

- [ ] **Step 4: Manual smoke test**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python reviewer/app.py --config config/config.yml
```

Press `9` to navigate to `/albums`. Verify:
- ✏ appears on each row; clicking it activates the inline input, pre-filled with the current name
- Enter saves; Esc cancels; name updates in place without reload
- 🗑 reveals inline confirm; Cancel restores buttons; Confirm removes the row from the table

- [ ] **Step 5: Commit**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && git add reviewer/templates/albums.html && git commit -m "feat(#136,#137): inline rename and delete UI on /albums page

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 3: README, spec, labels, close issues, push

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-05-25-album-rename-delete-136-137.md`

- [ ] **Step 1: Get current test count**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/ -q 2>&1 | tail -3
```

Note the number (should be 1205 passed).

- [ ] **Step 2: Update README**

Find the line(s) describing the `/albums` page (added in #135). It currently says something like:

```
- `/albums` page (key `9`) lists all albums with photo counts and links to the filtered library view
```

Append to that line:

```
; albums can be renamed and deleted directly from this page
```

Also find the test count line and update it to the count from Step 1.

- [ ] **Step 3: Mark the spec done**

In `docs/superpowers/specs/2026-05-25-album-rename-delete-136-137.md`, change:

```
**Status:** Approved, awaiting implementation plan
```

to:

```
**Status:** ✓ done
```

- [ ] **Step 4: Add labels to GH issues**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && \
  gh issue edit 136 --add-label "has-plan" && \
  gh issue edit 137 --add-label "has-plan"
```

- [ ] **Step 5: Commit**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && git add README.md docs/superpowers/specs/2026-05-25-album-rename-delete-136-137.md && git commit -m "docs(#136,#137): README + mark spec done

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

- [ ] **Step 6: Commit the plan file**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && git add docs/superpowers/plans/2026-05-25-album-rename-delete-136-137.md && git commit -m "docs(#136,#137): implementation plan

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

- [ ] **Step 7: Push**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && git push origin main
```

- [ ] **Step 8: Close GH issues with retrospectives**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && gh issue close 136 --comment "Implemented in 2 commits.

- \`rename_album()\` DB method in \`db/db.py\`
- \`PATCH /api/albums/<id>\` in \`reviewer/app.py\`
- Inline ✏ rename UI in \`albums.html\` (click-to-edit, Enter/Esc, saves without reload)

**Retrospective:** size estimate S ✓ — 3 files changed, ~60 LOC net. Sync pickup is automatic via existing \`sync_album_titles()\` in \`bp sync-albums\` — no new sync machinery needed."
```

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && gh issue close 137 --comment "Implemented in 2 commits.

- \`DELETE /api/albums/<id>\` in \`reviewer/app.py\` — calls existing \`mark_album_deleted()\`
- Inline 🗑 delete confirm UI in \`albums.html\` (row-level confirm, removes row without reload)

**Retrospective:** size estimate S ✓ — 2 files changed, ~30 LOC net. Flickr photoset removal is handled by existing \`bp sync-albums --remove --apply\` pipeline — no new machinery needed."
```
