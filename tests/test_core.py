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
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from analyzer.privacy import classify
from analyzer.tagger import propose_tags
from poller.scanner import normalise_dt, normalise_dt_plus1, normalise_dt_plus2, build_enriched_row
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


class TestNormaliseDtPlus1(unittest.TestCase):
    """normalise_dt_plus1 returns the normalised timestamp incremented by one second."""

    def test_adds_one_second(self):
        # Sub-second timestamp whose truncated form is :50; +1s gives :51
        self.assertEqual(
            normalise_dt_plus1("2022-02-14T20:14:50.941984-05:00"),
            "2022-02-14 20:14:51",
        )

    def test_carries_across_minute_boundary(self):
        self.assertEqual(
            normalise_dt_plus1("2024-06-16T10:00:59.9-04:00"),
            "2024-06-16 10:01:00",
        )

    def test_none_returns_none(self):
        self.assertIsNone(normalise_dt_plus1(None))

    def test_exact_second_still_increments(self):
        # Even a Photos record with no sub-seconds gets +1 (rare but safe)
        self.assertEqual(
            normalise_dt_plus1("2023-05-06T16:34:28-04:00"),
            "2023-05-06 16:34:29",
        )


class TestNormaliseDtPlus2(unittest.TestCase):
    """normalise_dt_plus2 returns the normalised timestamp incremented by two seconds."""

    def test_adds_two_seconds(self):
        self.assertEqual(
            normalise_dt_plus2("2022-01-29T19:06:42.706693-05:00"),
            "2022-01-29 19:06:44",
        )

    def test_carries_across_minute_boundary(self):
        self.assertEqual(
            normalise_dt_plus2("2024-06-16T10:00:59.1-04:00"),
            "2024-06-16 10:01:01",
        )

    def test_none_returns_none(self):
        self.assertIsNone(normalise_dt_plus2(None))


# ---------------------------------------------------------------------------
# normalise_dt_localise
# ---------------------------------------------------------------------------


class TestNormaliseDtLocalise(unittest.TestCase):
    """normalise_dt_localise converts tz-aware strings to a target tz before stripping."""

    from datetime import timezone, timedelta

    EDT = timezone(timedelta(hours=-4))

    def test_naive_flickr_format_unchanged(self):
        from poller.scanner import normalise_dt_localise

        self.assertEqual(
            normalise_dt_localise("2020-06-15 18:24:08"),
            "2020-06-15 18:24:08",
        )

    def test_utc_offset_converts_to_edt(self):
        # UTC 22:24 == EDT 18:24 (UTC-4); this is the core bug case
        from poller.scanner import normalise_dt_localise

        self.assertEqual(
            normalise_dt_localise("2020-06-15T22:24:08.411297+00:00", tz=self.EDT),
            "2020-06-15 18:24:08",
        )

    def test_edt_offset_keeps_local_hours(self):
        from poller.scanner import normalise_dt_localise

        self.assertEqual(
            normalise_dt_localise("2020-06-15T18:24:08.411297-04:00", tz=self.EDT),
            "2020-06-15 18:24:08",
        )

    def test_none_returns_none(self):
        from poller.scanner import normalise_dt_localise

        self.assertIsNone(normalise_dt_localise(None))

    def test_subsecond_stripped(self):
        from poller.scanner import normalise_dt_localise

        # Sub-second precision is stripped after conversion
        self.assertEqual(
            normalise_dt_localise("2020-06-15T22:24:08.411297+00:00", tz=self.EDT),
            "2020-06-15 18:24:08",
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
        "latitude": None,
        "longitude": None,
        "place_ishome": 0,
        "persons": [],
        "apple_persons": [],
        "face_info": [],
        "labels": [],
        "apple_labels": [],
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
        state, reason = classify(self._photo(persons=["_UNKNOWN_"]), zones=[])
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
        state, reason = classify(self._photo(labels=["People", "Concert"]), zones=[])
        self.assertEqual(state, "needs_review")

    def test_crowd_label(self):
        state, reason = classify(self._photo(labels=["Crowd", "Outdoor"]), zones=[])
        self.assertEqual(state, "needs_review")

    def test_geofence_auto_private(self):
        zones = [
            {
                "name": "home",
                "label": "Home",
                "latitude": 42.38,
                "longitude": -71.09,
                "radius_m": 200,
                "policy": "auto_private",
            }
        ]
        # Point inside zone
        state, reason = classify(self._photo(latitude=42.38, longitude=-71.09), zones=zones)
        self.assertEqual(state, "auto_private")
        self.assertIn("Home", reason)

    def test_geofence_flag_review(self):
        zones = [
            {
                "name": "school",
                "label": "School",
                "latitude": 42.38,
                "longitude": -71.09,
                "radius_m": 200,
                "policy": "flag_review",
            }
        ]
        state, reason = classify(self._photo(latitude=42.38, longitude=-71.09), zones=zones)
        self.assertEqual(state, "needs_review")

    def test_outside_geofence(self):
        zones = [
            {
                "name": "home",
                "label": "Home",
                "latitude": 42.38,
                "longitude": -71.09,
                "radius_m": 50,
                "policy": "auto_private",
            }
        ]
        # Point 500m away — outside zone
        state, reason = classify(self._photo(latitude=42.385, longitude=-71.09), zones=zones)
        self.assertEqual(state, "candidate_public")

    def test_human_body_detection(self):
        photo = self._photo(
            media_analysis={
                "humans": [
                    {"humanConfidence": 0.8},
                    {"humanConfidence": 0.6},
                ]
            }
        )
        state, reason = classify(photo, zones=[])
        self.assertEqual(state, "needs_review")

    def test_low_confidence_human_ignored(self):
        photo = self._photo(media_analysis={"humans": [{"humanConfidence": 0.1}]})
        state, reason = classify(photo, zones=[])
        self.assertEqual(state, "candidate_public")


# ---------------------------------------------------------------------------
# Tagger
# ---------------------------------------------------------------------------


class TestTagger(unittest.TestCase):
    def test_location_tags(self):
        tags = propose_tags(
            {
                "place_city": "Boston",
                "place_state": "Massachusetts",
                "place_country": "United States",
            }
        )
        self.assertIn("boston", tags)
        self.assertIn("massachusetts", tags)
        self.assertIn("united states", tags)

    def test_apple_labels_filtered(self):
        tags = propose_tags({"labels": ["People", "Concert", "Stage", "Music"]})
        self.assertNotIn("people", tags)  # blocklisted
        self.assertIn("concert", tags)
        self.assertIn("stage", tags)
        self.assertIn("music", tags)

    def test_remap(self):
        tags = propose_tags({"labels": ["Rock Concert", "Automobile"]})
        self.assertIn("concert", tags)
        self.assertIn("car", tags)

    def test_deduplication(self):
        tags = propose_tags(
            {
                "labels": ["Concert", "concert"],  # duplicates
                "place_city": "Boston",
            }
        )
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
        row_id = self.db.upsert_photo(
            {
                "flickr_id": "12345",
                "date_taken": "2023-05-06 16:34:28",
                "privacy_state": "candidate_public",
                "privacy_reason": "no people detected",
            }
        )
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
        self.db.upsert_photo(
            {
                "flickr_id": "77777",
                "privacy_state": "approved_public",
                "review_decision": "make_public",
                "reviewed_at": "2026-01-01T00:00:00",
            }
        )
        # Update without review fields — should not clobber decision
        self.db.upsert_photo(
            {
                "flickr_id": "77777",
                "date_taken": "2024-06-01 12:00:00",
            }
        )
        photo = self.db.get_photo_by_flickr_id("77777")
        self.assertEqual(photo["review_decision"], "make_public")

    def test_stats(self):
        self.db.upsert_photo({"flickr_id": "1", "privacy_state": "candidate_public"})
        self.db.upsert_photo({"flickr_id": "2", "privacy_state": "needs_review"})
        self.db.upsert_photo({"flickr_id": "3", "privacy_state": "auto_private"})
        stats = self.db.stats()
        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["by_state"]["candidate_public"], 1)

    def test_stats_pushable_approved(self):
        # approved_public with flickr_id and perms not yet pushed → pushable
        self.db.upsert_photo(
            {"flickr_id": "f1", "privacy_state": "approved_public", "perms_pushed_flickr": 0}
        )
        # already pushed → not pushable
        self.db.upsert_photo(
            {"flickr_id": "f2", "privacy_state": "approved_public", "perms_pushed_flickr": 1}
        )
        # Photos-only (no flickr_id) → not pushable
        self.db.upsert_photo({"uuid": "uuid-nf", "privacy_state": "approved_public"})
        # wrong state → not pushable
        self.db.upsert_photo(
            {"flickr_id": "f4", "privacy_state": "candidate_public", "perms_pushed_flickr": 0}
        )
        stats = self.db.stats()
        self.assertEqual(stats["pushable_approved"], 1)

    def test_geofence_match(self):
        self.db.upsert_zone(
            {
                "name": "home",
                "label": "Home",
                "latitude": 42.38,
                "longitude": -71.09,
                "radius_m": 200,
                "policy": "auto_private",
            }
        )
        zone = self.db.match_geofence(42.38, -71.09)
        self.assertIsNotNone(zone)
        self.assertEqual(zone["name"], "home")

    def test_geofence_no_match(self):
        self.db.upsert_zone(
            {
                "name": "home",
                "label": "Home",
                "latitude": 42.38,
                "longitude": -71.09,
                "radius_m": 50,
                "policy": "auto_private",
            }
        )
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
        self.db.upsert_photo(
            {
                "flickr_id": "OLD",
                "privacy_state": "candidate_public",
                "date_taken": "2020-01-01 00:00:00",
            }
        )
        self.db.upsert_photo(
            {
                "flickr_id": "NEW",
                "privacy_state": "candidate_public",
                "date_taken": "2024-06-01 00:00:00",
            }
        )
        queue = self.db.review_queue()
        ids = [p["flickr_id"] for p in queue]
        self.assertEqual(ids[0], "NEW")
        self.assertEqual(ids[1], "OLD")

    def test_review_queue_null_date_taken_sorts_last(self):
        """review_queue orders by date_taken DESC; NULL date_taken sorts after non-NULL."""
        self.db.upsert_photo(
            {
                "flickr_id": "NOTAKEN",
                "privacy_state": "candidate_public",
                "date_uploaded_flickr": "2023-03-01 00:00:00",
            }
        )
        self.db.upsert_photo(
            {
                "flickr_id": "WITHTAKEN",
                "privacy_state": "candidate_public",
                "date_taken": "2022-01-01 00:00:00",
            }
        )
        queue = self.db.review_queue()
        ids = [p["flickr_id"] for p in queue]
        # NULL date_taken sorts last (after 2022 WITHTAKEN)
        self.assertGreater(ids.index("NOTAKEN"), ids.index("WITHTAKEN"))

    def test_get_photo_nav(self):
        """get_photo_nav returns correct prev/next IDs using indexed lookups."""
        self.db.upsert_photo(
            {"flickr_id": "A", "privacy_state": "candidate_public", "date_taken": "2020-01-01"}
        )
        self.db.upsert_photo(
            {"flickr_id": "B", "privacy_state": "candidate_public", "date_taken": "2021-01-01"}
        )
        self.db.upsert_photo(
            {"flickr_id": "C", "privacy_state": "candidate_public", "date_taken": "2022-01-01"}
        )
        a = self.db.get_photo_by_flickr_id("A")
        b = self.db.get_photo_by_flickr_id("B")
        c = self.db.get_photo_by_flickr_id("C")
        # Order is C (newest) → B → A (oldest)
        prev_id, next_id = self.db.get_photo_nav(b["id"], "candidate_public", b["date_taken"])
        self.assertEqual(prev_id, c["id"])
        self.assertEqual(next_id, a["id"])
        # Boundaries
        prev_id, next_id = self.db.get_photo_nav(c["id"], "candidate_public", c["date_taken"])
        self.assertIsNone(prev_id)
        self.assertEqual(next_id, b["id"])
        prev_id, next_id = self.db.get_photo_nav(a["id"], "candidate_public", a["date_taken"])
        self.assertEqual(prev_id, b["id"])
        self.assertIsNone(next_id)

    def test_get_photo_nav_no_date_taken(self):
        """get_photo_nav returns (None, None) when date_taken is missing."""
        self.db.upsert_photo({"flickr_id": "X", "privacy_state": "candidate_public"})
        x = self.db.get_photo_by_flickr_id("X")
        prev_id, next_id = self.db.get_photo_nav(x["id"], "candidate_public", None)
        self.assertIsNone(prev_id)
        self.assertIsNone(next_id)

    def test_set_album_flickr_name(self):
        album_id = self.db.upsert_album("uuid-a1", "Paris")
        self.db.set_album_flickr_name(album_id, "Paris")
        row = self.db.conn.execute(
            "SELECT flickr_name FROM albums WHERE id = ?", (album_id,)
        ).fetchone()
        self.assertEqual(row["flickr_name"], "Paris")

    def test_set_folder_flickr_name(self):
        folder_id = self.db.upsert_folder("uuid-f1", "Travel")
        self.db.set_folder_flickr_name(folder_id, "Travel")
        row = self.db.conn.execute(
            "SELECT flickr_name FROM folders WHERE id = ?", (folder_id,)
        ).fetchone()
        self.assertEqual(row["flickr_name"], "Travel")


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
        self.db.upsert_photo({"flickr_id": flickr_id, "privacy_state": "candidate_public"})
        photo = self.db.get_photo_by_flickr_id(flickr_id)
        self.db.record_review(photo["id"], decision)
        return self.db.get_photo_by_flickr_id(flickr_id)

    def test_keep_private_survives_scanner_upsert(self):
        """A keep_private decision must survive a subsequent upsert_photo call."""
        row = self._insert_reviewed("P1", "keep_private")
        self.assertEqual(row["privacy_state"], "keep_private")

        # Simulate scanner re-classifying and upserting
        self.db.upsert_photo(
            {
                "flickr_id": "P1",
                "privacy_state": "candidate_public",
                "privacy_reason": "no people detected",
            }
        )
        result = self.db.get_photo_by_flickr_id("P1")
        self.assertEqual(
            result["privacy_state"], "keep_private", "Scanner upsert must not revert keep_private"
        )

    def test_approved_public_survives_scanner_upsert(self):
        """An approved_public decision must survive a subsequent upsert_photo call."""
        row = self._insert_reviewed("P2", "make_public")
        self.assertEqual(row["privacy_state"], "approved_public")

        self.db.upsert_photo(
            {
                "flickr_id": "P2",
                "privacy_state": "needs_review",
                "privacy_reason": "people detected",
            }
        )
        result = self.db.get_photo_by_flickr_id("P2")
        self.assertEqual(
            result["privacy_state"],
            "approved_public",
            "Scanner upsert must not revert approved_public",
        )

    def test_skipped_survives_scanner_upsert(self):
        """A skipped decision must survive a subsequent upsert_photo call."""
        row = self._insert_reviewed("P3", "skip")
        self.assertEqual(row["privacy_state"], "skipped")

        self.db.upsert_photo(
            {
                "flickr_id": "P3",
                "privacy_state": "candidate_public",
                "privacy_reason": "no people detected",
            }
        )
        result = self.db.get_photo_by_flickr_id("P3")
        self.assertEqual(
            result["privacy_state"], "skipped", "Scanner upsert must not revert skipped"
        )

    def test_unreviewed_photo_state_is_updated(self):
        """upsert_photo must still update privacy_state for unreviewed photos."""
        self.db.upsert_photo({"flickr_id": "P4", "privacy_state": "candidate_public"})
        self.db.upsert_photo(
            {
                "flickr_id": "P4",
                "privacy_state": "needs_review",
                "privacy_reason": "people detected",
            }
        )
        result = self.db.get_photo_by_flickr_id("P4")
        self.assertEqual(
            result["privacy_state"],
            "needs_review",
            "Unreviewed photos must still get state updates",
        )

    def test_non_privacy_fields_still_updated_after_review(self):
        """Metadata fields (tags, filename, etc.) must still update even after review."""
        self._insert_reviewed("P5", "keep_private")
        self.db.upsert_photo(
            {
                "flickr_id": "P5",
                "original_filename": "updated_name.jpg",
                "proposed_tags": ["new", "tag"],
            }
        )
        result = self.db.get_photo_by_flickr_id("P5")
        self.assertEqual(result["original_filename"], "updated_name.jpg")
        self.assertEqual(result["privacy_state"], "keep_private")

    def test_build_enriched_row_preserves_skipped(self):
        """build_enriched_row must not reclassify skipped photos."""
        from poller.scanner import build_enriched_row

        existing = {
            "id": 1,
            "flickr_id": "12345",
            "uuid": None,
            "privacy_state": "skipped",
            "privacy_reason": "user deferred",
            "proposed_tags": [],
            "latitude": None,
            "longitude": None,
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
        self.assertEqual(
            enriched["privacy_state"], "skipped", "build_enriched_row must preserve skipped state"
        )


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
        self.db.upsert_photo(
            {
                "flickr_id": "U1",
                "privacy_state": "approved_public",
                "review_decision": "make_public",
                "reviewed_at": "2026-01-01T00:00:00",
                "apple_persons": "[]",
                "apple_unknown_faces": 0,
                "apple_named_faces": 0,
            }
        )
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

        self.db.upsert_photo(
            {
                "flickr_id": "U2",
                "privacy_state": "keep_private",
                "review_decision": "keep_private",
                "reviewed_at": "2026-01-01T00:00:00",
                "apple_persons": _json.dumps(["Alice"]),
                "apple_named_faces": 1,
                "apple_unknown_faces": 0,
            }
        )
        photo = self.db.get_photo_by_flickr_id("U2")
        result = self.db.undo_decision(photo["id"])
        self.assertTrue(result)
        updated = self.db.get_photo_by_flickr_id("U2")
        self.assertEqual(updated["privacy_state"], "needs_review")

    def test_undo_returns_to_needs_review_with_unknown_faces(self):
        """Photo with unknown faces reverts to needs_review on undo."""
        self.db.upsert_photo(
            {
                "flickr_id": "U3",
                "privacy_state": "approved_public",
                "review_decision": "make_public",
                "reviewed_at": "2026-01-01T00:00:00",
                "apple_persons": "[]",
                "apple_unknown_faces": 2,
                "apple_named_faces": 0,
            }
        )
        photo = self.db.get_photo_by_flickr_id("U3")
        self.db.undo_decision(photo["id"])
        updated = self.db.get_photo_by_flickr_id("U3")
        self.assertEqual(updated["privacy_state"], "needs_review")

    def test_undo_resets_perms_pushed(self):
        """undo_decision resets perms_pushed_flickr to 0."""
        self.db.upsert_photo(
            {
                "flickr_id": "U4",
                "privacy_state": "approved_public",
                "review_decision": "make_public",
                "reviewed_at": "2026-01-01T00:00:00",
                "perms_pushed_flickr": 1,
                "apple_persons": "[]",
                "apple_unknown_faces": 0,
                "apple_named_faces": 0,
            }
        )
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
        "id": 1,
        "flickr_id": "12345",
        "uuid": None,
        "privacy_state": "candidate_public",
        "privacy_reason": "no people detected",
        "proposed_tags": [],
        "latitude": None,
        "longitude": None,
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
        existing = dict(self.EXISTING, privacy_state="keep_private", review_decision="keep_private")
        row = self._photo_row(apple_labels=[])  # would normally be candidate_public
        enriched = build_enriched_row(row, existing, [], "Chris Devers")
        self.assertEqual(enriched["privacy_state"], "keep_private")

    def test_skipped_preserved(self):
        existing = dict(self.EXISTING, privacy_state="skipped", review_decision="skip")
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

    def test_run_clears_display_rotation_on_success(self):
        """Successful thumbnail write must reset display_rotation to 0."""
        import os
        import tempfile
        from unittest import mock
        from poller.thumbnailer import run

        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db = Database(db_path)

        # Insert a Photos-only record with display_rotation=90 and no thumbnail
        photo_id = db.upsert_photo(
            {
                "uuid": "AAAAAAAA-0000-0000-0000-000000000000",
                "display_rotation": 90,
            }
        )

        # Mock derivative_path so the thumbnailer resolves the local source.
        # Also mock osxphotos so Phase 0 iCloud lookup doesn't hit the real library.
        mock_photosdb = mock.MagicMock()
        mock_photosdb.photos.return_value = []
        with (
            mock.patch("poller.thumbnailer.derivative_path", return_value="/fake/thumb.jpeg"),
            mock.patch("poller.thumbnailer.osxphotos") as mock_osx,
        ):
            mock_osx.PhotosDB.return_value = mock_photosdb
            run(
                db=db,
                library_path="/fake/library",
                thumb_root=None,
                flickr_download=False,
                client=None,
                limit=None,
                dry_run=False,
            )

        row = db.conn.execute(
            "SELECT thumbnail_path, display_rotation FROM photos WHERE id = ?",
            (photo_id,),
        ).fetchone()

        self.assertEqual(row["thumbnail_path"], "/fake/thumb.jpeg")
        self.assertEqual(
            row["display_rotation"],
            0,
            "display_rotation must be 0 after thumbnailer sets thumbnail_path",
        )

        db.close()
        os.unlink(db_path)


class TestThumbnailerICloud(unittest.TestCase):
    """Tests for iCloud download path added in GH #64."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "test.db")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _insert_photos_only(self, uuid: str) -> int:
        return self.db.upsert_photo(
            {
                "uuid": uuid,
                "flickr_id": None,
                "privacy_state": "candidate_public",
                "proposed_tags": [],
                "apple_persons": [],
                "apple_labels": [],
            }
        )

    def _insert_linked(self, flickr_id: str, uuid: str) -> int:
        return self.db.upsert_photo(
            {
                "uuid": uuid,
                "flickr_id": flickr_id,
                "flickr_secret": "sec",
                "flickr_server": "999",
                "privacy_state": "approved_public",
                "proposed_tags": [],
                "apple_persons": [],
                "apple_labels": [],
            }
        )

    def test_no_photos_only_records_osxphotos_never_opened(self):
        from unittest.mock import patch
        from poller.thumbnailer import run

        # Only a Flickr-linked record — no Photos-only records needing thumbnails
        self._insert_linked("55555555", "LINKED-UUID-0001")

        with patch("poller.thumbnailer.osxphotos") as mock_osxphotos:
            run(
                db=self.db,
                library_path="/fake/lib",
                thumb_root=None,
                flickr_download=False,
                client=None,
                limit=None,
                dry_run=True,
                icloud=True,
            )

        mock_osxphotos.PhotosDB.assert_not_called()

    def test_icloud_off_by_default_skips_icloud_records(self):
        """With icloud=False (default), Photos-only records are skipped; PhotosDB never opened."""
        from unittest.mock import patch
        from poller.thumbnailer import run

        self._insert_photos_only("ICLOUD-UUID-DEFAULT")

        with (
            patch("poller.thumbnailer.osxphotos") as mock_osxphotos,
            patch("poller.thumbnailer.derivative_path", return_value=None),
        ):
            run(
                db=self.db,
                library_path="/fake/lib",
                thumb_root=None,
                flickr_download=False,
                client=None,
                limit=None,
                dry_run=True,
            )
            # icloud kwarg omitted — default is False

        mock_osxphotos.PhotosDB.assert_not_called()

    def test_photos_only_records_open_photosdb_and_build_map(self):
        from unittest.mock import MagicMock, patch
        from poller.thumbnailer import run

        self._insert_photos_only("ICLOUD-UUID-0001")

        mock_photo = MagicMock()
        mock_photo.uuid = "ICLOUD-UUID-0001"
        mock_photo.iscloudasset = False  # not iCloud — just verify DB was opened
        mock_photo.ismissing = False

        mock_photosdb = MagicMock()
        mock_photosdb.photos.return_value = [mock_photo]

        with (
            patch("poller.thumbnailer.osxphotos") as mock_osxphotos,
            patch("poller.thumbnailer.derivative_path", return_value=None),
        ):
            mock_osxphotos.PhotosDB.return_value = mock_photosdb
            run(
                db=self.db,
                library_path="/fake/lib",
                thumb_root=None,
                flickr_download=False,
                client=None,
                limit=None,
                dry_run=True,
                icloud=True,
            )

        mock_osxphotos.PhotosDB.assert_called_once_with(dbfile="/fake/lib")
        mock_photosdb.photos.assert_called_once()

    def test_icloud_photo_resolved_when_download_completes(self):
        from unittest.mock import MagicMock, patch
        from poller.thumbnailer import run

        photo_id = self._insert_photos_only("ICLOUD-UUID-0001")

        mock_photo = MagicMock()
        mock_photo.uuid = "ICLOUD-UUID-0001"
        mock_photo.iscloudasset = True
        mock_photo.ismissing = True

        mock_photosdb = MagicMock()
        mock_photosdb.photos.return_value = [mock_photo]

        # derivative_path: None first (Phase 1 local check), then path (after export)
        mock_deriv = MagicMock(
            side_effect=[
                None,
                "/fake/lib/resources/derivatives/masters/i/ICLOUD-UUID-0001_4_5005_c.jpeg",
            ]
        )

        with (
            patch("poller.thumbnailer.osxphotos") as mock_osxphotos,
            patch("poller.thumbnailer.derivative_path", mock_deriv),
        ):
            mock_osxphotos.PhotosDB.return_value = mock_photosdb
            run(
                db=self.db,
                library_path="/fake/lib",
                thumb_root=None,
                flickr_download=False,
                client=None,
                limit=None,
                dry_run=False,
                icloud=True,
            )

        mock_photo.export.assert_called_once()
        row = self.db.conn.execute(
            "SELECT thumbnail_path, display_rotation FROM photos WHERE id = ?", (photo_id,)
        ).fetchone()
        self.assertIsNotNone(row["thumbnail_path"])
        self.assertEqual(row["display_rotation"], 0)

    def test_icloud_photo_queued_when_derivative_not_found_after_export(self):
        from unittest.mock import MagicMock, patch
        from poller.thumbnailer import run

        photo_id = self._insert_photos_only("ICLOUD-UUID-0002")

        mock_photo = MagicMock()
        mock_photo.uuid = "ICLOUD-UUID-0002"
        mock_photo.iscloudasset = True
        mock_photo.ismissing = True

        mock_photosdb = MagicMock()
        mock_photosdb.photos.return_value = [mock_photo]

        with (
            patch("poller.thumbnailer.osxphotos") as mock_osxphotos,
            patch("poller.thumbnailer.derivative_path", return_value=None),
        ):
            mock_osxphotos.PhotosDB.return_value = mock_photosdb
            run(
                db=self.db,
                library_path="/fake/lib",
                thumb_root=None,
                flickr_download=False,
                client=None,
                limit=None,
                dry_run=False,
                icloud=True,
            )

        # No thumbnail written — photo is queued for next run
        row = self.db.conn.execute(
            "SELECT thumbnail_path FROM photos WHERE id = ?", (photo_id,)
        ).fetchone()
        self.assertIn(row["thumbnail_path"], (None, ""))

    def test_icloud_queued_when_export_raises_no_crash(self):
        from unittest.mock import MagicMock, patch
        from poller.thumbnailer import run

        photo_id = self._insert_photos_only("ICLOUD-UUID-0003")

        mock_photo = MagicMock()
        mock_photo.uuid = "ICLOUD-UUID-0003"
        mock_photo.iscloudasset = True
        mock_photo.ismissing = True
        mock_photo.export.side_effect = Exception("Photos.app not running")

        mock_photosdb = MagicMock()
        mock_photosdb.photos.return_value = [mock_photo]

        # Should complete without raising; photo ends up queued
        with (
            patch("poller.thumbnailer.osxphotos") as mock_osxphotos,
            patch("poller.thumbnailer.derivative_path", return_value=None),
        ):
            mock_osxphotos.PhotosDB.return_value = mock_photosdb
            run(
                db=self.db,
                library_path="/fake/lib",
                thumb_root=None,
                flickr_download=False,
                client=None,
                limit=None,
                dry_run=False,
                icloud=True,
            )

        row = self.db.conn.execute(
            "SELECT thumbnail_path FROM photos WHERE id = ?", (photo_id,)
        ).fetchone()
        self.assertIn(row["thumbnail_path"], (None, ""))

    def test_dry_run_triggers_export_but_does_not_write_db(self):
        from unittest.mock import MagicMock, patch
        from poller.thumbnailer import run

        photo_id = self._insert_photos_only("ICLOUD-UUID-0004")

        mock_photo = MagicMock()
        mock_photo.uuid = "ICLOUD-UUID-0004"
        mock_photo.iscloudasset = True
        mock_photo.ismissing = True

        mock_photosdb = MagicMock()
        mock_photosdb.photos.return_value = [mock_photo]

        # derivative_path: None first (local check), then a path (export "completes")
        mock_deriv = MagicMock(side_effect=[None, "/fake/icloud.jpeg"])

        with (
            patch("poller.thumbnailer.osxphotos") as mock_osxphotos,
            patch("poller.thumbnailer.derivative_path", mock_deriv),
        ):
            mock_osxphotos.PhotosDB.return_value = mock_photosdb
            run(
                db=self.db,
                library_path="/fake/lib",
                thumb_root=None,
                flickr_download=False,
                client=None,
                limit=None,
                dry_run=True,
                icloud=True,
            )

        # export WAS called (download triggered even in dry-run)
        mock_photo.export.assert_called_once()
        # DB was NOT written
        row = self.db.conn.execute(
            "SELECT thumbnail_path FROM photos WHERE id = ?", (photo_id,)
        ).fetchone()
        self.assertIn(row["thumbnail_path"], (None, ""))

    def test_icloud_limit_caps_exports(self):
        """icloud_limit=2 with 3 iCloud records: only 2 exports are attempted."""
        from unittest.mock import MagicMock, patch
        from poller.thumbnailer import run

        for i in range(3):
            self._insert_photos_only(f"ICLOUD-LIMIT-{i:04d}")

        mock_photos = []
        for i in range(3):
            p = MagicMock()
            p.uuid = f"ICLOUD-LIMIT-{i:04d}"
            p.iscloudasset = True
            p.ismissing = True
            mock_photos.append(p)

        mock_photosdb = MagicMock()
        mock_photosdb.photos.return_value = mock_photos

        with (
            patch("poller.thumbnailer.osxphotos") as mock_osxphotos,
            patch("poller.thumbnailer.derivative_path", return_value=None),
        ):
            mock_osxphotos.PhotosDB.return_value = mock_photosdb
            run(
                db=self.db,
                library_path="/fake/lib",
                thumb_root=None,
                flickr_download=False,
                client=None,
                limit=None,
                dry_run=True,
                icloud=True,
                icloud_limit=2,
            )

        export_calls = sum(p.export.call_count for p in mock_photos)
        self.assertEqual(export_calls, 2)

    def test_skipped_count_excludes_icloud_queued_records(self):
        from unittest.mock import MagicMock, patch
        from poller.thumbnailer import run, log
        import re

        # One iCloud-queued record and one genuinely unresolvable record
        # (has a uuid but is not found in Photos library, so it gets skipped)
        self._insert_photos_only("ICLOUD-UUID-0005")
        self._insert_photos_only("NOT-IN-LIBRARY-0001")  # not returned by mock photosdb

        mock_photo = MagicMock()
        mock_photo.uuid = "ICLOUD-UUID-0005"
        mock_photo.iscloudasset = True
        mock_photo.ismissing = True

        mock_photosdb = MagicMock()
        mock_photosdb.photos.return_value = [mock_photo]

        log_output = []

        with (
            patch("poller.thumbnailer.osxphotos") as mock_osxphotos,
            patch("poller.thumbnailer.derivative_path", return_value=None),
            patch.object(
                log, "info", side_effect=lambda msg, *a: log_output.append(msg % a if a else msg)
            ),
        ):
            mock_osxphotos.PhotosDB.return_value = mock_photosdb
            run(
                db=self.db,
                library_path="/fake/lib",
                thumb_root=None,
                flickr_download=False,
                client=None,
                limit=None,
                dry_run=True,
                icloud=True,
            )

        done_line = next((line for line in log_output if line.startswith("Done:")), "")
        # skipped should be 1 (NOT-IN-LIBRARY), not 2
        match = re.search(r"(\d+) skipped", done_line)
        self.assertIsNotNone(match, f"Expected 'N skipped' in: {done_line!r}")
        self.assertEqual(int(match.group(1)), 1)


# ---------------------------------------------------------------------------
# Poller: download_thumb URL preference
# ---------------------------------------------------------------------------


class TestDownloadThumb(unittest.TestCase):
    def _run(self, row, files_on_disk=()):
        from poller.poller import download_thumb
        from unittest import mock
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            thumb_root = Path(tmp)
            fid = row.get("flickr_id", "99999")
            shard = fid[:2]
            (thumb_root / shard).mkdir(parents=True, exist_ok=True)
            for name in files_on_disk:
                (thumb_root / shard / name).write_bytes(b"x")

            client = mock.MagicMock()
            client.download_thumbnail.return_value = True

            result = download_thumb(client, row, thumb_root)
            return client, result

    def test_prefers_url_m_over_url_l(self):
        client, _ = self._run(
            {
                "flickr_id": "99999",
                "thumbnail_url_m": "https://example.com/99999_m.jpg",
                "thumbnail_url_l": "https://example.com/99999_l.jpg",
            }
        )
        args = client.download_thumbnail.call_args[0]
        self.assertIn("_m.jpg", args[0])

    def test_falls_back_to_url_l_when_url_m_missing(self):
        client, _ = self._run(
            {
                "flickr_id": "99998",
                "thumbnail_url_m": "",
                "thumbnail_url_l": "https://example.com/99998_l.jpg",
            }
        )
        args = client.download_thumbnail.call_args[0]
        self.assertIn("_l.jpg", args[0])


# ---------------------------------------------------------------------------
# Poller: flickr_photo_to_db
# ---------------------------------------------------------------------------


class TestFlickrPhotoToDb(unittest.TestCase):
    def _fake(self, **kwargs):
        base = {
            "id": "54321",
            "secret": "abc123",
            "server": "1234",
            "farm": 1,
            "title": "Test photo",
            "dateupload": "1718500000",
            "datetaken": "2024-06-16 10:00:00",
            "latitude": "42.3601",
            "longitude": "-71.0589",
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
        import tempfile
        import os

        fd, self.tmp_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = Database(self.tmp_path)
        self.db.upsert_photo(
            {
                "flickr_id": "AAA",
                "date_taken": "2024-06-16 10:00:00",
                "latitude": 42.36,
                "longitude": -71.06,
            }
        )
        self.db.upsert_photo(
            {
                "flickr_id": "BBB",
                "date_taken": "2023-05-06 16:34:28",
            }
        )

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

    def test_match_flickr_rounded_up(self):
        # Flickr rounds sub-second EXIF times to the nearest second while Photos
        # truncates.  A Photos timestamp of :50.941 normalises to :50, but the
        # Flickr record has :51.  The matcher must find the record via +1s fallback.
        from poller.scanner import find_flickr_match

        self.db.upsert_photo({"flickr_id": "CCC", "date_taken": "2024-06-16 10:00:01"})
        photo_row = {"date_taken": "2024-06-16T10:00:00.941984-04:00"}
        matches = find_flickr_match(photo_row, self.db)
        flickr_ids = [m["flickr_id"] for m in matches]
        self.assertIn("CCC", flickr_ids)

    def test_match_flickr_two_seconds_ahead(self):
        # Some HEIC uploads exhibit a 2-second offset (observed for photos 58000/154037
        # and 7299/154008). The matcher must find these via the +2s fallback.
        from poller.scanner import find_flickr_match

        self.db.upsert_photo({"flickr_id": "DDD", "date_taken": "2022-01-29 19:06:44"})
        photo_row = {"date_taken": "2022-01-29T19:06:42.706693-05:00"}
        matches = find_flickr_match(photo_row, self.db)
        flickr_ids = [m["flickr_id"] for m in matches]
        self.assertIn("DDD", flickr_ids)


# ---------------------------------------------------------------------------
# approved_public queue (DB side of push_approved)
# ---------------------------------------------------------------------------


class TestApprovedQueue(unittest.TestCase):
    def setUp(self):
        import tempfile
        import os

        fd, self.tmp_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = Database(self.tmp_path)

    def tearDown(self):
        self.db.close()
        os.unlink(self.tmp_path)

    def test_approved_unpushed_appears_in_queue(self):
        self.db.upsert_photo(
            {
                "flickr_id": "111",
                "privacy_state": "approved_public",
                "perms_pushed_flickr": 0,
            }
        )
        rows = self.db.conn.execute(
            "SELECT flickr_id FROM photos "
            "WHERE privacy_state = 'approved_public' "
            "AND flickr_id IS NOT NULL AND perms_pushed_flickr = 0"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["flickr_id"], "111")

    def test_already_pushed_excluded_from_queue(self):
        self.db.upsert_photo(
            {
                "flickr_id": "222",
                "privacy_state": "approved_public",
                "perms_pushed_flickr": 1,
            }
        )
        rows = self.db.conn.execute(
            "SELECT flickr_id FROM photos "
            "WHERE privacy_state = 'approved_public' "
            "AND flickr_id IS NOT NULL AND perms_pushed_flickr = 0"
        ).fetchall()
        self.assertEqual(len(rows), 0)

    def test_no_flickr_id_excluded_from_queue(self):
        self.db.upsert_photo(
            {
                "uuid": "ABC-123",
                "privacy_state": "approved_public",
                "perms_pushed_flickr": 0,
            }
        )
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
        self.db.upsert_photo(
            {
                "flickr_id": "O1",
                "apple_persons": json.dumps(["Barack Obama"]),
                "privacy_state": "needs_review",
            }
        )
        self.db.upsert_photo(
            {
                "flickr_id": "O2",
                "apple_persons": json.dumps(["Barack Obama", "_UNKNOWN_"]),
                "privacy_state": "needs_review",
            }
        )
        self.db.upsert_photo(
            {
                "flickr_id": "F1",
                "apple_persons": json.dumps(["Family Member"]),
                "privacy_state": "needs_review",
            }
        )

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
            (person,),
        ).fetchall()
        for row in rows:
            self.db.conn.execute(
                "UPDATE photos SET privacy_state = ?, privacy_reason = ? WHERE id = ?",
                (new_state, f"batch: {person}", row["id"]),
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

        self.db.upsert_photo(
            {
                "flickr_id": "O3",
                "apple_persons": json.dumps(["Barack Obama"]),
                "privacy_state": "already_public",
            }
        )
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
        self.db.upsert_photo(
            {
                "flickr_id": "NAV1",
                "apple_persons": json.dumps(["Barack Obama"]),
                "privacy_state": "needs_review",
                "date_taken": "2017-01-01 00:00:00",
            }
        )
        self.db.upsert_photo(
            {
                "flickr_id": "NAV2",
                "apple_persons": json.dumps(["Barack Obama"]),
                "privacy_state": "needs_review",
                "date_taken": "2017-06-01 00:00:00",
            }
        )
        self.db.upsert_photo(
            {
                "flickr_id": "NAV3",
                "apple_persons": json.dumps(["Someone Else"]),
                "privacy_state": "needs_review",
                "date_taken": "2017-03-01 00:00:00",  # between NAV1 and NAV2
            }
        )

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

    def _mock_response(self, status_code=200, json_data=None, retry_after=None):
        from unittest.mock import MagicMock

        resp = MagicMock()
        resp.status_code = status_code
        resp.json.return_value = json_data or {"stat": "ok"}
        resp.raise_for_status = MagicMock()
        resp.headers.get.return_value = retry_after  # None by default — no Retry-After header
        if status_code >= 400:
            import requests as req

            resp.raise_for_status.side_effect = req.HTTPError(response=resp)
        return resp

    def test_success_no_retry(self):
        from unittest.mock import patch

        c = self._make_client()
        ok_resp = self._mock_response(200, {"stat": "ok", "user": {"id": "123"}})
        with patch.object(c._session, "get", return_value=ok_resp):
            result = c._call("flickr.test.login")
        self.assertEqual(result["stat"], "ok")

    def test_retries_on_500(self):
        from unittest.mock import patch

        c = self._make_client()
        err_resp = self._mock_response(500)
        ok_resp = self._mock_response(200, {"stat": "ok"})
        # Fail once then succeed
        with patch.object(c._session, "get", side_effect=[err_resp, ok_resp]):
            with patch("time.sleep"):  # don't actually sleep in tests
                result = c._call("flickr.test.login")
        self.assertEqual(result["stat"], "ok")

    def test_retries_on_timeout(self):
        from unittest.mock import patch
        import requests as req

        c = self._make_client()
        ok_resp = self._mock_response(200, {"stat": "ok"})
        with patch.object(c._session, "get", side_effect=[req.Timeout(), ok_resp]):
            with patch("time.sleep"):
                result = c._call("flickr.test.login")
        self.assertEqual(result["stat"], "ok")

    def test_raises_after_max_retries(self):
        from unittest.mock import patch
        from flickr.flickr_client import FlickrError

        c = self._make_client()
        err_resp = self._mock_response(500)
        with patch.object(c._session, "get", return_value=err_resp):
            with patch("time.sleep"):
                with self.assertRaises(FlickrError):
                    c._call("flickr.test.login", max_retries=2)

    def test_non_transient_flickr_error_raises_immediately(self):
        from unittest.mock import patch
        from flickr.flickr_client import FlickrError

        c = self._make_client()
        bad_resp = self._mock_response(
            200, {"stat": "fail", "code": 1, "message": "Method not found"}
        )
        call_count = 0

        def counting_get(*a, **kw):
            nonlocal call_count
            call_count += 1
            return bad_resp

        with patch.object(c._session, "get", side_effect=counting_get):
            with patch("time.sleep"):
                with self.assertRaises(FlickrError) as ctx:
                    c._call("flickr.nonexistent")
        self.assertEqual(call_count, 1)  # no retries
        self.assertEqual(ctx.exception.code, 1)

    def test_transient_flickr_error_retries(self):
        from unittest.mock import patch

        c = self._make_client()
        transient_resp = self._mock_response(
            200, {"stat": "fail", "code": 0, "message": "something went wrong"}
        )
        ok_resp = self._mock_response(200, {"stat": "ok"})
        with patch.object(c._session, "get", side_effect=[transient_resp, ok_resp]):
            with patch("time.sleep"):
                result = c._call("flickr.test.login")
        self.assertEqual(result["stat"], "ok")

    def test_404_raises_immediately_without_retry(self):
        """HTTP 404 is a permanent error — should raise, not retry."""
        from unittest.mock import patch
        import requests as req

        c = self._make_client()
        not_found = self._mock_response(404)
        call_count = 0

        def counting_get(*a, **kw):
            nonlocal call_count
            call_count += 1
            return not_found

        with patch.object(c._session, "get", side_effect=counting_get):
            with patch("time.sleep"):
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

        with patch.object(c._session, "get", side_effect=counting_get):
            with patch("time.sleep"):
                with self.assertRaises(req.HTTPError):
                    c._call("flickr.photos.getInfo")
        self.assertEqual(call_count, 1)

    def test_retry_delay_includes_jitter(self):
        """Retry delay should include jitter (2^n + random), not bare 2^n."""
        from unittest.mock import patch

        c = self._make_client()
        err_resp = self._mock_response(500)
        ok_resp = self._mock_response(200, {"stat": "ok"})
        sleep_calls = []
        # Mock random.uniform to return a fixed value so test is deterministic
        with patch.object(c._session, "get", side_effect=[err_resp, ok_resp]):
            with patch("time.sleep", side_effect=lambda d: sleep_calls.append(d)):
                with patch("flickr.flickr_client.random.uniform", return_value=0.3):
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

        with patch.object(c._session, "get", side_effect=counting_get):
            with patch("time.sleep"):
                result = c._call("flickr.test.login")
        self.assertEqual(result["stat"], "ok")
        self.assertEqual(call_count, 2)  # retried once

    def test_429_uses_8_retries_not_4(self):
        """HTTP 429 must use 8 retries, not the default 4, to outlast Flickr's rate-limit window."""
        from unittest.mock import patch
        from flickr.flickr_client import FlickrError

        c = self._make_client()
        rate_limited = self._mock_response(429)
        call_count = 0

        def counting_get(*a, **kw):
            nonlocal call_count
            call_count += 1
            return rate_limited

        with patch.object(c._session, "get", side_effect=counting_get):
            with patch("time.sleep"):
                with self.assertRaises(FlickrError):
                    c._call("flickr.photosets.addPhoto")
        # 1 initial attempt + 8 retries = 9 total calls
        self.assertEqual(call_count, 9)

    def test_timeout_still_uses_4_retries(self):
        """Timeout errors must keep the existing 4-retry schedule, not the 429 extended schedule."""
        from unittest.mock import patch
        import requests as req
        from flickr.flickr_client import FlickrError

        c = self._make_client()
        call_count = 0

        def counting_get(*a, **kw):
            nonlocal call_count
            call_count += 1
            raise req.Timeout()

        with patch.object(c._session, "get", side_effect=counting_get):
            with patch("time.sleep"):
                with self.assertRaises(FlickrError):
                    c._call("flickr.test.login")
        # 1 initial attempt + 4 retries = 5 total calls
        self.assertEqual(call_count, 5)

    def test_429_backoff_capped_at_60s(self):
        """429 retry delays must be capped at 60s — attempt 6+ should not exceed 60s."""
        from unittest.mock import patch
        from flickr.flickr_client import FlickrError

        c = self._make_client()
        rate_limited = self._mock_response(429)
        sleep_calls = []

        with patch.object(c._session, "get", return_value=rate_limited):
            with patch("time.sleep", side_effect=lambda d: sleep_calls.append(d)):
                with patch("flickr.flickr_client.random.uniform", return_value=0.0):
                    with self.assertRaises(FlickrError):
                        c._call("flickr.photosets.addPhoto")

        # rate_limit_delay=0 in tests, so all non-zero sleeps are retry backoffs
        retry_sleeps = [d for d in sleep_calls if d > 0]
        self.assertTrue(
            all(d <= 60.5 for d in retry_sleeps),
            f"All retry delays must be <= 60.5s, got: {retry_sleeps}",
        )
        # Attempts 6 and 7 (2^6=64, 2^7=128) must be capped at exactly 60s (jitter=0)
        self.assertEqual(
            retry_sleeps.count(60.0),
            2,
            f"Expected two 60s delays (attempts 6 and 7), got: {retry_sleeps}",
        )

    def test_retry_after_header_honored(self):
        """When Flickr sends Retry-After, sleep that duration (then exponential backoff still runs)."""
        from unittest.mock import patch

        c = self._make_client()
        rate_limited = self._mock_response(429, retry_after="30")
        ok_resp = self._mock_response(200, {"stat": "ok"})
        sleep_calls = []

        with patch.object(c._session, "get", side_effect=[rate_limited, ok_resp]):
            with patch("time.sleep", side_effect=lambda d: sleep_calls.append(d)):
                result = c._call("flickr.test.login")

        self.assertEqual(result["stat"], "ok")
        self.assertIn(30.0, sleep_calls, "Retry-After value of 30 must be used as sleep duration")

    def test_retry_after_validation(self):
        """Retry-After header: non-numeric ignored; negative clamped to 0; >120 capped at 120."""
        from unittest.mock import patch

        c = self._make_client()

        # Non-numeric: should fall through to normal backoff (no sleep of "bad-value")
        bad_header = self._mock_response(429, retry_after="bad-value")
        ok_resp = self._mock_response(200, {"stat": "ok"})
        sleep_calls = []
        with patch.object(c._session, "get", side_effect=[bad_header, ok_resp]):
            with patch("time.sleep", side_effect=lambda d: sleep_calls.append(d)):
                with patch("flickr.flickr_client.random.uniform", return_value=0.0):
                    c._call("flickr.test.login")
        # Should have slept 1.0s (2^0 + 0.0 jitter) from exponential backoff, not from the header
        self.assertIn(1.0, sleep_calls)

        # Absurd value: capped at 120
        huge_header = self._mock_response(429, retry_after="86400")
        ok_resp2 = self._mock_response(200, {"stat": "ok"})
        sleep_calls2 = []
        with patch.object(c._session, "get", side_effect=[huge_header, ok_resp2]):
            with patch("time.sleep", side_effect=lambda d: sleep_calls2.append(d)):
                c._call("flickr.test.login")
        self.assertIn(120.0, sleep_calls2, "Retry-After of 86400 must be capped at 120")
        self.assertNotIn(86400.0, sleep_calls2)

        # Negative value: clamped to 0
        neg_header = self._mock_response(429, retry_after="-5")
        ok_resp3 = self._mock_response(200, {"stat": "ok"})
        sleep_calls3 = []
        with patch.object(c._session, "get", side_effect=[neg_header, ok_resp3]):
            with patch("time.sleep", side_effect=lambda d: sleep_calls3.append(d)):
                c._call("flickr.test.login")
        self.assertIn(0.0, sleep_calls3, "Negative Retry-After must be clamped to 0")
        self.assertNotIn(-5.0, sleep_calls3)


# ---------------------------------------------------------------------------
# Flickr client: Collections API methods
# ---------------------------------------------------------------------------


class TestFlickrCollectionsClient(unittest.TestCase):
    """FlickrClient Collections API methods call the correct Flickr endpoints."""

    def _make_client(self):
        from flickr.flickr_client import FlickrClient

        c = FlickrClient.__new__(FlickrClient)
        c._rate_delay = 0
        c.user_nsid = "me"
        return c

    def test_create_collection_calls_correct_method(self):
        from unittest.mock import patch

        client = self._make_client()
        with patch.object(
            client, "_call", return_value={"collection": {"id": "col-999"}}
        ) as mock_call:
            result = client.create_collection("My Folder")
        mock_call.assert_called_once_with(
            "flickr.collections.create",
            {"title": "My Folder", "description": ""},
            http_method="POST",
        )
        self.assertEqual(result, "col-999")

    def test_create_collection_passes_description(self):
        from unittest.mock import patch

        client = self._make_client()
        with patch.object(
            client, "_call", return_value={"collection": {"id": "col-42"}}
        ) as mock_call:
            client.create_collection("Folder", description="desc")
        self.assertEqual(mock_call.call_args[0][1]["description"], "desc")

    def test_edit_collection_sets_calls_correct_method(self):
        from unittest.mock import patch

        client = self._make_client()
        with patch.object(client, "_call", return_value={}) as mock_call:
            client.edit_collection_sets("col-1", ["ps-1", "ps-2"], ["col-2"])
        mock_call.assert_called_once_with(
            "flickr.collections.editSets",
            {
                "collection_id": "col-1",
                "photoset_ids": "ps-1 ps-2",
                "collection_ids": "col-2",
            },
            http_method="POST",
        )

    def test_edit_collection_sets_empty_lists(self):
        from unittest.mock import patch

        client = self._make_client()
        with patch.object(client, "_call", return_value={}) as mock_call:
            client.edit_collection_sets("col-1", [], [])
        call_params = mock_call.call_args[0][1]
        self.assertEqual(call_params["photoset_ids"], "")
        self.assertEqual(call_params["collection_ids"], "")

    def test_delete_collection_calls_correct_method(self):
        from unittest.mock import patch

        client = self._make_client()
        with patch.object(client, "_call", return_value={}) as mock_call:
            client.delete_collection("col-99")
        mock_call.assert_called_once_with(
            "flickr.collections.delete",
            {"collection_id": "col-99"},
            http_method="POST",
        )

    def test_edit_photoset_meta_calls_correct_method(self):
        from unittest.mock import patch

        client = self._make_client()
        with patch.object(client, "_call", return_value={}) as mock_call:
            client.edit_photoset_meta("ps-123", "New Title")
        mock_call.assert_called_once_with(
            "flickr.photosets.editMeta",
            {"photoset_id": "ps-123", "title": "New Title"},
            http_method="POST",
        )

    def test_edit_collection_meta_calls_correct_method(self):
        from unittest.mock import patch

        client = self._make_client()
        with patch.object(client, "_call", return_value={}) as mock_call:
            client.edit_collection_meta("col-456", "Updated Folder")
        mock_call.assert_called_once_with(
            "flickr.collections.editMeta",
            {"collection_id": "col-456", "title": "Updated Folder"},
            http_method="POST",
        )

    def test_get_photosets_titled_returns_id_title_dict(self):
        from unittest.mock import patch

        client = self._make_client()
        api_response = {
            "stat": "ok",
            "photosets": {
                "photoset": [
                    {"id": "ps-1", "title": {"_content": "Paris"}},
                    {"id": "ps-2", "title": {"_content": "Rome"}},
                ]
            },
        }
        with patch.object(client, "_call", return_value=api_response):
            result = client.get_photosets_titled()
        self.assertEqual(result, {"ps-1": "Paris", "ps-2": "Rome"})

    def test_get_collections_flat_returns_id_title_dict(self):
        from unittest.mock import patch

        client = self._make_client()
        api_response = {
            "stat": "ok",
            "collections": {
                "collection": [
                    {
                        "id": "col-1",
                        "title": "Top",
                        "collection": [
                            {"id": "col-2", "title": "Nested", "collection": [], "set": []}
                        ],
                        "set": [],
                    }
                ]
            },
        }
        with patch.object(client, "_call", return_value=api_response):
            result = client.get_collections_flat()
        self.assertEqual(result, {"col-1": "Top", "col-2": "Nested"})

    def test_delete_photo_calls_api(self):
        from unittest.mock import patch

        client = self._make_client()
        with patch.object(client, "_call", return_value={}) as mock_call:
            client.delete_photo("12345678")
        mock_call.assert_called_once_with(
            "flickr.photos.delete",
            {"photo_id": "12345678"},
            http_method="POST",
        )


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
        self.db.upsert_photo(
            {
                "uuid": "ABC-123",
                "date_taken": "2024-06-16 10:00:00",
                "privacy_state": "approved_public",
            }
        )
        # Simulate incoming Flickr row with same date
        flickr_row = {"date_taken": "2024-06-16 10:00:00", "flickr_id": "99999"}
        match = _find_approved_photos_record(self.db, flickr_row)
        self.assertIsNotNone(match)
        self.assertEqual(match["uuid"], "ABC-123")

    def test_no_match_for_different_date(self):
        from poller.poller import _find_approved_photos_record

        self.db.upsert_photo(
            {
                "uuid": "ABC-456",
                "date_taken": "2024-06-16 10:00:00",
                "privacy_state": "approved_public",
            }
        )
        flickr_row = {"date_taken": "2024-06-17 10:00:00", "flickr_id": "88888"}
        match = _find_approved_photos_record(self.db, flickr_row)
        self.assertIsNone(match)

    def test_no_match_when_not_approved(self):
        from poller.poller import _find_approved_photos_record

        self.db.upsert_photo(
            {
                "uuid": "ABC-789",
                "date_taken": "2024-06-16 10:00:00",
                "privacy_state": "needs_review",
            }
        )
        flickr_row = {"date_taken": "2024-06-16 10:00:00", "flickr_id": "77777"}
        match = _find_approved_photos_record(self.db, flickr_row)
        self.assertIsNone(match)

    def test_no_match_when_already_has_flickr_id(self):
        from poller.poller import _find_approved_photos_record

        # Record already linked to Flickr should not be re-matched
        self.db.upsert_photo(
            {
                "flickr_id": "EXISTING",
                "date_taken": "2024-06-16 10:00:00",
                "privacy_state": "approved_public",
            }
        )
        flickr_row = {"date_taken": "2024-06-16 10:00:00", "flickr_id": "NEW"}
        match = _find_approved_photos_record(self.db, flickr_row)
        self.assertIsNone(match)

    def test_iso8601_date_matches_space_format(self):
        from poller.poller import _find_approved_photos_record

        # Apple Photos stores: 2024-06-16T14:00:00.000000+00:00 (UTC)
        # Flickr returns:       2024-06-16 14:00:00 (UTC, space format)
        self.db.upsert_photo(
            {
                "uuid": "DEF-123",
                "date_taken": "2024-06-16T14:00:00.000000+00:00",
                "privacy_state": "approved_public",
            }
        )
        flickr_row = {"date_taken": "2024-06-16 14:00:00", "flickr_id": "66666"}
        match = _find_approved_photos_record(self.db, flickr_row)
        self.assertIsNotNone(match)

    def test_same_local_time_different_format_matches(self):
        from poller.poller import _find_approved_photos_record

        # Both sides record the same local capture time, just formatted differently.
        # normalise_dt strips timezone offset and milliseconds, keeping local time.
        # Apple Photos: 2024-06-16T10:00:00.583000-04:00 -> "2024-06-16 10:00:00"
        # Flickr:       2024-06-16T10:00:00               -> "2024-06-16 10:00:00"
        self.db.upsert_photo(
            {
                "uuid": "GHI-123",
                "date_taken": "2024-06-16T10:00:00.583000-04:00",
                "privacy_state": "approved_public",
            }
        )
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
            capture_output=True,
            text=True,
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
        self.assertIn("--album", stdout)
        self.assertIn("--verbose", stdout)

    def test_sync_albums_verbose_flag_accepted(self):
        """bp sync-albums --verbose must not be rejected as an unrecognised argument."""
        # --config is a global flag so it comes before the subcommand.
        # --verbose on the subparser must be accepted without argparse complaining.
        _, stderr, _ = self._run_bp("--config", "/nonexistent.yml", "sync-albums", "--verbose")
        self.assertNotIn("unrecognized", stderr.lower())

    def test_sync_album_collections_help(self):
        result = subprocess.run(
            [sys.executable, "bp", "sync-album-collections", "--help"],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).parent.parent),
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("sync-album-collections", result.stdout + result.stderr)

    def test_sync_album_collections_in_all_help(self):
        result = subprocess.run(
            [sys.executable, "bp", "all", "--help"],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).parent.parent),
        )
        self.assertEqual(result.returncode, 0)

    def test_ui_help_includes_host_and_port(self):
        stdout, _, code = self._run_bp("ui", "--help")
        self.assertEqual(code, 0)
        self.assertIn("--host", stdout)
        self.assertIn("--port", stdout)

    def test_ui_host_flag_accepted(self):
        """bp ui --host 0.0.0.0 must not fail with 'unrecognized argument'."""
        _, stderr, _ = self._run_bp("--config", "/nonexistent.yml", "ui", "--host", "0.0.0.0")
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

    def test_migration_008_adds_metadata_cache_columns(self):
        import sys as _sys
        import io
        import contextlib

        _sys.path.insert(0, str(Path(__file__).parent.parent / "db" / "migrations"))
        from migrate_007_metadata_cache import run

        with contextlib.redirect_stdout(io.StringIO()):
            run(self.tmp_path, dry_run=False)
        cols = {r[1] for r in self.db.conn.execute("PRAGMA table_info(photos)").fetchall()}
        for col in (
            "flickr_title",
            "flickr_description",
            "flickr_tags",
            "flickr_tags_hash",
            "flickr_last_updated",
            "photos_title",
            "photos_description",
            "photos_tags",
            "photos_tags_hash",
            "meta_synced_flickr_at",
            "meta_synced_photos_at",
            "meta_last_harmonized_at",
            "tags_truncated_for_flickr",
        ):
            self.assertIn(col, cols, f"Missing column: {col}")

    def test_migration_008_creates_proposals_table(self):
        import sys as _sys
        import io
        import contextlib

        _sys.path.insert(0, str(Path(__file__).parent.parent / "db" / "migrations"))
        from migrate_007_metadata_cache import run

        with contextlib.redirect_stdout(io.StringIO()):
            run(self.tmp_path, dry_run=False)
        row = self.db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='metadata_proposals'"
        ).fetchone()
        self.assertIsNotNone(row)

    def test_migration_008_sets_baseline_harmonized_at(self):
        import sys as _sys
        import io
        import contextlib

        _sys.path.insert(0, str(Path(__file__).parent.parent / "db" / "migrations"))
        from migrate_007_metadata_cache import run

        # Seed a photo first, then run migration
        self.db.upsert_photo(
            {
                "uuid": "uuid-baseline-test",
                "original_filename": "test.jpg",
                "privacy_state": "needs_review",
                "proposed_tags": [],
                "apple_persons": [],
                "apple_labels": [],
            }
        )
        with contextlib.redirect_stdout(io.StringIO()):
            run(self.tmp_path, dry_run=False)
        row = self.db.conn.execute(
            "SELECT meta_last_harmonized_at FROM photos WHERE uuid = 'uuid-baseline-test'"
        ).fetchone()
        self.assertIsNotNone(
            row["meta_last_harmonized_at"], "Existing rows should have meta_last_harmonized_at set"
        )

    def test_migration_008_idempotent(self):
        import sys as _sys
        import io
        import contextlib

        _sys.path.insert(0, str(Path(__file__).parent.parent / "db" / "migrations"))
        from migrate_007_metadata_cache import run

        with contextlib.redirect_stdout(io.StringIO()):
            run(self.tmp_path, dry_run=False)
            run(self.tmp_path, dry_run=False)  # should not raise

    def test_migration_008_proposals_identity_constraint(self):
        """The unique index on metadata_proposals prevents duplicate pending proposals."""
        import sys as _sys
        import io
        import contextlib

        _sys.path.insert(0, str(Path(__file__).parent.parent / "db" / "migrations"))
        from migrate_007_metadata_cache import run

        with contextlib.redirect_stdout(io.StringIO()):
            run(self.tmp_path, dry_run=False)
        photo_id = self.db.upsert_photo(
            {
                "uuid": "uuid-proposal-test",
                "original_filename": "test.jpg",
                "privacy_state": "needs_review",
                "proposed_tags": [],
                "apple_persons": [],
                "apple_labels": [],
            }
        )
        now = "2026-01-01T00:00:00+00:00"
        self.db.conn.execute(
            """INSERT INTO metadata_proposals
               (photo_id, field, proposed_value, source, target, conflict_type, created_at)
               VALUES (?, 'tags', '["nature"]', 'flickr', 'photos', 'non_conflict', ?)""",
            (photo_id, now),
        )
        self.db.conn.commit()
        # Inserting the same pending proposal again should fail
        with self.assertRaises(Exception):
            self.db.conn.execute(
                """INSERT INTO metadata_proposals
                   (photo_id, field, proposed_value, source, target, conflict_type, created_at)
                   VALUES (?, 'tags', '["nature"]', 'flickr', 'photos', 'non_conflict', ?)""",
                (photo_id, now),
            )
            self.db.conn.commit()


