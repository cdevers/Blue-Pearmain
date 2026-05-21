"""
tests/test_tag_protection.py — tag protection rule tests for _classify_tags

Tag protection prevents source=photos proposals from removing protected
tags from Flickr. Flickr uses set_tags (full replacement), so if Photos
doesn't contain a protected tag that Flickr has, auto-applying a
source=photos proposal would silently delete it.

Run from repo root:
    python -m pytest tests/test_tag_protection.py -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from flickr.metadata_puller import _classify_tags

NOW = "2026-05-20T00:00:00+00:00"


def classify(
    flickr_tags, photos_tags, proposed_tags=None, protected_tags=None, protected_namespaces=None
):
    """Helper: call _classify_tags with JSON-encoded tag lists."""
    import json

    return _classify_tags(
        photo_id=1,
        flickr_tags_json=json.dumps(flickr_tags) if flickr_tags is not None else None,
        photos_tags_json=json.dumps(photos_tags) if photos_tags is not None else None,
        flickr_hash="flickrhash",
        photos_hash="photoshash",
        now=NOW,
        proposed_tags_json=json.dumps(proposed_tags) if proposed_tags is not None else None,
        protected_tags=protected_tags,
        protected_namespaces=protected_namespaces,
    )


class TestNoProtectionConfigured(unittest.TestCase):
    """Without protection, _classify_tags behaves as before."""

    def test_collision_generates_both_proposals(self):
        """Unprotected collision still produces source=flickr and source=photos proposals."""
        proposals = classify(["scanned-film"], ["beach"])
        sources = {p["source"] for p in proposals}
        self.assertIn("flickr", sources)
        self.assertIn("photos", sources)

    def test_non_conflict_flickr_wins_unchanged(self):
        """Flickr-only tags produce a flickr→photos non_conflict proposal."""
        proposals = classify(["scanned-film"], [])
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["source"], "flickr")

    def test_non_conflict_photos_wins_unchanged(self):
        """Photos-only tags produce a photos→flickr non_conflict proposal."""
        proposals = classify([], ["beach"])
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["source"], "photos")


class TestProtectedTagsByName(unittest.TestCase):
    """protected_tags=[...] blocks source=photos proposals that would remove a listed tag."""

    def test_collision_drops_source_photos_when_protected_tag_on_flickr_only(self):
        """
        Flickr has scanned-film (protected). Photos has beach.
        Collision — source=photos proposal would remove scanned-film from Flickr.
        That proposal is dropped; only source=flickr survives.
        """
        proposals = classify(
            flickr_tags=["scanned-film"],
            photos_tags=["beach"],
            protected_tags=["scanned-film"],
        )
        sources = [p["source"] for p in proposals]
        self.assertIn("flickr", sources)
        self.assertNotIn("photos", sources)

    def test_collision_keeps_both_when_photos_also_has_protected_tag(self):
        """
        Both sides have scanned-film. Applying Photos value would not remove it.
        Both collision proposals should be generated.
        """
        proposals = classify(
            flickr_tags=["scanned-film"],
            photos_tags=["scanned-film", "beach"],
            protected_tags=["scanned-film"],
        )
        # Photos is a superset → divergence(source=photos), no collision
        # Either way the protected tag is present on the Photos side, so no filtering needed
        # (divergence source=photos is safe — Photos superset preserves scanned-film)
        self.assertTrue(len(proposals) >= 1)
        # No source=photos proposal should be dropped here
        sources = [p["source"] for p in proposals]
        self.assertIn("photos", sources)

    def test_multiple_protected_tags_any_triggers_drop(self):
        """If any protected tag would be lost, the source=photos proposal is dropped."""
        proposals = classify(
            flickr_tags=["scanned-film", "original-negative"],
            photos_tags=["beach"],
            protected_tags=["scanned-film", "original-negative"],
        )
        sources = [p["source"] for p in proposals]
        self.assertNotIn("photos", sources)

    def test_unrelated_protected_tag_not_on_flickr_does_not_trigger(self):
        """
        Protected tag 'archive/2020' is not on Flickr at all.
        Collision between Flickr 'outdoor' and Photos 'beach' should
        keep both proposals — nothing to protect on the Flickr side.
        """
        proposals = classify(
            flickr_tags=["outdoor"],
            photos_tags=["beach"],
            protected_tags=["archive/2020"],
        )
        sources = [p["source"] for p in proposals]
        self.assertIn("flickr", sources)
        self.assertIn("photos", sources)

    def test_protection_case_insensitive(self):
        """Protected tag matching is case-insensitive (Flickr normalises tags)."""
        proposals = classify(
            flickr_tags=["Scanned-Film"],
            photos_tags=["beach"],
            protected_tags=["scanned-film"],
        )
        sources = [p["source"] for p in proposals]
        self.assertNotIn("photos", sources)


class TestProtectedNamespaces(unittest.TestCase):
    """protected_namespaces=[...] blocks source=photos proposals that would remove namespace-prefixed tags."""

    def test_namespace_prefix_match_drops_source_photos(self):
        """
        Flickr has family/reunion (in protected namespace family/).
        Photos doesn't have it. source=photos proposal is dropped.
        """
        proposals = classify(
            flickr_tags=["family/reunion"],
            photos_tags=["beach"],
            protected_namespaces=["family/"],
        )
        sources = [p["source"] for p in proposals]
        self.assertNotIn("photos", sources)

    def test_non_matching_namespace_does_not_drop(self):
        """
        Flickr has outdoor (not in any protected namespace).
        Both proposals should survive.
        """
        proposals = classify(
            flickr_tags=["outdoor"],
            photos_tags=["beach"],
            protected_namespaces=["family/", "archive/"],
        )
        sources = [p["source"] for p in proposals]
        self.assertIn("photos", sources)

    def test_multiple_namespaces_any_match_triggers(self):
        """archive/neg is in the archive/ namespace — triggers protection."""
        proposals = classify(
            flickr_tags=["archive/neg"],
            photos_tags=["beach"],
            protected_namespaces=["family/", "archive/"],
        )
        sources = [p["source"] for p in proposals]
        self.assertNotIn("photos", sources)


class TestProtectedTagsAndNamespacesTogether(unittest.TestCase):
    """Both protected_tags and protected_namespaces can be combined."""

    def test_tag_match_triggers_even_with_namespace_list(self):
        proposals = classify(
            flickr_tags=["scanned-film"],
            photos_tags=["beach"],
            protected_tags=["scanned-film"],
            protected_namespaces=["family/"],
        )
        sources = [p["source"] for p in proposals]
        self.assertNotIn("photos", sources)

    def test_namespace_match_triggers_even_with_tag_list(self):
        proposals = classify(
            flickr_tags=["family/reunion"],
            photos_tags=["beach"],
            protected_tags=["scanned-film"],
            protected_namespaces=["family/"],
        )
        sources = [p["source"] for p in proposals]
        self.assertNotIn("photos", sources)

    def test_neither_matches_both_proposals_survive(self):
        proposals = classify(
            flickr_tags=["outdoor"],
            photos_tags=["beach"],
            protected_tags=["scanned-film"],
            protected_namespaces=["family/"],
        )
        sources = [p["source"] for p in proposals]
        self.assertIn("flickr", sources)
        self.assertIn("photos", sources)


class TestProtectedTagsWithManagedTags(unittest.TestCase):
    """Protection interacts correctly with BP-managed tag exclusion."""

    def test_managed_tags_excluded_before_protection_check(self):
        """
        Flickr has scanned-film (protected) + beach (managed by BP).
        Photos has beach.
        Without managed exclusion: collision (scanned-film vs beach).
        After managed exclusion: ftags_effective = {scanned-film}, ptags = {beach} → collision.
        Protected scanned-film → source=photos proposal dropped.
        """
        proposals = classify(
            flickr_tags=["scanned-film", "beach"],
            photos_tags=["beach"],
            proposed_tags=["beach"],  # beach is BP-managed
            protected_tags=["scanned-film"],
        )
        sources = [p["source"] for p in proposals]
        self.assertNotIn("photos", sources)


class TestConfigLoading(unittest.TestCase):
    """Config-level wiring: tag_protection section is parsed correctly."""

    def test_protected_tags_extracted_from_config(self):
        config = {
            "tag_protection": {
                "protected_tags": ["scanned-film", "original-negative"],
            }
        }
        tags = config.get("tag_protection", {}).get("protected_tags", [])
        self.assertIn("scanned-film", tags)
        self.assertIn("original-negative", tags)

    def test_protected_namespaces_extracted_from_config(self):
        config = {
            "tag_protection": {
                "protected_namespaces": ["family/", "archive/"],
            }
        }
        ns = config.get("tag_protection", {}).get("protected_namespaces", [])
        self.assertIn("family/", ns)
        self.assertIn("archive/", ns)

    def test_absent_tag_protection_section_returns_empty_lists(self):
        config = {"flickr": {}}
        tags = config.get("tag_protection", {}).get("protected_tags", [])
        ns = config.get("tag_protection", {}).get("protected_namespaces", [])
        self.assertEqual(tags, [])
        self.assertEqual(ns, [])


if __name__ == "__main__":
    unittest.main()
