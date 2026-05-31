# `bp match-legacy --apply` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `bp match-legacy --apply`, which re-runs the shared privacy classifier against matched legacy metadata and demotes Flickr-only `candidate_public` photos out of the publish-candidate queue when the legacy data shows people/home.

**Architecture:** Pure decision logic lives in `poller/legacy_match.py` (shape a legacy asset for `classify()`, a people-positive predicate, and `resolve_apply_decision()` that gates by tier and picks the most-private outcome). An atomic DB helper (`db.reclassify_legacy_match`) writes `privacy_state` + an `operation_log` row in one transaction. An orchestration function in `poller/legacy_apply.py` queries eligible photos, calls the decision logic, and persists. The `bp` CLI renames `match-legacy-preview` → `match-legacy` and adds `--apply` to invoke the orchestration.

**Tech Stack:** Python 3, SQLite (`sqlite3`), pytest. No osxphotos / no NAS access in any test.

**Spec:** `docs/superpowers/specs/2026-05-31-match-legacy-apply-166-design.md`

---

## File Structure

- `analyzer/privacy.py` — add `CLASSIFIER_VERSION` constant (versions the rules in `classify()`).
- `poller/legacy_match.py` — add pure helpers: `_json_list`, `shape_legacy_for_classify`, `is_people_positive`, `CLASSIFIER_PRECEDENCE`, `resolve_apply_decision`, and the two frozen-format helpers `format_legacy_reason` / `format_legacy_trigger` (single source of truth for the `privacy_reason` and `operation_log.trigger` contract strings).
- `db/db.py` — add `reclassify_legacy_match(photo_id, new_state, reason, *, trigger)` (atomic state + audit write); takes pre-formatted `reason`/`trigger`, carries no format literal.
- `poller/legacy_apply.py` — **new**: `apply_legacy_matches(db, library_uuid, *, self_name, zones, person_policies, classifier_version)` orchestration returning the frozen counts dict `{eligible, reclassified, needs_review, auto_private, unchanged, failed}` (per-photo atomic, continues past failures).
- `bp` — rename `match-legacy-preview` subcommand → `match-legacy`; add `--apply`; rename `cmd_match_legacy_preview` → `cmd_match_legacy`; call the orchestration in apply mode.
- `tests/test_match_legacy_apply.py` — **new**: decision-logic + orchestration + atomicity tests.
- `README.md` — document `bp match-legacy [--apply]`.

---

### Task 1: Classifier ruleset version constant

**Files:**
- Modify: `analyzer/privacy.py` (top of file, after the module docstring / imports)
- Test: `tests/test_match_legacy_apply.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_match_legacy_apply.py` with this header and first test:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_match_legacy_apply.py::test_classifier_version_is_a_positive_int -v`
Expected: FAIL with `ImportError: cannot import name 'CLASSIFIER_VERSION'`

- [ ] **Step 3: Add the constant**

In `analyzer/privacy.py`, immediately after the `FACE_QUALITY_THRESHOLD = 0.0` line (around line 33), add:

```python
# Ruleset version for classify(). Bump by hand whenever the rules in
# classify() change, so audit rows can be correlated to the logic in force.
CLASSIFIER_VERSION = 1
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_match_legacy_apply.py::test_classifier_version_is_a_positive_int -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add analyzer/privacy.py tests/test_match_legacy_apply.py
git commit -m "feat(#166): add CLASSIFIER_VERSION ruleset stamp"
```

---

### Task 2: Shape a legacy asset for `classify()`

Reconstructs the record shape `classify()` expects, injecting `_UNKNOWN_` sentinels from `unknown_face_count` (legacy stores unknown faces only as a count; `classify()` counts `_UNKNOWN_` entries).

**Files:**
- Modify: `poller/legacy_match.py`
- Test: `tests/test_match_legacy_apply.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_match_legacy_apply.py`:

```python
def test_shape_injects_unknown_sentinels():
    from legacy_match import shape_legacy_for_classify

    shaped = shape_legacy_for_classify(
        {"persons": "[]", "labels": "[]", "unknown_face_count": 2,
         "latitude": None, "longitude": None}
    )
    assert shaped["persons"] == ["_UNKNOWN_", "_UNKNOWN_"]


def test_shape_parses_json_persons_and_labels_and_passes_latlon():
    from legacy_match import shape_legacy_for_classify

    shaped = shape_legacy_for_classify(
        {"persons": '["Aunt May"]', "labels": '["beach"]',
         "unknown_face_count": 0, "latitude": 1.5, "longitude": -2.5}
    )
    assert shaped["persons"] == ["Aunt May"]
    assert shaped["labels"] == ["beach"]
    assert shaped["latitude"] == 1.5
    assert shaped["longitude"] == -2.5