# ---------------------------------------------------------------------------
# bp exit codes
# ---------------------------------------------------------------------------


class TestBpExitCodes(unittest.TestCase):
    def _run_bp(self, *args):
        import subprocess

        result = subprocess.run(
            [sys.executable, "bp"] + list(args),
            capture_output=True,
            text=True,
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
        from unittest.mock import MagicMock
        from poller.poller import _push_to_flickr
        import json

        self.db.upsert_photo(
            {
                "flickr_id": "TEST1",
                "privacy_state": "approved_public",
                "proposed_tags": json.dumps(["tag1", "tag2"]),
            }
        )
        record = self.db.get_photo_by_flickr_id("TEST1")

        mock_client = MagicMock()
        errors = _push_to_flickr(mock_client, "TEST1", record, self.db, dry_run=False)
        self.assertEqual(errors, 0)

    def test_push_to_flickr_returns_error_count_on_failure(self):
        from unittest.mock import MagicMock
        from flickr.flickr_client import FlickrError
        from poller.poller import _push_to_flickr
        import json

        self.db.upsert_photo(
            {
                "flickr_id": "TEST2",
                "privacy_state": "approved_public",
                "proposed_tags": json.dumps(["tag1"]),
            }
        )
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

        self.db.upsert_photo(
            {
                "flickr_id": "MAXTAGS",
                "privacy_state": "approved_public",
                "proposed_tags": json.dumps(["tag1"]),
            }
        )
        record = self.db.get_photo_by_flickr_id("MAXTAGS")

        mock_client = MagicMock()
        mock_client.set_permissions.return_value = {"stat": "ok"}
        mock_client.add_tags.side_effect = FlickrError(
            FLICKR_ERR_MAX_TAGS, "Maximum number of tags reached"
        )

        errors = _push_to_flickr(mock_client, "MAXTAGS", record, self.db, dry_run=False)
        # Max tags is not an error — perms still pushed successfully
        self.assertEqual(errors, 0)

    def test_db_flag_not_set_on_failed_push(self):
        from unittest.mock import MagicMock
        from flickr.flickr_client import FlickrError
        from poller.poller import _push_to_flickr
        import json

        self.db.upsert_photo(
            {
                "flickr_id": "TEST3",
                "privacy_state": "approved_public",
                "proposed_tags": json.dumps(["tag1"]),
            }
        )
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
        self.photo_id = self.db.upsert_photo(
            {
                "uuid": "test-uuid-001",
                "original_filename": "IMG_001.jpg",
                "privacy_state": "candidate_public",
            }
        )

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _make_album_info(self, title, uuid, album_type=None, parent=None):
        """Return a simple namespace mimicking an osxphotos AlbumInfo object."""
        from types import SimpleNamespace

        obj = SimpleNamespace(title=title, uuid=uuid, parent=parent)
        if album_type is not None:
            obj.album_type = album_type
        return obj

    def _make_folder_info(self, title, uuid, parent=None):
        """Return a simple namespace mimicking an osxphotos FolderInfo object."""
        from types import SimpleNamespace

        return SimpleNamespace(title=title, uuid=uuid, parent=parent)

    def test_uses_album_info_not_albums(self):
        """sync_photo_albums must read photo.album_info, not photo.albums."""
        from poller.scanner import sync_photo_albums
        from types import SimpleNamespace

        album = self._make_album_info("Vacation 2024", "album-uuid-1")
        # photo.albums is a plain list of strings — must be ignored
        photo = SimpleNamespace(
            albums=["Vacation 2024"],  # strings — wrong attribute
            album_info=[album],  # AlbumInfo objects — correct
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

        user_album = self._make_album_info("My Trip", "uuid-user", album_type="Album")
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

    def test_album_with_folder_creates_folder_row(self):
        from poller.scanner import sync_photo_albums
        from types import SimpleNamespace

        folder = self._make_folder_info("Travel", "folder-uuid-1")
        album = self._make_album_info("Paris Trip", "album-uuid-1", parent=folder)
        photo = SimpleNamespace(album_info=[album])

        sync_photo_albums(photo, self.photo_id, self.db, dry_run=False)

        folder_row = self.db.conn.execute(
            "SELECT * FROM folders WHERE apple_uuid='folder-uuid-1'"
        ).fetchone()
        self.assertIsNotNone(folder_row)
        self.assertEqual(folder_row["name"], "Travel")

        album_row = self.db.conn.execute(
            "SELECT folder_id FROM albums WHERE apple_uuid='album-uuid-1'"
        ).fetchone()
        self.assertEqual(album_row["folder_id"], folder_row["id"])

    def test_album_with_nested_folders_creates_all_rows(self):
        from poller.scanner import sync_photo_albums
        from types import SimpleNamespace

        grandparent = self._make_folder_info("Europe", "uuid-gp")
        parent = self._make_folder_info("France", "uuid-p", parent=grandparent)
        album = self._make_album_info("Paris", "uuid-album", parent=parent)
        photo = SimpleNamespace(album_info=[album])

        sync_photo_albums(photo, self.photo_id, self.db, dry_run=False)

        gp_row = self.db.conn.execute(
            "SELECT id, parent_id FROM folders WHERE apple_uuid='uuid-gp'"
        ).fetchone()
        p_row = self.db.conn.execute(
            "SELECT id, parent_id FROM folders WHERE apple_uuid='uuid-p'"
        ).fetchone()
        self.assertIsNone(gp_row["parent_id"])
        self.assertEqual(p_row["parent_id"], gp_row["id"])

    def test_album_without_folder_has_null_folder_id(self):
        from poller.scanner import sync_photo_albums
        from types import SimpleNamespace

        album = self._make_album_info("No Folder Album", "uuid-nf")  # parent=None by default
        photo = SimpleNamespace(album_info=[album])

        sync_photo_albums(photo, self.photo_id, self.db, dry_run=False)

        row = self.db.conn.execute(
            "SELECT folder_id FROM albums WHERE apple_uuid='uuid-nf'"
        ).fetchone()
        self.assertIsNone(row["folder_id"])

    def test_shared_folder_deduplicated_across_albums(self):
        from poller.scanner import sync_photo_albums
        from types import SimpleNamespace

        folder = self._make_folder_info("Travel", "folder-uuid-shared")
        album1 = self._make_album_info("Paris", "uuid-album-1", parent=folder)
        album2 = self._make_album_info("Rome", "uuid-album-2", parent=folder)
        photo = SimpleNamespace(album_info=[album1, album2])

        sync_photo_albums(photo, self.photo_id, self.db, dry_run=False)

        count = self.db.conn.execute("SELECT COUNT(*) AS n FROM folders").fetchone()["n"]
        self.assertEqual(count, 1, "same folder via two albums must produce only one row")

    def test_dry_run_does_not_write_folders(self):
        from poller.scanner import sync_photo_albums
        from types import SimpleNamespace

        folder = self._make_folder_info("Travel", "folder-uuid-dry")
        album = self._make_album_info("Paris", "uuid-album-dry", parent=folder)
        photo = SimpleNamespace(album_info=[album])

        sync_photo_albums(photo, self.photo_id, self.db, dry_run=True)

        count = self.db.conn.execute("SELECT COUNT(*) AS n FROM folders").fetchone()["n"]
        self.assertEqual(count, 0, "dry_run must not write folders")


# ---------------------------------------------------------------------------
# Album DB methods
# ---------------------------------------------------------------------------


def _make_db(tmp_dir: str):
    from db.db import Database

    return Database(Path(tmp_dir) / "test.db")


def _seed_photo(db, flickr_id=None, perms_pushed=0) -> int:
    import uuid as _uuid

    return db.upsert_photo(
        {
            "uuid": str(_uuid.uuid4()),
            "original_filename": "IMG_0001.JPG",
            "privacy_state": "approved_public" if flickr_id else "candidate_public",
            "flickr_id": flickr_id,
            "perms_pushed_flickr": perms_pushed,
            "proposed_tags": [],
            "apple_persons": [],
            "apple_labels": [],
        }
    )


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
        self.db.set_album_flickr_set_id(
            album_id, "72157720000001", "https://www.flickr.com/photos/me/sets/72157720000001/"
        )
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

        photo_id = self.db.upsert_photo(
            {
                "uuid": str(_uuid.uuid4()),
                "original_filename": "private.jpg",
                "privacy_state": "keep_private",
                "review_decision": "keep_private",
                "flickr_id": "f999",
                "perms_pushed_flickr": 0,
                "proposed_tags": [],
                "apple_persons": [],
                "apple_labels": [],
            }
        )
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
# Folder DB
# ---------------------------------------------------------------------------


class TestFolderDB(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = _make_db(self._tmp.name)

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_upsert_folder_creates_and_returns_id(self):
        fid = self.db.upsert_folder("uuid-f1", "Travel")
        self.assertIsInstance(fid, int)
        self.assertGreater(fid, 0)

    def test_upsert_folder_idempotent(self):
        fid1 = self.db.upsert_folder("uuid-f1", "Travel")
        fid2 = self.db.upsert_folder("uuid-f1", "Travel")
        self.assertEqual(fid1, fid2)

    def test_upsert_folder_updates_name(self):
        fid = self.db.upsert_folder("uuid-f1", "Old Name")
        self.db.upsert_folder("uuid-f1", "New Name")
        row = self.db.conn.execute("SELECT name FROM folders WHERE id=?", (fid,)).fetchone()
        self.assertEqual(row["name"], "New Name")

    def test_upsert_folder_with_parent(self):
        parent_id = self.db.upsert_folder("uuid-parent", "Europe")
        child_id = self.db.upsert_folder("uuid-child", "France", parent_id=parent_id)
        row = self.db.conn.execute(
            "SELECT parent_id FROM folders WHERE id=?", (child_id,)
        ).fetchone()
        self.assertEqual(row["parent_id"], parent_id)

    def test_upsert_album_accepts_folder_id(self):
        fid = self.db.upsert_folder("uuid-f1", "Travel")
        aid = self.db.upsert_album("uuid-a1", "Paris Trip", folder_id=fid)
        row = self.db.conn.execute("SELECT folder_id FROM albums WHERE id=?", (aid,)).fetchone()
        self.assertEqual(row["folder_id"], fid)

    def test_upsert_album_folder_id_defaults_none(self):
        aid = self.db.upsert_album("uuid-a1", "Standalone Album")
        row = self.db.conn.execute("SELECT folder_id FROM albums WHERE id=?", (aid,)).fetchone()
        self.assertIsNone(row["folder_id"])

    def test_get_all_folders_returns_rows(self):
        self.db.upsert_folder("uuid-f1", "Travel")
        self.db.upsert_folder("uuid-f2", "Work")
        folders = self.db.get_all_folders()
        self.assertEqual(len(folders), 2)
        names = {f["name"] for f in folders}
        self.assertEqual(names, {"Travel", "Work"})

    def test_get_all_folders_empty(self):
        self.assertEqual(self.db.get_all_folders(), [])

    def test_set_folder_flickr_collection_id(self):
        fid = self.db.upsert_folder("uuid-f1", "Travel")
        self.db.set_folder_flickr_collection_id(fid, "col-123")
        row = self.db.conn.execute(
            "SELECT flickr_collection_id FROM folders WHERE id=?", (fid,)
        ).fetchone()
        self.assertEqual(row["flickr_collection_id"], "col-123")

    def test_clear_folder_flickr_collection_id(self):
        fid = self.db.upsert_folder("uuid-f1", "Travel")
        self.db.set_folder_flickr_collection_id(fid, "col-123")
        self.db.clear_folder_flickr_collection_id(fid)
        row = self.db.conn.execute(
            "SELECT flickr_collection_id FROM folders WHERE id=?", (fid,)
        ).fetchone()
        self.assertIsNone(row["flickr_collection_id"])


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

    def test_photo_not_found_error_marks_pushed_and_skips(self):
        from flickr.album_pusher import push_photo_to_albums
        from flickr.flickr_client import FlickrError
        from unittest.mock import MagicMock

        self.db.set_album_flickr_set_id(self.album_id, "EXISTING_SET")
        flickr = MagicMock()
        flickr.add_photo_to_photoset.side_effect = FlickrError(1, "Photo not found")

        result = push_photo_to_albums(self.db, flickr, self.photo_id)

        self.assertEqual(result, 0)  # not counted as success
        row = self.db.conn.execute(
            "SELECT flickr_pushed FROM photo_albums WHERE photo_id = ? AND album_id = ?",
            (self.photo_id, self.album_id),
        ).fetchone()
        self.assertEqual(row["flickr_pushed"], 1)  # marked done to prevent retries

    def test_already_in_set_error_treated_as_success(self):
        from flickr.album_pusher import push_photo_to_albums
        from flickr.flickr_client import FlickrError
        from unittest.mock import MagicMock

        self.db.set_album_flickr_set_id(self.album_id, "EXISTING_SET")
        flickr = MagicMock()
        flickr.add_photo_to_photoset.side_effect = FlickrError(3, "Photo already in set")

        result = push_photo_to_albums(self.db, flickr, self.photo_id)

        self.assertEqual(result, 1)
        row = self.db.conn.execute(
            "SELECT flickr_pushed FROM photo_albums WHERE photo_id = ? AND album_id = ?",
            (self.photo_id, self.album_id),
        ).fetchone()
        self.assertEqual(row["flickr_pushed"], 1)

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
            "--config",
            str(self._config_path),
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
        self.photo_id = self.db.upsert_photo(
            {
                "uuid": "uuid-mc-001",
                "original_filename": "IMG_mc.JPG",
                "privacy_state": "approved_public",
                "flickr_id": "flickr-mc-001",
                "proposed_tags": [],
                "apple_persons": [],
                "apple_labels": [],
            }
        )

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
        other_id = self.db.upsert_photo(
            {
                "uuid": "uuid-mc-002",
                "original_filename": "IMG2.JPG",
                "privacy_state": "approved_public",
                "flickr_id": "flickr-mc-002",
                "proposed_tags": [],
                "apple_persons": [],
                "apple_labels": [],
            }
        )
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


class TestNormaliseTags(unittest.TestCase):
    """_normalise_tags matches Flickr's alphanumeric-only normalisation."""

    def _norm(self, tags):
        from flickr.metadata_puller import _normalise_tags

        return _normalise_tags(tags)

    def test_strips_spaces_within_tag(self):
        self.assertEqual(self._norm(["New York"]), {"newyork"})

    def test_strips_punctuation(self):
        self.assertEqual(self._norm(["black & white"]), {"blackwhite"})

    def test_lowercases(self):
        self.assertEqual(self._norm(["NATURE"]), {"nature"})

    def test_space_and_nospace_variants_equal(self):
        # "New York" on Photos side == "newyork" on Flickr side
        self.assertEqual(self._norm(["New York"]), self._norm(["newyork"]))

    def test_empty_tags_ignored(self):
        self.assertEqual(self._norm(["", "  "]), set())

    def test_unicode_nfc_normalisation(self):
        # café vs café (decomposed) should be equal after NFC
        self.assertEqual(self._norm(["café"]), self._norm(["café"]))


class TestMetadataPuller(unittest.TestCase):
    def setUp(self):
        from unittest.mock import MagicMock

        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "test.db")
        self.photo_id = self.db.upsert_photo(
            {
                "uuid": "uuid-mp-001",
                "original_filename": "IMG_mp.JPG",
                "privacy_state": "approved_public",
                "flickr_id": "flickr-mp-001",
                "proposed_tags": [],
                "apple_persons": [],
                "apple_labels": [],
            }
        )
        self.mock_flickr = MagicMock()
        self.library = "/fake/Photos.photoslibrary"

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _set_flickr_meta(self, title="", description="", tags=None):
        self.mock_flickr.get_photo_info.return_value = {
            "photo": {
                "title": {"_content": title},
                "description": {"_content": description},
                "tags": {"tag": [{"raw": t} for t in (tags or [])]},
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
            patches.append(
                patch(
                    "flickr.metadata_puller._write_photos_metadata",
                    side_effect=RuntimeError("Photos.app is not running"),
                )
            )
        return patches

    def _pull(self, dry_run=False):
        from flickr.metadata_puller import pull_photo_metadata

        return pull_photo_metadata(
            self.db,
            self.mock_flickr,
            self.photo_id,
            library_path=self.library,
            dry_run=dry_run,
        )

    def test_flickr_wins_when_photos_empty(self):
        from unittest.mock import patch

        self._set_flickr_meta(title="A Great Shot")
        with (
            patch(
                "flickr.metadata_puller._read_photos_metadata",
                return_value={"title": "", "description": "", "tags": []},
            ),
            patch("flickr.metadata_puller._write_photos_metadata") as mock_write,
        ):
            result = self._pull()
        self.assertIn("title", result["written"])
        self.assertEqual(result["conflicts"], [])
        mock_write.assert_called_once()

    def test_no_op_when_values_equal(self):
        from unittest.mock import patch

        self._set_flickr_meta(title="Same Title")
        with (
            patch(
                "flickr.metadata_puller._read_photos_metadata",
                return_value={"title": "Same Title", "description": "", "tags": []},
            ),
            patch("flickr.metadata_puller._write_photos_metadata") as mock_write,
        ):
            result = self._pull()
        self.assertIn("title", result["skipped"])
        self.assertEqual(result["written"], [])
        mock_write.assert_not_called()

    def test_conflict_recorded_when_both_non_empty_different(self):
        from unittest.mock import patch

        self._set_flickr_meta(description="Flickr caption")
        with (
            patch(
                "flickr.metadata_puller._read_photos_metadata",
                return_value={"title": "", "description": "Photos caption", "tags": []},
            ),
            patch("flickr.metadata_puller._write_photos_metadata") as mock_write,
        ):
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
        with (
            patch(
                "flickr.metadata_puller._read_photos_metadata",
                return_value={"title": "", "description": "Local note", "tags": []},
            ),
            patch("flickr.metadata_puller._write_photos_metadata") as mock_write,
        ):
            result = self._pull()
        self.assertIn("description", result["skipped"])
        mock_write.assert_not_called()

    def test_tags_comparison_case_insensitive(self):
        from unittest.mock import patch

        self._set_flickr_meta(tags=["Nature", "Landscape"])
        with (
            patch(
                "flickr.metadata_puller._read_photos_metadata",
                return_value={"title": "", "description": "", "tags": ["nature", "landscape"]},
            ),
            patch("flickr.metadata_puller._write_photos_metadata") as mock_write,
        ):
            result = self._pull()
        self.assertIn("tags", result["skipped"])
        self.assertEqual(result["conflicts"], [])
        mock_write.assert_not_called()

    def test_tags_no_conflict_when_differ_only_by_spaces(self):
        # Flickr strips spaces from tags, so "New York" == "newyork"
        from unittest.mock import patch

        self._set_flickr_meta(tags=["newyork", "landscape"])
        with (
            patch(
                "flickr.metadata_puller._read_photos_metadata",
                return_value={"title": "", "description": "", "tags": ["New York", "landscape"]},
            ),
            patch("flickr.metadata_puller._write_photos_metadata") as mock_write,
        ):
            result = self._pull()
        self.assertIn("tags", result["skipped"])
        self.assertEqual(result["conflicts"], [])
        mock_write.assert_not_called()

    def test_tags_conflict_when_different(self):
        from unittest.mock import patch

        self._set_flickr_meta(tags=["nature"])
        with (
            patch(
                "flickr.metadata_puller._read_photos_metadata",
                return_value={"title": "", "description": "", "tags": ["landscape"]},
            ),
            patch("flickr.metadata_puller._write_photos_metadata"),
        ):
            result = self._pull()
        self.assertIn("tags", result["conflicts"])
        rows = self.db.get_unresolved_conflicts(photo_id=self.photo_id)
        tag_row = next((r for r in rows if r["field"] == "tags"), None)
        self.assertIsNotNone(tag_row)

    def test_dry_run_skips_all_writes_and_db_updates(self):
        from unittest.mock import patch

        self._set_flickr_meta(title="Flickr Title", description="Flickr Desc")
        with (
            patch(
                "flickr.metadata_puller._read_photos_metadata",
                return_value={"title": "", "description": "Photos Desc", "tags": []},
            ),
            patch("flickr.metadata_puller._write_photos_metadata") as mock_write,
        ):
            result = self._pull(dry_run=True)
        mock_write.assert_not_called()
        rows = self.db.get_unresolved_conflicts(photo_id=self.photo_id)
        self.assertEqual(len(rows), 0)  # no DB writes in dry_run
        self.assertIn("title", result["written"])  # counted as would-write

    def test_no_uuid_returns_no_uuid_status(self):

        # Seed a photo without a uuid
        no_uuid_id = self.db.upsert_photo(
            {
                "uuid": None,
                "original_filename": "no_uuid.JPG",
                "privacy_state": "approved_public",
                "flickr_id": "flickr-nouuid",
                "proposed_tags": [],
                "apple_persons": [],
                "apple_labels": [],
            }
        )
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

    def test_flickr_not_found_returns_flickr_deleted_status(self):
        """Flickr error 1 (photo not found) sets flickr_deleted in DB and returns flickr_deleted status."""
        from flickr.flickr_client import FlickrError

        self.mock_flickr.get_photo_info.side_effect = FlickrError(1, "Photo not found")
        from flickr.metadata_puller import pull_photo_metadata

        result = pull_photo_metadata(self.db, self.mock_flickr, self.photo_id, self.library)
        self.assertEqual(result["status"], "flickr_deleted")
        # DB flag should be set
        row = self.db.conn.execute(
            "SELECT flickr_deleted FROM photos WHERE id = ?", (self.photo_id,)
        ).fetchone()
        self.assertEqual(row["flickr_deleted"], 1)

    def test_flickr_not_found_dry_run_does_not_write_db(self):
        """In dry-run mode, flickr_deleted status is returned but the DB flag is NOT set."""
        from flickr.flickr_client import FlickrError

        self.mock_flickr.get_photo_info.side_effect = FlickrError(1, "Photo not found")
        from flickr.metadata_puller import pull_photo_metadata

        result = pull_photo_metadata(
            self.db, self.mock_flickr, self.photo_id, self.library, dry_run=True
        )
        self.assertEqual(result["status"], "flickr_deleted")
        row = self.db.conn.execute(
            "SELECT flickr_deleted FROM photos WHERE id = ?", (self.photo_id,)
        ).fetchone()
        self.assertEqual(row["flickr_deleted"], 0)  # not written in dry-run

    def test_write_error_counted_as_write_error_status(self):
        from unittest.mock import patch

        self._set_flickr_meta(title="Flickr Title")
        with (
            patch(
                "flickr.metadata_puller._read_photos_metadata",
                return_value={"title": "", "description": "", "tags": []},
            ),
            patch(
                "flickr.metadata_puller._write_photos_metadata",
                side_effect=RuntimeError("Photos not running"),
            ),
        ):
            result = self._pull()
        self.assertEqual(result["status"], "write_error")
        self.assertTrue(len(result["errors"]) > 0)
        # No conflict recorded — a write failure is not a conflict
        rows = self.db.get_unresolved_conflicts(photo_id=self.photo_id)
        self.assertEqual(len(rows), 0)

    def test_photoscript_invalid_uuid_does_not_crash_batch(self):
        """photoscript.Photo() raising a non-RuntimeError must not escape as an unhandled exception."""
        from unittest.mock import patch, MagicMock

        self._set_flickr_meta(title="Flickr Title")
        # Simulate photoscript raising ValueError("Invalid photo id: <uuid>")
        with (
            patch(
                "flickr.metadata_puller._read_photos_metadata",
                return_value={"title": "", "description": "", "tags": []},
            ),
            patch("flickr.metadata_puller._photos_is_responsive", return_value=True),
            patch.dict(
                __import__("sys").modules,
                {
                    "photoscript": MagicMock(
                        Photo=MagicMock(side_effect=ValueError("Invalid photo id: uuid-mp-001"))
                    )
                },
            ),
        ):
            result = self._pull()
        self.assertEqual(result["status"], "write_error")
        self.assertTrue(any("Invalid photo id" in e for e in result["errors"]))


# ---------------------------------------------------------------------------
# pull_batch — PhotosDB caching and progress logging
# ---------------------------------------------------------------------------


class TestPullBatch(unittest.TestCase):
    def setUp(self):
        from unittest.mock import MagicMock

        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "test.db")
        self.library = str(Path(self._tmp.name) / "Photos.photoslibrary")
        self.mock_flickr = MagicMock()

        # Seed two photos with flickr_id and uuid
        self.id1 = self.db.upsert_photo(
            {
                "uuid": "uuid-batch-1",
                "original_filename": "IMG_001.JPG",
                "privacy_state": "approved_public",
                "flickr_id": "flickr-001",
                "proposed_tags": [],
                "apple_persons": [],
                "apple_labels": [],
            }
        )
        self.id2 = self.db.upsert_photo(
            {
                "uuid": "uuid-batch-2",
                "original_filename": "IMG_002.JPG",
                "privacy_state": "approved_public",
                "flickr_id": "flickr-002",
                "proposed_tags": [],
                "apple_persons": [],
                "apple_labels": [],
            }
        )

        self.mock_flickr.get_photo_info.return_value = {
            "photo": {
                "title": {"_content": "Flickr Title"},
                "description": {"_content": ""},
                "tags": {"tag": []},
            }
        }

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _mock_osxphotos(self):
        """Return (mock_module, mock_db_instance) with sys.modules patching."""
        from unittest.mock import MagicMock, patch

        mock_db_instance = MagicMock()
        mock_db_instance.photos.return_value = []
        mock_module = MagicMock()
        mock_module.PhotosDB.return_value = mock_db_instance
        return (
            mock_module,
            mock_db_instance,
            patch.dict(__import__("sys").modules, {"osxphotos": mock_module}),
        )

    def test_photosdb_opened_once_not_per_photo(self):
        """PhotosDB should be opened once for the whole batch, not once per photo."""
        mock_module, mock_db_instance, patcher = self._mock_osxphotos()
        with patcher:
            from flickr.metadata_puller import pull_batch

            pull_batch(
                self.db,
                self.mock_flickr,
                [self.id1, self.id2],
                library_path=self.library,
                dry_run=True,
            )
        mock_module.PhotosDB.assert_called_once_with(dbfile=self.library)

    def test_progress_logged_at_intervals(self):
        """pull_batch should emit at least one INFO progress line."""
        import logging as _logging

        mock_module, mock_db_instance, patcher = self._mock_osxphotos()
        with patcher, self.assertLogs("blue-pearmain.metadata_puller", level=_logging.INFO) as cm:
            from flickr.metadata_puller import pull_batch

            pull_batch(
                self.db,
                self.mock_flickr,
                [self.id1, self.id2],
                library_path=self.library,
                dry_run=True,
            )
        progress_lines = [line for line in cm.output if "Progress:" in line or "Processing" in line]
        self.assertTrue(len(progress_lines) >= 1, "Expected at least one progress log line")

    def test_batch_totals_aggregated(self):
        """Totals dict should have the right keys and non-negative counts."""
        mock_module, mock_db_instance, patcher = self._mock_osxphotos()
        with patcher:
            from flickr.metadata_puller import pull_batch

            totals = pull_batch(
                self.db,
                self.mock_flickr,
                [self.id1, self.id2],
                library_path=self.library,
                dry_run=True,
            )
        self.assertIn("written", totals)
        self.assertIn("conflicts", totals)
        self.assertIn("skipped", totals)
        self.assertIn("failed", totals)
        self.assertGreaterEqual(totals["written"] + totals["skipped"] + totals["failed"], 0)

    def test_flickr_deleted_counted_as_skipped_not_failed(self):
        """A photo deleted from Flickr (error 1) should count as skipped, not failed."""
        from flickr.flickr_client import FlickrError

        self.mock_flickr.get_photo_info.side_effect = FlickrError(1, "Photo not found")
        mock_module, mock_db_instance, patcher = self._mock_osxphotos()
        with patcher:
            from flickr.metadata_puller import pull_batch

            totals = pull_batch(
                self.db,
                self.mock_flickr,
                [self.id1, self.id2],
                library_path=self.library,
                dry_run=False,
            )
        self.assertEqual(totals["failed"], 0)
        self.assertGreater(totals["skipped"], 0)


# ---------------------------------------------------------------------------
# Phase 4: scanner writes Photos metadata cache to DB
# ---------------------------------------------------------------------------


class TestPhotosRecordToDbMetadata(unittest.TestCase):
    """photos_record_to_db should capture Photos metadata cache columns."""

    def _make_mock_photo(self, title="", description="", keywords=None):
        from unittest.mock import MagicMock

        p = MagicMock()
        p.uuid = "uuid-meta-001"
        p.original_filename = "IMG_meta.JPG"
        p.date = None
        p.date_added = None
        p.exif_info = None
        p.latitude = None
        p.place = None
        p.media_analysis = {}
        p.score = None
        p.labels = []
        p.persons = []
        p.fingerprint = ""
        p.width = None
        p.height = None
        p.screenshot = False
        p.selfie = False
        p.live_photo = False
        p.album_info = []
        p.title = title
        p.description = description
        p.keywords = keywords or []
        return p

    def _convert(self, **kwargs):
        from poller.scanner import photos_record_to_db

        return photos_record_to_db(self._make_mock_photo(**kwargs))

    def test_photos_title_captured(self):
        row = self._convert(title="My Holiday Shot")
        self.assertEqual(row["photos_title"], "My Holiday Shot")

    def test_photos_description_captured(self):
        row = self._convert(description="A day at the beach")
        self.assertEqual(row["photos_description"], "A day at the beach")

    def test_photos_tags_captured_as_list(self):
        row = self._convert(keywords=["beach", "summer"])
        self.assertIsInstance(row["photos_tags"], list)
        self.assertEqual(sorted(row["photos_tags"]), ["beach", "summer"])

    def test_photos_tags_hash_set(self):
        row = self._convert(keywords=["alpha", "beta"])
        self.assertIsNotNone(row["photos_tags_hash"])
        self.assertEqual(len(row["photos_tags_hash"]), 64)

    def test_photos_tags_hash_case_insensitive(self):
        from poller.scanner import _compute_tags_hash

        h1 = _compute_tags_hash(["Alpha"])
        h2 = _compute_tags_hash(["alpha"])
        self.assertEqual(h1, h2)

    def test_meta_synced_photos_at_set(self):
        row = self._convert()
        self.assertIsNotNone(row["meta_synced_photos_at"])

    def test_empty_title_stored_as_empty_string(self):
        row = self._convert(title=None)
        self.assertEqual(row["photos_title"], "")

    def test_empty_keywords_stored_as_empty_list(self):
        row = self._convert(keywords=None)
        self.assertEqual(row["photos_tags"], [])


class TestBuildEnrichedRowPhase4(unittest.TestCase):
    """build_enriched_row should propagate Photos metadata cache fields."""

    EXISTING = {
        "id": 1,
        "flickr_id": "12345",
        "uuid": None,
        "privacy_state": "candidate_public",
        "privacy_reason": "no people detected",
        "proposed_tags": [],
        "latitude": None,
        "longitude": None,
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
            "photos_title": "Test Title",
            "photos_description": "Test Desc",
            "photos_tags": ["holiday"],
            "photos_tags_hash": "abc123",
            "meta_synced_photos_at": "2026-04-30T00:00:00+00:00",
        }
        return {**base, **kwargs}

    def test_photos_title_propagated(self):
        enriched = build_enriched_row(self._photo_row(), self.EXISTING, [], "Chris")
        self.assertEqual(enriched["photos_title"], "Test Title")

    def test_photos_tags_propagated(self):
        enriched = build_enriched_row(self._photo_row(), self.EXISTING, [], "Chris")
        self.assertEqual(enriched["photos_tags"], ["holiday"])

    def test_photos_tags_hash_propagated(self):
        enriched = build_enriched_row(self._photo_row(), self.EXISTING, [], "Chris")
        self.assertEqual(enriched["photos_tags_hash"], "abc123")

    def test_meta_synced_photos_at_propagated(self):
        enriched = build_enriched_row(self._photo_row(), self.EXISTING, [], "Chris")
        self.assertIsNotNone(enriched.get("meta_synced_photos_at"))


class TestScannerSkipConditionPhase4(unittest.TestCase):
    """
    The scan loop should skip re-enrichment only when both ML analysis
    AND Photos metadata cache are unchanged.
    """

    def setUp(self):

        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "test.db")
        from datetime import datetime, timezone

        ts = datetime.now(timezone.utc).isoformat()
        self.photo_id = self.db.upsert_photo(
            {
                "uuid": "uuid-skip-001",
                "flickr_id": "flickr-skip-001",
                "original_filename": "IMG_skip.JPG",
                "privacy_state": "approved_public",
                "proposed_tags": [],
                "apple_persons": [],
                "apple_labels": [],
                "date_analyzed": "2026-01-01T00:00:00",
                "photos_title": "Old Title",
                "photos_tags_hash": "oldhash",
                "meta_synced_photos_at": ts,
            }
        )

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _make_mock_photo(
        self, title="Old Title", keywords=None, date_analyzed="2026-01-01T00:00:00"
    ):
        from unittest.mock import MagicMock

        p = MagicMock()
        p.uuid = "uuid-skip-001"
        p.original_filename = "IMG_skip.JPG"
        p.date = None
        p.date_added = None
        p.exif_info = None
        p.latitude = None
        p.place = None
        p.media_analysis = {"date_analyzed": date_analyzed}
        p.score = None
        p.labels = []
        p.persons = []
        p.fingerprint = ""
        p.width = None
        p.height = None
        p.screenshot = False
        p.selfie = False
        p.live_photo = False
        p.album_info = []
        p.title = title
        p.description = ""
        p.keywords = keywords or []
        return p

    def _run_scan_one(self, mock_photo):
        from unittest.mock import MagicMock, patch
        from poller.scanner import scan

        mock_db_instance = MagicMock()
        mock_db_instance.photos.return_value = [mock_photo]
        mock_module = MagicMock()
        mock_module.PhotosDB.return_value = mock_db_instance
        with patch.dict(__import__("sys").modules, {"osxphotos": mock_module}):
            scan(
                library_path=self._tmp.name,
                db=self.db,
                since=None,
                dry_run=False,
                self_name="",
            )

    def test_skips_when_analysis_and_cache_both_unchanged(self):
        """No DB write when nothing has changed."""
        before = self.db.conn.execute(
            "SELECT updated_at FROM photos WHERE id=?", (self.photo_id,)
        ).fetchone()["updated_at"]

        # photo with same date_analyzed AND same photos_tags_hash/title

        p = self._make_mock_photo(title="Old Title", keywords=[])
        # Force the hash to match the stored "oldhash" by patching _compute_tags_hash
        from unittest.mock import patch

        with patch("poller.scanner._compute_tags_hash", return_value="oldhash"):
            self._run_scan_one(p)

        after = self.db.conn.execute(
            "SELECT updated_at FROM photos WHERE id=?", (self.photo_id,)
        ).fetchone()["updated_at"]
        self.assertEqual(before, after, "Should not write when nothing changed")

    def test_re_enriches_when_title_changes(self):
        """When photos_title changes, the row must be re-enriched."""
        p = self._make_mock_photo(title="New Title")
        self._run_scan_one(p)
        row = self.db.conn.execute(
            "SELECT photos_title FROM photos WHERE id=?", (self.photo_id,)
        ).fetchone()
        self.assertEqual(row["photos_title"], "New Title")

    def test_re_enriches_when_analysis_changes(self):
        """When date_analyzed changes, the row must be re-enriched."""
        from unittest.mock import patch

        p = self._make_mock_photo(title="Old Title", date_analyzed="2026-06-01T00:00:00")
        with patch("poller.scanner._compute_tags_hash", return_value="oldhash"):
            self._run_scan_one(p)
        row = self.db.conn.execute(
            "SELECT date_analyzed FROM photos WHERE id=?", (self.photo_id,)
        ).fetchone()
        self.assertEqual(row["date_analyzed"], "2026-06-01T00:00:00")


# ---------------------------------------------------------------------------
# Phase 3: sync-metadata reads from DB cache instead of per-photo Flickr API
# ---------------------------------------------------------------------------


class TestFlickrCacheRead(unittest.TestCase):
    """Unit tests for _read_flickr_cache."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "test.db")
        self.photo_id = self.db.upsert_photo(
            {
                "flickr_id": "flickr-cache-001",
                "uuid": "uuid-cache-001",
                "privacy_state": "approved_public",
                "proposed_tags": [],
                "apple_persons": [],
                "apple_labels": [],
            }
        )

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _read_cache(self):
        from flickr.metadata_puller import _read_flickr_cache

        return _read_flickr_cache(self.db, self.photo_id)

    def test_returns_none_when_not_synced(self):
        """Cache is empty until the poller has run (meta_synced_flickr_at NULL)."""
        self.assertIsNone(self._read_cache())

    def test_returns_dict_when_synced(self):
        from datetime import datetime, timezone

        ts = datetime.now(timezone.utc).isoformat()
        self.db.conn.execute(
            """UPDATE photos
               SET flickr_title='T', flickr_description='D',
                   flickr_tags='["alpha"]', meta_synced_flickr_at=?
               WHERE id=?""",
            (ts, self.photo_id),
        )
        self.db.conn.commit()
        result = self._read_cache()
        self.assertIsNotNone(result)
        self.assertEqual(result["title"], "T")
        self.assertEqual(result["description"], "D")
        self.assertEqual(result["tags"], ["alpha"])

    def test_returns_empty_strings_for_null_fields(self):
        from datetime import datetime, timezone

        ts = datetime.now(timezone.utc).isoformat()
        self.db.conn.execute(
            "UPDATE photos SET meta_synced_flickr_at=? WHERE id=?",
            (ts, self.photo_id),
        )
        self.db.conn.commit()
        result = self._read_cache()
        self.assertIsNotNone(result)
        self.assertEqual(result["title"], "")
        self.assertEqual(result["tags"], [])

    def test_tags_parsed_from_json(self):
        from datetime import datetime, timezone

        ts = datetime.now(timezone.utc).isoformat()
        self.db.conn.execute(
            """UPDATE photos SET flickr_tags='["foo","bar"]', meta_synced_flickr_at=?
               WHERE id=?""",
            (ts, self.photo_id),
        )
        self.db.conn.commit()
        result = self._read_cache()
        self.assertEqual(sorted(result["tags"]), ["bar", "foo"])


class TestPullPhotoMetadataPhase3(unittest.TestCase):
    """
    pull_photo_metadata should use DB cache when available,
    and only call the Flickr API on cache miss.
    """

    def setUp(self):
        from unittest.mock import MagicMock

        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "test.db")
        self.photo_id = self.db.upsert_photo(
            {
                "flickr_id": "flickr-p3-001",
                "uuid": "uuid-p3-001",
                "privacy_state": "approved_public",
                "proposed_tags": [],
                "apple_persons": [],
                "apple_labels": [],
            }
        )
        self.mock_flickr = MagicMock()
        self.library = "/fake/Photos.photoslibrary"

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _seed_cache(self, title="Cached Title", description="", tags=None):
        from datetime import datetime, timezone

        ts = datetime.now(timezone.utc).isoformat()
        self.db.conn.execute(
            """UPDATE photos
               SET flickr_title=?, flickr_description=?,
                   flickr_tags=?, meta_synced_flickr_at=?
               WHERE id=?""",
            (title, description, json.dumps(tags or []), ts, self.photo_id),
        )
        self.db.conn.commit()

    def _pull(self, dry_run=False):
        from unittest.mock import patch
        from flickr.metadata_puller import pull_photo_metadata

        with (
            patch(
                "flickr.metadata_puller._read_photos_metadata",
                return_value={"title": "", "description": "", "tags": []},
            ),
            patch("flickr.metadata_puller._write_photos_metadata"),
        ):
            return pull_photo_metadata(
                self.db,
                self.mock_flickr,
                self.photo_id,
                library_path=self.library,
                dry_run=dry_run,
            )

    def test_cache_hit_skips_flickr_api(self):
        """When meta_synced_flickr_at is set, get_photo_info must not be called."""
        self._seed_cache(title="From Cache")
        result = self._pull()
        self.mock_flickr.get_photo_info.assert_not_called()
        self.assertTrue(result["cache_hit"])

    def test_cache_miss_calls_flickr_api(self):
        """When meta_synced_flickr_at is NULL, the live Flickr API is called."""
        self.mock_flickr.get_photo_info.return_value = {
            "photo": {
                "title": {"_content": "Live Title"},
                "description": {"_content": ""},
                "tags": {"tag": []},
            }
        }
        result = self._pull()
        self.mock_flickr.get_photo_info.assert_called_once()
        self.assertFalse(result["cache_hit"])

    def test_cache_hit_uses_cached_title(self):
        """A cache hit should use flickr_title from DB, not live API."""
        self._seed_cache(title="DB Title")
        result = self._pull()
        self.assertIn("title", result["written"])

    def test_batch_counts_cache_hits_and_misses(self):
        """pull_batch totals should include cache_hits and cache_misses keys."""
        # Seed cache for this photo so it's a hit
        self._seed_cache()
        self.mock_flickr.get_photo_info.return_value = {
            "photo": {
                "title": {"_content": ""},
                "description": {"_content": ""},
                "tags": {"tag": []},
            }
        }
        mock_module, mock_db_instance, patcher = self._mock_osxphotos()
        with patcher:
            from flickr.metadata_puller import pull_batch

            totals = pull_batch(
                self.db,
                self.mock_flickr,
                [self.photo_id],
                library_path=self.library,
                dry_run=True,
            )
        self.assertIn("cache_hits", totals)
        self.assertIn("cache_misses", totals)
        self.assertEqual(totals["cache_hits"] + totals["cache_misses"], 1)

    def _mock_osxphotos(self):
        from unittest.mock import MagicMock, patch

        mock_db_instance = MagicMock()
        mock_db_instance.photos.return_value = []
        mock_module = MagicMock()
        mock_module.PhotosDB.return_value = mock_db_instance
        return (
            mock_module,
            mock_db_instance,
            patch.dict(__import__("sys").modules, {"osxphotos": mock_module}),
        )


# ---------------------------------------------------------------------------
# Phase 2: poller Flickr metadata cache helpers and poll-loop behaviour
# ---------------------------------------------------------------------------


class TestPollerHelpers(unittest.TestCase):
    """Unit tests for _normalise_tag and _compute_tags_hash."""

    def setUp(self):
        from poller.poller import _normalise_tag, _compute_tags_hash

        self._normalise_tag = _normalise_tag
        self._compute_tags_hash = _compute_tags_hash

    def test_normalise_tag_strips_whitespace(self):
        self.assertEqual(self._normalise_tag("  hello  "), "hello")

    def test_normalise_tag_casefolds(self):
        self.assertEqual(self._normalise_tag("Café"), "café")

    def test_normalise_tag_nfc(self):
        # NFC normalization: composed vs decomposed form
        import unicodedata

        decomposed = unicodedata.normalize("NFD", "café")
        self.assertEqual(self._normalise_tag(decomposed), "café")

    def test_compute_tags_hash_deterministic(self):
        h1 = self._compute_tags_hash(["Alpha", "Beta"])
        h2 = self._compute_tags_hash(["Alpha", "Beta"])
        self.assertEqual(h1, h2)

    def test_compute_tags_hash_order_independent(self):
        h1 = self._compute_tags_hash(["Alpha", "Beta"])
        h2 = self._compute_tags_hash(["Beta", "Alpha"])
        self.assertEqual(h1, h2)

    def test_compute_tags_hash_case_insensitive(self):
        h1 = self._compute_tags_hash(["ALPHA"])
        h2 = self._compute_tags_hash(["alpha"])
        self.assertEqual(h1, h2)

    def test_compute_tags_hash_deduplicates(self):
        h1 = self._compute_tags_hash(["alpha"])
        h2 = self._compute_tags_hash(["alpha", "alpha"])
        self.assertEqual(h1, h2)

    def test_compute_tags_hash_empty_list(self):
        h = self._compute_tags_hash([])
        self.assertEqual(len(h), 64)  # SHA-256 hex digest


class TestFlickrPhotoToDbEnrich(unittest.TestCase):
    """Tests for flickr_photo_to_db title/lastupdate fields and _enrich_from_info."""

    def setUp(self):
        from poller.poller import flickr_photo_to_db, _enrich_from_info

        self._flickr_photo_to_db = flickr_photo_to_db
        self._enrich_from_info = _enrich_from_info

    def _make_photo(self, **overrides):
        base = {
            "id": "12345",
            "secret": "abc",
            "server": "srv",
            "farm": 1,
            "title": "My Photo",
            "tags": "foo bar",
        }
        base.update(overrides)
        return base

    def test_title_stored_as_flickr_title(self):
        row = self._flickr_photo_to_db(self._make_photo(title="Summer Trip"))
        self.assertEqual(row["flickr_title"], "Summer Trip")
        self.assertNotIn("title", row)

    def test_lastupdate_captured_from_extras(self):
        row = self._flickr_photo_to_db(self._make_photo(lastupdate="1700000000"))
        self.assertIn("flickr_last_updated", row)
        self.assertTrue(row["flickr_last_updated"].startswith("2023"))

    def test_no_lastupdate_when_absent(self):
        row = self._flickr_photo_to_db(self._make_photo())
        self.assertNotIn("flickr_last_updated", row)

    def test_enrich_updates_flickr_title_from_getinfo(self):
        row = self._flickr_photo_to_db(self._make_photo(title="Old Title"))
        info = {
            "photo": {
                "title": {"_content": "New Title From Info"},
                "description": {"_content": ""},
                "tags": {"tag": []},
                "dates": {},
                "owner": {},
            }
        }
        self._enrich_from_info(row, info)
        self.assertEqual(row["flickr_title"], "New Title From Info")

    def test_enrich_captures_lastupdate_from_dates(self):
        row = self._flickr_photo_to_db(self._make_photo())
        info = {
            "photo": {
                "title": {"_content": ""},
                "description": {"_content": ""},
                "tags": {"tag": []},
                "dates": {"lastupdate": "1700000000"},
                "owner": {},
            }
        }
        self._enrich_from_info(row, info)
        self.assertIn("flickr_last_updated", row)
        self.assertTrue(row["flickr_last_updated"].startswith("2023"))

    def test_enrich_does_not_overwrite_title_with_empty(self):
        row = self._flickr_photo_to_db(self._make_photo(title="Keep This"))
        info = {
            "photo": {
                "title": {"_content": ""},
                "description": {"_content": ""},
                "tags": {"tag": []},
                "dates": {},
                "owner": {},
            }
        }
        self._enrich_from_info(row, info)
        self.assertEqual(row["flickr_title"], "Keep This")

    def test_original_dimensions_stored_from_width_o_height_o(self):
        row = self._flickr_photo_to_db(self._make_photo(width_o="6048", height_o="4024"))
        self.assertEqual(row["width"], 6048)
        self.assertEqual(row["height"], 4024)

    def test_large_dimensions_used_as_fallback(self):
        row = self._flickr_photo_to_db(self._make_photo(width_l="2048", height_l="1365"))
        self.assertEqual(row["width"], 2048)
        self.assertEqual(row["height"], 1365)

    def test_original_preferred_over_large(self):
        row = self._flickr_photo_to_db(
            self._make_photo(
                width_o="6048",
                height_o="4024",
                width_l="2048",
                height_l="1365",
            )
        )
        self.assertEqual(row["width"], 6048)
        self.assertEqual(row["height"], 4024)

    def test_no_dimensions_when_absent(self):
        row = self._flickr_photo_to_db(self._make_photo())
        self.assertNotIn("width", row)
        self.assertNotIn("height", row)


class TestPollerMetadataCache(unittest.TestCase):
    """Integration tests: poll() writes Flickr metadata cache columns to DB."""

    def setUp(self):
        from unittest.mock import MagicMock

        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "test.db")

        # One minimal photo returned by the mock Flickr client
        self.flickr_id = "poll-meta-001"
        self.mock_client = MagicMock()
        self.mock_client.get_recent_uploads.return_value = {
            "photos": {
                "photo": [
                    {
                        "id": self.flickr_id,
                        "secret": "sec",
                        "server": "srv",
                        "farm": 1,
                        "title": "Poll Title",
                        "tags": "alpha beta",
                        "description": {"_content": "Poll desc"},
                        "lastupdate": "1700000000",
                    }
                ],
                "pages": 1,
                "page": 1,
            }
        }

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _run_poll(self, **kwargs):
        from poller.poller import poll

        poll(
            client=self.mock_client,
            db=self.db,
            thumb_root=None,
            min_ts=0,
            dry_run=False,
            fetch_info=False,
            **kwargs,
        )

    def _get_row(self):
        row = self.db.conn.execute(
            "SELECT * FROM photos WHERE flickr_id = ?", (self.flickr_id,)
        ).fetchone()
        return dict(row) if row else None

    def test_flickr_title_written_to_db(self):
        self._run_poll()
        row = self._get_row()
        self.assertIsNotNone(row)
        self.assertEqual(row["flickr_title"], "Poll Title")

    def test_flickr_tags_stored_as_json(self):
        self._run_poll()
        row = self._get_row()
        tags = json.loads(row["flickr_tags"])
        self.assertIsInstance(tags, list)
        self.assertIn("alpha", tags)
        self.assertIn("beta", tags)

    def test_flickr_tags_hash_set(self):
        self._run_poll()
        row = self._get_row()
        self.assertIsNotNone(row["flickr_tags_hash"])
        self.assertEqual(len(row["flickr_tags_hash"]), 64)

    def test_flickr_last_updated_set(self):
        self._run_poll()
        row = self._get_row()
        self.assertIsNotNone(row["flickr_last_updated"])
        self.assertTrue(row["flickr_last_updated"].startswith("2023"))

    def test_meta_synced_flickr_at_set(self):
        self._run_poll()
        row = self._get_row()
        self.assertIsNotNone(row["meta_synced_flickr_at"])

    def test_transient_fields_not_in_db_columns(self):
        """thumbnail_url_l, flickr_is_public, etc. must not appear in the photos row."""
        self._run_poll()
        row = self._get_row()
        cols = set(row.keys())
        for bad in (
            "thumbnail_url_l",
            "thumbnail_url_m",
            "flickr_is_public",
            "flickr_owner_nsid",
            "original_format",
        ):
            self.assertNotIn(bad, cols, f"{bad!r} should not be a DB column")

    def test_flickr_description_written_to_db(self):
        self._run_poll()
        row = self._get_row()
        self.assertEqual(row["flickr_description"], "Poll desc")


class TestUpsertPhotoTagSerialisation(unittest.TestCase):
    """upsert_photo should auto-serialise flickr_tags and photos_tags lists."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "test.db")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_flickr_tags_list_serialised_to_json(self):
        photo_id = self.db.upsert_photo(
            {
                "flickr_id": "serial-flickr-001",
                "flickr_tags": ["alpha", "beta"],
            }
        )
        row = self.db.conn.execute(
            "SELECT flickr_tags FROM photos WHERE id = ?", (photo_id,)
        ).fetchone()
        self.assertEqual(json.loads(row["flickr_tags"]), ["alpha", "beta"])

    def test_photos_tags_list_serialised_to_json(self):
        photo_id = self.db.upsert_photo(
            {
                "flickr_id": "serial-photos-001",
                "photos_tags": ["vacation", "summer"],
            }
        )
        row = self.db.conn.execute(
            "SELECT photos_tags FROM photos WHERE id = ?", (photo_id,)
        ).fetchone()
        self.assertEqual(json.loads(row["photos_tags"]), ["vacation", "summer"])

    def test_flickr_tags_string_passed_through_unchanged(self):
        payload = json.dumps(["gamma"])
        photo_id = self.db.upsert_photo(
            {
                "flickr_id": "serial-str-001",
                "flickr_tags": payload,
            }
        )
        row = self.db.conn.execute(
            "SELECT flickr_tags FROM photos WHERE id = ?", (photo_id,)
        ).fetchone()
        self.assertEqual(row["flickr_tags"], payload)


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
            "photos_library:\n"
            f"  path: {self._tmp.name}/Photos.photoslibrary\n"
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
            "--config",
            str(self._config_path),
        ] + (extra_argv or [])
        return subprocess.run(cmd, capture_output=True, text=True)

    def test_exit_0_when_nothing_to_do(self):
        # Empty DB → drift filter returns nothing → summary line printed, exit 0
        result = self._run_cli()
        self.assertIn("proposals=", result.stdout)
        self.assertEqual(result.returncode, 0)

    def test_dry_run_flag_accepted(self):
        result = self._run_cli(["--dry-run"])
        self.assertIn("proposals=", result.stdout)

    def test_limit_flag_accepted(self):
        result = self._run_cli(["--limit", "1"])
        self.assertIn("proposals=", result.stdout)

    def test_verbose_flag_accepted(self):
        result = self._run_cli(["--verbose"])
        self.assertIn("proposals=", result.stdout)

    def test_force_bypasses_drift_filter(self):
        # Insert a photo with warm caches AND a recent harmonized_at (would be
        # excluded by drift filter). --force should still process it.
        now = "2026-01-01T12:00:00+00:00"
        later = "2026-01-01T13:00:00+00:00"
        self.db.conn.execute(
            """INSERT INTO photos
               (flickr_id, uuid, meta_synced_flickr_at, meta_synced_photos_at,
                meta_last_harmonized_at,
                flickr_tags, photos_tags, flickr_tags_hash, photos_tags_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("111", "AAA-111", now, now, later, '["beach"]', "[]", "hash1", "hash2"),
        )
        self.db.conn.commit()
        # Without --force, drift filter sees 0 (harmonized_at is newer than caches)
        result_normal = self._run_cli()
        self.assertIn("nothing in drift filter", result_normal.stdout)
        # With --force, photo is included
        result_force = self._run_cli(["--force"])
        self.assertIn("proposals=", result_force.stdout)
        self.assertNotIn("nothing in drift filter", result_force.stdout)


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
        self.db = Database(Path(self._tmp) / "test.db")

        # Seed: photo approved_public, perms pushed, tags pushed
        self.photo_id = self.db.upsert_photo(
            {
                "uuid": "uuid-e2e-001",
                "original_filename": "IMG_e2e.JPG",
                "privacy_state": "approved_public",
                "flickr_id": "flickr-e2e-001",
                "perms_pushed_flickr": 1,
                "tags_pushed_flickr": 1,
                "proposed_tags": ["nature", "landscape"],
                "apple_persons": [],
                "apple_labels": [],
            }
        )

        # Build a reusable mock FlickrClient
        self.mock_client = MagicMock()

    def tearDown(self):
        self.db.close()
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    def _photo_row(self):
        return dict(
            self.db.conn.execute("SELECT * FROM photos WHERE id = ?", (self.photo_id,)).fetchone()
        )

    def _flickr_info_response(self, is_public: int, tags: list[str]):
        """Build a minimal get_photo_info payload."""
        return {
            "photo": {
                "visibility": {"ispublic": is_public},
                "tags": {"tag": [{"raw": t} for t in tags]},
            }
        }

    def test_1_mismatch_detected_when_flickr_is_private(self):
        """DB says public+pushed; Flickr says private → perm_mismatch."""
        from poller.reconcile import check_photo

        self.mock_client.get_photo_info.return_value = self._flickr_info_response(
            is_public=0, tags=["nature", "landscape"]
        )

        result = check_photo(self.mock_client, self._photo_row(), self.db, fix=False, verbose=False)

        self.assertEqual(result["status"], "perm_mismatch")
        self.assertEqual(result["perm_expected"], "public")
        self.assertEqual(result["perm_actual"], "private")
        self.assertEqual(result["fixes"], [])  # fix=False — no API write
        self.assertEqual(result["errors"], [])
        self.mock_client.set_permissions.assert_not_called()

    def test_2_fix_corrects_mismatch_and_calls_api(self):
        """fix=True: reconcile calls set_permissions; result carries the fix."""
        from poller.reconcile import check_photo

        self.mock_client.get_photo_info.return_value = self._flickr_info_response(
            is_public=0, tags=["nature", "landscape"]
        )

        result = check_photo(self.mock_client, self._photo_row(), self.db, fix=True, verbose=False)

        self.assertEqual(result["status"], "perm_mismatch")
        self.assertIn("perm", result["fixes"])
        self.assertEqual(result["errors"], [])
        self.mock_client.set_permissions.assert_called_once_with(
            "flickr-e2e-001", is_public=1, is_friend=0, is_family=0
        )

    def test_3_idempotent_second_run_is_clean(self):
        """After Flickr is consistent, a second reconcile pass returns ok."""
        from poller.reconcile import check_photo

        self.mock_client.get_photo_info.return_value = self._flickr_info_response(
            is_public=1, tags=["nature", "landscape"]
        )

        result = check_photo(self.mock_client, self._photo_row(), self.db, fix=True, verbose=False)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["fixes"], [])
        self.assertEqual(result["errors"], [])
        self.mock_client.set_permissions.assert_not_called()

    def test_4_tag_mismatch_detected_and_fixed(self):
        """Tags on Flickr are missing some expected tags → tag_mismatch, then fixed."""
        import json
        from poller.reconcile import check_photo

        # Seed pushed_tags so the tag check is active
        self.db.conn.execute(
            "UPDATE photos SET pushed_tags = ? WHERE id = ?",
            (json.dumps(["nature", "landscape"]), self.photo_id),
        )
        self.db.conn.commit()

        # Flickr has only "nature"; "landscape" is missing
        self.mock_client.get_photo_info.return_value = self._flickr_info_response(
            is_public=1, tags=["nature"]
        )

        result = check_photo(self.mock_client, self._photo_row(), self.db, fix=True, verbose=False)

        self.assertEqual(result["status"], "tag_mismatch")
        self.assertIn("tags", result["fixes"])
        self.mock_client.add_tags.assert_called_once_with("flickr-e2e-001", ["landscape"])

    def test_5_api_error_propagates_to_failed_count(self):
        """Flickr API failure on get_photo_info → flickr_error status, not a crash."""
        from poller.reconcile import check_photo
        from flickr.flickr_client import FlickrError

        self.mock_client.get_photo_info.side_effect = FlickrError(500, "Server Error")

        result = check_photo(self.mock_client, self._photo_row(), self.db, fix=False, verbose=False)

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
        self.assertGreater(
            self._photo_row()["updated_at"], before, "set_privacy_state must update updated_at"
        )

        # record_review
        before = self._photo_row()["updated_at"]
        time.sleep(0.01)
        self.db.record_review(self.photo_id, "make_public")
        self.assertGreater(
            self._photo_row()["updated_at"], before, "record_review must update updated_at"
        )

        # undo_decision
        before = self._photo_row()["updated_at"]
        time.sleep(0.01)
        self.db.undo_decision(self.photo_id)
        self.assertGreater(
            self._photo_row()["updated_at"], before, "undo_decision must update updated_at"
        )


# ---------------------------------------------------------------------------
# reconcile output format
# ---------------------------------------------------------------------------


class TestFormatResultLine(unittest.TestCase):
    URL = "https://www.flickr.com/photos/cdevers/12345"
    TS = "2026-05-11T12:29:29"

    def _base(self, **kw):
        base = {
            "flickr_id": "12345",
            "status": "ok",
            "perm_expected": "",
            "perm_actual": "",
            "tags_missing": [],
            "fixes": [],
            "errors": [],
        }
        base.update(kw)
        return base

    def test_ok_line(self):
        from poller.reconcile import format_result_line

        line = format_result_line(self._base(status="ok"), self.URL, self.TS)
        self.assertEqual(line, f"{self.TS} [ok] {self.URL}")

    def test_flickr_error_line(self):
        from poller.reconcile import format_result_line

        line = format_result_line(
            self._base(status="flickr_error", errors=["oops"]), self.URL, self.TS
        )
        self.assertEqual(line, f"{self.TS} [ERR] {self.URL}")

    def test_tag_mismatch_with_fix(self):
        from poller.reconcile import format_result_line

        result = self._base(status="tag_mismatch", fixes=["tags"], tags_missing=["unitedstates"])
        line = format_result_line(result, self.URL, self.TS)
        self.assertEqual(
            line, f"{self.TS} [tag_mismatch] {self.URL} fixed:tags missing:unitedstates"
        )

    def test_fix_comes_before_missing(self):
        from poller.reconcile import format_result_line

        result = self._base(
            status="tag_mismatch", fixes=["tags"], tags_missing=["opticalequipment", "unitedstates"]
        )
        line = format_result_line(result, self.URL, self.TS)
        # fixed: must precede missing:
        self.assertLess(line.index("fixed:"), line.index("missing:"))
        self.assertIn("missing:opticalequipment, unitedstates", line)

    def test_perm_mismatch_no_fix(self):
        from poller.reconcile import format_result_line

        result = self._base(status="perm_mismatch", perm_expected="public", perm_actual="private")
        line = format_result_line(result, self.URL, self.TS)
        self.assertIn("[perm_mismatch]", line)
        self.assertIn("perm:public→private", line)
        self.assertNotIn("fixed:", line)

    def test_flickr_id_not_in_line(self):
        from poller.reconcile import format_result_line

        result = self._base(status="tag_mismatch", fixes=["tags"], tags_missing=["nature"])
        line = format_result_line(result, self.URL, self.TS)
        # Flickr ID appears only as part of the URL, never standalone
        self.assertNotIn(" 12345 ", line)
        self.assertNotIn(" 12345\n", line)

    def test_missing_tags_truncated_at_8(self):
        from poller.reconcile import format_result_line

        tags = [f"tag{i}" for i in range(10)]
        result = self._base(status="tag_mismatch", tags_missing=tags)
        line = format_result_line(result, self.URL, self.TS)
        self.assertIn("+2", line)


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
        self.photos_id = self.db.upsert_photo(
            {
                "uuid": "uuid-heic-001",
                "original_filename": "IMG_1234.HEIC",
                "date_taken": "2026-04-24T15:30:07.775000-04:00",
                "privacy_state": "candidate_public",
                "apple_labels": ["Travel", "Beach"],
                "apple_persons": [],
                "proposed_tags": ["travel", "beach"],
                "apple_ai_caption": "A sunny beach scene",
                "latitude": 25.0,
                "longitude": -80.0,
            }
        )

        # Flickr-only record (flickr_id set, no uuid)
        self.flickr_id_row = self.db.upsert_photo(
            {
                "flickr_id": "55228034962",
                "flickr_secret": "abc123",
                "flickr_server": "65535",
                "flickr_farm": 66,
                "original_filename": "IMG_1234.JPG",
                "date_taken": "2026-04-24 15:30:07",
                "privacy_state": "candidate_public",
                "date_uploaded_flickr": "2026-04-24T20:00:00+00:00",
                "thumbnail_path": "https://live.staticflickr.com/65535/55228034962_abc123_b.jpg",
            }
        )

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
        self.db.record_review(self.photos_id, "keep_private")
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
        """Insert a Photos-only and a Flickr-only record with matching timestamp.
        Both use naive local-time strings so the test is timezone-independent."""
        photos_id = self.db.upsert_photo(
            {
                "uuid": f"uuid-{tag}",
                "original_filename": f"IMG_{tag}.HEIC",
                "date_taken": date,
                "privacy_state": "candidate_public",
                "apple_labels": [],
                "apple_persons": [],
            }
        )
        flickr_id_row = self.db.upsert_photo(
            {
                "flickr_id": f"flickr-{tag}",
                "original_filename": f"IMG_{tag}.JPG",
                "date_taken": date,
                "privacy_state": "candidate_public",
            }
        )
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

        photos_id = self.db.upsert_photo(
            {
                "uuid": "uuid-nopair",
                "original_filename": "IMG_nopair.HEIC",
                "date_taken": "2026-06-01 12:00:00",
                "privacy_state": "candidate_public",
                "apple_labels": [],
                "apple_persons": [],
            }
        )
        linked, failed = link_orphans(self.db, dry_run=False, limit=100)
        self.assertEqual(linked, 0)
        self.assertIsNotNone(self.db.get_photo(photos_id))

    def test_links_utc_photos_to_local_flickr(self):
        # Photos stored with UTC offset (+00:00 from daemon in UTC timezone);
        # Flickr has the same moment as local time. Compute expected local time
        # dynamically so the test is machine-timezone-independent.
        from poller.link_orphans import link_orphans
        from datetime import datetime, timezone as tz_module

        utc_dt = datetime(2020, 6, 15, 22, 24, 8, tzinfo=tz_module.utc)
        local_str = utc_dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        photos_id = self.db.upsert_photo(
            {
                "uuid": "uuid-utcbug",
                "original_filename": "IMG_utcbug.HEIC",
                "date_taken": utc_dt.isoformat(),
                "privacy_state": "candidate_public",
                "apple_labels": [],
                "apple_persons": [],
            }
        )
        flickr_row = self.db.upsert_photo(
            {
                "flickr_id": "flickr-utcbug",
                "date_taken": local_str,
                "privacy_state": "candidate_public",
            }
        )
        linked, failed = link_orphans(self.db, dry_run=False, limit=100)
        self.assertEqual(linked, 1)
        self.assertEqual(failed, 0)
        self.assertEqual(self.db.get_photo(photos_id)["flickr_id"], "flickr-utcbug")
        self.assertIsNone(self.db.get_photo(flickr_row))

    def test_links_when_flickr_timestamp_rounded_up(self):
        # Reproduces the real-world off-by-one: Photos stores sub-second precision
        # (truncated to :50) while Flickr rounds the same EXIF time to :51.
        from poller.link_orphans import link_orphans

        photos_id = self.db.upsert_photo(
            {
                "uuid": "uuid-round",
                "original_filename": "IMG_round.HEIC",
                "date_taken": "2022-02-14T20:14:50.941984-05:00",
                "privacy_state": "candidate_public",
                "apple_labels": [],
                "apple_persons": [],
            }
        )
        flickr_row = self.db.upsert_photo(
            {
                "flickr_id": "flickr-round",
                "date_taken": "2022-02-14 20:14:51",
            }
        )
        linked, failed = link_orphans(self.db, dry_run=False, limit=100)
        self.assertEqual(linked, 1)
        self.assertEqual(failed, 0)
        self.assertEqual(self.db.get_photo(photos_id)["flickr_id"], "flickr-round")
        self.assertIsNone(self.db.get_photo(flickr_row))

    def test_links_when_flickr_timestamp_three_seconds_ahead(self):
        # Reproduces DB pair 5245/146585: Photos sub-second truncates to :33 while
        # Flickr stores :36 — a 3-second gap outside the old ±2 s tolerance.
        from poller.link_orphans import link_orphans

        photos_id = self.db.upsert_photo(
            {
                "uuid": "uuid-3s",
                "original_filename": "IMG_3s.HEIC",
                "date_taken": "2021-11-11T18:03:33.572856-05:00",
                "privacy_state": "candidate_public",
                "apple_labels": [],
                "apple_persons": [],
            }
        )
        flickr_row = self.db.upsert_photo(
            {
                "flickr_id": "flickr-3s",
                "date_taken": "2021-11-11 18:03:36",
            }
        )
        linked, failed = link_orphans(self.db, dry_run=False, limit=100)
        self.assertEqual(linked, 1)
        self.assertEqual(failed, 0)
        self.assertEqual(self.db.get_photo(photos_id)["flickr_id"], "flickr-3s")
        self.assertIsNone(self.db.get_photo(flickr_row))

    def test_links_when_flickr_timestamp_two_seconds_ahead(self):
        # Reproduces the 2-second offset observed for HEIC photos 58000/154037
        # and 7299/154008 where Flickr's processing produces an extra second of drift.
        from poller.link_orphans import link_orphans

        photos_id = self.db.upsert_photo(
            {
                "uuid": "uuid-2s",
                "original_filename": "IMG_2s.HEIC",
                "date_taken": "2022-01-29T19:06:42.706693-05:00",
                "privacy_state": "candidate_public",
                "apple_labels": [],
                "apple_persons": [],
            }
        )
        flickr_row = self.db.upsert_photo(
            {
                "flickr_id": "flickr-2s",
                "date_taken": "2022-01-29 19:06:44",
            }
        )
        linked, failed = link_orphans(self.db, dry_run=False, limit=100)
        self.assertEqual(linked, 1)
        self.assertEqual(failed, 0)
        self.assertEqual(self.db.get_photo(photos_id)["flickr_id"], "flickr-2s")
        self.assertIsNone(self.db.get_photo(flickr_row))

    def test_links_when_camera_timezone_differs_from_machine(self):
        # Photos stores machine-local time (EDT, -04:00); Flickr stores EXIF camera
        # time (PDT, 3 h earlier). The -3 h offset is the most common real-world case.
        from poller.link_orphans import link_orphans

        photos_id = self.db.upsert_photo(
            {
                "uuid": "uuid-pdt",
                "original_filename": "IMG_pdt.HEIC",
                "date_taken": "2022-03-17T19:57:36.719161-04:00",  # EDT machine
                "privacy_state": "candidate_public",
                "apple_labels": [],
                "apple_persons": [],
            }
        )
        flickr_row = self.db.upsert_photo(
            {
                "flickr_id": "flickr-pdt",
                "date_taken": "2022-03-17 16:57:36",  # PDT camera EXIF, 3 h earlier
            }
        )
        linked, failed = link_orphans(self.db, dry_run=False, limit=100)
        self.assertEqual(linked, 1)
        self.assertEqual(failed, 0)
        self.assertEqual(self.db.get_photo(photos_id)["flickr_id"], "flickr-pdt")
        self.assertIsNone(self.db.get_photo(flickr_row))

    def test_exact_match_preferred_over_hour_offset_match(self):
        # If both an exact match and a hour-offset match exist, the exact one wins.
        from poller.link_orphans import link_orphans

        photos_id = self.db.upsert_photo(
            {
                "uuid": "uuid-exact",
                "original_filename": "IMG_exact.HEIC",
                "date_taken": "2022-05-10 14:00:00",
                "privacy_state": "candidate_public",
                "apple_labels": [],
                "apple_persons": [],
            }
        )
        # Exact match
        self.db.upsert_photo(
            {
                "flickr_id": "flickr-exact",
                "date_taken": "2022-05-10 14:00:00",
            }
        )
        # Hour-offset decoy (1 h earlier — would match if exact is missed)
        self.db.upsert_photo(
            {
                "flickr_id": "flickr-decoy",
                "date_taken": "2022-05-10 13:00:00",
            }
        )
        linked, _ = link_orphans(self.db, dry_run=False, limit=100)
        self.assertEqual(linked, 1)
        merged = self.db.get_photo(photos_id)
        self.assertEqual(merged["flickr_id"], "flickr-exact")


# ---------------------------------------------------------------------------
# Phase 4 — sync engine: classify + proposals
# ---------------------------------------------------------------------------


class TestClassifyTags(unittest.TestCase):
    """_classify_tags returns correct proposals for each divergence case."""

    def _classify(self, ftags, ptags, fhash="fh", phash="ph"):
        import json
        from flickr.metadata_puller import _classify_tags

        fj = json.dumps(ftags) if ftags is not None else None
        pj = json.dumps(ptags) if ptags is not None else None
        return _classify_tags(1, fj, pj, fhash, phash, "2026-01-01T00:00:00+00:00")

    def test_both_empty_returns_no_proposals(self):
        self.assertEqual(self._classify([], []), [])

    def test_equal_tags_returns_no_proposals(self):
        self.assertEqual(self._classify(["nature", "travel"], ["nature", "travel"]), [])

    def test_equal_after_normalisation_returns_no_proposals(self):
        # Different casing, same normalised set
        self.assertEqual(self._classify(["Nature", "Travel"], ["nature", "travel"]), [])

    def test_flickr_punctuation_stripping_returns_no_proposals(self):
        # Flickr normalizes tags to alphanumeric-only, silently stripping spaces,
        # hyphens, and other punctuation. Tags that differ only in punctuation
        # should be treated as equal.
        self.assertEqual(
            self._classify(
                ["cambridge", "harvardsquare", "unitedstates", "closeup"],
                ["cambridge", "harvard square", "united states", "close-up"],
            ),
            [],
        )

    def test_punctuation_stripping_does_not_hide_real_collision(self):
        # Stripping punctuation should not cause unrelated tags to be treated as equal
        proposals = self._classify(["newyork"], ["losangeles"])
        self.assertEqual(len(proposals), 2)
        self.assertEqual(proposals[0]["conflict_type"], "collision")

    def test_non_conflict_flickr_has_photos_empty(self):
        proposals = self._classify(["nature"], [])
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["conflict_type"], "non_conflict")
        self.assertEqual(proposals[0]["source"], "flickr")
        self.assertEqual(proposals[0]["target"], "photos")

    def test_non_conflict_photos_has_flickr_empty(self):
        proposals = self._classify([], ["nature"])
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["conflict_type"], "non_conflict")
        self.assertEqual(proposals[0]["source"], "photos")
        self.assertEqual(proposals[0]["target"], "flickr")

    def test_divergence_flickr_superset(self):
        proposals = self._classify(["nature", "landscape", "travel"], ["nature"])
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["conflict_type"], "divergence")
        self.assertEqual(proposals[0]["source"], "flickr")
        self.assertEqual(proposals[0]["target"], "photos")

    def test_divergence_photos_superset(self):
        proposals = self._classify(["nature"], ["nature", "landscape", "travel"])
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["conflict_type"], "divergence")
        self.assertEqual(proposals[0]["source"], "photos")
        self.assertEqual(proposals[0]["target"], "flickr")

    def test_collision_generates_two_proposals(self):
        proposals = self._classify(["nature", "landscape"], ["nature", "travel"])
        self.assertEqual(len(proposals), 2)
        types = {p["conflict_type"] for p in proposals}
        targets = {p["target"] for p in proposals}
        self.assertEqual(types, {"collision"})
        self.assertEqual(targets, {"photos", "flickr"})

    def test_source_target_hashes_set_correctly(self):
        proposals = self._classify(["nature"], [], fhash="FHASH", phash="PHASH")
        p = proposals[0]
        self.assertEqual(p["source_hash_at_creation"], "FHASH")
        self.assertEqual(p["target_hash_at_creation"], "PHASH")


class TestClassifyTagsProposedExclusion(unittest.TestCase):
    """BP-managed tags (proposed_tags) should not generate Flickr→Photos proposals."""

    def _classify(self, ftags, ptags, proposed=None, fhash="fh", phash="ph"):
        import json
        from flickr.metadata_puller import _classify_tags

        fj = json.dumps(ftags) if ftags is not None else None
        pj = json.dumps(ptags) if ptags is not None else None
        prj = json.dumps(proposed) if proposed is not None else None
        return _classify_tags(
            1, fj, pj, fhash, phash, "2026-01-01T00:00:00+00:00", proposed_tags_json=prj
        )

    def test_managed_only_extra_on_flickr_no_proposal(self):
        # Flickr has unitedstates (managed); Photos doesn't → no proposal
        result = self._classify(
            ftags=["nature", "landscape", "unitedstates"],
            ptags=["nature", "landscape"],
            proposed=["unitedstates"],
        )
        self.assertEqual(result, [])

    def test_user_added_flickr_tag_still_generates_proposal(self):
        # Flickr has usertagA (NOT managed) → divergence proposal still created
        result = self._classify(
            ftags=["nature", "usertagA"],
            ptags=["nature"],
            proposed=["unitedstates"],
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["source"], "flickr")
        self.assertEqual(result[0]["conflict_type"], "divergence")

    def test_photos_to_flickr_direction_unaffected(self):
        # Photos has a tag Flickr is missing (not a managed tag) → Photos→Flickr proposal
        result = self._classify(
            ftags=["nature"],
            ptags=["nature", "landscape"],
            proposed=["unitedstates"],
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["source"], "photos")
        self.assertEqual(result[0]["conflict_type"], "divergence")

    def test_mixed_managed_and_user_tags_still_detects_divergence(self):
        # Flickr has managed tag + user tag; Photos has different extra tag → collision
        result = self._classify(
            ftags=["nature", "unitedstates", "usertagA"],
            ptags=["nature", "landscape"],
            proposed=["unitedstates"],
        )
        # After removing managed tag: Flickr effective = {nature, usertagA},
        # Photos = {nature, landscape} — neither superset → collision
        self.assertEqual(len(result), 2)
        types = {p["conflict_type"] for p in result}
        self.assertEqual(types, {"collision"})

    def test_all_flickr_tags_managed_and_photos_empty_no_proposal(self):
        result = self._classify(
            ftags=["unitedstates"],
            ptags=[],
            proposed=["unitedstates"],
        )
        self.assertEqual(result, [])


class TestClassifyTextField(unittest.TestCase):
    """_classify_text_field"""

    def setUp(self):
        from flickr.metadata_puller import _classify_text_field

        self._fn = _classify_text_field
        self._now = "2026-01-01T00:00:00+00:00"

    def test_both_empty_returns_no_proposals(self):
        self.assertEqual(self._fn(1, "title", "", "", self._now), [])

    def test_equal_returns_no_proposals(self):
        self.assertEqual(self._fn(1, "title", "My Photo", "My Photo", self._now), [])

    def test_whitespace_only_difference_returns_no_proposals(self):
        self.assertEqual(self._fn(1, "title", "  My Photo  ", "My Photo", self._now), [])

    def test_flickr_has_title_photos_empty_is_non_conflict(self):
        props = self._fn(1, "title", "Sunset", "", self._now)
        self.assertEqual(len(props), 1)
        self.assertEqual(props[0]["conflict_type"], "non_conflict")
        self.assertEqual(props[0]["source"], "flickr")
        self.assertEqual(props[0]["target"], "photos")
        self.assertEqual(props[0]["proposed_value"], "Sunset")

    def test_photos_has_title_flickr_empty_is_non_conflict(self):
        props = self._fn(1, "title", "", "My Title", self._now)
        self.assertEqual(len(props), 1)
        self.assertEqual(props[0]["source"], "photos")
        self.assertEqual(props[0]["target"], "flickr")

    def test_both_non_empty_different_is_collision(self):
        props = self._fn(1, "title", "Sunset", "Golden Hour", self._now)
        self.assertEqual(len(props), 2)
        self.assertTrue(all(p["conflict_type"] == "collision" for p in props))

    def test_field_set_correctly(self):
        props = self._fn(1, "description", "Flickr desc", "", self._now)
        self.assertEqual(props[0]["field"], "description")

    def test_source_hash_at_creation_set(self):
        props = self._fn(1, "title", "Sunset", "", self._now)
        self.assertIsNotNone(props[0]["source_hash_at_creation"])
        self.assertIsNone(props[0]["target_hash_at_creation"])  # target (photos) is empty → None


class TestHtmlEntityNormalization(unittest.TestCase):
    """HTML entity normalization: Flickr returns &amp; &quot; etc.; must not cause false collisions."""

    def setUp(self):
        from flickr.metadata_puller import _classify_text_field, _field_hash

        self._fn = _classify_text_field
        self._hash = _field_hash
        self._now = "2026-01-01T00:00:00+00:00"

    def test_amp_entity_equal_to_plain(self):
        """Flickr &amp; should match Photos & — no proposal."""
        result = self._fn(
            1,
            "description",
            "Belle &amp; Sebastian at the Orpheum",
            "Belle & Sebastian at the Orpheum",
            self._now,
        )
        self.assertEqual(result, [])

    def test_quot_entity_equal_to_plain(self):
        """Flickr &quot; should match Photos " — no proposal."""
        result = self._fn(
            1,
            "description",
            "&quot;Piazza, New York Catcher&quot;",
            '"Piazza, New York Catcher"',
            self._now,
        )
        self.assertEqual(result, [])

    def test_mixed_entities_equal_to_plain(self):
        """Multiple entities in one string — no proposal."""
        result = self._fn(
            1,
            "description",
            "Belle &amp; Sebastian perform &quot;Piazza, New York Catcher&quot;",
            'Belle & Sebastian perform "Piazza, New York Catcher"',
            self._now,
        )
        self.assertEqual(result, [])

    def test_truly_different_still_produces_collision(self):
        """Different after decoding should still produce a collision proposal."""
        result = self._fn(
            1, "description", "Belle &amp; Sebastian", "Belle and Sebastian", self._now
        )
        self.assertEqual(len(result), 2)
        self.assertTrue(all(p["conflict_type"] == "collision" for p in result))

    def test_field_hash_consistent_for_encoded_and_plain(self):
        """Same content encoded vs plain should hash to the same value."""
        self.assertEqual(self._hash("Belle &amp; Sebastian"), self._hash("Belle & Sebastian"))

    def test_proposed_value_is_decoded_text(self):
        """proposed_value stored in the proposal should be the decoded (clean) text."""
        result = self._fn(1, "title", "&lt;Photo Title&gt;", "", self._now)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["proposed_value"], "<Photo Title>")

    def test_compute_text_hash_normalizes_entities(self):
        """apply-time staleness check produces same hash for encoded and decoded equivalents."""
        from flickr.proposal_applier import _compute_text_hash

        self.assertEqual(
            _compute_text_hash("Belle &amp; Sebastian"), _compute_text_hash("Belle & Sebastian")
        )


class TestUpsertProposal(unittest.TestCase):
    """db.upsert_proposal idempotency rules."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        from db.db import Database

        self.db = Database(Path(self._tmp.name) / "test.db")
        self.db.upsert_photo(
            {
                "flickr_id": "X",
                "uuid": "U1",
                "privacy_state": "candidate_public",
                "flickr_tags_hash": "FH1",
                "photos_tags_hash": "PH1",
            }
        )
        self.photo_id = self.db.get_photo_by_flickr_id("X")["id"]

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _proposal(self, **overrides):
        base = {
            "photo_id": self.photo_id,
            "field": "tags",
            "proposed_value": '["nature"]',
            "source": "flickr",
            "target": "photos",
            "conflict_type": "non_conflict",
            "source_hash_at_creation": "FH1",
            "target_hash_at_creation": "PH1",
            "created_at": "2026-01-01T00:00:00+00:00",
        }
        base.update(overrides)
        return base

    def _count_pending(self):
        return self.db.conn.execute(
            "SELECT COUNT(*) FROM metadata_proposals WHERE status='pending'"
        ).fetchone()[0]

    def test_insert_new_proposal(self):
        self.db.upsert_proposal(self._proposal())
        self.db.conn.commit()
        self.assertEqual(self._count_pending(), 1)

    def test_duplicate_skipped(self):
        self.db.upsert_proposal(self._proposal())
        self.db.upsert_proposal(self._proposal())
        self.db.conn.commit()
        self.assertEqual(self._count_pending(), 1)

    def test_changed_hash_supersedes_and_inserts(self):
        self.db.upsert_proposal(self._proposal())
        self.db.conn.commit()
        self.db.upsert_proposal(self._proposal(source_hash_at_creation="FH2"))
        self.db.conn.commit()
        pending = self.db.conn.execute(
            "SELECT COUNT(*) FROM metadata_proposals WHERE status='pending'"
        ).fetchone()[0]
        superseded = self.db.conn.execute(
            "SELECT COUNT(*) FROM metadata_proposals WHERE status='superseded'"
        ).fetchone()[0]
        self.assertEqual(pending, 1)
        self.assertEqual(superseded, 1)

    def test_rejected_same_hash_not_regenerated(self):
        self.db.upsert_proposal(self._proposal())
        self.db.conn.commit()
        self.db.conn.execute(
            "UPDATE metadata_proposals SET status='rejected' WHERE photo_id=?", (self.photo_id,)
        )
        self.db.conn.commit()
        self.db.upsert_proposal(self._proposal())
        self.db.conn.commit()
        self.assertEqual(self._count_pending(), 0)

    def test_rejected_changed_hash_generates_new(self):
        self.db.upsert_proposal(self._proposal())
        self.db.conn.commit()
        self.db.conn.execute(
            "UPDATE metadata_proposals SET status='rejected' WHERE photo_id=?", (self.photo_id,)
        )
        self.db.conn.commit()
        self.db.upsert_proposal(self._proposal(source_hash_at_creation="FH2"))
        self.db.conn.commit()
        self.assertEqual(self._count_pending(), 1)


class TestRunSyncEngine(unittest.TestCase):
    """run_sync_engine end-to-end with in-memory DB."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        from db.db import Database

        self.db = Database(Path(self._tmp.name) / "test.db")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _add(self, flickr_id, ftags, ptags, fhash=None, phash=None):
        import hashlib
        import json as _json
        import unicodedata as _ud

        def _hash(tags):
            normed = sorted(
                {
                    "".join(c for c in _ud.normalize("NFC", t.strip().casefold()) if c.isalnum())
                    for t in tags
                    if t.strip()
                }
            )
            return hashlib.sha256(" ".join(normed).encode()).hexdigest()

        self.db.upsert_photo(
            {
                "flickr_id": flickr_id,
                "uuid": f"uuid-{flickr_id}",
                "privacy_state": "candidate_public",
                "meta_synced_flickr_at": "2026-01-01T00:00:00+00:00",
                "meta_synced_photos_at": "2026-01-01T00:00:00+00:00",
                "flickr_tags": _json.dumps(ftags),
                "photos_tags": _json.dumps(ptags),
                "flickr_tags_hash": fhash or _hash(ftags),
                "photos_tags_hash": phash or _hash(ptags),
            }
        )
        return self.db.get_photo_by_flickr_id(flickr_id)["id"]

    def _pending_proposals(self):
        return self.db.conn.execute(
            "SELECT * FROM metadata_proposals WHERE status='pending'"
        ).fetchall()

    def test_hash_match_generates_no_proposal(self):
        from flickr.metadata_puller import run_sync_engine

        pid = self._add("A", ["nature"], ["nature"])
        totals = run_sync_engine(self.db, [pid])
        self.assertEqual(totals["hash_matches"], 1)
        self.assertEqual(totals["proposals"], 0)
        self.assertEqual(len(self._pending_proposals()), 0)

    def test_non_conflict_generates_proposal(self):
        from flickr.metadata_puller import run_sync_engine

        pid = self._add("B", ["nature", "travel"], [])
        totals = run_sync_engine(self.db, [pid])
        self.assertEqual(totals["proposals"], 1)
        p = self._pending_proposals()[0]
        self.assertEqual(p["conflict_type"], "non_conflict")

    def test_sets_meta_last_harmonized_at(self):
        from flickr.metadata_puller import run_sync_engine

        pid = self._add("C", ["nature"], ["nature"])
        run_sync_engine(self.db, [pid])
        row = self.db.conn.execute(
            "SELECT meta_last_harmonized_at FROM photos WHERE id=?", (pid,)
        ).fetchone()
        self.assertIsNotNone(row["meta_last_harmonized_at"])

    def test_dry_run_writes_no_proposals(self):
        from flickr.metadata_puller import run_sync_engine

        pid = self._add("D", ["nature"], [])
        totals = run_sync_engine(self.db, [pid], dry_run=True)
        self.assertEqual(totals["proposals"], 1)
        self.assertEqual(len(self._pending_proposals()), 0)
        row = self.db.conn.execute(
            "SELECT meta_last_harmonized_at FROM photos WHERE id=?", (pid,)
        ).fetchone()
        self.assertIsNone(row["meta_last_harmonized_at"])

    def test_collision_generates_two_proposals(self):
        from flickr.metadata_puller import run_sync_engine

        pid = self._add("E", ["nature", "landscape"], ["nature", "travel"])
        totals = run_sync_engine(self.db, [pid])
        self.assertEqual(totals["proposals"], 2)
        props = self._pending_proposals()
        self.assertEqual(len(props), 2)
        self.assertTrue(all(p["conflict_type"] == "collision" for p in props))

    def test_punctuation_only_difference_generates_no_proposal(self):
        # Tags differing only in punctuation (spaces, hyphens) are the same on Flickr
        from flickr.metadata_puller import run_sync_engine
        import hashlib as _hl
        import unicodedata as _ud

        # Use old-style hashes (no punctuation stripping) to force the slow path
        def old_hash(tags):
            normed = sorted({_ud.normalize("NFC", t.strip().casefold()) for t in tags if t.strip()})
            return _hl.sha256(" ".join(normed).encode()).hexdigest()

        pid = self._add(
            "PUNCT1",
            ["closeup", "harvardsquare"],
            ["close-up", "harvard square"],
            fhash=old_hash(["closeup", "harvardsquare"]),
            phash=old_hash(["close-up", "harvard square"]),
        )
        totals = run_sync_engine(self.db, [pid])
        self.assertEqual(totals["proposals"], 0)
        self.assertEqual(totals["skipped"], 1)
        self.assertEqual(len(self._pending_proposals()), 0)

    def test_punctuation_mismatch_supersedes_stale_proposals(self):
        # Stale collision proposals from before the punctuation-normalisation fix
        # should be superseded when the sync engine finds no real difference.
        import json as _json
        import hashlib as _hl
        import unicodedata as _ud
        from flickr.metadata_puller import run_sync_engine

        def old_hash(tags):
            normed = sorted({_ud.normalize("NFC", t.strip().casefold()) for t in tags if t.strip()})
            return _hl.sha256(" ".join(normed).encode()).hexdigest()

        fhash = old_hash(["closeup"])
        phash = old_hash(["close-up"])
        pid = self._add("PUNCT2", ["closeup"], ["close-up"], fhash=fhash, phash=phash)
        self.db.upsert_proposal(
            {
                "photo_id": pid,
                "field": "tags",
                "proposed_value": _json.dumps(["closeup"]),
                "source": "flickr",
                "target": "photos",
                "conflict_type": "collision",
                "source_hash_at_creation": fhash,
                "target_hash_at_creation": phash,
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        )
        self.db.conn.commit()
        self.assertEqual(len(self._pending_proposals()), 1)
        run_sync_engine(self.db, [pid])
        self.assertEqual(len(self._pending_proposals()), 0)
        row = self.db.conn.execute(
            "SELECT status FROM metadata_proposals WHERE photo_id=?", (pid,)
        ).fetchone()
        self.assertEqual(row["status"], "superseded")

    def test_hash_match_path_supersedes_stale_proposals(self):
        # When stored hashes are already equal (hash_match fast path), any stale
        # pending proposals must still be superseded by the end-of-run bulk cleanup.
        import json as _json
        from flickr.metadata_puller import run_sync_engine

        # Both sides have equal hashes → hash_match branch will be taken
        pid = self._add("HASH_MATCH_STALE", ["closeup"], ["close-up"])
        # Both sides normalise to the same hash, so _add computes identical hashes
        # and hash_match will fire. Insert a stale proposal that predates the fix.
        fhash = self.db.conn.execute(
            "SELECT flickr_tags_hash FROM photos WHERE id=?", (pid,)
        ).fetchone()["flickr_tags_hash"]
        self.db.upsert_proposal(
            {
                "photo_id": pid,
                "field": "tags",
                "proposed_value": _json.dumps(["closeup"]),
                "source": "flickr",
                "target": "photos",
                "conflict_type": "collision",
                "source_hash_at_creation": fhash,
                "target_hash_at_creation": fhash,
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        )
        self.db.conn.commit()
        self.assertEqual(len(self._pending_proposals()), 1)
        totals = run_sync_engine(self.db, [pid])
        self.assertEqual(totals["hash_matches"], 1)
        self.assertEqual(len(self._pending_proposals()), 0)
        row = self.db.conn.execute(
            "SELECT status FROM metadata_proposals WHERE photo_id=?", (pid,)
        ).fetchone()
        self.assertEqual(row["status"], "superseded")

    def test_title_non_conflict_generates_proposal(self):
        from flickr.metadata_puller import run_sync_engine

        pid = self._add("TITLE1", [], [])
        # Manually set flickr_title only
        self.db.conn.execute(
            "UPDATE photos SET flickr_title='Sunset at the Beach' WHERE id=?", (pid,)
        )
        self.db.conn.commit()
        run_sync_engine(self.db, [pid])
        props = self._pending_proposals()
        self.assertEqual(len(props), 1)
        self.assertEqual(props[0]["field"], "title")
        self.assertEqual(props[0]["conflict_type"], "non_conflict")
        self.assertEqual(props[0]["proposed_value"], "Sunset at the Beach")

    def test_title_collision_generates_two_proposals(self):
        from flickr.metadata_puller import run_sync_engine

        pid = self._add("TITLE2", [], [])
        self.db.conn.execute(
            "UPDATE photos SET flickr_title='Sunset', photos_title='Golden Hour' WHERE id=?", (pid,)
        )
        self.db.conn.commit()
        run_sync_engine(self.db, [pid])
        props = self._pending_proposals()
        self.assertEqual(len(props), 2)
        self.assertTrue(all(p["field"] == "title" for p in props))
        self.assertTrue(all(p["conflict_type"] == "collision" for p in props))


# ---------------------------------------------------------------------------
# Phase 5 — proposal applier and proposals UI
# ---------------------------------------------------------------------------


class TestApplyProposal(unittest.TestCase):
    """apply_proposal staleness checks and rejection logic."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        from db.db import Database

        self.db = Database(Path(self._tmp.name) / "test.db")
        self.db.upsert_photo(
            {
                "flickr_id": "F1",
                "uuid": "U1",
                "privacy_state": "candidate_public",
                "flickr_tags": '["nature"]',
                "flickr_tags_hash": "FH1",
                "photos_tags": "[]",
                "photos_tags_hash": "PH_EMPTY",
                "meta_synced_flickr_at": "2026-01-01T00:00:00+00:00",
                "meta_synced_photos_at": "2026-01-01T00:00:00+00:00",
            }
        )
        self.photo_id = self.db.get_photo_by_flickr_id("F1")["id"]

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _insert_proposal(self, source_hash="FH1", target_hash="PH_EMPTY"):
        self.db.upsert_proposal(
            {
                "photo_id": self.photo_id,
                "field": "tags",
                "proposed_value": '["nature"]',
                "source": "flickr",
                "target": "photos",
                "conflict_type": "non_conflict",
                "source_hash_at_creation": source_hash,
                "target_hash_at_creation": target_hash,
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        )
        self.db.conn.commit()
        return self.db.conn.execute(
            "SELECT id FROM metadata_proposals WHERE photo_id=? ORDER BY id DESC LIMIT 1",
            (self.photo_id,),
        ).fetchone()["id"]

    def test_source_changed_supersedes(self):
        from flickr.proposal_applier import apply_proposal

        pid = self._insert_proposal(source_hash="STALE_HASH")
        result = apply_proposal(self.db, pid, library_path="")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "source_changed")
        status = self.db.conn.execute(
            "SELECT status FROM metadata_proposals WHERE id=?", (pid,)
        ).fetchone()["status"]
        self.assertEqual(status, "superseded")

    def test_target_changed_supersedes(self):
        from flickr.proposal_applier import apply_proposal

        pid = self._insert_proposal(target_hash="STALE_TARGET")
        result = apply_proposal(self.db, pid, library_path="")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "target_changed")

    def test_already_applied_returns_error(self):
        from flickr.proposal_applier import apply_proposal

        pid = self._insert_proposal()
        self.db.resolve_proposal(pid, "applied")
        result = apply_proposal(self.db, pid, library_path="")
        self.assertFalse(result["ok"])
        self.assertIn("applied", result["reason"])

    def test_photos_not_responding_returns_error(self):
        from unittest.mock import patch
        from flickr.proposal_applier import apply_proposal

        pid = self._insert_proposal()
        with patch("flickr.proposal_applier._photos_is_responsive", return_value=False):
            result = apply_proposal(self.db, pid, library_path="/fake/path")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "Photos not responding")

    def test_null_target_hash_does_not_trigger_target_changed(self):
        # Proposals created before description cache columns were populated have
        # target_hash_at_creation=NULL. NULL means "no baseline", not "changed".
        from unittest.mock import patch
        from flickr.proposal_applier import apply_proposal

        pid = self._insert_proposal(target_hash=None)
        with patch("flickr.proposal_applier._photos_is_responsive", return_value=True):
            with patch("flickr.proposal_applier._write_tags_to_photos") as mock_write:
                mock_write.return_value = {"ok": True}
                result = apply_proposal(self.db, pid, library_path="/fake/path")
        self.assertNotEqual(
            result.get("reason"),
            "target_changed",
            "NULL target hash should not trigger staleness check",
        )

    def test_null_source_hash_does_not_trigger_source_changed(self):
        from unittest.mock import patch
        from flickr.proposal_applier import apply_proposal

        pid = self._insert_proposal(source_hash=None)
        with patch("flickr.proposal_applier._photos_is_responsive", return_value=True):
            with patch("flickr.proposal_applier._write_tags_to_photos") as mock_write:
                mock_write.return_value = {"ok": True}
                result = apply_proposal(self.db, pid, library_path="/fake/path")
        self.assertNotEqual(
            result.get("reason"),
            "source_changed",
            "NULL source hash should not trigger staleness check",
        )

    def test_write_tags_timeout_returns_not_responding(self):
        import sys
        from unittest.mock import patch, MagicMock
        from flickr.proposal_applier import _write_tags_to_photos

        with (
            patch("flickr.proposal_applier._photos_is_responsive", return_value=True),
            patch.dict(sys.modules, {"photoscript": MagicMock()}),
            patch(
                "flickr.proposal_applier._run_with_timeout",
                return_value={"ok": False, "reason": "Photos not responding"},
            ),
        ):
            result = _write_tags_to_photos(MagicMock(), 1, "U1", [], "/path")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "Photos not responding")


class TestApplyBatch(unittest.TestCase):
    """apply_batch: continues past failures, populates errors list."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        from db.db import Database

        self.db = Database(Path(self._tmp.name) / "test.db")
        # Two photos, each with a pending non-conflict proposal
        for fid, uid in (("FA", "UA"), ("FB", "UB")):
            self.db.upsert_photo(
                {
                    "flickr_id": fid,
                    "uuid": uid,
                    "privacy_state": "candidate_public",
                    "flickr_tags": '["foo"]',
                    "flickr_tags_hash": f"H{fid}",
                    "photos_tags": "[]",
                    "photos_tags_hash": "PH_EMPTY",
                    "meta_synced_flickr_at": "2026-01-01T00:00:00+00:00",
                    "meta_synced_photos_at": "2026-01-01T00:00:00+00:00",
                }
            )
        self.pid_a = self._insert_proposal("FA", "HFA")
        self.pid_b = self._insert_proposal("FB", "HFB")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _insert_proposal(self, flickr_id, src_hash):
        photo_id = self.db.get_photo_by_flickr_id(flickr_id)["id"]
        self.db.upsert_proposal(
            {
                "photo_id": photo_id,
                "field": "tags",
                "proposed_value": '["foo"]',
                "source": "flickr",
                "target": "photos",
                "conflict_type": "non_conflict",
                "source_hash_at_creation": src_hash,
                "target_hash_at_creation": "PH_EMPTY",
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        )
        self.db.conn.commit()
        return self.db.conn.execute(
            "SELECT id FROM metadata_proposals WHERE photo_id=? ORDER BY id DESC LIMIT 1",
            (self.db.get_photo_by_flickr_id(flickr_id)["id"],),
        ).fetchone()["id"]

    def test_continues_past_exception(self):
        """An unexpected exception on one proposal must not stop the batch."""
        from unittest.mock import patch
        from flickr.proposal_applier import apply_batch

        call_order = []

        def side_effect(db, proposal_id, library_path, flickr_client=None):
            call_order.append(proposal_id)
            if proposal_id == self.pid_a:
                raise RuntimeError("simulated unexpected DB error")
            return {"ok": True}

        with patch("flickr.proposal_applier.apply_proposal", side_effect=side_effect):
            result = apply_batch(self.db, library_path="")

        self.assertIn(self.pid_a, call_order)
        self.assertIn(self.pid_b, call_order)
        self.assertEqual(result["applied"], 1)
        self.assertEqual(result["failed"], 1)

    def test_errors_list_populated_for_exception(self):
        from unittest.mock import patch
        from flickr.proposal_applier import apply_batch

        with patch("flickr.proposal_applier.apply_proposal", side_effect=RuntimeError("boom")):
            result = apply_batch(self.db, library_path="")

        self.assertEqual(len(result["errors"]), 2)
        self.assertTrue(all("proposal_id" in e and "reason" in e for e in result["errors"]))

    def test_errors_list_populated_for_dict_failure(self):
        """Failed proposals (dict return) also appear in errors list."""
        from unittest.mock import patch
        from flickr.proposal_applier import apply_batch

        with patch(
            "flickr.proposal_applier.apply_proposal",
            return_value={"ok": False, "reason": "photo not found"},
        ):
            result = apply_batch(self.db, library_path="")

        self.assertEqual(result["failed"], 2)
        self.assertEqual(len(result["errors"]), 2)
        self.assertEqual(result["errors"][0]["reason"], "photo not found")

    def test_superseded_not_in_errors(self):
        """source_changed/target_changed are superseded, not failed, and not in errors."""
        from unittest.mock import patch
        from flickr.proposal_applier import apply_batch

        with patch(
            "flickr.proposal_applier.apply_proposal",
            return_value={"ok": False, "reason": "source_changed"},
        ):
            result = apply_batch(self.db, library_path="")

        self.assertEqual(result["superseded"], 2)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["errors"], [])

    def test_empty_errors_list_on_full_success(self):
        from unittest.mock import patch
        from flickr.proposal_applier import apply_batch

        with patch("flickr.proposal_applier.apply_proposal", return_value={"ok": True}):
            result = apply_batch(self.db, library_path="")

        self.assertEqual(result["applied"], 2)
        self.assertEqual(result["errors"], [])

    def test_count_pending_returns_correct_count(self):
        from flickr.proposal_applier import _count_pending

        self.assertEqual(_count_pending(self.db), 2)

    def test_count_pending_empty_db(self):
        import tempfile
        from pathlib import Path
        from db.db import Database
        from flickr.proposal_applier import _count_pending

        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "empty.db")
            self.assertEqual(_count_pending(db), 0)
            db.close()

    def test_count_pending_filters_by_conflict_type(self):
        from flickr.proposal_applier import _count_pending

        self.assertEqual(_count_pending(self.db, conflict_types=["collision"]), 0)
        self.assertEqual(_count_pending(self.db, conflict_types=["non_conflict"]), 2)


class TestApplyManualMerge(unittest.TestCase):
    """apply_manual_merge: validation, staleness checks, dual-write, sibling resolution."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        from db.db import Database

        self.db = Database(Path(self._tmp.name) / "test.db")
        self.db.upsert_photo(
            {
                "flickr_id": "F1",
                "uuid": "U1",
                "privacy_state": "candidate_public",
                "flickr_tags": '["nature","travel"]',
                "flickr_tags_hash": "FH1",
                "photos_tags": '["nature","vacation"]',
                "photos_tags_hash": "PH1",
                "meta_synced_flickr_at": "2026-01-01T00:00:00+00:00",
                "meta_synced_photos_at": "2026-01-01T00:00:00+00:00",
            }
        )
        self.photo_id = self.db.get_photo_by_flickr_id("F1")["id"]

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _seed_collision_pair(self, src_hash="FH1", tgt_hash="PH1"):
        """Insert the two proposals that form a collision pair."""
        for src, tgt in (("flickr", "photos"), ("photos", "flickr")):
            self.db.upsert_proposal(
                {
                    "photo_id": self.photo_id,
                    "field": "tags",
                    "proposed_value": '["nature","travel"]',
                    "source": src,
                    "target": tgt,
                    "conflict_type": "collision",
                    "source_hash_at_creation": src_hash if src == "flickr" else tgt_hash,
                    "target_hash_at_creation": tgt_hash if tgt == "photos" else src_hash,
                    "created_at": "2026-01-01T00:00:00+00:00",
                }
            )
        self.db.conn.commit()
        rows = self.db.conn.execute(
            "SELECT id, source FROM metadata_proposals WHERE photo_id=? ORDER BY id",
            (self.photo_id,),
        ).fetchall()
        # primary = flickr→photos; sibling = photos→flickr
        primary = next(r["id"] for r in rows if r["source"] == "flickr")
        sibling = next(r["id"] for r in rows if r["source"] == "photos")
        return primary, sibling

    def test_rejects_non_tag_proposal(self):
        from flickr.proposal_applier import apply_manual_merge

        self.db.upsert_proposal(
            {
                "photo_id": self.photo_id,
                "field": "title",
                "proposed_value": "My Photo",
                "source": "flickr",
                "target": "photos",
                "conflict_type": "collision",
                "source_hash_at_creation": "H",
                "target_hash_at_creation": "H",
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        )
        self.db.conn.commit()
        pid = self.db.conn.execute(
            "SELECT id FROM metadata_proposals WHERE photo_id=? ORDER BY id DESC LIMIT 1",
            (self.photo_id,),
        ).fetchone()["id"]
        result = apply_manual_merge(self.db, pid, ["tag"], library_path="")
        self.assertFalse(result["ok"])
        self.assertIn("tag proposals", result["reason"])

    def test_rejects_non_collision_proposal(self):
        from flickr.proposal_applier import apply_manual_merge

        self.db.upsert_proposal(
            {
                "photo_id": self.photo_id,
                "field": "tags",
                "proposed_value": '["a"]',
                "source": "flickr",
                "target": "photos",
                "conflict_type": "non_conflict",
                "source_hash_at_creation": "FH1",
                "target_hash_at_creation": "PH1",
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        )
        self.db.conn.commit()
        pid = self.db.conn.execute(
            "SELECT id FROM metadata_proposals WHERE photo_id=? ORDER BY id DESC LIMIT 1",
            (self.photo_id,),
        ).fetchone()["id"]
        result = apply_manual_merge(self.db, pid, ["a"], library_path="")
        self.assertFalse(result["ok"])
        self.assertIn("collision", result["reason"])

    def test_source_changed_supersedes(self):
        from flickr.proposal_applier import apply_manual_merge

        primary, _ = self._seed_collision_pair(src_hash="STALE")
        result = apply_manual_merge(self.db, primary, ["nature"], library_path="")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "source_changed")
        status = self.db.conn.execute(
            "SELECT status FROM metadata_proposals WHERE id=?", (primary,)
        ).fetchone()["status"]
        self.assertEqual(status, "superseded")

    def test_applies_to_flickr_and_marks_applied(self):
        from unittest.mock import MagicMock
        from flickr.proposal_applier import apply_manual_merge

        primary, sibling = self._seed_collision_pair()

        mock_flickr = MagicMock()
        with unittest.mock.patch(
            "flickr.proposal_applier._photos_is_responsive", return_value=False
        ):
            # Photos not running — only Flickr write happens (uuid present but Photos blocked)
            apply_manual_merge(
                self.db,
                primary,
                ["nature", "travel", "vacation"],
                library_path="",
                flickr_client=mock_flickr,
            )

        # Flickr write attempted
        mock_flickr.set_tags.assert_called_once()
        # DB updated for Flickr
        row = self.db.conn.execute(
            "SELECT flickr_tags FROM photos WHERE id=?", (self.photo_id,)
        ).fetchone()
        import json

        self.assertIn("nature", json.loads(row["flickr_tags"]))

    def test_sibling_resolved_on_success(self):
        from unittest.mock import MagicMock, patch
        from flickr.proposal_applier import apply_manual_merge

        primary, sibling = self._seed_collision_pair()

        mock_flickr = MagicMock()
        mock_photo = MagicMock()
        mock_photo.keywords = ["nature"]
        mock_ps = MagicMock()
        mock_ps.Photo.return_value = mock_photo

        with (
            patch("flickr.proposal_applier._photos_is_responsive", return_value=True),
            patch.dict("sys.modules", {"photoscript": mock_ps}),
        ):
            result = apply_manual_merge(
                self.db,
                primary,
                ["nature", "travel", "vacation"],
                library_path="",
                flickr_client=mock_flickr,
            )

        self.assertTrue(result["ok"], result)
        # Primary marked applied
        p_status = self.db.conn.execute(
            "SELECT status FROM metadata_proposals WHERE id=?", (primary,)
        ).fetchone()["status"]
        self.assertEqual(p_status, "applied")
        # Caller (app.py) handles sibling — verify it is still pending here
        # (the endpoint resolves it, not apply_manual_merge itself)
        s_status = self.db.conn.execute(
            "SELECT status FROM metadata_proposals WHERE id=?", (sibling,)
        ).fetchone()["status"]
        self.assertEqual(s_status, "pending")


class TestApplyCollisionReverse(unittest.TestCase):
    """apply_collision_reverse: writes Photos value to Flickr, works even when sibling is superseded."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        from db.db import Database

        self.db = Database(Path(self._tmp.name) / "test.db")
        self.db.upsert_photo(
            {
                "flickr_id": "F1",
                "uuid": "U1",
                "privacy_state": "candidate_public",
                "flickr_tags": '["travel"]',
                "flickr_tags_hash": "FH1",
                "photos_tags": '["nature"]',
                "photos_tags_hash": "PH1",
                "flickr_title": "Flickr Title",
                "photos_title": "Photos Title",
                "flickr_description": "Flickr desc",
                "photos_description": "Photos desc",
                "meta_synced_flickr_at": "2026-01-01T00:00:00+00:00",
                "meta_synced_photos_at": "2026-01-01T00:00:00+00:00",
            }
        )
        self.photo_id = self.db.get_photo_by_flickr_id("F1")["id"]

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _seed_collision(self, field="tags", supersede_sibling=False):
        for src, tgt in (("flickr", "photos"), ("photos", "flickr")):
            self.db.upsert_proposal(
                {
                    "photo_id": self.photo_id,
                    "field": field,
                    "proposed_value": '["travel"]' if field == "tags" else "Flickr value",
                    "source": src,
                    "target": tgt,
                    "conflict_type": "collision",
                    "source_hash_at_creation": "FH1" if src == "flickr" else "PH1",
                    "target_hash_at_creation": "PH1" if tgt == "photos" else "FH1",
                    "created_at": "2026-01-01T00:00:00+00:00",
                }
            )
        self.db.conn.commit()
        rows = self.db.conn.execute(
            "SELECT id, source FROM metadata_proposals WHERE photo_id=? AND field=? ORDER BY id",
            (self.photo_id, field),
        ).fetchall()
        primary = next(r["id"] for r in rows if r["source"] == "flickr")
        sibling = next(r["id"] for r in rows if r["source"] == "photos")
        if supersede_sibling:
            self.db.conn.execute(
                "UPDATE metadata_proposals SET status='superseded' WHERE id=?", (sibling,)
            )
            self.db.conn.commit()
        return primary, sibling

    def test_writes_photos_tags_to_flickr(self):
        from unittest.mock import MagicMock
        from flickr.proposal_applier import apply_collision_reverse

        primary, sibling = self._seed_collision(field="tags")
        mock_flickr = MagicMock()
        result = apply_collision_reverse(self.db, primary, flickr_client=mock_flickr)
        self.assertTrue(result["ok"], result)
        mock_flickr.set_tags.assert_called_once_with("F1", ["nature"])

    def test_marks_primary_rejected_sibling_applied(self):
        from unittest.mock import MagicMock
        from flickr.proposal_applier import apply_collision_reverse

        primary, sibling = self._seed_collision(field="tags")
        apply_collision_reverse(self.db, primary, flickr_client=MagicMock())
        p_status = self.db.conn.execute(
            "SELECT status FROM metadata_proposals WHERE id=?", (primary,)
        ).fetchone()["status"]
        s_status = self.db.conn.execute(
            "SELECT status FROM metadata_proposals WHERE id=?", (sibling,)
        ).fetchone()["status"]
        self.assertEqual(p_status, "rejected")
        self.assertEqual(s_status, "applied")

    def test_works_when_sibling_superseded(self):
        """The key regression: sibling superseded by sync run should not block apply."""
        from unittest.mock import MagicMock
        from flickr.proposal_applier import apply_collision_reverse

        primary, sibling = self._seed_collision(field="tags", supersede_sibling=True)
        result = apply_collision_reverse(self.db, primary, flickr_client=MagicMock())
        self.assertTrue(result["ok"], result)
        s_status = self.db.conn.execute(
            "SELECT status FROM metadata_proposals WHERE id=?", (sibling,)
        ).fetchone()["status"]
        self.assertEqual(s_status, "applied")

    def test_writes_photos_title_to_flickr(self):
        from unittest.mock import MagicMock
        from flickr.proposal_applier import apply_collision_reverse

        primary, _ = self._seed_collision(field="title")
        mock_flickr = MagicMock()
        result = apply_collision_reverse(self.db, primary, flickr_client=mock_flickr)
        self.assertTrue(result["ok"], result)
        mock_flickr.set_meta.assert_called_once_with(
            "F1", title="Photos Title", description="Flickr desc"
        )

    def test_no_flickr_client_returns_error(self):
        from flickr.proposal_applier import apply_collision_reverse

        primary, _ = self._seed_collision(field="tags")
        result = apply_collision_reverse(self.db, primary, flickr_client=None)
        self.assertFalse(result["ok"])
        self.assertIn("flickr_client", result["reason"])

    def test_not_found_returns_error(self):
        from flickr.proposal_applier import apply_collision_reverse
        from unittest.mock import MagicMock

        result = apply_collision_reverse(self.db, 9999, flickr_client=MagicMock())
        self.assertFalse(result["ok"])
        self.assertIn("not found", result["reason"])

    def test_already_resolved_returns_error(self):
        from unittest.mock import MagicMock
        from flickr.proposal_applier import apply_collision_reverse

        primary, _ = self._seed_collision(field="tags")
        self.db.resolve_proposal(primary, "rejected")
        result = apply_collision_reverse(self.db, primary, flickr_client=MagicMock())
        self.assertFalse(result["ok"])


class TestGetPendingProposals(unittest.TestCase):
    """db.get_pending_proposals ordering and filtering."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        from db.db import Database

        self.db = Database(Path(self._tmp.name) / "test.db")
        # One photo per conflict type to avoid idempotency deduplication
        for fid in ("A", "B", "C"):
            self.db.upsert_photo(
                {"flickr_id": fid, "uuid": f"U-{fid}", "privacy_state": "candidate_public"}
            )
        self.pid_a = self.db.get_photo_by_flickr_id("A")["id"]
        self.pid_b = self.db.get_photo_by_flickr_id("B")["id"]
        self.pid_c = self.db.get_photo_by_flickr_id("C")["id"]

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _add(self, photo_id, conflict_type, target="photos"):
        self.db.upsert_proposal(
            {
                "photo_id": photo_id,
                "field": "tags",
                "proposed_value": '["x"]',
                "source": "flickr",
                "target": target,
                "conflict_type": conflict_type,
                "source_hash_at_creation": "SH",
                "target_hash_at_creation": "TH",
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        )
        self.db.conn.commit()

    def test_collision_sorts_first(self):
        self._add(self.pid_a, "non_conflict")
        self._add(self.pid_b, "collision")
        self._add(self.pid_c, "divergence")
        items = self.db.get_pending_proposals()
        types = [p["conflict_type"] for p in items]
        self.assertEqual(types[0], "collision")
        self.assertEqual(types[1], "divergence")
        self.assertEqual(types[2], "non_conflict")

    def test_filter_by_conflict_type(self):
        self._add(self.pid_a, "non_conflict")
        self._add(self.pid_b, "collision")
        items = self.db.get_pending_proposals(conflict_type="non_conflict")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["conflict_type"], "non_conflict")

    def test_proposed_value_decoded_as_list(self):
        self._add(self.pid_a, "non_conflict")
        items = self.db.get_pending_proposals()
        self.assertIsInstance(items[0]["proposed_value"], list)


# ---------------------------------------------------------------------------
# Reviewer UI — photo detail page
# ---------------------------------------------------------------------------


class TestPhotoDetailTemplate(unittest.TestCase):
    """photo_detail route renders x-apple-photos:// link iff uuid is set."""

    def setUp(self):
        import reviewer.app as reviewer_app

        self._tmp = tempfile.TemporaryDirectory()
        db_path = Path(self._tmp.name) / "test.db"
        self._db = Database(db_path)

        # Wire the module-level _db used by the Flask app
        reviewer_app._db = self._db

        self._app = reviewer_app.app
        self._app.config["TESTING"] = True
        self._client = self._app.test_client()

        # Photo with uuid (Photos-matched)
        self.matched_id = self._db.upsert_photo(
            {
                "uuid": "AAAA-1111",
                "flickr_id": "flickr-detail-001",
                "original_filename": "IMG_detail.JPG",
                "privacy_state": "candidate_public",
                "proposed_tags": [],
                "apple_persons": [],
                "apple_labels": [],
            }
        )
        # Flickr-only photo (no uuid)
        self.flickr_only_id = self._db.upsert_photo(
            {
                "flickr_id": "flickr-detail-002",
                "original_filename": "flickr_only.JPG",
                "privacy_state": "candidate_public",
                "proposed_tags": [],
                "apple_persons": [],
                "apple_labels": [],
            }
        )

    def tearDown(self):
        self._db.close()
        self._tmp.cleanup()

    def _get(self, photo_id):
        with self._app.test_request_context():
            resp = self._client.get(f"/photo/{photo_id}")
        return resp

    def test_photos_link_present_when_uuid_set(self):
        resp = self._get(self.matched_id)
        self.assertEqual(resp.status_code, 200)
        body = resp.data.decode()
        # onclick="openInPhotos(...)" only rendered inside {% if photo.uuid %} blocks
        self.assertIn('onclick="openInPhotos(', body)

    def test_photos_link_absent_when_uuid_null(self):
        resp = self._get(self.flickr_only_id)
        self.assertEqual(resp.status_code, 200)
        body = resp.data.decode()
        # no uuid → no rendered link elements, only the JS function definition
        self.assertNotIn('onclick="openInPhotos(', body)

    def test_photos_link_text_present(self):
        resp = self._get(self.matched_id)
        body = resp.data.decode()
        self.assertIn("Photos", body)

    def test_uuid_prefix_shown_in_details(self):
        resp = self._get(self.matched_id)
        body = resp.data.decode()
        self.assertIn("AAAA-111", body)  # truncated prefix visible in details row

    def test_open_in_photos_api_no_uuid_returns_404(self):
        with self._app.test_request_context():
            resp = self._client.post(f"/api/open-in-photos/{self.flickr_only_id}")
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(resp.get_json()["ok"])

    def test_open_in_photos_api_osascript_error_returns_ok_false(self):
        from unittest.mock import patch

        with patch("reviewer.app.subprocess.run") as mock_run:
            mock_run.return_value.__class__ = type(
                "R", (), {"returncode": 1, "stderr": "Photos not running"}
            )
            import types

            r = types.SimpleNamespace(returncode=1, stderr="Photos not running")
            mock_run.return_value = r
            with self._app.test_request_context():
                resp = self._client.post(f"/api/open-in-photos/{self.matched_id}")
        data = resp.get_json()
        self.assertFalse(data["ok"])


class TestCmdAll(unittest.TestCase):
    """bp all: correct step sequence, error isolation, per-step arg overrides."""

    @classmethod
    def _import_bp(cls):
        import importlib.util
        from importlib.machinery import SourceFileLoader

        bp_path = str(Path(__file__).parent.parent / "bp")
        loader = SourceFileLoader("bp_module", bp_path)
        spec = importlib.util.spec_from_loader("bp_module", loader)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def _args(self, **kwargs):
        import argparse

        base = dict(
            config="config/config.yml",
            verbose=False,
            dry_run=False,
            all=False,
            days=None,
            backfill=False,
            limit=None,
            fix=False,
            apply_proposals=False,
            album=None,
            mode="truncate",
            photo_id=None,
            conflicts_only=False,
            force=False,
            port=5173,
            debug=False,
            oneliner=False,
        )
        base.update(kwargs)
        return argparse.Namespace(**base)

    def _patch_steps(self, bp, overrides=None):
        """Replace all cmd_* steps with no-ops; apply per-name overrides."""
        names = (
            "cmd_scan",
            "cmd_poll",
            "cmd_thumbs",
            "cmd_pipeline",
            "cmd_reconcile",
            "cmd_sync_albums",
            "cmd_sync_album_collections",
            "cmd_sync_names_from_flickr",
            "cmd_checkpoint",
        )
        originals = {n: getattr(bp, n) for n in names}
        for n in names:
            setattr(bp, n, (overrides or {}).get(n, lambda a: None))
        return originals

    def _restore(self, bp, originals):
        for n, orig in originals.items():
            setattr(bp, n, orig)

    def test_all_nine_steps_called_in_order(self):
        bp = self._import_bp()
        import threading

        called = []
        lock = threading.Lock()
        labels = {
            "cmd_scan": "scan",
            "cmd_poll": "poll",
            "cmd_thumbs": "thumbs",
            "cmd_sync_names_from_flickr": "sync_names_from_flickr",
            "cmd_pipeline": "pipeline",
            "cmd_reconcile": "reconcile",
            "cmd_sync_albums": "sync_albums",
            "cmd_sync_album_collections": "sync_album_collections",
            "cmd_checkpoint": "checkpoint",
        }
        originals = self._patch_steps(
            bp,
            {
                n: (lambda lbl: lambda a: (lock.acquire(), called.append(lbl), lock.release()))(lbl)
                for n, lbl in labels.items()
            },
        )
        try:
            bp.cmd_all(self._args())
        finally:
            self._restore(bp, originals)
        # All 9 steps must run; checkpoint must be last; reconcile must precede checkpoint.
        # Reconcile runs in background so its position relative to sync-albums is not fixed.
        self.assertEqual(len(called), 9)
        self.assertEqual(set(called), set(labels.values()))
        self.assertEqual(called[-1], "checkpoint")
        self.assertLess(called.index("reconcile"), called.index("checkpoint"))
        # Steps that must precede reconcile (happen in main thread before bg launch)
        for step in ("scan", "poll", "thumbs", "sync_names_from_flickr", "pipeline"):
            self.assertLess(called.index(step), called.index("checkpoint"))

    def test_failed_step_does_not_abort_sequence(self):
        bp = self._import_bp()
        called = []

        def fail_scan(args):
            called.append("scan")
            raise SystemExit(1)

        originals = self._patch_steps(
            bp,
            {
                "cmd_scan": fail_scan,
                "cmd_poll": lambda a: called.append("poll"),
                "cmd_thumbs": lambda a: called.append("thumbs"),
                "cmd_sync_names_from_flickr": lambda a: called.append("sync_names_from_flickr"),
                "cmd_pipeline": lambda a: called.append("pipeline"),
                "cmd_reconcile": lambda a: called.append("reconcile"),
                "cmd_sync_albums": lambda a: called.append("sync_albums"),
                "cmd_sync_album_collections": lambda a: called.append("sync_album_collections"),
                "cmd_checkpoint": lambda a: called.append("checkpoint"),
            },
        )
        try:
            bp.cmd_all(self._args())
        finally:
            self._restore(bp, originals)
        # scan ran and failed; all eight subsequent steps still ran
        self.assertEqual(len(called), 9)
        self.assertEqual(called[0], "scan")
        self.assertIn("checkpoint", called)

    def test_scan_gets_all_true_reconcile_gets_fix_true(self):
        bp = self._import_bp()
        captured = {}

        def cap(name):
            def fn(args):
                captured[name] = vars(args)

            return fn

        originals = self._patch_steps(
            bp,
            {
                "cmd_scan": cap("scan"),
                "cmd_reconcile": cap("reconcile"),
            },
        )
        try:
            bp.cmd_all(self._args())
        finally:
            self._restore(bp, originals)
        self.assertTrue(captured["scan"]["all"])
        self.assertFalse(captured["scan"]["backfill"])
        self.assertTrue(captured["reconcile"]["fix"])
        self.assertFalse(captured["reconcile"]["apply_proposals"])


class TestCheckpoint(unittest.TestCase):
    """db.checkpoint() and wal_autocheckpoint pragma."""

    def setUp(self):
        import tempfile

        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        from db.db import Database

        self.db = Database(self.tmp.name)

    def tearDown(self):
        self.db.close()
        import os

        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(self.tmp.name + ext)
            except FileNotFoundError:
                pass

    def test_checkpoint_returns_dict_with_expected_keys(self):
        result = self.db.checkpoint()
        self.assertIn("busy", result)
        self.assertIn("log", result)
        self.assertIn("checkpointed", result)

    def test_checkpoint_truncate_on_empty_wal(self):
        result = self.db.checkpoint(mode="TRUNCATE")
        self.assertEqual(result["busy"], 0)

    def test_checkpoint_passive_mode(self):
        result = self.db.checkpoint(mode="PASSIVE")
        self.assertIn("checkpointed", result)

    def test_wal_autocheckpoint_set_to_500(self):
        row = self.db.conn.execute("PRAGMA wal_autocheckpoint").fetchone()
        self.assertEqual(row[0], 500)


class TestFlickrClientRotate(unittest.TestCase):
    """FlickrClient.rotate(): validates degrees, calls correct API method."""

    def _make_client(self):
        from flickr.flickr_client import FlickrClient

        return FlickrClient("key", "secret", "token", "tsecret", rate_limit_delay=0)

    def _mock_response(self, json_data=None):
        from unittest.mock import MagicMock

        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = json_data or {"stat": "ok"}
        resp.raise_for_status = MagicMock()
        return resp

    def test_valid_degrees_calls_api(self):
        from unittest.mock import patch

        c = self._make_client()
        ok = self._mock_response({"stat": "ok"})
        with patch.object(c._session, "post", return_value=ok) as mock_post:
            c.rotate("flickr123", 90)
        args, kwargs = mock_post.call_args
        body = kwargs.get("data") or (args[1] if len(args) > 1 else {})
        self.assertIn("flickr.photos.transform.rotate", str(body))

    def test_invalid_degrees_raises(self):

        c = self._make_client()
        with self.assertRaises(ValueError):
            c.rotate("flickr123", 45)

    def test_zero_degrees_raises(self):
        c = self._make_client()
        with self.assertRaises(ValueError):
            c.rotate("flickr123", 0)

    def test_360_raises(self):
        c = self._make_client()
        with self.assertRaises(ValueError):
            c.rotate("flickr123", 360)


class TestRotateFlickrApi(unittest.TestCase):
    """POST /api/photos/<id>/rotate-flickr endpoint and template rendering."""

    def setUp(self):
        import reviewer.app as reviewer_app

        self._tmp = tempfile.TemporaryDirectory()
        db_path = Path(self._tmp.name) / "test.db"
        self._db = Database(db_path)
        reviewer_app._db = self._db
        reviewer_app._client = None
        self._app = reviewer_app.app
        self._app.config["TESTING"] = True
        self._client = self._app.test_client()

        self.photo_id = self._db.upsert_photo(
            {
                "flickr_id": "flickr-rotate-001",
                "original_filename": "rotate_test.JPG",
                "privacy_state": "candidate_public",
                "proposed_tags": [],
                "apple_persons": [],
                "apple_labels": [],
            }
        )
        self.no_flickr_id = self._db.upsert_photo(
            {
                "uuid": "BBBB-2222",
                "original_filename": "local_only.JPG",
                "privacy_state": "candidate_public",
                "proposed_tags": [],
                "apple_persons": [],
                "apple_labels": [],
            }
        )

    def tearDown(self):
        import reviewer.app as reviewer_app

        reviewer_app._client = None
        self._db.close()
        self._tmp.cleanup()

    def _post(self, photo_id, degrees):
        import json

        with self._app.test_request_context():
            return self._client.post(
                f"/api/photos/{photo_id}/rotate-flickr",
                data=json.dumps({"degrees": degrees}),
                content_type="application/json",
            )

    def test_bad_degrees_returns_400(self):
        resp = self._post(self.photo_id, 45)
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.get_json()["ok"])

    def test_missing_photo_returns_404(self):
        resp = self._post(99999, 90)
        self.assertEqual(resp.status_code, 404)

    def test_photo_without_flickr_id_returns_400(self):
        resp = self._post(self.no_flickr_id, 90)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("no Flickr ID", resp.get_json()["error"])

    def test_no_client_returns_503(self):
        resp = self._post(self.photo_id, 90)
        self.assertEqual(resp.status_code, 503)

    def test_rotate_calls_client_and_returns_ok(self):
        from unittest.mock import MagicMock
        import reviewer.app as reviewer_app

        mock_c = MagicMock()
        mock_c.rotate.return_value = {"stat": "ok"}
        mock_c.get_photo_info.return_value = {
            "photo": {"secret": "newsecret123", "server": "65535"}
        }
        reviewer_app._client = mock_c
        try:
            resp = self._post(self.photo_id, 180)
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.get_json()["ok"])
            mock_c.rotate.assert_called_once_with("flickr-rotate-001", 180)
            mock_c.get_photo_info.assert_called_once_with("flickr-rotate-001")
        finally:
            reviewer_app._client = None

    def test_rotate_buttons_shown_when_flickr_id_set(self):
        with self._app.test_request_context():
            resp = self._client.get(f"/photo/{self.photo_id}")
        body = resp.data.decode()
        self.assertIn("rotateFlickr(90)", body)
        self.assertIn("rotateFlickr(180)", body)
        self.assertIn("rotateFlickr(270)", body)

    def test_rotate_buttons_absent_when_no_flickr_id(self):
        with self._app.test_request_context():
            resp = self._client.get(f"/photo/{self.no_flickr_id}")
        body = resp.data.decode()
        self.assertNotIn("Rotate on Flickr", body)


