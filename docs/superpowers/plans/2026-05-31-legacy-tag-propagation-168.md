# Legacy Metadata Propagation (#168) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When `bp match-legacy --apply` matches a Flickr-only `candidate_public` photo to a legacy asset, propagate the asset's keywords/labels into `proposed_tags` and its title/description into `proposed_title`/`proposed_description`, merging not clobbering, idempotently.

**Architecture:** Three layers mirroring #166. `poller/legacy_match.py` (pure) derives the payload and the audit trigger. `db/db.py` owns the merge + persistence + audit in one transaction (`apply_legacy_metadata`). `poller/legacy_apply.py` sequences it into the existing per-photo loop. A new `proposed_title` column is added the same way BP adds additive `photos` columns (schema.sql + guarded inline ALTER).

**Tech Stack:** Python 3, SQLite, pytest. Reuses `analyzer.tagger.propose_tags` (keyword/label normalisation, blocklist, remap).

**Spec:** `docs/superpowers/specs/2026-05-31-legacy-tag-propagation-168-design.md`

---

## File Structure

- `poller/legacy_match.py` — **modify.** Add `from analyzer.tagger import propose_tags`; add pure functions `legacy_metadata_payload(tier, matched_assets)` and `format_legacy_metadata_trigger(tier, matched_assets, classifier_version)`. Uses existing `CONFIDENT`, `_json_list`.
- `db/schema.sql` — **modify.** Add `proposed_title TEXT` next to `proposed_description`.
- `db/db.py` — **modify.** Add module-level `_decode_proposed_tags(raw) -> (list, bool)`; add guarded `proposed_title` ALTER in `_ensure_schema`; add method `apply_legacy_metadata(...)` next to `reclassify_legacy_match`.
- `poller/legacy_apply.py` — **modify.** Import the new helpers; add the metadata step + `metadata_attempted`/`metadata_applied`/`metadata_failed` counts in `apply_legacy_matches`.
- `bp` — **modify.** Print the three new count lines in `cmd_match_legacy`'s `--apply` branch.
- `tests/test_legacy_tag_propagation.py` — **create.** All new tests.
- `README.md` — **modify.** Note the new propagation behaviour.

---

## Task 1: Pure payload + trigger derivation (`legacy_match.py`)

**Files:**
- Modify: `poller/legacy_match.py` (import at line 27; new functions after `format_legacy_trigger`, ~line 199)
- Test: `tests/test_legacy_tag_propagation.py` (create)

- [ ] **Step 1: Create the test file with the standard header and the payload tests**

Create `tests/test_legacy_tag_propagation.py`:

```python
# tests/test_legacy_tag_propagation.py
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "poller"))


def test_payload_confident_takes_single_asset_tags():
    from legacy_match import CONFIDENT, legacy_metadata_payload

    asset = {"keywords": '["Beach", "Summer"]', "labels": '["sky"]', "title": "Trip", "description": "At the shore"}
    out = legacy_metadata_payload(CONFIDENT, [asset])
    assert out["add_tags"] == ["beach", "sky", "summer"]
    assert out["title"] == "Trip"
    assert out["description"] == "At the shore"


def test_payload_ambiguous_intersects_tags_and_drops_scalars():
    from legacy_match import AMBIGUOUS, legacy_metadata_payload

    a = {"keywords": '["beach", "birthday"]', "labels": "[]", "title": "A", "description": "da"}
    b = {"keywords": '["beach", "picnic"]', "labels": "[]", "title": "B", "description": "db"}
    out = legacy_metadata_payload(AMBIGUOUS, [a, b])
    assert out["add_tags"] == ["beach"]          # shared only
    assert out["title"] is None                   # scalars confident-only
    assert out["description"] is None


def test_payload_ambiguous_no_shared_tags_is_empty():
    from legacy_match import AMBIGUOUS, legacy_metadata_payload

    a = {"keywords": '["beach"]', "labels": "[]"}
    b = {"keywords": '["mountain"]', "labels": "[]"}
    out = legacy_metadata_payload(AMBIGUOUS, [a, b])
    assert out["add_tags"] == []


def test_payload_applies_label_blocklist_and_remap():
    from legacy_match import CONFIDENT, legacy_metadata_payload

    asset = {"keywords": "[]", "labels": '["people", "automobile"]', "title": "", "description": None}
    out = legacy_metadata_payload(CONFIDENT, [asset])
    assert "people" not in out["add_tags"]        # blocklisted
    assert "car" in out["add_tags"]               # automobile -> car
    assert out["title"] is None                   # "" -> None
    assert out["description"] is None             # None -> None


def test_payload_empty_keywords_and_labels():
    from legacy_match import CONFIDENT, legacy_metadata_payload

    out = legacy_metadata_payload(CONFIDENT, [{"keywords": "[]", "labels": "[]", "title": "T", "description": ""}])
    assert out["add_tags"] == []
    assert out["title"] == "T"
    assert out["description"] is None             # whitespace/"" -> None
```

