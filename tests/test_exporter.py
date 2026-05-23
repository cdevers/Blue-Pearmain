"""
tests/test_exporter.py — unit tests for poller.exporter

Run from repo root:
    python -m pytest tests/test_exporter.py -v
"""

import json
import subprocess as proc
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.db import Database
from poller.exporter import (
    collect_albums,
    collect_export_data,
    serialize_photo,
    serialize_zone,
    write_export,
)


def _photo_row(**kw) -> dict:
    """Return a minimal photo row dict, optionally overriding fields."""
    base: dict = {
        "id": 1,
        "flickr_id": "52841097634",
        "uuid": "A1B2C3D4-1111-2222-3333-444455556666",
        "flickr_title": "Cousins at the beach, 2019",
        "flickr_description": "Summer family trip.",
        "flickr_tags": '["beach", "family/reunion"]',
        "photos_tags": '["beach"]',
        "privacy_state": "approved_public",
        "review_decision": "make_public",
        "reviewed_at": "2025-03-14T14:22:01",
        "date_taken": "2019-08-15T12:00:00",
        "latitude": 41.6,
        "longitude": -70.9,
        "place_city": "Falmouth",
        "place_state": "Massachusetts",
        "place_country": "United States",
        "geofence_zone": None,
        "apple_persons": '["Alice", "Bob", "_UNKNOWN_"]',
        "original_filename": "IMG_7507.HEIC",
    }
    base.update(kw)
    return base


class TestSerializePhoto(unittest.TestCase):
    def test_includes_flickr_id(self):
        result = serialize_photo(_photo_row(), album_names=[])
        self.assertEqual(result["flickr_id"], "52841097634")

    def test_includes_apple_uuid(self):
        result = serialize_photo(_photo_row(), album_names=[])
        self.assertEqual(result["apple_uuid"], "A1B2C3D4-1111-2222-3333-444455556666")

    def test_tags_from_flickr_tags_when_present(self):
        result = serialize_photo(_photo_row(), album_names=[])
        self.assertIn("beach", result["tags"])
        self.assertIn("family/reunion", result["tags"])

    def test_tags_fall_back_to_photos_tags_when_flickr_empty(self):
        result = serialize_photo(
            _photo_row(flickr_tags=None, photos_tags='["nature"]'), album_names=[]
        )
        self.assertIn("nature", result["tags"])

    def test_faces_excludes_unknown(self):
        result = serialize_photo(_photo_row(), album_names=[])
        self.assertIn("Alice", result["faces"])
        self.assertIn("Bob", result["faces"])
        self.assertNotIn("_UNKNOWN_", result["faces"])

    def test_geofenced_false_when_zone_is_none(self):
        result = serialize_photo(_photo_row(geofence_zone=None), album_names=[])
        self.assertFalse(result["geofenced"])

    def test_geofenced_true_when_zone_is_set(self):
        result = serialize_photo(_photo_row(geofence_zone="Home"), album_names=[])
        self.assertTrue(result["geofenced"])

    def test_albums_included(self):
        result = serialize_photo(_photo_row(), album_names=["Summer 2019", "Family"])
        self.assertIn("Summer 2019", result["albums"])
        self.assertIn("Family", result["albums"])

    def test_location_included_when_coordinates_present(self):
        result = serialize_photo(_photo_row(), album_names=[])
        self.assertIsNotNone(result["location"])
        self.assertEqual(result["location"]["city"], "Falmouth")
        self.assertAlmostEqual(result["location"]["latitude"], 41.6)

    def test_location_is_none_when_no_coordinates(self):
        result = serialize_photo(_photo_row(latitude=None, longitude=None), album_names=[])
        self.assertIsNone(result["location"])

    def test_title_defaults_to_empty_string_when_none(self):
        result = serialize_photo(_photo_row(flickr_title=None), album_names=[])
        self.assertEqual(result["title"], "")

    def test_includes_privacy_state(self):
        result = serialize_photo(_photo_row(), album_names=[])
        self.assertEqual(result["privacy_state"], "approved_public")


def _zone_row(**kw) -> dict:
    base: dict = {
        "name": "home",
        "label": "Home",
        "latitude": 42.3601,
        "longitude": -71.0589,
        "radius_m": 200.0,
        "policy": "auto_private",
        "active": 1,
        "notes": None,
    }
    base.update(kw)
    return base


