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


def _orch_db():
    """Database with operation_log + legacy_index migrations and helpers ready."""
    from db.db import Database
    from db.migrations.migrate_020_operation_log import run as run_op_log
    from db.migrations.migrate_026_legacy_index import run_on_conn as run_legacy

    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    db = Database(Path(f.name))
    run_op_log(str(f.name))
    run_legacy(db.conn)
    db.set_legacy_library({"library_uuid": "L", "asset_count": 0})
    return db


def _seed_photo(db, pid, flickr_id, state="candidate_public", date_taken="2010-06-01 12:00:00"):
    db.conn.execute(
        "INSERT INTO photos (id, uuid, flickr_id, privacy_state, privacy_reason, "
        "date_taken, width, height, flickr_title) "
        "VALUES (?, NULL, ?, ?, 'no people detected', ?, 4000, 3000, '')",
        (pid, flickr_id, state, date_taken),
    )
    db.conn.commit()


def _seed_asset(db, asset_uuid, **over):
    row = {
        "library_uuid": "L",
        "asset_uuid": asset_uuid,
        "original_filename": "img.jpg",
        "fingerprint": "fp",
        "date_taken": "2010-06-01T12:00:00-00:00",
        "width": 4000,
        "height": 3000,
        "latitude": None,
        "longitude": None,
        "title": "",
        "description": None,
        "keywords": "[]",
        "labels": "[]",
        "persons": "[]",
        "named_face_count": 0,
        "unknown_face_count": 0,
        "master_rel_path": "m.jpg",
        "thumbnail_cache_key": asset_uuid,
        "thumbnail_status": "ok",
    }
    row.update(over)
    db.upsert_legacy_asset(row)


def test_apply_demotes_matched_people_photo():
    from legacy_apply import apply_legacy_matches

    db = _orch_db()
    _seed_photo(db, 1, "100")
    _seed_asset(db, "A", persons='["Aunt May"]', named_face_count=1)
    counts = apply_legacy_matches(
        db, "L", self_name="Me", zones=[], person_policies={}, classifier_version=1
    )
    assert counts["reclassified"] == 1
    assert counts["needs_review"] == 1
    assert counts["auto_private"] == 0
    state = db.conn.execute("SELECT privacy_state FROM photos WHERE id = 1").fetchone()[
        "privacy_state"
    ]
    assert state == "needs_review"


def test_apply_leaves_people_free_match_unchanged():
    from legacy_apply import apply_legacy_matches

    db = _orch_db()
    _seed_photo(db, 1, "100")
    _seed_asset(db, "A")  # no people signal
    counts = apply_legacy_matches(
        db, "L", self_name="Me", zones=[], person_policies={}, classifier_version=1
    )
    assert counts["reclassified"] == 0
    assert counts["unchanged"] == 1
    state = db.conn.execute("SELECT privacy_state FROM photos WHERE id = 1").fetchone()[
        "privacy_state"
    ]
    assert state == "candidate_public"


def test_apply_never_touches_human_reviewed_photo():
    from legacy_apply import apply_legacy_matches

    db = _orch_db()
    _seed_photo(db, 1, "100", state="approved_public")
    _seed_asset(db, "A", persons='["Aunt May"]', named_face_count=1, latitude=10.0, longitude=20.0)
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
    counts = apply_legacy_matches(
        db, "L", self_name="Me", zones=zones, person_policies={}, classifier_version=1
    )
    assert counts["reclassified"] == 0
    state = db.conn.execute("SELECT privacy_state FROM photos WHERE id = 1").fetchone()[
        "privacy_state"
    ]
    assert state == "approved_public"
    logs = db.conn.execute("SELECT COUNT(*) AS n FROM operation_log WHERE photo_id = 1").fetchone()[
        "n"
    ]
    assert logs == 0


def test_apply_is_idempotent():
    from legacy_apply import apply_legacy_matches

    db = _orch_db()
    _seed_photo(db, 1, "100")
    _seed_asset(db, "A", persons='["Aunt May"]', named_face_count=1)
    first = apply_legacy_matches(
        db, "L", self_name="Me", zones=[], person_policies={}, classifier_version=1
    )
    second = apply_legacy_matches(
        db, "L", self_name="Me", zones=[], person_policies={}, classifier_version=1
    )
    assert first["reclassified"] == 1
    assert second["reclassified"] == 0
    logs = db.conn.execute("SELECT COUNT(*) AS n FROM operation_log WHERE photo_id = 1").fetchone()[
        "n"
    ]
    assert logs == 1


def test_apply_counts_contract_invariants():
    from legacy_apply import apply_legacy_matches

    db = _orch_db()
    _seed_photo(db, 1, "100")  # -> reclassified (people)
    _seed_asset(db, "A", persons='["Aunt May"]', named_face_count=1)
    _seed_photo(db, 2, "200", date_taken="2012-01-01 09:00:00")  # -> unchanged
    _seed_asset(db, "B", date_taken="2012-01-01T09:00:00-00:00")  # no signal
    counts = apply_legacy_matches(
        db, "L", self_name="Me", zones=[], person_policies={}, classifier_version=1
    )
    assert set(counts) == {
        "eligible",
        "reclassified",
        "needs_review",
        "auto_private",
        "unchanged",
        "failed",
        "metadata_matched",
        "metadata_applied",
        "metadata_failed",
    }
    assert counts["eligible"] == 2
    assert counts["reclassified"] + counts["unchanged"] + counts["failed"] == counts["eligible"]
    assert counts["needs_review"] + counts["auto_private"] == counts["reclassified"]


