# Video Review UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make video tiles visually distinct in the review grid — a centred ▶ play-button overlay on the thumbnail and a `video` label in the meta row, so the operator knows they are deciding about a moving image rather than a still.

**Architecture:** Three layers — (1) DB migration: add `is_video INTEGER NOT NULL DEFAULT 0` column, backfill from filename extension; (2) Ingestion: set `is_video` in scanner (from `photo.ismovie`) and Flickr poller (from `photo.get("media") == "video"`); (3) Review UI: add `is_video` to `review_queue()` SELECT, add ▶ overlay and `video-label` badge to the template.

**Tech Stack:** Python/SQLite, Jinja2, CSS. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-05-21-video-review-ui-design.md`

**GitHub issue:** #127

---

## File Map

| File | Change |
|------|--------|
| `db/migrations/migrate_021_is_video.py` | New migration: add `is_video` column, backfill from filename extension |
| `poller/scanner.py` | Set `is_video = 1 if photo.ismovie else 0` in photo row builder |
| `poller/poller.py` | Set `is_video = 1 if photo.get("media") == "video" else 0` in Flickr row builder |
| `db/db.py` | Add `is_video` to `review_queue()` SELECT |
| `reviewer/templates/review.html` | CSS (▶ badge, video-label); Jinja (conditional badge + label) |
| `tests/test_video_review_ui.py` | New test file: migration backfill, scanner field, poller field, review_queue field, template rendering |

---

## Task 1: Migration — add `is_video` column and backfill

**Files:**
- Create: `db/migrations/migrate_021_is_video.py`
- Create: `tests/test_video_review_ui.py`

### Background

`is_video` does not yet exist in the `photos` schema. This migration adds it with `DEFAULT 0` and backfills from filename extension for existing rows. HEIC files are NOT treated as videos. The column is idempotent (checked via `schema_migrations` table).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_video_review_ui.py`:

```python
"""
tests/test_video_review_ui.py — tests for video detection in the review UI

Run from repo root:
    python -m pytest tests/test_video_review_ui.py -v
"""

import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.db import Database
from db.migrations.migrate_021_is_video import run as migrate_is_video


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "test.db"
    db = Database(path)
    db.close()
    return path


class TestMigration021:
    def test_migration_adds_is_video_column(self, db_path):
        migrate_is_video(str(db_path))
        conn = sqlite3.connect(db_path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(photos)").fetchall()}
        conn.close()
        assert "is_video" in cols

    def test_migration_backfills_mov(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute(
            "INSERT INTO photos (uuid, original_filename, privacy_state, updated_at, date_synced)"
            " VALUES ('u-mov', 'VID_001.MOV', 'candidate_public', '2024-01-01', '2024-01-01')"
        )
        conn.commit()
        conn.close()
        migrate_is_video(str(db_path))
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT is_video FROM photos WHERE uuid = 'u-mov'").fetchone()
        conn.close()
        assert row["is_video"] == 1

    def test_migration_backfills_mp4(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO photos (uuid, original_filename, privacy_state, updated_at, date_synced)"
            " VALUES ('u-mp4', 'clip.mp4', 'candidate_public', '2024-01-01', '2024-01-01')"
        )
        conn.commit()
        conn.close()
        migrate_is_video(str(db_path))
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT is_video FROM photos WHERE uuid = 'u-mp4'").fetchone()
        conn.close()
        assert row["is_video"] == 1

    def test_migration_backfills_m4v(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO photos (uuid, original_filename, privacy_state, updated_at, date_synced)"
            " VALUES ('u-m4v', 'clip.M4V', 'candidate_public', '2024-01-01', '2024-01-01')"
        )
        conn.commit()
        conn.close()
        migrate_is_video(str(db_path))
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT is_video FROM photos WHERE uuid = 'u-m4v'").fetchone()
        conn.close()
        assert row["is_video"] == 1

    def test_migration_leaves_jpg_as_zero(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO photos (uuid, original_filename, privacy_state, updated_at, date_synced)"
            " VALUES ('u-jpg', 'IMG_001.JPG', 'candidate_public', '2024-01-01', '2024-01-01')"
        )
        conn.commit()
        conn.close()
        migrate_is_video(str(db_path))
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT is_video FROM photos WHERE uuid = 'u-jpg'").fetchone()
        conn.close()
        assert row["is_video"] == 0

    def test_migration_leaves_heic_as_zero(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO photos (uuid, original_filename, privacy_state, updated_at, date_synced)"
            " VALUES ('u-heic', 'IMG_002.HEIC', 'candidate_public', '2024-01-01', '2024-01-01')"
        )
        conn.commit()
        conn.close()
        migrate_is_video(str(db_path))
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT is_video FROM photos WHERE uuid = 'u-heic'").fetchone()
        conn.close()
        assert row["is_video"] == 0

    def test_migration_is_idempotent(self, db_path):
        migrate_is_video(str(db_path))
        # Running twice must not raise
        migrate_is_video(str(db_path))
        conn = sqlite3.connect(db_path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(photos)").fetchall()}
        conn.close()
        assert "is_video" in cols
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_video_review_ui.py::TestMigration021 -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'db.migrations.migrate_021_is_video'`.

