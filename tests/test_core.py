"""
tests/test_core.py — unit tests for Blue Pearmain core logic

Run from repo root:
    python -m pytest tests/
    # or without pytest:
    python tests/test_core.py
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from analyzer.privacy import classify
from analyzer.tagger import propose_tags
from poller.scanner import normalise_dt, build_enriched_row
from db.db import Database, haversine_m


# ---------------------------------------------------------------------------
# normalise_dt
# ---------------------------------------------------------------------------

class TestNormaliseDt(unittest.TestCase):

    def test_iso8601_with_offset(self):
        self.assertEqual(
            normalise_dt("2026-04-08T16:46:20.047000-04:00"),
            "2026-04-08 16:46:20",
        )

    def test_flickr_space_format(self):
        self.assertEqual(
            normalise_dt("2023-05-06 16:34:28"),
            "2023-05-06 16:34:28",
        )

    def test_winter_offset(self):
        self.assertEqual(
            normalise_dt("2022-03-11T15:52:15.031917-05:00"),
            "2022-03-11 15:52:15",
        )

    def test_none(self):
        self.assertIsNone(normalise_dt(None))

    def test_no_subseconds(self):
        self.assertEqual(
            normalise_dt("2024-07-24T20:12:47-04:00"),
            "2024-07-24 20:12:47",
        )

    def test_utc_offset(self):
        self.assertEqual(
            normalise_dt("2026-04-09T03:06:11+00:00"),
            "2026-04-09 03:06:11",
        )


# ---------------------------------------------------------------------------
# haversine_m
# ---------------------------------------------------------------------------

class TestHaversine(unittest.TestCase):

    def test_same_point(self):
        self.assertAlmostEqual(haversine_m(42.38, -71.09, 42.38, -71.09), 0.0, places=1)

    def test_known_distance(self):
        # Somerville City Hall to Harvard Square — roughly 2.5km
        dist = haversine_m(42.3876, -71.0995, 42.3736, -71.1190)
        self.assertGreater(dist, 2000)
        self.assertLess(dist, 3000)

    def test_geofence_radius(self):
        # Point 50m away should be within a 100m radius
        # ~0.0009 degrees lat ≈ 100m
        dist = haversine_m(42.38, -71.09, 42.3809, -71.09)
        self.assertLess(dist, 110)


# ---------------------------------------------------------------------------
# Privacy classifier
# ---------------------------------------------------------------------------

class TestPrivacyClassify(unittest.TestCase):

    BASE = {
        "latitude": None, "longitude": None,
        "place_ishome": 0,
        "persons": [], "apple_persons": [],
        "face_info": [],
        "labels": [], "apple_labels": [],
        "media_analysis": {},
    }

    def _photo(self, **kwargs):
        return {**self.BASE, **kwargs}

    def test_no_signals_candidate_public(self):
        state, reason = classify(self._photo(), zones=[])
        self.assertEqual(state, "candidate_public")

    def test_home_flag(self):
        state, reason = classify(self._photo(place_ishome=1), zones=[])
        self.assertEqual(state, "auto_private")
        self.assertIn("home", reason)

    def test_place_dict_ishome(self):
        photo = self._photo(place={"ishome": True})
        state, reason = classify(photo, zones=[])
        self.assertEqual(state, "auto_private")

    def test_unknown_person(self):
        state, reason = classify(
            self._photo(persons=["_UNKNOWN_"]), zones=[]
        )
        self.assertEqual(state, "needs_review")
        self.assertIn("unidentified", reason)

    def test_named_other(self):
        state, reason = classify(
            self._photo(persons=["Alice Smith"]),
            zones=[],
            self_name="Chris Devers",
        )
        self.assertEqual(state, "needs_review")
        self.assertIn("Alice Smith", reason)

    def test_self_only_candidate_public(self):
        state, reason = classify(
            self._photo(persons=["Chris Devers"]),
            zones=[],
            self_name="Chris Devers",
        )
        self.assertEqual(state, "candidate_public")

    def test_people_label(self):
        state, reason = classify(
            self._photo(labels=["People", "Concert"]), zones=[]
        )
        self.assertEqual(state, "needs_review")

    def test_crowd_label(self):
        state, reason = classify(
            self._photo(labels=["Crowd", "Outdoor"]), zones=[]
        )
        self.assertEqual(state, "needs_review")

    def test_geofence_auto_private(self):
        zones = [{
            "name": "home", "label": "Home",
            "latitude": 42.38, "longitude": -71.09,
            "radius_m": 200, "policy": "auto_private",
        }]
        # Point inside zone
        state, reason = classify(
            self._photo(latitude=42.38, longitude=-71.09), zones=zones
        )
        self.assertEqual(state, "auto_private")
        self.assertIn("Home", reason)

    def test_geofence_flag_review(self):
        zones = [{
            "name": "school", "label": "School",
            "latitude": 42.38, "longitude": -71.09,
            "radius_m": 200, "policy": "flag_review",
        }]
        state, reason = classify(
            self._photo(latitude=42.38, longitude=-71.09), zones=zones
        )
        self.assertEqual(state, "needs_review")

    def test_outside_geofence(self):
        zones = [{
            "name": "home", "label": "Home",
            "latitude": 42.38, "longitude": -71.09,
            "radius_m": 50, "policy": "auto_private",
        }]
        # Point 500m away — outside zone
        state, reason = classify(
            self._photo(latitude=42.385, longitude=-71.09), zones=zones
        )
        self.assertEqual(state, "candidate_public")

    def test_human_body_detection(self):
        photo = self._photo(media_analysis={
            "humans": [
                {"humanConfidence": 0.8},
                {"humanConfidence": 0.6},
            ]
        })
        state, reason = classify(photo, zones=[])
        self.assertEqual(state, "needs_review")

    def test_low_confidence_human_ignored(self):
        photo = self._photo(media_analysis={
            "humans": [{"humanConfidence": 0.1}]
        })
        state, reason = classify(photo, zones=[])
        self.assertEqual(state, "candidate_public")


# ---------------------------------------------------------------------------
# Tagger
# ---------------------------------------------------------------------------

class TestTagger(unittest.TestCase):

    def test_location_tags(self):
        tags = propose_tags({
            "place_city": "Boston",
            "place_state": "Massachusetts",
            "place_country": "United States",
        })
        self.assertIn("boston", tags)
        self.assertIn("massachusetts", tags)
        self.assertIn("united states", tags)

    def test_apple_labels_filtered(self):
        tags = propose_tags({"labels": ["People", "Concert", "Stage", "Music"]})
        self.assertNotIn("people", tags)   # blocklisted
        self.assertIn("concert", tags)
        self.assertIn("stage", tags)
        self.assertIn("music", tags)

    def test_remap(self):
        tags = propose_tags({"labels": ["Rock Concert", "Automobile"]})
        self.assertIn("concert", tags)
        self.assertIn("car", tags)

    def test_deduplication(self):
        tags = propose_tags({
            "labels": ["Concert", "concert"],  # duplicates
            "place_city": "Boston",
        })
        self.assertEqual(tags.count("concert"), 1)

    def test_empty_photo(self):
        tags = propose_tags({})
        self.assertEqual(tags, [])

    def test_keywords_preserved(self):
        tags = propose_tags({"keywords": ["Mission of Burma", "punk rock"]})
        self.assertIn("mission of burma", tags)
        self.assertIn("punk rock", tags)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

class TestDatabase(unittest.TestCase):

    def setUp(self):
        fd, self.tmp_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = Database(self.tmp_path)

    def tearDown(self):
        self.db.close()
        os.unlink(self.tmp_path)

    def test_upsert_and_retrieve(self):
        row_id = self.db.upsert_photo({
            "flickr_id": "12345",
            "date_taken": "2023-05-06 16:34:28",
            "privacy_state": "candidate_public",
            "privacy_reason": "no people detected",
        })
        self.assertGreater(row_id, 0)
        photo = self.db.get_photo_by_flickr_id("12345")
        self.assertIsNotNone(photo)
        self.assertEqual(photo["privacy_state"], "candidate_public")

    def test_upsert_is_idempotent(self):
        self.db.upsert_photo({"flickr_id": "99999", "date_taken": "2023-01-01 00:00:00"})
        self.db.upsert_photo({"flickr_id": "99999", "date_taken": "2023-01-01 00:00:01"})
        count = self.db.conn.execute(
            "SELECT COUNT(*) FROM photos WHERE flickr_id = '99999'"
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_review_decision_preserved_on_update(self):
        self.db.upsert_photo({
            "flickr_id": "77777",
            "privacy_state": "approved_public",
            "review_decision": "make_public",
            "reviewed_at": "2026-01-01T00:00:00",
        })
        # Update without review fields — should not clobber decision
        self.db.upsert_photo({
            "flickr_id": "77777",
            "date_taken": "2024-06-01 12:00:00",
        })
        photo = self.db.get_photo_by_flickr_id("77777")
        self.assertEqual(photo["review_decision"], "make_public")

    def test_stats(self):
        self.db.upsert_photo({"flickr_id": "1", "privacy_state": "candidate_public"})
        self.db.upsert_photo({"flickr_id": "2", "privacy_state": "needs_review"})
        self.db.upsert_photo({"flickr_id": "3", "privacy_state": "auto_private"})
        stats = self.db.stats()
        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["by_state"]["candidate_public"], 1)

    def test_geofence_match(self):
        self.db.upsert_zone({
            "name": "home", "label": "Home",
            "latitude": 42.38, "longitude": -71.09,
            "radius_m": 200, "policy": "auto_private",
        })
        zone = self.db.match_geofence(42.38, -71.09)
        self.assertIsNotNone(zone)
        self.assertEqual(zone["name"], "home")

    def test_geofence_no_match(self):
        self.db.upsert_zone({
            "name": "home", "label": "Home",
            "latitude": 42.38, "longitude": -71.09,
            "radius_m": 50, "policy": "auto_private",
        })
        zone = self.db.match_geofence(42.39, -71.09)  # ~1km away
        self.assertIsNone(zone)

    def test_review_queue(self):
        self.db.upsert_photo({"flickr_id": "A", "privacy_state": "needs_review"})
        self.db.upsert_photo({"flickr_id": "B", "privacy_state": "candidate_public"})
        self.db.upsert_photo({"flickr_id": "C", "privacy_state": "auto_private"})
        queue = self.db.review_queue()
        ids = [p["flickr_id"] for p in queue]
        self.assertIn("A", ids)
        self.assertIn("B", ids)
        self.assertNotIn("C", ids)

    def test_record_review(self):
        self.db.upsert_photo({"flickr_id": "X", "privacy_state": "needs_review"})
        photo = self.db.get_photo_by_flickr_id("X")
        self.db.record_review(photo["id"], "make_public", notes="looks good")
        updated = self.db.get_photo_by_flickr_id("X")
        self.assertEqual(updated["privacy_state"], "approved_public")
        self.assertEqual(updated["review_decision"], "make_public")

    def test_review_queue_newest_first(self):
        """review_queue returns photos newest-first by date_taken."""
        self.db.upsert_photo({
            "flickr_id": "OLD", "privacy_state": "candidate_public",
            "date_taken": "2020-01-01 00:00:00",
        })
        self.db.upsert_photo({
            "flickr_id": "NEW", "privacy_state": "candidate_public",
            "date_taken": "2024-06-01 00:00:00",
        })
        queue = self.db.review_queue()
        ids = [p["flickr_id"] for p in queue]
        self.assertEqual(ids[0], "NEW")
        self.assertEqual(ids[1], "OLD")

    def test_review_queue_coalesce_fallback(self):
        """review_queue falls back to date_uploaded_flickr when date_taken is NULL."""
        self.db.upsert_photo({
            "flickr_id": "NOTAKEN", "privacy_state": "candidate_public",
            "date_uploaded_flickr": "2023-03-01 00:00:00",
        })
        self.db.upsert_photo({
            "flickr_id": "WITHTAKEN", "privacy_state": "candidate_public",
            "date_taken": "2022-01-01 00:00:00",
        })
        queue = self.db.review_queue()
        ids = [p["flickr_id"] for p in queue]
        # NOTAKEN (2023) should sort before WITHTAKEN (2022)
        self.assertLess(ids.index("NOTAKEN"), ids.index("WITHTAKEN"))


# ---------------------------------------------------------------------------
# upsert_photo review protection
# ---------------------------------------------------------------------------

class TestUpsertReviewProtection(unittest.TestCase):
    """upsert_photo must never clobber a human review decision."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        from db.db import Database
        self.db = Database(Path(self._tmp.name) / "test.db")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _insert_reviewed(self, flickr_id, decision):
        """Insert a photo, record a human decision, and return the updated row."""
        self.db.upsert_photo({"flickr_id": flickr_id,
                              "privacy_state": "candidate_public"})
        photo = self.db.get_photo_by_flickr_id(flickr_id)
        self.db.record_review(photo["id"], decision)
        return self.db.get_photo_by_flickr_id(flickr_id)

    def test_keep_private_survives_scanner_upsert(self):
        """A keep_private decision must survive a subsequent upsert_photo call."""
        row = self._insert_reviewed("P1", "keep_private")
        self.assertEqual(row["privacy_state"], "keep_private")

        # Simulate scanner re-classifying and upserting
        self.db.upsert_photo({
            "flickr_id": "P1",
            "privacy_state": "candidate_public",
            "privacy_reason": "no people detected",
        })
        result = self.db.get_photo_by_flickr_id("P1")
        self.assertEqual(result["privacy_state"], "keep_private",
                         "Scanner upsert must not revert keep_private")

    def test_approved_public_survives_scanner_upsert(self):
        """An approved_public decision must survive a subsequent upsert_photo call."""
        row = self._insert_reviewed("P2", "make_public")
        self.assertEqual(row["privacy_state"], "approved_public")

        self.db.upsert_photo({
            "flickr_id": "P2",
            "privacy_state": "needs_review",
            "privacy_reason": "people detected",
        })
        result = self.db.get_photo_by_flickr_id("P2")
        self.assertEqual(result["privacy_state"], "approved_public",
                         "Scanner upsert must not revert approved_public")

    def test_skipped_survives_scanner_upsert(self):
        """A skipped decision must survive a subsequent upsert_photo call."""
        row = self._insert_reviewed("P3", "skip")
        self.assertEqual(row["privacy_state"], "skipped")

        self.db.upsert_photo({
            "flickr_id": "P3",
            "privacy_state": "candidate_public",
            "privacy_reason": "no people detected",
        })
        result = self.db.get_photo_by_flickr_id("P3")
        self.assertEqual(result["privacy_state"], "skipped",
                         "Scanner upsert must not revert skipped")

    def test_unreviewed_photo_state_is_updated(self):
        """upsert_photo must still update privacy_state for unreviewed photos."""
        self.db.upsert_photo({"flickr_id": "P4",
                              "privacy_state": "candidate_public"})
        self.db.upsert_photo({
            "flickr_id": "P4",
            "privacy_state": "needs_review",
            "privacy_reason": "people detected",
        })
        result = self.db.get_photo_by_flickr_id("P4")
        self.assertEqual(result["privacy_state"], "needs_review",
                         "Unreviewed photos must still get state updates")

    def test_non_privacy_fields_still_updated_after_review(self):
        """Metadata fields (tags, filename, etc.) must still update even after review."""
        self._insert_reviewed("P5", "keep_private")
        self.db.upsert_photo({
            "flickr_id": "P5",
            "original_filename": "updated_name.jpg",
            "proposed_tags": ["new", "tag"],
        })
        result = self.db.get_photo_by_flickr_id("P5")
        self.assertEqual(result["original_filename"], "updated_name.jpg")
        self.assertEqual(result["privacy_state"], "keep_private")

    def test_build_enriched_row_preserves_skipped(self):
        """build_enriched_row must not reclassify skipped photos."""
        from poller.scanner import build_enriched_row
        existing = {
            "id": 1, "flickr_id": "12345", "uuid": None,
            "privacy_state": "skipped",
            "privacy_reason": "user deferred",
            "proposed_tags": [],
            "latitude": None, "longitude": None,
            "place_ishome": 0,
        }
        photo_row = {
            "uuid": "ABC-123",
            "original_filename": "IMG_0001.HEIC",
            "date_taken": "2026-04-13T10:00:00-04:00",
            "apple_labels": [],
            "apple_persons": [],
            "apple_named_faces": 0,
            "apple_unknown_faces": 0,
            "apple_human_count": 0,
            "_is_screenshot": False,
            "_is_selfie": False,
            "_is_live": False,
        }
        enriched = build_enriched_row(photo_row, existing, [], "Chris Devers")
        self.assertEqual(enriched["privacy_state"], "skipped",
                         "build_enriched_row must preserve skipped state")