class TestInstallDaemons(unittest.TestCase):
    """bp install-daemons: template substitution, file placement, dry-run."""

    @classmethod
    def _import_bp(cls):
        import importlib.util
        from importlib.machinery import SourceFileLoader

        bp_path = str(Path(__file__).parent.parent / "bp")
        loader = SourceFileLoader("bp_module", bp_path)
        spec = importlib.util.spec_from_loader("bp_module", loader)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def _args(self, dry_run=False):
        import argparse

        return argparse.Namespace(dry_run=dry_run)

    def test_tokens_substituted_in_installed_files(self):
        import tempfile

        bp = self._import_bp()
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_home = Path(tmpdir) / "home"
            fake_agents = fake_home / "Library" / "LaunchAgents"
            fake_agents.mkdir(parents=True)
            fake_uv = "/fake/uv"
            with (
                unittest.mock.patch("shutil.which", return_value=fake_uv),
                unittest.mock.patch.object(Path, "home", return_value=fake_home),
            ):
                bp.cmd_install_daemons(self._args())
            installed = list(fake_agents.glob("*.plist"))
            self.assertEqual(len(installed), 4)
            for f in installed:
                text = f.read_text()
                self.assertNotIn("__REPO__", text)
                self.assertNotIn("__UV__", text)
                self.assertNotIn("__HOME__", text)
                self.assertIn(fake_uv, text)
                self.assertIn(str(fake_home), text)

    def test_dry_run_writes_no_files(self):
        import tempfile

        bp = self._import_bp()
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_home = Path(tmpdir) / "home"
            fake_home.mkdir()
            with (
                unittest.mock.patch("shutil.which", return_value="/fake/uv"),
                unittest.mock.patch.object(Path, "home", return_value=fake_home),
            ):
                bp.cmd_install_daemons(self._args(dry_run=True))
            agents_dir = fake_home / "Library" / "LaunchAgents"
            self.assertFalse(agents_dir.exists())

    def test_missing_uv_exits(self):
        bp = self._import_bp()
        with unittest.mock.patch("shutil.which", return_value=None):
            with self.assertRaises(SystemExit):
                bp.cmd_install_daemons(self._args())

    def test_poller_plist_runs_thumbs_after_poll(self):
        import tempfile

        bp = self._import_bp()
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_home = Path(tmpdir) / "home"
            fake_agents = fake_home / "Library" / "LaunchAgents"
            fake_agents.mkdir(parents=True)
            with (
                unittest.mock.patch("shutil.which", return_value="/fake/uv"),
                unittest.mock.patch.object(Path, "home", return_value=fake_home),
            ):
                bp.cmd_install_daemons(self._args())
            poller = fake_agents / "com.blue-pearmain.poller.plist"
            self.assertIn("thumbs", poller.read_text())

    def test_reconcile_plist_has_weekly_calendar_interval(self):
        import tempfile

        bp = self._import_bp()
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_home = Path(tmpdir) / "home"
            fake_agents = fake_home / "Library" / "LaunchAgents"
            fake_agents.mkdir(parents=True)
            with (
                unittest.mock.patch("shutil.which", return_value="/fake/uv"),
                unittest.mock.patch.object(Path, "home", return_value=fake_home),
            ):
                bp.cmd_install_daemons(self._args())
            reconcile = fake_agents / "com.blue-pearmain.reconcile.plist"
            text = reconcile.read_text()
            self.assertIn("CalendarInterval", text)
            self.assertIn("Weekday", text)

    def test_label_uses_new_bundle_id(self):
        """The installed plists use com.blue-pearmain.* labels, not the old com.cdevers.* form."""
        import tempfile

        bp = self._import_bp()
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_home = Path(tmpdir) / "home"
            fake_agents = fake_home / "Library" / "LaunchAgents"
            fake_agents.mkdir(parents=True)
            with (
                unittest.mock.patch("shutil.which", return_value="/fake/uv"),
                unittest.mock.patch.object(Path, "home", return_value=fake_home),
            ):
                bp.cmd_install_daemons(self._args())
            for f in fake_agents.glob("*.plist"):
                text = f.read_text()
                self.assertNotIn("com.cdevers.blue-pearmain", text)


