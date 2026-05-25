# Album Membership Editing — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add album membership editing to the library view — bulk add photos to existing albums, remove from the currently-filtered album, and a lightweight `/albums` index page.

**Architecture:** Three new DB methods handle read and write (no commit internally — the route commits once for atomicity). Three new Flask routes cover the albums page, membership writes, and membership reads. The library template gains two new action-bar buttons and two panels; `base.html` gains a nav entry at key `9`.

**Tech Stack:** SQLite / Python sqlite3, Flask, Jinja2, vanilla JS (existing patterns), pytest

---

## Files

| Action | File | What changes |
|--------|------|-------------|
| Modify | `db/db.py` | 4 new methods: `get_all_albums_with_counts`, `get_album_membership_for_photos`, `bulk_upsert_photo_albums`, `bulk_remove_photo_albums` |
| Modify | `reviewer/app.py` | 3 new routes + pass `current_album` in `/library` |
| Create | `reviewer/templates/albums.html` | New albums index page |
| Modify | `reviewer/templates/base.html` | Albums nav entry (desktop + mobile, key 9) |
| Modify | `reviewer/templates/library.html` | Album add/remove buttons, panels, JS |
| Create | `tests/test_db_album_membership.py` | DB method unit tests |
| Create | `tests/test_album_membership_api.py` | Route integration tests |
| Modify | `README.md` | Note album membership editing |

---

## Task 1: DB methods

**Files:**
- Modify: `db/db.py` (after the existing `get_all_albums` method, around line 1004)
- Create: `tests/test_db_album_membership.py`

### Background

The `photo_albums` table has columns: `photo_id`, `album_id`, `flickr_pushed` (int), `pushed_at` (text), `removed_at` (text nullable — tombstone).

- `removed_at IS NULL` → active membership
- `removed_at IS NOT NULL` → tombstoned (queued for Flickr removal)

`_now_iso()` is a module-level helper in `db/db.py` that returns `datetime.now(timezone.utc).isoformat()`.

