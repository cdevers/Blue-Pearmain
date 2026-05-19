"""
tests/test_deduplicator.py — unit tests for poller/deduplicator.py
"""

import json
import sqlite3 as _sqlite3
import sys
import unittest

sys.path.insert(0, ".")

from poller.deduplicator import (
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
            id=1,
            uuid="UUID-LO",
            fingerprint="FP-LO",
            width=1620,
            height=1080,
        )
        hi = make_photo(
            id=2,
            uuid="UUID-HI",
            fingerprint="FP-HI",
            width=6048,
            height=4024,
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
            id=1,
            flickr_id="111",
            date_uploaded_flickr="2026-04-10T14:00:00+00:00",
        )
        b = make_photo(
            id=2,
            flickr_id="222",
            date_uploaded_flickr="2026-04-10T14:33:00+00:00",
        )
        group = _classify_group([a, b])
        self.assertEqual(group.group_type, "device_upload")

    def test_device_upload_keeper_is_earliest(self):
        a = make_photo(
            id=1,
            flickr_id="111",
            date_uploaded_flickr="2026-04-10T14:00:00+00:00",
        )
        b = make_photo(
            id=2,
            flickr_id="222",
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
        lo = make_photo(id=1, uuid="UUID-LO", fingerprint="FP-LO", width=None, height=None)
        hi = make_photo(id=2, uuid="UUID-HI", fingerprint="FP-HI", width=None, height=None)
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


class TestNormaliseUtcSecond(unittest.TestCase):
    def test_iso_with_negative_offset_converts_to_utc(self):
        from poller.deduplicator import _normalise_to_utc_second

        # 14:12:43 at -04:00 is 18:12:43 UTC
        self.assertEqual(
            _normalise_to_utc_second("2024-09-28T14:12:43.000000-04:00"),
            "2024-09-28 18:12:43",
        )

    def test_naive_string_treated_as_utc(self):
        from poller.deduplicator import _normalise_to_utc_second

        self.assertEqual(
            _normalise_to_utc_second("2024-09-28 14:12:43"),
            "2024-09-28 14:12:43",
        )

    def test_truncation_not_rounding(self):
        from poller.deduplicator import _normalise_to_utc_second

        # .999999 should truncate to :43, not round to :44
        self.assertEqual(
            _normalise_to_utc_second("2024-09-28T14:12:43.999999+00:00"),
            "2024-09-28 14:12:43",
        )

    def test_invalid_returns_none(self):
        from poller.deduplicator import _normalise_to_utc_second

        self.assertIsNone(_normalise_to_utc_second("not-a-date"))

    def test_empty_returns_none(self):
        from poller.deduplicator import _normalise_to_utc_second

        self.assertIsNone(_normalise_to_utc_second(""))


class TestReuploadMatchKey(unittest.TestCase):
    def test_smaller_id_first(self):
        from poller.deduplicator import _reupload_match_key

        self.assertEqual(_reupload_match_key("54000", "48000"), "reupload:48000:54000")

    def test_already_in_order(self):
        from poller.deduplicator import _reupload_match_key

        self.assertEqual(_reupload_match_key("48000", "54000"), "reupload:48000:54000")

    def test_commutative(self):
        from poller.deduplicator import _reupload_match_key

        self.assertEqual(
            _reupload_match_key("54000", "48000"),
            _reupload_match_key("48000", "54000"),
        )


class TestClassifyReuploadPair(unittest.TestCase):
    """Tests for _classify_reupload_pair().

    linked  = record with both uuid and flickr_id (Photos-linked, possibly low-res)
    orphan  = Flickr-only record with no uuid (candidate_public, possibly re-upload)

    Flickr IDs: linked=48922000000, orphan=54060000000 → gap=5138000000 >> CROSS_SESSION_THRESHOLD
    """

    def _linked(self, **kwargs):
        return make_photo(
            id=1,
            flickr_id="48922000000",
            uuid="AAAA-1111",
            original_filename="DSC_0042.JPG",
            date_taken="2022-08-14T10:23:11+00:00",
            **kwargs,
        )

    def _orphan(self, **kwargs):
        defaults = dict(
            id=2,
            flickr_id="54060000000",
            uuid=None,
            original_filename="DSC_0042.JPG",
            date_taken="2022-08-14T10:23:11+00:00",
            privacy_state="candidate_public",
        )
        defaults.update(kwargs)
        return make_photo(**defaults)

    def test_filename_match_large_gap_is_reupload(self):
        from poller.deduplicator import _classify_reupload_pair

        group = _classify_reupload_pair(
            self._linked(),
            self._orphan(),
            filename_match=True,
            linked_match_count=1,
            orphan_match_count=1,
        )
        self.assertEqual(group.group_type, "reupload")

    def test_small_gap_is_uncertain(self):
        from poller.deduplicator import _classify_reupload_pair

        # gap = 50, well below CROSS_SESSION_THRESHOLD=100_000
        orphan = self._orphan(flickr_id="48922000050")
        group = _classify_reupload_pair(
            self._linked(), orphan, filename_match=True, linked_match_count=1, orphan_match_count=1
        )
        self.assertEqual(group.group_type, "reupload_uncertain")

    def test_timestamp_only_fallback_always_uncertain(self):
        from poller.deduplicator import _classify_reupload_pair

        group = _classify_reupload_pair(
            self._linked(),
            self._orphan(),
            filename_match=False,
            linked_match_count=1,
            orphan_match_count=1,
        )
        self.assertEqual(group.group_type, "reupload_uncertain")

    def test_multiple_linked_candidates_forces_uncertain(self):
        from poller.deduplicator import _classify_reupload_pair

        group = _classify_reupload_pair(
            self._linked(),
            self._orphan(),
            filename_match=True,
            linked_match_count=2,
            orphan_match_count=1,
        )
        self.assertEqual(group.group_type, "reupload_uncertain")

    def test_multiple_orphan_candidates_forces_uncertain(self):
        from poller.deduplicator import _classify_reupload_pair

        group = _classify_reupload_pair(
            self._linked(),
            self._orphan(),
            filename_match=True,
            linked_match_count=1,
            orphan_match_count=2,
        )
        self.assertEqual(group.group_type, "reupload_uncertain")

    def test_orphan_dramatically_larger_wins_keeper(self):
        from poller.deduplicator import _classify_reupload_pair

        # orphan 6000×4000 (24M px) vs linked 1620×1080 (1.75M px) → ratio ≈ 13.7×
        linked = self._linked(width=1620, height=1080)
        orphan = self._orphan(width=6000, height=4000)
        group = _classify_reupload_pair(
            linked, orphan, filename_match=True, linked_match_count=1, orphan_match_count=1
        )
        self.assertEqual(group.group_type, "reupload")
        self.assertIs(group.keeper, orphan)
        self.assertEqual(group.discards, [linked])

    def test_similar_sizes_linked_wins_and_group_is_uncertain(self):
        from poller.deduplicator import _classify_reupload_pair

        # ratio = 1050²/1000² ≈ 1.1, below REUPLOAD_KEEPER_PIXEL_RATIO=1.5
        linked = self._linked(width=1000, height=1000)
        orphan = self._orphan(width=1050, height=1050)
        group = _classify_reupload_pair(
            linked, orphan, filename_match=True, linked_match_count=1, orphan_match_count=1
        )
        self.assertEqual(group.group_type, "reupload_uncertain")
        self.assertIs(group.keeper, linked)

    def test_only_orphan_has_dims_linked_still_wins(self):
        from poller.deduplicator import _classify_reupload_pair

        linked = self._linked(width=None, height=None)
        orphan = self._orphan(width=6000, height=4000)
        group = _classify_reupload_pair(
            linked, orphan, filename_match=True, linked_match_count=1, orphan_match_count=1
        )
        self.assertEqual(group.group_type, "reupload_uncertain")
        self.assertIs(group.keeper, linked)

    def test_no_dims_keeper_assumed_true_in_notes(self):
        from poller.deduplicator import _classify_reupload_pair

        # make_photo() defaults width=None, height=None
        group = _classify_reupload_pair(
            self._linked(),
            self._orphan(),
            filename_match=True,
            linked_match_count=1,
            orphan_match_count=1,
        )
        data = json.loads(group.notes)
        self.assertTrue(data["keeper_assumed"])

    def test_zero_width_treated_as_no_dims(self):
        from poller.deduplicator import _classify_reupload_pair

        # width=0 → pixels property returns None → treated as no dimensions
        linked = self._linked(width=0, height=0)
        orphan = self._orphan(width=6000, height=4000)
        group = _classify_reupload_pair(
            linked, orphan, filename_match=True, linked_match_count=1, orphan_match_count=1
        )
        self.assertIs(group.keeper, linked)
        self.assertEqual(group.group_type, "reupload_uncertain")

    def test_match_key_smaller_flickr_id_first(self):
        from poller.deduplicator import _classify_reupload_pair

        group = _classify_reupload_pair(
            self._linked(),
            self._orphan(),
            filename_match=True,
            linked_match_count=1,
            orphan_match_count=1,
        )
        self.assertEqual(group.match_key, "reupload:48922000000:54060000000")

    def test_evidence_blob_contains_required_fields(self):
        from poller.deduplicator import _classify_reupload_pair

        linked = self._linked(width=1620, height=1080)
        orphan = self._orphan(width=6000, height=4000)
        group = _classify_reupload_pair(
            linked, orphan, filename_match=True, linked_match_count=1, orphan_match_count=1
        )
        data = json.loads(group.notes)
        for key in (
            "keeper_flickr_id",
            "discard_flickr_id",
            "filename_match",
            "timestamp_delta_s",
            "upload_session_gap",
            "dimension_ratio",
            "linked_match_count",
            "orphan_match_count",
            "keeper_assumed",
            "summary",
        ):
            self.assertIn(key, data, f"missing key: {key}")
        self.assertEqual(data["keeper_flickr_id"], orphan.flickr_id)
        self.assertEqual(data["discard_flickr_id"], linked.flickr_id)


def _make_db() -> _sqlite3.Connection:
    """Return an in-memory DB with the minimal photos schema for reupload tests."""
    conn = _sqlite3.connect(":memory:")
    conn.row_factory = _sqlite3.Row
    conn.execute("""
        CREATE TABLE photos (
            id INTEGER PRIMARY KEY,
            flickr_id TEXT,
            uuid TEXT,
            original_filename TEXT,
            date_taken TEXT,
            date_added_photos TEXT,
            date_uploaded_flickr TEXT,
            fingerprint TEXT,
            width INTEGER,
            height INTEGER,
            privacy_state TEXT DEFAULT 'candidate_public',
            duplicate_group_id INTEGER
        )
    """)
    return conn


def _insert(conn, **kwargs):
    cols = ", ".join(kwargs.keys())
    placeholders = ", ".join("?" for _ in kwargs)
    conn.execute(f"INSERT INTO photos ({cols}) VALUES ({placeholders})", list(kwargs.values()))


def _make_db_with_groups() -> _sqlite3.Connection:
    """In-memory DB with photos + duplicate_groups for Phase 2 tests."""
    conn = _sqlite3.connect(":memory:")
    conn.row_factory = _sqlite3.Row
    conn.execute("""
        CREATE TABLE duplicate_groups (
            id INTEGER PRIMARY KEY,
            match_key TEXT NOT NULL UNIQUE,
            group_type TEXT NOT NULL,
            resolved INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE photos (
            id INTEGER PRIMARY KEY,
            flickr_id TEXT,
            uuid TEXT,
            original_filename TEXT,
            date_taken TEXT,
            date_added_photos TEXT,
            date_uploaded_flickr TEXT,
            fingerprint TEXT,
            width INTEGER,
            height INTEGER,
            privacy_state TEXT DEFAULT 'candidate_public',
            duplicate_group_id INTEGER,
            duplicate_role TEXT,
            flickr_deleted INTEGER DEFAULT 0
        )
    """)
    return conn


class TestFetchReuploadCandidates(unittest.TestCase):
    def test_matched_pair_produces_one_group(self):
        from poller.deduplicator import _fetch_reupload_candidates

        conn = _make_db()
        _insert(
            conn,
            id=1,
            flickr_id="48922000000",
            uuid="AAAA",
            original_filename="DSC_0042.JPG",
            date_taken="2022-08-14T10:23:11+00:00",
            privacy_state="approved_public",
        )
        _insert(
            conn,
            id=2,
            flickr_id="54060000000",
            uuid=None,
            original_filename="DSC_0042.JPG",
            date_taken="2022-08-14T10:23:11+00:00",
            privacy_state="candidate_public",
        )
        groups, conflicts = _fetch_reupload_candidates(conn)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(conflicts), 0)
        self.assertEqual(groups[0].group_type, "reupload")

    def test_already_grouped_orphan_goes_to_conflicts(self):
        from poller.deduplicator import _fetch_reupload_candidates

        conn = _make_db()
        _insert(
            conn,
            id=1,
            flickr_id="48922000000",
            uuid="AAAA",
            original_filename="DSC_0042.JPG",
            date_taken="2022-08-14T10:23:11+00:00",
            privacy_state="approved_public",
        )
        _insert(
            conn,
            id=2,
            flickr_id="54060000000",
            uuid=None,
            original_filename="DSC_0042.JPG",
            date_taken="2022-08-14T10:23:11+00:00",
            privacy_state="candidate_public",
            duplicate_group_id=99,
        )
        groups, conflicts = _fetch_reupload_candidates(conn)
        self.assertEqual(len(groups), 0)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["flickr_id"], "54060000000")
        self.assertEqual(conflicts[0]["side"], "orphan")

    def test_null_filename_fallback_produces_uncertain(self):
        from poller.deduplicator import _fetch_reupload_candidates

        conn = _make_db()
        _insert(
            conn,
            id=1,
            flickr_id="48922000000",
            uuid="AAAA",
            original_filename=None,
            date_taken="2022-08-14T10:23:11+00:00",
            privacy_state="approved_public",
        )
        _insert(
            conn,
            id=2,
            flickr_id="54060000000",
            uuid=None,
            original_filename=None,
            date_taken="2022-08-14T10:23:11+00:00",
            privacy_state="candidate_public",
        )
        groups, conflicts = _fetch_reupload_candidates(conn)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].group_type, "reupload_uncertain")

    def test_no_timestamp_overlap_produces_no_groups(self):
        from poller.deduplicator import _fetch_reupload_candidates

        conn = _make_db()
        _insert(
            conn,
            id=1,
            flickr_id="48922000000",
            uuid="AAAA",
            original_filename="DSC_0042.JPG",
            date_taken="2022-08-14T10:23:11+00:00",
            privacy_state="approved_public",
        )
        _insert(
            conn,
            id=2,
            flickr_id="54060000000",
            uuid=None,
            original_filename="DSC_0042.JPG",
            date_taken="2023-01-01T00:00:00+00:00",  # completely different date
            privacy_state="candidate_public",
        )
        groups, conflicts = _fetch_reupload_candidates(conn)
        self.assertEqual(len(groups), 0)

    def test_two_second_window_matches(self):
        from poller.deduplicator import _fetch_reupload_candidates

        conn = _make_db()
        _insert(
            conn,
            id=1,
            flickr_id="48922000000",
            uuid="AAAA",
            original_filename="DSC_0042.JPG",
            date_taken="2022-08-14T10:23:11+00:00",
            privacy_state="approved_public",
        )
        _insert(
            conn,
            id=2,
            flickr_id="54060000000",
            uuid=None,
            original_filename="DSC_0042.JPG",
            date_taken="2022-08-14T10:23:13+00:00",  # 2 seconds later
            privacy_state="candidate_public",
        )
        groups, conflicts = _fetch_reupload_candidates(conn)
        self.assertEqual(len(groups), 1)

    def test_three_second_gap_does_not_match(self):
        from poller.deduplicator import _fetch_reupload_candidates

        conn = _make_db()
        _insert(
            conn,
            id=1,
            flickr_id="48922000000",
            uuid="AAAA",
            original_filename="DSC_0042.JPG",
            date_taken="2022-08-14T10:23:11+00:00",
            privacy_state="approved_public",
        )
        _insert(
            conn,
            id=2,
            flickr_id="54060000000",
            uuid=None,
            original_filename="DSC_0042.JPG",
            date_taken="2022-08-14T10:23:14+00:00",  # 3 seconds later — outside window
            privacy_state="candidate_public",
        )
        groups, conflicts = _fetch_reupload_candidates(conn)
        self.assertEqual(len(groups), 0)

    def test_include_approved_adds_approved_public(self):
        from poller.deduplicator import _fetch_reupload_candidates

        conn = _make_db()
        # linked record (approved_public, has uuid)
        _insert(
            conn,
            id=1,
            flickr_id="48922000000",
            uuid="AAAA",
            original_filename="DSC_0042.JPG",
            date_taken="2022-08-14T10:23:11+00:00",
            privacy_state="approved_public",
        )
        # orphan with approved_public (orientation duplicate)
        _insert(
            conn,
            id=2,
            flickr_id="54060000000",
            uuid=None,
            original_filename="DSC_0042.JPG",
            date_taken="2022-08-14T10:23:11+00:00",
            privacy_state="approved_public",
        )
        groups, conflicts = _fetch_reupload_candidates(conn, include_approved=True)
        self.assertEqual(len(groups), 1)

    def test_include_approved_off_excludes_approved_public(self):
        from poller.deduplicator import _fetch_reupload_candidates

        conn = _make_db()
        _insert(
            conn,
            id=1,
            flickr_id="48922000000",
            uuid="AAAA",
            original_filename="DSC_0042.JPG",
            date_taken="2022-08-14T10:23:11+00:00",
            privacy_state="approved_public",
        )
        _insert(
            conn,
            id=2,
            flickr_id="54060000000",
            uuid=None,
            original_filename="DSC_0042.JPG",
            date_taken="2022-08-14T10:23:11+00:00",
            privacy_state="approved_public",
        )
        # Without include_approved, the approved_public orphan is excluded
        groups, conflicts = _fetch_reupload_candidates(conn)
        self.assertEqual(len(groups), 0)

    def test_include_approved_null_filename_classified_uncertain(self):
        from poller.deduplicator import _fetch_reupload_candidates

        conn = _make_db()
        _insert(
            conn,
            id=1,
            flickr_id="48922000000",
            uuid="AAAA",
            original_filename=None,
            date_taken="2022-08-14T10:23:11+00:00",
            privacy_state="approved_public",
        )
        _insert(
            conn,
            id=2,
            flickr_id="54060000000",
            uuid=None,
            original_filename=None,
            date_taken="2022-08-14T10:23:11+00:00",
            privacy_state="approved_public",
        )
        groups, conflicts = _fetch_reupload_candidates(conn, include_approved=True)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].group_type, "reupload_uncertain")


