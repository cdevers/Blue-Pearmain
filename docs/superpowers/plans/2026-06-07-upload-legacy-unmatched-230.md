# Upload Legacy-Unmatched Assets to Flickr — Implementation Plan (#230)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `bp upload-legacy-unmatched` — a one-shot command that uploads iPhoto library assets with no Flickr counterpart directly to Flickr, bypassing Apple Photos and iCloud.

**Architecture:** Three layers following the `legacy_apply.py` pattern: (1) `FlickrClient.upload_photo()` handles the multipart POST and XML response specific to Flickr's upload endpoint; (2) `poller/legacy_uploader.py` orchestrates Phase 1 (recovery of partial failures) and Phase 2 (upload loop); (3) `bp upload-legacy-unmatched` wires everything together from config. A new migration 031 adds `uploaded_flickr_id` and `uploaded_at` to `legacy_assets` as the idempotency key.

**Tech Stack:** Python 3, SQLite, `requests_oauthlib.OAuth1Session`, `xml.etree.ElementTree`, pytest.

---

## File map

| File | Action | Responsibility |
|------|--------|----------------|
| `db/migrations/migrate_031_legacy_upload.py` | Create | Add `uploaded_flickr_id`, `uploaded_at` to `legacy_assets` |
| `db/db.py` | Modify | Add `mark_legacy_uploaded`, `iter_unrecovered_legacy_uploads`, `record_legacy_upload` |
| `flickr/flickr_client.py` | Modify | Add `UPLOAD_URL` constant + `upload_photo()` method |
| `poller/legacy_uploader.py` | Create | Phase 1 recovery + Phase 2 upload loop |
| `bp` | Modify | `cmd_upload_legacy_unmatched`, subparser, dispatch entry |
| `README.md` | Modify | Document new command |
| `tests/test_migrate_031.py` | Create | Migration idempotency tests |
| `tests/test_db_legacy_upload.py` | Create | DB method tests |
| `tests/test_flickr_upload.py` | Create | `FlickrClient.upload_photo()` tests |
| `tests/test_legacy_uploader.py` | Create | Orchestration tests |

---

## Task 1: Migration 031 — add upload columns to legacy_assets

**Files:**
- Create: `db/migrations/migrate_031_legacy_upload.py`
- Create: `tests/test_migrate_031.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_migrate_031.py
from __future__ import annotations
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def _make_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    # base legacy tables (026)
    from db.migrations.migrate_026_legacy_index import run_on_conn
    run_on_conn(conn)
    return conn


def test_adds_uploaded_columns(tmp_path):
    conn = _make_conn()
    from db.migrations.migrate_031_legacy_upload import run_on_conn as run_031
    run_031(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(legacy_assets)").fetchall()}
    assert "uploaded_flickr_id" in cols
    assert "uploaded_at" in cols


def test_idempotent(tmp_path):
    conn = _make_conn()
    from db.migrations.migrate_031_legacy_upload import run_on_conn as run_031
    run_031(conn)
    run_031(conn)  # must not raise
    cols = {r[1] for r in conn.execute("PRAGMA table_info(legacy_assets)").fetchall()}
    assert "uploaded_flickr_id" in cols


def test_defaults_are_null():
    conn = _make_conn()
    from db.migrations.migrate_031_legacy_upload import run_on_conn as run_031
    run_031(conn)
    conn.execute(
        "INSERT INTO legacy_libraries (library_uuid, asset_count) VALUES ('L', 0)"
    )
    conn.execute(
        "INSERT INTO legacy_assets (library_uuid, asset_uuid, named_face_count, unknown_face_count) "
        "VALUES ('L', 'A', 0, 0)"
    )
    row = conn.execute(
        "SELECT uploaded_flickr_id, uploaded_at FROM legacy_assets WHERE asset_uuid = 'A'"
    ).fetchone()
    assert row["uploaded_flickr_id"] is None
    assert row["uploaded_at"] is None
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_migrate_031.py -q
```
Expected: `ModuleNotFoundError: No module named 'db.migrations.migrate_031_legacy_upload'`

- [ ] **Step 3: Write the migration**

```python
# db/migrations/migrate_031_legacy_upload.py
"""
migrate_031_legacy_upload.py

Add uploaded_flickr_id and uploaded_at columns to legacy_assets.
These support idempotent re-runs of bp upload-legacy-unmatched (#230):
uploaded_flickr_id is set immediately after a successful Flickr upload,
before the photos row is created, so a re-run skips assets already uploaded.

Idempotent: skips if already applied.
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import yaml

MIGRATION_NAME = "migrate_031_legacy_upload"


def run_on_conn(conn: sqlite3.Connection) -> None:
    if conn.row_factory is None:
        conn.row_factory = sqlite3.Row

    try:
        row = conn.execute(
            "SELECT id FROM schema_migrations WHERE name = ?", (MIGRATION_NAME,)
        ).fetchone()
        if row is not None:
            return
    except Exception:
        pass

    conn.execute("BEGIN")

    tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    if "legacy_assets" in tables:
        existing = {r[1] for r in conn.execute("PRAGMA table_info(legacy_assets)").fetchall()}
        if "uploaded_flickr_id" not in existing:
            conn.execute("ALTER TABLE legacy_assets ADD COLUMN uploaded_flickr_id TEXT")
        if "uploaded_at" not in existing:
            conn.execute("ALTER TABLE legacy_assets ADD COLUMN uploaded_at TEXT")

    conn.execute(
        "INSERT INTO schema_migrations (name, applied_at) "
        "VALUES (?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))",
        (MIGRATION_NAME,),
    )
    conn.execute("COMMIT")


def run(db_path: str, dry_run: bool = False) -> None:
    if dry_run:
        print("  [dry-run] Would add uploaded_flickr_id and uploaded_at to legacy_assets")
        return
    conn = sqlite3.connect(db_path)
    run_on_conn(conn)
    conn.close()
    print("  Applied:  migrate_031_legacy_upload")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migration 031: add uploaded_flickr_id + uploaded_at to legacy_assets"
    )
    parser.add_argument("--config", default="config/config.yml")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    with open(args.config) as f:
        config = yaml.safe_load(f)
    db_path = str(Path(config["database"]["path"]).expanduser())
    run(db_path, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m pytest tests/test_migrate_031.py -q
```
Expected: `3 passed`