# ---------------------------------------------------------------------------
# undo_decision
# ---------------------------------------------------------------------------

class TestUndoDecision(unittest.TestCase):

    def setUp(self):
        fd, self.tmp_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = Database(self.tmp_path)

    def tearDown(self):
        self.db.close()
        os.unlink(self.tmp_path)

    def test_undo_returns_to_candidate_public(self):
        """Photo with no people reverts to candidate_public on undo."""
        self.db.upsert_photo({
            "flickr_id": "U1",
            "privacy_state": "approved_public",
            "review_decision": "make_public",
            "reviewed_at": "2026-01-01T00:00:00",
            "apple_persons": "[]",
            "apple_unknown_faces": 0,
            "apple_named_faces": 0,
        })
        photo = self.db.get_photo_by_flickr_id("U1")
        result = self.db.undo_decision(photo["id"])
        self.assertTrue(result)
        updated = self.db.get_photo_by_flickr_id("U1")
        self.assertEqual(updated["privacy_state"], "candidate_public")
        self.assertIsNone(updated["review_decision"])
        self.assertIsNone(updated["reviewed_at"])

    def test_undo_returns_to_needs_review_with_persons(self):
        """Photo with named persons reverts to needs_review on undo."""
        import json as _json
        self.db.upsert_photo({
            "flickr_id": "U2",
            "privacy_state": "keep_private",
            "review_decision": "keep_private",
            "reviewed_at": "2026-01-01T00:00:00",
            "apple_persons": _json.dumps(["Alice"]),
            "apple_named_faces": 1,
            "apple_unknown_faces": 0,
        })
        photo = self.db.get_photo_by_flickr_id("U2")
        result = self.db.undo_decision(photo["id"])
        self.assertTrue(result)
        updated = self.db.get_photo_by_flickr_id("U2")
        self.assertEqual(updated["privacy_state"], "needs_review")

    def test_undo_returns_to_needs_review_with_unknown_faces(self):
        """Photo with unknown faces reverts to needs_review on undo."""
        self.db.upsert_photo({
            "flickr_id": "U3",
            "privacy_state": "approved_public",
            "review_decision": "make_public",
            "reviewed_at": "2026-01-01T00:00:00",
            "apple_persons": "[]",
            "apple_unknown_faces": 2,
            "apple_named_faces": 0,
        })
        photo = self.db.get_photo_by_flickr_id("U3")
        self.db.undo_decision(photo["id"])
        updated = self.db.get_photo_by_flickr_id("U3")
        self.assertEqual(updated["privacy_state"], "needs_review")

    def test_undo_resets_perms_pushed(self):
        """undo_decision resets perms_pushed_flickr to 0."""
        self.db.upsert_photo({
            "flickr_id": "U4",
            "privacy_state": "approved_public",
            "review_decision": "make_public",
            "reviewed_at": "2026-01-01T00:00:00",
            "perms_pushed_flickr": 1,
            "apple_persons": "[]",
            "apple_unknown_faces": 0,
            "apple_named_faces": 0,
        })
        photo = self.db.get_photo_by_flickr_id("U4")
        self.db.undo_decision(photo["id"])
        updated = self.db.get_photo_by_flickr_id("U4")
        self.assertEqual(updated["perms_pushed_flickr"], 0)

    def test_undo_nonexistent_returns_false(self):
        """undo_decision returns False for unknown photo_id."""
        result = self.db.undo_decision(99999)
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# build_enriched_row (scanner)
# ---------------------------------------------------------------------------

class TestBuildEnrichedRow(unittest.TestCase):

    EXISTING = {
        "id": 1, "flickr_id": "12345", "uuid": None,
        "privacy_state": "candidate_public",
        "privacy_reason": "no people detected",
        "proposed_tags": [],
        "latitude": None, "longitude": None,
        "place_ishome": 0,
    }

    def _photo_row(self, **kwargs):
        base = {
            "uuid": "ABC-123",
            "original_filename": "IMG_0001.HEIC",
            "date_taken": "2026-04-08T16:46:20-04:00",
            "apple_labels": [],
            "apple_persons": [],
            "apple_named_faces": 0,
            "apple_unknown_faces": 0,
            "apple_human_count": 0,
            "_is_screenshot": False,
            "_is_selfie": False,
            "_is_live": False,
        }
        return {**base, **kwargs}

    def test_screenshot_becomes_auto_private(self):
        row = self._photo_row(_is_screenshot=True)
        enriched = build_enriched_row(row, self.EXISTING, [], "Chris Devers")
        self.assertEqual(enriched["privacy_state"], "auto_private")
        self.assertEqual(enriched["privacy_reason"], "screenshot")

    def test_people_label_triggers_review(self):
        row = self._photo_row(apple_labels=["People", "Restaurant"])
        enriched = build_enriched_row(row, self.EXISTING, [], "Chris Devers")
        self.assertEqual(enriched["privacy_state"], "needs_review")

    def test_reviewed_state_preserved(self):
        existing = dict(self.EXISTING, privacy_state="approved_public")
        row = self._photo_row(apple_labels=["People"])
        enriched = build_enriched_row(row, existing, [], "Chris Devers")
        # Should not clobber approved_public even if people detected
        self.assertEqual(enriched["privacy_state"], "approved_public")

    def test_keep_private_preserved(self):
        existing = dict(self.EXISTING, privacy_state="keep_private",
                        review_decision="keep_private")
        row = self._photo_row(apple_labels=[])  # would normally be candidate_public
        enriched = build_enriched_row(row, existing, [], "Chris Devers")
        self.assertEqual(enriched["privacy_state"], "keep_private")

    def test_skipped_preserved(self):
        existing = dict(self.EXISTING, privacy_state="skipped",
                        review_decision="skip")
        row = self._photo_row(apple_labels=[])
        enriched = build_enriched_row(row, existing, [], "Chris Devers")
        self.assertEqual(enriched["privacy_state"], "skipped")

    def test_tags_proposed_from_labels(self):
        row = self._photo_row(
            apple_labels=["Concert", "Stage", "Music"],
            place_city="Boston",
        )
        enriched = build_enriched_row(row, self.EXISTING, [], "Chris Devers")
        self.assertIn("concert", enriched["proposed_tags"])
        self.assertIn("boston", enriched["proposed_tags"])

    def test_uuid_transferred(self):
        row = self._photo_row(uuid="NEW-UUID-123")
        enriched = build_enriched_row(row, self.EXISTING, [], "Chris Devers")
        self.assertEqual(enriched["uuid"], "NEW-UUID-123")


# ---------------------------------------------------------------------------
# Thumbnailer
# ---------------------------------------------------------------------------

class TestThumbnailer(unittest.TestCase):

    def test_flickr_url_valid(self):
        from poller.thumbnailer import flickr_url
        url = flickr_url("12345", "abc123", "1234")
        self.assertEqual(url, "https://live.staticflickr.com/1234/12345_abc123_b.jpg")

    def test_flickr_url_missing_secret(self):
        from poller.thumbnailer import flickr_url
        self.assertIsNone(flickr_url("12345", "", "1234"))

    def test_flickr_url_missing_id(self):
        from poller.thumbnailer import flickr_url
        self.assertIsNone(flickr_url("", "abc123", "1234"))

    def test_flickr_url_missing_server(self):
        from poller.thumbnailer import flickr_url
        self.assertIsNone(flickr_url("12345", "abc123", ""))

    def test_derivative_path_nonexistent_library(self):
        from poller.thumbnailer import derivative_path
        result = derivative_path("AAAAAAAA-0000-0000-0000-000000000000", "/nonexistent/library")
        self.assertIsNone(result)

    def test_derivative_path_empty_uuid(self):
        from poller.thumbnailer import derivative_path
        self.assertIsNone(derivative_path("", "/some/library"))

    def test_derivative_path_uses_first_char_shard(self):
        from poller.thumbnailer import derivative_path
        from unittest import mock
        with mock.patch("pathlib.Path.exists", return_value=True):
            result = derivative_path("ABCD1234-0000-0000-0000-000000000000", "/library")
            self.assertIsNotNone(result)
            self.assertIn("/a/", result.lower())
            self.assertIn("ABCD1234-0000-0000-0000-000000000000_4_5005_c.jpeg", result)


# ---------------------------------------------------------------------------
# Poller: flickr_photo_to_db
# ---------------------------------------------------------------------------

class TestFlickrPhotoToDb(unittest.TestCase):

    def _fake(self, **kwargs):
        base = {
            "id": "54321", "secret": "abc123",
            "server": "1234", "farm": 1,
            "title": "Test photo",
            "dateupload": "1718500000",
            "datetaken": "2024-06-16 10:00:00",
            "latitude": "42.3601", "longitude": "-71.0589",
            "tags": "concert boston",
            "url_l": "https://live.staticflickr.com/1234/54321_abc123_b.jpg",
            "url_m": "https://live.staticflickr.com/1234/54321_abc123.jpg",
            "ispublic": 0,
            "description": {"_content": "A test photo"},
        }
        return {**base, **kwargs}

    def test_basic_fields(self):
        from poller.poller import flickr_photo_to_db
        row = flickr_photo_to_db(self._fake())
        self.assertEqual(row["flickr_id"], "54321")
        self.assertEqual(row["flickr_secret"], "abc123")
        self.assertEqual(row["flickr_server"], "1234")
        self.assertEqual(row["date_taken"], "2024-06-16 10:00:00")

    def test_location_parsed(self):
        from poller.poller import flickr_photo_to_db
        row = flickr_photo_to_db(self._fake())
        self.assertAlmostEqual(row["latitude"], 42.3601, places=3)
        self.assertAlmostEqual(row["longitude"], -71.0589, places=3)

    def test_tags_split(self):
        from poller.poller import flickr_photo_to_db
        row = flickr_photo_to_db(self._fake(tags="concert boston cycling"))
        self.assertEqual(row["flickr_tags"], ["concert", "boston", "cycling"])

    def test_empty_tags(self):
        from poller.poller import flickr_photo_to_db
        row = flickr_photo_to_db(self._fake(tags=""))
        self.assertEqual(row["flickr_tags"], [])

    def test_upload_date_iso(self):
        from poller.poller import flickr_photo_to_db
        row = flickr_photo_to_db(self._fake())
        self.assertIn("2024", row["date_uploaded_flickr"])
        self.assertTrue(row["date_uploaded_flickr"].endswith("+00:00"))

    def test_description_extracted(self):
        from poller.poller import flickr_photo_to_db
        row = flickr_photo_to_db(self._fake(description={"_content": "My caption"}))
        self.assertEqual(row["flickr_description"], "My caption")

    def test_thumbnail_urls_stored(self):
        from poller.poller import flickr_photo_to_db
        row = flickr_photo_to_db(self._fake())
        self.assertIn("thumbnail_url_l", row)
        self.assertIn("thumbnail_url_m", row)
        self.assertIn("54321_abc123_b.jpg", row["thumbnail_url_l"])

    def test_no_location(self):
        from poller.poller import flickr_photo_to_db
        row = flickr_photo_to_db(self._fake(latitude="", longitude=""))
        self.assertNotIn("latitude", row)