- [ ] **Step 3: Create `db/migrations/migrate_021_is_video.py`**

```python
"""
migrate_021_is_video.py

Adds:
  photos.is_video INTEGER NOT NULL DEFAULT 0

Backfills is_video=1 for existing rows where the filename extension is
.mov, .mp4, or .m4v (case-insensitive). HEIC files are NOT videos in BP's
model — they are handled as stills. Live Photos (.heic with embedded clip)
are likewise treated as stills.

Safe to run multiple times (idempotent — checks schema_migrations).

Usage:
    python db/migrations/migrate_021_is_video.py --config config/config.yml
"""

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import yaml

MIGRATION_NAME = "migrate_021_is_video"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run(db_path: str, dry_run: bool = False) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    try:
        row = conn.execute(
            "SELECT id FROM schema_migrations WHERE name = ?", (MIGRATION_NAME,)
        ).fetchone()
        if row is not None:
            print("  Skipped:  migration already applied")
            conn.close()
            return
    except Exception:
        pass

    existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(photos)").fetchall()}

    if dry_run:
        if "is_video" not in existing_cols:
            print("  [dry-run] Would add photos.is_video column")
            print("  [dry-run] Would backfill is_video=1 for .mov/.mp4/.m4v files")
        else:
            print("  [dry-run] photos.is_video already exists")
        conn.close()
        return

    conn.execute("BEGIN")

    if "is_video" not in existing_cols:
        conn.execute(
            "ALTER TABLE photos ADD COLUMN is_video INTEGER NOT NULL DEFAULT 0"
        )

    conn.execute(
        """UPDATE photos
           SET is_video = 1
           WHERE lower(original_filename) LIKE '%.mov'
              OR lower(original_filename) LIKE '%.mp4'
              OR lower(original_filename) LIKE '%.m4v'"""
    )

    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (name, applied_at) VALUES (?, ?)",
        (MIGRATION_NAME, now_iso()),
    )
    conn.commit()
    conn.close()
    print("  Applied:  migrate_021_is_video")


def main() -> int:
    parser = argparse.ArgumentParser(description="Migration 021: add is_video flag")
    parser.add_argument("--config", default="config/config.yml")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with open(args.config) as f:
        config = yaml.safe_load(f)

    db_path = str(Path(config["database"]["path"]).expanduser())
    run(db_path, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_video_review_ui.py::TestMigration021 -v
```

Expected: all 7 tests PASS.

- [ ] **Step 5: Run full suite**