- [ ] **Step 5: Commit**

```bash
git add db/migrations/migrate_031_legacy_upload.py tests/test_migrate_031.py
git commit -m "feat(#230): migration 031 — add uploaded_flickr_id/uploaded_at to legacy_assets"
```

---

## Task 2: DB methods for legacy upload tracking

**Files:**
- Modify: `db/db.py` (append three methods after `delete_legacy_assets_not_in`, the last method in the file)
- Create: `tests/test_db_legacy_upload.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_db_legacy_upload.py
from __future__ import annotations
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.db import Database  # noqa: E402


def _make_db(tmp_path) -> Database:
    from db.migrations.migrate_020_operation_log import run as run_op_log
    from db.migrations.migrate_026_legacy_index import run_on_conn as run_legacy
    from db.migrations.migrate_031_legacy_upload import run_on_conn as run_upload

    db = Database(str(tmp_path / "curator.db"))
    run_op_log(str(tmp_path / "curator.db"))
    run_legacy(db.conn)
    run_upload(db.conn)
    return db


def _seed(db, library_uuid="L", asset_uuid="A", date_taken="2005-06-01 12:00:00"):
    db.set_legacy_library({"library_uuid": library_uuid, "display_name": "Test"})
    db.upsert_legacy_asset({
        "library_uuid": library_uuid,
        "asset_uuid": asset_uuid,
        "original_filename": "img.jpg",
        "date_taken": date_taken,
        "named_face_count": 0,
        "unknown_face_count": 0,
        "title": "Vacation",
        "description": "sunny day",
        "keywords": '["beach"]',
    })


class TestMarkLegacyUploaded:
    def test_sets_uploaded_flickr_id(self, tmp_path):
        db = _make_db(tmp_path)
        _seed(db)
        db.mark_legacy_uploaded("L", "A", "flickr123")
        row = db.conn.execute(
            "SELECT uploaded_flickr_id, uploaded_at FROM legacy_assets WHERE asset_uuid='A'"
        ).fetchone()
        assert row["uploaded_flickr_id"] == "flickr123"
        assert row["uploaded_at"] is not None

    def test_overwrites_previous_value(self, tmp_path):
        db = _make_db(tmp_path)
        _seed(db)
        db.mark_legacy_uploaded("L", "A", "first")
        db.mark_legacy_uploaded("L", "A", "second")
        row = db.conn.execute(
            "SELECT uploaded_flickr_id FROM legacy_assets WHERE asset_uuid='A'"
        ).fetchone()
        assert row["uploaded_flickr_id"] == "second"


class TestIterUnrecoveredLegacyUploads:
    def test_returns_asset_when_no_photos_row(self, tmp_path):
        db = _make_db(tmp_path)
        _seed(db)
        db.mark_legacy_uploaded("L", "A", "flickr999")
        results = db.iter_unrecovered_legacy_uploads("L")
        assert len(results) == 1
        assert results[0]["asset_uuid"] == "A"
        assert results[0]["uploaded_flickr_id"] == "flickr999"

    def test_excludes_asset_when_photos_row_exists(self, tmp_path):
        db = _make_db(tmp_path)
        _seed(db)
        db.mark_legacy_uploaded("L", "A", "flickr999")
        db.conn.execute(
            "INSERT INTO photos (flickr_id, uuid, privacy_state) VALUES ('flickr999', NULL, 'auto_private')"
        )
        db.conn.commit()
        assert db.iter_unrecovered_legacy_uploads("L") == []

    def test_excludes_asset_with_no_uploaded_flickr_id(self, tmp_path):
        db = _make_db(tmp_path)
        _seed(db)
        assert db.iter_unrecovered_legacy_uploads("L") == []

    def test_scoped_to_library(self, tmp_path):
        db = _make_db(tmp_path)
        _seed(db, library_uuid="L1", asset_uuid="X")
        _seed(db, library_uuid="L2", asset_uuid="Y")
        db.set_legacy_library({"library_uuid": "L2", "display_name": "Other"})
        db.mark_legacy_uploaded("L2", "Y", "flickr777")
        assert db.iter_unrecovered_legacy_uploads("L1") == []
        assert len(db.iter_unrecovered_legacy_uploads("L2")) == 1


class TestRecordLegacyUpload:
    def test_creates_photos_row_and_operation_log(self, tmp_path):
        db = _make_db(tmp_path)
        photo_id = db.record_legacy_upload(
            flickr_id="flickr42",
            privacy_state="auto_private",
            privacy_reason="geofence: home",
            date_taken="2005-06-01 12:00:00",
            width=4000,
            height=3000,
            flickr_title="Vacation",
            flickr_tags='["beach"]',
            flickr_description="sunny day",
            trigger="legacy:A clf=1",
        )
        row = db.conn.execute("SELECT * FROM photos WHERE id = ?", (photo_id,)).fetchone()
        assert row["flickr_id"] == "flickr42"
        assert row["uuid"] is None
        assert row["privacy_state"] == "auto_private"
        assert row["flickr_title"] == "Vacation"

        log_row = db.conn.execute(
            "SELECT * FROM operation_log WHERE photo_id = ?", (photo_id,)
        ).fetchone()
        assert log_row["operation"] == "upload_legacy_asset"
        assert log_row["target"] == "flickr_id"
        assert log_row["old_value"] is None
        assert log_row["new_value"] == "flickr42"
        assert log_row["trigger"] == "legacy:A clf=1"
        assert log_row["actor"] == "bp"

    def test_photos_row_and_log_roll_back_together(self, tmp_path):
        """If the operation_log INSERT fails, the photos INSERT should also roll back."""
        import sqlite3 as _sqlite3

        db = _make_db(tmp_path)
        # Drop operation_log to force INSERT failure
        db.conn.execute("DROP TABLE operation_log")
        db.conn.commit()

        try:
            db.record_legacy_upload(
                flickr_id="flickr99",
                privacy_state="auto_private",
                privacy_reason="test",
                date_taken=None,
                width=None,
                height=None,
                flickr_title="",
                flickr_tags="[]",
                flickr_description="",
                trigger="legacy:X clf=1",
            )
        except Exception:
            pass

        count = db.conn.execute(
            "SELECT COUNT(*) FROM photos WHERE flickr_id='flickr99'"
        ).fetchone()[0]
        assert count == 0
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_db_legacy_upload.py -q
```
Expected: `AttributeError: 'Database' object has no attribute 'mark_legacy_uploaded'`