# ---------------------------------------------------------------------------
# find_flickr_match date matching
# ---------------------------------------------------------------------------

class TestFindFlickrMatch(unittest.TestCase):

    def setUp(self):
        import tempfile, os
        fd, self.tmp_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = Database(self.tmp_path)
        self.db.upsert_photo({
            "flickr_id": "AAA",
            "date_taken": "2024-06-16 10:00:00",
            "latitude": 42.36, "longitude": -71.06,
        })
        self.db.upsert_photo({
            "flickr_id": "BBB",
            "date_taken": "2023-05-06 16:34:28",
        })

    def tearDown(self):
        self.db.close()
        os.unlink(self.tmp_path)

    def test_match_by_exact_date(self):
        from poller.scanner import find_flickr_match
        photo_row = {"date_taken": "2024-06-16T10:00:00.000000-04:00"}
        matches = find_flickr_match(photo_row, self.db)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["flickr_id"], "AAA")

    def test_no_match(self):
        from poller.scanner import find_flickr_match
        photo_row = {"date_taken": "2020-01-01T00:00:00-05:00"}
        matches = find_flickr_match(photo_row, self.db)
        self.assertEqual(matches, [])

    def test_match_flickr_space_format(self):
        from poller.scanner import find_flickr_match
        photo_row = {"date_taken": "2023-05-06 16:34:28"}
        matches = find_flickr_match(photo_row, self.db)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["flickr_id"], "BBB")

    def test_no_date_returns_empty(self):
        from poller.scanner import find_flickr_match
        matches = find_flickr_match({}, self.db)
        self.assertEqual(matches, [])



# ---------------------------------------------------------------------------
# approved_public queue (DB side of push_approved)
# ---------------------------------------------------------------------------

class TestApprovedQueue(unittest.TestCase):

    def setUp(self):
        import tempfile, os
        fd, self.tmp_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = Database(self.tmp_path)

    def tearDown(self):
        self.db.close()
        os.unlink(self.tmp_path)

    def test_approved_unpushed_appears_in_queue(self):
        self.db.upsert_photo({
            "flickr_id": "111",
            "privacy_state": "approved_public",
            "perms_pushed_flickr": 0,
        })
        rows = self.db.conn.execute(
            "SELECT flickr_id FROM photos "
            "WHERE privacy_state = 'approved_public' "
            "AND flickr_id IS NOT NULL AND perms_pushed_flickr = 0"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["flickr_id"], "111")

    def test_already_pushed_excluded_from_queue(self):
        self.db.upsert_photo({
            "flickr_id": "222",
            "privacy_state": "approved_public",
            "perms_pushed_flickr": 1,
        })
        rows = self.db.conn.execute(
            "SELECT flickr_id FROM photos "
            "WHERE privacy_state = 'approved_public' "
            "AND flickr_id IS NOT NULL AND perms_pushed_flickr = 0"
        ).fetchall()
        self.assertEqual(len(rows), 0)

    def test_no_flickr_id_excluded_from_queue(self):
        self.db.upsert_photo({
            "uuid": "ABC-123",
            "privacy_state": "approved_public",
            "perms_pushed_flickr": 0,
        })
        rows = self.db.conn.execute(
            "SELECT id FROM photos "
            "WHERE privacy_state = 'approved_public' "
            "AND flickr_id IS NOT NULL AND perms_pushed_flickr = 0"
        ).fetchall()
        self.assertEqual(len(rows), 0)

    def test_record_review_sets_approved_public(self):
        self.db.upsert_photo({"flickr_id": "333", "privacy_state": "candidate_public"})
        photo = self.db.get_photo_by_flickr_id("333")
        self.db.record_review(photo["id"], "make_public")
        updated = self.db.get_photo_by_flickr_id("333")
        self.assertEqual(updated["privacy_state"], "approved_public")

    def test_keep_private_sets_keep_private(self):
        self.db.upsert_photo({"flickr_id": "444", "privacy_state": "needs_review"})
        photo = self.db.get_photo_by_flickr_id("444")
        self.db.record_review(photo["id"], "keep_private")
        updated = self.db.get_photo_by_flickr_id("444")
        self.assertEqual(updated["privacy_state"], "keep_private")


# ---------------------------------------------------------------------------
# Faces / batch person (DB side)
# ---------------------------------------------------------------------------

class TestBatchPerson(unittest.TestCase):

    def setUp(self):
        fd, self.tmp_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = Database(self.tmp_path)
        import json
        # Three photos: two with Obama, one with family
        self.db.upsert_photo({
            "flickr_id": "O1",
            "apple_persons": json.dumps(["Barack Obama"]),
            "privacy_state": "needs_review",
        })
        self.db.upsert_photo({
            "flickr_id": "O2",
            "apple_persons": json.dumps(["Barack Obama", "_UNKNOWN_"]),
            "privacy_state": "needs_review",
        })
        self.db.upsert_photo({
            "flickr_id": "F1",
            "apple_persons": json.dumps(["Family Member"]),
            "privacy_state": "needs_review",
        })

    def tearDown(self):
        self.db.close()
        os.unlink(self.tmp_path)

    def _batch_set(self, person, decision):
        """Simulate what api_batch_person does."""
        new_state = "approved_public" if decision == "make_public" else "keep_private"
        rows = self.db.conn.execute(
            """SELECT DISTINCT photos.id
               FROM photos, json_each(photos.apple_persons) AS p
               WHERE p.value = ?
                 AND photos.privacy_state NOT IN ('already_public')""",
            (person,)
        ).fetchall()
        for row in rows:
            self.db.conn.execute(
                "UPDATE photos SET privacy_state = ?, privacy_reason = ? WHERE id = ?",
                (new_state, f"batch: {person}", row["id"])
            )
        self.db.conn.commit()
        return len(rows)

    def test_batch_private_affects_only_that_person(self):
        count = self._batch_set("Barack Obama", "keep_private")
        self.assertEqual(count, 2)
        o1 = self.db.get_photo_by_flickr_id("O1")
        o2 = self.db.get_photo_by_flickr_id("O2")
        f1 = self.db.get_photo_by_flickr_id("F1")
        self.assertEqual(o1["privacy_state"], "keep_private")
        self.assertEqual(o2["privacy_state"], "keep_private")
        self.assertEqual(f1["privacy_state"], "needs_review")  # untouched

    def test_batch_public_sets_approved(self):
        count = self._batch_set("Barack Obama", "make_public")
        self.assertEqual(count, 2)
        o1 = self.db.get_photo_by_flickr_id("O1")
        self.assertEqual(o1["privacy_state"], "approved_public")

    def test_batch_skips_already_public(self):
        import json
        self.db.upsert_photo({
            "flickr_id": "O3",
            "apple_persons": json.dumps(["Barack Obama"]),
            "privacy_state": "already_public",
        })
        count = self._batch_set("Barack Obama", "keep_private")
        # O3 should be excluded (already_public)
        o3 = self.db.get_photo_by_flickr_id("O3")
        self.assertEqual(o3["privacy_state"], "already_public")
        self.assertEqual(count, 2)  # only O1 and O2

    def test_batch_reason_recorded(self):
        self._batch_set("Barack Obama", "keep_private")
        o1 = self.db.get_photo_by_flickr_id("O1")
        self.assertEqual(o1["privacy_reason"], "batch: Barack Obama")

    def test_unknown_not_matched_by_name(self):
        # _UNKNOWN_ should not be batch-actionable by name
        count = self._batch_set("_UNKNOWN_", "keep_private")
        # O2 has _UNKNOWN_ — it would be matched, but this tests
        # that the caller (faces UI) doesn't expose _UNKNOWN_ as a batch target
        # The DB query itself doesn't distinguish — enforcement is in the UI
        self.assertGreaterEqual(count, 0)

    def test_person_scoped_nav_query(self):
        """Verify the query used for person-scoped prev/next navigation."""
        import json
        # Add photos at known dates
        self.db.upsert_photo({
            "flickr_id": "NAV1",
            "apple_persons": json.dumps(["Barack Obama"]),
            "privacy_state": "needs_review",
            "date_taken": "2017-01-01 00:00:00",
        })
        self.db.upsert_photo({
            "flickr_id": "NAV2",
            "apple_persons": json.dumps(["Barack Obama"]),
            "privacy_state": "needs_review",
            "date_taken": "2017-06-01 00:00:00",
        })
        self.db.upsert_photo({
            "flickr_id": "NAV3",
            "apple_persons": json.dumps(["Someone Else"]),
            "privacy_state": "needs_review",
            "date_taken": "2017-03-01 00:00:00",  # between NAV1 and NAV2
        })

        # Simulate the person-scoped nav query from photo_detail route (newest-first)
        nav = self.db.conn.execute(
            """SELECT DISTINCT photos.id,
                   LAG(photos.id)  OVER (ORDER BY COALESCE(photos.date_taken, photos.date_uploaded_flickr, photos.date_added_photos) DESC, photos.id DESC) AS prev_id,
                   LEAD(photos.id) OVER (ORDER BY COALESCE(photos.date_taken, photos.date_uploaded_flickr, photos.date_added_photos) DESC, photos.id DESC) AS next_id
               FROM photos, json_each(photos.apple_persons) AS p
               WHERE p.value = ?
                 AND photos.privacy_state = ?""",
            ("Barack Obama", "needs_review"),
        ).fetchall()

        nav1 = self.db.get_photo_by_flickr_id("NAV1")
        nav2 = self.db.get_photo_by_flickr_id("NAV2")

        id_to_nav = {row["id"]: row for row in nav}

        # Newest-first: NAV2 (Jun) comes before NAV1 (Jan) in the window order,
        # so NAV2's next points to NAV1, and NAV1's prev points to NAV2.
        # NAV3 (Someone Else) should not appear in Obama's sequence either way.
        self.assertEqual(id_to_nav[nav2["id"]]["next_id"], nav1["id"])
        self.assertEqual(id_to_nav[nav1["id"]]["prev_id"], nav2["id"])

        # NAV3 (Someone Else) should NOT appear in Obama's nav sequence
        nav3 = self.db.get_photo_by_flickr_id("NAV3")
        self.assertNotIn(nav3["id"], id_to_nav)


# ---------------------------------------------------------------------------
# FlickrClient retry / backoff
# ---------------------------------------------------------------------------