```bash
python -m pytest tests/ -q
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add db/migrations/migrate_021_is_video.py tests/test_video_review_ui.py
git commit -m "feat(db): add is_video migration and backfill for .mov/.mp4/.m4v files

Migration 021 adds is_video INTEGER NOT NULL DEFAULT 0 to photos
and backfills 1 for existing .MOV/.MP4/.M4V records. HEIC excluded.
Idempotent via schema_migrations check.

Part of #127
Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 2: Set `is_video` in scanner and Flickr poller

**Files:**
- Modify: `poller/scanner.py`
- Modify: `poller/poller.py`
- Modify: `tests/test_video_review_ui.py`

### Background

Two ingestion paths need to set `is_video`:

1. **Scanner** (`poller/scanner.py`): When building a photo row from `osxphotos`, `photo.ismovie` is the authoritative field. Live Photos (`photo.live_photo = True, photo.ismovie = False`) are correctly excluded — `ismovie` is `False` for them.

2. **Flickr poller** (`poller/poller.py`): The Flickr list API returns a `media` field on each photo (`"photo"` or `"video"`). The row-builder function `_build_flickr_row(photo, info)` (lines 100–180) builds the initial dict from the paginated response.

Look for where `row["_is_screenshot"]` is set in scanner.py (around line 166) and add `is_video` immediately after. In poller.py, look for where `row["original_format"]` is set (around line 174) and add `is_video` after it.

- [ ] **Step 1: Write the failing tests**

Add these classes to `tests/test_video_review_ui.py` (after `TestMigration021`):

```python
class TestScannerIsVideo:
    def test_scanner_sets_is_video_for_movie(self):
        """photo.ismovie = True → is_video = 1"""
        from unittest.mock import MagicMock
        sys.path.insert(0, str(Path(__file__).parent.parent / "poller"))
        from scanner import _build_photo_row  # noqa: PLC0415

        photo = MagicMock()
        photo.ismovie = True
        photo.live_photo = False
        photo.uuid = "test-uuid"
        photo.original_filename = "VID_001.MOV"
        photo.filename = "VID_001.MOV"
        photo.date = None
        photo.score = None
        photo.screenshot = False
        photo.selfie = False
        photo.fingerprint = ""
        photo.width = 1920
        photo.height = 1080
        photo.favorite = False

        row = _build_photo_row(photo)
        assert row.get("is_video") == 1

    def test_scanner_sets_is_video_zero_for_still(self):
        """photo.ismovie = False → is_video = 0"""
        from unittest.mock import MagicMock
        sys.path.insert(0, str(Path(__file__).parent.parent / "poller"))
        from scanner import _build_photo_row  # noqa: PLC0415

        photo = MagicMock()
        photo.ismovie = False
        photo.live_photo = False
        photo.uuid = "test-uuid-2"
        photo.original_filename = "IMG_001.JPG"
        photo.filename = "IMG_001.JPG"
        photo.date = None
        photo.score = None
        photo.screenshot = False
        photo.selfie = False
        photo.fingerprint = ""
        photo.width = 4032
        photo.height = 3024
        photo.favorite = False

        row = _build_photo_row(photo)
        assert row.get("is_video") == 0

    def test_scanner_live_photo_is_not_video(self):
        """Live Photo: ismovie=False, live_photo=True → is_video = 0"""
        from unittest.mock import MagicMock
        sys.path.insert(0, str(Path(__file__).parent.parent / "poller"))
        from scanner import _build_photo_row  # noqa: PLC0415

        photo = MagicMock()
        photo.ismovie = False
        photo.live_photo = True
        photo.uuid = "test-uuid-3"
        photo.original_filename = "IMG_001.HEIC"
        photo.filename = "IMG_001.HEIC"
        photo.date = None
        photo.score = None
        photo.screenshot = False
        photo.selfie = False
        photo.fingerprint = ""
        photo.width = 4032
        photo.height = 3024
        photo.favorite = False

        row = _build_photo_row(photo)
        assert row.get("is_video") == 0


