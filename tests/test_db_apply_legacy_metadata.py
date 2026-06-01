# tests/test_db_apply_legacy_metadata.py
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "poller"))


def test_photos_table_has_proposed_title_column():
    from db.db import Database

    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    db = Database(Path(f.name))
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(photos)").fetchall()}
    assert "proposed_title" in cols
    assert "proposed_description" in cols  # sanity: existing column still there


def _meta_db():
    """Fresh Database (has proposed_title via _ensure_schema) + operation_log +
    one Flickr-only candidate_public photo (id=1, empty proposed_* fields)."""
    from db.db import Database
    from db.migrations.migrate_020_operation_log import run as run_op_log

    import tempfile

    tmp = tempfile.mkdtemp()
    db_path = Path(tmp) / "test.db"
    db = Database(db_path)
    run_op_log(str(db_path))
    db.conn.execute(
        "INSERT INTO photos (id, uuid, flickr_id, privacy_state, privacy_reason) "
        "VALUES (1, NULL, '100', 'candidate_public', 'no people detected')"
    )
    db.conn.commit()
    return db


def _logs(db, pid=1):
    return db.conn.execute(
        "SELECT operation, target, old_value, new_value, trigger, actor "
        "FROM operation_log WHERE photo_id = ? ORDER BY id",
        (pid,),
    ).fetchall()


def test_apply_metadata_fills_empty_tags_title_description():
    db = _meta_db()
    changed = db.apply_legacy_metadata(
        1,
        add_tags=["beach", "summer"],
        title="Trip",
        description="At the shore",
        trigger="legacy-meta:A tier=confident clf=1",
    )
    assert changed is True
    row = db.conn.execute(
        "SELECT proposed_tags, proposed_title, proposed_description FROM photos WHERE id = 1"
    ).fetchone()
    assert json.loads(row["proposed_tags"]) == ["beach", "summer"]
    assert row["proposed_title"] == "Trip"
    assert row["proposed_description"] == "At the shore"
    logs = _logs(db)
    assert len(logs) == 1
    assert logs[0]["operation"] == "match_legacy_metadata"
    assert logs[0]["target"] == "legacy_metadata"
    assert logs[0]["actor"] == "bp"
    nv = json.loads(logs[0]["new_value"])
    assert nv["fields"] == ["proposed_tags", "proposed_title", "proposed_description"]
    assert nv["tags_added"] == 2


def test_apply_metadata_merges_tags_no_clobber():
    db = _meta_db()
    db.conn.execute(
        "UPDATE photos SET proposed_tags = ? WHERE id = 1", (json.dumps(["beach", "old"]),)
    )
    db.conn.commit()
    changed = db.apply_legacy_metadata(1, add_tags=["beach", "new"], trigger="t")
    assert changed is True
    assert json.loads(
        db.conn.execute("SELECT proposed_tags FROM photos WHERE id = 1").fetchone()["proposed_tags"]
    ) == ["beach", "new", "old"]
    nv = json.loads(_logs(db)[0]["new_value"])
    assert nv["fields"] == ["proposed_tags"]
    assert nv["tags_added"] == 1  # only "new" is delta


def test_apply_metadata_does_not_clobber_existing_scalars():
    db = _meta_db()
    db.conn.execute(
        "UPDATE photos SET proposed_title = 'Human Draft', proposed_description = 'edited' WHERE id = 1"
    )
    db.conn.commit()
    changed = db.apply_legacy_metadata(
        1, add_tags=[], title="Legacy", description="legacy desc", trigger="t"
    )
    assert changed is False
    row = db.conn.execute(
        "SELECT proposed_title, proposed_description FROM photos WHERE id = 1"
    ).fetchone()
    assert row["proposed_title"] == "Human Draft"
    assert row["proposed_description"] == "edited"
    assert _logs(db) == []