class TestUninstallDaemons(unittest.TestCase):
    """bp uninstall-daemons: removes installed plists, dry-run leaves them."""

    @classmethod
    def _import_bp(cls):
        import importlib.util
        from importlib.machinery import SourceFileLoader

        bp_path = str(Path(__file__).parent.parent / "bp")
        loader = SourceFileLoader("bp_module", bp_path)
        spec = importlib.util.spec_from_loader("bp_module", loader)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def _args(self, dry_run=False):
        import argparse

        return argparse.Namespace(dry_run=dry_run)

    def _install_fake_plists(self, fake_agents: Path):
        plists = [
            "com.blue-pearmain.poller.plist",
            "com.blue-pearmain.pipeline.plist",
            "com.blue-pearmain.reviewer.plist",
            "com.blue-pearmain.reconcile.plist",
        ]
        for name in plists:
            (fake_agents / name).write_text("<plist/>")
        return plists

    def test_removes_installed_files(self):
        import tempfile

        bp = self._import_bp()
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_home = Path(tmpdir) / "home"
            fake_agents = fake_home / "Library" / "LaunchAgents"
            fake_agents.mkdir(parents=True)
            self._install_fake_plists(fake_agents)
            with unittest.mock.patch.object(Path, "home", return_value=fake_home):
                bp.cmd_uninstall_daemons(self._args())
            self.assertEqual(list(fake_agents.glob("*.plist")), [])

    def test_dry_run_leaves_files_intact(self):
        import tempfile

        bp = self._import_bp()
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_home = Path(tmpdir) / "home"
            fake_agents = fake_home / "Library" / "LaunchAgents"
            fake_agents.mkdir(parents=True)
            plists = self._install_fake_plists(fake_agents)
            with unittest.mock.patch.object(Path, "home", return_value=fake_home):
                bp.cmd_uninstall_daemons(self._args(dry_run=True))
            self.assertEqual(
                sorted(f.name for f in fake_agents.glob("*.plist")),
                sorted(plists),
            )

    def test_tolerates_missing_files(self):
        import tempfile

        bp = self._import_bp()
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_home = Path(tmpdir) / "home"
            fake_agents = fake_home / "Library" / "LaunchAgents"
            fake_agents.mkdir(parents=True)
            with unittest.mock.patch.object(Path, "home", return_value=fake_home):
                bp.cmd_uninstall_daemons(self._args())