class TestPollerIsVideo:
    def test_poller_sets_is_video_for_flickr_video(self):
        """photo dict with media='video' → is_video = 1"""
        sys.path.insert(0, str(Path(__file__).parent.parent / "poller"))
        from poller import _build_flickr_row  # noqa: PLC0415

        photo = {
            "id": "12345",
            "secret": "abc",
            "server": "s1",
            "farm": 1,
            "title": "A video",
            "media": "video",
            "tags": "",
        }
        row = _build_flickr_row(photo, info=None)
        assert row.get("is_video") == 1

    def test_poller_sets_is_video_zero_for_photo(self):
        """photo dict with media='photo' → is_video = 0"""
        sys.path.insert(0, str(Path(__file__).parent.parent / "poller"))
        from poller import _build_flickr_row  # noqa: PLC0415

        photo = {
            "id": "67890",
            "secret": "def",
            "server": "s2",
            "farm": 2,
            "title": "A photo",
            "media": "photo",
            "tags": "",
        }
        row = _build_flickr_row(photo, info=None)
        assert row.get("is_video") == 0

    def test_poller_sets_is_video_zero_when_media_absent(self):
        """photo dict missing media key → is_video = 0"""
        sys.path.insert(0, str(Path(__file__).parent.parent / "poller"))
        from poller import _build_flickr_row  # noqa: PLC0415

        photo = {
            "id": "11111",
            "secret": "ghi",
            "server": "s3",
            "farm": 3,
            "title": "No media key",
            "tags": "",
        }
        row = _build_flickr_row(photo, info=None)
        assert row.get("is_video") == 0
```

**Note on `_build_photo_row` signature:** Check the actual function signature in `poller/scanner.py` — it may take additional arguments beyond just `photo`. Read the function definition and adjust the test to match. Common signature: `_build_photo_row(photo, albums=None)` or similar. The mock only needs attributes that the function actually reads.

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_video_review_ui.py::TestScannerIsVideo tests/test_video_review_ui.py::TestPollerIsVideo -v
```

Expected: FAIL — `is_video` key not found in returned row.

- [ ] **Step 3: Add `is_video` to `poller/scanner.py`**

Find the block setting `_is_screenshot` in `_build_photo_row` (around line 166):

```python
    row["_is_screenshot"] = bool(getattr(photo, "screenshot", False))
    row["_is_selfie"] = bool(getattr(photo, "selfie", False))
    row["_is_live"] = bool(getattr(photo, "live_photo", False))
```

Add `is_video` immediately after these lines:

```python
    row["is_video"] = 1 if getattr(photo, "ismovie", False) else 0
```

- [ ] **Step 4: Add `is_video` to `poller/poller.py`**

Find where `row["original_format"]` is set in `_build_flickr_row` (around line 174):

```python
    row["original_format"] = photo.get("originalformat", "")
```

Add `is_video` immediately after it:

```python
    row["is_video"] = 1 if photo.get("media") == "video" else 0
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
python -m pytest tests/test_video_review_ui.py::TestScannerIsVideo tests/test_video_review_ui.py::TestPollerIsVideo -v
```

Expected: all 6 tests PASS.

- [ ] **Step 6: Run full suite**

```bash
python -m pytest tests/ -q
```

Expected: all pass.

- [ ] **Step 7: Run lint**

```bash
make lint
```

Fix any issues.

- [ ] **Step 8: Commit**

```bash
git add poller/scanner.py poller/poller.py tests/test_video_review_ui.py
git commit -m "feat(ingestion): set is_video from osxphotos ismovie and Flickr media field

Scanner: photo.ismovie=True → is_video=1; Live Photos (ismovie=False) → 0.
Poller: media='video' → is_video=1; media='photo' or absent → 0.

Part of #127
Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 3: Review UI — `review_queue()` + template badges

**Files:**
- Modify: `db/db.py` (the `review_queue` method)
- Modify: `reviewer/templates/review.html`
- Modify: `tests/test_video_review_ui.py`

### Background

This task exposes `is_video` to the review grid:
1. Add `is_video` to the `review_queue()` SELECT
2. Add a centred ▶ overlay (`video-badge`) on the thumbnail for video tiles
3. Add a `video` text label (`video-label`) in the meta row for video tiles

`is_video` requires the migration (Task 1) to have run; the `Database` class auto-runs migrations at init, so test fixtures will have the column available.

- [ ] **Step 1: Write the failing tests**

Add this class to `tests/test_video_review_ui.py`:

```python
import reviewer.app as app_module


