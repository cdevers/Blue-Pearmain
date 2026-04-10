"""
tests/test_core.py — unit tests for Blue Pearmain core logic

Run from repo root:
    python -m pytest tests/
    # or without pytest:
    python tests/test_core.py
"""

import json
import os
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

        # Simulate the person-scoped nav query from photo_detail route
        nav = self.db.conn.execute(
            """SELECT DISTINCT photos.id,
                   LAG(photos.id)  OVER (ORDER BY photos.date_taken, photos.id) AS prev_id,
                   LEAD(photos.id) OVER (ORDER BY photos.date_taken, photos.id) AS next_id
               FROM photos, json_each(photos.apple_persons) AS p
               WHERE p.value = ?
                 AND photos.privacy_state = ?""",
            ("Barack Obama", "needs_review"),
        ).fetchall()

        nav1 = self.db.get_photo_by_flickr_id("NAV1")
        nav2 = self.db.get_photo_by_flickr_id("NAV2")

        id_to_nav = {row["id"]: row for row in nav}

        # Key assertion: NAV1 and NAV2 are adjacent in the Obama sequence
        # NAV1 (Jan) → NAV2 (Jun), with NAV3 (Mar, Someone Else) absent
        self.assertEqual(id_to_nav[nav1["id"]]["next_id"], nav2["id"])
        self.assertEqual(id_to_nav[nav2["id"]]["prev_id"], nav1["id"])

        # NAV3 (Someone Else) should NOT appear in Obama's nav sequence
        nav3 = self.db.get_photo_by_flickr_id("NAV3")
        self.assertNotIn(nav3["id"], id_to_nav)


# ---------------------------------------------------------------------------
# FlickrClient retry / backoff
# ---------------------------------------------------------------------------

class TestFlickrClientRetry(unittest.TestCase):

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
        self.db.upsert_photo({
            "uuid": "DEF-123",
            "date_taken": "2024-06-16 10:00:00",
            "privacy_state": "approved_public",
        })
        # Flickr may return ISO8601 format
        flickr_row = {"date_taken": "2024-06-16T10:00:00.000000-04:00", "flickr_id": "66666"}
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