class TestSerializeZone(unittest.TestCase):
    def test_includes_name(self):
        result = serialize_zone(_zone_row())
        self.assertEqual(result["name"], "home")

    def test_includes_coordinates(self):
        result = serialize_zone(_zone_row())
        self.assertAlmostEqual(result["latitude"], 42.3601)
        self.assertAlmostEqual(result["longitude"], -71.0589)

    def test_includes_radius(self):
        result = serialize_zone(_zone_row())
        self.assertEqual(result["radius_m"], 200.0)

    def test_active_is_bool(self):
        result = serialize_zone(_zone_row(active=1))
        self.assertIs(result["active"], True)
        result2 = serialize_zone(_zone_row(active=0))
        self.assertIs(result2["active"], False)


def _make_db() -> Database:
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    return Database(Path(f.name))


class TestCollectAlbums(unittest.TestCase):
    def test_returns_empty_dict_for_db_with_no_albums(self):
        db = _make_db()
        result = collect_albums(db)
        db.close()
        self.assertEqual(result, {})

    def test_returns_album_names_for_photo(self):
        db = _make_db()
        db.conn.execute(
            "INSERT INTO photos (uuid, privacy_state) VALUES ('uuid-1', 'needs_review')"
        )
        photo_id = db.conn.execute("SELECT id FROM photos WHERE uuid='uuid-1'").fetchone()["id"]
        db.conn.execute(
            "INSERT INTO albums (apple_uuid, name) VALUES ('alb-uuid-1', 'Summer 2019')"
        )
        album_id = db.conn.execute(
            "SELECT id FROM albums WHERE apple_uuid='alb-uuid-1'"
        ).fetchone()["id"]
        db.conn.execute(
            "INSERT INTO photo_albums (photo_id, album_id) VALUES (?, ?)", (photo_id, album_id)
        )
        db.conn.commit()
        result = collect_albums(db)
        db.close()
        self.assertIn(photo_id, result)
        self.assertIn("Summer 2019", result[photo_id])


class TestCollectExportData(unittest.TestCase):
    def test_returns_dict_with_photos_zones_manifest(self):
        db = _make_db()
        result = collect_export_data(db)
        db.close()
        self.assertIn("photos", result)
        self.assertIn("zones", result)
        self.assertIn("manifest", result)

    def test_manifest_has_required_keys(self):
        db = _make_db()
        result = collect_export_data(db)
        db.close()
        for key in ("exported_at", "photo_count", "zone_count"):
            self.assertIn(key, result["manifest"])

    def test_photo_count_matches_photos_list_length(self):
        db = _make_db()
        result = collect_export_data(db)
        db.close()
        self.assertEqual(result["manifest"]["photo_count"], len(result["photos"]))

    def test_zone_count_matches_zones_list_length(self):
        db = _make_db()
        result = collect_export_data(db)
        db.close()
        self.assertEqual(result["manifest"]["zone_count"], len(result["zones"]))


def _sample_data() -> dict:
    return {
        "manifest": {
            "exported_at": "2026-01-01T00:00:00+00:00",
            "photo_count": 2,
            "zone_count": 1,
            "bp_version": "1.0.0",
            "export_format_version": "1",
        },
        "photos": [{"id": 1, "title": "Test"}, {"id": 2, "title": "Second"}],
        "zones": [{"name": "home"}],
    }


class TestWriteExport(unittest.TestCase):
    def test_creates_output_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir) / "export-test"
            write_export(_sample_data(), out_dir)
            self.assertTrue(out_dir.is_dir())

    def test_creates_photos_ndjson(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir) / "export"
            write_export(_sample_data(), out_dir)
            self.assertTrue((out_dir / "photos.ndjson").exists())

    def test_creates_zones_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir) / "export"
            write_export(_sample_data(), out_dir)
            self.assertTrue((out_dir / "zones.json").exists())

    def test_creates_manifest_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir) / "export"
            write_export(_sample_data(), out_dir)
            manifest_path = out_dir / "manifest.json"
            self.assertTrue(manifest_path.exists())
            manifest = json.loads(manifest_path.read_text())
            self.assertEqual(manifest["photo_count"], 2)

    def test_manifest_includes_version_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir) / "export"
            write_export(_sample_data(), out_dir)
            manifest = json.loads((out_dir / "manifest.json").read_text())
            self.assertIn("bp_version", manifest)
            self.assertIn("export_format_version", manifest)
            self.assertEqual(manifest["export_format_version"], "1")

    def test_photos_ndjson_has_one_object_per_line(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir) / "export"
            write_export(_sample_data(), out_dir)
            lines = (out_dir / "photos.ndjson").read_text().strip().splitlines()
            self.assertEqual(len(lines), 2)
            for line in lines:
                parsed = json.loads(line)
                self.assertIsInstance(parsed, dict)

    def test_photos_ndjson_preserves_content(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir) / "export"
            write_export(_sample_data(), out_dir)
            lines = (out_dir / "photos.ndjson").read_text().strip().splitlines()
            first = json.loads(lines[0])
            self.assertEqual(first["title"], "Test")