@pytest.fixture()
def db_fixture(tmp_path):
    """DB with migration applied and seeded photos."""
    db = Database(tmp_path / "test.db")
    migrate_is_video(str(tmp_path / "test.db"))

    # Video photo
    db.upsert_photo({
        "uuid": "uuid-vid-1",
        "original_filename": "VID_001.MOV",
        "privacy_state": "candidate_public",
        "is_video": 1,
        "apple_persons": [],
        "proposed_tags": [],
    })
    # Still photo
    db.upsert_photo({
        "uuid": "uuid-still-1",
        "original_filename": "IMG_001.JPG",
        "privacy_state": "candidate_public",
        "is_video": 0,
        "apple_persons": [],
        "proposed_tags": [],
    })
    return db


class TestReviewQueueIsVideo:
    def test_review_queue_returns_is_video_field(self, db_fixture):
        photos = db_fixture.review_queue(states=["candidate_public"])
        assert len(photos) == 2
        by_uuid = {p["uuid"]: p for p in photos}
        assert by_uuid["uuid-vid-1"]["is_video"] == 1
        assert by_uuid["uuid-still-1"]["is_video"] == 0
        db_fixture.close()


@pytest.fixture()
def flask_client_video(tmp_path):
    """Flask test client with seeded video and still photos."""
    db = Database(tmp_path / "test.db")
    migrate_is_video(str(tmp_path / "test.db"))

    db.upsert_photo({
        "uuid": "uuid-vid-flask",
        "original_filename": "VID_FLASK.MOV",
        "privacy_state": "candidate_public",
        "is_video": 1,
        "apple_persons": [],
        "proposed_tags": [],
    })
    db.upsert_photo({
        "uuid": "uuid-still-flask",
        "original_filename": "IMG_FLASK.JPG",
        "privacy_state": "candidate_public",
        "is_video": 0,
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


class TestVideoTemplate:
    def test_video_badge_rendered_for_video_tile(self, flask_client_video):
        r = flask_client_video.get("/review?state=candidate_public")
        html = r.data.decode()
        assert "video-badge" in html
        assert "▶" in html

    def test_video_label_rendered_for_video_tile(self, flask_client_video):
        r = flask_client_video.get("/review?state=candidate_public")
        html = r.data.decode()
        assert "video-label" in html
        assert ">video<" in html

    def test_video_badge_absent_for_still_tile(self, flask_client_video):
        r = flask_client_video.get("/review?state=candidate_public")
        html = r.data.decode()
        # Only one video photo, so one video-badge; count confirms it's not on every tile
        assert html.count("video-badge") == 1

    def test_video_label_absent_for_still_tile(self, flask_client_video):
        r = flask_client_video.get("/review?state=candidate_public")
        html = r.data.decode()
        assert html.count("video-label") == 1
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_video_review_ui.py::TestReviewQueueIsVideo tests/test_video_review_ui.py::TestVideoTemplate -v
```

Expected: FAIL — `KeyError: 'is_video'` from review_queue.

- [ ] **Step 3: Add `is_video` to `review_queue()` in `db/db.py`**

Find the SELECT in `review_queue()` (search for `geofence_zone, apple_persons, privacy_reason, width, height`):

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

Replace with (add `is_video` at end of SELECT):

```python
rows = self.conn.execute(
    f"""SELECT id, uuid, flickr_id, original_filename,
               apple_unknown_faces, apple_named_faces, proposed_tags,
               display_rotation, is_screenshot, updated_at,
               geofence_zone, apple_persons, privacy_reason,
               width, height, is_video
        FROM photos
        WHERE privacy_state IN ({placeholders}){screenshot_filter}
        ORDER BY date_taken DESC, id DESC
        LIMIT ? OFFSET ?""",
    states + [limit, offset],
).fetchall()
```

The result loop does not need changes — `is_video` is a plain integer.

- [ ] **Step 4: Add CSS to `reviewer/templates/review.html`**

Inside the `{% block extra_style %}` block, after the existing `.person-chip.protected` rule (last of the pano/chip CSS), add:

```css
/* Video overlay — centred play button on thumbnail */
.photo-card .thumb .video-badge {
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  font-size: 28px;
  color: rgba(255, 255, 255, 0.85);
  text-shadow: 0 1px 4px rgba(0, 0, 0, 0.7);
  pointer-events: none;
  line-height: 1;
}

/* Meta row label — video type indicator */
.video-label {
  display: inline-block;
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  color: #aaa;
  background: #2a2a2a;
  border-radius: 3px;
  padding: 1px 5px;
  margin-top: 3px;
}
```

- [ ] **Step 5: Add ▶ overlay badge to the thumbnail**

In the `.thumb` div, after the existing `{% if photo.is_screenshot %}` badge block and before the closing `</div>`:

```html
      {% if photo.is_screenshot %}
      <span class="screenshot-badge">screenshot</span>
      {% endif %}
      {% if photo.is_video %}
      <span class="video-badge">▶</span>
      {% endif %}
      {% if photo.is_protected %}
```

- [ ] **Step 6: Add `video-label` badge to the meta row**

In the `.meta` div, after the `<div class="filename">` line:

```html
      <div class="filename">{{ photo.original_filename or photo.flickr_id or '?' }}</div>
      {% if photo.is_video %}
      <span class="video-label">video</span>
      {% endif %}
      <div class="tag-row" id="tags-{{ photo.id }}">
```

- [ ] **Step 7: Run tests to verify they pass**

```bash
python -m pytest tests/test_video_review_ui.py -v
```

Expected: all 17 tests PASS (7 migration + 3 scanner + 3 poller + 1 review_queue + 4 template).

- [ ] **Step 8: Run full suite**

```bash
python -m pytest tests/ -q
```

Expected: all pass.

- [ ] **Step 9: Run lint**

```bash
make lint
```

Fix any issues.

- [ ] **Step 10: Update README.md**

Find the panoramic paragraph added in v1.0.6. Add a sentence about video handling:

> Videos (`.MOV`, `.MP4`, `.M4V`) are flagged with a centred ▶ play-button overlay on the thumbnail and a `video` label in the meta row, so the operator knows they are reviewing a moving image before deciding.

- [ ] **Step 11: Commit**

```bash
git add db/db.py reviewer/templates/review.html tests/test_video_review_ui.py README.md
git commit -m "feat(ui): video badge overlay and label in review grid

Adds is_video to review_queue() SELECT. Video tiles show a centred
▶ overlay on the thumbnail and a 'video' text label in the meta row.

Closes #127
Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage check:**

| Spec requirement | Task |
|---|---|
| `migrate_021_is_video.py` migration file | Task 1, Step 3 |
| `is_video INTEGER NOT NULL DEFAULT 0` column added | Task 1, Step 3 |
| Backfill `.mov`, `.mp4`, `.m4v` (case-insensitive) | Task 1, Step 3 |
| HEIC NOT backfilled | Task 1, Step 3 (only those 3 extensions matched) |
| Idempotent migration | Task 1, Step 3 (schema_migrations check) |
| Scanner: `photo.ismovie` → `is_video` | Task 2, Step 3 |
| Live Photo (`ismovie=False`) → `is_video=0` | Task 2, Step 3 (correct: uses `photo.ismovie` directly) |
| Poller: `media == "video"` → `is_video=1` | Task 2, Step 4 |
| `is_video` in `review_queue()` SELECT | Task 3, Step 3 |
| `video-badge` ▶ overlay on thumbnail (centred, absolute) | Task 3, Steps 4+5 |
| `video-label` text badge in meta row | Task 3, Steps 4+6 |
| No thumbnailer changes | ✓ not in this plan |
| No HEIC/Live Photo special casing | ✓ excluded by using `ismovie` directly |
| README update | Task 3, Step 10 |

All spec requirements covered.