- [ ] **Step 3: Add the three methods to db/db.py**

Append after the `delete_legacy_assets_not_in` method (the last method in the file):

```python
    def mark_legacy_uploaded(
        self, library_uuid: str, asset_uuid: str, flickr_id: str
    ) -> None:
        """Record that a legacy asset has been uploaded to Flickr.

        Sets uploaded_flickr_id and uploaded_at immediately after upload,
        before the photos row is created. This is the idempotency guard for
        bp upload-legacy-unmatched (#230) — on re-run, assets with this set
        are skipped in the upload loop and repaired in the recovery phase.
        """
        self.conn.execute(
            "UPDATE legacy_assets SET uploaded_flickr_id = ?, uploaded_at = ? "
            "WHERE library_uuid = ? AND asset_uuid = ?",
            (flickr_id, _now_iso(), library_uuid, asset_uuid),
        )
        self.conn.commit()

    def iter_unrecovered_legacy_uploads(self, library_uuid: str) -> list[dict]:
        """Return legacy assets uploaded to Flickr but missing a photos row.

        These are partial failures from bp upload-legacy-unmatched: the Flickr
        upload succeeded and uploaded_flickr_id was set, but the photos row
        write failed. Phase 1 of the next run repairs them.
        """
        rows = self.conn.execute(
            "SELECT la.* FROM legacy_assets la "
            "WHERE la.library_uuid = ? AND la.uploaded_flickr_id IS NOT NULL "
            "AND NOT EXISTS "
            "(SELECT 1 FROM photos p WHERE p.flickr_id = la.uploaded_flickr_id)",
            (library_uuid,),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def record_legacy_upload(
        self,
        flickr_id: str,
        privacy_state: str,
        privacy_reason: str,
        *,
        date_taken: str | None,
        width: int | None,
        height: int | None,
        flickr_title: str,
        flickr_tags: str,
        flickr_description: str,
        trigger: str,
    ) -> int:
        """Atomically insert a photos row + operation_log entry for a legacy upload.

        Unlike log_operation (fire-and-forget), both writes are in one transaction:
        an operation_log failure rolls the photos INSERT back too. Never a DB record
        without its audit trail.
        """
        now = _now_iso()
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO photos "
                "(flickr_id, uuid, privacy_state, privacy_reason, date_taken, "
                " width, height, flickr_title, flickr_tags, flickr_description, "
                " date_synced, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    flickr_id, None, privacy_state, privacy_reason, date_taken,
                    width, height, flickr_title, flickr_tags, flickr_description,
                    now, now,
                ),
            )
            photo_id = cursor.lastrowid
            self.conn.execute(
                "INSERT INTO operation_log "
                "(occurred_at, photo_id, operation, target, "
                " old_value, new_value, trigger, actor) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (now, photo_id, "upload_legacy_asset", "flickr_id",
                 None, flickr_id, trigger, "bp"),
            )
        return photo_id
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_db_legacy_upload.py -q
```
Expected: `8 passed`

- [ ] **Step 5: Commit**

```bash
git add db/db.py tests/test_db_legacy_upload.py
git commit -m "feat(#230): db methods for legacy upload tracking"
```

---

## Task 3: FlickrClient.upload_photo()

