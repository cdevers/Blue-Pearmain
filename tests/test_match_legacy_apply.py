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