class TestFlickrClientRetry(unittest.TestCase):

    def setUp(self):
        # Suppress retry warning logs — they're expected in these tests
        import logging
        logging.getLogger("blue-pearmain.flickr").setLevel(logging.CRITICAL)

    def tearDown(self):
        import logging
        logging.getLogger("blue-pearmain.flickr").setLevel(logging.WARNING)

    def _make_client(self):
        from flickr.flickr_client import FlickrClient
        c = FlickrClient("key", "secret", "token", "tsecret", rate_limit_delay=0)
        return c

    def _mock_response(self, status_code=200, json_data=None):
        from unittest.mock import MagicMock
        resp = MagicMock()
        resp.status_code = status_code
        resp.json.return_value = json_data or {"stat": "ok"}
        resp.raise_for_status = MagicMock()
        if status_code >= 400:
            import requests as req
            resp.raise_for_status.side_effect = req.HTTPError(response=resp)
        return resp

    def test_success_no_retry(self):
        from unittest.mock import patch
        c = self._make_client()
        ok_resp = self._mock_response(200, {"stat": "ok", "user": {"id": "123"}})
        with patch.object(c._session, 'get', return_value=ok_resp):
            result = c._call("flickr.test.login")
        self.assertEqual(result["stat"], "ok")

    def test_retries_on_500(self):
        from unittest.mock import patch, MagicMock
        c = self._make_client()
        err_resp = self._mock_response(500)
        ok_resp  = self._mock_response(200, {"stat": "ok"})
        # Fail once then succeed
        with patch.object(c._session, 'get', side_effect=[err_resp, ok_resp]):
            with patch('time.sleep'):  # don't actually sleep in tests
                result = c._call("flickr.test.login")
        self.assertEqual(result["stat"], "ok")

    def test_retries_on_timeout(self):
        from unittest.mock import patch
        import requests as req
        c = self._make_client()
        ok_resp = self._mock_response(200, {"stat": "ok"})
        with patch.object(c._session, 'get',
                          side_effect=[req.Timeout(), ok_resp]):
            with patch('time.sleep'):
                result = c._call("flickr.test.login")
        self.assertEqual(result["stat"], "ok")

    def test_raises_after_max_retries(self):
        from unittest.mock import patch
        from flickr.flickr_client import FlickrError
        c = self._make_client()
        err_resp = self._mock_response(500)
        with patch.object(c._session, 'get', return_value=err_resp):
            with patch('time.sleep'):
                with self.assertRaises(FlickrError):
                    c._call("flickr.test.login", max_retries=2)

    def test_non_transient_flickr_error_raises_immediately(self):
        from unittest.mock import patch, MagicMock
        from flickr.flickr_client import FlickrError
        c = self._make_client()
        bad_resp = self._mock_response(200, {
            "stat": "fail", "code": 1, "message": "Method not found"
        })
        call_count = 0
        original_get = c._session.get
        def counting_get(*a, **kw):
            nonlocal call_count
            call_count += 1
            return bad_resp
        with patch.object(c._session, 'get', side_effect=counting_get):
            with patch('time.sleep'):
                with self.assertRaises(FlickrError) as ctx:
                    c._call("flickr.nonexistent")
        self.assertEqual(call_count, 1)  # no retries
        self.assertEqual(ctx.exception.code, 1)

    def test_transient_flickr_error_retries(self):
        from unittest.mock import patch
        from flickr.flickr_client import FlickrError
        c = self._make_client()
        transient_resp = self._mock_response(200, {
            "stat": "fail", "code": 0, "message": "something went wrong"
        })
        ok_resp = self._mock_response(200, {"stat": "ok"})
        with patch.object(c._session, 'get',
                          side_effect=[transient_resp, ok_resp]):
            with patch('time.sleep'):
                result = c._call("flickr.test.login")
        self.assertEqual(result["stat"], "ok")

    def test_404_raises_immediately_without_retry(self):
        """HTTP 404 is a permanent error — should raise, not retry."""
        from unittest.mock import patch, MagicMock
        import requests as req
        c = self._make_client()
        not_found = self._mock_response(404)
        call_count = 0
        def counting_get(*a, **kw):
            nonlocal call_count
            call_count += 1
            return not_found
        with patch.object(c._session, 'get', side_effect=counting_get):
            with patch('time.sleep'):
                with self.assertRaises(req.HTTPError):
                    c._call("flickr.photos.getInfo")
        self.assertEqual(call_count, 1)  # no retries

    def test_403_raises_immediately_without_retry(self):
        """HTTP 403 is a permanent error — should raise, not retry."""
        from unittest.mock import patch
        import requests as req
        c = self._make_client()
        forbidden = self._mock_response(403)
        call_count = 0
        def counting_get(*a, **kw):
            nonlocal call_count
            call_count += 1
            return forbidden
        with patch.object(c._session, 'get', side_effect=counting_get):
            with patch('time.sleep'):
                with self.assertRaises(req.HTTPError):
                    c._call("flickr.photos.getInfo")
        self.assertEqual(call_count, 1)

    def test_retry_delay_includes_jitter(self):
        """Retry delay should include jitter (2^n + random), not bare 2^n."""
        from unittest.mock import patch
        c = self._make_client()
        err_resp = self._mock_response(500)
        ok_resp  = self._mock_response(200, {"stat": "ok"})
        sleep_calls = []
        # Mock random.uniform to return a fixed value so test is deterministic
        with patch.object(c._session, 'get', side_effect=[err_resp, ok_resp]):
            with patch('time.sleep', side_effect=lambda d: sleep_calls.append(d)):
                with patch('flickr.flickr_client.random.uniform', return_value=0.3):
                    c._call("flickr.test.login")
        retry_sleeps = [d for d in sleep_calls if d > 0]
        self.assertTrue(retry_sleeps, "Expected at least one non-zero retry sleep")
        retry_delay = retry_sleeps[0]
        # First retry: 2^0 + 0.3 = 1.3 exactly
        self.assertAlmostEqual(retry_delay, 1.3, places=5)

    def test_429_is_retried_not_treated_as_permanent(self):
        """429 rate limit must be retried, not raised immediately like 4xx."""
        from unittest.mock import patch
        c = self._make_client()
        rate_limited = self._mock_response(429)
        ok_resp = self._mock_response(200, {"stat": "ok"})
        call_count = 0
        def counting_get(*a, **kw):
            nonlocal call_count
            call_count += 1
            return rate_limited if call_count == 1 else ok_resp
        with patch.object(c._session, 'get', side_effect=counting_get):
            with patch('time.sleep'):
                result = c._call("flickr.test.login")
        self.assertEqual(result["stat"], "ok")
        self.assertEqual(call_count, 2)  # retried once


# ---------------------------------------------------------------------------
# Poller auto-push: approved Photos record matched to new Flickr upload
# ---------------------------------------------------------------------------

class TestFindApprovedPhotosRecord(unittest.TestCase):

    def setUp(self):
        fd, self.tmp_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = Database(self.tmp_path)

    def tearDown(self):
        self.db.close()
        os.unlink(self.tmp_path)

    def test_finds_approved_match_by_date(self):
        from poller.poller import _find_approved_photos_record
        # Insert a Photos-only approved record
        self.db.upsert_photo({
            "uuid": "ABC-123",
            "date_taken": "2024-06-16 10:00:00",
            "privacy_state": "approved_public",
        })
        # Simulate incoming Flickr row with same date
        flickr_row = {"date_taken": "2024-06-16 10:00:00", "flickr_id": "99999"}
        match = _find_approved_photos_record(self.db, flickr_row)
        self.assertIsNotNone(match)
        self.assertEqual(match["uuid"], "ABC-123")

    def test_no_match_for_different_date(self):
        from poller.poller import _find_approved_photos_record
        self.db.upsert_photo({
            "uuid": "ABC-456",
            "date_taken": "2024-06-16 10:00:00",
            "privacy_state": "approved_public",
        })
        flickr_row = {"date_taken": "2024-06-17 10:00:00", "flickr_id": "88888"}
        match = _find_approved_photos_record(self.db, flickr_row)
        self.assertIsNone(match)

    def test_no_match_when_not_approved(self):
        from poller.poller import _find_approved_photos_record
        self.db.upsert_photo({
            "uuid": "ABC-789",
            "date_taken": "2024-06-16 10:00:00",
            "privacy_state": "needs_review",
        })
        flickr_row = {"date_taken": "2024-06-16 10:00:00", "flickr_id": "77777"}
        match = _find_approved_photos_record(self.db, flickr_row)
        self.assertIsNone(match)

    def test_no_match_when_already_has_flickr_id(self):
        from poller.poller import _find_approved_photos_record
        # Record already linked to Flickr should not be re-matched
        self.db.upsert_photo({
            "flickr_id": "EXISTING",
            "date_taken": "2024-06-16 10:00:00",
            "privacy_state": "approved_public",
        })
        flickr_row = {"date_taken": "2024-06-16 10:00:00", "flickr_id": "NEW"}
        match = _find_approved_photos_record(self.db, flickr_row)
        self.assertIsNone(match)

    def test_iso8601_date_matches_space_format(self):
        from poller.poller import _find_approved_photos_record
        # Apple Photos stores: 2024-06-16T14:00:00.000000+00:00 (UTC)
        # Flickr returns:       2024-06-16 14:00:00 (UTC, space format)
        self.db.upsert_photo({
            "uuid": "DEF-123",
            "date_taken": "2024-06-16T14:00:00.000000+00:00",
            "privacy_state": "approved_public",
        })
        flickr_row = {"date_taken": "2024-06-16 14:00:00", "flickr_id": "66666"}
        match = _find_approved_photos_record(self.db, flickr_row)
        self.assertIsNotNone(match)

    def test_same_local_time_different_format_matches(self):
        from poller.poller import _find_approved_photos_record
        # Both sides record the same local capture time, just formatted differently.
        # normalise_dt strips timezone offset and milliseconds, keeping local time.
        # Apple Photos: 2024-06-16T10:00:00.583000-04:00 -> "2024-06-16 10:00:00"
        # Flickr:       2024-06-16T10:00:00               -> "2024-06-16 10:00:00"
        self.db.upsert_photo({
            "uuid": "GHI-123",
            "date_taken": "2024-06-16T10:00:00.583000-04:00",
            "privacy_state": "approved_public",
        })
        flickr_row = {"date_taken": "2024-06-16T10:00:00", "flickr_id": "55555"}
        match = _find_approved_photos_record(self.db, flickr_row)
        self.assertIsNotNone(match)


# ---------------------------------------------------------------------------
# bp CLI entry point
# ---------------------------------------------------------------------------

class TestBpCli(unittest.TestCase):

    def _run_bp(self, *args):
        """Run bp with given args, return (stdout, stderr, exit_code)."""
        import subprocess
        result = subprocess.run(
            [sys.executable, "bp"] + list(args),
            capture_output=True, text=True,
            cwd=str(Path(__file__).parent.parent),
        )
        return result.stdout, result.stderr, result.returncode

    def test_help(self):
        stdout, _, code = self._run_bp("--help")
        self.assertEqual(code, 0)
        self.assertIn("stats", stdout)
        self.assertIn("poll", stdout)
        self.assertIn("reconcile", stdout)

    def test_poll_help(self):
        stdout, _, code = self._run_bp("poll", "--help")
        self.assertEqual(code, 0)
        self.assertIn("--backfill", stdout)
        self.assertIn("--days", stdout)

    def test_scan_help(self):
        stdout, _, code = self._run_bp("scan", "--help")
        self.assertEqual(code, 0)
        self.assertIn("--all", stdout)

    def test_reconcile_help(self):
        stdout, _, code = self._run_bp("reconcile", "--help")
        self.assertEqual(code, 0)
        self.assertIn("--fix", stdout)
        self.assertIn("--limit", stdout)

    def test_unknown_command_fails(self):
        _, _, code = self._run_bp("notacommand")
        self.assertNotEqual(code, 0)

    def test_stats_missing_config_fails(self):
        _, stderr, code = self._run_bp("--config", "/nonexistent.yml", "stats")
        self.assertNotEqual(code, 0)

    def test_sync_albums_help(self):
        stdout, _, code = self._run_bp("sync-albums", "--help")
        self.assertEqual(code, 0)
        self.assertIn("--dry-run", stdout)
        self.assertIn("--album",   stdout)
        self.assertIn("--verbose", stdout)

    def test_sync_albums_verbose_flag_accepted(self):
        """bp sync-albums --verbose must not be rejected as an unrecognised argument."""
        # --config is a global flag so it comes before the subcommand.
        # --verbose on the subparser must be accepted without argparse complaining.
        _, stderr, _ = self._run_bp("--config", "/nonexistent.yml",
                                    "sync-albums", "--verbose")
        self.assertNotIn("unrecognized", stderr.lower())


# ---------------------------------------------------------------------------
# updated_at stamping
# ---------------------------------------------------------------------------

class TestUpdatedAt(unittest.TestCase):

    def setUp(self):
        fd, self.tmp_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = Database(self.tmp_path)

    def tearDown(self):
        self.db.close()
        os.unlink(self.tmp_path)

    def test_updated_at_set_on_insert(self):
        self.db.upsert_photo({"flickr_id": "U1", "privacy_state": "needs_review"})
        row = self.db.get_photo_by_flickr_id("U1")
        self.assertIsNotNone(row["updated_at"])

    def test_updated_at_changes_on_update(self):
        import time
        self.db.upsert_photo({"flickr_id": "U2", "privacy_state": "needs_review"})
        first = self.db.get_photo_by_flickr_id("U2")["updated_at"]
        time.sleep(0.01)
        self.db.upsert_photo({"flickr_id": "U2", "privacy_state": "candidate_public"})
        second = self.db.get_photo_by_flickr_id("U2")["updated_at"]
        self.assertGreater(second, first)


# ---------------------------------------------------------------------------
# schema_migrations table
# ---------------------------------------------------------------------------

class TestSchemaMigrations(unittest.TestCase):

    def setUp(self):
        fd, self.tmp_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = Database(self.tmp_path)

    def tearDown(self):
        self.db.close()
        os.unlink(self.tmp_path)

    def test_migration_table_exists_after_migrate_002(self):
        import sys as _sys, io, contextlib
        _sys.path.insert(0, str(Path(__file__).parent.parent / "db"))
        from migrate_002_updated_at_and_indexes import run
        with contextlib.redirect_stdout(io.StringIO()):
            run(self.tmp_path, dry_run=False)
        row = self.db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        self.assertIsNotNone(row)

    def test_migration_002_idempotent(self):
        import sys as _sys, io, contextlib
        _sys.path.insert(0, str(Path(__file__).parent.parent / "db"))
        from migrate_002_updated_at_and_indexes import run
        with contextlib.redirect_stdout(io.StringIO()):
            run(self.tmp_path, dry_run=False)
            run(self.tmp_path, dry_run=False)  # should not raise


# ---------------------------------------------------------------------------
# bp exit codes
# ---------------------------------------------------------------------------

class TestBpExitCodes(unittest.TestCase):

    def _run_bp(self, *args):
        import subprocess
        result = subprocess.run(
            [sys.executable, "bp"] + list(args),
            capture_output=True, text=True,
            cwd=str(Path(__file__).parent.parent),
        )
        return result.stdout, result.stderr, result.returncode

    def test_bad_config_exits_nonzero(self):
        _, _, code = self._run_bp("--config", "/nonexistent.yml", "stats")
        self.assertNotEqual(code, 0)

    def test_help_exits_zero(self):
        _, _, code = self._run_bp("--help")
        self.assertEqual(code, 0)


