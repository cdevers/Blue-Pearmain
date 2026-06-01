# tests/test_legacy_tag_propagation.py
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "poller"))


def test_payload_confident_takes_single_asset_tags():
    from legacy_match import CONFIDENT, legacy_metadata_payload

    asset = {
        "keywords": '["Beach", "Summer"]',
        "labels": '["sky"]',
        "title": "Trip",
        "description": "At the shore",
    }
    out = legacy_metadata_payload(CONFIDENT, [asset])
    assert out["add_tags"] == ["beach", "sky", "summer"]
    assert out["title"] == "Trip"
    assert out["description"] == "At the shore"


def test_payload_ambiguous_intersects_tags_and_drops_scalars():
    from legacy_match import AMBIGUOUS, legacy_metadata_payload

    a = {"keywords": '["beach", "birthday"]', "labels": "[]", "title": "A", "description": "da"}
    b = {"keywords": '["beach", "picnic"]', "labels": "[]", "title": "B", "description": "db"}
    out = legacy_metadata_payload(AMBIGUOUS, [a, b])
    assert out["add_tags"] == ["beach"]  # shared only
    assert out["title"] is None  # scalars confident-only
    assert out["description"] is None


def test_payload_ambiguous_no_shared_tags_is_empty():
    from legacy_match import AMBIGUOUS, legacy_metadata_payload

    a = {"keywords": '["beach"]', "labels": "[]"}
    b = {"keywords": '["mountain"]', "labels": "[]"}
    out = legacy_metadata_payload(AMBIGUOUS, [a, b])
    assert out["add_tags"] == []


def test_payload_applies_label_blocklist_and_remap():
    from legacy_match import CONFIDENT, legacy_metadata_payload

    asset = {
        "keywords": "[]",
        "labels": '["people", "automobile"]',
        "title": "",
        "description": None,
    }
    out = legacy_metadata_payload(CONFIDENT, [asset])
    assert "people" not in out["add_tags"]  # blocklisted
    assert "car" in out["add_tags"]  # automobile -> car
    assert out["title"] is None  # "" -> None
    assert out["description"] is None  # None -> None


def test_payload_empty_keywords_and_labels():
    from legacy_match import CONFIDENT, legacy_metadata_payload

    out = legacy_metadata_payload(
        CONFIDENT, [{"keywords": "[]", "labels": "[]", "title": "T", "description": ""}]
    )
    assert out["add_tags"] == []
    assert out["title"] == "T"
    assert out["description"] is None  # whitespace/"" -> None


def test_payload_whitespace_only_scalars_become_none():
    from legacy_match import CONFIDENT, legacy_metadata_payload

    out = legacy_metadata_payload(
        CONFIDENT, [{"keywords": "[]", "labels": "[]", "title": "   ", "description": "\t\n"}]
    )
    assert out["title"] is None  # whitespace-only stripped to None
    assert out["description"] is None


def test_payload_confident_multi_asset_raises():
    """CONFIDENT is contracted to exactly one matched asset; assert fires if violated."""
    import pytest
    from legacy_match import CONFIDENT, legacy_metadata_payload

    a = {"keywords": "[]", "labels": "[]", "title": "", "description": None}
    with pytest.raises(AssertionError, match="CONFIDENT must return exactly one"):
        legacy_metadata_payload(CONFIDENT, [a, a])


def test_metadata_trigger_confident_names_asset():
    from legacy_match import CONFIDENT, format_legacy_metadata_trigger

    t = format_legacy_metadata_trigger(CONFIDENT, [{"asset_uuid": "ABC"}], 1)
    assert "ABC" in t
    assert "tier=confident" in t
    assert "clf=1" in t


def test_metadata_trigger_ambiguous_records_count_not_uuid():
    from legacy_match import AMBIGUOUS, format_legacy_metadata_trigger

    t = format_legacy_metadata_trigger(AMBIGUOUS, [{"asset_uuid": "A"}, {"asset_uuid": "B"}], 2)
    assert "A" not in t.replace("ambiguous", "")  # no single uuid leaked
    assert "n=2" in t
    assert "tier=ambiguous" in t
    assert "clf=2" in t