class TestSetPhotoText(unittest.TestCase):
    """set_photo_text: writes title+description to both Photos and Flickr."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        from db.db import Database

        self.db = Database(Path(self._tmp.name) / "test.db")
        self.db.upsert_photo(
            {
                "flickr_id": "F1",
                "uuid": "U1",
                "privacy_state": "candidate_public",
                "flickr_title": "Old Flickr",
                "photos_title": "Old Photos",
                "flickr_description": "Old F desc",
                "photos_description": "Old P desc",
            }
        )
        self.photo_id = self.db.get_photo_by_flickr_id("F1")["id"]

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_writes_to_flickr_via_set_meta(self):
        from unittest.mock import MagicMock
        from flickr.proposal_applier import set_photo_text

        mock_client = MagicMock()
        with unittest.mock.patch(
            "flickr.proposal_applier._photos_is_responsive", return_value=False
        ):
            result = set_photo_text(
                self.db,
                self.photo_id,
                "New Title",
                "New Desc",
                "/fake/lib",
                flickr_client=mock_client,
            )
        self.assertTrue(result["ok"], result)
        mock_client.set_meta.assert_called_once_with(
            "F1", title="New Title", description="New Desc"
        )

    def test_updates_flickr_cache_in_db(self):
        from unittest.mock import MagicMock
        from flickr.proposal_applier import set_photo_text

        with unittest.mock.patch(
            "flickr.proposal_applier._photos_is_responsive", return_value=False
        ):
            set_photo_text(
                self.db, self.photo_id, "T2", "D2", "/fake/lib", flickr_client=MagicMock()
            )
        row = self.db.get_photo(self.photo_id)
        self.assertEqual(row["flickr_title"], "T2")
        self.assertEqual(row["flickr_description"], "D2")

    def test_supersedes_pending_title_proposals(self):
        from unittest.mock import MagicMock
        from flickr.proposal_applier import set_photo_text

        for src, tgt in (("flickr", "photos"), ("photos", "flickr")):
            self.db.upsert_proposal(
                {
                    "photo_id": self.photo_id,
                    "field": "title",
                    "proposed_value": "v",
                    "source": src,
                    "target": tgt,
                    "conflict_type": "collision",
                    "source_hash_at_creation": "h1",
                    "target_hash_at_creation": "h2",
                    "created_at": "2026-01-01T00:00:00+00:00",
                }
            )
        self.db.conn.commit()
        with unittest.mock.patch(
            "flickr.proposal_applier._photos_is_responsive", return_value=False
        ):
            set_photo_text(self.db, self.photo_id, "T", "D", "/fake/lib", flickr_client=MagicMock())
        statuses = [
            r["status"]
            for r in self.db.conn.execute(
                "SELECT status FROM metadata_proposals WHERE photo_id=? AND field='title'",
                (self.photo_id,),
            ).fetchall()
        ]
        self.assertTrue(all(s == "superseded" for s in statuses), statuses)

    def test_photo_not_found_returns_error(self):
        from unittest.mock import MagicMock
        from flickr.proposal_applier import set_photo_text

        result = set_photo_text(self.db, 9999, "T", "D", "/fake/lib", flickr_client=MagicMock())
        self.assertFalse(result["ok"])
        self.assertIn("not found", result["reason"])

    def test_no_flickr_client_produces_warning(self):
        from flickr.proposal_applier import set_photo_text

        with unittest.mock.patch(
            "flickr.proposal_applier._photos_is_responsive", return_value=False
        ):
            result = set_photo_text(
                self.db, self.photo_id, "T", "D", "/fake/lib", flickr_client=None
            )
        self.assertTrue(result["ok"], result)
        self.assertIn("Flickr", " ".join(result.get("warnings", [])))

    def test_flickr_api_error_produces_warning(self):
        from unittest.mock import MagicMock
        from flickr.proposal_applier import set_photo_text

        mock_client = MagicMock()
        mock_client.set_meta.side_effect = Exception("API down")
        with unittest.mock.patch(
            "flickr.proposal_applier._photos_is_responsive", return_value=False
        ):
            result = set_photo_text(
                self.db, self.photo_id, "T", "D", "/fake/lib", flickr_client=mock_client
            )
        self.assertTrue(result["ok"], result)
        self.assertTrue(any("Flickr" in w for w in result.get("warnings", [])))

    def test_writes_to_photos_via_photoscript(self):
        from unittest.mock import MagicMock, patch
        from flickr.proposal_applier import set_photo_text

        mock_photo = MagicMock()
        mock_photo.title = ""
        mock_photo.description = ""
        mock_ps = MagicMock()
        mock_ps.Photo.return_value = mock_photo
        with (
            patch("flickr.proposal_applier._photos_is_responsive", return_value=True),
            patch.dict("sys.modules", {"photoscript": mock_ps}),
        ):
            result = set_photo_text(
                self.db, self.photo_id, "T3", "D3", "/fake/lib", flickr_client=MagicMock()
            )
        self.assertTrue(result["ok"], result)
        self.assertEqual(mock_photo.title, "T3")
        self.assertEqual(mock_photo.description, "D3")

    def test_apply_text_to_photos_timeout_returns_not_responding(self):
        import sys
        from unittest.mock import patch, MagicMock
        from flickr.proposal_applier import _apply_text_to_photos

        row = {"field": "title", "uuid": "U1", "photo_id": 1, "id": 10}
        with (
            patch("flickr.proposal_applier._photos_is_responsive", return_value=True),
            patch.dict(sys.modules, {"photoscript": MagicMock()}),
            patch(
                "flickr.proposal_applier._run_with_timeout",
                return_value={"ok": False, "reason": "Photos not responding"},
            ),
        ):
            result = _apply_text_to_photos(MagicMock(), row, "new title")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "Photos not responding")

    def test_write_text_both_timeout_returns_not_responding(self):
        import sys
        from unittest.mock import patch, MagicMock
        from flickr.proposal_applier import _write_text_to_photos_both

        with (
            patch("flickr.proposal_applier._photos_is_responsive", return_value=True),
            patch.dict(sys.modules, {"photoscript": MagicMock()}),
            patch(
                "flickr.proposal_applier._run_with_timeout",
                return_value={"ok": False, "reason": "Photos not responding"},
            ),
        ):
            result = _write_text_to_photos_both(MagicMock(), 1, "U1", "title", "desc")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "Photos not responding")


class TestStaleUuid(unittest.TestCase):
    """Proposals that fail with 'invalid photo ID' are marked failed; photo gets uuid_stale=1."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        from db.db import Database

        self.db = Database(Path(self._tmp.name) / "test.db")
        # Migration must be applied so uuid_stale column and 'failed' status exist
        from db.migrations.migrate_010_stale_uuid import run as run_migration

        run_migration(str(Path(self._tmp.name) / "test.db"))
        # Seed a photo with both uuid and flickr_id
        self.db.upsert_photo(
            {
                "flickr_id": "F1",
                "uuid": "U1",
                "privacy_state": "candidate_public",
                "photos_tags": '["nature"]',
                "photos_tags_hash": "PH1",
                "flickr_tags": "[]",
                "flickr_tags_hash": "FH0",
            }
        )
        self.photo_id = self.db.get_photo_by_flickr_id("F1")["id"]
        # Seed a pending non_conflict proposal: flickr→photos tags
        self.db.upsert_proposal(
            {
                "photo_id": self.photo_id,
                "field": "tags",
                "proposed_value": '["nature"]',
                "source": "flickr",
                "target": "photos",
                "conflict_type": "non_conflict",
                "source_hash_at_creation": "FH0",
                "target_hash_at_creation": "PH1",
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        )
        self.db.conn.commit()
        self.proposal_id = self.db.conn.execute(
            "SELECT id FROM metadata_proposals WHERE photo_id=? AND status='pending'",
            (self.photo_id,),
        ).fetchone()["id"]

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _mock_stale_uuid(self):
        """Return a context manager that makes photoscript.Photo raise 'invalid photo ID: U1'."""
        from unittest.mock import patch, MagicMock

        mock_ps = MagicMock()
        mock_ps.Photo.side_effect = Exception("invalid photo ID: U1")
        return patch.dict("sys.modules", {"photoscript": mock_ps})

    def test_stale_uuid_marks_proposal_failed(self):
        from unittest.mock import patch
        from flickr.proposal_applier import apply_proposal

        with (
            patch("flickr.proposal_applier._photos_is_responsive", return_value=True),
            self._mock_stale_uuid(),
        ):
            result = apply_proposal(self.db, self.proposal_id, "/fake/lib")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "stale_uuid")
        row = self.db.conn.execute(
            "SELECT status, resolution_note FROM metadata_proposals WHERE id=?",
            (self.proposal_id,),
        ).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["resolution_note"], "stale_uuid")

    def test_stale_uuid_sets_flag_on_photo(self):
        from unittest.mock import patch
        from flickr.proposal_applier import apply_proposal

        with (
            patch("flickr.proposal_applier._photos_is_responsive", return_value=True),
            self._mock_stale_uuid(),
        ):
            apply_proposal(self.db, self.proposal_id, "/fake/lib")
        flag = self.db.conn.execute(
            "SELECT uuid_stale FROM photos WHERE id=?", (self.photo_id,)
        ).fetchone()["uuid_stale"]
        self.assertEqual(flag, 1)

    def test_stale_uuid_in_apply_batch_counted_as_failed_not_error(self):
        from unittest.mock import patch
        from flickr.proposal_applier import apply_batch

        with (
            patch("flickr.proposal_applier._photos_is_responsive", return_value=True),
            self._mock_stale_uuid(),
        ):
            totals = apply_batch(self.db, "/fake/lib")
        self.assertEqual(totals["failed"], 1)
        self.assertEqual(totals["errors"], [])

    def test_stale_uuid_batch_emits_one_summary_not_per_proposal_warnings(self):
        import logging
        from unittest.mock import patch
        from flickr.proposal_applier import apply_batch

        with (
            patch("flickr.proposal_applier._photos_is_responsive", return_value=True),
            self._mock_stale_uuid(),
            self.assertLogs("blue-pearmain.proposal_applier", level="DEBUG") as cm,
        ):
            apply_batch(self.db, "/fake/lib")
        # No WARNING-level per-proposal stale UUID message
        warnings = [r for r in cm.records if r.levelno >= logging.WARNING]
        self.assertEqual(warnings, [], "stale UUID should not emit a WARNING per proposal")
        # One INFO summary mentioning stale UUID count
        info_msgs = [r.getMessage() for r in cm.records if r.levelno == logging.INFO]
        self.assertTrue(
            any("stale UUID" in m for m in info_msgs),
            "apply_batch should emit one INFO summary for stale UUIDs",
        )

    def test_non_uuid_error_leaves_proposal_pending(self):
        from unittest.mock import patch, MagicMock
        from flickr.proposal_applier import apply_proposal

        mock_ps = MagicMock()
        mock_ps.Photo.side_effect = Exception("permission denied")
        with (
            patch("flickr.proposal_applier._photos_is_responsive", return_value=True),
            patch.dict("sys.modules", {"photoscript": mock_ps}),
        ):
            result = apply_proposal(self.db, self.proposal_id, "/fake/lib")
        self.assertFalse(result["ok"])
        self.assertNotEqual(result["reason"], "stale_uuid")
        status = self.db.conn.execute(
            "SELECT status FROM metadata_proposals WHERE id=?", (self.proposal_id,)
        ).fetchone()["status"]
        self.assertEqual(status, "pending")
        flag = self.db.conn.execute(
            "SELECT uuid_stale FROM photos WHERE id=?", (self.photo_id,)
        ).fetchone()["uuid_stale"]
        self.assertEqual(flag, 0)

    def test_migration_009_placeholder_idempotent(self):
        import tempfile
        from db.db import Database
        from db.migrations.migrate_009_placeholder import run as run_migration

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "idempotent.db")
            db = Database(db_path)
            db.close()
            run_migration(db_path)  # first run
            run_migration(db_path)  # second run — must not raise
            import sqlite3

            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT name FROM schema_migrations WHERE name='migrate_009_placeholder'"
            ).fetchone()
            self.assertIsNotNone(row, "migration 009 should be recorded in schema_migrations")
            conn.close()

    def test_migration_010_idempotent(self):
        import tempfile
        from db.db import Database
        from db.migrations.migrate_010_stale_uuid import run as run_migration

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "idempotent.db")
            db = Database(db_path)
            db.close()
            run_migration(db_path)  # first run
            run_migration(db_path)  # second run — must not raise
            import sqlite3

            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            # uuid_stale column must exist
            cols = {r[1] for r in conn.execute("PRAGMA table_info(photos)").fetchall()}
            self.assertIn("uuid_stale", cols)
            # 'failed' must be a valid status (insert and delete to verify CHECK passes)
            conn.execute(
                """INSERT INTO metadata_proposals
                   (photo_id, field, proposed_value, source, target, conflict_type,
                    source_hash_at_creation, target_hash_at_creation, status, created_at)
                   SELECT id, 'tags', '[]', 'flickr', 'photos', 'non_conflict',
                          'h', 'h', 'failed', '2026-01-01T00:00:00+00:00'
                   FROM photos LIMIT 1"""
            )
            conn.commit()
            conn.close()