# ---------------------------------------------------------------------------
# _delete_discards helpers
# ---------------------------------------------------------------------------


def _make_dedup_db():
    """In-memory DB with photos + duplicate_groups tables for delete-discards tests."""
    conn = _sqlite3.connect(":memory:")
    conn.row_factory = _sqlite3.Row
    conn.execute("""
        CREATE TABLE photos (
            id INTEGER PRIMARY KEY,
            flickr_id TEXT,
            uuid TEXT,
            privacy_state TEXT DEFAULT 'candidate_public',
            duplicate_role TEXT,
            duplicate_group_id INTEGER,
            flickr_deleted INTEGER DEFAULT 0,
            updated_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE duplicate_groups (
            id INTEGER PRIMARY KEY,
            match_key TEXT UNIQUE,
            group_type TEXT,
            keeper_id INTEGER,
            photo_count INTEGER DEFAULT 0,
            resolved INTEGER DEFAULT 0,
            notes TEXT,
            updated_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        )
    """)
    return conn


def _insert_group_and_discard(
    conn,
    group_id: int = 1,
    discard_flickr_id: str = "54060000000",
    privacy_state: str = "approved_public",
    flickr_deleted: int = 0,
    resolved: int = 0,
    notes: str | None = None,
):
    """Insert a keeper + discard pair into a duplicate_group for testing."""
    import json as _json

    default_notes = _json.dumps(
        {
            "keeper_flickr_id": "48922000000",
            "discard_flickr_id": discard_flickr_id,
            "summary": f"DSC_0042.JPG | 2022-08-14T10:23:11 | linked=48922000000 → orphan={discard_flickr_id}",
        }
    )
    conn.execute(
        "INSERT INTO duplicate_groups (id, match_key, group_type, keeper_id, photo_count, resolved, notes) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            group_id,
            f"reupload:48922000000:{discard_flickr_id}",
            "reupload_uncertain",
            10,
            2,
            resolved,
            notes or default_notes,
        ),
    )
    conn.execute(
        "INSERT INTO photos (id, flickr_id, uuid, privacy_state, duplicate_role, duplicate_group_id, flickr_deleted) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (10, "48922000000", "AAAA", "approved_public", "keeper", group_id, 0),
    )
    conn.execute(
        "INSERT INTO photos (id, flickr_id, uuid, privacy_state, duplicate_role, duplicate_group_id, flickr_deleted) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (20, discard_flickr_id, None, privacy_state, "discard", group_id, flickr_deleted),
    )