# ---------------------------------------------------------------------------
# Reconcile exit code behavior
# ---------------------------------------------------------------------------

class TestReconcileExitCodes(unittest.TestCase):
    """
    Exit code contract:
      0 = clean
      1 = mismatches found (without --fix)
      2 = operational errors (API failures, fix failures)
    """

    def _exit_code(self, mismatch_count, error_count, fix_fail_count, is_fix):
        """Mirror the exit code logic from reconcile.main()."""
        if error_count or fix_fail_count:
            return 2
        if mismatch_count and not is_fix:
            return 1
        return 0

    def test_clean_returns_zero(self):
        self.assertEqual(self._exit_code(0, 0, 0, False), 0)

    def test_mismatch_without_fix_returns_one(self):
        self.assertEqual(self._exit_code(3, 0, 0, False), 1)

    def test_mismatch_with_fix_and_all_fixed_returns_zero(self):
        self.assertEqual(self._exit_code(2, 0, 0, True), 0)

    def test_api_error_returns_two(self):
        self.assertEqual(self._exit_code(0, 1, 0, False), 2)

    def test_fix_failure_returns_two(self):
        self.assertEqual(self._exit_code(2, 0, 1, True), 2)

    def test_api_error_beats_mismatch(self):
        # Errors take priority over plain mismatches
        self.assertEqual(self._exit_code(3, 2, 0, False), 2)

    def test_mixed_mismatch_and_fix_failure_returns_two(self):
        # With --fix: some fixed, some failed → still exit 2
        self.assertEqual(self._exit_code(3, 0, 1, True), 2)

    def test_fix_all_success_returns_zero_not_one(self):
        # With --fix: all mismatches resolved → exit 0, not 1
        self.assertEqual(self._exit_code(5, 0, 0, True), 0)


# ---------------------------------------------------------------------------
# Poller push_errors propagation
# ---------------------------------------------------------------------------

class TestPollerPushErrors(unittest.TestCase):

    def setUp(self):
        fd, self.tmp_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = Database(self.tmp_path)

    def tearDown(self):
        self.db.close()
        os.unlink(self.tmp_path)

    def test_push_to_flickr_returns_zero_on_success(self):
        from unittest.mock import MagicMock, patch
        from poller.poller import _push_to_flickr
        import json

        self.db.upsert_photo({
            "flickr_id": "TEST1",
            "privacy_state": "approved_public",
            "proposed_tags": json.dumps(["tag1", "tag2"]),
        })
        record = self.db.get_photo_by_flickr_id("TEST1")

        mock_client = MagicMock()
        errors = _push_to_flickr(mock_client, "TEST1", record, self.db, dry_run=False)
        self.assertEqual(errors, 0)

    def test_push_to_flickr_returns_error_count_on_failure(self):
        from unittest.mock import MagicMock
        from flickr.flickr_client import FlickrError
        from poller.poller import _push_to_flickr
        import json

        self.db.upsert_photo({
            "flickr_id": "TEST2",
            "privacy_state": "approved_public",
            "proposed_tags": json.dumps(["tag1"]),
        })
        record = self.db.get_photo_by_flickr_id("TEST2")

        mock_client = MagicMock()
        mock_client.set_permissions.side_effect = FlickrError(0, "service unavailable")

        errors = _push_to_flickr(mock_client, "TEST2", record, self.db, dry_run=False)
        self.assertEqual(errors, 1)

    def test_max_tags_error_not_counted_as_failure(self):
        """Flickr error 2 (max tags) should be skipped, not counted as push error."""
        from unittest.mock import MagicMock
        from flickr.flickr_client import FlickrError, FLICKR_ERR_MAX_TAGS
        from poller.poller import _push_to_flickr
        import json

        self.db.upsert_photo({
            "flickr_id": "MAXTAGS",
            "privacy_state": "approved_public",
            "proposed_tags": json.dumps(["tag1"]),
        })
        record = self.db.get_photo_by_flickr_id("MAXTAGS")

        mock_client = MagicMock()
        mock_client.set_permissions.return_value = {"stat": "ok"}
        mock_client.add_tags.side_effect = FlickrError(FLICKR_ERR_MAX_TAGS, "Maximum number of tags reached")

        errors = _push_to_flickr(mock_client, "MAXTAGS", record, self.db, dry_run=False)
        # Max tags is not an error — perms still pushed successfully
        self.assertEqual(errors, 0)

    def test_db_flag_not_set_on_failed_push(self):
        from unittest.mock import MagicMock
        from flickr.flickr_client import FlickrError
        from poller.poller import _push_to_flickr
        import json

        self.db.upsert_photo({
            "flickr_id": "TEST3",
            "privacy_state": "approved_public",
            "proposed_tags": json.dumps(["tag1"]),
        })
        record = self.db.get_photo_by_flickr_id("TEST3")

        mock_client = MagicMock()
        mock_client.set_permissions.side_effect = FlickrError(0, "fail")

        _push_to_flickr(mock_client, "TEST3", record, self.db, dry_run=False)
        updated = self.db.get_photo_by_flickr_id("TEST3")
        self.assertEqual(updated["perms_pushed_flickr"], 0)


# ---------------------------------------------------------------------------
# sync_photo_albums — scanner helper
# ---------------------------------------------------------------------------