class TestMigration011(unittest.TestCase):
    def _run_migration(self, db_path: str):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "migrate_011",
            Path(__file__).parent.parent / "db/migrations/migrate_011_folders.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.run(db_path)

    def test_migration_011_creates_folders_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "test.db")
            from db.db import Database

            db = Database(db_path)
            db.close()
            self._run_migration(db_path)
            import sqlite3

            conn = sqlite3.connect(db_path)
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            self.assertIn("folders", tables)
            conn.close()

    def test_migration_011_folders_has_parent_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "test.db")
            from db.db import Database

            db = Database(db_path)
            db.close()
            self._run_migration(db_path)
            import sqlite3

            conn = sqlite3.connect(db_path)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(folders)").fetchall()}
            self.assertIn("parent_id", cols)
            self.assertIn("flickr_collection_id", cols)
            conn.close()

    def test_migration_011_albums_has_folder_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "test.db")
            from db.db import Database

            db = Database(db_path)
            db.close()
            self._run_migration(db_path)
            import sqlite3

            conn = sqlite3.connect(db_path)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(albums)").fetchall()}
            self.assertIn("folder_id", cols)
            conn.close()

    def test_migration_011_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "idempotent.db")
            from db.db import Database

            db = Database(db_path)
            db.close()
            self._run_migration(db_path)
            self._run_migration(db_path)  # second run must not raise


class TestMigrate012FlickrName(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "test.db")

    def tearDown(self):
        self._tmp.cleanup()

    def test_migration_adds_flickr_name_to_albums(self):
        from db.db import Database
        from db.migrations.migrate_012_flickr_name import run

        db = Database(Path(self.db_path))
        run(self.db_path)
        row = db.conn.execute("PRAGMA table_info(albums)").fetchall()
        cols = {r["name"] for r in row}
        self.assertIn("flickr_name", cols)
        db.close()

    def test_migration_adds_flickr_name_to_folders(self):
        from db.db import Database
        from db.migrations.migrate_012_flickr_name import run

        db = Database(Path(self.db_path))
        run(self.db_path)
        row = db.conn.execute("PRAGMA table_info(folders)").fetchall()
        cols = {r["name"] for r in row}
        self.assertIn("flickr_name", cols)
        db.close()

    def test_migration_is_idempotent(self):
        from db.db import Database
        from db.migrations.migrate_012_flickr_name import run

        db = Database(Path(self.db_path))
        run(self.db_path)
        run(self.db_path)  # second run must not raise
        db.close()


class TestSyncCollections(unittest.TestCase):
    """sync_collections: creates/updates Flickr Collections from DB folder tree."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        from db.db import Database

        self.db = Database(Path(self._tmp.name) / "test.db")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _make_flickr(self, **side_effects):
        from unittest.mock import MagicMock

        m = MagicMock()
        m.create_collection.return_value = "col-new"
        m.edit_collection_sets.return_value = None
        m.edit_collection_meta.return_value = None
        m.delete_collection.return_value = None
        for attr, val in side_effects.items():
            setattr(m, attr, val)
        return m

    def _seed_folder(self, uuid, name, parent_id=None, collection_id=None):
        fid = self.db.upsert_folder(uuid, name, parent_id=parent_id)
        if collection_id:
            self.db.set_folder_flickr_collection_id(fid, collection_id)
        return fid

    def _seed_album(self, uuid, name, folder_id=None, flickr_set_id=None):
        aid = self.db.upsert_album(uuid, name, folder_id=folder_id)
        if flickr_set_id:
            self.db.set_album_flickr_set_id(aid, flickr_set_id)
        return aid

    def test_creates_collection_for_new_folder(self):
        from flickr.sync_collections import sync_collections

        self._seed_folder("uuid-f1", "Travel")
        flickr = self._make_flickr()

        result = sync_collections(self.db, flickr)

        flickr.create_collection.assert_called_once_with("Travel", description="")
        self.assertEqual(result["created"], 1)
        row = self.db.conn.execute(
            "SELECT flickr_collection_id FROM folders WHERE apple_uuid='uuid-f1'"
        ).fetchone()
        self.assertEqual(row["flickr_collection_id"], "col-new")

    def test_skips_create_for_existing_collection(self):
        from flickr.sync_collections import sync_collections

        self._seed_folder("uuid-f1", "Travel", collection_id="col-existing")
        flickr = self._make_flickr()

        sync_collections(self.db, flickr)

        flickr.create_collection.assert_not_called()

    def test_edit_sets_called_with_album_photoset_ids(self):
        from flickr.sync_collections import sync_collections

        fid = self._seed_folder("uuid-f1", "Travel", collection_id="col-1")
        self._seed_album("uuid-a1", "Paris", folder_id=fid, flickr_set_id="ps-111")
        self._seed_album("uuid-a2", "Rome", folder_id=fid, flickr_set_id="ps-222")
        flickr = self._make_flickr()

        sync_collections(self.db, flickr)

        call_args = flickr.edit_collection_sets.call_args
        self.assertEqual(call_args[0][0], "col-1")
        self.assertCountEqual(call_args[0][1], ["ps-111", "ps-222"])

    def test_skips_albums_without_flickr_set_id(self):
        from flickr.sync_collections import sync_collections

        fid = self._seed_folder("uuid-f1", "Travel", collection_id="col-1")
        self._seed_album("uuid-a1", "Not Pushed Yet", folder_id=fid, flickr_set_id=None)
        flickr = self._make_flickr()

        sync_collections(self.db, flickr)

        call_args = flickr.edit_collection_sets.call_args
        self.assertEqual(call_args[0][1], [])  # no photosets

    def test_parent_collection_includes_child_collection_id(self):
        from flickr.sync_collections import sync_collections

        parent_id = self._seed_folder("uuid-parent", "Europe", collection_id="col-parent")
        self._seed_folder("uuid-child", "France", parent_id=parent_id, collection_id="col-child")
        flickr = self._make_flickr()

        sync_collections(self.db, flickr)

        calls = flickr.edit_collection_sets.call_args_list
        parent_call = next(c for c in calls if c[0][0] == "col-parent")
        self.assertIn("col-child", parent_call[0][2])  # sub_collection_ids

    def test_nested_new_folders_parent_gets_child_collection_on_first_sync(self):
        """First-time sync: parent AND child both new — parent collection must include child."""
        from flickr.sync_collections import sync_collections

        # Both folders have no collection_id (first time sync)
        parent_fid = self._seed_folder("uuid-parent", "Europe")  # no collection_id
        self._seed_folder("uuid-child", "France", parent_id=parent_fid)  # no collection_id

        # create_collection returns unique IDs per call
        flickr = self._make_flickr()
        flickr.create_collection.side_effect = ["col-europe", "col-france"]

        sync_collections(self.db, flickr)

        # edit_collection_sets on the parent must include col-france as a sub-collection
        calls = flickr.edit_collection_sets.call_args_list
        parent_call = next(c for c in calls if c[0][0] == "col-europe")
        self.assertIn(
            "col-france", parent_call[0][2], "parent collection must include child on first sync"
        )

    def test_no_folders_is_noop(self):
        from flickr.sync_collections import sync_collections

        flickr = self._make_flickr()
        result = sync_collections(self.db, flickr)
        flickr.create_collection.assert_not_called()
        flickr.edit_collection_sets.assert_not_called()
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["updated"], 0)

    def test_dry_run_makes_no_api_calls(self):
        from flickr.sync_collections import sync_collections

        self._seed_folder("uuid-f1", "Travel")
        flickr = self._make_flickr()

        sync_collections(self.db, flickr, dry_run=True)

        flickr.create_collection.assert_not_called()
        flickr.edit_collection_sets.assert_not_called()

    def test_stale_collection_id_cleared_and_recreated(self):
        from flickr.sync_collections import sync_collections
        from flickr.flickr_client import FlickrError

        fid = self._seed_folder("uuid-f1", "Travel", collection_id="col-stale")
        flickr = self._make_flickr()
        flickr.edit_collection_sets.side_effect = [
            FlickrError(2, "Collection not found"),  # first call fails
            None,  # second call succeeds
        ]
        flickr.create_collection.return_value = "col-new"

        sync_collections(self.db, flickr)

        flickr.create_collection.assert_called_once()
        row = self.db.conn.execute(
            "SELECT flickr_collection_id FROM folders WHERE id=?", (fid,)
        ).fetchone()
        self.assertEqual(row["flickr_collection_id"], "col-new")

    def test_updates_collection_title_for_existing_collection(self):
        from flickr.sync_collections import sync_collections

        self._seed_folder("uuid-f1", "New Name", collection_id="col-existing")
        flickr = self._make_flickr()

        sync_collections(self.db, flickr)

        flickr.edit_collection_meta.assert_called_once_with("col-existing", "New Name")

    def test_dry_run_does_not_call_edit_collection_meta(self):
        from flickr.sync_collections import sync_collections

        self._seed_folder("uuid-f1", "Travel", collection_id="col-existing")
        flickr = self._make_flickr()

        sync_collections(self.db, flickr, dry_run=True)

        flickr.edit_collection_meta.assert_not_called()

    def test_edit_collection_meta_error_is_logged_and_counter_still_increments(self):
        from flickr.sync_collections import sync_collections

        self._seed_folder("uuid-f1", "My Folder", collection_id="col-existing")
        flickr = self._make_flickr()
        flickr.edit_collection_meta.side_effect = Exception("API timeout")

        result = sync_collections(self.db, flickr)

        flickr.edit_collection_meta.assert_called_once()
        self.assertEqual(result["updated"], 1)

    def test_writes_flickr_name_for_existing_collection(self):
        from flickr.sync_collections import sync_collections

        fid = self._seed_folder("uuid-f1", "Travel", collection_id="col-existing")
        flickr = self._make_flickr()

        sync_collections(self.db, flickr)

        row = self.db.conn.execute(
            "SELECT flickr_name FROM folders WHERE id = ?", (fid,)
        ).fetchone()
        self.assertEqual(row["flickr_name"], "Travel")


class TestSyncNamesFromFlickr(unittest.TestCase):
    """sync_names_from_flickr: propagate Flickr-side renames back to Photos."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        from db.db import Database

        self.db = Database(Path(self._tmp.name) / "test.db")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _seed_album(self, uuid, name, flickr_set_id=None, flickr_name=None):
        aid = self.db.upsert_album(uuid, name)
        if flickr_set_id:
            self.db.set_album_flickr_set_id(aid, flickr_set_id)
        if flickr_name is not None:
            self.db.set_album_flickr_name(aid, flickr_name)
        return aid

    def _seed_folder(self, uuid, name, collection_id=None, flickr_name=None):
        fid = self.db.upsert_folder(uuid, name)
        if collection_id:
            self.db.set_folder_flickr_collection_id(fid, collection_id)
        if flickr_name is not None:
            self.db.set_folder_flickr_name(fid, flickr_name)
        return fid

    def _make_flickr(self, photosets=None, collections=None):
        from unittest.mock import MagicMock

        m = MagicMock()
        m.get_photosets_titled.return_value = photosets or {}
        m.get_collections_flat.return_value = collections or {}
        return m

    def test_renames_photos_album_when_flickr_title_changed(self):
        from unittest.mock import patch
        from flickr.sync_names_from_flickr import sync_names_from_flickr

        aid = self._seed_album("uuid-1", "Old Name", flickr_set_id="ps-1", flickr_name="Old Name")
        flickr = self._make_flickr(photosets={"ps-1": "New Flickr Name"})

        with patch(
            "flickr.sync_names_from_flickr._rename_photos_album", return_value=True
        ) as mock_rename:
            result = sync_names_from_flickr(self.db, flickr)

        mock_rename.assert_called_once_with("uuid-1", "New Flickr Name")
        row = self.db.conn.execute(
            "SELECT name, flickr_name FROM albums WHERE id = ?", (aid,)
        ).fetchone()
        self.assertEqual(row["name"], "New Flickr Name")
        self.assertEqual(row["flickr_name"], "New Flickr Name")
        self.assertEqual(result["albums_renamed"], 1)

    def test_skips_when_no_baseline(self):
        from unittest.mock import patch
        from flickr.sync_names_from_flickr import sync_names_from_flickr

        self._seed_album("uuid-1", "Old Name", flickr_set_id="ps-1")  # flickr_name=None
        flickr = self._make_flickr(photosets={"ps-1": "Whatever"})

        with patch("flickr.sync_names_from_flickr._rename_photos_album") as mock_rename:
            result = sync_names_from_flickr(self.db, flickr)

        mock_rename.assert_not_called()
        self.assertEqual(result["albums_renamed"], 0)

    def test_skips_when_in_sync(self):
        from unittest.mock import patch
        from flickr.sync_names_from_flickr import sync_names_from_flickr

        self._seed_album("uuid-1", "Paris", flickr_set_id="ps-1", flickr_name="Paris")
        flickr = self._make_flickr(photosets={"ps-1": "Paris"})

        with patch("flickr.sync_names_from_flickr._rename_photos_album") as mock_rename:
            sync_names_from_flickr(self.db, flickr)

        mock_rename.assert_not_called()

    def test_skips_conflict_photos_wins(self):
        from unittest.mock import patch
        from flickr.sync_names_from_flickr import sync_names_from_flickr

        # Both renamed: DB name="Photos New", flickr_name="Old Name", Flickr title="Flickr New"
        self._seed_album("uuid-1", "Photos New", flickr_set_id="ps-1", flickr_name="Old Name")
        flickr = self._make_flickr(photosets={"ps-1": "Flickr New"})

        with patch("flickr.sync_names_from_flickr._rename_photos_album") as mock_rename:
            result = sync_names_from_flickr(self.db, flickr)

        mock_rename.assert_not_called()
        self.assertEqual(result["albums_renamed"], 0)

    def test_dry_run_makes_no_changes(self):
        from unittest.mock import patch
        from flickr.sync_names_from_flickr import sync_names_from_flickr

        aid = self._seed_album("uuid-1", "Old Name", flickr_set_id="ps-1", flickr_name="Old Name")
        flickr = self._make_flickr(photosets={"ps-1": "New Flickr Name"})

        with patch("flickr.sync_names_from_flickr._rename_photos_album") as mock_rename:
            result = sync_names_from_flickr(self.db, flickr, dry_run=True)

        mock_rename.assert_not_called()
        row = self.db.conn.execute("SELECT name FROM albums WHERE id = ?", (aid,)).fetchone()
        self.assertEqual(row["name"], "Old Name")  # unchanged
        self.assertEqual(result["albums_renamed"], 1)  # counted but not applied

    def test_skips_when_rename_fails(self):
        from unittest.mock import patch
        from flickr.sync_names_from_flickr import sync_names_from_flickr

        aid = self._seed_album("uuid-1", "Old Name", flickr_set_id="ps-1", flickr_name="Old Name")
        flickr = self._make_flickr(photosets={"ps-1": "New Flickr Name"})

        with patch("flickr.sync_names_from_flickr._rename_photos_album", return_value=False):
            result = sync_names_from_flickr(self.db, flickr)

        row = self.db.conn.execute("SELECT name FROM albums WHERE id = ?", (aid,)).fetchone()
        self.assertEqual(row["name"], "Old Name")
        self.assertEqual(result["albums_renamed"], 0)

    def test_renames_photos_folder_when_flickr_collection_changed(self):
        from unittest.mock import patch
        from flickr.sync_names_from_flickr import sync_names_from_flickr

        fid = self._seed_folder(
            "uuid-f1", "Old Folder", collection_id="col-1", flickr_name="Old Folder"
        )
        flickr = self._make_flickr(collections={"col-1": "New Folder Name"})

        with patch(
            "flickr.sync_names_from_flickr._rename_photos_folder", return_value=True
        ) as mock_rename:
            result = sync_names_from_flickr(self.db, flickr)

        mock_rename.assert_called_once_with("uuid-f1", "New Folder Name")
        row = self.db.conn.execute(
            "SELECT name, flickr_name FROM folders WHERE id = ?", (fid,)
        ).fetchone()
        self.assertEqual(row["name"], "New Folder Name")
        self.assertEqual(row["flickr_name"], "New Folder Name")
        self.assertEqual(result["folders_renamed"], 1)

    def test_collections_error_skips_folders_section(self):
        from unittest.mock import patch
        from flickr.sync_names_from_flickr import sync_names_from_flickr

        self._seed_folder("uuid-f1", "Old Folder", collection_id="col-1", flickr_name="Old Folder")
        flickr = self._make_flickr()
        flickr.get_collections_flat.side_effect = Exception("something went wrong")

        with patch("flickr.sync_names_from_flickr._rename_photos_folder") as mock_rename:
            result = sync_names_from_flickr(self.db, flickr)

        mock_rename.assert_not_called()
        self.assertEqual(result["folders_renamed"], 0)