The two write methods (`bulk_upsert_photo_albums`, `bulk_remove_photo_albums`) do **not** call `self.conn.commit()` — the caller (the route) commits once after all operations to ensure atomicity.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_db_album_membership.py`:

```python
"""
tests/test_db_album_membership.py — unit tests for album membership DB methods (#135)

Run from repo root:
    python -m pytest tests/test_db_album_membership.py -v
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from db.db import Database


def _make_db() -> tuple[Database, str]:
    tmp = tempfile.mkdtemp()
    db = Database(Path(tmp) / "test.db")
    return db, tmp


def _seed(db: Database) -> tuple[int, int, int, int]:
    """Return (photo1_id, photo2_id, album1_id, album2_id)."""
    p1 = db.upsert_photo({
        "uuid": "u1", "original_filename": "A.JPG",
        "privacy_state": "needs_review",
        "apple_persons": [], "apple_labels": [],
        "apple_unknown_faces": 0, "apple_named_faces": 0,
    })
    p2 = db.upsert_photo({
        "uuid": "u2", "original_filename": "B.JPG",
        "privacy_state": "needs_review",
        "apple_persons": [], "apple_labels": [],
        "apple_unknown_faces": 0, "apple_named_faces": 0,
    })
    a1 = db.upsert_album("album-uuid-1", "Summer 2024")
    a2 = db.upsert_album("album-uuid-2", "Trips")
    return p1, p2, a1, a2


class TestGetAllAlbumsWithCounts(unittest.TestCase):
    def setUp(self):
        self.db, self.tmp = _make_db()
        self.p1, self.p2, self.a1, self.a2 = _seed(self.db)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp)

    def test_returns_all_albums(self):
        albums = self.db.get_all_albums_with_counts()
        names = {a["name"] for a in albums}
        self.assertIn("Summer 2024", names)
        self.assertIn("Trips", names)

    def test_photo_count_zero_when_no_members(self):
        albums = self.db.get_all_albums_with_counts()
        for a in albums:
            self.assertEqual(a["photo_count"], 0)

    def test_photo_count_reflects_active_members(self):
        self.db.upsert_photo_album(self.p1, self.a1)
        self.db.upsert_photo_album(self.p2, self.a1)
        albums = self.db.get_all_albums_with_counts()
        summer = next(a for a in albums if a["name"] == "Summer 2024")
        self.assertEqual(summer["photo_count"], 2)

    def test_tombstoned_members_not_counted(self):
        self.db.upsert_photo_album(self.p1, self.a1)
        self.db.mark_photo_album_removed(self.p1, self.a1)
        albums = self.db.get_all_albums_with_counts()
        summer = next(a for a in albums if a["name"] == "Summer 2024")
        self.assertEqual(summer["photo_count"], 0)

    def test_deleted_albums_excluded(self):
        self.db.mark_album_deleted(self.a2)
        albums = self.db.get_all_albums_with_counts()
        names = {a["name"] for a in albums}
        self.assertNotIn("Trips", names)


class TestGetAlbumMembershipForPhotos(unittest.TestCase):
    def setUp(self):
        self.db, self.tmp = _make_db()
        self.p1, self.p2, self.a1, self.a2 = _seed(self.db)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp)

    def test_empty_photo_ids_returns_empty_dict(self):
        result = self.db.get_album_membership_for_photos([])
        self.assertEqual(result, {})

    def test_returns_active_memberships(self):
        self.db.upsert_photo_album(self.p1, self.a1)
        self.db.upsert_photo_album(self.p2, self.a1)
        result = self.db.get_album_membership_for_photos([self.p1, self.p2])
        self.assertIn(self.a1, result)
        self.assertIn(self.p1, result[self.a1])
        self.assertIn(self.p2, result[self.a1])

    def test_tombstoned_memberships_excluded(self):
        self.db.upsert_photo_album(self.p1, self.a1)
        self.db.mark_photo_album_removed(self.p1, self.a1)
        result = self.db.get_album_membership_for_photos([self.p1])
        self.assertNotIn(self.a1, result)

    def test_only_queried_photos_returned(self):
        self.db.upsert_photo_album(self.p1, self.a1)
        self.db.upsert_photo_album(self.p2, self.a1)
        result = self.db.get_album_membership_for_photos([self.p1])
        if self.a1 in result:
            self.assertNotIn(self.p2, result[self.a1])


class TestBulkUpsertPhotoAlbums(unittest.TestCase):
    def setUp(self):
        self.db, self.tmp = _make_db()
        self.p1, self.p2, self.a1, self.a2 = _seed(self.db)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp)

    def test_adds_new_memberships(self):
        n = self.db.bulk_upsert_photo_albums([self.p1, self.p2], self.a1)
        self.db.conn.commit()
        self.assertEqual(n, 2)
        row = self.db.conn.execute(
            "SELECT removed_at FROM photo_albums WHERE photo_id=? AND album_id=?",
            (self.p1, self.a1),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertIsNone(row["removed_at"])

    def test_idempotent_already_active(self):
        self.db.upsert_photo_album(self.p1, self.a1)
        n = self.db.bulk_upsert_photo_albums([self.p1], self.a1)
        self.db.conn.commit()
        self.assertEqual(n, 0)  # already active — not counted
        count = self.db.conn.execute(
            "SELECT COUNT(*) FROM photo_albums WHERE photo_id=? AND album_id=?",
            (self.p1, self.a1),
        ).fetchone()[0]
        self.assertEqual(count, 1)  # still exactly one row

    def test_reactivates_tombstoned_row(self):
        self.db.upsert_photo_album(self.p1, self.a1)
        self.db.mark_photo_album_removed(self.p1, self.a1)
        n = self.db.bulk_upsert_photo_albums([self.p1], self.a1)
        self.db.conn.commit()
        self.assertEqual(n, 1)  # re-activation counted
        row = self.db.conn.execute(
            "SELECT removed_at FROM photo_albums WHERE photo_id=? AND album_id=?",
            (self.p1, self.a1),
        ).fetchone()
        self.assertIsNone(row["removed_at"])

    def test_empty_photo_ids_returns_zero(self):
        n = self.db.bulk_upsert_photo_albums([], self.a1)
        self.db.conn.commit()
        self.assertEqual(n, 0)


class TestBulkRemovePhotoAlbums(unittest.TestCase):
    def setUp(self):
        self.db, self.tmp = _make_db()
        self.p1, self.p2, self.a1, self.a2 = _seed(self.db)
        self.db.upsert_photo_album(self.p1, self.a1)
        self.db.upsert_photo_album(self.p2, self.a1)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp)

    def test_tombstones_active_members(self):
        n = self.db.bulk_remove_photo_albums([self.p1, self.p2], self.a1)
        self.db.conn.commit()
        self.assertEqual(n, 2)
        row = self.db.conn.execute(
            "SELECT removed_at FROM photo_albums WHERE photo_id=? AND album_id=?",
            (self.p1, self.a1),
        ).fetchone()
        self.assertIsNotNone(row["removed_at"])

    def test_idempotent_already_tombstoned(self):
        self.db.mark_photo_album_removed(self.p1, self.a1)
        n = self.db.bulk_remove_photo_albums([self.p1], self.a1)
        self.db.conn.commit()
        self.assertEqual(n, 0)  # already tombstoned — not double-counted

    def test_empty_photo_ids_returns_zero(self):
        n = self.db.bulk_remove_photo_albums([], self.a1)
        self.db.conn.commit()
        self.assertEqual(n, 0)


class TestAddRemoveCycleInvariant(unittest.TestCase):
    """After repeated add/remove/add cycles, exactly one row must exist and be active."""

    def setUp(self):
        self.db, self.tmp = _make_db()
        self.p1, _, self.a1, _ = _seed(self.db)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp)

    def test_single_row_after_repeated_cycles(self):
        for _ in range(3):
            self.db.bulk_upsert_photo_albums([self.p1], self.a1)
            self.db.conn.commit()
            self.db.bulk_remove_photo_albums([self.p1], self.a1)
            self.db.conn.commit()
        self.db.bulk_upsert_photo_albums([self.p1], self.a1)
        self.db.conn.commit()

        rows = self.db.conn.execute(
            "SELECT removed_at FROM photo_albums WHERE photo_id=? AND album_id=?",
            (self.p1, self.a1),
        ).fetchall()
        self.assertEqual(len(rows), 1, "Must be exactly one row")
        self.assertIsNone(rows[0]["removed_at"], "Row must be active")
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/test_db_album_membership.py -v 2>&1 | head -40
```

Expected: failures with `AttributeError: 'Database' object has no attribute 'bulk_upsert_photo_albums'` (and similar).

- [ ] **Step 3: Add the four DB methods to `db/db.py`**

Find the existing `get_all_albums` method (around line 995). Add the following four methods directly after it, before the `# ── Bulk operations` section:

```python
    def get_all_albums_with_counts(self) -> list[dict]:
        """Return all non-deleted albums with active photo membership counts, ordered by name."""
        rows = self.conn.execute(
            """SELECT a.id, a.name, a.flickr_set_id,
                      COUNT(pa.photo_id) AS photo_count
               FROM albums a
               LEFT JOIN photo_albums pa ON pa.album_id = a.id
                                         AND pa.removed_at IS NULL
               WHERE a.deleted_at IS NULL
               GROUP BY a.id
               ORDER BY a.name""",
        ).fetchall()
        return [dict(r) for r in rows]

    def get_album_membership_for_photos(self, photo_ids: list[int]) -> dict[int, set[int]]:
        """
        Return {album_id: {photo_id, ...}} for all active memberships among the given photo_ids.
        Used to show current membership state in the Add-to-album panel.
        Empty list input returns empty dict.
        """
        if not photo_ids:
            return {}
        placeholders = ",".join("?" * len(photo_ids))
        rows = self.conn.execute(
            f"""SELECT album_id, photo_id
                FROM photo_albums
                WHERE photo_id IN ({placeholders})
                  AND removed_at IS NULL""",
            photo_ids,
        ).fetchall()
        result: dict[int, set[int]] = {}
        for row in rows:
            result.setdefault(row["album_id"], set()).add(row["photo_id"])
        return result

    def bulk_upsert_photo_albums(self, photo_ids: list[int], album_id: int) -> int:
        """
        Add photo_ids to album_id without committing — caller must commit.
        Idempotent: already-active rows are no-ops (not counted).
        Tombstoned rows have removed_at cleared and are counted as re-activated.
        Returns count of newly inserted or re-activated rows.
        """
        if not photo_ids:
            return 0
        added = 0
        for photo_id in photo_ids:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO photo_albums (photo_id, album_id) VALUES (?, ?)",
                (photo_id, album_id),
            )
            if cur.rowcount > 0:
                added += 1
            else:
                cur2 = self.conn.execute(
                    "UPDATE photo_albums SET removed_at = NULL "
                    "WHERE photo_id = ? AND album_id = ? AND removed_at IS NOT NULL",
                    (photo_id, album_id),
                )
                added += cur2.rowcount
        return added

    def bulk_remove_photo_albums(self, photo_ids: list[int], album_id: int) -> int:
        """
        Tombstone photo_ids in album_id without committing — caller must commit.
        Only tombstones active (non-tombstoned) rows; already-tombstoned rows are no-ops.
        Returns count of newly tombstoned rows.
        """
        if not photo_ids:
            return 0
        removed = 0
        _now = _now_iso()
        for photo_id in photo_ids:
            cur = self.conn.execute(
                "UPDATE photo_albums SET removed_at = ? "
                "WHERE photo_id = ? AND album_id = ? AND removed_at IS NULL",
                (_now, photo_id, album_id),
            )
            removed += cur.rowcount
        return removed
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/test_db_album_membership.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Run full suite**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/ -q
```

Expected: all tests pass.

- [ ] **Step 6: Run make lint**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && make lint 2>&1 | tail -20
```

Fix any mypy errors in the new methods before committing.

- [ ] **Step 7: Commit**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && git add db/db.py tests/test_db_album_membership.py && git commit -m "feat(#135): DB methods for album membership editing

- get_all_albums_with_counts — albums index page
- get_album_membership_for_photos — panel population
- bulk_upsert_photo_albums — atomic add, caller commits
- bulk_remove_photo_albums — atomic remove, caller commits

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 2: Backend routes

**Files:**
- Modify: `reviewer/app.py`
- Create: `tests/test_album_membership_api.py`

### Background

The Flask app uses `db()` to get the database singleton. Routes return either `render_template(...)` or `jsonify(...)`. The type alias `_JsonResp = tuple[Response, int] | Response` is used in the file.

For the `POST /api/album-membership` route: all adds and removes execute in a single DB transaction — the route calls `db().conn.commit()` once after all batch method calls. On exception, it calls `db().conn.rollback()`.

Album ID validation: check against `db().get_all_albums()` (returns `[{id, name, flickr_set_id}]`). Photo ID validation: check non-empty and all ints — existence not validated (internal tool, IDs come from BP's own UI).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_album_membership_api.py`:

```python
"""
tests/test_album_membership_api.py — integration tests for album membership routes (#135)

Run from repo root:
    python -m pytest tests/test_album_membership_api.py -v
"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import pytest

import reviewer.app as app_module
from db.db import Database


def _photo_payload(i: int) -> dict:
    return {
        "uuid": f"u{i}",
        "original_filename": f"IMG_{i:04d}.JPG",
        "privacy_state": "needs_review",
        "apple_persons": [], "apple_labels": [],
        "apple_unknown_faces": 0, "apple_named_faces": 0,
    }


@pytest.fixture(scope="module")
def client_with_albums():
    with tempfile.TemporaryDirectory() as tmp:
        test_db = Database(Path(tmp) / "test.db")
        p1 = test_db.upsert_photo(_photo_payload(1))
        p2 = test_db.upsert_photo(_photo_payload(2))
        p3 = test_db.upsert_photo(_photo_payload(3))
        a1 = test_db.upsert_album("album-uuid-1", "Summer 2024")
        a2 = test_db.upsert_album("album-uuid-2", "Trips")
        app_module._db = test_db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test-secret"
        with app_module.app.test_client() as c:
            yield c, p1, p2, p3, a1, a2, test_db
        app_module._db = None


class TestAlbumsIndexPage:
    def test_albums_page_200(self, client_with_albums):
        c, *_ = client_with_albums
        resp = c.get("/albums")
        assert resp.status_code == 200

    def test_albums_page_shows_album_names(self, client_with_albums):
        c, _, _, _, a1, a2, db = client_with_albums
        resp = c.get("/albums")
        html = resp.data.decode()
        assert "Summer 2024" in html
        assert "Trips" in html

    def test_albums_page_links_to_library(self, client_with_albums):
        c, _, _, _, a1, _, _ = client_with_albums
        resp = c.get("/albums")
        html = resp.data.decode()
        assert f"/library?album_id={a1}" in html


class TestAlbumMembershipWrite:
    def test_add_valid(self, client_with_albums):
        c, p1, p2, _, a1, _, db = client_with_albums
        resp = c.post(
            "/api/album-membership",
            data=json.dumps({"photo_ids": [p1, p2], "add": [a1]}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["added"] == 2
        # Verify DB state
        row = db.conn.execute(
            "SELECT removed_at, flickr_pushed FROM photo_albums WHERE photo_id=? AND album_id=?",
            (p1, a1),
        ).fetchone()
        assert row is not None
        assert row["removed_at"] is None
        assert row["flickr_pushed"] == 0

    def test_remove_valid(self, client_with_albums):
        c, p1, p2, _, a1, _, db = client_with_albums
        # Ensure photos are members first
        db.upsert_photo_album(p1, a1)
        db.upsert_photo_album(p2, a1)
        resp = c.post(
            "/api/album-membership",
            data=json.dumps({"photo_ids": [p1, p2], "remove": [a1]}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["removed"] == 2
        row = db.conn.execute(
            "SELECT removed_at FROM photo_albums WHERE photo_id=? AND album_id=?",
            (p1, a1),
        ).fetchone()
        assert row["removed_at"] is not None

    def test_add_and_remove_in_same_request(self, client_with_albums):
        c, p1, p2, p3, a1, a2, db = client_with_albums
        db.upsert_photo_album(p3, a1)
        resp = c.post(
            "/api/album-membership",
            data=json.dumps({"photo_ids": [p1, p3], "add": [a2], "remove": [a1]}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["added"] >= 1
        assert data["removed"] >= 1

    def test_add_idempotent(self, client_with_albums):
        c, p1, _, _, a1, _, db = client_with_albums
        db.upsert_photo_album(p1, a1)  # already a member
        resp = c.post(
            "/api/album-membership",
            data=json.dumps({"photo_ids": [p1], "add": [a1]}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        # No duplicate rows
        count = db.conn.execute(
            "SELECT COUNT(*) FROM photo_albums WHERE photo_id=? AND album_id=?",
            (p1, a1),
        ).fetchone()[0]
        assert count == 1

    def test_empty_photo_ids_returns_400(self, client_with_albums):
        c, *_ = client_with_albums
        resp = c.post(
            "/api/album-membership",
            data=json.dumps({"photo_ids": [], "add": [1]}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_invalid_album_id_returns_400(self, client_with_albums):
        c, p1, _, _, _, _, _ = client_with_albums
        resp = c.post(
            "/api/album-membership",
            data=json.dumps({"photo_ids": [p1], "add": [99999]}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_missing_photo_ids_returns_400(self, client_with_albums):
        c, *_ = client_with_albums
        resp = c.post(
            "/api/album-membership",
            data=json.dumps({"add": [1]}),
            content_type="application/json",
        )
        assert resp.status_code == 400


class TestAlbumMembershipRead:
    def test_get_membership_returns_200(self, client_with_albums):
        c, p1, p2, _, a1, _, db = client_with_albums
        db.upsert_photo_album(p1, a1)
        resp = c.get(f"/api/album-membership?photo_ids={p1},{p2}")
        assert resp.status_code == 200

    def test_get_membership_includes_active_albums(self, client_with_albums):
        c, p1, _, _, a1, _, db = client_with_albums
        db.upsert_photo_album(p1, a1)
        resp = c.get(f"/api/album-membership?photo_ids={p1}")
        data = resp.get_json()
        # JSON keys are strings
        assert str(a1) in data["membership"]
        assert p1 in data["membership"][str(a1)]

    def test_get_membership_excludes_tombstoned(self, client_with_albums):
        c, p1, _, _, a1, _, db = client_with_albums
        db.upsert_photo_album(p1, a1)
        db.mark_photo_album_removed(p1, a1)
        resp = c.get(f"/api/album-membership?photo_ids={p1}")
        data = resp.get_json()
        assert str(a1) not in data["membership"]

    def test_get_membership_empty_photo_ids_returns_empty(self, client_with_albums):
        c, *_ = client_with_albums
        resp = c.get("/api/album-membership?photo_ids=")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["membership"] == {}


class TestLibraryCurrentAlbum:
    def test_library_filtered_by_album_passes_album_name(self, client_with_albums):
        c, _, _, _, a1, _, _ = client_with_albums
        resp = c.get(f"/library?album_id={a1}")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "Summer 2024" in html
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/test_album_membership_api.py -v 2>&1 | head -30
```

Expected: failures with `404` responses (routes don't exist yet) and some attribute errors.

- [ ] **Step 3: Add the routes to `reviewer/app.py`**

Find the existing `@app.route("/library")` route. Just before it, add the Albums nav route. Then find the existing `/api/bulk-edit` route (or any other API route) and add the two membership API routes nearby.

**3a — Albums index page route** (add near other page routes):

```python
@app.route("/albums")
def albums_index() -> str:
    albums = db().get_all_albums_with_counts()
    return render_template("albums.html", albums=albums)
```

**3b — Membership write route** (add near `/api/bulk-edit`):

```python
@app.route("/api/album-membership", methods=["POST"])
def api_album_membership_write() -> _JsonResp:
    data = request.get_json(silent=True) or {}
    photo_ids: list[int] = data.get("photo_ids", [])
    add_album_ids: list[int] = data.get("add", [])
    remove_album_ids: list[int] = data.get("remove", [])

    if not photo_ids:
        return jsonify({"error": "photo_ids required"}), 400
    if not isinstance(photo_ids, list) or not all(isinstance(i, int) for i in photo_ids):
        return jsonify({"error": "photo_ids must be a list of integers"}), 400

    # Validate album IDs exist
    all_requested = set(add_album_ids) | set(remove_album_ids)
    if all_requested:
        valid_ids = {a["id"] for a in db().get_all_albums()}
        invalid = all_requested - valid_ids
        if invalid:
            return jsonify({"error": f"Unknown album_id(s): {sorted(invalid)}"}), 400

    try:
        added = 0
        removed = 0
        for album_id in add_album_ids:
            added += db().bulk_upsert_photo_albums(photo_ids, album_id)
        for album_id in remove_album_ids:
            removed += db().bulk_remove_photo_albums(photo_ids, album_id)
        db().conn.commit()
    except Exception:
        try:
            db().conn.rollback()
        except Exception:
            pass
        raise

    return jsonify({"added": added, "removed": removed})
```

**3c — Membership read route** (add immediately after the write route):

```python
@app.route("/api/album-membership", methods=["GET"])
def api_album_membership_read() -> _JsonResp:
    raw = request.args.get("photo_ids", "")
    if not raw:
        return jsonify({"membership": {}})
    try:
        photo_ids = [int(x) for x in raw.split(",") if x.strip()]
    except ValueError:
        return jsonify({"error": "photo_ids must be comma-separated integers"}), 400
    membership = db().get_album_membership_for_photos(photo_ids)
    # JSON keys must be strings; convert set → list for serialisation
    serialisable = {str(k): list(v) for k, v in membership.items()}
    return jsonify({"membership": serialisable})
```

**3d — Pass `current_album` in the `/library` route** (find the existing `render_template("library.html", ...)` call):

Find where `albums` is built (`albums = db().get_all_albums()`) and add:

```python
    current_album = None
    if album_id is not None:
        current_album = next((a for a in albums if a["id"] == album_id), None)
```

Then add `current_album=current_album` to the `render_template(...)` call.

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/test_album_membership_api.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Run full suite**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/ -q
```

Expected: all tests pass.

- [ ] **Step 6: Run make lint**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && make lint 2>&1 | tail -20
```

Fix any mypy errors before committing.

- [ ] **Step 7: Commit**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && git add reviewer/app.py tests/test_album_membership_api.py && git commit -m "feat(#135): album membership routes — GET/POST /api/album-membership, GET /albums

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 3: Albums page template + nav

**Files:**
- Create: `reviewer/templates/albums.html`
- Modify: `reviewer/templates/base.html`

- [ ] **Step 1: Create `reviewer/templates/albums.html`**

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
      <tr>
        <td>{{ album.name }}</td>
        <td class="count">{{ album.photo_count }}</td>
        <td><a href="{{ url_for('library', album_id=album.id) }}">View in library →</a></td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  {% else %}
  <p class="albums-empty">No albums yet. Albums are imported from Apple Photos automatically.</p>
  {% endif %}
</div>
{% endblock %}
```

- [ ] **Step 2: Add Albums to the nav in `reviewer/templates/base.html`**

Find this line in the desktop nav (around line 265):

```html
  <a href="{{ url_for('library') }}" {% if request.endpoint == 'library' %}class="active"{% endif %}><kbd class="nav-key">8</kbd>Library</a>
```

Add the Albums link immediately after it:

```html
  <a href="{{ url_for('albums_index') }}" {% if request.endpoint == 'albums_index' %}class="active"{% endif %}><kbd class="nav-key">9</kbd>Albums</a>
```

Find the mobile nav drawer (the `<div id="mobile-nav-drawer">` block). Find the Library entry in it:

```html
    <a href="{{ url_for('library') }}" {% if request.endpoint == 'library' %}class="active"{% endif %}>
      Library
    </a>
```

Add the Albums mobile entry immediately after it:

```html
    <a href="{{ url_for('albums_index') }}" {% if request.endpoint == 'albums_index' %}class="active"{% endif %}>
      Albums
    </a>
```

Find the keyboard shortcut JS object (around line 391):

```js
  const _nav = {
    '1': {{ url_for('dashboard') | tojson }},
    ...
    '8': {{ url_for('library') | tojson }},
  };
```

Add key `9` to the object:

```js
  const _nav = {
    '1': {{ url_for('dashboard') | tojson }},
    '2': {{ url_for('review', state='candidate_public') | tojson }},
    '3': {{ url_for('faces') | tojson }},
    '4': {{ url_for('zones') | tojson }},
    '5': {{ url_for('duplicates') | tojson }},
    '6': {{ url_for('conflicts') | tojson }},
    '7': {{ url_for('proposals') | tojson }},
    '8': {{ url_for('library') | tojson }},
    '9': {{ url_for('albums_index') | tojson }},
  };
```

- [ ] **Step 3: Run full suite to catch any template/route errors**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/ -q
```

Expected: all tests pass (the `test_album_membership_api.py::TestAlbumsIndexPage` tests should now pass too since the template exists).

- [ ] **Step 4: Manual smoke test**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python reviewer/app.py --config config/config.yml
```

Open browser, press `9` — should navigate to `/albums`. Confirm album list renders, counts show, "View in library →" links work.

- [ ] **Step 5: Commit**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && git add reviewer/templates/albums.html reviewer/templates/base.html && git commit -m "feat(#135): /albums index page + nav entry (key 9)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 4: Library UI — add/remove album membership

**Files:**
- Modify: `reviewer/templates/library.html`

### What to add

1. **Action bar**: "Add to album ▾" button (always); "Remove from [Album]" button (only when `current_album` set)
2. **Add panel**: New `lib-edit-panel` below the action bar with a checkbox list of albums, populated from template context; JS fetches current membership via `GET /api/album-membership` on open to grey out already-member albums
3. **Remove confirmation**: Inline prompt in the action bar when remove button clicked
4. **JS**: `openAlbumPanel()`, `closeAlbumPanel()`, `applyAlbumAdd()`, `confirmAlbumRemove()`

The `toast(msg, kind)` function is globally available from `base.html`.

- [ ] **Step 1: Add CSS for the album add panel and remove button**

Find the `.lib-edit-panel.visible { display: block; }` CSS block (around line 73). After the `.lib-edit-panel` block, add:

```css
/* ── Remove-album confirmation (inline in action bar) ────── */
#remove-album-confirm {
  display: flex; align-items: center; gap: 8px; font-size: 12px;
}
#remove-album-confirm button {
  padding: 2px 8px; border-radius: 3px; font-size: 12px; cursor: pointer;
}
#remove-album-confirm .confirm-yes {
  background: #aa2222; color: #fff; border: none;
}
#remove-album-confirm .confirm-no {
  background: none; border: 1px solid #444; color: var(--muted);
}

/* ── Album checkbox list in add panel ────────────────────── */
.album-checkbox-row {
  display: flex; align-items: center; gap: 6px;
  font-size: 13px; color: var(--text);
}
.album-checkbox-row.already-member { opacity: 0.4; }
.album-checkbox-row input[type=checkbox] { cursor: pointer; }
```

- [ ] **Step 2: Add action bar buttons**

Find the action bar HTML (around line 209):

```html
<div class="lib-action-bar" id="lib-action-bar">
  <span class="sel-count" id="sel-count-label">0 selected</span>
  <span class="sep">│</span>
  <button onclick="openPanel('title')">Edit title</button>
  <button onclick="openPanel('description')">Edit description</button>
  <button onclick="openPanel('tags_add')">Add tags</button>
  <button onclick="openPanel('tags_remove')">Remove tags</button>
  <button class="clear-btn" onclick="clearSelection()">✕ Clear</button>
</div>
```

Replace it with:

```html
<div class="lib-action-bar" id="lib-action-bar">
  <span class="sel-count" id="sel-count-label">0 selected</span>
  <span class="sep">│</span>
  <button onclick="openPanel('title')">Edit title</button>
  <button onclick="openPanel('description')">Edit description</button>
  <button onclick="openPanel('tags_add')">Add tags</button>
  <button onclick="openPanel('tags_remove')">Remove tags</button>
  <span class="sep">│</span>
  <button onclick="openAlbumPanel()">Add to album ▾</button>
  {% if current_album %}
  <span class="sep">│</span>
  <button id="remove-album-btn" style="color:#ff7a7a"
          onclick="showRemoveConfirm()">Remove from {{ current_album.name }}</button>
  <span id="remove-album-confirm" style="display:none">
    <span style="color:#ccc">Remove
      <span id="remove-album-count-label"></span> from
      <strong>{{ current_album.name }}</strong>?
    </span>
    <button class="confirm-yes" onclick="confirmAlbumRemove()">Confirm</button>
    <button class="confirm-no" onclick="hideRemoveConfirm()">Cancel</button>
  </span>
  {% endif %}
  <button class="clear-btn" onclick="clearSelection()">✕ Clear</button>
</div>
```

- [ ] **Step 3: Add the album add panel**

Find the existing `<div class="lib-edit-panel" id="lib-edit-panel">` block. Add a new panel as its sibling, immediately after the closing `</div>` of the existing panel:

```html
<!-- Album add panel -->
<div class="lib-edit-panel" id="album-add-panel">
  <h4 id="album-panel-title">Add to album</h4>
  <div id="album-checkbox-list" style="display:flex;flex-wrap:wrap;gap:10px 20px;margin-bottom:12px;min-height:24px">
    <span style="color:var(--muted);font-size:12px">Loading…</span>
  </div>
  <div style="display:flex;gap:8px;align-items:center">
    <button id="album-apply-btn" onclick="applyAlbumAdd()" disabled>Apply</button>
    <button class="btn-cancel" onclick="closeAlbumPanel()">Cancel</button>
  </div>
</div>
```

The album data is passed from Flask as `albums` (already in template context). To make it available to JS, add this just before the closing `</script>` tag (or right after the existing `_photoData` block):

```js
// ── Album data from server ───────────────────────────────────────────
const _albums = {{ albums | tojson }};
// [{id, name, flickr_set_id}, ...]
```

- [ ] **Step 4: Add the album JS functions**

Find the end of the `<script>` block (just before `</script>`). Add:

```js
// ── Album membership ─────────────────────────────────────────────────
let _membershipCache = {};  // {album_id: Set of photo_ids} — populated on panel open

async function openAlbumPanel() {
  closePanel();  // close metadata panel if open
  const panel = document.getElementById('album-add-panel');
  const list = document.getElementById('album-checkbox-list');
  const title = document.getElementById('album-panel-title');
  const applyBtn = document.getElementById('album-apply-btn');

  panel.classList.add('visible');
  title.textContent = `Add to album · ${_selectionCount()} photos`;
  applyBtn.disabled = true;
  list.innerHTML = '<span style="color:var(--muted);font-size:12px">Loading…</span>';

  // Fetch current membership for selected photos
  const ids = [..._selectedIds].join(',');
  _membershipCache = {};
  if (ids) {
    try {
      const r = await fetch(`/api/album-membership?photo_ids=${ids}`);
      const data = await r.json();
      // Convert to {album_id_int: Set<photo_id_int>}
      for (const [k, v] of Object.entries(data.membership || {})) {
        _membershipCache[parseInt(k)] = new Set(v);
      }
    } catch (e) { /* non-fatal — grey-out won't work but add still works */ }
  }

  // Render checkboxes
  if (_albums.length === 0) {
    list.innerHTML = '<span style="color:var(--muted);font-size:12px">No albums found.</span>';
    return;
  }
  list.innerHTML = '';
  for (const album of _albums) {
    const memberSet = _membershipCache[album.id] || new Set();
    const selIds = [..._selectedIds];
    const allMembers = selIds.length > 0 && selIds.every(id => memberSet.has(id));
    const row = document.createElement('label');
    row.className = 'album-checkbox-row' + (allMembers ? ' already-member' : '');
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.value = album.id;
    cb.disabled = allMembers;
    if (allMembers) cb.title = 'All selected photos are already in this album';
    cb.addEventListener('change', function() {
      applyBtn.disabled = !list.querySelector('input[type=checkbox]:checked:not(:disabled)');
    });
    row.appendChild(cb);
    row.appendChild(document.createTextNode(album.name));
    if (allMembers) {
      const note = document.createElement('span');
      note.style.cssText = 'font-size:11px;color:var(--muted)';
      note.textContent = '(already member)';
      row.appendChild(note);
    }
    list.appendChild(row);
  }
}

function closeAlbumPanel() {
  document.getElementById('album-add-panel').classList.remove('visible');
  _membershipCache = {};
}

async function applyAlbumAdd() {
  const checked = document.querySelectorAll('#album-checkbox-list input[type=checkbox]:checked:not(:disabled)');
  const albumIds = [...checked].map(cb => parseInt(cb.value));
  if (albumIds.length === 0) return;

  const btn = document.getElementById('album-apply-btn');
  btn.disabled = true;
  btn.textContent = 'Adding…';

  const payload = { photo_ids: [..._selectedIds], add: albumIds };
  try {
    const r = await fetch('/api/album-membership', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await r.json();
    if (r.ok) {
      const n = data.added;
      toast(`Added to ${albumIds.length} album${albumIds.length !== 1 ? 's' : ''} (${n} new membership${n !== 1 ? 's' : ''})`, 'ok');
      closeAlbumPanel();
      clearSelection();
    } else {
      toast('Error: ' + (data.error || 'unknown'), 'err');
      btn.disabled = false;
      btn.textContent = 'Apply';
    }
  } catch (e) {
    toast('Network error — try again', 'err');
    btn.disabled = false;
    btn.textContent = 'Apply';
  }
}

function showRemoveConfirm() {
  document.getElementById('remove-album-btn').style.display = 'none';
  const confirm = document.getElementById('remove-album-confirm');
  const countLabel = document.getElementById('remove-album-count-label');
  const n = _selectionCount();
  countLabel.textContent = `${n} photo${n !== 1 ? 's' : ''}`;
  confirm.style.display = 'flex';
}

function hideRemoveConfirm() {
  document.getElementById('remove-album-confirm').style.display = 'none';
  document.getElementById('remove-album-btn').style.display = '';
}

{% if current_album %}
async function confirmAlbumRemove() {
  const albumId = {{ current_album.id }};
  const payload = { photo_ids: [..._selectedIds], remove: [albumId] };
  hideRemoveConfirm();

  try {
    const r = await fetch('/api/album-membership', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await r.json();
    if (r.ok) {
      toast(`Removed ${data.removed} photo${data.removed !== 1 ? 's' : ''} from {{ current_album.name | tojson }}`, 'ok');
      clearSelection();
      // Reload grid — removed photos no longer appear in this album filter
      setTimeout(() => location.reload(), 800);
    } else {
      toast('Error: ' + (data.error || 'unknown'), 'err');
    }
  } catch (e) {
    toast('Network error — try again', 'err');
  }
}
{% endif %}
```

- [ ] **Step 5: Make `closePanel()` also close the album panel**

Find the existing `closePanel()` function:

```js
function closePanel() {
  document.getElementById('lib-edit-panel').classList.remove('visible');
  document.getElementById('panel-preview').innerHTML = '';
  document.getElementById('panel-confirm-btn').disabled = true;
  _currentField = null;
  _panelTags = [];
  const wrap = document.getElementById('tag-chip-wrap');
  if (wrap) wrap.querySelectorAll('.tag-chip').forEach(c => c.remove());
}
```

Add one line to close the album panel too:

```js
function closePanel() {
  document.getElementById('lib-edit-panel').classList.remove('visible');
  document.getElementById('album-add-panel').classList.remove('visible');
  document.getElementById('panel-preview').innerHTML = '';
  document.getElementById('panel-confirm-btn').disabled = true;
  _currentField = null;
  _panelTags = [];
  const wrap = document.getElementById('tag-chip-wrap');
  if (wrap) wrap.querySelectorAll('.tag-chip').forEach(c => c.remove());
}
```

- [ ] **Step 6: Also hide the remove confirmation when selection is cleared**

Find the `clearSelection()` function. Add `hideRemoveConfirm()` call at the top (guard against the function not existing when `current_album` is not set):

```js
function clearSelection() {
  if (typeof hideRemoveConfirm === 'function') hideRemoveConfirm();
  _selectAllFilter = false;
  // ... rest unchanged ...
}
```

- [ ] **Step 7: Run full test suite**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/ -q
```

Expected: all tests pass (library.html changes are JS/template only — tests still pass).

- [ ] **Step 8: Manual smoke test**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python reviewer/app.py --config config/config.yml
```

Confirm:
- Library view: select photos → "Add to album ▾" appears in action bar
- Click "Add to album ▾" → panel opens with album list, already-member albums greyed
- Check albums → Apply → toast success, panel closes, selection cleared
- Filter to an album → select photos → "Remove from [Album]" appears in red
- Click remove → inline confirm appears with photo count
- Click Confirm → toast success, page reloads, photos no longer shown (since they're no longer in the filtered album)

- [ ] **Step 9: Commit**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && git add reviewer/templates/library.html && git commit -m "feat(#135): library view — add to album panel + remove from album action

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 5: README, spec, issue close

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-05-25-album-membership-editing-135.md`

- [ ] **Step 1: Update README**

Find the line describing the library view (around line 25):

```
- Library view with multi-select for bulk title, description, and tag editing across photo sets — changes queue as proposals before writing to Flickr; double-click any photo to open its detail page (larger image, editable title/description/tags) with a back link returning to the library
```

Append to that line (or add a new bullet immediately after):

```
- Album membership editing from the library view: add selected photos to existing albums or remove them from the currently-filtered album; changes queue for `bp sync-albums` — no immediate Flickr calls
- `/albums` page (key `9`) lists all albums with photo counts and links to the filtered library view
```

Find the test count line (around line 573) and update it to reflect the new tests. Run `python -m pytest tests/ -q` first to get the current count, then update accordingly.

- [ ] **Step 2: Mark the spec done**

In `docs/superpowers/specs/2026-05-25-album-membership-editing-135.md`, change:

```
**Status:** Approved, awaiting implementation plan
```

to:

```
**Status:** ✓ done
```

- [ ] **Step 3: Apply the `has-plan` label to GH #135**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && gh issue edit 135 --add-label "has-plan"
```

- [ ] **Step 4: Commit**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && git add README.md docs/superpowers/specs/2026-05-25-album-membership-editing-135.md && git commit -m "docs(#135): README + mark spec done

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

- [ ] **Step 5: Push**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && git push origin main
```

- [ ] **Step 6: Close GH issue with retrospective**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && gh issue close 135 --comment "Implemented across 4 commits:

- DB: \`get_all_albums_with_counts\`, \`get_album_membership_for_photos\`, \`bulk_upsert_photo_albums\`, \`bulk_remove_photo_albums\`
- Routes: \`GET/POST /api/album-membership\`, \`GET /albums\`
- Templates: \`albums.html\` (new), \`base.html\` (nav key 9), \`library.html\` (add panel + remove confirm)

**Retrospective:** size estimate M ✓ — 4 files created/modified substantively (db.py, app.py, albums.html, library.html), ~350 LOC net. Design review feedback incorporated before implementation: transactions in batch DB methods, explicit re-activation semantics, invariant test. No scope changes from spec."
```