def test_apply_isolates_per_photo_failure_and_continues(monkeypatch):
    from legacy_apply import apply_legacy_matches

    db = _orch_db()
    _seed_photo(db, 1, "100")
    _seed_asset(db, "A", persons='["Aunt May"]', named_face_count=1)
    _seed_photo(db, 2, "200", date_taken="2012-01-01 09:00:00")
    _seed_asset(
        db, "B", persons='["Uncle Ben"]', named_face_count=1, date_taken="2012-01-01T09:00:00-00:00"
    )

    real = db.reclassify_legacy_match
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")  # first photo's write fails
        return real(*a, **k)

    monkeypatch.setattr(db, "reclassify_legacy_match", flaky)
    counts = apply_legacy_matches(
        db, "L", self_name="Me", zones=[], person_policies={}, classifier_version=1
    )
    assert counts["failed"] == 1
    assert counts["reclassified"] == 1  # second photo still processed
    demoted = db.conn.execute(
        "SELECT COUNT(*) AS n FROM photos WHERE privacy_state = 'needs_review'"
    ).fetchone()["n"]
    assert demoted == 1


def test_apply_resumes_failed_photo_on_rerun(monkeypatch):
    """candidate_public scope is the resume point: a photo that failed last run
    is still candidate_public and gets re-attempted; a succeeded photo is not."""
    from legacy_apply import apply_legacy_matches

    db = _orch_db()
    _seed_photo(db, 1, "100")  # A: will succeed first pass
    _seed_asset(db, "A", persons='["Aunt May"]', named_face_count=1)
    _seed_photo(db, 2, "200", date_taken="2012-01-01 09:00:00")  # B: fails first
    _seed_asset(
        db, "B", persons='["Uncle Ben"]', named_face_count=1, date_taken="2012-01-01T09:00:00-00:00"
    )

    real = db.reclassify_legacy_match

    def fail_photo_2(photo_id, *a, **k):
        if photo_id == 2:
            raise RuntimeError("boom")
        return real(photo_id, *a, **k)

    # First pass: A succeeds, B fails.
    monkeypatch.setattr(db, "reclassify_legacy_match", fail_photo_2)
    first = apply_legacy_matches(
        db, "L", self_name="Me", zones=[], person_policies={}, classifier_version=1
    )
    assert first["reclassified"] == 1 and first["failed"] == 1

    # Second pass: patch removed. Only B is still candidate_public, so only B is
    # attempted; A was demoted and is no longer re-seen.
    monkeypatch.setattr(db, "reclassify_legacy_match", real)
    second = apply_legacy_matches(
        db, "L", self_name="Me", zones=[], person_policies={}, classifier_version=1
    )
    assert second["eligible"] == 1  # only B remains candidate_public
    assert second["reclassified"] == 1 and second["failed"] == 0
    states = dict(db.conn.execute("SELECT id, privacy_state FROM photos").fetchall())
    assert states[1] == "needs_review" and states[2] == "needs_review"
    # Each photo logged exactly once across both passes (no dup for A).
    logs = dict(
        db.conn.execute(
            "SELECT photo_id, COUNT(*) AS n FROM operation_log GROUP BY photo_id"
        ).fetchall()
    )
    assert logs == {1: 1, 2: 1}


def _assert_pure_noop(db, pid, updated_at_before):
    """A no-op must touch neither photos nor operation_log for this photo."""
    row = db.conn.execute(
        "SELECT privacy_state, privacy_reason, updated_at FROM photos WHERE id = ?",
        (pid,),
    ).fetchone()
    assert row["privacy_state"] == "candidate_public"
    assert row["privacy_reason"] == "no people detected"  # byte-for-byte unchanged
    assert row["updated_at"] == updated_at_before  # true no-op: no touch
    logs = db.conn.execute(
        "SELECT COUNT(*) AS n FROM operation_log WHERE photo_id = ?", (pid,)
    ).fetchone()["n"]
    assert logs == 0


def test_apply_confident_candidate_verdict_is_pure_noop():
    """Confident match whose classifier verdict is candidate_public: no write."""
    from legacy_apply import apply_legacy_matches

    db = _orch_db()
    _seed_photo(db, 1, "100")
    # Self-only person, no other signal -> classify() -> candidate_public.
    _seed_asset(db, "A", persons='["Me"]', named_face_count=1)
    before = db.conn.execute("SELECT updated_at FROM photos WHERE id = 1").fetchone()["updated_at"]
    counts = apply_legacy_matches(
        db, "L", self_name="Me", zones=[], person_policies={}, classifier_version=1
    )
    assert counts["unchanged"] == 1
    assert counts["reclassified"] == 0
    _assert_pure_noop(db, 1, before)


def test_apply_ambiguous_mixed_skip_is_pure_noop():
    """Ambiguous-mixed match (one people-positive candidate, one not): skipped."""
    from legacy_apply import apply_legacy_matches

    db = _orch_db()
    _seed_photo(db, 1, "100")
    # Two assets at the same wall-clock -> ambiguous; mixed people signal -> skip.
    _seed_asset(db, "A", persons='["Aunt May"]', named_face_count=1)
    _seed_asset(db, "B")  # no people signal -> mixed -> not acted on
    before = db.conn.execute("SELECT updated_at FROM photos WHERE id = 1").fetchone()["updated_at"]
    counts = apply_legacy_matches(
        db, "L", self_name="Me", zones=[], person_policies={}, classifier_version=1
    )
    assert counts["unchanged"] == 1
    assert counts["reclassified"] == 0
    _assert_pure_noop(db, 1, before)