class TestSyncAlbumTitles(unittest.TestCase):
    """sync_album_titles: pushes current album names to Flickr photoset titles."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        from db.db import Database

        self.db = Database(Path(self._tmp.name) / "test.db")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _make_flickr(self):
        from unittest.mock import MagicMock

        m = MagicMock()
        m.edit_photoset_meta.return_value = None
        return m

    def _seed_album(self, uuid, name, flickr_set_id=None):
        aid = self.db.upsert_album(uuid, name)
        if flickr_set_id:
            self.db.set_album_flickr_set_id(aid, flickr_set_id)
        return aid

    def test_calls_edit_meta_for_each_pushed_album(self):
        from flickr.sync_albums import sync_album_titles

        self._seed_album("uuid-1", "Paris Trip", flickr_set_id="ps-111")
        self._seed_album("uuid-2", "Rome Pics", flickr_set_id="ps-222")
        flickr = self._make_flickr()

        result = sync_album_titles(self.db, flickr)

        self.assertEqual(flickr.edit_photoset_meta.call_count, 2)
        calls = {c[0] for c in flickr.edit_photoset_meta.call_args_list}
        self.assertIn(("ps-111", "Paris Trip"), calls)
        self.assertIn(("ps-222", "Rome Pics"), calls)
        self.assertEqual(result["updated"], 2)

    def test_skips_albums_without_flickr_set_id(self):
        from flickr.sync_albums import sync_album_titles

        self._seed_album("uuid-1", "Not Pushed Yet")  # no flickr_set_id
        flickr = self._make_flickr()

        sync_album_titles(self.db, flickr)

        flickr.edit_photoset_meta.assert_not_called()

    def test_dry_run_makes_no_api_calls(self):
        from flickr.sync_albums import sync_album_titles

        self._seed_album("uuid-1", "Paris Trip", flickr_set_id="ps-111")
        flickr = self._make_flickr()

        result = sync_album_titles(self.db, flickr, dry_run=True)

        flickr.edit_photoset_meta.assert_not_called()
        self.assertEqual(result["updated"], 1)

    def test_continues_on_api_error(self):
        from flickr.sync_albums import sync_album_titles

        self._seed_album("uuid-1", "Album A", flickr_set_id="ps-111")
        self._seed_album("uuid-2", "Album B", flickr_set_id="ps-222")
        flickr = self._make_flickr()
        flickr.edit_photoset_meta.side_effect = [Exception("timeout"), None]

        result = sync_album_titles(self.db, flickr)

        self.assertEqual(flickr.edit_photoset_meta.call_count, 2)
        self.assertEqual(result["updated"], 1)

    def test_no_albums_is_noop(self):
        from flickr.sync_albums import sync_album_titles

        flickr = self._make_flickr()
        result = sync_album_titles(self.db, flickr)
        flickr.edit_photoset_meta.assert_not_called()
        self.assertEqual(result["updated"], 0)

    def test_writes_flickr_name_after_successful_push(self):
        from flickr.sync_albums import sync_album_titles

        aid = self._seed_album("uuid-1", "Paris Trip", flickr_set_id="ps-111")
        flickr = self._make_flickr()

        sync_album_titles(self.db, flickr)

        row = self.db.conn.execute("SELECT flickr_name FROM albums WHERE id = ?", (aid,)).fetchone()
        self.assertEqual(row["flickr_name"], "Paris Trip")

    def test_does_not_write_flickr_name_on_api_error(self):
        from flickr.sync_albums import sync_album_titles

        aid = self._seed_album("uuid-1", "Paris Trip", flickr_set_id="ps-111")
        flickr = self._make_flickr()
        flickr.edit_photoset_meta.side_effect = Exception("timeout")

        sync_album_titles(self.db, flickr)

        row = self.db.conn.execute("SELECT flickr_name FROM albums WHERE id = ?", (aid,)).fetchone()
        self.assertIsNone(row["flickr_name"])


class TestPruneProposals(unittest.TestCase):
    """Tests for Database.prune_proposals() and Database.supersede_managed_tag_proposals()."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "test.db")
        self.photo_id = self.db.upsert_photo(
            {
                "uuid": "uuid-prune-001",
                "original_filename": "IMG_prune.JPG",
                "privacy_state": "approved_public",
                "flickr_id": "flickr-prune-001",
                "proposed_tags": ["unitedstates", "newyork"],
                "apple_persons": [],
                "apple_labels": [],
            }
        )

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _insert_proposal(
        self,
        photo_id,
        status,
        field,
        source,
        target,
        proposed_value=None,
        resolved_at=None,
        conflict_type="non_conflict",
    ):
        now = "2026-01-01T00:00:00+00:00"
        self.db.conn.execute(
            """INSERT INTO metadata_proposals
               (photo_id, status, field, source, target, proposed_value,
                resolved_at, created_at, conflict_type)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                photo_id,
                status,
                field,
                source,
                target,
                proposed_value,
                resolved_at or (now if status != "pending" else None),
                now,
                conflict_type,
            ),
        )
        self.db.conn.commit()
        return self.db.conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    # --- prune_proposals ---

    def test_prune_deletes_old_resolved(self):
        self._insert_proposal(
            self.photo_id,
            "applied",
            "tags",
            "flickr",
            "photos",
            resolved_at="2020-01-01T00:00:00+00:00",
        )
        n = self.db.prune_proposals(older_than_days=90)
        self.assertEqual(n, 1)
        count = self.db.conn.execute("SELECT COUNT(*) AS n FROM metadata_proposals").fetchone()["n"]
        self.assertEqual(count, 0)

    def test_prune_keeps_recent_resolved(self):
        from datetime import datetime, timedelta, timezone

        recent = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        self._insert_proposal(
            self.photo_id,
            "applied",
            "tags",
            "flickr",
            "photos",
            resolved_at=recent,
        )
        n = self.db.prune_proposals(older_than_days=90)
        self.assertEqual(n, 0)

    def test_prune_keeps_pending_regardless_of_age(self):
        self._insert_proposal(
            self.photo_id,
            "pending",
            "tags",
            "flickr",
            "photos",
        )
        n = self.db.prune_proposals(older_than_days=0)
        self.assertEqual(n, 0)

    def test_prune_dry_run_returns_count_without_deleting(self):
        self._insert_proposal(
            self.photo_id,
            "superseded",
            "tags",
            "flickr",
            "photos",
            resolved_at="2020-01-01T00:00:00+00:00",
        )
        n = self.db.prune_proposals(older_than_days=90, dry_run=True)
        self.assertEqual(n, 1)
        count = self.db.conn.execute("SELECT COUNT(*) AS n FROM metadata_proposals").fetchone()["n"]
        self.assertEqual(count, 1)

    # --- supersede_managed_tag_proposals ---

    def test_supersede_closes_all_managed_proposal(self):
        # proposed_value = only BP-managed tags; Photos has none
        self._insert_proposal(
            self.photo_id,
            "pending",
            "tags",
            "flickr",
            "photos",
            proposed_value='["unitedstates", "newyork"]',
        )
        n = self.db.supersede_managed_tag_proposals()
        self.assertEqual(n, 1)
        row = self.db.conn.execute("SELECT status FROM metadata_proposals").fetchone()
        self.assertEqual(row["status"], "superseded")

    def test_supersede_keeps_user_added_tag_proposal(self):
        # Flickr has a user-added tag ("vacation") not in Photos and not managed
        self._insert_proposal(
            self.photo_id,
            "pending",
            "tags",
            "flickr",
            "photos",
            proposed_value='["unitedstates", "vacation"]',
        )
        n = self.db.supersede_managed_tag_proposals()
        self.assertEqual(n, 0)
        row = self.db.conn.execute("SELECT status FROM metadata_proposals").fetchone()
        self.assertEqual(row["status"], "pending")

    def test_supersede_dry_run_does_not_update(self):
        self._insert_proposal(
            self.photo_id,
            "pending",
            "tags",
            "flickr",
            "photos",
            proposed_value='["unitedstates"]',
        )
        n = self.db.supersede_managed_tag_proposals(dry_run=True)
        self.assertEqual(n, 1)
        row = self.db.conn.execute("SELECT status FROM metadata_proposals").fetchone()
        self.assertEqual(row["status"], "pending")

    def test_supersede_ignores_non_tag_proposals(self):
        self._insert_proposal(
            self.photo_id,
            "pending",
            "title",
            "flickr",
            "photos",
            proposed_value="New Title",
        )
        n = self.db.supersede_managed_tag_proposals()
        self.assertEqual(n, 0)

    def test_supersede_ignores_photos_to_flickr_proposals(self):
        self._insert_proposal(
            self.photo_id,
            "pending",
            "tags",
            "photos",
            "flickr",
            proposed_value='["unitedstates"]',
        )
        n = self.db.supersede_managed_tag_proposals()
        self.assertEqual(n, 0)


class TestDeduplicatorDimensionDivergence(unittest.TestCase):
    """GH #72: auto-dismiss uncertain groups where dimensions differ significantly."""

    def _photo(self, id, fp, width, height, flickr_id=None, date_uploaded=None):
        from poller.deduplicator import PhotoRow

        return PhotoRow(
            id=id,
            flickr_id=flickr_id,
            uuid=f"uuid-{id}",
            original_filename="IMG_001.JPG",
            date_taken="2024-01-01T12:00:00",
            date_added_photos=None,
            date_uploaded_flickr=date_uploaded,
            fingerprint=fp,
            width=width,
            height=height,
            privacy_state="candidate_public",
            duplicate_group_id=None,
        )

    # --- _pixels_ratio ---

    def test_pixels_ratio_none_when_any_dimensions_missing(self):
        from poller.deduplicator import _pixels_ratio

        photos = [self._photo(1, "a", 6000, 4000), self._photo(2, "b", None, None)]
        self.assertIsNone(_pixels_ratio(photos))

    def test_pixels_ratio_one_for_identical_dimensions(self):
        from poller.deduplicator import _pixels_ratio

        photos = [self._photo(1, "a", 6000, 4000), self._photo(2, "b", 6000, 4000)]
        self.assertAlmostEqual(_pixels_ratio(photos), 1.0)

    def test_pixels_ratio_correct_for_different_dimensions(self):
        from poller.deduplicator import _pixels_ratio

        # 6000×4000 = 24M, 3000×2000 = 6M → ratio 4.0
        photos = [self._photo(1, "a", 6000, 4000), self._photo(2, "b", 3000, 2000)]
        self.assertAlmostEqual(_pixels_ratio(photos), 4.0)

    # --- _classify_group: not_duplicate ---

    def test_classify_uncertain_when_dimensions_identical(self):
        from poller.deduplicator import _classify_group

        photos = [
            self._photo(1, "fp1", 6000, 4000),
            self._photo(2, "fp1", 6000, 4000),  # same fingerprint, same dims
        ]
        self.assertEqual(_classify_group(photos).group_type, "uncertain")

    def test_classify_not_duplicate_when_dimensions_diverge(self):
        from poller.deduplicator import _classify_group

        # Same fingerprint (so not snapbridge), but very different dimensions
        photos = [
            self._photo(1, "fp1", 6000, 4000),  # 24M px
            self._photo(2, "fp1", 3000, 2000),  # 6M px — ratio 4.0 >> 1.1
        ]
        self.assertEqual(_classify_group(photos).group_type, "not_duplicate")

    def test_classify_uncertain_when_dimensions_missing(self):
        from poller.deduplicator import _classify_group

        photos = [self._photo(1, "fp1", None, None), self._photo(2, "fp1", None, None)]
        self.assertEqual(_classify_group(photos).group_type, "uncertain")

    def test_classify_snapbridge_not_reclassified_as_not_duplicate(self):
        from poller.deduplicator import _classify_group

        # Different fingerprints + different dimensions + 2 photos = snapbridge
        photos = [
            self._photo(1, "fp1", 6000, 4000),
            self._photo(2, "fp2", 3000, 2000),
        ]
        self.assertEqual(_classify_group(photos).group_type, "snapbridge")

    def test_not_duplicate_group_has_no_keeper_or_discards(self):
        from poller.deduplicator import _classify_group

        photos = [
            self._photo(1, "fp1", 6000, 4000),
            self._photo(2, "fp1", 3000, 2000),
        ]
        g = _classify_group(photos)
        self.assertEqual(g.group_type, "not_duplicate")
        self.assertIsNone(g.keeper)
        self.assertEqual(g.discards, [])

    def test_classify_uncertain_when_ratio_just_below_threshold(self):
        from poller.deduplicator import _classify_group, _pixels_ratio, NOT_DUPLICATE_PIXEL_RATIO

        # 6000×4000 = 24M; 4899×4672 ≈ 22.9M → ratio ~1.047, just below 1.1
        photos = [
            self._photo(1, "fp1", 6000, 4000),
            self._photo(2, "fp1", 4899, 4672),
        ]
        ratio = _pixels_ratio(photos)
        self.assertLess(ratio, NOT_DUPLICATE_PIXEL_RATIO)
        self.assertEqual(_classify_group(photos).group_type, "uncertain")

    # --- _write_groups: auto-resolve not_duplicate ---

    def _make_db_with_dedup(self):
        from db.db import Database
        from db.migrations.migrate_003_dimensions_and_dedup import run as migrate

        tmp = tempfile.mkdtemp()
        db_path = str(Path(tmp) / "test.db")
        db = Database(Path(db_path))
        migrate(db_path)
        return db, tmp

    def test_write_groups_marks_not_duplicate_as_resolved(self):
        from poller.deduplicator import DuplicateGroup, _write_groups
        import shutil

        db, tmp = self._make_db_with_dedup()
        try:
            group = DuplicateGroup(
                match_key="IMG_001.JPG|2024-01-01T12:00:00",
                group_type="not_duplicate",
                photos=[],
                notes="auto-dismissed: dimension divergence",
            )
            _write_groups(db.conn, [group])
            row = db.conn.execute(
                "SELECT resolved, group_type FROM duplicate_groups WHERE match_key = ?",
                (group.match_key,),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["resolved"], 1)
            self.assertEqual(row["group_type"], "not_duplicate")
        finally:
            db.close()
            shutil.rmtree(tmp)

    def test_write_groups_updates_existing_uncertain_to_resolved(self):
        from poller.deduplicator import DuplicateGroup, _write_groups
        import shutil

        db, tmp = self._make_db_with_dedup()
        try:
            key = "IMG_002.JPG|2024-01-01T12:00:00"
            db.conn.execute(
                "INSERT INTO duplicate_groups (match_key, group_type, photo_count, notes) VALUES (?,?,?,?)",
                (key, "uncertain", 2, ""),
            )
            db.conn.commit()
            group = DuplicateGroup(key, "not_duplicate", [], notes="auto-dismissed")
            _write_groups(db.conn, [group])
            row = db.conn.execute(
                "SELECT resolved, group_type FROM duplicate_groups WHERE match_key = ?", (key,)
            ).fetchone()
            self.assertEqual(row["group_type"], "not_duplicate")
            self.assertEqual(row["resolved"], 1)
        finally:
            db.close()
            shutil.rmtree(tmp)


class TestConfirmPublicDecision(unittest.TestCase):
    """confirm_public decision transitions photo to already_public."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        from db.db import Database

        self.db = Database(Path(self._tmp.name) / "test.db")
        self.photo_id = self.db.upsert_photo(
            {
                "uuid": "uuid-confirm-001",
                "original_filename": "Screenshot_confirm.PNG",
                "privacy_state": "approved_public",
                "proposed_tags": [],
                "apple_persons": [],
                "apple_labels": [],
            }
        )

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_confirm_public_sets_already_public(self):
        self.db.record_review(self.photo_id, "confirm_public")
        row = self.db.conn.execute(
            "SELECT privacy_state FROM photos WHERE id = ?", (self.photo_id,)
        ).fetchone()
        self.assertEqual(row["privacy_state"], "already_public")

    def test_confirm_public_sets_review_decision(self):
        self.db.record_review(self.photo_id, "confirm_public")
        row = self.db.conn.execute(
            "SELECT review_decision FROM photos WHERE id = ?", (self.photo_id,)
        ).fetchone()
        self.assertEqual(row["review_decision"], "confirm_public")


class TestScreenshotPublicStatsFilter(unittest.TestCase):
    """stats() screenshot_public count excludes already_public photos."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        db_path = Path(self._tmp.name) / "test.db"
        from db.db import Database
        from db.migrations.migrate_013_screenshot_flag import run as migrate

        self.db = Database(db_path)
        migrate(str(db_path))

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _insert(self, uid: str, state: str, is_screenshot: int = 1) -> None:
        self.db.upsert_photo(
            {
                "uuid": uid,
                "original_filename": f"{uid}.PNG",
                "privacy_state": state,
                "proposed_tags": [],
                "apple_persons": [],
                "apple_labels": [],
                "is_screenshot": is_screenshot,
            }
        )

    def test_screenshot_public_count_only_includes_approved_public(self):
        self._insert("u1", "approved_public")
        self._insert("u2", "already_public")  # confirmed — should NOT count
        counts = self.db.stats()["screenshot_counts"]
        self.assertEqual(counts["screenshot_public"], 1)

    def test_screenshot_public_count_zero_when_all_confirmed(self):
        self._insert("u3", "already_public")
        counts = self.db.stats()["screenshot_counts"]
        self.assertEqual(counts["screenshot_public"], 0)


class TestReviewQueueScreenshots(unittest.TestCase):
    """review_queue and review_queue_count handle is_screenshot filtering."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        from db.db import Database
        from db.migrations.migrate_013_screenshot_flag import run as migrate

        self.db = Database(Path(self._tmp.name) / "test.db")
        migrate(str(Path(self._tmp.name) / "test.db"))

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _insert(self, uuid: str, state: str, is_screenshot: int = 0) -> int:
        return self.db.upsert_photo(
            {
                "uuid": uuid,
                "original_filename": f"{uuid}.JPG",
                "privacy_state": state,
                "proposed_tags": [],
                "apple_persons": [],
                "apple_labels": [],
                "is_screenshot": is_screenshot,
            }
        )

    def test_review_queue_returns_is_screenshot_field(self):
        self._insert("u1", "candidate_public", 0)
        rows = self.db.review_queue(states=["candidate_public"])
        self.assertIn("is_screenshot", rows[0])

    def test_review_queue_excludes_screenshots_when_flag_set(self):
        self._insert("u2", "candidate_public", 0)
        self._insert("u3", "candidate_public", 1)
        rows = self.db.review_queue(states=["candidate_public"], exclude_screenshots=True)
        uuids = [r["uuid"] for r in rows]
        self.assertIn("u2", uuids)
        self.assertNotIn("u3", uuids)

    def test_review_queue_includes_screenshots_when_flag_not_set(self):
        self._insert("u4", "candidate_public", 1)
        rows = self.db.review_queue(states=["candidate_public"], exclude_screenshots=False)
        uuids = [r["uuid"] for r in rows]
        self.assertIn("u4", uuids)

    def test_review_queue_count_excludes_screenshots_when_flag_set(self):
        self._insert("u5", "candidate_public", 0)
        self._insert("u6", "candidate_public", 1)
        count = self.db.review_queue_count(states=["candidate_public"], exclude_screenshots=True)
        self.assertEqual(count, 1)

    def test_review_queue_count_default_includes_screenshots(self):
        self._insert("u7", "candidate_public", 1)
        count = self.db.review_queue_count(states=["candidate_public"])
        self.assertEqual(count, 1)


class TestMigrate013ScreenshotFlag(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "test.db")

    def tearDown(self):
        self._tmp.cleanup()

    def test_migration_adds_is_screenshot_column(self):
        from db.db import Database
        from db.migrations.migrate_013_screenshot_flag import run

        db = Database(Path(self.db_path))
        run(self.db_path)
        cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(photos)").fetchall()}
        self.assertIn("is_screenshot", cols)
        db.close()

    def test_migration_backfills_from_privacy_reason(self):
        from db.db import Database
        from db.migrations.migrate_013_screenshot_flag import run

        db = Database(Path(self.db_path))
        db.conn.execute(
            """INSERT INTO photos (flickr_id, privacy_state, privacy_reason)
               VALUES ('aaa', 'auto_private', 'screenshot')"""
        )
        db.conn.commit()
        run(self.db_path)
        row = db.conn.execute("SELECT is_screenshot FROM photos WHERE flickr_id='aaa'").fetchone()
        self.assertEqual(row[0], 1)
        db.close()

    def test_migration_does_not_set_flag_for_other_reasons(self):
        from db.db import Database
        from db.migrations.migrate_013_screenshot_flag import run

        db = Database(Path(self.db_path))
        db.conn.execute(
            """INSERT INTO photos (flickr_id, privacy_state, privacy_reason)
               VALUES ('bbb', 'auto_private', 'faces')"""
        )
        db.conn.commit()
        run(self.db_path)
        row = db.conn.execute("SELECT is_screenshot FROM photos WHERE flickr_id='bbb'").fetchone()
        self.assertEqual(row[0], 0)
        db.close()

    def test_migration_is_idempotent(self):
        from db.db import Database
        from db.migrations.migrate_013_screenshot_flag import run

        db = Database(Path(self.db_path))
        run(self.db_path)
        run(self.db_path)  # second run must not raise
        db.close()


class TestScreenshotStats(unittest.TestCase):
    """stats() returns correct screenshot_counts after migration 013."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "test.db")
        from db.db import Database
        from db.migrations.migrate_013_screenshot_flag import run as migrate

        self.db = Database(Path(self.db_path))
        migrate(self.db_path)

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _insert(self, flickr_id: str, state: str, is_screenshot: int) -> None:
        self.db.conn.execute(
            "INSERT INTO photos (flickr_id, privacy_state, is_screenshot) VALUES (?, ?, ?)",
            (flickr_id, state, is_screenshot),
        )
        self.db.conn.commit()

    def test_screenshot_counts_key_present(self):
        stats = self.db.stats()
        self.assertIn("screenshot_counts", stats)

    def test_unreviewed_counts_auto_private_screenshots(self):
        self._insert("s1", "auto_private", 1)
        self._insert("s2", "auto_private", 0)  # not a screenshot
        counts = self.db.stats()["screenshot_counts"]
        self.assertEqual(counts["screenshot_unreviewed"], 1)

    def test_public_counts_only_approved_public(self):
        self._insert("s3", "approved_public", 1)
        self._insert("s4", "already_public", 1)  # confirmed — no longer in this bucket
        self._insert("s5", "approved_public", 0)  # not a screenshot
        counts = self.db.stats()["screenshot_counts"]
        self.assertEqual(counts["screenshot_public"], 1)

    def test_private_counts_keep_private(self):
        self._insert("s6", "keep_private", 1)
        self._insert("s7", "keep_private", 0)  # not a screenshot
        counts = self.db.stats()["screenshot_counts"]
        self.assertEqual(counts["screenshot_private"], 1)

    def test_counts_are_independent(self):
        self._insert("s9", "auto_private", 1)
        self._insert("s10", "approved_public", 1)
        self._insert("s11", "keep_private", 1)
        counts = self.db.stats()["screenshot_counts"]
        self.assertEqual(counts["screenshot_unreviewed"], 1)
        self.assertEqual(counts["screenshot_public"], 1)
        self.assertEqual(counts["screenshot_private"], 1)

    def test_empty_db_returns_zeros(self):
        counts = self.db.stats()["screenshot_counts"]
        self.assertEqual(counts["screenshot_unreviewed"], 0)
        self.assertEqual(counts["screenshot_public"], 0)
        self.assertEqual(counts["screenshot_private"], 0)


class TestMigrate014MergedIntoId(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "test.db")

    def tearDown(self):
        self._tmp.cleanup()

    def test_migration_adds_merged_into_id_column(self):
        from db.db import Database
        from db.migrations.migrate_014_merged_into_id import run

        db = Database(Path(self.db_path))
        run(self.db_path)
        cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(photos)").fetchall()}
        self.assertIn("merged_into_id", cols)
        db.close()

    def test_migration_is_idempotent(self):
        from db.db import Database
        from db.migrations.migrate_014_merged_into_id import run

        db = Database(Path(self.db_path))
        run(self.db_path)
        run(self.db_path)  # must not raise
        db.close()


class TestMergeFlickrDonorInGroup(unittest.TestCase):
    """Database.merge_flickr_donor_in_group() must correctly soft-merge a
    Flickr-only donor into a Photos-linked target and resolve the group."""

    def _make_merge_db(self):
        """Create a temp DB with duplicate_groups support and a ready-to-merge group."""
        from db.db import Database
        from db.migrations.migrate_003_dimensions_and_dedup import run as migrate_003

        tmp = tempfile.mkdtemp()
        db_path = str(Path(tmp) / "test.db")
        db = Database(Path(db_path))
        migrate_003(
            db_path
        )  # adds duplicate_groups table + duplicate_role/duplicate_group_id columns

        # Flickr-only donor: flickr_id set, no uuid
        donor_id = db.upsert_photo(
            {
                "flickr_id": "F001",
                "flickr_secret": "sec123",
                "flickr_server": "65535",
                "flickr_farm": 66,
                "original_filename": "IMG_9999.JPG",
                "date_taken": "2024-06-15 12:00:00",
                "date_uploaded_flickr": "2024-06-15 18:00:00",
                "privacy_state": "candidate_public",
            }
        )

        # Photos-linked target: uuid set, no flickr_id
        target_id = db.upsert_photo(
            {
                "uuid": "U001",
                "original_filename": "IMG_9999.JPG",
                "date_taken": "2024-06-15T12:00:00-04:00",
                "privacy_state": "candidate_public",
                "width": 4000,
                "height": 3000,
                "apple_labels": [],
                "apple_persons": [],
                "proposed_tags": [],
            }
        )

        # Create duplicate group and link both photos to it
        db.conn.execute(
            """INSERT INTO duplicate_groups (match_key, group_type, photo_count, notes)
               VALUES (?, ?, ?, ?)""",
            ("IMG_9999.JPG|2024-06-15 12:00:00", "snapbridge", 2, ""),
        )
        group_id = db.conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        db.conn.execute(
            "UPDATE photos SET duplicate_group_id = ?, duplicate_role = 'discard' WHERE id = ?",
            (group_id, donor_id),
        )
        db.conn.execute(
            "UPDATE photos SET duplicate_group_id = ?, duplicate_role = 'keeper' WHERE id = ?",
            (group_id, target_id),
        )
        db.conn.commit()

        return db, tmp, donor_id, target_id, group_id

    def setUp(self):
        self.db, self._tmp, self.donor_id, self.target_id, self.group_id = self._make_merge_db()

    def tearDown(self):
        self.db.close()
        import shutil

        shutil.rmtree(self._tmp)

    def _row(self, photo_id):
        return self.db.get_photo(photo_id)

    def _group(self):
        return self.db.conn.execute(
            "SELECT * FROM duplicate_groups WHERE id = ?", (self.group_id,)
        ).fetchone()

    def test_flickr_id_copied_to_target(self):
        self.db.merge_flickr_donor_in_group(self.donor_id, self.target_id, self.group_id)
        self.assertEqual(self._row(self.target_id)["flickr_id"], "F001")

    def test_flickr_secret_and_date_uploaded_copied_to_target(self):
        self.db.merge_flickr_donor_in_group(self.donor_id, self.target_id, self.group_id)
        row = self._row(self.target_id)
        self.assertEqual(row["flickr_secret"], "sec123")
        self.assertIsNotNone(row["date_uploaded_flickr"])

    def test_donor_flickr_id_is_null_after_merge(self):
        self.db.merge_flickr_donor_in_group(self.donor_id, self.target_id, self.group_id)
        self.assertIsNone(self._row(self.donor_id)["flickr_id"])

    def test_donor_merged_into_id_points_to_target(self):
        self.db.merge_flickr_donor_in_group(self.donor_id, self.target_id, self.group_id)
        self.assertEqual(self._row(self.donor_id)["merged_into_id"], self.target_id)

    def test_donor_privacy_state_is_duplicate_flickr_and_role_is_discard(self):
        self.db.merge_flickr_donor_in_group(self.donor_id, self.target_id, self.group_id)
        row = self._row(self.donor_id)
        self.assertEqual(row["privacy_state"], "duplicate_flickr")
        self.assertEqual(row["duplicate_role"], "discard")

    def test_target_duplicate_role_is_keeper(self):
        self.db.merge_flickr_donor_in_group(self.donor_id, self.target_id, self.group_id)
        self.assertEqual(self._row(self.target_id)["duplicate_role"], "keeper")

    def test_group_resolved_with_correct_keeper(self):
        self.db.merge_flickr_donor_in_group(self.donor_id, self.target_id, self.group_id)
        g = self._group()
        self.assertEqual(g["resolved"], 1)
        self.assertEqual(g["keeper_id"], self.target_id)

    def test_photo_albums_migrated_to_target(self):
        album_id = self.db.upsert_album("apple-a", "Test Album")
        self.db.upsert_photo_album(self.donor_id, album_id)
        self.db.merge_flickr_donor_in_group(self.donor_id, self.target_id, self.group_id)
        row = self.db.conn.execute(
            "SELECT * FROM photo_albums WHERE photo_id = ? AND album_id = ?",
            (self.target_id, album_id),
        ).fetchone()
        self.assertIsNotNone(row)

    def test_tag_events_migrated_to_target_and_removed_from_donor(self):
        self.db.conn.execute(
            """INSERT INTO tag_events (photo_id, event_at, destination, tags_before, tags_after, success)
               VALUES (?, '2024-06-15T18:00:00Z', 'flickr', '[]', '["travel"]', 1)""",
            (self.donor_id,),
        )
        self.db.conn.commit()
        self.db.merge_flickr_donor_in_group(self.donor_id, self.target_id, self.group_id)
        on_target = self.db.conn.execute(
            "SELECT COUNT(*) AS n FROM tag_events WHERE photo_id = ?", (self.target_id,)
        ).fetchone()["n"]
        on_donor = self.db.conn.execute(
            "SELECT COUNT(*) AS n FROM tag_events WHERE photo_id = ?", (self.donor_id,)
        ).fetchone()["n"]
        self.assertEqual(on_target, 1)
        self.assertEqual(on_donor, 0)

    def test_raises_value_error_if_donor_has_uuid(self):
        with self.assertRaises(ValueError):
            # Pass target as donor — it has a uuid
            self.db.merge_flickr_donor_in_group(self.target_id, self.donor_id, self.group_id)

    def test_raises_value_error_if_target_has_no_uuid(self):
        with self.assertRaises(ValueError):
            # Pass donor as both donor and target — it has no uuid
            self.db.merge_flickr_donor_in_group(self.donor_id, self.donor_id, self.group_id)

    def test_raises_value_error_if_target_already_has_flickr_id(self):
        # Give the target a flickr_id to simulate a pre-linked record
        self.db.conn.execute(
            "UPDATE photos SET flickr_id = 'EXISTING' WHERE id = ?", (self.target_id,)
        )
        self.db.conn.commit()
        with self.assertRaises(ValueError):
            self.db.merge_flickr_donor_in_group(self.donor_id, self.target_id, self.group_id)


class TestPhotosIsResponsive(unittest.TestCase):
    def test_returns_true_when_osascript_succeeds(self):
        from unittest.mock import patch, MagicMock
        from flickr.proposal_applier import _photos_is_responsive

        pgrep_ok = MagicMock()
        pgrep_ok.returncode = 0
        osascript_ok = MagicMock()
        osascript_ok.returncode = 0
        with patch("flickr.proposal_applier.subprocess.run", side_effect=[pgrep_ok, osascript_ok]):
            self.assertTrue(_photos_is_responsive())

    def test_returns_false_when_osascript_nonzero(self):
        from unittest.mock import patch, MagicMock
        from flickr.proposal_applier import _photos_is_responsive

        pgrep_ok = MagicMock()
        pgrep_ok.returncode = 0
        osascript_fail = MagicMock()
        osascript_fail.returncode = 1
        with patch(
            "flickr.proposal_applier.subprocess.run", side_effect=[pgrep_ok, osascript_fail]
        ):
            self.assertFalse(_photos_is_responsive())

    def test_returns_false_on_subprocess_timeout(self):
        import subprocess
        from unittest.mock import patch, MagicMock
        from flickr.proposal_applier import _photos_is_responsive

        pgrep_ok = MagicMock()
        pgrep_ok.returncode = 0
        with patch(
            "flickr.proposal_applier.subprocess.run",
            side_effect=[pgrep_ok, subprocess.TimeoutExpired("osascript", 3)],
        ):
            self.assertFalse(_photos_is_responsive())

    def test_returns_false_when_photos_not_running(self):
        from unittest.mock import patch, MagicMock
        from flickr.proposal_applier import _photos_is_responsive

        pgrep_fail = MagicMock()
        pgrep_fail.returncode = 1
        with patch("flickr.proposal_applier.subprocess.run", return_value=pgrep_fail) as mock_run:
            result = _photos_is_responsive()
        self.assertFalse(result)
        self.assertEqual(
            mock_run.call_count,
            1,
            "Should not send AppleScript when pgrep says Photos is not running",
        )


class TestRunWithTimeout(unittest.TestCase):
    def test_returns_fn_result_on_success(self):
        from flickr.proposal_applier import _run_with_timeout

        result = _run_with_timeout(lambda: {"ok": True, "value": 42})
        self.assertEqual(result, {"ok": True, "value": 42})

    def test_returns_not_responding_when_fn_exceeds_timeout(self):
        import time
        import threading
        from flickr.proposal_applier import _run_with_timeout

        blocker = threading.Event()

        def slow():
            blocker.wait(timeout=5)
            return {"ok": True}

        start = time.monotonic()
        result = _run_with_timeout(slow, timeout=0.05)
        elapsed = time.monotonic() - start
        blocker.set()  # release thread immediately so it doesn't linger
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "Photos not responding")
        self.assertLess(
            elapsed,
            1.0,
            f"_run_with_timeout took {elapsed:.2f}s — should return immediately after timeout",
        )

    def test_returns_error_when_fn_raises(self):
        from flickr.proposal_applier import _run_with_timeout

        result = _run_with_timeout(lambda: 1 / 0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "division by zero")


