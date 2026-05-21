# Per-Person Privacy Policy — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow the user to declare persistent "always private" rules for named people in Apple Photos. Photos containing a policy-protected person are classified `auto_private` at scan time — even for future arrivals — without requiring repeated manual batch actions.

**Architecture:** A `person_policies` table (migration 019) stores `(person_name, policy)` pairs. `db.py` gets three new methods (get/set/delete). `analyzer/privacy.py`'s `classify()` receives a `person_policies` dict and checks it immediately after the geofence step. `poller/scanner.py` loads policies from the DB and passes them through. The reviewer's Faces page gets a policy badge per person and a new `POST /api/person_policy` endpoint to toggle it.

**Tech Stack:** SQLite migration (same pattern as existing `migrate_018_*`), `analyzer/privacy.py` (pure logic, no I/O), Flask JSON endpoints, Jinja2 template update.

---

## File map

| Action | Path | Responsibility |
|--------|------|----------------|
| Create | `db/migrations/migrate_019_person_policies.py` | Add `person_policies` table |
| Modify | `db/db.py` | `get_person_policies()`, `set_person_policy()`, `delete_person_policy()` |
| Modify | `analyzer/privacy.py` | `classify()` gains `person_policies` param |
| Modify | `poller/scanner.py` | Load policies from DB, pass to `classify()` |
| Modify | `reviewer/app.py` | `POST /api/person_policy`, update `/faces` to return policy state |
| Modify | `reviewer/templates/faces.html` | Policy badge + toggle button per person row |
| Create | `tests/test_person_policy.py` | All tests for policy logic and DB methods |
| Modify | `README.md` | Update test count |

---

### Task 1 — DB migration: `person_policies` table

**Files:**
- Create: `db/migrations/migrate_019_person_policies.py`

The `person_policies` table is simple: one row per protected person. The only supported policy for now is `always_private`. The `UNIQUE` constraint on `person_name` ensures one policy per person. The migration is idempotent (safe to re-run).

- [ ] **Step 1.1 — Write the failing test**

Create `tests/test_person_policy.py`:

```python
"""
tests/test_person_policy.py — tests for per-person privacy policy

Run from repo root:
    python -m pytest tests/test_person_policy.py -v
"""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.migrations.migrate_019_person_policies import run as run_migration, _already_migrated


def _tmp_db() -> tuple[sqlite3.Connection, str]:
    """Create a minimal throw-away SQLite DB."""
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    conn = sqlite3.connect(f.name)
    conn.row_factory = sqlite3.Row
    # Minimal schema needed by the migration
    conn.execute(
        "CREATE TABLE schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    conn.commit()
    return conn, f.name


class TestMigration019(unittest.TestCase):

    def test_creates_person_policies_table(self):
        conn, path = _tmp_db()
        run_migration(path)
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        self.assertIn("person_policies", tables)
        conn.close()

    def test_table_has_expected_columns(self):
        conn, path = _tmp_db()
        run_migration(path)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(person_policies)").fetchall()]
        for col in ("id", "person_name", "policy", "created_at"):
            self.assertIn(col, cols)
        conn.close()

    def test_idempotent_second_run_does_not_fail(self):
        conn, path = _tmp_db()
        run_migration(path)
        run_migration(path)  # must not raise
        conn.close()

    def test_already_migrated_returns_true_after_run(self):
        conn, path = _tmp_db()
        run_migration(path)
        self.assertTrue(_already_migrated(conn))
        conn.close()

    def test_already_migrated_returns_false_before_run(self):
        conn, path = _tmp_db()
        self.assertFalse(_already_migrated(conn))
        conn.close()
```

- [ ] **Step 1.2 — Run to confirm failure**

```bash
python -m pytest tests/test_person_policy.py::TestMigration019 -v
```

Expected: `ModuleNotFoundError: No module named 'db.migrations.migrate_019_person_policies'`

- [ ] **Step 1.3 — Write the migration**

Create `db/migrations/migrate_019_person_policies.py`:

