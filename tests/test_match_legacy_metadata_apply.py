# tests/test_match_legacy_metadata_apply.py
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "poller"))


def _logs(db, pid=1):
    return db.conn.execute(
        "SELECT operation, target, old_value, new_value, trigger, actor "
        "FROM operation_log WHERE photo_id = ? ORDER BY id",
        (pid,),
    ).fetchall()


def _orch_db_168():
    """Database with operation_log + legacy_index migrations and a legacy library."""
    from db.db import Database
    from db.migrations.migrate_020_operation_log import run as run_op_log
    from db.migrations.migrate_026_legacy_index import run_on_conn as run_legacy

    tmp = tempfile.mkdtemp()
    db_path = Path(tmp) / "test.db"
    db = Database(db_path)
    run_op_log(str(db_path))
    run_legacy(db.conn)
    db.set_legacy_library({"library_uuid": "L", "asset_count": 0})
    return db


def _seed_photo_168(db, pid, flickr_id, state="candidate_public", date_taken="2010-06-01 12:00:00"):
    db.conn.execute(
        "INSERT INTO photos (id, uuid, flickr_id, privacy_state, privacy_reason, "
        "date_taken, width, height, flickr_title) "
        "VALUES (?, NULL, ?, ?, 'no people detected', ?, 4000, 3000, '')",
        (pid, flickr_id, state, date_taken),
    )
    db.conn.commit()


def _seed_asset_168(db, asset_uuid, **over):
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


def test_orch_matched_not_demoted_photo_is_tagged():
    from legacy_apply import apply_legacy_matches

    db = _orch_db_168()
    _seed_photo_168(db, 1, "100")
    _seed_asset_168(db, "A", keywords='["beach", "summer"]', title="Shore Day", description="fun")
    counts = apply_legacy_matches(
        db, "L", self_name="Me", zones=[], person_policies={}, classifier_version=1
    )
    assert counts["reclassified"] == 0  # no people => stays public
    assert counts["unchanged"] == 1  # privacy unchanged
    assert counts["metadata_matched"] == 1
    assert counts["metadata_applied"] == 1
    assert counts["metadata_failed"] == 0
    row = db.conn.execute(
        "SELECT privacy_state, proposed_tags, proposed_title FROM photos WHERE id = 1"
    ).fetchone()
    assert row["privacy_state"] == "candidate_public"
    assert json.loads(row["proposed_tags"]) == ["beach", "summer"]
    assert row["proposed_title"] == "Shore Day"


def test_orch_demoted_photo_is_reclassified_and_tagged():
    from legacy_apply import apply_legacy_matches

    db = _orch_db_168()
    _seed_photo_168(db, 1, "100")
    _seed_asset_168(db, "A", persons='["Aunt May"]', named_face_count=1, keywords='["family"]')
    counts = apply_legacy_matches(
        db, "L", self_name="Me", zones=[], person_policies={}, classifier_version=1
    )
    assert counts["reclassified"] == 1
    assert counts["metadata_matched"] == 1
    assert counts["metadata_applied"] == 1
    row = db.conn.execute("SELECT privacy_state, proposed_tags FROM photos WHERE id = 1").fetchone()
    assert row["privacy_state"] == "needs_review"
    assert json.loads(row["proposed_tags"]) == ["family"]
    # Two audit rows: the demotion (txn 1) and the metadata (txn 2).
    ops = [r["operation"] for r in _logs(db)]
    assert "match_legacy_apply" in ops
    assert "match_legacy_metadata" in ops


def test_orch_no_match_photo_not_attempted():
    from legacy_apply import apply_legacy_matches

    db = _orch_db_168()
    _seed_photo_168(db, 1, "100", date_taken="1999-01-01 00:00:00")  # no asset at this time
    _seed_asset_168(db, "A", keywords='["beach"]')
    counts = apply_legacy_matches(
        db, "L", self_name="Me", zones=[], person_policies={}, classifier_version=1
    )
    assert counts["metadata_matched"] == 0
    assert counts["metadata_applied"] == 0
    assert (
        db.conn.execute("SELECT proposed_tags FROM photos WHERE id = 1").fetchone()["proposed_tags"]
        is None
    )


def test_orch_idempotent_rerun_no_duplicate_tags_or_logs():
    from legacy_apply import apply_legacy_matches

    db = _orch_db_168()
    _seed_photo_168(db, 1, "100")
    _seed_asset_168(db, "A", keywords='["beach"]', title="T")
    first = apply_legacy_matches(
        db, "L", self_name="Me", zones=[], person_policies={}, classifier_version=1
    )
    second = apply_legacy_matches(
        db, "L", self_name="Me", zones=[], person_policies={}, classifier_version=1
    )
    assert first["metadata_applied"] == 1
    assert second["metadata_matched"] == 1  # still matches
    assert second["metadata_applied"] == 0  # nothing left to change
    assert json.loads(
        db.conn.execute("SELECT proposed_tags FROM photos WHERE id = 1").fetchone()["proposed_tags"]
    ) == ["beach"]
    meta_logs = [r for r in _logs(db) if r["operation"] == "match_legacy_metadata"]
    assert len(meta_logs) == 1  # only the first run logged