- [ ] **Step 2: Run the payload tests to verify they fail**

Run: `cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/test_legacy_tag_propagation.py -q`
Expected: FAIL with `ImportError: cannot import name 'legacy_metadata_payload'`.

- [ ] **Step 3: Add the `propose_tags` import**

In `poller/legacy_match.py`, the existing import at line 27 is:
```python
from analyzer.privacy import PEOPLE_LABELS  # noqa: E402
```
Add directly below it:
```python
from analyzer.tagger import propose_tags  # noqa: E402
```

- [ ] **Step 4: Implement `legacy_metadata_payload`**

In `poller/legacy_match.py`, after `format_legacy_trigger` (ends ~line 199), add:

```python
def legacy_metadata_payload(tier: str, matched_assets: list[dict]) -> dict:
    """Build the legacy-derived staging payload for a matched photo.

    Contract (mirrors classify_match output): confident => exactly one asset;
    ambiguous => two or more; no-match never calls this. The empty-guard is
    defensive only.

    add_tags: propose_tags() per asset, then combined by confidence — we branch
    on tier explicitly rather than relying on cardinality. CONFIDENT yields the
    single asset's tags (tag_sets[0]); AMBIGUOUS (N>=2) yields the INTERSECTION
    (tags shared by every candidate), so an uncertain match can't pull
    event-specific tags from the wrong photo. Branching on tier (not "len==1 so
    intersection happens to work") means a future classifier that returns
    CONFIDENT with >1 asset won't silently switch to intersection semantics.
    Sorted/deduped/lowercased by propose_tags. NOT merged with the photo's
    existing proposed_tags (db does that). title/description only for confident
    matches; None otherwise.
    """
    tag_sets = []
    for asset in matched_assets:
        shaped = {
            "keywords": _json_list(asset.get("keywords")),
            "labels": _json_list(asset.get("labels")),
        }
        tag_sets.append(set(propose_tags(shaped)))

    if not tag_sets:
        tags: set = set()
    elif tier == CONFIDENT:
        tags = tag_sets[0]
    else:
        tags = set.intersection(*tag_sets)

    title = None
    description = None
    if tier == CONFIDENT and matched_assets:
        asset = matched_assets[0]
        title = (asset.get("title") or "").strip() or None
        description = (asset.get("description") or "").strip() or None

    return {"add_tags": sorted(tags), "title": title, "description": description}
```

- [ ] **Step 5: Run the payload tests to verify they pass**

Run: `cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/test_legacy_tag_propagation.py -q`
Expected: PASS (5 tests).

- [ ] **Step 6: Add the trigger tests**

Append to `tests/test_legacy_tag_propagation.py`:

```python
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
```

- [ ] **Step 7: Run the trigger tests to verify they fail**

Run: `cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/test_legacy_tag_propagation.py -q`
Expected: FAIL with `ImportError: cannot import name 'format_legacy_metadata_trigger'`.

- [ ] **Step 8: Implement `format_legacy_metadata_trigger`**

In `poller/legacy_match.py`, directly after `legacy_metadata_payload`, add:

```python
def format_legacy_metadata_trigger(
    tier: str, matched_assets: list[dict], classifier_version: int
) -> str:
    """operation_log.trigger for a metadata propagation write. Confident names
    the single source asset; ambiguous records only the candidate count (tags
    are an intersection over N assets — naming one would misattribute)."""
    if tier == CONFIDENT and matched_assets:
        uuid = str(matched_assets[0].get("asset_uuid", ""))
        return f"legacy-meta:{uuid} tier={tier} clf={classifier_version}"
    return f"legacy-meta:ambiguous tier={tier} n={len(matched_assets)} clf={classifier_version}"
```