```python
"""
migrate_019_person_policies.py

Adds the person_policies table. Each row declares a persistent privacy
policy for a named person in Apple Photos.

Supported policies:
  always_private — any photo containing this person is classified auto_private
                   at scan time, regardless of other signals.

Safe to run multiple times (idempotent).

Usage:
    python db/migrations/migrate_019_person_policies.py --config config/config.yml
"""

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import yaml

MIGRATION_NAME = "migrate_019_person_policies"


def _already_migrated(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute(
            "SELECT id FROM schema_migrations WHERE name = ?", (MIGRATION_NAME,)
        ).fetchone()
        if row is not None:
            return True
    except Exception:
        pass
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]
    return "person_policies" in tables


def run(db_path: str, dry_run: bool = False) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    if _already_migrated(conn):
        print("  Skipped:  migration already applied")
        conn.close()
        return

    ddl = """
        CREATE TABLE person_policies (
            id          INTEGER PRIMARY KEY,
            person_name TEXT NOT NULL UNIQUE,
            policy      TEXT NOT NULL CHECK(policy IN ('always_private')),
            created_at  TEXT NOT NULL
        )
    """

    if not dry_run:
        conn.execute(ddl)
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (name, applied_at) VALUES (?, ?)",
            (MIGRATION_NAME, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        print("  Applied:  created person_policies table")
    else:
        print("  Dry-run:  would create person_policies table")

    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/config.yml")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    with open(args.config) as f:
        config = yaml.safe_load(f)
    db_path = str(Path(config["database"]["path"]).expanduser())
    run(db_path, dry_run=args.dry_run)
```

- [ ] **Step 1.4 — Run to confirm tests pass**

```bash
python -m pytest tests/test_person_policy.py::TestMigration019 -v
```

Expected: `5 passed`

- [ ] **Step 1.5 — Commit**

```bash
git add db/migrations/migrate_019_person_policies.py tests/test_person_policy.py
git commit -m "feat: add person_policies migration 019 (GH #114)"
```

---

### Task 2 — DB methods: `get_person_policies`, `set_person_policy`, `delete_person_policy`

**Files:**
- Modify: `db/db.py`
- Modify: `tests/test_person_policy.py`

`get_person_policies()` returns `dict[str, str]` mapping `person_name → policy`. The other two are write operations.

- [ ] **Step 2.1 — Write the failing tests**

Append to `tests/test_person_policy.py`:

```python
from db.db import Database


def _make_db_with_migration() -> Database:
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    from db.db import Database
    db = Database(Path(f.name))
    from db.migrations.migrate_019_person_policies import run as migrate
    migrate(f.name)
    return db


class TestPersonPolicyDbMethods(unittest.TestCase):

    def test_get_person_policies_returns_empty_dict_initially(self):
        db = _make_db_with_migration()
        result = db.get_person_policies()
        db.close()
        self.assertEqual(result, {})

    def test_set_person_policy_stores_a_policy(self):
        db = _make_db_with_migration()
        db.set_person_policy("Alice", "always_private")
        result = db.get_person_policies()
        db.close()
        self.assertEqual(result.get("Alice"), "always_private")

    def test_set_person_policy_upserts_on_duplicate_name(self):
        db = _make_db_with_migration()
        db.set_person_policy("Alice", "always_private")
        db.set_person_policy("Alice", "always_private")  # second call — must not raise
        result = db.get_person_policies()
        db.close()
        self.assertEqual(list(result.keys()).count("Alice"), 1)

    def test_delete_person_policy_removes_entry(self):
        db = _make_db_with_migration()
        db.set_person_policy("Bob", "always_private")
        db.delete_person_policy("Bob")
        result = db.get_person_policies()
        db.close()
        self.assertNotIn("Bob", result)

    def test_delete_person_policy_no_op_when_not_present(self):
        db = _make_db_with_migration()
        db.delete_person_policy("Nobody")  # must not raise
        db.close()

    def test_get_person_policies_returns_all_policies(self):
        db = _make_db_with_migration()
        db.set_person_policy("Alice", "always_private")
        db.set_person_policy("Charlie", "always_private")
        result = db.get_person_policies()
        db.close()
        self.assertIn("Alice", result)
        self.assertIn("Charlie", result)
```

- [ ] **Step 2.2 — Run to confirm failure**

```bash
python -m pytest tests/test_person_policy.py::TestPersonPolicyDbMethods -v
```

Expected: `AttributeError: 'Database' object has no attribute 'get_person_policies'`

- [ ] **Step 2.3 — Add three methods to `db/db.py`**