# ---------------------------------------------------------------------------
# TestDeleteDiscards
# ---------------------------------------------------------------------------


class TestDeleteDiscards(unittest.TestCase):
    def test_dry_run_no_api_calls(self):
        from unittest.mock import MagicMock
        from poller.deduplicator import _delete_discards

        conn = _make_dedup_db()
        _insert_group_and_discard(conn)
        client = MagicMock()
        deleted, already_gone, errors = _delete_discards(conn, client, dry_run=True)
        self.assertEqual(deleted, 0)
        self.assertEqual(already_gone, 0)
        self.assertEqual(errors, 0)
        client.delete_photo.assert_not_called()

    def test_apply_success_sets_flickr_deleted_and_resolved(self):
        from unittest.mock import MagicMock
        from poller.deduplicator import _delete_discards

        conn = _make_dedup_db()
        _insert_group_and_discard(conn)
        client = MagicMock()
        deleted, already_gone, errors = _delete_discards(conn, client, dry_run=False)
        self.assertEqual(deleted, 1)
        self.assertEqual(already_gone, 0)
        self.assertEqual(errors, 0)
        client.delete_photo.assert_called_once_with("54060000000")
        row = conn.execute(
            "SELECT flickr_deleted FROM photos WHERE flickr_id = '54060000000'"
        ).fetchone()
        self.assertEqual(row["flickr_deleted"], 1)
        group = conn.execute("SELECT resolved FROM duplicate_groups WHERE id = 1").fetchone()
        self.assertEqual(group["resolved"], 1)

    def test_flickr_error_1_treated_as_success(self):
        from unittest.mock import MagicMock
        from flickr.flickr_client import FlickrError
        from poller.deduplicator import _delete_discards

        conn = _make_dedup_db()
        _insert_group_and_discard(conn)
        client = MagicMock()
        client.delete_photo.side_effect = FlickrError(1, "Photo not found")
        deleted, already_gone, errors = _delete_discards(conn, client, dry_run=False)
        self.assertEqual(deleted, 0)
        self.assertEqual(already_gone, 1)
        self.assertEqual(errors, 0)
        row = conn.execute(
            "SELECT flickr_deleted FROM photos WHERE flickr_id = '54060000000'"
        ).fetchone()
        self.assertEqual(row["flickr_deleted"], 1)
        group = conn.execute("SELECT resolved FROM duplicate_groups WHERE id = 1").fetchone()
        self.assertEqual(group["resolved"], 1)

    def test_other_flickr_error_leaves_record_untouched(self):
        from unittest.mock import MagicMock
        from flickr.flickr_client import FlickrError
        from poller.deduplicator import _delete_discards

        conn = _make_dedup_db()
        _insert_group_and_discard(conn)
        client = MagicMock()
        client.delete_photo.side_effect = FlickrError(99, "Insufficient permissions")
        deleted, already_gone, errors = _delete_discards(conn, client, dry_run=False)
        self.assertEqual(deleted, 0)
        self.assertEqual(already_gone, 0)
        self.assertEqual(errors, 1)
        row = conn.execute(
            "SELECT flickr_deleted FROM photos WHERE flickr_id = '54060000000'"
        ).fetchone()
        self.assertEqual(row["flickr_deleted"], 0)
        group = conn.execute("SELECT resolved FROM duplicate_groups WHERE id = 1").fetchone()
        self.assertEqual(group["resolved"], 0)

    def test_candidate_public_discard_excluded(self):
        from unittest.mock import MagicMock
        from poller.deduplicator import _delete_discards

        conn = _make_dedup_db()
        _insert_group_and_discard(conn, privacy_state="candidate_public")
        client = MagicMock()
        deleted, already_gone, errors = _delete_discards(conn, client, dry_run=False)
        self.assertEqual(deleted + already_gone + errors, 0)
        client.delete_photo.assert_not_called()

    def test_already_flickr_deleted_excluded(self):
        from unittest.mock import MagicMock
        from poller.deduplicator import _delete_discards

        conn = _make_dedup_db()
        _insert_group_and_discard(conn, flickr_deleted=1)
        client = MagicMock()
        deleted, already_gone, errors = _delete_discards(conn, client, dry_run=False)
        self.assertEqual(deleted + already_gone + errors, 0)
        client.delete_photo.assert_not_called()

    def test_resolved_group_excluded(self):
        from unittest.mock import MagicMock
        from poller.deduplicator import _delete_discards

        conn = _make_dedup_db()
        _insert_group_and_discard(conn, resolved=1)
        client = MagicMock()
        deleted, already_gone, errors = _delete_discards(conn, client, dry_run=False)
        self.assertEqual(deleted + already_gone + errors, 0)
        client.delete_photo.assert_not_called()