def test_apply_metadata_whitespace_existing_scalar_treated_as_empty():
    db = _meta_db()
    db.conn.execute("UPDATE photos SET proposed_title = '   ' WHERE id = 1")
    db.conn.commit()
    changed = db.apply_legacy_metadata(1, add_tags=[], title="Legacy", trigger="t")
    assert changed is True
    assert (
        db.conn.execute("SELECT proposed_title FROM photos WHERE id = 1").fetchone()[
            "proposed_title"
        ]
        == "Legacy"
    )


def test_apply_metadata_whitespace_incoming_scalar_is_not_staged():
    db = _meta_db()
    changed = db.apply_legacy_metadata(
        1, add_tags=["beach"], title="   ", description="\t\n", trigger="t"
    )
    assert changed is True  # tags changed
    row = db.conn.execute(
        "SELECT proposed_title, proposed_description FROM photos WHERE id = 1"
    ).fetchone()
    assert row["proposed_title"] is None  # whitespace-only incoming not staged
    assert row["proposed_description"] is None
    nv = json.loads(_logs(db)[0]["new_value"])
    assert nv["fields"] == ["proposed_tags"]  # only tags, no scalar fields


def test_apply_metadata_stores_stripped_scalar():
    db = _meta_db()
    db.apply_legacy_metadata(1, add_tags=[], title="  Trip  ", trigger="t")
    assert (
        db.conn.execute("SELECT proposed_title FROM photos WHERE id = 1").fetchone()[
            "proposed_title"
        ]
        == "Trip"
    )


def test_apply_metadata_idempotent_rerun_returns_false():
    db = _meta_db()
    db.apply_legacy_metadata(1, add_tags=["beach"], title="T", trigger="t")
    changed = db.apply_legacy_metadata(1, add_tags=["beach"], title="T", trigger="t")
    assert changed is False
    assert len(_logs(db)) == 1  # only the first write logged


def test_apply_metadata_partial_tags_unchanged_title_filled():
    db = _meta_db()
    db.conn.execute("UPDATE photos SET proposed_tags = ? WHERE id = 1", (json.dumps(["beach"]),))
    db.conn.commit()
    changed = db.apply_legacy_metadata(1, add_tags=["beach"], title="T", trigger="t")
    assert changed is True
    nv = json.loads(_logs(db)[0]["new_value"])
    assert nv["fields"] == ["proposed_title"]
    assert "tags_added" not in nv


def test_apply_metadata_audit_new_value_omits_absent_keys():
    """new_value must omit absent keys entirely (no JSON nulls) so downstream
    consumers can rely on key-presence. Title-only fill => only `fields`."""
    db = _meta_db()
    db.conn.execute("UPDATE photos SET proposed_tags = ? WHERE id = 1", (json.dumps(["beach"]),))
    db.conn.commit()
    db.apply_legacy_metadata(1, add_tags=["beach"], title="T", trigger="t")  # tags unchanged
    nv = json.loads(_logs(db)[0]["new_value"])
    assert nv == {"fields": ["proposed_title"]}  # exact shape: no tags_added, no tags_repaired
    assert None not in nv.values()


def test_apply_metadata_repairs_malformed_tags_and_flags_it():
    db = _meta_db()
    db.conn.execute("UPDATE photos SET proposed_tags = ? WHERE id = 1", ('"not-a-list"',))
    db.conn.commit()
    changed = db.apply_legacy_metadata(1, add_tags=["beach"], trigger="t")
    assert changed is True
    assert json.loads(
        db.conn.execute("SELECT proposed_tags FROM photos WHERE id = 1").fetchone()["proposed_tags"]
    ) == ["beach"]
    nv = json.loads(_logs(db)[0]["new_value"])
    assert nv["tags_repaired"] is True


def test_apply_metadata_repairs_malformed_tags_with_no_add():
    db = _meta_db()
    db.conn.execute("UPDATE photos SET proposed_tags = ? WHERE id = 1", ('{"a": 1}',))
    db.conn.commit()
    changed = db.apply_legacy_metadata(1, add_tags=[], trigger="t")
    assert changed is True  # forced by malformed even though merged == current == []
    assert (
        json.loads(
            db.conn.execute("SELECT proposed_tags FROM photos WHERE id = 1").fetchone()[
                "proposed_tags"
            ]
        )
        == []
    )
    nv = json.loads(_logs(db)[0]["new_value"])
    assert nv["tags_repaired"] is True