Find the `active_zones` method in `db/db.py` (around line 547). Add the three new methods nearby (after `active_zones` is a sensible spot since it's another "policy-like" lookup):

```python
    # -----------------------------------------------------------------------
    # Person policies
    # -----------------------------------------------------------------------

    def get_person_policies(self) -> dict[str, str]:
        """Return {person_name: policy} for all rows in person_policies."""
        try:
            rows = self.conn.execute(
                "SELECT person_name, policy FROM person_policies"
            ).fetchall()
            return {r["person_name"]: r["policy"] for r in rows}
        except Exception:
            # Table absent (migration not yet applied): return empty dict
            return {}

    def set_person_policy(self, person_name: str, policy: str) -> None:
        """Insert or replace a policy for person_name."""
        now = _now_iso()
        self.conn.execute(
            """INSERT INTO person_policies (person_name, policy, created_at)
               VALUES (?, ?, ?)
               ON CONFLICT(person_name) DO UPDATE SET policy=excluded.policy""",
            (person_name, policy, now),
        )
        self.conn.commit()

    def delete_person_policy(self, person_name: str) -> None:
        """Remove any policy for person_name. No-op if none exists."""
        self.conn.execute(
            "DELETE FROM person_policies WHERE person_name = ?", (person_name,)
        )
        self.conn.commit()
```

Note: `_now_iso()` is already defined in `db/db.py` as a module-level function.

- [ ] **Step 2.4 — Run to confirm all pass**

```bash
python -m pytest tests/test_person_policy.py -v
```

Expected: `11 passed`

- [ ] **Step 2.5 — Commit**

```bash
git add db/db.py tests/test_person_policy.py
git commit -m "feat: add person_policy DB methods to db.py (GH #114)"
```

---

### Task 3 — `classify()` gains `person_policies` parameter

**Files:**
- Modify: `analyzer/privacy.py`
- Modify: `tests/test_person_policy.py`

The policy check fires after the geofence step (step 2) and before the named-persons step (step 3). If any named person (other than self) in the photo has an `always_private` policy, `classify()` returns `("auto_private", "person policy: <name>")` immediately.

- [ ] **Step 3.1 — Write the failing tests**

Append to `tests/test_person_policy.py`:

```python
from analyzer.privacy import classify


class TestClassifyWithPersonPolicies(unittest.TestCase):

    def _photo(self, persons):
        return {"apple_persons": persons, "place_ishome": False}

    def test_always_private_policy_overrides_needs_review(self):
        """A policy-protected person → auto_private, not needs_review."""
        photo = self._photo(["Alice"])
        state, reason = classify(
            photo, zones=[], self_name="Me",
            person_policies={"Alice": "always_private"},
        )
        self.assertEqual(state, "auto_private")
        self.assertIn("Alice", reason)

    def test_always_private_includes_person_name_in_reason(self):
        photo = self._photo(["Bob"])
        _, reason = classify(
            photo, zones=[], self_name="Me",
            person_policies={"Bob": "always_private"},
        )
        self.assertIn("person policy", reason)
        self.assertIn("Bob", reason)

    def test_no_policy_for_person_falls_through_to_needs_review(self):
        """A named person with no policy still triggers needs_review."""
        photo = self._photo(["Alice"])
        state, _ = classify(
            photo, zones=[], self_name="Me",
            person_policies={},
        )
        self.assertEqual(state, "needs_review")

    def test_self_name_excluded_from_policy_check(self):
        """The photographer's own name is never matched against policies."""
        photo = self._photo(["Me"])
        state, _ = classify(
            photo, zones=[], self_name="Me",
            person_policies={"Me": "always_private"},
        )
        # Photo of only self → candidate_public (no other persons)
        self.assertEqual(state, "candidate_public")

    def test_policy_on_one_person_triggers_even_when_other_persons_present(self):
        """If any person has always_private policy, the photo is auto_private."""
        photo = self._photo(["Alice", "Bob"])
        state, _ = classify(
            photo, zones=[], self_name="Me",
            person_policies={"Alice": "always_private"},
        )
        self.assertEqual(state, "auto_private")

    def test_no_person_policies_arg_behaves_as_before(self):
        """Omitting person_policies entirely is equivalent to no policies."""
        photo = self._photo(["Alice"])
        state, _ = classify(photo, zones=[], self_name="Me")
        self.assertEqual(state, "needs_review")

    def test_home_flag_still_takes_precedence_over_policy(self):
        """Home flag is checked before person policies."""
        photo = {"apple_persons": ["Alice"], "place_ishome": True}
        state, reason = classify(
            photo, zones=[], self_name="Me",
            person_policies={"Alice": "always_private"},
        )
        self.assertEqual(state, "auto_private")
        self.assertIn("home", reason)
```

- [ ] **Step 3.2 — Run to confirm failure**

```bash
python -m pytest tests/test_person_policy.py::TestClassifyWithPersonPolicies -v
```

Expected: `TypeError: classify() got an unexpected keyword argument 'person_policies'`

- [ ] **Step 3.3 — Update `classify()` in `analyzer/privacy.py`**

Change the function signature and add the policy check:

```python
def classify(
    photo: dict,
    zones: list[dict],
    self_name: str = "",
    person_policies: dict[str, str] | None = None,
) -> tuple[str, str]:
    """
    Classify a photo into a privacy state.

    Args:
        photo:           dict from osxphotos (or equivalent from Flickr poller)
        zones:           list of active geofence zone dicts from the database
        self_name:       the photographer's name as it appears in Apple's People
        person_policies: {person_name: policy} from db.get_person_policies().
                         Only 'always_private' is recognised; others are ignored.

    Returns:
        (state, reason) tuple
    """

    lat = photo.get("latitude")
    lon = photo.get("longitude")

    # ------------------------------------------------------------------
    # 1. Apple's own home flag — highest priority
    # ------------------------------------------------------------------
    if photo.get("place_ishome") or (
        isinstance(photo.get("place"), dict) and photo["place"].get("ishome")
    ):
        return "auto_private", "home location (Apple Photos)"

    # ------------------------------------------------------------------
    # 2. Custom geofence zones
    # ------------------------------------------------------------------
    if lat is not None and lon is not None:
        for zone in zones:
            dist = haversine_m(lat, lon, zone["latitude"], zone["longitude"])
            if dist <= zone["radius_m"]:
                policy = zone.get("policy", "auto_private")
                label = zone.get("label") or zone.get("name", "unnamed zone")
                if policy == "auto_private":
                    return "auto_private", f"geofence: {label}"
                elif policy == "flag_review":
                    return "needs_review", f"geofence flag: {label}"
                # policy == 'auto_public' falls through to normal logic

    # ------------------------------------------------------------------
    # 2b. Person policies — before general person detection
    # ------------------------------------------------------------------
    if person_policies:
        persons = _get_persons(photo)
        named_others = [p for p in persons if p and p != self_name and p != "_UNKNOWN_"]
        for name in named_others:
            if person_policies.get(name) == "always_private":
                return "auto_private", f"person policy: {name}"

    # ------------------------------------------------------------------
    # 3. Named persons other than self
    # ------------------------------------------------------------------
    persons = _get_persons(photo)
    named_others = [p for p in persons if p and p != self_name and p != "_UNKNOWN_"]
    if named_others:
        names = ", ".join(named_others)
        return "needs_review", f"named person(s): {names}"

    # ------------------------------------------------------------------
    # 4. Unknown faces detected
    # ------------------------------------------------------------------
    unknown_count = _count_unknown_faces(photo)
    if unknown_count > 0:
        return "needs_review", f"{unknown_count} unidentified face(s)"

    # ------------------------------------------------------------------
    # 5. Apple's people/crowd labels
    # ------------------------------------------------------------------
    labels = _get_labels(photo)
    labels_lower = {lbl.lower() for lbl in labels}
    matched = labels_lower & PEOPLE_LABELS
    if matched:
        return "needs_review", f"people label(s): {', '.join(sorted(matched))}"

    # ------------------------------------------------------------------
    # 6. Human body detection in media_analysis
    # ------------------------------------------------------------------
    human_count = _confident_human_count(photo)
    if human_count > 0:
        return "needs_review", f"{human_count} human(s) detected (body detection)"

    # ------------------------------------------------------------------
    # 7. No people signals — candidate for public
    # ------------------------------------------------------------------
    return "candidate_public", "no people detected"
```

- [ ] **Step 3.4 — Run to confirm all pass**

```bash
python -m pytest tests/test_person_policy.py -v
python -m pytest tests/ -q
```

Expected: all pass (privacy classifier existing tests must still pass — the new param is optional).

- [ ] **Step 3.5 — Commit**

```bash
git add analyzer/privacy.py tests/test_person_policy.py
git commit -m "feat: classify() gains person_policies param (GH #114)"
```

---

### Task 4 — Scanner loads and passes person policies

**Files:**
- Modify: `poller/scanner.py`

The scanner already loads `zones` from `db.active_zones()` before the main photo loop. Person policies load the same way: one call before the loop, then the dict is passed to every `classify()` call in the scan.

- [ ] **Step 4.1 — Write the failing test**

Append to `tests/test_person_policy.py`:

```python
from unittest.mock import patch, MagicMock, call


class TestScannerPassesPolicies(unittest.TestCase):
    """Verify that build_enriched_row passes person_policies to classify()."""

    def test_build_enriched_row_passes_person_policies_to_classify(self):
        """
        When person_policies contains an always_private entry that matches a
        named person in the photo, build_enriched_row should produce
        privacy_state='auto_private'.
        """
        from poller.scanner import build_enriched_row

        photo_row = {
            "uuid": "test-uuid",
            "filename": "IMG_001.jpg",
            "date": "2024-01-01 12:00:00",
            "latitude": None,
            "longitude": None,
            "place_ishome": False,
            "place": None,
            "persons": ["Alice"],
            "labels": [],
            "face_info": [],
            "media_analysis": {},
            "title": "",
            "description": "",
            "keywords": [],
            "albums": [],
        }

        result = build_enriched_row(
            photo_row,
            existing={},
            zones=[],
            self_name="Me",
            person_policies={"Alice": "always_private"},
        )
        self.assertEqual(result["privacy_state"], "auto_private")
```

- [ ] **Step 4.2 — Run to confirm failure**

```bash
python -m pytest tests/test_person_policy.py::TestScannerPassesPolicies -v
```

Expected: `TypeError: build_enriched_row() got an unexpected keyword argument 'person_policies'`

- [ ] **Step 4.3 — Update `build_enriched_row` in `poller/scanner.py`**

Find the `build_enriched_row` function signature (around line 460). The current signature is:

```python
def build_enriched_row(
    photo_row: dict,
    existing: dict,
    zones: list[dict],
    self_name: str,
) -> dict:
```

Change it to:

```python
def build_enriched_row(
    photo_row: dict,
    existing: dict,
    zones: list[dict],
    self_name: str,
    person_policies: dict[str, str] | None = None,
) -> dict:
```

Find the line inside `build_enriched_row` that calls `classify()`. It looks like:

```python
state, reason = classify(merged, zones, self_name=self_name)
```

Change it to:

```python
state, reason = classify(merged, zones, self_name=self_name, person_policies=person_policies)
```

- [ ] **Step 4.4 — Load policies in the scanner's main function and pass them through**

Find the section in the scanner's main function where `zones` is loaded (around line 582):

```python
zones = db.active_zones()
```

Add directly after:

```python
person_policies = db.get_person_policies()
```

Find every call to `build_enriched_row(...)` in `scanner.py` (there are two, around lines 649 and 662). Update each to pass `person_policies`:

```python
# First call (around line 649)
enriched_row = build_enriched_row(photo_row, existing_by_uuid, zones, self_name, person_policies=person_policies)

# Second call (around line 662)
enriched_row = build_enriched_row(photo_row, primary, zones, self_name, person_policies=person_policies)
```

Also find the direct `classify()` call in the scanner (around line 702 — used for Flickr-only records):

```python
state, reason = classify(photo_row, zones, self_name=self_name)
```

Change to:

```python
state, reason = classify(photo_row, zones, self_name=self_name, person_policies=person_policies)
```

- [ ] **Step 4.5 — Run to confirm all tests pass**

```bash
python -m pytest tests/test_person_policy.py -v
python -m pytest tests/ -q
```

Expected: all pass.

- [ ] **Step 4.6 — Commit**

```bash
git add poller/scanner.py tests/test_person_policy.py
git commit -m "feat: scanner loads person_policies and passes to classify() (GH #114)"
```

---

### Task 5 — API endpoint: `POST /api/person_policy`

**Files:**
- Modify: `reviewer/app.py`
- Modify: `tests/test_person_policy.py`

The endpoint accepts `{"person": "Alice", "policy": "always_private"}` to set a policy, or `{"person": "Alice", "policy": null}` to remove it. The Faces page calls this via JavaScript.

- [ ] **Step 5.1 — Write the failing test**

Append to `tests/test_person_policy.py`:

```python
import json

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestPersonPolicyApi(unittest.TestCase):
    """Test POST /api/person_policy endpoint via Flask test client."""

    def setUp(self):
        import tempfile, os
        from reviewer.app import app, get_db_path, _db_cache

        self.db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_file.close()

        # Apply migrations to the test DB
        from db.db import Database
        db = Database(Path(self.db_file.name))
        from db.migrations.migrate_019_person_policies import run as migrate
        migrate(self.db_file.name)
        db.close()

        app.config["TESTING"] = True
        app.config["TEST_DB_PATH"] = self.db_file.name
        self.client = app.test_client()

    def tearDown(self):
        import os
        os.unlink(self.db_file.name)

    def _post(self, person, policy):
        return self.client.post(
            "/api/person_policy",
            data=json.dumps({"person": person, "policy": policy}),
            content_type="application/json",
        )

    def test_set_policy_returns_ok(self):
        resp = self._post("Alice", "always_private")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertTrue(data["ok"])

    def test_clear_policy_returns_ok(self):
        self._post("Alice", "always_private")
        resp = self._post("Alice", None)
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertTrue(data["ok"])

    def test_invalid_policy_returns_400(self):
        resp = self._post("Alice", "unknown_policy")
        self.assertEqual(resp.status_code, 400)

    def test_empty_person_returns_400(self):
        resp = self._post("", "always_private")
        self.assertEqual(resp.status_code, 400)
```

Note: the test accesses the DB via `reviewer/app.py`'s `db()` helper. In the test, the app must use the test DB file. Check how other UI tests handle this — look at `tests/test_review_ui.py` for the pattern used to inject a test DB path. If the app reads `TEST_DB_PATH` from `app.config`, add that support; if the existing tests use a different mechanism, mirror that pattern exactly.

- [ ] **Step 5.2 — Run to confirm failure**

```bash
python -m pytest tests/test_person_policy.py::TestPersonPolicyApi -v
```

Expected: `404 NOT FOUND` (route doesn't exist yet)

- [ ] **Step 5.3 — Add the route to `reviewer/app.py`**

Find a natural place near the `api_batch_person` route (around line 351). Add after it:

```python
@app.route("/api/person_policy", methods=["POST"])
def api_person_policy() -> _JsonResp:
    """
    Set or clear a privacy policy for a named person.

    Request body: {"person": "Alice", "policy": "always_private" | null}
    policy=null removes any existing policy for that person.
    """
    data = request.get_json(force=True)
    person = (data.get("person") or "").strip()
    policy = data.get("policy")

    if not person:
        return jsonify({"ok": False, "error": "person name required"}), 400

    valid_policies = {"always_private", None}
    if policy not in valid_policies:
        return jsonify({"ok": False, "error": f"unknown policy: {policy!r}"}), 400

    if policy is None:
        db().delete_person_policy(person)
    else:
        db().set_person_policy(person, policy)

    return jsonify({"ok": True, "person": person, "policy": policy})
```

- [ ] **Step 5.4 — Add a GET endpoint for querying a single person's policy**

This is needed so the Faces page can pre-populate the toggle state without requiring a full-page reload. Add immediately after the POST route:

```python
@app.route("/api/person_policy/<path:person_name>", methods=["GET"])
def api_get_person_policy(person_name: str) -> _JsonResp:
    """Return the current policy for a named person, or null if none."""
    policies = db().get_person_policies()
    return jsonify({"person": person_name, "policy": policies.get(person_name)})
```

- [ ] **Step 5.5 — Run to confirm the API tests pass**

```bash
python -m pytest tests/test_person_policy.py::TestPersonPolicyApi -v
```

Expected: `4 passed` (adjust for however many pass given test-DB wiring — fix setUp if needed by checking how `tests/test_review_ui.py` injects its DB).

- [ ] **Step 5.6 — Commit**

```bash
git add reviewer/app.py tests/test_person_policy.py
git commit -m "feat: add POST /api/person_policy endpoint (GH #114)"
```

---

### Task 6 — Faces page: policy badge and toggle

**Files:**
- Modify: `reviewer/app.py` — pass `person_policies` dict to the `faces.html` template
- Modify: `reviewer/templates/faces.html` — policy badge + toggle button per person row

The Faces page already renders one row per named person. Each row gets a small "🔒 Always private" badge when the person has a policy, and a toggle button ("Set always private" / "Remove policy") that calls `POST /api/person_policy` via `fetch`.

- [ ] **Step 6.1 — Pass `person_policies` to the Faces template**

In `reviewer/app.py`, find the `faces()` route (around line 297). It ends with:

```python
return render_template(
    "faces.html",
    named=named,
    unknown_count=unknown_count,
    unknown_photos=unknown_photos,
    stats=db().stats(),
)
```

Change to:

```python
return render_template(
    "faces.html",
    named=named,
    unknown_count=unknown_count,
    unknown_photos=unknown_photos,
    stats=db().stats(),
    person_policies=db().get_person_policies(),
)
```

- [ ] **Step 6.2 — Add CSS for the policy badge and button**

In `reviewer/templates/faces.html`, find the `{% block extra_style %}` section. Append inside it:

```css
.policy-badge {
  font-size: 11px;
  color: var(--red);
  font-weight: 600;
  margin-left: 8px;
}
.btn-policy {
  background: transparent;
  border: 1px solid var(--border);
  color: var(--muted);
  font-size: 11px;
  padding: 4px 10px;
  border-radius: var(--radius);
  cursor: pointer;
}
.btn-policy:hover { border-color: var(--red); color: var(--red); }
.btn-policy.active { border-color: var(--red); color: var(--red); background: rgba(229,115,115,0.08); }
```

- [ ] **Step 6.3 — Add the policy badge and button to each person row**

In `reviewer/templates/faces.html`, find the `person-name` `div` inside the person row loop. It looks like:

```html
<div class="person-name">
  <a href="/review?person={{ row.person | urlencode }}">{{ row.person }}</a>
</div>
```

Change to:

```html
<div class="person-name">
  <a href="/review?person={{ row.person | urlencode }}">{{ row.person }}</a>
  {% if person_policies.get(row.person) == 'always_private' %}
    <span class="policy-badge">🔒 always private</span>
  {% endif %}
</div>
```

Find the `person-actions` `div` in the row. Add a policy toggle button after the existing buttons:

```html
<div class="person-actions">
  <!-- existing Review / All private / All public buttons unchanged -->
  ...
  <button
    class="btn-policy {% if person_policies.get(row.person) == 'always_private' %}active{% endif %}"
    data-person="{{ row.person }}"
    data-has-policy="{{ 'true' if person_policies.get(row.person) == 'always_private' else 'false' }}"
    onclick="togglePersonPolicy(this)">
    {% if person_policies.get(row.person) == 'always_private' %}Remove policy{% else %}Always private{% endif %}
  </button>
</div>
```

- [ ] **Step 6.4 — Add the JavaScript toggle handler**

In `reviewer/templates/faces.html`, in the `{% block extra_script %}` section (or add one if absent), add:

```javascript
async function togglePersonPolicy(btn) {
  const person = btn.dataset.person;
  const hasPolicy = btn.dataset.hasPolicy === 'true';
  const newPolicy = hasPolicy ? null : 'always_private';

  const resp = await fetch('/api/person_policy', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({person, policy: newPolicy}),
  });
  if (!resp.ok) { alert('Failed to update policy.'); return; }

  // Reload the page so the badge and button state reflect the new policy
  window.location.reload();
}
```

- [ ] **Step 6.5 — Smoke-test the Faces page manually**

```bash
python reviewer/app.py --config config/config.yml
```

Open http://localhost:5173/faces in a browser. Verify each person row shows the "Always private" button. Click it for one person — confirm the page reloads with the 🔒 badge. Click "Remove policy" — confirm the badge disappears.

- [ ] **Step 6.6 — Commit**

```bash
git add reviewer/app.py reviewer/templates/faces.html
git commit -m "feat: add person policy badge and toggle to Faces page (GH #114)"
```

---

### Task 7 — Final checks, docs, README

**Files:**
- Modify: `README.md`
- Modify: `docs/future-directions.md`

- [ ] **Step 7.1 — Run the full test suite and lint**

```bash
python -m pytest tests/ -q
make lint
```

Expected: all tests pass, no lint errors. Fix any ruff formatting issues before continuing.

- [ ] **Step 7.2 — Update README test count**

Run `python -m pytest tests/ -q | tail -1` to get the exact count. Update the two test-count references in `README.md` to match.

- [ ] **Step 7.3 — Update future-directions.md**

Change the per-person privacy policy heading to mark it done:

```markdown
### Per-person privacy policy ([#114](https://github.com/cdevers/Blue-Pearmain/issues/114)) `size:M` · ✓ done
```

- [ ] **Step 7.4 — Commit and push**

```bash
git add README.md docs/future-directions.md tests/test_person_policy.py
git commit -m "docs: update test count and roadmap for per-person policy (Closes #114)"
git push
```