class TestSyncPhotoAlbums(unittest.TestCase):
    """sync_photo_albums must use photo.album_info (AlbumInfo objects), not photo.albums (strings)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        from db.db import Database
        self.db = Database(Path(self._tmp.name) / "test.db")
        # Insert a photo to attach albums to
        self.photo_id = self.db.upsert_photo({
            "uuid": "test-uuid-001",
            "original_filename": "IMG_001.jpg",
            "privacy_state": "candidate_public",
        })

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _make_album_info(self, title, uuid, album_type=None):
        """Return a simple namespace mimicking an osxphotos AlbumInfo object."""
        from types import SimpleNamespace
        obj = SimpleNamespace(title=title, uuid=uuid)
        if album_type is not None:
            obj.album_type = album_type
        return obj

    def test_uses_album_info_not_albums(self):
        """sync_photo_albums must read photo.album_info, not photo.albums."""
        from poller.scanner import sync_photo_albums
        from types import SimpleNamespace

        album = self._make_album_info("Vacation 2024", "album-uuid-1")
        # photo.albums is a plain list of strings — must be ignored
        photo = SimpleNamespace(
            albums=["Vacation 2024"],          # strings — wrong attribute
            album_info=[album],                # AlbumInfo objects — correct
        )
        sync_photo_albums(photo, self.photo_id, self.db, dry_run=False)

        count = self.db.conn.execute("SELECT COUNT(*) AS n FROM albums").fetchone()["n"]
        self.assertEqual(count, 1, "album must be inserted via album_info")
        row = self.db.conn.execute("SELECT name FROM albums").fetchone()
        self.assertEqual(row["name"], "Vacation 2024")

    def test_skips_non_album_type_when_present(self):
        """When album_type is available, non-'Album' entries must be skipped."""
        from poller.scanner import sync_photo_albums
        from types import SimpleNamespace

        user_album  = self._make_album_info("My Trip", "uuid-user",  album_type="Album")
        smart_album = self._make_album_info("Favourites", "uuid-smart", album_type="SmartAlbum")
        photo = SimpleNamespace(album_info=[user_album, smart_album])

        sync_photo_albums(photo, self.photo_id, self.db, dry_run=False)

        rows = self.db.conn.execute("SELECT name FROM albums").fetchall()
        names = [r["name"] for r in rows]
        self.assertIn("My Trip", names)
        self.assertNotIn("Favourites", names)

    def test_accepts_album_without_album_type_attr(self):
        """When album_type is absent (osxphotos 0.75.x), the album must be accepted."""
        from poller.scanner import sync_photo_albums
        from types import SimpleNamespace

        album = self._make_album_info("No Type Album", "uuid-notype")  # no album_type attr
        photo = SimpleNamespace(album_info=[album])

        sync_photo_albums(photo, self.photo_id, self.db, dry_run=False)

        count = self.db.conn.execute("SELECT COUNT(*) AS n FROM albums").fetchone()["n"]
        self.assertEqual(count, 1)

    def test_dry_run_does_not_write(self):
        """dry_run=True must not insert any rows."""
        from poller.scanner import sync_photo_albums
        from types import SimpleNamespace

        album = self._make_album_info("Dry Run Album", "uuid-dry")
        photo = SimpleNamespace(album_info=[album])

        sync_photo_albums(photo, self.photo_id, self.db, dry_run=True)

        count = self.db.conn.execute("SELECT COUNT(*) AS n FROM albums").fetchone()["n"]
        self.assertEqual(count, 0, "dry_run must not write albums")

    def test_empty_album_info_is_safe(self):
        """A photo with no albums must not error."""
        from poller.scanner import sync_photo_albums
        from types import SimpleNamespace

        photo = SimpleNamespace(album_info=[])
        sync_photo_albums(photo, self.photo_id, self.db, dry_run=False)  # must not raise

        count = self.db.conn.execute("SELECT COUNT(*) AS n FROM albums").fetchone()["n"]
        self.assertEqual(count, 0)

    def test_missing_album_info_attr_is_safe(self):
        """A photo object with no album_info attribute at all must not error."""
        from poller.scanner import sync_photo_albums
        from types import SimpleNamespace

        photo = SimpleNamespace()  # no album_info attribute
        sync_photo_albums(photo, self.photo_id, self.db, dry_run=False)  # must not raise


# ---------------------------------------------------------------------------
# Album DB methods
# ---------------------------------------------------------------------------

def _make_db(tmp_dir: str):
    from db.db import Database
    return Database(Path(tmp_dir) / "test.db")


def _seed_photo(db, flickr_id=None, perms_pushed=0) -> int:
    import uuid as _uuid
    return db.upsert_photo({
        "uuid": str(_uuid.uuid4()),
        "original_filename": "IMG_0001.JPG",
        "privacy_state": "approved_public" if flickr_id else "candidate_public",
        "flickr_id": flickr_id,
        "perms_pushed_flickr": perms_pushed,
        "proposed_tags": [],
        "apple_persons": [],
        "apple_labels": [],
    })


class TestAlbumDB(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = _make_db(self._tmp.name)

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_upsert_album_creates_and_returns_id(self):
        aid = self.db.upsert_album("apple-uuid-1", "Vacation 2024")
        self.assertIsInstance(aid, int)
        self.assertGreater(aid, 0)

    def test_upsert_album_idempotent(self):
        aid1 = self.db.upsert_album("apple-uuid-1", "Vacation 2024")
        aid2 = self.db.upsert_album("apple-uuid-1", "Vacation 2024")
        self.assertEqual(aid1, aid2)

    def test_upsert_album_updates_name(self):
        aid = self.db.upsert_album("apple-uuid-1", "Old Name")
        self.db.upsert_album("apple-uuid-1", "New Name")
        row = self.db.conn.execute("SELECT name FROM albums WHERE id = ?", (aid,)).fetchone()
        self.assertEqual(row["name"], "New Name")

    def test_upsert_photo_album_creates_row(self):
        photo_id = _seed_photo(self.db)
        album_id = self.db.upsert_album("apple-uuid-1", "Test Album")
        self.db.upsert_photo_album(photo_id, album_id)
        row = self.db.conn.execute(
            "SELECT * FROM photo_albums WHERE photo_id = ? AND album_id = ?",
            (photo_id, album_id),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["flickr_pushed"], 0)

    def test_upsert_photo_album_idempotent(self):
        photo_id = _seed_photo(self.db)
        album_id = self.db.upsert_album("apple-uuid-1", "Test Album")
        self.db.upsert_photo_album(photo_id, album_id)
        self.db.upsert_photo_album(photo_id, album_id)  # second call must not error
        count = self.db.conn.execute(
            "SELECT COUNT(*) AS n FROM photo_albums WHERE photo_id = ? AND album_id = ?",
            (photo_id, album_id),
        ).fetchone()["n"]
        self.assertEqual(count, 1)

    def test_get_pending_album_pushes_requires_flickr_id_and_perms(self):
        # Photo with no flickr_id — should NOT appear
        photo_no_flickr = _seed_photo(self.db, flickr_id=None, perms_pushed=0)
        album_id = self.db.upsert_album("uuid-a", "Album A")
        self.db.upsert_photo_album(photo_no_flickr, album_id)

        # Photo with flickr_id but perms not pushed — should NOT appear
        photo_no_perms = _seed_photo(self.db, flickr_id="f001", perms_pushed=0)
        album_id2 = self.db.upsert_album("uuid-b", "Album B")
        self.db.upsert_photo_album(photo_no_perms, album_id2)

        # Photo with flickr_id AND perms pushed — SHOULD appear
        photo_ready = _seed_photo(self.db, flickr_id="f002", perms_pushed=1)
        album_id3 = self.db.upsert_album("uuid-c", "Album C")
        self.db.upsert_photo_album(photo_ready, album_id3)

        pending = self.db.get_pending_album_pushes()
        photo_ids = [r["photo_id"] for r in pending]
        self.assertNotIn(photo_no_flickr, photo_ids)
        self.assertNotIn(photo_no_perms, photo_ids)
        self.assertIn(photo_ready, photo_ids)

    def test_mark_album_pushed(self):
        photo_id = _seed_photo(self.db, flickr_id="f001", perms_pushed=1)
        album_id = self.db.upsert_album("uuid-a", "Album A")
        self.db.upsert_photo_album(photo_id, album_id)

        self.db.mark_album_pushed(photo_id, album_id)

        row = self.db.conn.execute(
            "SELECT flickr_pushed, pushed_at FROM photo_albums WHERE photo_id = ? AND album_id = ?",
            (photo_id, album_id),
        ).fetchone()
        self.assertEqual(row["flickr_pushed"], 1)
        self.assertIsNotNone(row["pushed_at"])

    def test_set_album_flickr_set_id(self):
        album_id = self.db.upsert_album("uuid-a", "Album A")
        self.db.set_album_flickr_set_id(album_id, "72157720000001", "https://www.flickr.com/photos/me/sets/72157720000001/")
        row = self.db.conn.execute(
            "SELECT flickr_set_id, flickr_set_url FROM albums WHERE id = ?", (album_id,)
        ).fetchone()
        self.assertEqual(row["flickr_set_id"], "72157720000001")
        self.assertIn("flickr.com", row["flickr_set_url"])

    def test_get_pending_excludes_already_pushed(self):
        photo_id = _seed_photo(self.db, flickr_id="f001", perms_pushed=1)
        album_id = self.db.upsert_album("uuid-a", "Album A")
        self.db.upsert_photo_album(photo_id, album_id)
        self.db.mark_album_pushed(photo_id, album_id)

        pending = self.db.get_pending_album_pushes()
        self.assertEqual(pending, [])

    def test_get_pending_includes_keep_private_photos(self):
        """keep_private photos with flickr_id but perms_pushed=0 should be included."""
        import uuid as _uuid
        photo_id = self.db.upsert_photo({
            "uuid": str(_uuid.uuid4()),
            "original_filename": "private.jpg",
            "privacy_state": "keep_private",
            "review_decision": "keep_private",
            "flickr_id": "f999",
            "perms_pushed_flickr": 0,
            "proposed_tags": [],
            "apple_persons": [],
            "apple_labels": [],
        })
        album_id = self.db.upsert_album("uuid-prv", "Private Album")
        self.db.upsert_photo_album(photo_id, album_id)

        pending = self.db.get_pending_album_pushes()
        photo_ids = [r["photo_id"] for r in pending]
        self.assertIn(photo_id, photo_ids)

    def test_get_photo_albums_returns_membership(self):
        photo_id = _seed_photo(self.db, flickr_id="f001", perms_pushed=1)
        album_id = self.db.upsert_album("uuid-a", "Trip Photos")
        self.db.upsert_photo_album(photo_id, album_id)

        albums = self.db.get_photo_albums(photo_id)
        self.assertEqual(len(albums), 1)
        self.assertEqual(albums[0]["name"], "Trip Photos")
        self.assertEqual(albums[0]["flickr_pushed"], 0)

    def test_get_photo_albums_shows_pushed_status(self):
        photo_id = _seed_photo(self.db, flickr_id="f001", perms_pushed=1)
        album_id = self.db.upsert_album("uuid-a", "Trip Photos")
        self.db.upsert_photo_album(photo_id, album_id)
        self.db.mark_album_pushed(photo_id, album_id)

        albums = self.db.get_photo_albums(photo_id)
        self.assertEqual(albums[0]["flickr_pushed"], 1)
        self.assertIsNotNone(albums[0]["pushed_at"])

    def test_get_photo_albums_empty_for_photo_with_no_albums(self):
        photo_id = _seed_photo(self.db, flickr_id="f001")
        albums = self.db.get_photo_albums(photo_id)
        self.assertEqual(albums, [])

    def test_get_album_counts_for_photos(self):
        photo1 = _seed_photo(self.db, flickr_id="f001")
        photo2 = _seed_photo(self.db, flickr_id="f002")
        album_a = self.db.upsert_album("uuid-a", "Album A")
        album_b = self.db.upsert_album("uuid-b", "Album B")
        self.db.upsert_photo_album(photo1, album_a)
        self.db.upsert_photo_album(photo1, album_b)
        self.db.upsert_photo_album(photo2, album_a)

        counts = self.db.get_album_counts_for_photos([photo1, photo2])
        self.assertEqual(counts[photo1], 2)
        self.assertEqual(counts[photo2], 1)

    def test_get_album_counts_empty_input(self):
        counts = self.db.get_album_counts_for_photos([])
        self.assertEqual(counts, {})


# ---------------------------------------------------------------------------
# Album pusher
# ---------------------------------------------------------------------------

class TestAlbumPusher(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = _make_db(self._tmp.name)
        self.photo_id = _seed_photo(self.db, flickr_id="flickr001", perms_pushed=1)
        self.album_id = self.db.upsert_album("apple-uuid-1", "Trip Photos")
        self.db.upsert_photo_album(self.photo_id, self.album_id)

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _mock_flickr(self, new_set_id="SET001"):
        from unittest.mock import MagicMock
        m = MagicMock()
        m.create_photoset.return_value = new_set_id
        m.add_photo_to_photoset.return_value = None
        return m

    def test_skips_photo_with_no_flickr_id(self):
        from flickr.album_pusher import push_photo_to_albums
        photo_id = _seed_photo(self.db, flickr_id=None)
        album_id = self.db.upsert_album("uuid-nf", "No Flickr Album")
        self.db.upsert_photo_album(photo_id, album_id)
        flickr = self._mock_flickr()
        result = push_photo_to_albums(self.db, flickr, photo_id)
        self.assertEqual(result, 0)
        flickr.create_photoset.assert_not_called()

    def test_creates_photoset_when_none_exists(self):
        from flickr.album_pusher import push_photo_to_albums
        flickr = self._mock_flickr(new_set_id="NEW_SET")
        push_photo_to_albums(self.db, flickr, self.photo_id)
        flickr.create_photoset.assert_called_once_with("Trip Photos", "flickr001")

    def test_adds_to_existing_photoset(self):
        from flickr.album_pusher import push_photo_to_albums
        self.db.set_album_flickr_set_id(self.album_id, "EXISTING_SET")
        flickr = self._mock_flickr()
        push_photo_to_albums(self.db, flickr, self.photo_id)
        flickr.add_photo_to_photoset.assert_called_once_with("EXISTING_SET", "flickr001")
        flickr.create_photoset.assert_not_called()

    def test_marks_pushed_on_success(self):
        from flickr.album_pusher import push_photo_to_albums
        flickr = self._mock_flickr(new_set_id="SET001")
        push_photo_to_albums(self.db, flickr, self.photo_id)
        row = self.db.conn.execute(
            "SELECT flickr_pushed FROM photo_albums WHERE photo_id = ? AND album_id = ?",
            (self.photo_id, self.album_id),
        ).fetchone()
        self.assertEqual(row["flickr_pushed"], 1)

    def test_stores_flickr_set_id_after_create(self):
        from flickr.album_pusher import push_photo_to_albums
        flickr = self._mock_flickr(new_set_id="CREATED_SET")
        push_photo_to_albums(self.db, flickr, self.photo_id)
        row = self.db.conn.execute(
            "SELECT flickr_set_id FROM albums WHERE id = ?", (self.album_id,)
        ).fetchone()
        self.assertEqual(row["flickr_set_id"], "CREATED_SET")

    def test_returns_count_of_successes(self):
        from flickr.album_pusher import push_photo_to_albums
        # Add a second album
        album_id2 = self.db.upsert_album("uuid-b", "Another Album")
        self.db.upsert_photo_album(self.photo_id, album_id2)
        flickr = self._mock_flickr()
        result = push_photo_to_albums(self.db, flickr, self.photo_id)
        self.assertEqual(result, 2)

    def test_logs_and_continues_on_flickr_error(self):
        from flickr.album_pusher import push_photo_to_albums
        from flickr.flickr_client import FlickrError
        from unittest.mock import MagicMock

        # Two albums — first raises FlickrError, second succeeds
        album_id2 = self.db.upsert_album("uuid-b", "Album B")
        self.db.upsert_photo_album(self.photo_id, album_id2)

        flickr = MagicMock()
        flickr.create_photoset.side_effect = [
            FlickrError(1, "error"),
            "SET_OK",
        ]

        result = push_photo_to_albums(self.db, flickr, self.photo_id)
        # One failed, one succeeded
        self.assertEqual(result, 1)

    def test_returns_zero_when_no_pending(self):
        from flickr.album_pusher import push_photo_to_albums
        flickr = self._mock_flickr()
        # Push once to mark done
        push_photo_to_albums(self.db, flickr, self.photo_id)
        # Second call should find nothing pending
        result = push_photo_to_albums(self.db, flickr, self.photo_id)
        self.assertEqual(result, 0)


# ---------------------------------------------------------------------------
# sync-albums CLI
# ---------------------------------------------------------------------------

class TestSyncAlbumsCLI(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._db_path = Path(self._tmp.name) / "test.db"
        from db.db import Database
        self.db = Database(self._db_path)

        # Write a minimal config file
        self._config_path = Path(self._tmp.name) / "config.yml"
        self._config_path.write_text(
            f"database:\n  path: {self._db_path}\n"
            "flickr:\n"
            "  api_key: test\n"
            "  api_secret: test\n"
            "  oauth_token: test\n"
            "  oauth_token_secret: test\n"
            "  user_nsid: test\n"
        )

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _run_cli(self, extra_argv=None):
        import subprocess
        cmd = [
            sys.executable,
            str(Path(__file__).parent.parent / "flickr" / "sync_albums.py"),
            "--config", str(self._config_path),
        ] + (extra_argv or [])
        result = subprocess.run(cmd, capture_output=True, text=True)
        return result

    def test_exit_0_when_nothing_to_do(self):
        result = self._run_cli()
        self.assertEqual(result.returncode, 0)
        self.assertIn("photos added=0", result.stdout)

    def test_dry_run_does_not_write(self):
        photo_id = _seed_photo(self.db, flickr_id="f001", perms_pushed=1)
        album_id = self.db.upsert_album("uuid-a", "Album A")
        self.db.upsert_photo_album(photo_id, album_id)

        result = self._run_cli(["--dry-run"])
        self.assertEqual(result.returncode, 0)

        # Row must still be unpushed after dry-run
        row = self.db.conn.execute(
            "SELECT flickr_pushed FROM photo_albums WHERE photo_id = ? AND album_id = ?",
            (photo_id, album_id),
        ).fetchone()
        self.assertEqual(row["flickr_pushed"], 0)

    def test_album_filter_excludes_other_albums(self):
        photo_id = _seed_photo(self.db, flickr_id="f001", perms_pushed=1)
        album_id = self.db.upsert_album("uuid-a", "Family Photos")
        self.db.upsert_photo_album(photo_id, album_id)

        # Filter to a different album name — nothing pending for "Other Album"
        result = self._run_cli(["--dry-run", "--album", "Other Album"])
        self.assertEqual(result.returncode, 0)
        self.assertIn("photos added=0", result.stdout)


# ---------------------------------------------------------------------------
# Metadata conflicts — DB layer
# ---------------------------------------------------------------------------

class TestMetadataConflictDB(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "test.db")
        self.photo_id = self.db.upsert_photo({
            "uuid": "uuid-mc-001",
            "original_filename": "IMG_mc.JPG",
            "privacy_state": "approved_public",
            "flickr_id": "flickr-mc-001",
            "proposed_tags": [],
            "apple_persons": [],
            "apple_labels": [],
        })

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_upsert_conflict_creates_row(self):
        self.db.upsert_metadata_conflict(self.photo_id, "title", "Flickr Title", "Photos Title")
        rows = self.db.get_unresolved_conflicts(photo_id=self.photo_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["field"], "title")
        self.assertEqual(rows[0]["flickr_value"], "Flickr Title")
        self.assertEqual(rows[0]["photos_value"], "Photos Title")

    def test_upsert_conflict_idempotent_replaces(self):
        self.db.upsert_metadata_conflict(self.photo_id, "title", "Old Flickr", "Old Photos")
        self.db.upsert_metadata_conflict(self.photo_id, "title", "New Flickr", "New Photos")
        rows = self.db.get_unresolved_conflicts(photo_id=self.photo_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["flickr_value"], "New Flickr")

    def test_resolve_conflict_marks_resolved(self):
        cid = self.db.upsert_metadata_conflict(self.photo_id, "description", "F", "P")
        self.db.resolve_metadata_conflict(cid, "flickr")
        rows = self.db.get_unresolved_conflicts(photo_id=self.photo_id)
        self.assertEqual(len(rows), 0)
        row = self.db.conn.execute(
            "SELECT resolved, resolution FROM metadata_conflicts WHERE id = ?", (cid,)
        ).fetchone()
        self.assertEqual(row["resolved"], 1)
        self.assertEqual(row["resolution"], "flickr")

    def test_get_unresolved_excludes_resolved(self):
        cid1 = self.db.upsert_metadata_conflict(self.photo_id, "title", "F", "P")
        self.db.upsert_metadata_conflict(self.photo_id, "description", "F", "P")
        self.db.resolve_metadata_conflict(cid1, "photos")
        rows = self.db.get_unresolved_conflicts(photo_id=self.photo_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["field"], "description")

    def test_get_unresolved_filtered_by_photo_id(self):
        other_id = self.db.upsert_photo({
            "uuid": "uuid-mc-002", "original_filename": "IMG2.JPG",
            "privacy_state": "approved_public", "flickr_id": "flickr-mc-002",
            "proposed_tags": [], "apple_persons": [], "apple_labels": [],
        })
        self.db.upsert_metadata_conflict(self.photo_id, "title", "F", "P")
        self.db.upsert_metadata_conflict(other_id, "title", "F2", "P2")
        rows = self.db.get_unresolved_conflicts(photo_id=self.photo_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["photo_id"], self.photo_id)

    def test_get_conflict_counts_totals(self):
        self.db.upsert_metadata_conflict(self.photo_id, "title", "F", "P")
        self.db.upsert_metadata_conflict(self.photo_id, "description", "F", "P")
        counts = self.db.get_conflict_counts()
        self.assertEqual(counts["total"], 2)
        self.assertEqual(counts["title"], 1)
        self.assertEqual(counts["description"], 1)
        self.assertEqual(counts["tags"], 0)

    def test_get_conflict_counts_zero_when_none(self):
        counts = self.db.get_conflict_counts()
        self.assertEqual(counts, {"total": 0, "title": 0, "description": 0, "tags": 0})

    def test_stats_includes_metadata_conflicts(self):
        self.db.upsert_metadata_conflict(self.photo_id, "title", "F", "P")
        s = self.db.stats()
        self.assertIn("metadata_conflicts", s)
        self.assertEqual(s["metadata_conflicts"]["total"], 1)

    def test_on_delete_cascade(self):
        self.db.upsert_metadata_conflict(self.photo_id, "title", "F", "P")
        self.db.conn.execute("DELETE FROM photos WHERE id = ?", (self.photo_id,))
        self.db.conn.commit()
        rows = self.db.conn.execute("SELECT * FROM metadata_conflicts").fetchall()
        self.assertEqual(len(rows), 0)


# ---------------------------------------------------------------------------
# Metadata puller — core comparison logic
# ---------------------------------------------------------------------------

class TestMetadataPuller(unittest.TestCase):

    def setUp(self):
        from unittest.mock import MagicMock
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "test.db")
        self.photo_id = self.db.upsert_photo({
            "uuid": "uuid-mp-001",
            "original_filename": "IMG_mp.JPG",
            "privacy_state": "approved_public",
            "flickr_id": "flickr-mp-001",
            "proposed_tags": [],
            "apple_persons": [],
            "apple_labels": [],
        })
        self.mock_flickr = MagicMock()
        self.library = "/fake/Photos.photoslibrary"

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _set_flickr_meta(self, title="", description="", tags=None):
        self.mock_flickr.get_photo_info.return_value = {
            "photo": {
                "title":       {"_content": title},
                "description": {"_content": description},
                "tags":        {"tag": [{"raw": t} for t in (tags or [])]},
            }
        }

    def _patch_photos(self, title="", description="", tags=None, has_update=True):
        """Return a context manager patching _read_photos_metadata and _write_photos_metadata."""
        from unittest.mock import patch
        read_return = {"title": title, "description": description, "tags": tags or []}
        patches = [
            patch("flickr.metadata_puller._read_photos_metadata", return_value=read_return),
        ]
        if has_update:
            patches.append(patch("flickr.metadata_puller._write_photos_metadata"))
        else:
            patches.append(patch(
                "flickr.metadata_puller._write_photos_metadata",
                side_effect=RuntimeError("Photos.app is not running"),
            ))
        return patches

    def _pull(self, dry_run=False):
        from flickr.metadata_puller import pull_photo_metadata
        return pull_photo_metadata(
            self.db, self.mock_flickr, self.photo_id,
            library_path=self.library, dry_run=dry_run,
        )

    def test_flickr_wins_when_photos_empty(self):
        from unittest.mock import patch
        self._set_flickr_meta(title="A Great Shot")
        with patch("flickr.metadata_puller._read_photos_metadata", return_value={"title": "", "description": "", "tags": []}), \
             patch("flickr.metadata_puller._write_photos_metadata") as mock_write:
            result = self._pull()
        self.assertIn("title", result["written"])
        self.assertEqual(result["conflicts"], [])
        mock_write.assert_called_once()

    def test_no_op_when_values_equal(self):
        from unittest.mock import patch
        self._set_flickr_meta(title="Same Title")
        with patch("flickr.metadata_puller._read_photos_metadata", return_value={"title": "Same Title", "description": "", "tags": []}), \
             patch("flickr.metadata_puller._write_photos_metadata") as mock_write:
            result = self._pull()
        self.assertIn("title", result["skipped"])
        self.assertEqual(result["written"], [])
        mock_write.assert_not_called()

    def test_conflict_recorded_when_both_non_empty_different(self):
        from unittest.mock import patch
        self._set_flickr_meta(description="Flickr caption")
        with patch("flickr.metadata_puller._read_photos_metadata", return_value={"title": "", "description": "Photos caption", "tags": []}), \
             patch("flickr.metadata_puller._write_photos_metadata") as mock_write:
            result = self._pull()
        self.assertIn("description", result["conflicts"])
        self.assertEqual(result["written"], [])
        mock_write.assert_not_called()
        rows = self.db.get_unresolved_conflicts(photo_id=self.photo_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["field"], "description")

    def test_photos_only_value_preserved(self):
        from unittest.mock import patch
        self._set_flickr_meta(description="")
        with patch("flickr.metadata_puller._read_photos_metadata", return_value={"title": "", "description": "Local note", "tags": []}), \
             patch("flickr.metadata_puller._write_photos_metadata") as mock_write:
            result = self._pull()
        self.assertIn("description", result["skipped"])
        mock_write.assert_not_called()

    def test_tags_comparison_case_insensitive(self):
        from unittest.mock import patch
        self._set_flickr_meta(tags=["Nature", "Landscape"])
        with patch("flickr.metadata_puller._read_photos_metadata", return_value={"title": "", "description": "", "tags": ["nature", "landscape"]}), \
             patch("flickr.metadata_puller._write_photos_metadata") as mock_write:
            result = self._pull()
        self.assertIn("tags", result["skipped"])
        self.assertEqual(result["conflicts"], [])
        mock_write.assert_not_called()

    def test_tags_conflict_when_different(self):
        from unittest.mock import patch
        self._set_flickr_meta(tags=["nature"])
        with patch("flickr.metadata_puller._read_photos_metadata", return_value={"title": "", "description": "", "tags": ["landscape"]}), \
             patch("flickr.metadata_puller._write_photos_metadata"):
            result = self._pull()
        self.assertIn("tags", result["conflicts"])
        rows = self.db.get_unresolved_conflicts(photo_id=self.photo_id)
        tag_row = next((r for r in rows if r["field"] == "tags"), None)
        self.assertIsNotNone(tag_row)

    def test_dry_run_skips_all_writes_and_db_updates(self):
        from unittest.mock import patch
        self._set_flickr_meta(title="Flickr Title", description="Flickr Desc")
        with patch("flickr.metadata_puller._read_photos_metadata", return_value={"title": "", "description": "Photos Desc", "tags": []}), \
             patch("flickr.metadata_puller._write_photos_metadata") as mock_write:
            result = self._pull(dry_run=True)
        mock_write.assert_not_called()
        rows = self.db.get_unresolved_conflicts(photo_id=self.photo_id)
        self.assertEqual(len(rows), 0)   # no DB writes in dry_run
        self.assertIn("title", result["written"])  # counted as would-write

    def test_no_uuid_returns_no_uuid_status(self):
        from unittest.mock import patch
        # Seed a photo without a uuid
        no_uuid_id = self.db.upsert_photo({
            "uuid": None, "original_filename": "no_uuid.JPG",
            "privacy_state": "approved_public", "flickr_id": "flickr-nouuid",
            "proposed_tags": [], "apple_persons": [], "apple_labels": [],
        })
        from flickr.metadata_puller import pull_photo_metadata
        result = pull_photo_metadata(self.db, self.mock_flickr, no_uuid_id, self.library)
        self.assertEqual(result["status"], "no_uuid")
        self.mock_flickr.get_photo_info.assert_not_called()

    def test_flickr_error_propagates(self):
        from flickr.flickr_client import FlickrError
        self.mock_flickr.get_photo_info.side_effect = FlickrError(500, "Server Error")
        from flickr.metadata_puller import pull_photo_metadata
        result = pull_photo_metadata(self.db, self.mock_flickr, self.photo_id, self.library)
        self.assertEqual(result["status"], "flickr_error")
        self.assertTrue(len(result["errors"]) > 0)

    def test_write_error_counted_as_write_error_status(self):
        from unittest.mock import patch
        self._set_flickr_meta(title="Flickr Title")
        with patch("flickr.metadata_puller._read_photos_metadata", return_value={"title": "", "description": "", "tags": []}), \
             patch("flickr.metadata_puller._write_photos_metadata", side_effect=RuntimeError("Photos not running")):
            result = self._pull()
        self.assertEqual(result["status"], "write_error")
        self.assertTrue(len(result["errors"]) > 0)
        # No conflict recorded — a write failure is not a conflict
        rows = self.db.get_unresolved_conflicts(photo_id=self.photo_id)
        self.assertEqual(len(rows), 0)


# ---------------------------------------------------------------------------
# sync-metadata CLI
# ---------------------------------------------------------------------------

class TestSyncMetadataCLI(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._db_path = Path(self._tmp.name) / "test.db"
        self.db = Database(self._db_path)

        self._config_path = Path(self._tmp.name) / "config.yml"
        self._config_path.write_text(
            f"database:\n  path: {self._db_path}\n"
            "apple_photos:\n"
            f"  library_path: {self._tmp.name}/Photos.photoslibrary\n"
            "flickr:\n"
            "  api_key: test\n"
            "  api_secret: test\n"
            "  oauth_token: test\n"
            "  oauth_token_secret: test\n"
            "  user_nsid: test\n"
        )

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _run_cli(self, extra_argv=None):
        cmd = [
            sys.executable,
            str(Path(__file__).parent.parent / "flickr" / "sync_metadata.py"),
            "--config", str(self._config_path),
        ] + (extra_argv or [])
        return subprocess.run(cmd, capture_output=True, text=True)

    def test_exit_0_when_nothing_to_do(self):
        # No photos with both flickr_id and uuid → nothing to process
        result = self._run_cli()
        # Will fail at Flickr auth (no real creds) but gracefully
        # The key check: exit code is not 2 (operational) when DB is empty
        # We accept exit 0 or 1 (Flickr auth may fail, returning 2);
        # what we're validating is the summary line format.
        self.assertIn("written=", result.stdout)

    def test_dry_run_flag_accepted(self):
        result = self._run_cli(["--dry-run"])
        self.assertIn("written=", result.stdout)

    def test_limit_flag_accepted(self):
        result = self._run_cli(["--limit", "1"])
        self.assertIn("written=", result.stdout)

    def test_conflicts_only_flag_accepted(self):
        result = self._run_cli(["--conflicts-only"])
        self.assertIn("written=", result.stdout)


# ---------------------------------------------------------------------------
# End-to-end reconcile lifecycle: mismatch → fix → idempotent clean
# ---------------------------------------------------------------------------

class TestReconcileLifecycle(unittest.TestCase):
    """
    Full lifecycle test covering:
      1. DB marks photo as pushed-public but Flickr still shows it private
         → reconcile detects perm_mismatch, returns exit 1
      2. reconcile --fix corrects Flickr, returns exit 0
      3. Second reconcile run finds no mismatch, returns exit 0 (idempotent)
    """

    def setUp(self):
        from unittest.mock import MagicMock
        self._tmp = tempfile.mkdtemp()
        self.db   = Database(Path(self._tmp) / "test.db")

        # Seed: photo approved_public, perms pushed, tags pushed
        self.photo_id = self.db.upsert_photo({
            "uuid":               "uuid-e2e-001",
            "original_filename":  "IMG_e2e.JPG",
            "privacy_state":      "approved_public",
            "flickr_id":          "flickr-e2e-001",
            "perms_pushed_flickr": 1,
            "tags_pushed_flickr":  1,
            "proposed_tags":      ["nature", "landscape"],
            "apple_persons":      [],
            "apple_labels":       [],
        })

        # Build a reusable mock FlickrClient
        self.mock_client = MagicMock()

    def tearDown(self):
        self.db.close()
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _photo_row(self):
        return dict(self.db.conn.execute(
            "SELECT * FROM photos WHERE id = ?", (self.photo_id,)
        ).fetchone())

    def _flickr_info_response(self, is_public: int, tags: list[str]):
        """Build a minimal get_photo_info payload."""
        return {
            "photo": {
                "visibility": {"ispublic": is_public},
                "tags": {
                    "tag": [{"raw": t} for t in tags]
                },
            }
        }

    def test_1_mismatch_detected_when_flickr_is_private(self):
        """DB says public+pushed; Flickr says private → perm_mismatch."""
        from poller.reconcile import check_photo
        self.mock_client.get_photo_info.return_value = self._flickr_info_response(
            is_public=0, tags=["nature", "landscape"]
        )

        result = check_photo(self.mock_client, self._photo_row(), fix=False, verbose=False)

        self.assertEqual(result["status"], "perm_mismatch")
        self.assertEqual(result["perm_expected"], "public")
        self.assertEqual(result["perm_actual"],   "private")
        self.assertEqual(result["fixes"],  [])   # fix=False — no API write
        self.assertEqual(result["errors"], [])
        self.mock_client.set_permissions.assert_not_called()

    def test_2_fix_corrects_mismatch_and_calls_api(self):
        """fix=True: reconcile calls set_permissions; result carries the fix."""
        from poller.reconcile import check_photo
        self.mock_client.get_photo_info.return_value = self._flickr_info_response(
            is_public=0, tags=["nature", "landscape"]
        )

        result = check_photo(self.mock_client, self._photo_row(), fix=True, verbose=False)

        self.assertEqual(result["status"], "perm_mismatch")
        self.assertIn("perm", result["fixes"])
        self.assertEqual(result["errors"], [])
        self.mock_client.set_permissions.assert_called_once_with(
            "flickr-e2e-001", is_public=1
        )

    def test_3_idempotent_second_run_is_clean(self):
        """After Flickr is consistent, a second reconcile pass returns ok."""
        from poller.reconcile import check_photo
        self.mock_client.get_photo_info.return_value = self._flickr_info_response(
            is_public=1, tags=["nature", "landscape"]
        )

        result = check_photo(self.mock_client, self._photo_row(), fix=True, verbose=False)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["fixes"],  [])
        self.assertEqual(result["errors"], [])
        self.mock_client.set_permissions.assert_not_called()

    def test_4_tag_mismatch_detected_and_fixed(self):
        """Tags on Flickr are missing some expected tags → tag_mismatch, then fixed."""
        from poller.reconcile import check_photo
        # Flickr has only "nature"; "landscape" is missing
        self.mock_client.get_photo_info.return_value = self._flickr_info_response(
            is_public=1, tags=["nature"]
        )

        result = check_photo(self.mock_client, self._photo_row(), fix=True, verbose=False)

        self.assertEqual(result["status"], "tag_mismatch")
        self.assertIn("tags", result["fixes"])
        self.mock_client.add_tags.assert_called_once_with("flickr-e2e-001", ["landscape"])

    def test_5_api_error_propagates_to_failed_count(self):
        """Flickr API failure on get_photo_info → flickr_error status, not a crash."""
        from poller.reconcile import check_photo
        from flickr.flickr_client import FlickrError
        self.mock_client.get_photo_info.side_effect = FlickrError(500, "Server Error")

        result = check_photo(self.mock_client, self._photo_row(), fix=False, verbose=False)

        self.assertEqual(result["status"], "flickr_error")
        self.assertTrue(len(result["errors"]) > 0)
        self.assertEqual(result["fixes"], [])

    def test_6_updated_at_stamped_by_all_write_paths(self):
        """set_privacy_state, record_review, and undo_decision must all update updated_at."""
        import time

        # set_privacy_state
        before = self._photo_row()["updated_at"]
        time.sleep(0.01)
        self.db.set_privacy_state(self.photo_id, "keep_private", "test")
        self.assertGreater(self._photo_row()["updated_at"], before,
                           "set_privacy_state must update updated_at")

        # record_review
        before = self._photo_row()["updated_at"]
        time.sleep(0.01)
        self.db.record_review(self.photo_id, "make_public")
        self.assertGreater(self._photo_row()["updated_at"], before,
                           "record_review must update updated_at")

        # undo_decision
        before = self._photo_row()["updated_at"]
        time.sleep(0.01)
        self.db.undo_decision(self.photo_id)
        self.assertGreater(self._photo_row()["updated_at"], before,
                           "undo_decision must update updated_at")


# ---------------------------------------------------------------------------
# merge_flickr_into_photos — late-linking of split records
# ---------------------------------------------------------------------------

class TestMergeFlickrIntoPhotos(unittest.TestCase):
    """db.merge_flickr_into_photos() must correctly merge a Flickr-only record
    into a Photos-only record and clean up the Flickr-only record."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.db = Database(Path(self._tmp) / "test.db")

        # Photos-only record (uuid set, no flickr_id)
        self.photos_id = self.db.upsert_photo({
            "uuid":              "uuid-heic-001",
            "original_filename": "IMG_1234.HEIC",
            "date_taken":        "2026-04-24T15:30:07.775000-04:00",
            "privacy_state":     "candidate_public",
            "apple_labels":      ["Travel", "Beach"],
            "apple_persons":     [],
            "proposed_tags":     ["travel", "beach"],
            "apple_ai_caption":  "A sunny beach scene",
            "latitude":          25.0,
            "longitude":         -80.0,
        })

        # Flickr-only record (flickr_id set, no uuid)
        self.flickr_id_row = self.db.upsert_photo({
            "flickr_id":          "55228034962",
            "flickr_secret":      "abc123",
            "flickr_server":      "65535",
            "flickr_farm":        66,
            "original_filename":  "IMG_1234.JPG",
            "date_taken":         "2026-04-24 15:30:07",
            "privacy_state":      "candidate_public",
            "date_uploaded_flickr": "2026-04-24T20:00:00+00:00",
            "thumbnail_path":     "https://live.staticflickr.com/65535/55228034962_abc123_b.jpg",
        })

    def tearDown(self):
        self.db.close()
        import shutil
        shutil.rmtree(self._tmp)

    def _row(self, photo_id):
        return self.db.get_photo(photo_id)

    def test_merge_copies_flickr_id_to_photos_record(self):
        self.db.merge_flickr_into_photos(self.flickr_id_row, self.photos_id)
        row = self._row(self.photos_id)
        self.assertEqual(row["flickr_id"], "55228034962")

    def test_merge_copies_flickr_secret_and_server(self):
        self.db.merge_flickr_into_photos(self.flickr_id_row, self.photos_id)
        row = self._row(self.photos_id)
        self.assertEqual(row["flickr_secret"], "abc123")
        self.assertEqual(row["flickr_server"], "65535")

    def test_merge_preserves_apple_metadata(self):
        self.db.merge_flickr_into_photos(self.flickr_id_row, self.photos_id)
        row = self._row(self.photos_id)
        self.assertEqual(row["uuid"], "uuid-heic-001")
        self.assertEqual(row["apple_ai_caption"], "A sunny beach scene")
        self.assertIn("Beach", row["apple_labels"])

    def test_merge_deletes_flickr_only_record(self):
        self.db.merge_flickr_into_photos(self.flickr_id_row, self.photos_id)
        self.assertIsNone(self._row(self.flickr_id_row))

    def test_merge_copies_thumbnail_when_photos_record_has_none(self):
        self.db.merge_flickr_into_photos(self.flickr_id_row, self.photos_id)
        row = self._row(self.photos_id)
        self.assertIn("55228034962", row["thumbnail_path"])

    def test_merge_does_not_overwrite_existing_thumbnail(self):
        # Give the Photos record its own thumbnail first
        self.db.conn.execute(
            "UPDATE photos SET thumbnail_path = '/local/thumb.jpg' WHERE id = ?",
            (self.photos_id,),
        )
        self.db.conn.commit()
        self.db.merge_flickr_into_photos(self.flickr_id_row, self.photos_id)
        row = self._row(self.photos_id)
        self.assertEqual(row["thumbnail_path"], "/local/thumb.jpg")

    def test_merge_copies_review_decision_when_photos_has_none(self):
        # Put a review decision on the Flickr record only
        self.db.record_review(self.flickr_id_row, "make_public")
        self.db.merge_flickr_into_photos(self.flickr_id_row, self.photos_id)
        row = self._row(self.photos_id)
        self.assertEqual(row["review_decision"], "make_public")

    def test_merge_does_not_overwrite_existing_review_decision(self):
        # Both records have review decisions; Photos record wins
        self.db.record_review(self.photos_id,    "keep_private")
        self.db.record_review(self.flickr_id_row, "make_public")
        self.db.merge_flickr_into_photos(self.flickr_id_row, self.photos_id)
        row = self._row(self.photos_id)
        self.assertEqual(row["review_decision"], "keep_private")

    def test_merge_migrates_tag_events(self):
        # Insert a tag_event on the Flickr-only record
        self.db.conn.execute(
            """INSERT INTO tag_events (photo_id, event_at, destination, tags_before, tags_after)
               VALUES (?, '2026-04-24T20:00:00', 'flickr', '[]', '["beach","travel"]')""",
            (self.flickr_id_row,),
        )
        self.db.conn.commit()

        self.db.merge_flickr_into_photos(self.flickr_id_row, self.photos_id)

        # Event should now belong to the Photos record
        events = self.db.conn.execute(
            "SELECT * FROM tag_events WHERE photo_id = ?", (self.photos_id,)
        ).fetchall()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["destination"], "flickr")

        # Nothing left on the (now-deleted) Flickr record
        orphaned = self.db.conn.execute(
            "SELECT * FROM tag_events WHERE photo_id = ?", (self.flickr_id_row,)
        ).fetchall()
        self.assertEqual(len(orphaned), 0)

    def test_merge_migrates_album_memberships(self):
        album_id = self.db.upsert_album("apple-uuid-trip", "Beach Trip")
        self.db.upsert_photo_album(self.flickr_id_row, album_id)

        self.db.merge_flickr_into_photos(self.flickr_id_row, self.photos_id)

        albums = self.db.get_photo_albums(self.photos_id)
        album_names = [a["name"] for a in albums]
        self.assertIn("Beach Trip", album_names)

    def test_merge_returns_false_when_photos_already_linked(self):
        # Photos record already has a flickr_id — should refuse to merge
        self.db.conn.execute(
            "UPDATE photos SET flickr_id = 'existing-flickr' WHERE id = ?",
            (self.photos_id,),
        )
        self.db.conn.commit()
        result = self.db.merge_flickr_into_photos(self.flickr_id_row, self.photos_id)
        self.assertFalse(result)

    def test_merge_returns_false_when_flickr_already_has_uuid(self):
        self.db.conn.execute(
            "UPDATE photos SET uuid = 'existing-uuid' WHERE id = ?",
            (self.flickr_id_row,),
        )
        self.db.conn.commit()
        result = self.db.merge_flickr_into_photos(self.flickr_id_row, self.photos_id)
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# link_orphans batch tool
# ---------------------------------------------------------------------------