**Files:**
- Modify: `flickr/flickr_client.py` (add `UPLOAD_URL` constant after `REST_URL` on line 23; append `upload_photo` at end of class)
- Create: `tests/test_flickr_upload.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_flickr_upload.py
from __future__ import annotations
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from flickr.flickr_client import FlickrClient, FlickrError  # noqa: E402


def _client() -> FlickrClient:
    c = FlickrClient.__new__(FlickrClient)
    c.api_key = "key"
    c.user_nsid = "nsid"
    c._rate_delay = 0
    c._session = MagicMock()
    return c


def _ok_response(flickr_id: str = "99887766") -> MagicMock:
    m = MagicMock()
    m.text = f'<?xml version="1.0" ?><rsp stat="ok"><photoid>{flickr_id}</photoid></rsp>'
    m.raise_for_status = MagicMock()
    return m


def _err_response(msg: str = "oops") -> MagicMock:
    m = MagicMock()
    m.text = f'<?xml version="1.0" ?><rsp stat="fail"><err code="5" msg="{msg}" /></rsp>'
    m.raise_for_status = MagicMock()
    return m


class TestUploadPhoto:
    def test_posts_to_upload_url(self, tmp_path):
        photo = tmp_path / "img.jpg"
        photo.write_bytes(b"JPEG")
        c = _client()
        c._session.post.return_value = _ok_response()
        with patch.object(c, "_call", return_value={}):
            c.upload_photo(photo)
        url = c._session.post.call_args[0][0]
        assert url == "https://up.flickr.com/services/upload/"

    def test_returns_flickr_id_and_date_set_ok_true(self, tmp_path):
        photo = tmp_path / "img.jpg"
        photo.write_bytes(b"JPEG")
        c = _client()
        c._session.post.return_value = _ok_response("42")
        with patch.object(c, "_call", return_value={}):
            flickr_id, date_set_ok = c.upload_photo(photo, date_taken="2005-06-01 12:00:00")
        assert flickr_id == "42"
        assert date_set_ok is True

    def test_always_uploads_private(self, tmp_path):
        photo = tmp_path / "img.jpg"
        photo.write_bytes(b"JPEG")
        c = _client()
        c._session.post.return_value = _ok_response()
        with patch.object(c, "_call", return_value={}):
            c.upload_photo(photo)
        data = c._session.post.call_args[1]["data"]
        assert data["is_public"] == "0"
        assert data["is_friend"] == "0"
        assert data["is_family"] == "0"

    def test_passes_metadata_fields(self, tmp_path):
        photo = tmp_path / "img.jpg"
        photo.write_bytes(b"JPEG")
        c = _client()
        c._session.post.return_value = _ok_response()
        with patch.object(c, "_call", return_value={}):
            c.upload_photo(photo, title="Beach", description="Sunny", tags="beach summer")
        data = c._session.post.call_args[1]["data"]
        assert data["title"] == "Beach"
        assert data["description"] == "Sunny"
        assert data["tags"] == "beach summer"

    def test_calls_set_dates_when_date_taken_provided(self, tmp_path):
        photo = tmp_path / "img.jpg"
        photo.write_bytes(b"JPEG")
        c = _client()
        c._session.post.return_value = _ok_response("55")
        with patch.object(c, "_call", return_value={}) as mock_call:
            c.upload_photo(photo, date_taken="2005-06-01 12:00:00")
        mock_call.assert_called_once_with(
            "flickr.photos.setDates",
            {"photo_id": "55", "date_taken": "2005-06-01 12:00:00", "date_taken_granularity": "0"},
            http_method="POST",
        )

    def test_skips_set_dates_when_no_date_taken(self, tmp_path):
        photo = tmp_path / "img.jpg"
        photo.write_bytes(b"JPEG")
        c = _client()
        c._session.post.return_value = _ok_response()
        with patch.object(c, "_call", return_value={}) as mock_call:
            c.upload_photo(photo)
        mock_call.assert_not_called()

    def test_returns_date_set_ok_false_when_set_dates_fails(self, tmp_path):
        photo = tmp_path / "img.jpg"
        photo.write_bytes(b"JPEG")
        c = _client()
        c._session.post.return_value = _ok_response("77")
        with patch.object(c, "_call", side_effect=FlickrError(1, "not found")):
            flickr_id, date_set_ok = c.upload_photo(photo, date_taken="2005-06-01 12:00:00")
        assert flickr_id == "77"
        assert date_set_ok is False

    def test_raises_flickr_error_on_stat_fail(self, tmp_path):
        photo = tmp_path / "img.jpg"
        photo.write_bytes(b"JPEG")
        c = _client()
        c._session.post.return_value = _err_response("quota exceeded")
        with pytest.raises(FlickrError):
            c.upload_photo(photo)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_flickr_upload.py -q
```
Expected: `AttributeError: type object 'FlickrClient' has no attribute 'upload_photo'` (or similar)

- [ ] **Step 3: Add UPLOAD_URL constant to flickr_client.py**

In `flickr/flickr_client.py`, after line 23 (`REST_URL = "https://api.flickr.com/services/rest/"`):

```python
UPLOAD_URL = "https://up.flickr.com/services/upload/"
```

- [ ] **Step 4: Append upload_photo method to FlickrClient**

At the end of the `FlickrClient` class in `flickr/flickr_client.py`:

```python
    # -----------------------------------------------------------------------
    # Photo upload
    # -----------------------------------------------------------------------

    def upload_photo(
        self,
        path: "Path",
        title: str = "",
        description: str = "",
        tags: str = "",
        date_taken: str | None = None,
        is_public: int = 0,
        is_friend: int = 0,
        is_family: int = 0,
    ) -> "tuple[str, bool]":
        """Upload a photo file to Flickr. Returns (flickr_id, date_set_ok).

        Uses the upload endpoint (up.flickr.com), not the REST API — different
        base URL, multipart POST, XML response. Does NOT go through _call().

        All uploads are private (is_public=0, is_friend=0, is_family=0 by
        default). Privacy is managed by BP's pipeline after upload.

        If date_taken is provided, calls flickr.photos.setDates after upload.
        Returns date_set_ok=False (and logs a warning) if setDates fails —
        bp sync-metadata can repair the date later.
        """
        import xml.etree.ElementTree as ET
        from pathlib import Path as _Path

        data = {
            "title": title,
            "description": description,
            "tags": tags,
            "is_public": str(is_public),
            "is_friend": str(is_friend),
            "is_family": str(is_family),
            "content_type": "1",  # photo (not screenshot or other)
            "hidden": "2",        # hide from global search results
        }

        with open(path, "rb") as fh:
            resp = self._session.post(
                UPLOAD_URL,
                data=data,
                files={"photo": fh},
                timeout=120,
            )
        resp.raise_for_status()

        tree = ET.fromstring(resp.text)
        if tree.attrib.get("stat") != "ok":
            err_el = tree.find("err")
            msg = (
                err_el.attrib.get("msg", "unknown") if err_el is not None else "unknown"
            )
            raise FlickrError(-1, f"Upload failed: {msg}")

        photoid_el = tree.find("photoid")
        if photoid_el is None:
            raise FlickrError(-1, "Upload response missing <photoid>")
        flickr_id = (photoid_el.text or "").strip()

        date_set_ok = True
        if date_taken:
            try:
                self._call(
                    "flickr.photos.setDates",
                    {
                        "photo_id": flickr_id,
                        "date_taken": date_taken,
                        "date_taken_granularity": "0",
                    },
                    http_method="POST",
                )
            except Exception:
                log.warning(
                    f"upload_photo: setDates failed for {flickr_id} — "
                    "date can be repaired by bp sync-metadata"
                )
                date_set_ok = False

        return flickr_id, date_set_ok
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
python -m pytest tests/test_flickr_upload.py -q
```
Expected: `8 passed`