def test_apply_metadata_list_with_non_string_members_is_repaired():
    db = _meta_db()
    db.conn.execute(
        "UPDATE photos SET proposed_tags = ? WHERE id = 1", (json.dumps(["beach", 1, None]),)
    )
    db.conn.commit()
    changed = db.apply_legacy_metadata(1, add_tags=["summer"], trigger="t")
    assert changed is True
    # Non-string members (1, None) dropped; not str()-coerced into "1"/"none".
    assert json.loads(
        db.conn.execute("SELECT proposed_tags FROM photos WHERE id = 1").fetchone()["proposed_tags"]
    ) == ["beach", "summer"]
    nv = json.loads(_logs(db)[0]["new_value"])
    assert nv["tags_repaired"] is True
    assert nv["tags_added"] == 1  # only "summer" is new; "beach" was already present


def test_apply_metadata_clean_string_list_only_no_repair_flag():
    db = _meta_db()
    db.conn.execute("UPDATE photos SET proposed_tags = ? WHERE id = 1", (json.dumps(["beach"]),))
    db.conn.commit()
    changed = db.apply_legacy_metadata(1, add_tags=["summer"], trigger="t")
    assert changed is True
    nv = json.loads(_logs(db)[0]["new_value"])
    assert "tags_repaired" not in nv  # clean list of strings is not a repair


def test_apply_metadata_dedupes_duplicate_historical_tags():
    db = _meta_db()
    db.conn.execute(
        "UPDATE photos SET proposed_tags = ? WHERE id = 1", (json.dumps(["beach", "beach"]),)
    )
    db.conn.commit()
    changed = db.apply_legacy_metadata(1, add_tags=[], trigger="t")  # no new tags
    assert changed is True  # forced by the de-dup repair
    assert json.loads(
        db.conn.execute("SELECT proposed_tags FROM photos WHERE id = 1").fetchone()["proposed_tags"]
    ) == ["beach"]
    nv = json.loads(_logs(db)[0]["new_value"])
    assert nv["tags_repaired"] is True
    assert "tags_added" not in nv  # de-dup introduced no new members


def test_apply_metadata_strips_whitespace_in_stored_tags():
    db = _meta_db()
    db.conn.execute(
        "UPDATE photos SET proposed_tags = ? WHERE id = 1", (json.dumps(["  beach  ", ""]),)
    )
    db.conn.commit()
    changed = db.apply_legacy_metadata(1, add_tags=[], trigger="t")
    assert changed is True
    assert json.loads(
        db.conn.execute("SELECT proposed_tags FROM photos WHERE id = 1").fetchone()["proposed_tags"]
    ) == ["beach"]  # stripped, blank dropped
    nv = json.loads(_logs(db)[0]["new_value"])
    assert nv["tags_repaired"] is True


def test_apply_metadata_rolls_back_when_audit_insert_fails():
    db = _meta_db()
    real = db.conn

    class _AuditFailConn:
        def __init__(self, r):
            self._real = r

        def execute(self, sql, *a, **k):
            if sql.lstrip().upper().startswith("INSERT INTO OPERATION_LOG"):
                raise sqlite3.OperationalError("simulated audit failure")
            return self._real.execute(sql, *a, **k)

        def __enter__(self):
            return self._real.__enter__()

        def __exit__(self, *exc):
            return self._real.__exit__(*exc)

        def __getattr__(self, name):
            return getattr(self._real, name)

    db._local.conn = _AuditFailConn(real)
    try:
        raised = False
        try:
            db.apply_legacy_metadata(1, add_tags=["beach"], title="T", trigger="t")
        except sqlite3.OperationalError:
            raised = True
    finally:
        db._local.conn = real
    assert raised
    row = db.conn.execute(
        "SELECT proposed_tags, proposed_title FROM photos WHERE id = 1"
    ).fetchone()
    assert row["proposed_tags"] is None  # update rolled back
    assert row["proposed_title"] is None
    assert _logs(db) == []