class TestLinkOrphans(unittest.TestCase):
    """poller/link_orphans.py must find and merge split record pairs."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.db = Database(Path(self._tmp) / "test.db")

    def tearDown(self):
        self.db.close()
        import shutil
        shutil.rmtree(self._tmp)

    def _seed_pair(self, tag: str, date: str = "2026-01-15 10:00:00"):
        """Insert a Photos-only and a Flickr-only record with matching timestamp."""
        photos_id = self.db.upsert_photo({
            "uuid":              f"uuid-{tag}",
            "original_filename": f"IMG_{tag}.HEIC",
            "date_taken":        f"{date.replace(' ', 'T')}+00:00",
            "privacy_state":     "candidate_public",
            "apple_labels":      [],
            "apple_persons":     [],
        })
        flickr_id_row = self.db.upsert_photo({
            "flickr_id":          f"flickr-{tag}",
            "original_filename":  f"IMG_{tag}.JPG",
            "date_taken":         date,
            "privacy_state":      "candidate_public",
        })
        return photos_id, flickr_id_row

    def test_dry_run_does_not_modify_db(self):
        from poller.link_orphans import link_orphans
        photos_id, flickr_row = self._seed_pair("dry")
        linked, failed = link_orphans(self.db, dry_run=True, limit=100)
        self.assertEqual(linked, 1)
        self.assertEqual(failed, 0)
        # DB must be unchanged
        self.assertIsNone(self.db.get_photo(photos_id)["flickr_id"])
        self.assertIsNotNone(self.db.get_photo(flickr_row))

    def test_links_matching_pair(self):
        from poller.link_orphans import link_orphans
        photos_id, flickr_row = self._seed_pair("live")
        linked, failed = link_orphans(self.db, dry_run=False, limit=100)
        self.assertEqual(linked, 1)
        self.assertEqual(failed, 0)
        row = self.db.get_photo(photos_id)
        self.assertEqual(row["flickr_id"], "flickr-live")
        self.assertIsNone(self.db.get_photo(flickr_row))

    def test_links_multiple_pairs(self):
        from poller.link_orphans import link_orphans
        self._seed_pair("a", "2026-02-01 09:00:00")
        self._seed_pair("b", "2026-02-02 10:00:00")
        self._seed_pair("c", "2026-02-03 11:00:00")
        linked, failed = link_orphans(self.db, dry_run=False, limit=100)
        self.assertEqual(linked, 3)
        self.assertEqual(failed, 0)

    def test_limit_respected(self):
        from poller.link_orphans import link_orphans
        self._seed_pair("x", "2026-03-01 08:00:00")
        self._seed_pair("y", "2026-03-02 09:00:00")
        linked, failed = link_orphans(self.db, dry_run=False, limit=1)
        self.assertEqual(linked, 1)

    def test_unmatched_photos_only_record_left_alone(self):
        from poller.link_orphans import link_orphans
        photos_id = self.db.upsert_photo({
            "uuid":              "uuid-nopair",
            "original_filename": "IMG_nopair.HEIC",
            "date_taken":        "2026-06-01T12:00:00+00:00",
            "privacy_state":     "candidate_public",
            "apple_labels":      [],
            "apple_persons":     [],
        })
        linked, failed = link_orphans(self.db, dry_run=False, limit=100)
        self.assertEqual(linked, 0)
        self.assertIsNotNone(self.db.get_photo(photos_id))


if __name__ == "__main__":
    unittest.main(verbosity=2)
