# tests/test_legacy_match.py
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "poller"))

from legacy_match import classify_match, order_rows, preview_rows  # noqa: E402


def _photo(**kw):
    base = {
        "flickr_id": "1",
        "date_taken": "2010-06-01 12:00:00",
        "width": 4000,
        "height": 3000,
        "flickr_title": "Birthday",
    }
    base.update(kw)
    return base


def _cand(asset_uuid="A", **kw):
    base = {
        "asset_uuid": asset_uuid,
        "date_taken": "2010-06-01T12:00:00-00:00",
        "width": 4000,
        "height": 3000,
        "title": "Birthday",
    }
    base.update(kw)
    return base


class TestClassify:
    def test_confident_single_dims_and_title(self):
        tier, matches = classify_match(_photo(), [_cand("A")])
        assert tier == "confident"
        assert [m["asset_uuid"] for m in matches] == ["A"]

    def test_naive_flickr_vs_tzaware_apple_same_wall_clock(self):
        # Flickr naive capture time vs Apple tz-aware: match on local
        # wall-clock, NOT UTC. EXIF/Flickr date_taken is local time, so the
        # same shot has the same wall-clock on both sides regardless of offset.
        photo = _photo(date_taken="2010-06-01 16:00:00")
        cand = _cand("A", date_taken="2010-06-01T16:00:00-04:00")
        tier, _ = classify_match(photo, [cand])
        assert tier == "confident"

    def test_utc_equal_but_wall_clock_differs_is_no_match(self):
        # Regression guard for #162: these two are equal once converted to UTC
        # (both 20:00Z) but their local wall-clocks differ by 4h, so they are
        # NOT the same photo and must not match.
        photo = _photo(date_taken="2010-06-01 16:00:00")
        cand = _cand("A", date_taken="2010-06-01T12:00:00-04:00")
        tier, matches = classify_match(photo, [cand])
        assert tier == "no-match"
        assert matches == []

    def test_no_match_when_no_timestamp_candidate(self):
        tier, matches = classify_match(_photo(), [_cand("A", date_taken="2011-01-01 00:00:00")])
        assert tier == "no-match"
        assert matches == []

    def test_ambiguous_two_timestamp_matches(self):
        tier, matches = classify_match(_photo(), [_cand("A"), _cand("B")])
        assert tier == "ambiguous"
        assert {m["asset_uuid"] for m in matches} == {"A", "B"}

    def test_ambiguous_single_but_dims_conflict(self):
        tier, _ = classify_match(_photo(), [_cand("A", width=100, height=100)])
        assert tier == "ambiguous"

    def test_empty_title_one_side_never_demotes(self):
        # Confident even though Flickr title is missing.
        tier, _ = classify_match(_photo(flickr_title=""), [_cand("A", title="Birthday")])
        assert tier == "confident"

    def test_both_titles_nonempty_and_differ_is_ambiguous(self):
        tier, _ = classify_match(_photo(flickr_title="Party"), [_cand("A", title="Birthday")])
        assert tier == "ambiguous"

    def test_title_whitespace_only_counts_missing(self):
        tier, _ = classify_match(_photo(flickr_title="   "), [_cand("A", title="Birthday")])
        assert tier == "confident"


class TestPreviewRowsAndOrdering:
    def test_preview_emits_one_row_per_no_match(self):
        rows = preview_rows([(_photo(flickr_id="9"), [])])
        assert len(rows) == 1
        assert rows[0]["tier"] == "no-match"
        assert rows[0]["asset_uuid"] == ""

    def test_preview_emits_row_per_candidate_for_ambiguous(self):
        rows = preview_rows([(_photo(), [_cand("B"), _cand("A")])])
        assert {r["asset_uuid"] for r in rows} == {"A", "B"}
        assert all(r["tier"] == "ambiguous" for r in rows)

    def test_order_is_tier_then_date_then_flickr_then_asset(self):
        rows = [
            {
                "tier": "no-match",
                "date_norm": "2010-01-01 00:00:00",
                "flickr_id": "5",
                "asset_uuid": "",
            },
            {
                "tier": "confident",
                "date_norm": "2010-01-01 00:00:00",
                "flickr_id": "2",
                "asset_uuid": "Z",
            },
            {
                "tier": "ambiguous",
                "date_norm": "2009-01-01 00:00:00",
                "flickr_id": "1",
                "asset_uuid": "B",
            },
            {
                "tier": "ambiguous",
                "date_norm": "2009-01-01 00:00:00",
                "flickr_id": "1",
                "asset_uuid": "A",
            },
        ]
        out = order_rows(rows)
        assert [r["tier"] for r in out] == ["confident", "ambiguous", "ambiguous", "no-match"]
        # Within the two ambiguous rows: same date+flickr_id, so asset_uuid breaks the tie.
        assert [r["asset_uuid"] for r in out[1:3]] == ["A", "B"]

    def test_order_is_stable_across_runs(self):
        rows = [
            {"tier": "confident", "date_norm": "2010", "flickr_id": "2", "asset_uuid": "A"},
            {"tier": "confident", "date_norm": "2010", "flickr_id": "1", "asset_uuid": "A"},
        ]
        assert order_rows(list(rows)) == order_rows(list(reversed(rows)))