- [ ] **Step 6: Commit**

```bash
git add flickr/flickr_client.py tests/test_flickr_upload.py
git commit -m "feat(#230): FlickrClient.upload_photo() — multipart POST, XML response, setDates"
```

---

## Task 4: legacy_uploader.py — orchestration

**Files:**
- Create: `poller/legacy_uploader.py`
- Create: `tests/test_legacy_uploader.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_legacy_uploader.py
from __future__ import annotations
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "poller"))


def _make_db(tmp_path):
    from db.db import Database
    from db.migrations.migrate_020_operation_log import run as run_op_log
    from db.migrations.migrate_026_legacy_index import run_on_conn as run_legacy
    from db.migrations.migrate_031_legacy_upload import run_on_conn as run_upload

    db = Database(str(tmp_path / "curator.db"))
    run_op_log(str(tmp_path / "curator.db"))
    run_legacy(db.conn)
    run_upload(db.conn)
    return db


def _seed_lib(db, library_uuid="L"):
    db.set_legacy_library({"library_uuid": library_uuid, "display_name": "Test"})


def _seed_asset(db, asset_uuid="A", date_taken="2005-06-01 12:00:00",
                library_uuid="L", master_rel_path="Masters/img.jpg", **kw):
    db.upsert_legacy_asset({
        "library_uuid": library_uuid,
        "asset_uuid": asset_uuid,
        "original_filename": "img.jpg",
        "date_taken": date_taken,
        "named_face_count": 0,
        "unknown_face_count": 0,
        "master_rel_path": master_rel_path,
        "title": "Vacation",
        "description": "sunny",
        "keywords": '["beach"]',
        **kw,
    })


class _StubFlickr:
    """Records upload calls; returns sequential fake flickr_ids."""
    def __init__(self, fail=False, date_set_ok=True):
        self.calls = []
        self._fail = fail
        self._date_set_ok = date_set_ok
        self._counter = 0

    def upload_photo(self, path, *, title="", description="", tags="",
                     date_taken=None, is_public=0, is_friend=0, is_family=0):
        from flickr.flickr_client import FlickrError
        self.calls.append({"path": path, "date_taken": date_taken})
        if self._fail:
            raise FlickrError(-1, "simulated upload failure")
        self._counter += 1
        return f"flickr{self._counter:04d}", self._date_set_ok


def _run(db, library_uuid, library_path, flickr, *, dry_run=False, limit=None):
    from legacy_uploader import upload_unmatched_assets
    from analyzer.privacy import CLASSIFIER_VERSION
    return upload_unmatched_assets(
        db, library_uuid, library_path, flickr,
        self_name="", zones=[], person_policies={},
        classifier_version=CLASSIFIER_VERSION,
        limit=limit, dry_run=dry_run,
    )


class TestUploadUnmatchedAssets:
    def test_successful_upload_creates_photos_row(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_lib(db)
        _seed_asset(db)
        photo_file = tmp_path / "Masters" / "img.jpg"
        photo_file.parent.mkdir(parents=True)
        photo_file.write_bytes(b"JPEG")

        flickr = _StubFlickr()
        counts = _run(db, "L", tmp_path, flickr)

        assert counts["uploaded"] == 1
        assert counts["upload_failed"] == 0
        row = db.conn.execute("SELECT * FROM photos WHERE flickr_id = 'flickr0001'").fetchone()
        assert row is not None
        assert row["uuid"] is None
        assert row["privacy_state"] == "candidate_public"

    def test_uploaded_flickr_id_set_after_upload(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_lib(db)
        _seed_asset(db)
        (tmp_path / "Masters").mkdir()
        (tmp_path / "Masters" / "img.jpg").write_bytes(b"JPEG")

        _run(db, "L", tmp_path, _StubFlickr())

        row = db.conn.execute(
            "SELECT uploaded_flickr_id FROM legacy_assets WHERE asset_uuid='A'"
        ).fetchone()
        assert row["uploaded_flickr_id"] == "flickr0001"

    def test_dry_run_makes_no_writes(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_lib(db)
        _seed_asset(db)
        (tmp_path / "Masters").mkdir()
        (tmp_path / "Masters" / "img.jpg").write_bytes(b"JPEG")

        flickr = _StubFlickr()
        counts = _run(db, "L", tmp_path, flickr, dry_run=True)

        assert flickr.calls == []
        assert db.conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == 0
        # dry-run still classifies and counts
        assert counts["candidate_public"] == 1

    def test_asset_with_uploaded_flickr_id_is_skipped(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_lib(db)
        _seed_asset(db)
        db.mark_legacy_uploaded("L", "A", "already_done")

        flickr = _StubFlickr()
        counts = _run(db, "L", tmp_path, flickr)

        assert flickr.calls == []
        assert counts["skipped_already_uploaded"] == 1

    def test_missing_file_is_skipped(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_lib(db)
        _seed_asset(db)  # file does not exist on disk

        flickr = _StubFlickr()
        counts = _run(db, "L", tmp_path, flickr)

        assert flickr.calls == []
        assert counts["skipped_missing_file"] == 1
        assert counts["uploaded"] == 0

    def test_upload_failure_is_isolated(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_lib(db)
        _seed_asset(db, asset_uuid="A")
        _seed_asset(db, asset_uuid="B", date_taken="2006-01-01 10:00:00",
                    master_rel_path="Masters/b.jpg")
        (tmp_path / "Masters").mkdir()
        (tmp_path / "Masters" / "img.jpg").write_bytes(b"JPEG")
        (tmp_path / "Masters" / "b.jpg").write_bytes(b"JPEG")

        call_count = [0]
        class _FailFirst(_StubFlickr):
            def upload_photo(self, path, **kw):
                call_count[0] += 1
                if call_count[0] == 1:
                    from flickr.flickr_client import FlickrError
                    raise FlickrError(-1, "first fails")
                self._counter += 1
                return f"flickr{self._counter:04d}", True

        counts = _run(db, "L", tmp_path, _FailFirst())
        assert counts["upload_failed"] == 1
        assert counts["uploaded"] == 1

    def test_date_set_failed_is_counted(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_lib(db)
        _seed_asset(db)
        (tmp_path / "Masters").mkdir()
        (tmp_path / "Masters" / "img.jpg").write_bytes(b"JPEG")

        counts = _run(db, "L", tmp_path, _StubFlickr(date_set_ok=False))
        assert counts["date_set_failed"] == 1
        assert counts["uploaded"] == 1  # still uploaded

    def test_phase1_recovery_creates_photos_row(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_lib(db)
        _seed_asset(db)
        # Simulate: uploaded but photos row write failed
        db.mark_legacy_uploaded("L", "A", "orphan001")
        # No photos row for orphan001

        flickr = _StubFlickr()
        counts = _run(db, "L", tmp_path, flickr)

        assert counts["recovered"] == 1
        assert flickr.calls == []  # no new upload
        row = db.conn.execute("SELECT * FROM photos WHERE flickr_id='orphan001'").fetchone()
        assert row is not None

    def test_phase1_recovery_reports_only_in_dry_run(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_lib(db)
        _seed_asset(db)
        db.mark_legacy_uploaded("L", "A", "orphan002")

        counts = _run(db, "L", tmp_path, _StubFlickr(), dry_run=True)

        assert counts["recovered"] == 1
        assert db.conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == 0

    def test_privacy_state_stored_correctly(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_lib(db)
        _seed_asset(db, named_face_count=1, persons='["Alice"]')
        (tmp_path / "Masters").mkdir()
        (tmp_path / "Masters" / "img.jpg").write_bytes(b"JPEG")

        _run(db, "L", tmp_path, _StubFlickr())

        row = db.conn.execute("SELECT privacy_state FROM photos").fetchone()
        assert row["privacy_state"] == "needs_review"

    def test_limit_caps_uploads(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_lib(db)
        for i in range(5):
            _seed_asset(db, asset_uuid=f"A{i}", date_taken=f"200{i}-01-01 10:00:00",
                        master_rel_path=f"Masters/img{i}.jpg")
            (tmp_path / "Masters").mkdir(exist_ok=True)
            (tmp_path / f"Masters/img{i}.jpg").write_bytes(b"JPEG")

        flickr = _StubFlickr()
        counts = _run(db, "L", tmp_path, flickr, limit=2)
        assert counts["uploaded"] == 2
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_legacy_uploader.py -q
```
Expected: `ModuleNotFoundError: No module named 'legacy_uploader'`

