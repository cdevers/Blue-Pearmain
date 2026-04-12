"""
tests/test_deduplicator.py — unit tests for poller/deduplicator.py
"""

import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, ".")

from poller.deduplicator import (
    DuplicateGroup,
    PhotoRow,
    _classify_group,
    _is_snapbridge_pair,
    _upload_gap_minutes,
)


def make_photo(**kwargs) -> PhotoRow:
    defaults = dict(
        id=1,
        flickr_id=None,
        uuid=None,
        original_filename="DSC_0001.JPG",
        date_taken="2024-09-28T14:12:43.000000-04:00",
        date_added_photos=None,
        date_uploaded_flickr=None,
        fingerprint=None,
        width=None,
        height=None,
        privacy_state="candidate_public",
        duplicate_group_id=None,
    )
    defaults.update(kwargs)
    return PhotoRow(**defaults)


# ---------------------------------------------------------------------------
# _is_snapbridge_pair
# ---------------------------------------------------------------------------

class TestIsSnapbridgePair(unittest.TestCase):

    def _pair(self, fp_a="FP-A", fp_b="FP-B", pixels_a=None, pixels_b=None):
        def make(id, fp, w, h):
            return make_photo(id=id, fingerprint=fp, width=w, height=h)
        # Compute width/height from pixel count for convenience
        w_a, h_a = (pixels_a, 1) if pixels_a else (None, None)
        w_b, h_b = (pixels_b, 1) if pixels_b else (None, None)
        return [make(1, fp_a, w_a, h_a), make(2, fp_b, w_b, h_b)]

    def test_different_fingerprints_different_dimensions(self):
        pair = self._pair("FP-LO", "FP-HI", pixels_a=1620 * 1080, pixels_b=6048 * 4024)
        self.assertTrue(_is_snapbridge_pair(pair))

    def test_same_fingerprint_not_snapbridge(self):
        pair = self._pair("FP-SAME", "FP-SAME", pixels_a=6048 * 4024, pixels_b=1620 * 1080)
        self.assertFalse(_is_snapbridge_pair(pair))

    def test_different_fingerprints_same_dimensions_not_snapbridge(self):
        pair = self._pair("FP-A", "FP-B", pixels_a=6048 * 4024, pixels_b=6048 * 4024)
        self.assertFalse(_is_snapbridge_pair(pair))

    def test_missing_fingerprint_not_snapbridge(self):
        pair = self._pair(fp_a=None, fp_b="FP-B", pixels_a=1620 * 1080, pixels_b=6048 * 4024)
        self.assertFalse(_is_snapbridge_pair(pair))

    def test_dimensions_missing_stays_uncertain(self):
        # Different fingerprints but no dimensions yet — should not classify as snapbridge
        pair = self._pair("FP-A", "FP-B", pixels_a=None, pixels_b=None)
        self.assertFalse(_is_snapbridge_pair(pair))

    def test_three_photos_not_snapbridge(self):
        photos = [make_photo(id=i, fingerprint=f"FP-{i}", width=100, height=100) for i in range(3)]
        self.assertFalse(_is_snapbridge_pair(photos))


# ---------------------------------------------------------------------------
# _upload_gap_minutes
# ---------------------------------------------------------------------------

class TestUploadGapMinutes(unittest.TestCase):

    def test_gap_calculation(self):
        a = make_photo(id=1, date_uploaded_flickr="2026-04-10T14:00:00+00:00")
        b = make_photo(id=2, date_uploaded_flickr="2026-04-10T14:33:00+00:00")
        gap = _upload_gap_minutes([a, b])
        self.assertAlmostEqual(gap, 33.0, places=1)

    def test_no_uploads(self):
        a = make_photo(id=1, date_uploaded_flickr=None)
        b = make_photo(id=2, date_uploaded_flickr=None)
        self.assertIsNone(_upload_gap_minutes([a, b]))

    def test_one_upload(self):
        a = make_photo(id=1, date_uploaded_flickr="2026-04-10T14:00:00+00:00")
        b = make_photo(id=2, date_uploaded_flickr=None)
        self.assertIsNone(_upload_gap_minutes([a, b]))


# ---------------------------------------------------------------------------
# _classify_group
# ---------------------------------------------------------------------------

class TestClassifyGroup(unittest.TestCase):

    def _snapbridge_pair(self):
        lo = make_photo(
            id=1, uuid="UUID-LO", fingerprint="FP-LO",
            width=1620, height=1080,
        )
        hi = make_photo(
            id=2, uuid="UUID-HI", fingerprint="FP-HI",
            width=6048, height=4024,
        )
        return [lo, hi]

    def test_snapbridge_classification(self):
        group = _classify_group(self._snapbridge_pair())
        self.assertEqual(group.group_type, "snapbridge")

    def test_snapbridge_keeper_is_high_res(self):
        group = _classify_group(self._snapbridge_pair())
        self.assertIsNotNone(group.keeper)
        self.assertEqual(group.keeper.width, 6048)

    def test_snapbridge_discard_is_low_res(self):
        group = _classify_group(self._snapbridge_pair())
        self.assertEqual(len(group.discards), 1)
        self.assertEqual(group.discards[0].width, 1620)

    def test_device_upload_classification(self):
        a = make_photo(
            id=1, flickr_id="111",
            date_uploaded_flickr="2026-04-10T14:00:00+00:00",
        )
        b = make_photo(
            id=2, flickr_id="222",
            date_uploaded_flickr="2026-04-10T14:33:00+00:00",
        )
        group = _classify_group([a, b])
        self.assertEqual(group.group_type, "device_upload")

    def test_device_upload_keeper_is_earliest(self):
        a = make_photo(
            id=1, flickr_id="111",
            date_uploaded_flickr="2026-04-10T14:00:00+00:00",
        )
        b = make_photo(
            id=2, flickr_id="222",
            date_uploaded_flickr="2026-04-10T14:33:00+00:00",
        )
        group = _classify_group([a, b])
        self.assertEqual(group.keeper.flickr_id, "111")

    def test_uncertain_when_no_signals(self):
        a = make_photo(id=1)
        b = make_photo(id=2)
        group = _classify_group([a, b])
        self.assertEqual(group.group_type, "uncertain")
        self.assertIsNone(group.keeper)
        self.assertEqual(len(group.review), 2)

    def test_snapbridge_without_dimensions_stays_uncertain(self):
        # Different fingerprints but no dimensions yet — must stay uncertain
        # until scanner backfill populates width/height
        lo = make_photo(id=1, uuid="UUID-LO", fingerprint="FP-LO",
                        width=None, height=None)
        hi = make_photo(id=2, uuid="UUID-HI", fingerprint="FP-HI",
                        width=None, height=None)
        group = _classify_group([lo, hi])
        self.assertEqual(group.group_type, "uncertain")
        self.assertIsNone(group.keeper)


# ---------------------------------------------------------------------------
# PhotoRow.pixels
# ---------------------------------------------------------------------------

class TestPhotoRowPixels(unittest.TestCase):

    def test_pixels_computed(self):
        p = make_photo(width=6048, height=4024)
        self.assertEqual(p.pixels, 6048 * 4024)

    def test_pixels_none_when_missing(self):
        p = make_photo(width=None, height=None)
        self.assertIsNone(p.pixels)

    def test_pixels_none_when_partial(self):
        p = make_photo(width=6048, height=None)
        self.assertIsNone(p.pixels)


if __name__ == "__main__":
    unittest.main()