def test_shape_accepts_list_inputs_and_null_counts():
    from legacy_match import shape_legacy_for_classify

    shaped = shape_legacy_for_classify(
        {"persons": ["Bob"], "labels": ["x"], "unknown_face_count": None,
         "latitude": None, "longitude": None}
    )
    assert shaped["persons"] == ["Bob"]
    assert shaped["labels"] == ["x"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_match_legacy_apply.py -k shape -v`
Expected: FAIL with `ImportError: cannot import name 'shape_legacy_for_classify'`

- [ ] **Step 3: Implement the helpers**

In `poller/legacy_match.py`, add `import json` to the imports (after `import sys`), then add near the bottom of the file (after `order_rows`):

```python
def _json_list(value) -> list[str]:
    """Parse a stored JSON-list field (string or already-decoded list)."""
    if isinstance(value, list):
        return [str(x) for x in value if x]
    if isinstance(value, str) and value:
        try:
            data = json.loads(value)
        except (ValueError, TypeError):
            return []
        if isinstance(data, list):
            return [str(x) for x in data if x]
    return []


def shape_legacy_for_classify(asset: dict) -> dict:
    """Build a classify()-ready record from a legacy_assets row.

    Reconstructs `_UNKNOWN_` sentinels from unknown_face_count so the shared
    classifier counts unknown faces the same way it does for Apple records.
    """
    persons = _json_list(asset.get("persons"))
    unknown = int(asset.get("unknown_face_count") or 0)
    persons = persons + ["_UNKNOWN_"] * unknown
    return {
        "latitude": asset.get("latitude"),
        "longitude": asset.get("longitude"),
        "persons": persons,
        "labels": _json_list(asset.get("labels")),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_match_legacy_apply.py -k shape -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add poller/legacy_match.py tests/test_match_legacy_apply.py
git commit -m "feat(#166): shape legacy assets for the shared classifier"
```

---

### Task 3: People-positive predicate

**Files:**
- Modify: `poller/legacy_match.py`
- Test: `tests/test_match_legacy_apply.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_match_legacy_apply.py`:

```python
def test_people_positive_named_faces():
    from legacy_match import is_people_positive

    assert is_people_positive({"named_face_count": 1, "unknown_face_count": 0,
                               "persons": "[]", "labels": "[]"})


def test_people_positive_unknown_faces():
    from legacy_match import is_people_positive

    assert is_people_positive({"named_face_count": 0, "unknown_face_count": 3,
                               "persons": "[]", "labels": "[]"})


def test_people_positive_named_persons_list():
    from legacy_match import is_people_positive

    assert is_people_positive({"named_face_count": 0, "unknown_face_count": 0,
                               "persons": '["Bob"]', "labels": "[]"})


def test_people_positive_people_label():
    from legacy_match import is_people_positive

    assert is_people_positive({"named_face_count": 0, "unknown_face_count": 0,
                               "persons": "[]", "labels": '["Crowd"]'})


def test_not_people_positive_when_no_signals():
    from legacy_match import is_people_positive

    assert not is_people_positive({"named_face_count": 0, "unknown_face_count": 0,
                                   "persons": "[]", "labels": '["beach"]'})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_match_legacy_apply.py -k people_positive -v`
Expected: FAIL with `ImportError: cannot import name 'is_people_positive'`

- [ ] **Step 3: Implement the predicate**

In `poller/legacy_match.py`, add to the imports near the top (after the existing `from legacy_normalize import normalize_title` line):

```python
from analyzer.privacy import PEOPLE_LABELS  # noqa: E402
```

Also ensure the repo root is importable — at the top of the file, after the existing `sys.path.insert(0, str(Path(__file__).parent))` line, add:

```python
sys.path.insert(0, str(Path(__file__).parent.parent))
```

Then add (after `shape_legacy_for_classify`):

```python
def is_people_positive(asset: dict) -> bool:
    """True if the legacy asset shows any people signal classify() would act on."""
    if int(asset.get("named_face_count") or 0) > 0:
        return True
    if int(asset.get("unknown_face_count") or 0) > 0:
        return True
    if _json_list(asset.get("persons")):
        return True
    labels = {lbl.lower() for lbl in _json_list(asset.get("labels"))}
    return bool(labels & PEOPLE_LABELS)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_match_legacy_apply.py -k people_positive -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add poller/legacy_match.py tests/test_match_legacy_apply.py
git commit -m "feat(#166): people-positive predicate for legacy assets"
```

---

### Task 4: `resolve_apply_decision` — tier gating, precedence, deterministic reason

This is the core decision function. It gates by tier (act on `confident`, or `ambiguous` only when *every* candidate is people-positive), classifies each candidate in `asset_uuid` order, takes the most-private state, and returns a frozen-format reason. Returns `None` when no action should be taken (no-match, ambiguous-mixed, or result is `candidate_public`).

**Files:**
- Modify: `poller/legacy_match.py`
- Test: `tests/test_match_legacy_apply.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_match_legacy_apply.py`:

```python
def _photo(**kw):
    base = {"flickr_id": "1", "date_taken": "2010-06-01 12:00:00",
            "width": 4000, "height": 3000, "flickr_title": ""}
    base.update(kw)
    return base


def _cand(asset_uuid="A", **kw):
    base = {"asset_uuid": asset_uuid, "date_taken": "2010-06-01T12:00:00-00:00",
            "width": 4000, "height": 3000, "title": "",
            "persons": "[]", "labels": "[]",
            "named_face_count": 0, "unknown_face_count": 0,
            "latitude": None, "longitude": None}
    base.update(kw)
    return base


def test_confident_with_named_person_demotes_to_needs_review():
    from legacy_match import resolve_apply_decision

    d = resolve_apply_decision(_photo(), [_cand("A", persons='["Aunt May"]',
                                                named_face_count=1)],
                               zones=[], self_name="Me")
    assert d["state"] == "needs_review"
    assert d["tier"] == "confident"
    assert d["asset_uuid"] == "A"
    assert d["reason"] == "legacy-match[tier=confident,asset=A]: named person(s): Aunt May"


def test_confident_self_only_is_noop():
    from legacy_match import resolve_apply_decision

    d = resolve_apply_decision(_photo(), [_cand("A", persons='["Me"]',
                                                named_face_count=1)],
                               zones=[], self_name="Me")
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

    zones = [{"name": "home", "label": "home", "latitude": 10.0,
              "longitude": 20.0, "radius_m": 100.0, "policy": "auto_private"}]
    cand = _cand("A", latitude=10.0, longitude=20.0)
    d = resolve_apply_decision(_photo(), [cand], zones=zones, self_name="Me")
    assert d["state"] == "auto_private"
    assert d["reason"].startswith("legacy-match[tier=confident,asset=A]: geofence")


def test_ambiguous_all_people_is_acted_on():
    from legacy_match import resolve_apply_decision

    cands = [_cand("A", persons='["Aunt May"]', named_face_count=1),
             _cand("B", persons='["Uncle Ben"]', named_face_count=1)]
    d = resolve_apply_decision(_photo(), cands, zones=[], self_name="Me")
    assert d["state"] == "needs_review"
    assert d["tier"] == "ambiguous"


def test_ambiguous_mixed_is_noop():
    from legacy_match import resolve_apply_decision

    cands = [_cand("A", persons='["Aunt May"]', named_face_count=1),
             _cand("B")]  # B has no people signal
    assert resolve_apply_decision(_photo(), cands, zones=[], self_name="Me") is None


def test_ambiguous_precedence_most_private_wins():
    from legacy_match import resolve_apply_decision

    zones = [{"name": "home", "label": "home", "latitude": 10.0,
              "longitude": 20.0, "radius_m": 100.0, "policy": "auto_private"}]
    # A -> needs_review (named person), B -> auto_private (geofence home)
    cands = [_cand("A", persons='["Aunt May"]', named_face_count=1),
             _cand("B", named_face_count=1, latitude=10.0, longitude=20.0)]
    d = resolve_apply_decision(_photo(), cands, zones=zones, self_name="Me")
    assert d["state"] == "auto_private"
    assert d["asset_uuid"] == "B"
    # Order-independence: reversing the candidates must not change the winner.
    rev = resolve_apply_decision(_photo(), list(reversed(cands)),
                                 zones=zones, self_name="Me")
    assert rev["state"] == "auto_private"
    assert rev["asset_uuid"] == "B"


def test_ambiguous_reason_is_order_independent():
    from legacy_match import resolve_apply_decision

    # Both candidates yield needs_review; lower asset_uuid (A) must win the reason.
    cands = [_cand("A", persons='["Aunt May"]', named_face_count=1),
             _cand("B", persons='["Uncle Ben"]', named_face_count=1)]
    forward = resolve_apply_decision(_photo(), cands, zones=[], self_name="Me")
    reverse = resolve_apply_decision(_photo(), list(reversed(cands)),
                                     zones=[], self_name="Me")
    assert forward["reason"] == reverse["reason"]
    assert forward["asset_uuid"] == "A"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_match_legacy_apply.py -k "resolve or confident or ambiguous or no_match or geofenced" -v`
Expected: FAIL with `ImportError: cannot import name 'resolve_apply_decision'`

- [ ] **Step 3: Implement `resolve_apply_decision`**

In `poller/legacy_match.py`, add (after `is_people_positive`):

```python
# Most-private wins: lower rank = more private.
CLASSIFIER_PRECEDENCE = {"auto_private": 0, "needs_review": 1, "candidate_public": 2}


def format_legacy_reason(tier: str, asset_uuid: str, classifier_reason: str) -> str:
    """Frozen privacy_reason schema (#166). Single source of truth — never build
    this string inline; both this and format_legacy_trigger encode tier+asset and
    must stay in lockstep."""
    return f"legacy-match[tier={tier},asset={asset_uuid}]: {classifier_reason}"


def format_legacy_trigger(asset_uuid: str, tier: str, classifier_version: int) -> str:
    """Frozen operation_log.trigger schema (#166). Single source of truth — the
    orchestrator builds the string here and hands db the finished value, so the
    db layer carries no format literal and the two provenance strings can't drift."""
    return f"legacy:{asset_uuid} tier={tier} clf={classifier_version}"


def resolve_apply_decision(
    photo: dict,
    candidates: list[dict],
    zones: list[dict],
    self_name: str = "",
    person_policies: dict[str, str] | None = None,
) -> dict | None:
    """Decide whether/how to reclassify a candidate_public photo from its legacy
    match. Returns {tier, state, asset_uuid, reason} when the photo should be
    demoted, else None (no-match, ambiguous-mixed, or stays candidate_public).
    """
    from analyzer.privacy import classify

    tier, matches = classify_match(photo, candidates)
    if tier == NO_MATCH:
        return None
    if tier == AMBIGUOUS and not all(is_people_positive(c) for c in matches):
        return None

    ordered = sorted(matches, key=lambda c: str(c.get("asset_uuid", "")))
    best: tuple[int, str, str, str] | None = None
    for c in ordered:
        state, reason = classify(
            shape_legacy_for_classify(c),
            zones,
            self_name=self_name,
            person_policies=person_policies or {},
        )
        rank = CLASSIFIER_PRECEDENCE.get(state, 99)
        if best is None or rank < best[0]:
            best = (rank, state, reason, str(c.get("asset_uuid", "")))

    assert best is not None  # matches is non-empty for confident/ambiguous tiers
    _, state, reason, asset_uuid = best
    if state == "candidate_public":
        return None
    return {
        "tier": tier,
        "state": state,
        "asset_uuid": asset_uuid,
        "reason": format_legacy_reason(tier, asset_uuid, reason),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_match_legacy_apply.py -k "resolve or confident or ambiguous or no_match or geofenced" -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
git add poller/legacy_match.py tests/test_match_legacy_apply.py
git commit -m "feat(#166): resolve_apply_decision with tier gating + precedence"
```

---

### Task 5: Atomic DB write (`reclassify_legacy_match`)

Writes `privacy_state` and the `operation_log` row in one transaction so an interruption leaves both or neither.

**Files:**
- Modify: `db/db.py` (add method near `set_privacy_state`, around line 577)
- Test: `tests/test_match_legacy_apply.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_match_legacy_apply.py`:

```python
import sqlite3
import tempfile


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
        1, "needs_review",
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
        db.reclassify_legacy_match(1, "needs_review", "x",
                                   trigger="legacy:A tier=confident clf=1")
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_match_legacy_apply.py -k reclassify -v`
Expected: FAIL with `AttributeError: 'Database' object has no attribute 'reclassify_legacy_match'`

- [ ] **Step 3: Implement the method**

In `db/db.py`, immediately after `set_privacy_state` (after line 577), add:

```python
    def reclassify_legacy_match(
        self,
        photo_id: int,
        new_state: str,
        reason: str,
        *,
        trigger: str,
    ) -> None:
        """Atomically set privacy_state and append the audit row (one txn).

        `reason` and `trigger` are pre-formatted by the caller via
        legacy_match.format_legacy_reason / format_legacy_trigger — this method
        carries no format literal, so the two provenance strings stay in lockstep
        at their single source. Unlike log_operation (fire-and-forget), an
        audit-write failure here rolls the whole reclassification back — never a
        state change without its audit trail.
        """
        now = _now_iso()
        with self.conn:
            self.conn.execute(
                "UPDATE photos SET privacy_state = ?, privacy_reason = ?, "
                "date_synced = ?, updated_at = ? WHERE id = ?",
                (new_state, reason, now, now, photo_id),
            )
            self.conn.execute(
                "INSERT INTO operation_log "
                "(occurred_at, photo_id, operation, target, old_value, "
                " new_value, trigger, actor) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (now, photo_id, "match_legacy_apply", "privacy_state",
                 "candidate_public", new_state, trigger, "bp"),
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_match_legacy_apply.py -k reclassify -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add db/db.py tests/test_match_legacy_apply.py
git commit -m "feat(#166): atomic reclassify_legacy_match (state + audit)"
```

---

### Task 6: Orchestration (`apply_legacy_matches`)

Queries eligible photos, builds the wall-clock index, calls `resolve_apply_decision`, persists via `reclassify_legacy_match`, and returns counts. The `WHERE privacy_state = 'candidate_public'` clause is the eligibility guard — human-reviewed photos are never selected.

**Files:**
- Create: `poller/legacy_apply.py`
- Test: `tests/test_match_legacy_apply.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_match_legacy_apply.py`:

```python
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


def _seed_photo(db, pid, flickr_id, state="candidate_public",
                date_taken="2010-06-01 12:00:00"):
    db.conn.execute(
        "INSERT INTO photos (id, uuid, flickr_id, privacy_state, privacy_reason, "
        "date_taken, width, height, flickr_title) "
        "VALUES (?, NULL, ?, ?, 'no people detected', ?, 4000, 3000, '')",
        (pid, flickr_id, state, date_taken),
    )
    db.conn.commit()


def _seed_asset(db, asset_uuid, **over):
    row = {"library_uuid": "L", "asset_uuid": asset_uuid,
           "original_filename": "img.jpg", "fingerprint": "fp",
           "date_taken": "2010-06-01T12:00:00-00:00", "width": 4000, "height": 3000,
           "latitude": None, "longitude": None, "title": "", "description": None,
           "keywords": "[]", "labels": "[]", "persons": "[]",
           "named_face_count": 0, "unknown_face_count": 0,
           "master_rel_path": "m.jpg", "thumbnail_cache_key": asset_uuid,
           "thumbnail_status": "ok"}
    row.update(over)
    db.upsert_legacy_asset(row)


def test_apply_demotes_matched_people_photo():
    from legacy_apply import apply_legacy_matches

    db = _orch_db()
    _seed_photo(db, 1, "100")
    _seed_asset(db, "A", persons='["Aunt May"]', named_face_count=1)
    counts = apply_legacy_matches(db, "L", self_name="Me", zones=[],
                                  person_policies={}, classifier_version=1)
    assert counts["reclassified"] == 1
    assert counts["needs_review"] == 1
    assert counts["auto_private"] == 0
    state = db.conn.execute(
        "SELECT privacy_state FROM photos WHERE id = 1"
    ).fetchone()["privacy_state"]
    assert state == "needs_review"


def test_apply_leaves_people_free_match_unchanged():
    from legacy_apply import apply_legacy_matches

    db = _orch_db()
    _seed_photo(db, 1, "100")
    _seed_asset(db, "A")  # no people signal
    counts = apply_legacy_matches(db, "L", self_name="Me", zones=[],
                                  person_policies={}, classifier_version=1)
    assert counts["reclassified"] == 0
    assert counts["unchanged"] == 1
    state = db.conn.execute(
        "SELECT privacy_state FROM photos WHERE id = 1"
    ).fetchone()["privacy_state"]
    assert state == "candidate_public"


def test_apply_never_touches_human_reviewed_photo():
    from legacy_apply import apply_legacy_matches

    db = _orch_db()
    _seed_photo(db, 1, "100", state="approved_public")
    _seed_asset(db, "A", persons='["Aunt May"]', named_face_count=1,
                latitude=10.0, longitude=20.0)
    zones = [{"name": "home", "label": "home", "latitude": 10.0,
              "longitude": 20.0, "radius_m": 100.0, "policy": "auto_private"}]
    counts = apply_legacy_matches(db, "L", self_name="Me", zones=zones,
                                  person_policies={}, classifier_version=1)
    assert counts["reclassified"] == 0
    state = db.conn.execute(
        "SELECT privacy_state FROM photos WHERE id = 1"
    ).fetchone()["privacy_state"]
    assert state == "approved_public"
    logs = db.conn.execute(
        "SELECT COUNT(*) AS n FROM operation_log WHERE photo_id = 1"
    ).fetchone()["n"]
    assert logs == 0


def test_apply_is_idempotent():
    from legacy_apply import apply_legacy_matches

    db = _orch_db()
    _seed_photo(db, 1, "100")
    _seed_asset(db, "A", persons='["Aunt May"]', named_face_count=1)
    first = apply_legacy_matches(db, "L", self_name="Me", zones=[],
                                 person_policies={}, classifier_version=1)
    second = apply_legacy_matches(db, "L", self_name="Me", zones=[],
                                  person_policies={}, classifier_version=1)
    assert first["reclassified"] == 1
    assert second["reclassified"] == 0
    logs = db.conn.execute(
        "SELECT COUNT(*) AS n FROM operation_log WHERE photo_id = 1"
    ).fetchone()["n"]
    assert logs == 1


def test_apply_counts_contract_invariants():
    from legacy_apply import apply_legacy_matches

    db = _orch_db()
    _seed_photo(db, 1, "100")                    # -> reclassified (people)
    _seed_asset(db, "A", persons='["Aunt May"]', named_face_count=1)
    _seed_photo(db, 2, "200", date_taken="2012-01-01 09:00:00")  # -> unchanged
    _seed_asset(db, "B", date_taken="2012-01-01T09:00:00-00:00")  # no signal
    counts = apply_legacy_matches(db, "L", self_name="Me", zones=[],
                                  person_policies={}, classifier_version=1)
    assert set(counts) == {"eligible", "reclassified", "needs_review",
                           "auto_private", "unchanged", "failed"}
    assert counts["eligible"] == 2
    assert counts["reclassified"] + counts["unchanged"] + counts["failed"] \
        == counts["eligible"]
    assert counts["needs_review"] + counts["auto_private"] == counts["reclassified"]


def test_apply_isolates_per_photo_failure_and_continues(monkeypatch):
    from legacy_apply import apply_legacy_matches

    db = _orch_db()
    _seed_photo(db, 1, "100")
    _seed_asset(db, "A", persons='["Aunt May"]', named_face_count=1)
    _seed_photo(db, 2, "200", date_taken="2012-01-01 09:00:00")
    _seed_asset(db, "B", persons='["Uncle Ben"]', named_face_count=1,
                date_taken="2012-01-01T09:00:00-00:00")

    real = db.reclassify_legacy_match
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")  # first photo's write fails
        return real(*a, **k)

    monkeypatch.setattr(db, "reclassify_legacy_match", flaky)
    counts = apply_legacy_matches(db, "L", self_name="Me", zones=[],
                                  person_policies={}, classifier_version=1)
    assert counts["failed"] == 1
    assert counts["reclassified"] == 1   # second photo still processed
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
    _seed_asset(db, "B", persons='["Uncle Ben"]', named_face_count=1,
                date_taken="2012-01-01T09:00:00-00:00")

    real = db.reclassify_legacy_match

    def fail_photo_2(photo_id, *a, **k):
        if photo_id == 2:
            raise RuntimeError("boom")
        return real(photo_id, *a, **k)

    # First pass: A succeeds, B fails.
    monkeypatch.setattr(db, "reclassify_legacy_match", fail_photo_2)
    first = apply_legacy_matches(db, "L", self_name="Me", zones=[],
                                 person_policies={}, classifier_version=1)
    assert first["reclassified"] == 1 and first["failed"] == 1

    # Second pass: patch removed. Only B is still candidate_public, so only B is
    # attempted; A was demoted and is no longer re-seen.
    monkeypatch.setattr(db, "reclassify_legacy_match", real)
    second = apply_legacy_matches(db, "L", self_name="Me", zones=[],
                                  person_policies={}, classifier_version=1)
    assert second["eligible"] == 1      # only B remains candidate_public
    assert second["reclassified"] == 1 and second["failed"] == 0
    states = dict(db.conn.execute(
        "SELECT id, privacy_state FROM photos"
    ).fetchall())
    assert states[1] == "needs_review" and states[2] == "needs_review"
    # Each photo logged exactly once across both passes (no dup for A).
    logs = dict(db.conn.execute(
        "SELECT photo_id, COUNT(*) AS n FROM operation_log GROUP BY photo_id"
    ).fetchall())
    assert logs == {1: 1, 2: 1}


def _assert_pure_noop(db, pid, updated_at_before):
    """A no-op must touch neither photos nor operation_log for this photo."""
    row = db.conn.execute(
        "SELECT privacy_state, privacy_reason, updated_at FROM photos WHERE id = ?",
        (pid,),
    ).fetchone()
    assert row["privacy_state"] == "candidate_public"
    assert row["privacy_reason"] == "no people detected"   # byte-for-byte unchanged
    assert row["updated_at"] == updated_at_before          # true no-op: no touch
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
    before = db.conn.execute(
        "SELECT updated_at FROM photos WHERE id = 1"
    ).fetchone()["updated_at"]
    counts = apply_legacy_matches(db, "L", self_name="Me", zones=[],
                                  person_policies={}, classifier_version=1)
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
    before = db.conn.execute(
        "SELECT updated_at FROM photos WHERE id = 1"
    ).fetchone()["updated_at"]
    counts = apply_legacy_matches(db, "L", self_name="Me", zones=[],
                                  person_policies={}, classifier_version=1)
    assert counts["unchanged"] == 1
    assert counts["reclassified"] == 0
    _assert_pure_noop(db, 1, before)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_match_legacy_apply.py -k apply -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'legacy_apply'`

- [ ] **Step 3: Create `poller/legacy_apply.py`**

```python
# poller/legacy_apply.py
"""Apply legacy matches: demote matched Flickr-only candidate_public photos
out of the publish-candidate queue using the shared privacy classifier (#166).

Pure orchestration over db + legacy_match decision logic; no osxphotos, no NAS.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from legacy_match import (  # noqa: E402
    format_legacy_trigger,
    normalise_wall_clock,
    resolve_apply_decision,
)


def apply_legacy_matches(
    db,
    library_uuid: str,
    *,
    self_name: str,
    zones: list[dict],
    person_policies: dict[str, str],
    classifier_version: int,
) -> dict:
    """Reclassify eligible (candidate_public, Flickr-only) photos from their
    legacy matches. Returns the frozen counts dict (#166):
        {eligible, reclassified, needs_review, auto_private, unchanged, failed}

    `classifier_version` is captured once by the caller and threaded unchanged to
    every photo here — never re-read per photo — so one run is internally
    consistent and version monkeypatching in tests is deterministic.

    Atomicity is per photo (db.reclassify_legacy_match commits or rolls back a
    single photo). A photo whose write raises is rolled back, counted under
    `failed`, and the run continues — one bad row never aborts the batch. The
    operation is idempotent, so a failed photo is retried on the next pass.

    Invariants: reclassified + unchanged + failed == eligible, and
    needs_review + auto_private == reclassified.
    """
    by_date: dict[str, list[dict]] = defaultdict(list)
    for asset in db.iter_legacy_assets(library_uuid):
        norm = normalise_wall_clock(asset.get("date_taken"))
        if norm:
            by_date[norm].append(asset)

    photos = db.conn.execute(
        "SELECT id, flickr_id, date_taken, width, height, flickr_title "
        "FROM photos WHERE uuid IS NULL AND privacy_state = 'candidate_public'"
    ).fetchall()

    counts = {"eligible": len(photos), "reclassified": 0, "needs_review": 0,
              "auto_private": 0, "unchanged": 0, "failed": 0}
    for p in photos:
        photo = dict(p)
        norm = normalise_wall_clock(photo.get("date_taken"))
        candidates = by_date.get(norm, []) if norm else []
        decision = resolve_apply_decision(
            photo, candidates, zones,
            self_name=self_name, person_policies=person_policies,
        )
        if decision is None:
            counts["unchanged"] += 1
            continue
        trigger = format_legacy_trigger(
            decision["asset_uuid"], decision["tier"], classifier_version,
        )
        try:
            db.reclassify_legacy_match(
                photo["id"], decision["state"], decision["reason"],
                trigger=trigger,
            )
        except Exception:
            # Per-photo atomicity: the failed write already rolled back. Isolate
            # the failure, count it, and keep processing later photos (#166).
            counts["failed"] += 1
            continue
        counts["reclassified"] += 1
        counts[decision["state"]] += 1

    return counts
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_match_legacy_apply.py -k apply -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
git add poller/legacy_apply.py tests/test_match_legacy_apply.py
git commit -m "feat(#166): apply_legacy_matches orchestration"
```

---

### Task 7: CLI — rename `match-legacy-preview` → `match-legacy` + `--apply`

**Files:**
- Modify: `bp` (`cmd_match_legacy_preview` → `cmd_match_legacy` at line 835; parser at lines 1209-1217; dispatch at line 1278; arg defaults around line 1244)

- [ ] **Step 1: Rename the handler and add the apply branch**

In `bp`, rename `def cmd_match_legacy_preview(args)` to `def cmd_match_legacy(args)`. Update its imports line to also pull in the orchestration and add the apply branch. Replace the import line:

```python
    from legacy_match import normalise_wall_clock, order_rows, preview_rows
```

with:

```python
    from legacy_match import normalise_wall_clock, order_rows, preview_rows
    from legacy_apply import apply_legacy_matches
    from analyzer.privacy import CLASSIFIER_VERSION
```

Then, immediately before the `finally:` of that function (after the preview `print(...)` / CSV block, i.e. after the existing `if args.csv:` block ends), add:

```python
        if getattr(args, "apply", False):
            self_name = config.get("photos_library", {}).get("self_name", "")
            zones = db.active_zones()
            person_policies = db.get_person_policies()
            counts = apply_legacy_matches(
                db, library_uuid,
                self_name=self_name, zones=zones,
                person_policies=person_policies,
                classifier_version=CLASSIFIER_VERSION,
            )
            print("Applied legacy reclassification:")
            print(f"  eligible     : {counts['eligible']} candidate_public photos evaluated")
            print(f"  reclassified : {counts['reclassified']} photos moved out of candidate_public")
            print(f"    needs_review : {counts['needs_review']}")
            print(f"    auto_private : {counts['auto_private']}")
            print(f"  unchanged    : {counts['unchanged']}")
            print(f"  failed       : {counts['failed']} (rolled back and skipped)")
```

- [ ] **Step 2: Rename the subcommand parser**

In `bp` (around lines 1209-1217), replace:

```python
    # match-legacy-preview
    p_mlp = sub.add_parser(
        "match-legacy-preview",
        help="Report likely matches between legacy assets and Flickr-only candidate_public photos (no writes)",
    )
    p_mlp.add_argument("--library-uuid", default=None,
                       help="Which indexed library to match against (default: the most recently indexed)")
    p_mlp.add_argument("--csv", default=None, metavar="PATH",
                       help="Also write the full tiered report to a CSV file")
```

with:

```python
    # match-legacy
    p_mlp = sub.add_parser(
        "match-legacy",
        help="Match legacy assets to Flickr-only candidate_public photos; preview by default, --apply to reclassify",
    )
    p_mlp.add_argument("--library-uuid", default=None,
                       help="Which indexed library to match against (default: the most recently indexed)")
    p_mlp.add_argument("--csv", default=None, metavar="PATH",
                       help="Also write the full tiered report to a CSV file")
    p_mlp.add_argument("--apply", action="store_true",
                       help="Reclassify confident (and all-people ambiguous) matches out of the review queue")
```

- [ ] **Step 3: Update the dispatch table**

In `bp` (line 1278), replace:

```python
        "match-legacy-preview":  cmd_match_legacy_preview,
```

with:

```python
        "match-legacy":  cmd_match_legacy,
```

The `args.apply` default already exists (line 1244: `if not hasattr(args, "apply"): args.apply = False`) and `args.library_uuid` / `args.csv` defaults are present — no arg-default changes needed.

- [ ] **Step 4: Smoke-test the CLI wiring**

Run: `python bp match-legacy --help`
Expected: help text shows `--apply` and `--library-uuid` and `--csv`; no traceback.

Run: `python bp --help`
Expected: subcommand list shows `match-legacy` (and no longer `match-legacy-preview`).

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest tests/ -q`
Expected: all pass (including the new `tests/test_match_legacy_apply.py`).

- [ ] **Step 6: Commit**

```bash
git add bp
git commit -m "feat(#166): bp match-legacy --apply (rename from match-legacy-preview)"
```

---

### Task 8: Docs, lint, README

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-05-30-legacy-library-indexer-162-design.md` (cross-reference, optional)

- [ ] **Step 1: Update README**

Find the `match-legacy-preview` mention in `README.md`:

Run: `grep -n "match-legacy" README.md`

Replace the command name `match-legacy-preview` with `match-legacy`, and add one line documenting the apply mode, e.g.:

```markdown
- `bp match-legacy` — report which legacy-library assets match Flickr-only
  `candidate_public` photos (preview, no writes). Add `--apply` to reclassify
  confident (and all-people ambiguous) matches out of the review queue using
  the shared privacy classifier.
```

If `README.md` has no `match-legacy` mention, add the bullet above to the command list section.

- [ ] **Step 2: Run lint**

Run: `make lint`
Expected: mypy clean on `db/`, `poller/`, `analyzer/` (if covered), `bp`; ruff format + check pass. Fix any issues in the files touched by this plan (no bare `# type: ignore`).

- [ ] **Step 3: Run the full suite once more**

Run: `python -m pytest tests/ -q`
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add README.md docs/superpowers/specs/2026-05-30-legacy-library-indexer-162-design.md
git commit -m "docs(#166): document bp match-legacy --apply"
```

---

## Notes for the implementer

- **Python path:** `poller/` modules import siblings directly (`from legacy_match import ...`), not `from poller.legacy_match`. `legacy_match.py` and `legacy_apply.py` insert both `poller/` and the repo root onto `sys.path` so `from analyzer.privacy import ...` resolves.
- **Never read or print `config/config.yml`** — it holds secrets. The CLI only reads keys it needs (`database.path`, `thumbnails.path`, `photos_library.self_name`).
- **`config/config.yml` quoting:** the repo path contains a space; always quote it in shell commands.
- **No Flickr writes** anywhere in this work — `--apply` only edits local `privacy_state` and appends `operation_log` rows.
- **Branch + PR:** all work lands on `feat/match-legacy-apply-166`; open a PR and let the `test` check go green (main is protection-locked). Bump the version only on merge to main (target 1.5.0).