- [ ] **Step 3: Write legacy_uploader.py**

```python
# poller/legacy_uploader.py
"""Upload unmatched legacy assets to Flickr (#230).

Phase 1 (recovery): find assets with uploaded_flickr_id but no photos row
and create the missing photos rows. Repairs partial failures from a prior run.

Phase 2 (upload loop): for each asset in report_unmatched() that has no
uploaded_flickr_id, classify it, upload to Flickr, mark the legacy_assets row,
then write the photos row + operation_log entry atomically.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from legacy_match import shape_legacy_for_classify  # noqa: E402
from legacy_report import report_unmatched  # noqa: E402

log = logging.getLogger("blue-pearmain.legacy-uploader")

_UPLOAD_TRIGGER = "legacy:{asset_uuid} clf={clf}"


def _trigger(asset_uuid: str, classifier_version: int) -> str:
    return _UPLOAD_TRIGGER.format(asset_uuid=asset_uuid, clf=classifier_version)


def _classify_asset(asset: dict, zones: list, self_name: str, person_policies: dict):
    from analyzer.privacy import classify
    shaped = shape_legacy_for_classify(asset)
    return classify(shaped, zones, self_name, person_policies)


def _do_record(db, flickr_id: str, asset: dict, privacy_state: str, privacy_reason: str,
               classifier_version: int) -> None:
    """Write photos row + operation_log atomically. Raises on failure."""
    db.record_legacy_upload(
        flickr_id=flickr_id,
        privacy_state=privacy_state,
        privacy_reason=privacy_reason,
        date_taken=asset.get("date_taken"),
        width=asset.get("width"),
        height=asset.get("height"),
        flickr_title=asset.get("title") or "",
        flickr_tags=asset.get("keywords") or "[]",
        flickr_description=asset.get("description") or "",
        trigger=_trigger(asset["asset_uuid"], classifier_version),
    )


def upload_unmatched_assets(
    db,
    library_uuid: str,
    library_path: Path,
    flickr_client,
    *,
    self_name: str,
    zones: list[dict],
    person_policies: dict[str, str],
    classifier_version: int,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict:
    """Upload legacy assets with no Flickr counterpart.

    Returns counts dict with keys:
        eligible, uploaded, recovered,
        skipped_already_uploaded, skipped_missing_file,
        auto_private, needs_review, candidate_public,
        date_set_failed, db_write_failed, upload_failed.
    """
    counts: dict[str, int] = {
        "eligible": 0,
        "uploaded": 0,
        "recovered": 0,
        "skipped_already_uploaded": 0,
        "skipped_missing_file": 0,
        "auto_private": 0,
        "needs_review": 0,
        "candidate_public": 0,
        "date_set_failed": 0,
        "db_write_failed": 0,
        "upload_failed": 0,
    }

    # ── Phase 1: recover partial failures ────────────────────────────────────
    unrecovered = db.iter_unrecovered_legacy_uploads(library_uuid)
    for asset in unrecovered:
        privacy_state, privacy_reason = _classify_asset(
            asset, zones, self_name, person_policies
        )
        flickr_id = asset["uploaded_flickr_id"]
        if not dry_run:
            try:
                _do_record(db, flickr_id, asset, privacy_state, privacy_reason, classifier_version)
                counts["recovered"] += 1
            except Exception as exc:
                log.error(f"Phase 1: failed to create photos row for {flickr_id}: {exc}")
        else:
            counts["recovered"] += 1  # report-only in dry-run

    # ── Phase 2: upload loop ──────────────────────────────────────────────────
    report = report_unmatched(db, library_uuid)
    assets = report["assets"]
    if limit is not None:
        assets = assets[:limit]
    counts["eligible"] = len(assets)

    for asset in assets:
        # Idempotency guard: uploaded_flickr_id already set means upload happened
        if asset.get("uploaded_flickr_id"):
            counts["skipped_already_uploaded"] += 1
            continue

        # File must exist
        rel = asset.get("master_rel_path")
        if not rel:
            counts["skipped_missing_file"] += 1
            continue
        file_path = library_path / rel
        if not file_path.exists():
            counts["skipped_missing_file"] += 1
            continue

        # Classify
        privacy_state, privacy_reason = _classify_asset(asset, zones, self_name, person_policies)

        if dry_run:
            counts[privacy_state] += 1
            continue

        # Build tags string from JSON array
        kw = asset.get("keywords") or "[]"
        try:
            tags_list = json.loads(kw) if isinstance(kw, str) else kw
        except (ValueError, TypeError):
            tags_list = []
        tags_str = " ".join(str(t) for t in tags_list)

        # Upload to Flickr
        try:
            flickr_id, date_set_ok = flickr_client.upload_photo(
                file_path,
                title=asset.get("title") or "",
                description=asset.get("description") or "",
                tags=tags_str,
                date_taken=asset.get("date_taken"),
            )
        except Exception as exc:
            log.error(f"Upload failed for {asset['asset_uuid']}: {exc}")
            counts["upload_failed"] += 1
            continue

        if not date_set_ok:
            counts["date_set_failed"] += 1

        # Mark upload in legacy_assets (idempotency guard for re-runs)
        try:
            db.mark_legacy_uploaded(library_uuid, asset["asset_uuid"], flickr_id)
        except Exception as exc:
            log.error(
                f"ORPHAN: uploaded {flickr_id} but failed to mark in legacy_assets: {exc}"
            )
            print(
                f"ORPHAN UPLOAD — flickr_id={flickr_id} asset={asset['asset_uuid']}",
                flush=True,
            )
            counts["db_write_failed"] += 1
            continue

        # Create photos row + operation_log (atomic)
        try:
            _do_record(db, flickr_id, asset, privacy_state, privacy_reason, classifier_version)
        except Exception as exc:
            log.error(
                f"photos row write failed for {flickr_id}: {exc} "
                "— will be recovered on next run (uploaded_flickr_id is set)"
            )
            counts["db_write_failed"] += 1
            continue

        counts["uploaded"] += 1
        counts[privacy_state] += 1

    return counts
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_legacy_uploader.py -q
```
Expected: `11 passed`

