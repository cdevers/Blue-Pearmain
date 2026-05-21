"""
tests/test_explain.py — unit tests for poller.explain

Run from repo root:
    python -m pytest tests/test_explain.py -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from poller.explain import explain_photo_tags, explain_photo_perms


def _row(**kw) -> dict:
    """Return a minimal photo row with required fields, with optional overrides."""
    base: dict = {
        "id": 42,
        "flickr_id": "99900001",
        "flickr_title": "Test Photo",
        "flickr_tags": '["beach", "family"]',
        "photos_tags": '["beach", "family"]',
        "pushed_tags": '["beach", "family"]',
        "privacy_state": "approved_public",
        "review_decision": "make_public",
        "reviewed_at": "2025-03-14T14:22:01",
        "perms_pushed_flickr": 1,
        "tags_pushed_flickr": 1,
    }
    base.update(kw)
    return base


class TestExplainPhotoTags(unittest.TestCase):
    def test_returns_none_when_tags_match(self):
        # flickr_tags == photos_tags — no drift to explain
        result = explain_photo_tags(_row())
        self.assertIsNone(result)

    def test_returns_dict_when_photos_has_extra_tag(self):
        # Photos has scanned-film; Flickr does not
        result = explain_photo_tags(
            _row(
                flickr_tags='["beach"]',
                photos_tags='["beach", "scanned-film"]',
            )
        )
        self.assertIsNotNone(result)

    def test_explains_tags_in_photos_not_on_flickr(self):
        result = explain_photo_tags(
            _row(
                flickr_tags='["beach"]',
                photos_tags='["beach", "scanned-film"]',
            )
        )
        self.assertIn("last_known_flickr", result)
        self.assertIn("desired", result)
        self.assertIn("reason_codes", result)
        self.assertIn("reason", result)
        self.assertIn("scanned-film", result["reason"])
        self.assertIn("missing_remote_tag", result["reason_codes"])

    def test_returns_none_when_flickr_tags_is_null_and_photos_tags_empty(self):
        result = explain_photo_tags(_row(flickr_tags=None, photos_tags=None, pushed_tags=None))
        self.assertIsNone(result)

    def test_reports_pushed_tags_that_disappeared_from_flickr(self):
        # We pushed "archive" but it is no longer in Flickr cache
        result = explain_photo_tags(
            _row(
                flickr_tags='["beach"]',
                photos_tags='["beach"]',
                pushed_tags='["beach", "archive"]',
            )
        )
        self.assertIsNotNone(result)
        self.assertIn("archive", result["reason"])

    def test_last_known_flickr_is_sorted_list(self):
        result = explain_photo_tags(
            _row(
                flickr_tags='["family", "beach"]',
                photos_tags='["beach", "family", "scanned-film"]',
            )
        )
        self.assertEqual(result["last_known_flickr"], ["beach", "family"])

    def test_desired_is_sorted_list(self):
        result = explain_photo_tags(
            _row(
                flickr_tags='["beach"]',
                photos_tags='["scanned-film", "beach"]',
            )
        )
        self.assertEqual(result["desired"], ["beach", "scanned-film"])


class TestExplainPhotoPerms(unittest.TestCase):
    def test_returns_none_when_perms_pushed_and_state_unchanged(self):
        # Pushed approved_public, push confirmed
        result = explain_photo_perms(
            _row(
                privacy_state="approved_public",
                perms_pushed_flickr=1,
                review_decision="make_public",
            )
        )
        self.assertIsNone(result)

    def test_returns_dict_when_perms_not_yet_pushed(self):
        result = explain_photo_perms(
            _row(
                privacy_state="approved_public",
                perms_pushed_flickr=0,
                review_decision="make_public",
            )
        )
        self.assertIsNotNone(result)

    def test_explains_unpushed_perms(self):
        result = explain_photo_perms(
            _row(
                privacy_state="approved_public",
                perms_pushed_flickr=0,
                review_decision="make_public",
            )
        )
        self.assertIn("desired", result)
        self.assertIn("reason", result)
        self.assertIn("not yet pushed", result["reason"])

    def test_returns_none_when_no_review_decision(self):
        # No decision yet — nothing to explain for perms
        result = explain_photo_perms(
            _row(
                privacy_state="needs_review",
                perms_pushed_flickr=0,
                review_decision=None,
            )
        )
        self.assertIsNone(result)

    def test_friends_only_state_labelled_correctly(self):
        result = explain_photo_perms(
            _row(
                privacy_state="approved_friends",
                perms_pushed_flickr=0,
                review_decision="make_friends",
            )
        )
        self.assertIn("friends-only", result["desired"])
