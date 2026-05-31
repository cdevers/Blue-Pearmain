# tests/test_match_legacy_apply.py
from __future__ import annotations

import sqlite3
import sys
import tempfile
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


def _apply_db():
    """Fresh Database with operation_log migration and one candidate_public photo."""
    from db.db import Database
    from db.migrations.migrate_020_operation_log import run as run_op_log

    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    db = Database(Path(f.name))
    run_op_log(str(f.name))
    db.conn.execute(
        "INSERT INTO photos (id, uuid, privacy_state, privacy_reason) "
        "VALUES (1, NULL, 'candidate_public', 'no people detected')"
    )
    db.conn.commit()
    return db


def test_reclassify_writes_state_and_audit_atomically():
    from analyzer.privacy import CLASSIFIER_VERSION
    from legacy_match import format_legacy_reason, format_legacy_trigger

    db = _apply_db()
    db.reclassify_legacy_match(
        1,
        "needs_review",
        format_legacy_reason("confident", "A", "named person(s): Aunt May"),
        trigger=format_legacy_trigger("A", "confident", CLASSIFIER_VERSION),
    )
    row = db.conn.execute(
        "SELECT privacy_state, privacy_reason FROM photos WHERE id = 1"
    ).fetchone()
    assert row["privacy_state"] == "needs_review"
    assert "Aunt May" in row["privacy_reason"]
    log = db.conn.execute(
        "SELECT operation, target, old_value, new_value, trigger, actor "
        "FROM operation_log WHERE photo_id = 1"
    ).fetchall()
    assert len(log) == 1
    # Frozen audit-row shape (#166): assert every field by value, not presence.
    assert log[0]["operation"] == "match_legacy_apply"
    assert log[0]["target"] == "privacy_state"
    assert log[0]["old_value"] == "candidate_public"
    assert log[0]["new_value"] == "needs_review"
    assert log[0]["actor"] == "bp"
    assert log[0]["trigger"] == f"legacy:A tier=confident clf={CLASSIFIER_VERSION}"


class _AuditFailConn:
    """Wraps a real sqlite3 connection but raises on the operation_log INSERT.

    sqlite3.Connection methods are read-only (can't monkeypatch .execute on the
    instance), so we delegate through a wrapper and swap it onto db._local.conn.
    Context-manager + all other attrs delegate to the real connection, so the
    `with self.conn:` transaction (commit/rollback) still operates on it.
    """

    def __init__(self, real):
        self._real = real

    def execute(self, sql, *args, **kwargs):
        if sql.lstrip().upper().startswith("INSERT INTO OPERATION_LOG"):
            raise sqlite3.OperationalError("simulated audit failure")
        return self._real.execute(sql, *args, **kwargs)

    def __enter__(self):
        return self._real.__enter__()

    def __exit__(self, *exc):
        return self._real.__exit__(*exc)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_reclassify_rolls_back_when_audit_insert_fails():
    db = _apply_db()
    real = db.conn
    db._local.conn = _AuditFailConn(real)
    try:
        db.reclassify_legacy_match(1, "needs_review", "x", trigger="legacy:A tier=confident clf=1")
        raised = False
    except sqlite3.OperationalError:
        raised = True
    finally:
        db._local.conn = real  # restore for assertions
    assert raised
    row = db.conn.execute(
        "SELECT privacy_state, privacy_reason FROM photos WHERE id = 1"
    ).fetchone()
    assert row["privacy_state"] == "candidate_public"
    assert row["privacy_reason"] == "no people detected"
    count = db.conn.execute(
        "SELECT COUNT(*) AS n FROM operation_log WHERE photo_id = 1"
    ).fetchone()["n"]
    assert count == 0