- [ ] **Step 5: Commit**

```bash
git add poller/legacy_uploader.py tests/test_legacy_uploader.py
git commit -m "feat(#230): legacy_uploader.py — Phase 1 recovery + Phase 2 upload loop"
```

---

## Task 5: CLI command and README

**Files:**
- Modify: `bp` (add `cmd_upload_legacy_unmatched` function; add subparser after `legacy-report` block; add dispatch entry)
- Modify: `README.md`

- [ ] **Step 1: Add cmd_upload_legacy_unmatched to bp**

In `bp`, after the `cmd_legacy_report` function (around line 1168, before `def cmd_geocode`), add:

```python
def cmd_upload_legacy_unmatched(args: argparse.Namespace) -> None:
    """Upload legacy-only assets to Flickr, bypassing Apple Photos (#230)."""
    import yaml

    sys.path.insert(0, str(ROOT / "poller"))
    from db.db import Database
    from legacy_uploader import upload_unmatched_assets
    from analyzer.privacy import CLASSIFIER_VERSION

    with open(args.config) as f:
        config = yaml.safe_load(f)

    db_path = str(Path(config["database"]["path"]).expanduser())
    library_path = Path(_resolve_legacy_library_path(args, config)).expanduser()

    if not library_path.exists():
        print(f"Error: library not found / not mounted: {library_path}", file=sys.stderr)
        sys.exit(1)

    db = Database(db_path)
    try:
        libs = db.conn.execute(
            "SELECT library_uuid FROM legacy_libraries ORDER BY indexed_at DESC"
        ).fetchall()
        if not libs:
            print(
                "No indexed legacy library found. Run 'bp index-legacy' first.",
                file=sys.stderr,
            )
            sys.exit(1)
        if len(libs) > 1 and not getattr(args, "library_uuid", None):
            print(
                "Multiple legacy libraries indexed; pass --library-uuid <uuid>.",
                file=sys.stderr,
            )
            sys.exit(2)
        library_uuid = getattr(args, "library_uuid", None) or libs[0]["library_uuid"]

        if args.dry_run:
            flickr_client = None
        else:
            from flickr.flickr_client import FlickrClient
            flickr_client = FlickrClient.from_config(config)

        self_name = config.get("photos_library", {}).get("self_name", "")
        zones = db.active_zones()
        person_policies = db.get_person_policies()

        counts = upload_unmatched_assets(
            db,
            library_uuid,
            library_path,
            flickr_client,
            self_name=self_name,
            zones=zones,
            person_policies=person_policies,
            classifier_version=CLASSIFIER_VERSION,
            limit=getattr(args, "limit", None),
            dry_run=args.dry_run,
        )
    finally:
        db.close()

    prefix = "Legacy upload dry run" if args.dry_run else "Legacy upload"
    print(prefix)
    print(f"  library_uuid       : {library_uuid}")
    if counts["recovered"]:
        print(f"  recovered          : {counts['recovered']}")
    if args.dry_run:
        print(f"  eligible           : {counts['eligible']}")
        print(
            f"  would upload       : "
            f"{counts['eligible'] - counts['skipped_missing_file']}"
        )
        print(f"  skipped (no file)  : {counts['skipped_missing_file']}")
        print("\nWould-be privacy states:")
    else:
        print(f"  eligible           : {counts['eligible']}")
        print(f"  uploaded           : {counts['uploaded']}")
        print(f"  already uploaded   : {counts['skipped_already_uploaded']}")
        print(f"  skipped (no file)  : {counts['skipped_missing_file']}")
        print(f"  upload failed      : {counts['upload_failed']}")
        print(f"  db write failed    : {counts['db_write_failed']}")
        if counts["date_set_failed"]:
            print(f"  date set failed    : {counts['date_set_failed']}  (bp sync-metadata will fix)")
        print("\nPrivacy states applied:")
    print(f"  auto_private       : {counts['auto_private']}")
    print(f"  needs_review       : {counts['needs_review']}")
    print(f"  candidate_public   : {counts['candidate_public']}")
```