- [ ] **Step 9: Run all Task 1 tests to verify they pass**

Run: `cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/test_legacy_tag_propagation.py -q`
Expected: PASS (7 tests).

- [ ] **Step 10: Lint and commit**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain"
make lint
git add poller/legacy_match.py tests/test_legacy_tag_propagation.py
git commit -m "$(cat <<'EOF'
feat(#168): derive legacy metadata payload + audit trigger

Pure legacy_metadata_payload (confident=single asset tags, ambiguous=tag
intersection, scalars confident-only) and format_legacy_metadata_trigger
(asset uuid for confident, candidate count for ambiguous).

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Add `proposed_title` column

**Files:**
- Modify: `db/schema.sql:91` (next to `proposed_description`)
- Modify: `db/db.py` `_ensure_schema` (additive block ~line 410, after the `bp_rating` guard ~line 424)
- Test: `tests/test_legacy_tag_propagation.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_legacy_tag_propagation.py`:

```python
def test_photos_table_has_proposed_title_column():
    from db.db import Database

    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    db = Database(Path(f.name))
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(photos)").fetchall()}
    assert "proposed_title" in cols
    assert "proposed_description" in cols  # sanity: existing column still there
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/test_legacy_tag_propagation.py::test_photos_table_has_proposed_title_column -q`
Expected: FAIL — `assert 'proposed_title' in cols` (column absent).

- [ ] **Step 3: Add the column to schema.sql**

In `db/schema.sql`, line 91 currently reads:
```sql
    proposed_description    TEXT,                   -- draft description text (may be AI caption, edited)
```
Add a line directly above it:
```sql
    proposed_title          TEXT,                   -- draft title text (e.g. propagated legacy title, edited)
```

- [ ] **Step 4: Add the guarded inline ALTER in `_ensure_schema`**

In `db/db.py`, in `_ensure_schema`, after the `bp_rating` guard (the block ending ~line 424, before the `photo_albums` `pa_cols` block), insert — matching the existing per-column idiom exactly:

```python
        existing = {r[1] for r in self.conn.execute("PRAGMA table_info(photos)").fetchall()}
        if "proposed_title" not in existing:
            self.conn.execute("ALTER TABLE photos ADD COLUMN proposed_title TEXT")
            self.conn.commit()
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/test_legacy_tag_propagation.py::test_photos_table_has_proposed_title_column -q`
Expected: PASS.

- [ ] **Step 6: Lint and commit**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain"
make lint
git add db/schema.sql db/db.py tests/test_legacy_tag_propagation.py
git commit -m "$(cat <<'EOF'
feat(#168): add proposed_title staging column

schema.sql + guarded inline ALTER in _ensure_schema (BP's additive-column
idiom), so every Database() construction gains it. Mirrors proposed_description.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: `db.apply_legacy_metadata` + decode helper

**Files:**
- Modify: `db/db.py` — module-level `_decode_proposed_tags` (near `_json_loads_safe` ~line 44); method `apply_legacy_metadata` after `reclassify_legacy_match` (~line 618)
- Test: `tests/test_legacy_tag_propagation.py`

- [ ] **Step 1: Write the failing tests (merge, scalars, idempotency, malformed, audit, atomicity)**

Append to `tests/test_legacy_tag_propagation.py`:

```python
def _meta_db():
    """Fresh Database (has proposed_title via _ensure_schema) + operation_log +
    one Flickr-only candidate_public photo (id=1, empty proposed_* fields)."""
    from db.db import Database
    from db.migrations.migrate_020_operation_log import run as run_op_log

    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    db = Database(Path(f.name))
    run_op_log(str(f.name))
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
        1, add_tags=["beach", "summer"], title="Trip", description="At the shore",
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
    db.conn.execute("UPDATE photos SET proposed_tags = ? WHERE id = 1", (json.dumps(["beach", "old"]),))
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
    changed = db.apply_legacy_metadata(1, add_tags=[], title="Legacy", description="legacy desc", trigger="t")
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
    assert db.conn.execute("SELECT proposed_title FROM photos WHERE id = 1").fetchone()["proposed_title"] == "Legacy"


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
    assert json.loads(
        db.conn.execute("SELECT proposed_tags FROM photos WHERE id = 1").fetchone()["proposed_tags"]
    ) == []
    nv = json.loads(_logs(db)[0]["new_value"])
    assert nv["tags_repaired"] is True


def test_apply_metadata_list_with_non_string_members_is_repaired():
    db = _meta_db()
    db.conn.execute("UPDATE photos SET proposed_tags = ? WHERE id = 1", (json.dumps(["beach", 1, None]),))
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/test_legacy_tag_propagation.py -q -k apply_metadata`
Expected: FAIL — `AttributeError: 'Database' object has no attribute 'apply_legacy_metadata'`.

- [ ] **Step 3: Add the `_decode_proposed_tags` module helper**

In `db/db.py`, directly after `_json_loads_safe` (ends ~line 43), add:

```python
def _decode_proposed_tags(raw: str | None) -> tuple[list[str], bool]:
    """Decode a stored proposed_tags JSON string to (tags, was_malformed).

    NULL/blank -> ([], False). A decode error or a non-list value (bare string,
    dict, number) -> ([], True): malformed historical data we will repair in
    place. A valid list -> keep only string members; if any non-string members
    (numbers, nulls, nested objects) were dropped, flag was_malformed=True so the
    dirty row is rewritten rather than left in place. We do NOT str()-coerce
    non-strings: that would turn None into the literal tag "none". A clean
    list-of-strings -> (members, False).
    """
    if not raw:
        return [], False
    try:
        val = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return [], True
    if isinstance(val, list):
        clean = [x for x in val if isinstance(x, str)]
        return clean, len(clean) != len(val)
    return [], True
```

- [ ] **Step 4: Implement `apply_legacy_metadata`**

In `db/db.py`, directly after the `reclassify_legacy_match` method (ends ~line 617, before the `# Star ratings` comment ~line 619), add:

```python
    def apply_legacy_metadata(
        self,
        photo_id: int,
        *,
        add_tags: list[str],
        title: str | None = None,
        description: str | None = None,
        trigger: str,
    ) -> bool:
        """Stage propagated legacy metadata for one photo (one txn).

        proposed_tags: set-union of add_tags into the photo's existing tags
        (decoded via _decode_proposed_tags; non-list/malformed values are
        repaired in place). proposed_title / proposed_description: filled only
        when currently empty (NULL or whitespace-only) and the incoming value is
        non-empty — never clobbers a human draft. Writes ONE aggregate
        operation_log row iff something changed; new_value.fields is in schema
        order, tags_added is the delta, tags_repaired flags a repaired malformed
        row. Returns True iff anything changed.
        """
        row = self.conn.execute(
            "SELECT proposed_tags, proposed_title, proposed_description FROM photos WHERE id = ?",
            (photo_id,),
        ).fetchone()
        current, malformed = _decode_proposed_tags(row["proposed_tags"])
        merged = sorted(set(current) | {t for t in add_tags if t})
        tags_changed = merged != current or malformed

        sets: list[str] = []
        params: list = []
        changed_fields: list[str] = []
        if tags_changed:
            changed_fields.append("proposed_tags")
            sets.append("proposed_tags = ?")
            params.append(json.dumps(merged))
        if title and not (row["proposed_title"] or "").strip():
            changed_fields.append("proposed_title")
            sets.append("proposed_title = ?")
            params.append(title)
        if description and not (row["proposed_description"] or "").strip():
            changed_fields.append("proposed_description")
            sets.append("proposed_description = ?")
            params.append(description)

        if not changed_fields:
            return False

        new_value: dict = {
            "fields": [
                f
                for f in ("proposed_tags", "proposed_title", "proposed_description")
                if f in changed_fields
            ]
        }
        if tags_changed:
            # "new members introduced", not a length delta: current may contain
            # duplicates (dirty historical rows), so len(merged)-len(current) can
            # be wrong or negative. Count tags in merged that weren't in current.
            added = len(set(merged) - set(current))
            if added > 0:
                new_value["tags_added"] = added
            if malformed:
                new_value["tags_repaired"] = True

        now = _now_iso()
        with self.conn:
            self.conn.execute(
                f"UPDATE photos SET {', '.join(sets)}, updated_at = ? WHERE id = ?",
                (*params, now, photo_id),
            )
            self.conn.execute(
                "INSERT INTO operation_log "
                "(occurred_at, photo_id, operation, target, old_value, "
                " new_value, trigger, actor) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    now,
                    photo_id,
                    "match_legacy_metadata",
                    "legacy_metadata",
                    None,
                    json.dumps(new_value),
                    trigger,
                    "bp",
                ),
            )
        return True
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/test_legacy_tag_propagation.py -q -k apply_metadata`
Expected: PASS (11 tests).

- [ ] **Step 6: Lint and commit**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain"
make lint
git add db/db.py tests/test_legacy_tag_propagation.py
git commit -m "$(cat <<'EOF'
feat(#168): db.apply_legacy_metadata — merge tags, fill scalars, audit

Set-union proposed_tags (repair-in-place for malformed rows, tags_repaired
audit flag), fill empty proposed_title/proposed_description, one aggregate
operation_log row per write in a single txn. Adds _decode_proposed_tags.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Wire into `apply_legacy_matches` orchestration

**Files:**
- Modify: `poller/legacy_apply.py` (imports ~line 16; counts dict ~line 59; per-photo loop ~line 67)
- Test: `tests/test_legacy_tag_propagation.py`

- [ ] **Step 1: Write the failing orchestration tests**

Append to `tests/test_legacy_tag_propagation.py`:

```python
def _orch_db_168():
    """Database with operation_log + legacy_index migrations and a legacy library."""
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
        "library_uuid": "L", "asset_uuid": asset_uuid, "original_filename": "img.jpg",
        "fingerprint": "fp", "date_taken": "2010-06-01T12:00:00-00:00", "width": 4000,
        "height": 3000, "latitude": None, "longitude": None, "title": "", "description": None,
        "keywords": "[]", "labels": "[]", "persons": "[]", "named_face_count": 0,
        "unknown_face_count": 0, "master_rel_path": "m.jpg", "thumbnail_cache_key": asset_uuid,
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
    assert counts["reclassified"] == 0       # no people => stays public
    assert counts["unchanged"] == 1          # privacy unchanged
    assert counts["metadata_attempted"] == 1
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
    assert counts["metadata_attempted"] == 1
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
    assert counts["metadata_attempted"] == 0
    assert counts["metadata_applied"] == 0
    assert db.conn.execute("SELECT proposed_tags FROM photos WHERE id = 1").fetchone()["proposed_tags"] is None


def test_orch_idempotent_rerun_no_duplicate_tags_or_logs():
    from legacy_apply import apply_legacy_matches

    db = _orch_db_168()
    _seed_photo_168(db, 1, "100")
    _seed_asset_168(db, "A", keywords='["beach"]', title="T")
    first = apply_legacy_matches(db, "L", self_name="Me", zones=[], person_policies={}, classifier_version=1)
    second = apply_legacy_matches(db, "L", self_name="Me", zones=[], person_policies={}, classifier_version=1)
    assert first["metadata_applied"] == 1
    assert second["metadata_attempted"] == 1   # still matches
    assert second["metadata_applied"] == 0     # nothing left to change
    assert json.loads(
        db.conn.execute("SELECT proposed_tags FROM photos WHERE id = 1").fetchone()["proposed_tags"]
    ) == ["beach"]
    meta_logs = [r for r in _logs(db) if r["operation"] == "match_legacy_metadata"]
    assert len(meta_logs) == 1  # only the first run logged


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
    assert counts["metadata_attempted"] == 1
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
    assert counts["failed"] == 1            # demotion failed and rolled back
    assert counts["reclassified"] == 0
    assert counts["metadata_attempted"] == 1
    assert counts["metadata_applied"] == 1  # metadata still applied
    assert json.loads(
        db.conn.execute("SELECT proposed_tags FROM photos WHERE id = 1").fetchone()["proposed_tags"]
    ) == ["family"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/test_legacy_tag_propagation.py -q -k orch`
Expected: FAIL with `KeyError: 'metadata_attempted'`.

- [ ] **Step 3: Add the new imports**

In `poller/legacy_apply.py`, the import block at lines 16-20 currently reads:
```python
from legacy_match import (  # noqa: E402
    format_legacy_trigger,
    normalise_wall_clock,
    resolve_apply_decision,
)
```
Replace it with:
```python
from legacy_match import (  # noqa: E402
    classify_match,
    format_legacy_metadata_trigger,
    format_legacy_trigger,
    legacy_metadata_payload,
    normalise_wall_clock,
    resolve_apply_decision,
)
```

- [ ] **Step 4: Add the new counts keys**

In `poller/legacy_apply.py`, the counts dict (lines 59-66) currently reads:
```python
    counts = {
        "eligible": len(photos),
        "reclassified": 0,
        "needs_review": 0,
        "auto_private": 0,
        "unchanged": 0,
        "failed": 0,
    }
```
Replace with:
```python
    counts = {
        "eligible": len(photos),
        "reclassified": 0,
        "needs_review": 0,
        "auto_private": 0,
        "unchanged": 0,
        "failed": 0,
        "metadata_attempted": 0,
        "metadata_applied": 0,
        "metadata_failed": 0,
    }
```

- [ ] **Step 5: Add the metadata step in the per-photo loop**

In `poller/legacy_apply.py`, the per-photo loop currently ends each iteration like this (lines 86-99):
```python
        try:
            db.reclassify_legacy_match(
                photo["id"],
                decision["state"],
                decision["reason"],
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

The demotion path uses `continue` on success-counting. The metadata step must run for every matched photo regardless of demotion, so it cannot live after a `continue`. Restructure the tail of the loop body so the demotion block no longer early-`continue`s past the metadata step. Replace the block above with:

```python
        if decision is not None:
            try:
                db.reclassify_legacy_match(
                    photo["id"],
                    decision["state"],
                    decision["reason"],
                    trigger=trigger,
                )
                counts["reclassified"] += 1
                counts[decision["state"]] += 1
            except Exception:
                # Per-photo atomicity: the failed write already rolled back.
                # Isolate, count, and keep processing later photos (#166).
                counts["failed"] += 1

        # Metadata propagation (#168) — independent of the demotion above; runs
        # for every photo with a legacy match, demoted or not. We deliberately
        # recompute classify_match here rather than reusing resolve_apply_decision:
        # `decision is None` covers BOTH no-match and matched-but-not-demoted, so
        # it can't tell us whether to propagate. classify_match is pure and cheap,
        # and recomputing keeps #166's resolve_apply_decision untouched.
        tier, matched = classify_match(photo, candidates)
        if matched:
            counts["metadata_attempted"] += 1
            payload = legacy_metadata_payload(tier, matched)
            meta_trigger = format_legacy_metadata_trigger(tier, matched, classifier_version)
            try:
                if db.apply_legacy_metadata(
                    photo["id"],
                    add_tags=payload["add_tags"],
                    title=payload["title"],
                    description=payload["description"],
                    trigger=meta_trigger,
                ):
                    counts["metadata_applied"] += 1
            except Exception:
                counts["metadata_failed"] += 1

    return counts
```

This also requires that the earlier `if decision is None: counts["unchanged"] += 1; continue` guard NOT skip the metadata step. Locate the existing block (lines 78-80):
```python
        if decision is None:
            counts["unchanged"] += 1
            continue
```
Replace it with (drop the `continue`, keep the count, let control fall through to the metadata step):
```python
        if decision is None:
            counts["unchanged"] += 1
        trigger = None  # set below only when there is a decision
```

Then guard the existing `trigger = format_legacy_trigger(...)` (lines 81-85) and the demotion under `if decision is not None:` as shown above. The final loop body should read, in order:
1. compute `decision`
2. `if decision is None: counts["unchanged"] += 1`
3. `if decision is not None:` build `trigger`, reclassify (try/except), count
4. metadata step (classify_match → attempted/applied/failed)

- [ ] **Step 6: Verify the demotion `trigger` is built inside the `decision is not None` branch**

Confirm the existing lines 81-85:
```python
        trigger = format_legacy_trigger(
            decision["asset_uuid"],
            decision["tier"],
            classifier_version,
        )
```
now live inside the `if decision is not None:` block (move them there if Step 5's restructure didn't already). The final structure:
```python
        decision = resolve_apply_decision(
            photo, candidates, zones, self_name=self_name, person_policies=person_policies,
        )
        if decision is None:
            counts["unchanged"] += 1
        if decision is not None:
            trigger = format_legacy_trigger(
                decision["asset_uuid"], decision["tier"], classifier_version
            )
            try:
                db.reclassify_legacy_match(
                    photo["id"], decision["state"], decision["reason"], trigger=trigger,
                )
                counts["reclassified"] += 1
                counts[decision["state"]] += 1
            except Exception:
                counts["failed"] += 1
        tier, matched = classify_match(photo, candidates)
        if matched:
            counts["metadata_attempted"] += 1
            payload = legacy_metadata_payload(tier, matched)
            meta_trigger = format_legacy_metadata_trigger(tier, matched, classifier_version)
            try:
                if db.apply_legacy_metadata(
                    photo["id"], add_tags=payload["add_tags"],
                    title=payload["title"], description=payload["description"],
                    trigger=meta_trigger,
                ):
                    counts["metadata_applied"] += 1
            except Exception:
                counts["metadata_failed"] += 1
```

- [ ] **Step 7: Run the new orchestration tests to verify they pass**

Run: `cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/test_legacy_tag_propagation.py -q -k orch`
Expected: PASS (6 tests).

- [ ] **Step 8: Run the full #166 apply suite to confirm no regression**

Run: `cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/test_match_legacy_apply.py -q`
Expected: PASS (all existing tests — the new counts keys are additive; existing assertions on `reclassified`/`unchanged`/`failed`/state are unaffected, and people-free matches produce empty payloads that change nothing).

- [ ] **Step 9: Lint and commit**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain"
make lint
git add poller/legacy_apply.py tests/test_legacy_tag_propagation.py
git commit -m "$(cat <<'EOF'
feat(#168): propagate legacy metadata in apply_legacy_matches

Metadata step runs for every photo with a legacy match (demoted or not),
independent of the privacy demotion (two txns). Adds metadata_attempted /
metadata_applied / metadata_failed counts.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: CLI summary lines

**Files:**
- Modify: `bp` `cmd_match_legacy` `--apply` branch (after line 981)

- [ ] **Step 1: Add the three print lines**

In `bp`, the `--apply` summary currently ends (lines 980-981):
```python
            print(f"  unchanged    : {counts['unchanged']}")
            print(f"  failed       : {counts['failed']} (rolled back and skipped)")
```
Add directly below line 981:
```python
            print(f"  metadata     : {counts['metadata_attempted']} matched, "
                  f"{counts['metadata_applied']} updated, "
                  f"{counts['metadata_failed']} failed")
```

- [ ] **Step 2: Verify the CLI still loads and the help works**

Run: `cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python bp match-legacy --help`
Expected: exits 0, help text lists `--apply` and `--csv`. (The count keys printed are guaranteed present by Task 4's tests, so a `KeyError` here is impossible.)

- [ ] **Step 3: Run the CLI registration tests**

Run: `cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/test_cli_legacy.py -q`
Expected: PASS (3 tests).

- [ ] **Step 4: Lint and commit**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain"
make lint
git add bp
git commit -m "$(cat <<'EOF'
feat(#168): show metadata propagation counts in match-legacy --apply summary

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Docs + full suite + close-out

**Files:**
- Modify: `README.md` (the `match-legacy` section, ~line 196)

- [ ] **Step 1: Update the README**

In `README.md`, find the `match-legacy` / `--apply` description (~lines 196-198) and add a sentence to the `--apply` description noting metadata propagation. Locate the existing `--apply` line and append, on its own line within that command's description:
```markdown
  On match, `--apply` also stages the matched legacy asset's keywords/labels into the
  photo's proposed tags and (for confident matches) fills the empty proposed title/description
  fields from the legacy asset — merged, de-duplicated, and idempotent. These are local
  review-staging fields only; nothing is pushed to Flickr.
```

- [ ] **Step 2: Run the full test suite**

Run: `cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && python -m pytest tests/ -q`
Expected: PASS — the entire suite green (previous baseline was 1697; this adds the new `test_legacy_tag_propagation.py` cases).

- [ ] **Step 3: Final lint**

Run: `cd "/Users/cdevers/Documents/GitHub/Blue Pearmain" && make lint`
Expected: clean (mypy + ruff, no errors on touched files).

- [ ] **Step 4: Commit the README**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain"
git add README.md
git commit -m "$(cat <<'EOF'
docs(#168): note legacy metadata propagation in match-legacy --apply

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 5: Push the branch and open a PR**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain"
git push -u origin feat/legacy-tag-propagation-168
gh pr create --title "feat(#168): propagate legacy keywords/tags into matched photos" --body "$(cat <<'EOF'
## Summary
- `match-legacy --apply` now stages matched legacy keywords+labels into `proposed_tags` (merge/dedupe/idempotent) and fills the empty `proposed_title`/`proposed_description` staging fields from confident matches.
- Ambiguous matches use tag **intersection** (shared tags only); scalars are confident-only.
- New `proposed_title` column; new `metadata_attempted`/`metadata_applied`/`metadata_failed` counts.
- No Flickr writes. Closes #168.

## Test plan
- [ ] `python -m pytest tests/ -q` green
- [ ] `make lint` clean
- [ ] `bp match-legacy --apply` summary shows the metadata line
EOF
)"
```

- [ ] **Step 6: After merge — version bump, close issue with retrospective**

(Per repo policy: bump on merge to main via branch+PR; post a retrospective comment on #168 — size estimate vs actual, files/lines/tasks.)

---

## Self-Review

**1. Spec coverage:**
- Keywords + labels via `propose_tags` → Task 1 (payload), `test_payload_applies_label_blocklist_and_remap`. ✓
- Confident = single-asset tags; ambiguous = intersection → Task 1 tests. ✓
- Title/description confident-only, fill-if-empty, whitespace=empty → Task 1 (None for ambiguous), Task 3 (`does_not_clobber`, `whitespace_existing`). ✓
- `proposed_title` via schema.sql + inline guard → Task 2. ✓
- DB-layer merge, no analyzer import, JSON decode/coerce, malformed repair + `tags_repaired` → Task 3 (`_decode_proposed_tags`, repair tests). Non-string list members are dropped (not str()-coerced) and flagged as a repair → `test_apply_metadata_list_with_non_string_members_is_repaired`; clean string lists are not flagged → `test_apply_metadata_clean_string_list_only_no_repair_flag`. ✓
- Aggregate audit row, `fields` schema order, `tags_added` as new-members-introduced (set difference, not length delta) → Task 3 tests. ✓
- Two independent writes; `reclassify_legacy_match` untouched; demotion failure does not block metadata → Task 4 (`demoted_..._reclassified_and_tagged`, `demotion_failure_does_not_block_metadata`, separate try/except blocks). ✓
- `legacy_metadata_payload` branches on tier explicitly (CONFIDENT → single asset, AMBIGUOUS → intersection) — not reliant on confident-implies-cardinality-1 → Task 1. ✓
- All matched photos (demoted or not) → Task 4 (`matched_not_demoted` test; restructured loop drops the `continue`). ✓
- Counts `metadata_attempted`/`applied`/`failed` + invariants → Task 4 tests. ✓
- Idempotency → Task 3 + Task 4 rerun tests. ✓
- CLI summary → Task 5. ✓
- README → Task 6. ✓

**2. Placeholder scan:** No TBD/TODO; every code step has complete code; every test has assertions. ✓

**3. Type consistency:** `legacy_metadata_payload(tier, matched_assets) -> {add_tags, title, description}` consistent across Tasks 1 & 4. `format_legacy_metadata_trigger(tier, matched_assets, classifier_version)` consistent. `apply_legacy_metadata(photo_id, *, add_tags, title, description, trigger) -> bool` consistent across Tasks 3 & 4. `_decode_proposed_tags(raw) -> (list, bool)` consistent. ✓

**Note for the implementer (Task 4):** the restructure of `apply_legacy_matches` is the one delicate step — the original code used `continue` after the `decision is None` and after the demotion write, which would skip the new metadata step. The plan removes both `continue`s and gates the demotion under `if decision is not None:`. Run the existing `tests/test_match_legacy_apply.py` (Task 4 Step 8) to confirm the #166 behaviour is preserved.