class TestMarkReuploaDiscards(unittest.TestCase):
    """Tests for _mark_reupload_discards().

    Uses _make_db_with_groups() which creates both photos and duplicate_groups.
    """

    def _setup(
        self,
        group_type: str = "reupload",
        privacy_state: str = "candidate_public",
        flickr_deleted: int = 0,
        resolved: int = 0,
    ) -> _sqlite3.Connection:
        conn = _make_db_with_groups()
        conn.execute(
            "INSERT INTO duplicate_groups (id, match_key, group_type, resolved)"
            " VALUES (1, 'reupload:48000:54000', ?, ?)",
            (group_type, resolved),
        )
        conn.execute(
            "INSERT INTO photos"
            " (id, flickr_id, privacy_state, duplicate_group_id, duplicate_role, flickr_deleted)"
            " VALUES (1, '48922000000', ?, 1, 'discard', ?)",
            (privacy_state, flickr_deleted),
        )
        conn.commit()
        return conn

    def test_marks_reupload_discards(self):
        from poller.deduplicator import _mark_reupload_discards

        conn = self._setup()
        count = _mark_reupload_discards(conn, dry_run=False)
        self.assertEqual(count, 1)
        row = conn.execute("SELECT privacy_state FROM photos WHERE id = 1").fetchone()
        self.assertEqual(row["privacy_state"], "duplicate_flickr")

    def test_skips_uncertain_groups(self):
        from poller.deduplicator import _mark_reupload_discards

        conn = self._setup(group_type="reupload_uncertain")
        count = _mark_reupload_discards(conn, dry_run=False)
        self.assertEqual(count, 0)
        row = conn.execute("SELECT privacy_state FROM photos WHERE id = 1").fetchone()
        self.assertEqual(row["privacy_state"], "candidate_public")

    def test_skips_already_marked(self):
        from poller.deduplicator import _mark_reupload_discards

        conn = self._setup(privacy_state="duplicate_flickr")
        count = _mark_reupload_discards(conn, dry_run=False)
        self.assertEqual(count, 0)

    def test_skips_flickr_deleted(self):
        from poller.deduplicator import _mark_reupload_discards

        conn = self._setup(flickr_deleted=1)
        count = _mark_reupload_discards(conn, dry_run=False)
        self.assertEqual(count, 0)

    def test_dry_run_no_changes(self):
        from poller.deduplicator import _mark_reupload_discards

        conn = self._setup()
        count = _mark_reupload_discards(conn, dry_run=True)
        self.assertEqual(count, 1)  # eligible count returned even in dry-run
        row = conn.execute("SELECT privacy_state FROM photos WHERE id = 1").fetchone()
        self.assertEqual(row["privacy_state"], "candidate_public")  # unchanged

    def test_skips_resolved_groups(self):
        from poller.deduplicator import _mark_reupload_discards

        conn = self._setup(resolved=1)
        count = _mark_reupload_discards(conn, dry_run=False)
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