- [ ] **Step 2: Add the subparser to bp**

In `bp`, after the `p_lrep.set_defaults(func=cmd_legacy_report)` line and before the `# geocode` comment (around line 1778), add:

```python
    # upload-legacy-unmatched
    p_ulm = sub.add_parser(
        "upload-legacy-unmatched",
        help="Upload legacy-only assets (not yet on Flickr) directly to Flickr (#230)",
    )
    p_ulm.add_argument(
        "--dry-run",
        action="store_true",
        help="Classify and report without uploading or writing to the DB",
    )
    p_ulm.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Upload at most N assets (for incremental rollout)",
    )
    p_ulm.add_argument(
        "--library-uuid",
        default=None,
        help="Which indexed library to draw from (default: most recently indexed)",
    )
    p_ulm.add_argument(
        "--library",
        default=None,
        metavar="PATH",
        help="Path to the .photoslibrary bundle (overrides config legacy_library.path)",
    )
```

- [ ] **Step 3: Add dispatch entry to bp**

In `bp`, in the `dispatch` dict (around line 1928 where `"match-legacy"` and `"legacy-report"` are listed), add:

```python
        "upload-legacy-unmatched": cmd_upload_legacy_unmatched,
```

- [ ] **Step 4: Update README.md**

After the `bp legacy-report` lines in the command reference (after the `bp match-legacy --apply` block, around line 198), add:

```
bp legacy-report                   # Report legacy assets not yet on Flickr (counts + breakdown by year)
bp legacy-report --csv PATH        # Also write unmatched assets to a CSV file
bp upload-legacy-unmatched --dry-run  # Classify unmatched assets and report what would happen without uploading
bp upload-legacy-unmatched         # Upload unmatched legacy assets directly to Flickr (always private; BP pipeline handles promotion)
bp upload-legacy-unmatched --limit N  # Upload at most N assets (for incremental rollout)
```

- [ ] **Step 5: Run the full test suite and lint**

```bash
python -m pytest tests/ -q
```
Expected: all tests pass (count increases by the new tests added in tasks 1–4).

```bash
make lint
```
Expected: `All checks passed!`

If ruff formatting errors appear, run:
```bash
uv run --with ruff ruff format poller/legacy_uploader.py tests/test_legacy_uploader.py
```

- [ ] **Step 6: Commit**

```bash
git add bp README.md
git commit -m "feat(#230): bp upload-legacy-unmatched CLI command + README"
```

- [ ] **Step 7: Final commit — bump version and close issue**

```bash
make bump
ALLOW_MAIN_PUSH=1 git push && git push --tags
```

Then close GH #230 with a retrospective comment.

---

## Self-review

**Spec coverage check:**

| Spec requirement | Task |
|-----------------|------|
| Migration 031 — `uploaded_flickr_id`, `uploaded_at` | Task 1 |
| `FlickrClient.upload_photo()` — multipart POST, XML, `setDates`, returns `(str, bool)` | Task 3 |
| `UPLOAD_URL` constant | Task 3 |
| `mark_legacy_uploaded` | Task 2 |
| `iter_unrecovered_legacy_uploads` | Task 2 |
| `record_legacy_upload` — atomic photos + operation_log | Task 2 |
| Phase 1 recovery (pre-loop, dry-run reports only) | Task 4 |
| Phase 2 upload loop with idempotency guard | Task 4 |
| Per-asset error isolation | Task 4 |
| `--dry-run`, `--limit`, `--library-uuid`, `--library` flags | Task 5 |
| Output format (dry-run vs live) | Task 5 |
| README update | Task 5 |

**Type consistency check:**

- `upload_photo()` returns `tuple[str, bool]` — used as `flickr_id, date_set_ok = flickr_client.upload_photo(...)` in Task 4. ✓
- `record_legacy_upload()` takes `flickr_tags: str` (JSON array string) — `legacy_uploader.py` passes `asset.get("keywords") or "[]"` which is the raw JSON string from `legacy_assets`. ✓
- `_resolve_legacy_library_path(args, config)` — already exists in `bp`; `args` must have a `.library` attribute added by the subparser. ✓ (subparser adds `--library` which maps to `args.library`)

**Placeholder scan:** No TBDs or incomplete steps found.
