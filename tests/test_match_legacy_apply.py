# tests/test_match_legacy_apply.py
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "poller"))


def test_classifier_version_is_a_positive_int():
    from analyzer.privacy import CLASSIFIER_VERSION

    assert isinstance(CLASSIFIER_VERSION, int)
    assert CLASSIFIER_VERSION >= 1


def test_shape_injects_unknown_sentinels():
    from legacy_match import shape_legacy_for_classify

    shaped = shape_legacy_for_classify(
        {
            "persons": "[]",
            "labels": "[]",
            "unknown_face_count": 2,
            "latitude": None,
            "longitude": None,
        }
    )
    assert shaped["persons"] == ["_UNKNOWN_", "_UNKNOWN_"]


def test_shape_parses_json_persons_and_labels_and_passes_latlon():
    from legacy_match import shape_legacy_for_classify

    shaped = shape_legacy_for_classify(
        {
            "persons": '["Aunt May"]',
            "labels": '["beach"]',
            "unknown_face_count": 0,
            "latitude": 1.5,
            "longitude": -2.5,
        }
    )
    assert shaped["persons"] == ["Aunt May"]
    assert shaped["labels"] == ["beach"]
    assert shaped["latitude"] == 1.5
    assert shaped["longitude"] == -2.5


def test_shape_accepts_list_inputs_and_null_counts():
    from legacy_match import shape_legacy_for_classify

    shaped = shape_legacy_for_classify(
        {
            "persons": ["Bob"],
            "labels": ["x"],
            "unknown_face_count": None,
            "latitude": None,
            "longitude": None,
        }
    )
    assert shaped["persons"] == ["Bob"]
    assert shaped["labels"] == ["x"]


def test_people_positive_named_faces():
    from legacy_match import is_people_positive

    assert is_people_positive(
        {"named_face_count": 1, "unknown_face_count": 0, "persons": "[]", "labels": "[]"}
    )


def test_people_positive_unknown_faces():
    from legacy_match import is_people_positive

    assert is_people_positive(
        {"named_face_count": 0, "unknown_face_count": 3, "persons": "[]", "labels": "[]"}
    )


def test_people_positive_named_persons_list():
    from legacy_match import is_people_positive

    assert is_people_positive(
        {"named_face_count": 0, "unknown_face_count": 0, "persons": '["Bob"]', "labels": "[]"}
    )


def test_people_positive_people_label():
    from legacy_match import is_people_positive

    assert is_people_positive(
        {"named_face_count": 0, "unknown_face_count": 0, "persons": "[]", "labels": '["Crowd"]'}
    )


def test_not_people_positive_when_no_signals():
    from legacy_match import is_people_positive

    assert not is_people_positive(
        {"named_face_count": 0, "unknown_face_count": 0, "persons": "[]", "labels": '["beach"]'}
    )


def _photo(**kw):
    base = {
        "flickr_id": "1",
        "date_taken": "2010-06-01 12:00:00",
        "width": 4000,
        "height": 3000,
        "flickr_title": "",
    }
    base.update(kw)
    return base


def _cand(asset_uuid="A", **kw):
    base = {
        "asset_uuid": asset_uuid,
        "date_taken": "2010-06-01T12:00:00-00:00",
        "width": 4000,
        "height": 3000,
        "title": "",
        "persons": "[]",
        "labels": "[]",
        "named_face_count": 0,
        "unknown_face_count": 0,
        "latitude": None,
        "longitude": None,
    }
    base.update(kw)
    return base


def test_confident_with_named_person_demotes_to_needs_review():
    from legacy_match import resolve_apply_decision

    d = resolve_apply_decision(
        _photo(), [_cand("A", persons='["Aunt May"]', named_face_count=1)], zones=[], self_name="Me"
    )
    assert d["state"] == "needs_review"
    assert d["tier"] == "confident"
    assert d["asset_uuid"] == "A"
    assert d["reason"] == "legacy-match[tier=confident,asset=A]: named person(s): Aunt May"


def test_confident_self_only_is_noop():
    from legacy_match import resolve_apply_decision

    d = resolve_apply_decision(
        _photo(), [_cand("A", persons='["Me"]', named_face_count=1)], zones=[], self_name="Me"
    )
    assert d is None


def test_confident_no_people_no_geo_is_noop():
    from legacy_match import resolve_apply_decision

    d = resolve_apply_decision(_photo(), [_cand("A")], zones=[], self_name="Me")
    assert d is None