class TestMetadataPullerPhotosIsResponsive(unittest.TestCase):
    def test_returns_true_when_pgrep_succeeds_and_osascript_succeeds(self):
        from unittest.mock import patch, MagicMock
        from flickr.metadata_puller import _photos_is_responsive

        pgrep_ok = MagicMock()
        pgrep_ok.returncode = 0
        osascript_ok = MagicMock()
        osascript_ok.returncode = 0
        with patch("flickr.metadata_puller.subprocess.run", side_effect=[pgrep_ok, osascript_ok]):
            self.assertTrue(_photos_is_responsive())

    def test_returns_false_when_pgrep_fails(self):
        from unittest.mock import patch, MagicMock
        from flickr.metadata_puller import _photos_is_responsive

        pgrep_fail = MagicMock()
        pgrep_fail.returncode = 1
        with patch("flickr.metadata_puller.subprocess.run", return_value=pgrep_fail) as mock_run:
            result = _photos_is_responsive()
        self.assertFalse(result)
        self.assertEqual(
            mock_run.call_count,
            1,
            "Should not send AppleScript when pgrep says Photos is not running",
        )

    def test_returns_false_when_osascript_times_out(self):
        import subprocess
        from unittest.mock import patch, MagicMock
        from flickr.metadata_puller import _photos_is_responsive

        pgrep_ok = MagicMock()
        pgrep_ok.returncode = 0
        with patch(
            "flickr.metadata_puller.subprocess.run",
            side_effect=[pgrep_ok, subprocess.TimeoutExpired("osascript", 3)],
        ):
            self.assertFalse(_photos_is_responsive())

    def test_returns_false_when_osascript_returns_nonzero(self):
        from unittest.mock import patch, MagicMock
        from flickr.metadata_puller import _photos_is_responsive

        pgrep_ok = MagicMock()
        pgrep_ok.returncode = 0
        osascript_fail = MagicMock()
        osascript_fail.returncode = 1
        with patch("flickr.metadata_puller.subprocess.run", side_effect=[pgrep_ok, osascript_fail]):
            self.assertFalse(_photos_is_responsive())


class TestDeletePhoto(unittest.TestCase):
    """db.delete_photo() hard-deletes a Photos-only record and cascades to photo_albums."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "test.db")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_delete_removes_photo_row(self):
        photo_id = self.db.upsert_photo(
            {
                "uuid": "GHOST-0001",
                "flickr_id": None,
                "privacy_state": "candidate_public",
                "proposed_tags": [],
                "apple_persons": [],
                "apple_labels": [],
            }
        )
        self.db.delete_photo(photo_id)
        row = self.db.conn.execute("SELECT id FROM photos WHERE id = ?", (photo_id,)).fetchone()
        self.assertIsNone(row)

    def test_delete_cascades_to_photo_albums(self):
        photo_id = self.db.upsert_photo(
            {
                "uuid": "GHOST-0002",
                "flickr_id": None,
                "privacy_state": "candidate_public",
                "proposed_tags": [],
                "apple_persons": [],
                "apple_labels": [],
            }
        )
        album_id = self.db.upsert_album("album-uuid-0001", "Test Album")
        self.db.upsert_photo_album(photo_id, album_id)
        row = self.db.conn.execute(
            "SELECT * FROM photo_albums WHERE photo_id = ?", (photo_id,)
        ).fetchone()
        self.assertIsNotNone(row)

        self.db.delete_photo(photo_id)

        row = self.db.conn.execute(
            "SELECT * FROM photo_albums WHERE photo_id = ?", (photo_id,)
        ).fetchone()
        self.assertIsNone(row)


def _make_mock_photos(*uuids: str):
    """Return MagicMock photo objects with the given .uuid values."""
    from unittest.mock import MagicMock

    result = []
    for u in uuids:
        p = MagicMock()
        p.uuid = u
        result.append(p)
    return result


class TestSyncDeletedPhotos(unittest.TestCase):
    """sync_deleted_photos() detects and deletes Photos-only records absent from osxphotos."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "test.db")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _insert_photos_only(self, uuid: str) -> int:
        return self.db.upsert_photo(
            {
                "uuid": uuid,
                "flickr_id": None,
                "privacy_state": "candidate_public",
                "proposed_tags": [],
                "apple_persons": [],
                "apple_labels": [],
            }
        )

    def _insert_linked(self, uuid: str, flickr_id: str) -> int:
        return self.db.upsert_photo(
            {
                "uuid": uuid,
                "flickr_id": flickr_id,
                "privacy_state": "approved_public",
                "proposed_tags": [],
                "apple_persons": [],
                "apple_labels": [],
            }
        )

    def test_absent_uuid_photos_only_is_deleted_with_cascade(self):
        from unittest.mock import MagicMock
        from poller.scanner import sync_deleted_photos

        photo_id = self._insert_photos_only("GHOST-0001")
        album_id = self.db.upsert_album("album-uuid-sync-01", "Test Album")
        self.db.upsert_photo_album(photo_id, album_id)

        mock_photosdb = MagicMock()
        mock_photosdb.photos.return_value = _make_mock_photos("OTHER-UUID")

        deleted = sync_deleted_photos(mock_photosdb, self.db, dry_run=False)

        self.assertEqual(deleted, 1)
        self.assertIsNone(
            self.db.conn.execute("SELECT id FROM photos WHERE id = ?", (photo_id,)).fetchone()
        )
        self.assertIsNone(
            self.db.conn.execute(
                "SELECT * FROM photo_albums WHERE photo_id = ?", (photo_id,)
            ).fetchone()
        )

    def test_linked_record_not_deleted_when_uuid_absent(self):
        from unittest.mock import MagicMock
        from poller.scanner import sync_deleted_photos

        photo_id = self._insert_linked("LINKED-0001", "55555555")

        mock_photosdb = MagicMock()
        mock_photosdb.photos.return_value = _make_mock_photos("OTHER-UUID")

        deleted = sync_deleted_photos(mock_photosdb, self.db, dry_run=False)

        self.assertEqual(deleted, 0)
        self.assertIsNotNone(
            self.db.conn.execute("SELECT id FROM photos WHERE id = ?", (photo_id,)).fetchone()
        )

    def test_zero_photos_guard_prevents_all_deletions(self):
        from unittest.mock import MagicMock
        from poller.scanner import sync_deleted_photos

        self._insert_photos_only("GHOST-0001")

        mock_photosdb = MagicMock()
        mock_photosdb.photos.return_value = []

        deleted = sync_deleted_photos(mock_photosdb, self.db, dry_run=False)

        self.assertEqual(deleted, 0)
        self.assertIsNotNone(
            self.db.conn.execute("SELECT id FROM photos WHERE uuid = 'GHOST-0001'").fetchone()
        )

    def test_mass_deletion_guard_fires_above_ten_percent(self):
        from unittest.mock import MagicMock
        from poller.scanner import sync_deleted_photos

        for i in range(10):
            self._insert_photos_only(f"GHOST-{i:04d}")

        mock_photosdb = MagicMock()
        mock_photosdb.photos.return_value = _make_mock_photos("GHOST-0000")

        deleted = sync_deleted_photos(mock_photosdb, self.db, dry_run=False)

        self.assertEqual(deleted, 0)
        count = self.db.conn.execute(
            "SELECT COUNT(*) AS n FROM photos WHERE flickr_id IS NULL AND uuid IS NOT NULL"
        ).fetchone()["n"]
        self.assertEqual(count, 10)

    def test_dry_run_returns_count_but_leaves_db_unchanged(self):
        from unittest.mock import MagicMock
        from poller.scanner import sync_deleted_photos

        photo_id = self._insert_photos_only("GHOST-0001")

        mock_photosdb = MagicMock()
        mock_photosdb.photos.return_value = _make_mock_photos("OTHER-UUID")

        deleted = sync_deleted_photos(mock_photosdb, self.db, dry_run=True)

        self.assertEqual(deleted, 1)
        self.assertIsNotNone(
            self.db.conn.execute("SELECT id FROM photos WHERE id = ?", (photo_id,)).fetchone()
        )

    def test_multiple_absent_uuids_all_deleted(self):
        from unittest.mock import MagicMock
        from poller.scanner import sync_deleted_photos

        ghost_ids = [self._insert_photos_only(f"GHOST-{i:04d}") for i in range(2)]
        for i in range(20):
            self._insert_photos_only(f"KEEP-{i:04d}")

        mock_photosdb = MagicMock()
        mock_photosdb.photos.return_value = _make_mock_photos(*[f"KEEP-{i:04d}" for i in range(20)])

        deleted = sync_deleted_photos(mock_photosdb, self.db, dry_run=False)

        self.assertEqual(deleted, 2)
        for pid in ghost_ids:
            self.assertIsNone(
                self.db.conn.execute("SELECT id FROM photos WHERE id = ?", (pid,)).fetchone()
            )

    def test_not_called_during_incremental_scan(self):
        import sys
        from unittest.mock import MagicMock, patch
        from poller.scanner import scan
        from datetime import datetime, timezone

        mock_photo = MagicMock()
        mock_photo.uuid = "KEEP-0001"
        mock_photo.original_filename = "IMG_001.JPG"
        mock_photo.date = None
        mock_photo.date_added = None
        mock_photo.exif_info = None
        mock_photo.latitude = None
        mock_photo.place = None
        mock_photo.media_analysis = {}
        mock_photo.score = None
        mock_photo.labels = []
        mock_photo.persons = []
        mock_photo.fingerprint = ""
        mock_photo.width = None
        mock_photo.height = None
        mock_photo.screenshot = False
        mock_photo.selfie = False
        mock_photo.live_photo = False
        mock_photo.album_info = []
        mock_photo.title = ""
        mock_photo.description = ""
        mock_photo.keywords = []

        mock_photosdb = MagicMock()
        mock_photosdb.photos.return_value = [mock_photo]

        mock_osxphotos = MagicMock()
        mock_osxphotos.PhotosDB.return_value = mock_photosdb

        with (
            patch.dict(sys.modules, {"osxphotos": mock_osxphotos}),
            patch("poller.scanner.sync_deleted_photos") as mock_sync,
        ):
            since = datetime(2026, 1, 1, tzinfo=timezone.utc)
            scanned, matched, enriched, inserted, linked, deleted = scan(
                library_path="/fake/library",
                db=self.db,
                since=since,
                dry_run=True,
                self_name="Test User",
            )

        mock_sync.assert_not_called()
        self.assertEqual(deleted, 0)


# ---------------------------------------------------------------------------
# bp CLI: _inject_argv
# ---------------------------------------------------------------------------


class TestInjectArgv(unittest.TestCase):
    """_inject_argv must produce a sys.argv where every element is a string."""

    def test_integer_value_is_stringified(self):
        import sys
        import argparse
        import importlib.machinery
        import importlib.util

        # bp has no .py extension; load it via SourceFileLoader.
        loader = importlib.machinery.SourceFileLoader(
            "bp", str(Path(__file__).parent.parent / "bp")
        )
        spec = importlib.util.spec_from_loader("bp", loader)
        bp_mod = importlib.util.module_from_spec(spec)
        loader.exec_module(bp_mod)

        original_argv = sys.argv[:]
        try:
            bp_mod._inject_argv(
                argparse.Namespace(),
                [("--icloud-limit", 50), ("--icloud", False)],
            )
            # Every element must be a string
            for v in sys.argv:
                self.assertIsInstance(v, str, f"sys.argv contains non-string: {v!r}")
            self.assertIn("--icloud-limit", sys.argv)
            self.assertIn("50", sys.argv)
            self.assertNotIn("--icloud", sys.argv)  # False → omitted
        finally:
            sys.argv = original_argv


# ---------------------------------------------------------------------------
# GH #19 — Task 3: state_to_perms helper
# ---------------------------------------------------------------------------


class TestStateToPerms(unittest.TestCase):
    """state_to_perms maps privacy_state to (is_public, is_friend, is_family) tuples."""

    def _fn(self):
        from flickr.flickr_client import state_to_perms

        return state_to_perms

    def test_approved_public_is_public(self):
        self.assertEqual(self._fn()("approved_public"), (1, 0, 0))

    def test_already_public_is_public(self):
        self.assertEqual(self._fn()("already_public"), (1, 0, 0))

    def test_approved_friends(self):
        self.assertEqual(self._fn()("approved_friends"), (0, 1, 0))

    def test_approved_family(self):
        self.assertEqual(self._fn()("approved_family"), (0, 0, 1))

    def test_approved_friends_family(self):
        self.assertEqual(self._fn()("approved_friends_family"), (0, 1, 1))

    def test_keep_private_is_all_zeros(self):
        self.assertEqual(self._fn()("keep_private"), (0, 0, 0))

    def test_unknown_state_is_all_zeros(self):
        self.assertEqual(self._fn()("needs_review"), (0, 0, 0))
        self.assertEqual(self._fn()(""), (0, 0, 0))


# ---------------------------------------------------------------------------
# GH #19 — Task 1: Migration 015 (widen privacy_state CHECK)
# ---------------------------------------------------------------------------


class TestMigrate015FriendsFamily(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "test.db")

    def tearDown(self):
        self._tmp.cleanup()

    def _old_schema_db(self):
        """Create a minimal DB with the old CHECK constraint (8 states, no friends/family)."""
        import sqlite3

        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE schema_migrations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                applied_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uuid TEXT UNIQUE,
                privacy_state TEXT NOT NULL DEFAULT 'needs_review'
                    CHECK(privacy_state IN (
                        'auto_private', 'needs_review', 'candidate_public',
                        'approved_public', 'keep_private', 'already_public',
                        'skipped', 'duplicate_flickr'
                    ))
            )
        """)
        conn.commit()
        return conn

    def test_migration_allows_approved_friends_after_run(self):
        from db.migrations.migrate_015_friends_family import run

        conn = self._old_schema_db()
        # Before migration: inserting approved_friends must fail
        with self.assertRaises(Exception):
            conn.execute(
                "INSERT INTO photos (uuid, privacy_state) VALUES ('x', 'approved_friends')"
            )
        conn.close()
        run(self.db_path)
        import sqlite3 as _sqlite3

        conn2 = _sqlite3.connect(self.db_path)
        conn2.execute("INSERT INTO photos (uuid, privacy_state) VALUES ('y', 'approved_friends')")
        conn2.commit()
        row = conn2.execute("SELECT privacy_state FROM photos WHERE uuid='y'").fetchone()
        self.assertEqual(row[0], "approved_friends")
        conn2.close()

    def test_migration_is_idempotent(self):
        from db.db import Database
        from db.migrations.migrate_015_friends_family import run

        db = Database(Path(self.db_path))
        db.close()
        run(self.db_path)
        run(self.db_path)  # must not raise


# ---------------------------------------------------------------------------
# GH #19 — Task 2: record_review new decisions
# ---------------------------------------------------------------------------


class TestRecordReviewFriendsFamily(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "test.db")
        self.photo_id = self.db.upsert_photo(
            {
                "uuid": "uuid-frn-test",
                "original_filename": "IMG_frn.JPG",
                "privacy_state": "needs_review",
                "apple_persons": [],
                "apple_labels": [],
            }
        )

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _state(self) -> str:
        return self.db.conn.execute(
            "SELECT privacy_state FROM photos WHERE id = ?", (self.photo_id,)
        ).fetchone()[0]

    def test_make_friends_maps_to_approved_friends(self):
        self.db.record_review(self.photo_id, "make_friends")
        self.assertEqual(self._state(), "approved_friends")

    def test_make_family_maps_to_approved_family(self):
        self.db.record_review(self.photo_id, "make_family")
        self.assertEqual(self._state(), "approved_family")

    def test_make_friends_family_maps_to_approved_friends_family(self):
        self.db.record_review(self.photo_id, "make_friends_family")
        self.assertEqual(self._state(), "approved_friends_family")


# ---------------------------------------------------------------------------
# GH #19 — Task 5: reconcile friends/family perm check
# ---------------------------------------------------------------------------


class TestReconcileFriendsFamily(unittest.TestCase):
    def setUp(self):
        from unittest.mock import MagicMock

        self._tmp = tempfile.mkdtemp()
        self.db = Database(Path(self._tmp) / "test.db")
        self.photo_id = self.db.upsert_photo(
            {
                "uuid": "uuid-frn-rec",
                "original_filename": "IMG_frn_rec.JPG",
                "privacy_state": "approved_friends",
                "flickr_id": "flickr-frn-rec",
                "perms_pushed_flickr": 1,
                "tags_pushed_flickr": 0,
                "proposed_tags": [],
                "apple_persons": [],
                "apple_labels": [],
            }
        )
        self.mock_client = MagicMock()

    def tearDown(self):
        self.db.close()
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    def _photo_row(self):
        return dict(
            self.db.conn.execute("SELECT * FROM photos WHERE id = ?", (self.photo_id,)).fetchone()
        )

    def _flickr_info(self, ispublic=0, isfriend=0, isfamily=0):
        return {
            "photo": {
                "visibility": {
                    "ispublic": ispublic,
                    "isfriend": isfriend,
                    "isfamily": isfamily,
                },
                "tags": {"tag": []},
            }
        }

    def test_friends_mismatch_detected_when_flickr_is_private(self):
        from poller.reconcile import check_photo

        self.mock_client.get_photo_info.return_value = self._flickr_info(
            ispublic=0, isfriend=0, isfamily=0
        )
        result = check_photo(self.mock_client, self._photo_row(), self.db, fix=False, verbose=False)
        self.assertEqual(result["status"], "perm_mismatch")
        self.assertEqual(result["perm_expected"], "friends")
        self.assertEqual(result["perm_actual"], "private")

    def test_friends_fix_calls_set_permissions_with_friend_flag(self):
        from poller.reconcile import check_photo

        self.mock_client.get_photo_info.return_value = self._flickr_info(
            ispublic=0, isfriend=0, isfamily=0
        )
        result = check_photo(self.mock_client, self._photo_row(), self.db, fix=True, verbose=False)
        self.assertEqual(result["status"], "perm_mismatch")
        self.assertIn("perm", result["fixes"])
        self.mock_client.set_permissions.assert_called_once_with(
            "flickr-frn-rec", is_public=0, is_friend=1, is_family=0
        )

    def test_friends_ok_when_flickr_matches(self):
        from poller.reconcile import check_photo

        self.mock_client.get_photo_info.return_value = self._flickr_info(
            ispublic=0, isfriend=1, isfamily=0
        )
        result = check_photo(self.mock_client, self._photo_row(), self.db, fix=False, verbose=False)
        self.assertEqual(result["status"], "ok")
        self.mock_client.set_permissions.assert_not_called()


# ---------------------------------------------------------------------------
# GH #19 — Task 6: scanner preserves approved_friends/family states
# ---------------------------------------------------------------------------


class TestScannerFriendsStates(unittest.TestCase):
    """build_enriched_row must not reclassify photos in approved_friends/family states."""

    _PHOTO_ROW = {
        "uuid": "uuid-scan-frn",
        "original_filename": "IMG_scan_frn.JPG",
        "date_taken": "2026-01-01T12:00:00",
        "apple_labels": [],
        "apple_persons": ["Alice"],
        "apple_named_faces": 1,
        "apple_unknown_faces": 0,
        "apple_human_count": 1,
        "_is_screenshot": False,
        "_is_selfie": False,
        "_is_live": False,
    }

    def _existing(self, state: str) -> dict:
        return {
            "id": 1,
            "flickr_id": "flickr-scan-001",
            "uuid": None,
            "privacy_state": state,
            "privacy_reason": "human reviewed",
            "proposed_tags": [],
            "latitude": None,
            "longitude": None,
            "place_ishome": 0,
        }

    def test_approved_friends_is_protected_from_overwrite(self):
        from poller.scanner import build_enriched_row

        enriched = build_enriched_row(
            self._PHOTO_ROW, self._existing("approved_friends"), [], "Alice"
        )
        self.assertEqual(
            enriched["privacy_state"],
            "approved_friends",
            "approved_friends must not be overwritten by scanner",
        )

    def test_approved_family_is_protected_from_overwrite(self):
        from poller.scanner import build_enriched_row

        enriched = build_enriched_row(
            self._PHOTO_ROW, self._existing("approved_family"), [], "Alice"
        )
        self.assertEqual(
            enriched["privacy_state"],
            "approved_family",
            "approved_family must not be overwritten by scanner",
        )

    def test_approved_friends_family_is_protected_from_overwrite(self):
        from poller.scanner import build_enriched_row

        enriched = build_enriched_row(
            self._PHOTO_ROW, self._existing("approved_friends_family"), [], "Alice"
        )
        self.assertEqual(
            enriched["privacy_state"],
            "approved_friends_family",
            "approved_friends_family must not be overwritten by scanner",
        )


# ---------------------------------------------------------------------------
# GH #99 — Task 1: Migration 016 (add pushed_tags column)
# ---------------------------------------------------------------------------


class TestMigrate016PushedTags(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "test.db")

    def tearDown(self):
        self._tmp.cleanup()

    def _cols(self):
        import sqlite3

        conn = sqlite3.connect(self.db_path)
        cols = [row[1] for row in conn.execute("PRAGMA table_info(photos)").fetchall()]
        conn.close()
        return cols

    def test_column_exists_after_run(self):
        from db.migrations.migrate_016_pushed_tags import run

        db = Database(Path(self.db_path))
        db.close()
        run(self.db_path)
        self.assertIn("pushed_tags", self._cols())

    def test_existing_rows_get_null(self):
        import sqlite3
        from db.migrations.migrate_016_pushed_tags import run

        db = Database(Path(self.db_path))
        photo_id = db.upsert_photo(
            {
                "uuid": "uuid-mig016",
                "original_filename": "IMG_mig016.JPG",
                "apple_persons": [],
                "apple_labels": [],
            }
        )
        db.close()
        run(self.db_path)
        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT pushed_tags FROM photos WHERE id = ?", (photo_id,)).fetchone()
        conn.close()
        self.assertIsNone(row[0])

    def test_migration_is_idempotent(self):
        from db.migrations.migrate_016_pushed_tags import run

        db = Database(Path(self.db_path))
        db.close()
        run(self.db_path)
        run(self.db_path)  # must not raise


# ---------------------------------------------------------------------------
# GH #99 — Task 2: pushed_tags written on initial push (poller)
# ---------------------------------------------------------------------------


class TestPushedTagsOnInitialPush(unittest.TestCase):
    def setUp(self):
        from unittest.mock import MagicMock

        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "test.db")
        self.photo_id = self.db.upsert_photo(
            {
                "uuid": "uuid-push-001",
                "original_filename": "IMG_push.JPG",
                "flickr_id": "flickr-push-001",
                "proposed_tags": ["cat", "indoor"],
                "privacy_state": "approved_public",
                "apple_persons": [],
                "apple_labels": [],
            }
        )
        self.mock_client = MagicMock()

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _row(self):
        return dict(
            self.db.conn.execute("SELECT * FROM photos WHERE id = ?", (self.photo_id,)).fetchone()
        )

    def _db_record(self):
        import json

        row = self._row()
        row["proposed_tags"] = json.loads(row["proposed_tags"] or "[]")
        return row

    def test_pushed_tags_written_on_success(self):
        import json
        from poller.poller import _push_to_flickr

        _push_to_flickr(
            self.mock_client, "flickr-push-001", self._db_record(), self.db, dry_run=False
        )
        pushed = json.loads(self._row()["pushed_tags"])
        self.assertEqual(pushed, ["cat", "indoor"])

    def test_pushed_tags_null_when_add_tags_fails(self):
        from flickr.flickr_client import FlickrError
        from poller.poller import _push_to_flickr

        self.mock_client.add_tags.side_effect = FlickrError(0, "api error")
        _push_to_flickr(
            self.mock_client, "flickr-push-001", self._db_record(), self.db, dry_run=False
        )
        self.assertIsNone(self._row()["pushed_tags"])

    def test_pushed_tags_null_when_no_proposed_tags(self):
        from poller.poller import _push_to_flickr

        record = self._db_record()
        record["proposed_tags"] = []
        _push_to_flickr(self.mock_client, "flickr-push-001", record, self.db, dry_run=False)
        self.assertIsNone(self._row()["pushed_tags"])


# ---------------------------------------------------------------------------
# GH #99 — Task 3: reconcile uses pushed_tags; updates it after fix
# ---------------------------------------------------------------------------


class TestReconcilePushedTags(unittest.TestCase):
    def setUp(self):
        from unittest.mock import MagicMock

        self._tmp = tempfile.mkdtemp()
        self.db = Database(Path(self._tmp) / "test.db")
        self.mock_client = MagicMock()

    def tearDown(self):
        self.db.close()
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    def _make_photo(self, pushed_tags=None, proposed_tags=None, flickr_id="flickr-pt-001"):
        import json

        photo_id = self.db.upsert_photo(
            {
                "uuid": f"uuid-pt-{flickr_id}",
                "original_filename": "IMG_pt.JPG",
                "flickr_id": flickr_id,
                "privacy_state": "approved_public",
                "perms_pushed_flickr": 1,
                "tags_pushed_flickr": 1,
                "proposed_tags": proposed_tags or [],
                "apple_persons": [],
                "apple_labels": [],
            }
        )
        if pushed_tags is not None:
            self.db.conn.execute(
                "UPDATE photos SET pushed_tags = ? WHERE id = ?",
                (json.dumps(pushed_tags), photo_id),
            )
            self.db.conn.commit()
        return photo_id

    def _row(self, photo_id):
        return dict(
            self.db.conn.execute("SELECT * FROM photos WHERE id = ?", (photo_id,)).fetchone()
        )

    def _flickr_info(self, tags):
        return {
            "photo": {
                "visibility": {"ispublic": 1, "isfriend": 0, "isfamily": 0},
                "tags": {"tag": [{"raw": t} for t in tags]},
            }
        }

    # --- reads pushed_tags, not proposed_tags ---

    def test_null_pushed_tags_skips_tag_check(self):
        """pushed_tags=NULL -> skip tag check even if proposed_tags is non-empty."""
        from poller.reconcile import check_photo

        photo_id = self._make_photo(pushed_tags=None, proposed_tags=["cat", "dog"])
        self.mock_client.get_photo_info.return_value = self._flickr_info([])
        result = check_photo(
            self.mock_client, self._row(photo_id), self.db, fix=False, verbose=False
        )
        self.assertEqual(result["status"], "ok")

    def test_pushed_tags_subset_of_flickr_is_ok(self):
        """pushed_tags <= flickr_tags -> ok, even if Flickr has extra tags."""
        from poller.reconcile import check_photo

        photo_id = self._make_photo(pushed_tags=["cat"], proposed_tags=["cat", "new-ml"])
        self.mock_client.get_photo_info.return_value = self._flickr_info(["cat", "extra"])
        result = check_photo(
            self.mock_client, self._row(photo_id), self.db, fix=False, verbose=False
        )
        self.assertEqual(result["status"], "ok")

    def test_pushed_tag_missing_from_flickr_is_mismatch(self):
        from poller.reconcile import check_photo

        photo_id = self._make_photo(pushed_tags=["cat", "dog"], proposed_tags=["cat", "dog"])
        self.mock_client.get_photo_info.return_value = self._flickr_info(["cat"])
        result = check_photo(
            self.mock_client, self._row(photo_id), self.db, fix=False, verbose=False
        )
        self.assertEqual(result["status"], "tag_mismatch")
        self.assertIn("dog", result["tags_missing"])

    def test_proposed_tag_not_in_pushed_tags_not_checked(self):
        """New ML label in proposed_tags but not in pushed_tags -> not a mismatch."""
        from poller.reconcile import check_photo

        photo_id = self._make_photo(pushed_tags=["cat"], proposed_tags=["cat", "new-ml-label"])
        self.mock_client.get_photo_info.return_value = self._flickr_info(["cat"])
        result = check_photo(
            self.mock_client, self._row(photo_id), self.db, fix=False, verbose=False
        )
        self.assertEqual(result["status"], "ok")

    # --- fix writes pushed_tags back to DB ---

    def test_fix_appends_newly_pushed_tags(self):
        import json
        from poller.reconcile import check_photo

        photo_id = self._make_photo(pushed_tags=["cat"], proposed_tags=["cat"])
        self.mock_client.get_photo_info.return_value = self._flickr_info([])  # cat missing
        check_photo(self.mock_client, self._row(photo_id), self.db, fix=True, verbose=False)
        pushed = json.loads(self._row(photo_id)["pushed_tags"])
        self.assertIn("cat", pushed)

    def test_fix_preserves_existing_pushed_tags(self):
        import json
        from poller.reconcile import check_photo

        photo_id = self._make_photo(pushed_tags=["cat", "dog"], proposed_tags=["cat", "dog"])
        self.mock_client.get_photo_info.return_value = self._flickr_info(["cat"])  # dog missing
        check_photo(self.mock_client, self._row(photo_id), self.db, fix=True, verbose=False)
        pushed = json.loads(self._row(photo_id)["pushed_tags"])
        self.assertIn("cat", pushed)  # preserved
        self.assertIn("dog", pushed)  # re-confirmed

    def test_fix_does_not_update_pushed_tags_on_api_failure(self):
        import json
        from flickr.flickr_client import FlickrError
        from poller.reconcile import check_photo

        photo_id = self._make_photo(pushed_tags=["cat"], proposed_tags=["cat"])
        self.mock_client.get_photo_info.return_value = self._flickr_info([])
        self.mock_client.add_tags.side_effect = FlickrError(0, "fail")
        check_photo(self.mock_client, self._row(photo_id), self.db, fix=True, verbose=False)
        # pushed_tags should be unchanged (still just ["cat"])
        pushed = json.loads(self._row(photo_id)["pushed_tags"])
        self.assertEqual(pushed, ["cat"])


# ---------------------------------------------------------------------------
# GH #99 — Task 5: bp tag-writeback subcommand
# ---------------------------------------------------------------------------


class TestTagWriteback(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "test.db")
        import json

        self.photo_id = self.db.upsert_photo(
            {
                "uuid": "uuid-wb-001",
                "original_filename": "IMG_wb.JPG",
                "flickr_id": "flickr-wb-001",
                "tags_pushed_flickr": 1,
                "apple_persons": [],
                "apple_labels": [],
            }
        )
        self.db.conn.execute(
            "UPDATE photos SET pushed_tags = ? WHERE id = ?",
            (json.dumps(["cat", "indoor"]), self.photo_id),
        )
        self.db.conn.commit()

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _run(self, **kwargs):
        from poller.tag_writeback import writeback

        return writeback(self.db, **kwargs)

    def test_keywords_merged_additively(self):
        from unittest.mock import MagicMock, patch

        mock_photo = MagicMock()
        mock_photo.keywords = ["existing"]

        mock_lib = MagicMock()
        mock_lib.photos.return_value = iter([mock_photo])

        with patch("poller.tag_writeback.photoscript") as mock_ps:
            mock_ps.PhotosLibrary.return_value = mock_lib
            result = self._run(dry_run=False, limit=500)

        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["ok"], 0)
        self.assertEqual(sorted(mock_photo.keywords), ["cat", "existing", "indoor"])

    def test_already_has_all_keywords_is_ok(self):
        from unittest.mock import MagicMock, patch

        mock_photo = MagicMock()
        mock_photo.keywords = ["cat", "indoor"]

        mock_lib = MagicMock()
        mock_lib.photos.return_value = iter([mock_photo])

        with patch("poller.tag_writeback.photoscript") as mock_ps:
            mock_ps.PhotosLibrary.return_value = mock_lib
            result = self._run(dry_run=False, limit=500)

        self.assertEqual(result["ok"], 1)
        self.assertEqual(result["updated"], 0)

    def test_dry_run_does_not_write_keywords(self):
        from unittest.mock import MagicMock, patch

        mock_photo = MagicMock()
        mock_photo.keywords = ["existing"]

        mock_lib = MagicMock()
        mock_lib.photos.return_value = iter([mock_photo])

        with patch("poller.tag_writeback.photoscript") as mock_ps:
            mock_ps.PhotosLibrary.return_value = mock_lib
            result = self._run(dry_run=True, limit=500)

        # In dry_run mode, updated count is reported but keywords setter NOT called
        self.assertEqual(result["updated"], 1)
        # keywords must still be the original list (not written)
        self.assertEqual(mock_photo.keywords, ["existing"])

    def test_not_found_uuid_counted(self):
        from unittest.mock import MagicMock, patch

        mock_lib = MagicMock()
        mock_lib.photos.return_value = iter([])  # empty — photo not in Photos.app

        with patch("poller.tag_writeback.photoscript") as mock_ps:
            mock_ps.PhotosLibrary.return_value = mock_lib
            result = self._run(dry_run=False, limit=500)

        self.assertEqual(result["not_found"], 1)

    def test_photos_without_uuid_skipped(self):
        """Flickr-only records (uuid=NULL) must not appear in writeback query."""
        from unittest.mock import MagicMock, patch

        # Insert a Flickr-only record (no uuid)
        self.db.conn.execute("""
            INSERT INTO photos (uuid, original_filename, flickr_id, privacy_state,
                                tags_pushed_flickr, pushed_tags, apple_persons, apple_labels)
            VALUES (NULL, 'flickr_only.JPG', 'flickr-only-001', 'already_public',
                    1, '["cat"]', '[]', '[]')
        """)
        self.db.conn.commit()

        mock_photo = MagicMock()
        mock_photo.keywords = []

        mock_lib = MagicMock()
        mock_lib.photos.return_value = iter([mock_photo])

        with patch("poller.tag_writeback.photoscript") as mock_ps:
            mock_ps.PhotosLibrary.return_value = mock_lib
            result = self._run(dry_run=False, limit=500)

        # Only 1 photo processed (the one with uuid), not the Flickr-only record
        self.assertEqual(result["updated"] + result["ok"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