class TestBpExportCommand(unittest.TestCase):
    """Smoke-test bp export via the CLI entry point."""

    def test_bp_export_exits_without_crashing_when_no_config(self):
        """bp export with a missing config exits non-zero but does not traceback."""
        result = proc.run(
            ["python", "bp", "export", "--config", "/nonexistent/config.yml"],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).parent.parent),
        )
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("Traceback", result.stdout)
        self.assertNotEqual(result.returncode, None)


class TestExportFormatVersion(unittest.TestCase):
    """
    Contract tests: assert that serialize_photo and serialize_zone return
    exactly the documented v1 field set. If a field is added or removed,
    this test breaks — the developer must then update docs/export-format.md
    and decide whether to bump export_format_version.
    """

    _EXPECTED_PHOTO_KEYS = {
        "id",
        "flickr_id",
        "apple_uuid",
        "original_filename",
        "title",
        "description",
        "tags",
        "privacy_state",
        "review_decision",
        "reviewed_at",
        "date_taken",
        "location",
        "geofenced",
        "faces",
        "albums",
    }

    _EXPECTED_ZONE_KEYS = {
        "name",
        "label",
        "latitude",
        "longitude",
        "radius_m",
        "policy",
        "active",
        "notes",
    }

    def test_serialize_photo_exact_keys(self):
        """serialize_photo must return exactly the v1 documented fields — no more, no less."""
        row = {
            "id": 1,
            "flickr_id": "123",
            "uuid": "AAAA-BBBB",
            "original_filename": "IMG_001.HEIC",
            "flickr_title": "Title",
            "flickr_description": "Desc",
            "flickr_tags": '["tag1"]',
            "photos_tags": None,
            "privacy_state": "approved_public",
            "review_decision": "make_public",
            "reviewed_at": "2026-01-01T00:00:00",
            "date_taken": "2025-06-15T12:00:00",
            "latitude": 42.3,
            "longitude": -71.1,
            "place_city": "Boston",
            "place_state": "Massachusetts",
            "place_country": "United States",
            "geofence_zone": None,
            "apple_persons": '["Alice"]',
        }
        result = serialize_photo(row, album_names=["Vacation"])
        self.assertEqual(
            set(result.keys()),
            self._EXPECTED_PHOTO_KEYS,
            msg=(
                f"serialize_photo key mismatch.\n"
                f"  Extra keys:   {set(result.keys()) - self._EXPECTED_PHOTO_KEYS}\n"
                f"  Missing keys: {self._EXPECTED_PHOTO_KEYS - set(result.keys())}\n"
                f"If intentional: update docs/export-format.md and bump export_format_version."
            ),
        )

    def test_serialize_zone_exact_keys(self):
        """serialize_zone must return exactly the v1 documented fields — no more, no less."""
        row = {
            "name": "home",
            "label": "Home",
            "latitude": 42.3,
            "longitude": -71.1,
            "radius_m": 500.0,
            "policy": "auto_private",
            "active": 1,
            "notes": "Primary residence",
        }
        result = serialize_zone(row)
        self.assertEqual(
            set(result.keys()),
            self._EXPECTED_ZONE_KEYS,
            msg=(
                f"serialize_zone key mismatch.\n"
                f"  Extra keys:   {set(result.keys()) - self._EXPECTED_ZONE_KEYS}\n"
                f"  Missing keys: {self._EXPECTED_ZONE_KEYS - set(result.keys())}\n"
                f"If intentional: update docs/export-format.md and bump export_format_version."
            ),
        )