def test_no_match_is_noop():
    from legacy_match import resolve_apply_decision

    photo = _photo(date_taken="2010-06-01 12:00:00")
    cand = _cand("A", date_taken="2011-01-01T00:00:00-00:00")
    assert resolve_apply_decision(photo, [cand], zones=[], self_name="Me") is None


def test_geofenced_home_demotes_to_auto_private():
    from legacy_match import resolve_apply_decision

    zones = [
        {
            "name": "home",
            "label": "home",
            "latitude": 10.0,
            "longitude": 20.0,
            "radius_m": 100.0,
            "policy": "auto_private",
        }
    ]
    cand = _cand("A", latitude=10.0, longitude=20.0)
    d = resolve_apply_decision(_photo(), [cand], zones=zones, self_name="Me")
    assert d["state"] == "auto_private"
    assert d["reason"].startswith("legacy-match[tier=confident,asset=A]: geofence")


def test_ambiguous_all_people_is_acted_on():
    from legacy_match import resolve_apply_decision

    cands = [
        _cand("A", persons='["Aunt May"]', named_face_count=1),
        _cand("B", persons='["Uncle Ben"]', named_face_count=1),
    ]
    d = resolve_apply_decision(_photo(), cands, zones=[], self_name="Me")
    assert d["state"] == "needs_review"
    assert d["tier"] == "ambiguous"


def test_ambiguous_mixed_is_noop():
    from legacy_match import resolve_apply_decision

    cands = [
        _cand("A", persons='["Aunt May"]', named_face_count=1),
        _cand("B"),
    ]  # B has no people signal
    assert resolve_apply_decision(_photo(), cands, zones=[], self_name="Me") is None


def test_ambiguous_precedence_most_private_wins():
    from legacy_match import resolve_apply_decision

    zones = [
        {
            "name": "home",
            "label": "home",
            "latitude": 10.0,
            "longitude": 20.0,
            "radius_m": 100.0,
            "policy": "auto_private",
        }
    ]
    # A -> needs_review (named person), B -> auto_private (geofence home)
    cands = [
        _cand("A", persons='["Aunt May"]', named_face_count=1),
        _cand("B", named_face_count=1, latitude=10.0, longitude=20.0),
    ]
    d = resolve_apply_decision(_photo(), cands, zones=zones, self_name="Me")
    assert d["state"] == "auto_private"
    assert d["asset_uuid"] == "B"
    # Order-independence: reversing the candidates must not change the winner.
    rev = resolve_apply_decision(_photo(), list(reversed(cands)), zones=zones, self_name="Me")
    assert rev["state"] == "auto_private"
    assert rev["asset_uuid"] == "B"


def test_ambiguous_reason_is_order_independent():
    from legacy_match import resolve_apply_decision

    # Both candidates yield needs_review; lower asset_uuid (A) must win the reason.
    cands = [
        _cand("A", persons='["Aunt May"]', named_face_count=1),
        _cand("B", persons='["Uncle Ben"]', named_face_count=1),
    ]
    forward = resolve_apply_decision(_photo(), cands, zones=[], self_name="Me")
    reverse = resolve_apply_decision(_photo(), list(reversed(cands)), zones=[], self_name="Me")
    assert forward["reason"] == reverse["reason"]
    assert forward["asset_uuid"] == "A"


def test_reason_and_trigger_share_provenance():
    """The two contract strings, built from the same decision, must carry the
    same asset UUID and tier — guards against the formatters drifting apart."""
    from legacy_match import (
        format_legacy_reason,
        format_legacy_trigger,
        resolve_apply_decision,
    )

    cands = [_cand("A", persons='["Aunt May"]', named_face_count=1)]
    d = resolve_apply_decision(_photo(), cands, zones=[], self_name="Me")
    reason = d["reason"]  # already built via format_legacy_reason
    trigger = format_legacy_trigger(d["asset_uuid"], d["tier"], 1)
    # Both strings encode the same provenance, just in their own grammars.
    assert f"asset={d['asset_uuid']}" in reason
    assert f"tier={d['tier']}" in reason
    assert f"legacy:{d['asset_uuid']} " in trigger
    assert f"tier={d['tier']} " in trigger
    # Sanity: reason is exactly what the helper produces for these inputs.
    assert reason == format_legacy_reason(d["tier"], d["asset_uuid"], "named person(s): Aunt May")