def test_orch_ambiguous_intersection_merges_with_existing_tags_idempotent():
    """End-to-end: a photo with existing proposed_tags matched ambiguously to
    two assets gets (existing ∪ shared-tag intersection), and a rerun is a
    no-op (no duplicate tags, no second metadata log)."""
    from legacy_apply import apply_legacy_matches

    db = _orch_db_168()
    _seed_photo_168(db, 1, "100")
    db.conn.execute("UPDATE photos SET proposed_tags = ? WHERE id = 1", (json.dumps(["old"]),))
    db.conn.commit()
    # Two assets at the same timestamp => ambiguous; intersection of tags = {beach}.
    _seed_asset_168(db, "A", keywords='["beach", "birthday"]', title="Party")
    _seed_asset_168(db, "B", keywords='["beach", "picnic"]', title="Outing")

    first = apply_legacy_matches(
        db, "L", self_name="Me", zones=[], person_policies={}, classifier_version=1
    )
    assert first["metadata_matched"] == 1
    assert first["metadata_applied"] == 1
    row = db.conn.execute(
        "SELECT proposed_tags, proposed_title FROM photos WHERE id = 1"
    ).fetchone()
    assert json.loads(row["proposed_tags"]) == ["beach", "old"]  # merged, deduped, sorted
    assert row["proposed_title"] is None  # ambiguous => no scalar

    second = apply_legacy_matches(
        db, "L", self_name="Me", zones=[], person_policies={}, classifier_version=1
    )
    assert second["metadata_matched"] == 1
    assert second["metadata_applied"] == 0  # nothing left to change
    assert json.loads(
        db.conn.execute("SELECT proposed_tags FROM photos WHERE id = 1").fetchone()["proposed_tags"]
    ) == ["beach", "old"]
    meta_logs = [r for r in _logs(db) if r["operation"] == "match_legacy_metadata"]
    assert len(meta_logs) == 1


def test_orch_ambiguous_zero_shared_tags_is_noop():
    """Ambiguous match whose candidates share no tags: counted as matched, but
    the empty payload changes nothing — no apply, no audit row, no scalar."""
    from legacy_apply import apply_legacy_matches

    db = _orch_db_168()
    _seed_photo_168(db, 1, "100")
    _seed_asset_168(db, "A", keywords='["beach"]', title="X")  # disjoint...
    _seed_asset_168(db, "B", keywords='["mountain"]', title="Y")  # ...no intersection

    counts = apply_legacy_matches(
        db, "L", self_name="Me", zones=[], person_policies={}, classifier_version=1
    )
    assert counts["metadata_matched"] == 1
    assert counts["metadata_applied"] == 0
    assert counts["metadata_failed"] == 0
    assert (
        db.conn.execute("SELECT proposed_tags FROM photos WHERE id = 1").fetchone()["proposed_tags"]
        is None
    )
    assert [r for r in _logs(db) if r["operation"] == "match_legacy_metadata"] == []


def test_orch_metadata_failure_isolated(monkeypatch):
    from legacy_apply import apply_legacy_matches

    db = _orch_db_168()
    _seed_photo_168(db, 1, "100")
    _seed_asset_168(db, "A", keywords='["beach"]')

    def _boom(*a, **k):
        raise RuntimeError("metadata write failed")

    monkeypatch.setattr(db, "apply_legacy_metadata", _boom)
    counts = apply_legacy_matches(
        db, "L", self_name="Me", zones=[], person_policies={}, classifier_version=1
    )
    assert counts["metadata_matched"] == 1
    assert counts["metadata_applied"] == 0
    assert counts["metadata_failed"] == 1


def test_orch_demotion_failure_does_not_block_metadata(monkeypatch):
    """Policy: privacy demotion and metadata propagation are two independent
    writes. If the demotion (txn 1) fails, metadata (txn 2) still runs. This is
    intentional — do not "fix" it by short-circuiting on demotion failure."""
    from legacy_apply import apply_legacy_matches

    db = _orch_db_168()
    _seed_photo_168(db, 1, "100")
    # A photo that WOULD demote (has a named person) so reclassify is attempted.
    _seed_asset_168(db, "A", persons='["Aunt May"]', named_face_count=1, keywords='["family"]')

    def _boom(*a, **k):
        raise RuntimeError("demotion write failed")

    monkeypatch.setattr(db, "reclassify_legacy_match", _boom)
    counts = apply_legacy_matches(
        db, "L", self_name="Me", zones=[], person_policies={}, classifier_version=1
    )
    assert counts["failed"] == 1  # demotion failed and rolled back
    assert counts["reclassified"] == 0
    assert counts["metadata_matched"] == 1
    assert counts["metadata_applied"] == 1  # metadata still applied
    assert json.loads(
        db.conn.execute("SELECT proposed_tags FROM photos WHERE id = 1").fetchone()["proposed_tags"]
    ) == ["family"]
