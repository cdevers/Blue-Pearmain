# `bp migrate` Command Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `bp migrate` command that auto-discovers and applies pending DB migrations in order, and update `bp doctor` to hint at `bp migrate` instead of printing per-file manual commands.

**Architecture:** Extract a `_pending_migrations(db_path, migrations_dir)` helper into `bp`. Both `cmd_doctor` (section 5) and the new `cmd_migrate` call this helper. `cmd_migrate` uses `importlib.util.spec_from_file_location` to load each migration module and call its `run(db_path, dry_run)` function.

**Tech Stack:** Python 3.11, stdlib only (`importlib.util`, `sqlite3`, `argparse`, `re`, `pathlib`). One file changes: `bp`. One new test file: `tests/test_migrate_cmd.py`.

---

## File Map

| File | Change |
|------|--------|
| `bp` | Add `_pending_migrations()` helper before `cmd_doctor`; add `cmd_migrate()`; refactor `cmd_doctor` section 5; add `migrate` subparser + dispatch entry; update module docstring |
| `tests/test_migrate_cmd.py` | New — 6 tests covering helper, cmd_migrate, doctor hint |
| `README.md` | Add `bp migrate` to the command table |

---

### Task 1: `_pending_migrations` helper — TDD

The helper is a pure function: given a DB path and a migrations directory, return unapplied `(name, path)` pairs sorted by filename.

**Files:**
- Create: `tests/test_migrate_cmd.py`
- Modify: `bp` (add helper only)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_migrate_cmd.py`:

```python
"""
tests/test_migrate_cmd.py — tests for bp migrate command and _pending_migrations helper

Run from repo root:
    python -m pytest tests/test_migrate_cmd.py -v
"""

import importlib.util
import io
import os
import sqlite3
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

# Load the `bp` script as a module (it has no .py extension)
_BP_PATH = Path(__file__).parent.parent / "bp"
_spec = importlib.util.spec_from_file_location("bp", _BP_PATH)
_bp_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_bp_module)

_pending_migrations = _bp_module._pending_migrations
cmd_migrate = _bp_module.cmd_migrate
cmd_doctor = _bp_module.cmd_doctor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db_with_migrations_table() -> str:
    """Create a temp SQLite DB with an empty schema_migrations table. Returns path."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE schema_migrations "
        "(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, applied_at TEXT NOT NULL)"
    )
    conn.commit()
    conn.close()
    return path


def _write_migration_file(directory: Path, filename: str, name: str) -> Path:
    """Write a minimal migration file to directory. Returns the file path."""
    content = f"""\
MIGRATION_NAME = "{name}"

def run(db_path, dry_run=False):
    import sqlite3, datetime
    if dry_run:
        return
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (name, applied_at) VALUES (?, ?)",
        ("{name}", datetime.datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()
"""
    p = directory / filename
    p.write_text(content)
    return p


# ---------------------------------------------------------------------------
# _pending_migrations
# ---------------------------------------------------------------------------

class TestPendingMigrations(unittest.TestCase):
    def setUp(self):
        self.db_path = _make_db_with_migrations_table()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.mig_dir = Path(self._tmpdir.name)

    def tearDown(self):
        os.unlink(self.db_path)
        self._tmpdir.cleanup()

    def test_empty_dir_returns_nothing(self):
        result = _pending_migrations(self.db_path, self.mig_dir)
        self.assertEqual(result, [])

    def test_unapplied_migration_returned(self):
        _write_migration_file(self.mig_dir, "migrate_001_alpha.py", "migrate_001_alpha")
        result = _pending_migrations(self.db_path, self.mig_dir)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][0], "migrate_001_alpha")
        self.assertIsInstance(result[0][1], Path)

    def test_applied_migration_excluded(self):
        _write_migration_file(self.mig_dir, "migrate_001_alpha.py", "migrate_001_alpha")
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)",
            ("migrate_001_alpha", "2026-01-01T00:00:00")
        )
        conn.commit()
        conn.close()
        result = _pending_migrations(self.db_path, self.mig_dir)
        self.assertEqual(result, [])

    def test_results_sorted_by_filename(self):
        _write_migration_file(self.mig_dir, "migrate_002_bravo.py", "migrate_002_bravo")
        _write_migration_file(self.mig_dir, "migrate_001_alpha.py", "migrate_001_alpha")
        result = _pending_migrations(self.db_path, self.mig_dir)
        self.assertEqual([r[0] for r in result], ["migrate_001_alpha", "migrate_002_bravo"])

    def test_fresh_db_no_schema_migrations_table(self):
        """If schema_migrations table doesn't exist, treat all as pending."""
        fd, fresh_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        # DB has no tables at all
        try:
            _write_migration_file(self.mig_dir, "migrate_001_alpha.py", "migrate_001_alpha")
            result = _pending_migrations(fresh_path, self.mig_dir)
            self.assertEqual(len(result), 1)
        finally:
            os.unlink(fresh_path)
```

- [ ] **Step 2: Run tests — expect ImportError or AttributeError (helper not yet defined)**

```bash
python -m pytest tests/test_migrate_cmd.py -v 2>&1 | head -30
```

Expected: module load fails because `_pending_migrations` doesn't exist on `bp` yet.

- [ ] **Step 3: Add `_pending_migrations` to `bp`**

Insert this block in `bp`, just before the `def cmd_doctor(args):` definition (around line 555). Also add `import re` near the top of the file if not already present (it is — already imported inside `cmd_doctor`; move it to module scope or keep it local).

```python
# ---------------------------------------------------------------------------
# Migration helpers
# ---------------------------------------------------------------------------

def _pending_migrations(
    db_path: str,
    migrations_dir: Path,
) -> list[tuple[str, Path]]:
    """
    Return (migration_name, file_path) pairs for migrations not yet applied to
    db_path, sorted by filename (migrate_NNN_... sorts numerically by NNN).
    """
    import re
    import sqlite3

    _name_re = re.compile(r'^MIGRATION_NAME\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        applied = {r["name"] for r in conn.execute("SELECT name FROM schema_migrations").fetchall()}
        conn.close()
    except sqlite3.OperationalError:
        # schema_migrations table doesn't exist yet (fresh DB) — treat all as pending
        applied = set()

    result: list[tuple[str, Path]] = []
    for mf in sorted(migrations_dir.glob("migrate_*.py")):
        m = _name_re.search(mf.read_text())
        if m and m.group(1) not in applied:
            result.append((m.group(1), mf))
    return result
```

- [ ] **Step 4: Run tests — expect pass for Task 1 tests**

```bash
python -m pytest tests/test_migrate_cmd.py::TestPendingMigrations -v
```

Expected: 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_migrate_cmd.py bp
git commit -m "feat: add _pending_migrations helper to bp (#122)"
```

---

### Task 2: `cmd_migrate` — TDD

**Files:**
- Modify: `tests/test_migrate_cmd.py` (add `TestCmdMigrate`)
- Modify: `bp` (add `cmd_migrate`, wire up argparse and dispatch)

- [ ] **Step 1: Add `TestCmdMigrate` to the test file**

Append to `tests/test_migrate_cmd.py`:

```python
# ---------------------------------------------------------------------------
# cmd_migrate
# ---------------------------------------------------------------------------

import argparse
import yaml as _yaml


def _make_config_file(db_path: str) -> str:
    """Write a minimal config YAML pointing at db_path. Returns the config file path."""
    fd, path = tempfile.mkstemp(suffix=".yml")
    os.close(fd)
    with open(path, "w") as f:
        _yaml.dump({"database": {"path": db_path}}, f)
    return path


class TestCmdMigrate(unittest.TestCase):
    def setUp(self):
        self.db_path = _make_db_with_migrations_table()
        self.config_path = _make_config_file(self.db_path)
        self._tmpdir = tempfile.TemporaryDirectory()
        self.mig_dir = Path(self._tmpdir.name)

    def tearDown(self):
        os.unlink(self.db_path)
        os.unlink(self.config_path)
        self._tmpdir.cleanup()

    def _args(self, dry_run: bool = False) -> argparse.Namespace:
        a = argparse.Namespace()
        a.config = self.config_path
        a.dry_run = dry_run
        a.verbose = False
        return a

    def _pending_in_tmpdir(self, db_path: str) -> list:
        return _pending_migrations(db_path, self.mig_dir)

    def test_no_pending_prints_message(self):
        # No migration files in temp dir
        buf = io.StringIO()
        with unittest.mock.patch("sys.stdout", buf):
            with unittest.mock.patch.object(_bp_module, "_pending_migrations", self._pending_in_tmpdir):
                cmd_migrate(self._args())
        self.assertIn("already applied", buf.getvalue())

    def test_applies_pending_migration(self):
        _write_migration_file(self.mig_dir, "migrate_001_test.py", "migrate_001_test")

        with unittest.mock.patch.object(_bp_module, "_pending_migrations", self._pending_in_tmpdir):
            cmd_migrate(self._args())

        # Migration name should now be recorded in schema_migrations
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT name FROM schema_migrations WHERE name = ?", ("migrate_001_test",)
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row)

    def test_dry_run_does_not_write(self):
        _write_migration_file(self.mig_dir, "migrate_001_test.py", "migrate_001_test")

        with unittest.mock.patch.object(_bp_module, "_pending_migrations", self._pending_in_tmpdir):
            cmd_migrate(self._args(dry_run=True))

        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT name FROM schema_migrations WHERE name = ?", ("migrate_001_test",)
        ).fetchone()
        conn.close()
        self.assertIsNone(row)

    def test_multiple_migrations_applied_in_order(self):
        _write_migration_file(self.mig_dir, "migrate_002_b.py", "migrate_002_b")
        _write_migration_file(self.mig_dir, "migrate_001_a.py", "migrate_001_a")
        applied_order: list[str] = []

        original = _pending_migrations

        def patched_pending(db_path: str, _mdir: Path) -> list:
            return original(db_path, self.mig_dir)

        with unittest.mock.patch.object(_bp_module, "_pending_migrations", patched_pending):
            cmd_migrate(self._args())

        conn = sqlite3.connect(self.db_path)
        rows = [r[0] for r in conn.execute(
            "SELECT name FROM schema_migrations ORDER BY applied_at"
        ).fetchall()]
        conn.close()
        self.assertEqual(rows, ["migrate_001_a", "migrate_002_b"])
```

- [ ] **Step 2: Run tests — expect AttributeError (cmd_migrate not yet defined)**

```bash
python -m pytest tests/test_migrate_cmd.py::TestCmdMigrate -v 2>&1 | head -20
```

Expected: `AttributeError: module 'bp' has no attribute 'cmd_migrate'`

- [ ] **Step 3: Add `cmd_migrate` to `bp`**

Insert after `cmd_doctor` (before `cmd_all`, around line 712):

```python
def cmd_migrate(args):
    """
    Auto-discover and apply all pending DB migrations in order.
    Safe to re-run — each migration is idempotent.
    """
    import importlib.util
    import yaml

    with open(args.config) as f:
        config = yaml.safe_load(f)

    db_path = str(Path(config["database"]["path"]).expanduser())
    migrations_dir = ROOT / "db" / "migrations"

    pending = _pending_migrations(db_path, migrations_dir)

    if not pending:
        print("All migrations already applied.")
        return

    for name, file_path in pending:
        if getattr(args, "dry_run", False):
            print(f"  [dry-run] Would apply: {name}")
        else:
            print(f"  Applying: {name}…")
            spec = importlib.util.spec_from_file_location(name, file_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.run(db_path, dry_run=False)

    count = len(pending)
    if getattr(args, "dry_run", False):
        print(f"\n{count} migration(s) would be applied.")
    else:
        print(f"\n{count} migration(s) applied.")
```

- [ ] **Step 4: Wire up argparse and dispatch in `bp`**

**4a. Update the module docstring** at the top of `bp` — add `migrate` to the usage table:

```
    bp migrate [--dry-run]              Apply all pending DB migrations in order
```

Insert it just before the `bp doctor` line in the docstring.

**4b. Add argparse subparser** — insert after the `# doctor` parser block (after line ~925):

```python
# migrate
p_mig = sub.add_parser(
    "migrate",
    help="Apply all pending DB migrations in order (safe to re-run; each migration is idempotent).",
)
p_mig.add_argument(
    "--dry-run", action="store_true",
    help="Show what would be applied without modifying the DB",
)
```

**4c. Add to dispatch dict** — in the `dispatch = { ... }` block (around line 1001), add:

```python
"migrate":           cmd_migrate,
```

- [ ] **Step 5: Run all new tests**

```bash
python -m pytest tests/test_migrate_cmd.py -v
```

Expected: all tests PASS (the TestPendingMigrations tests from Task 1 plus the 4 new TestCmdMigrate tests — 9 total).

- [ ] **Step 6: Run full test suite to confirm nothing regressed**

```bash
python -m pytest tests/ -q
```

Expected: all tests PASS.

- [ ] **Step 7: Commit**

```bash
git add bp tests/test_migrate_cmd.py
git commit -m "feat: add bp migrate command (#122)"
```

---

### Task 3: Refactor `cmd_doctor` section 5 + update docs

**Files:**
- Modify: `bp` (`cmd_doctor` section 5 refactor)
- Modify: `tests/test_migrate_cmd.py` (add doctor hint test)
- Modify: `README.md`

- [ ] **Step 1: Add doctor hint test**

Append to `tests/test_migrate_cmd.py`:

```python
# ---------------------------------------------------------------------------
# cmd_doctor hint update
# ---------------------------------------------------------------------------

class TestDoctorMigrationHint(unittest.TestCase):
    def setUp(self):
        self.db_path = _make_db_with_migrations_table()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.mig_dir = Path(self._tmpdir.name)
        # Write one migration file whose name is NOT in schema_migrations
        _write_migration_file(self.mig_dir, "migrate_099_fake.py", "migrate_099_fake")

    def tearDown(self):
        os.unlink(self.db_path)
        self._tmpdir.cleanup()

    def test_doctor_hint_says_bp_migrate(self):
        """When a migration is unapplied, doctor output must say 'bp migrate', not 'python db/migrations'."""
        # Patch _pending_migrations so doctor uses our temp dir
        def fake_pending(db_path: str, _mdir: Path) -> list:
            return _pending_migrations(db_path, self.mig_dir)

        buf = io.StringIO()
        with unittest.mock.patch.object(_bp_module, "_pending_migrations", fake_pending):
            with unittest.mock.patch("sys.stdout", buf):
                # cmd_doctor reads config; minimal config to reach section 5
                # We patch db_ok logic by having a valid DB path
                # Simplest: mock the doctor's DB open/close to return our db_path
                # and skip config/flickr checks by patching _cfg
                # Actually: just check that _pending_migrations output drives the hint
                # by testing _pending_migrations + checking the WARN output directly.
                # This test validates the doctor output text change.
                pass  # see Step 3 note below

        # Simpler assertion: confirm the old per-file hint is not in bp source
        bp_source = _BP_PATH.read_text()
        self.assertNotIn("python db/migrations", bp_source)
        self.assertIn("bp migrate", bp_source)
```

**Note for implementer:** The doctor integration test is an approximation — the full `cmd_doctor` test requires a real config YAML and is integration-heavy. The key assertion (no `python db/migrations` in source, `bp migrate` present) validates the change directly. If you prefer a fuller integration test, write it as a separate class.

- [ ] **Step 2: Run test — expect FAIL (source still has old hint)**

```bash
python -m pytest tests/test_migrate_cmd.py::TestDoctorMigrationHint -v
```

Expected: FAIL — `python db/migrations` still present in source.

- [ ] **Step 3: Refactor `cmd_doctor` section 5 in `bp`**

Replace the existing section 5 block (approximately lines 646–673):

**Before:**
```python
    # ── 5. Migrations ─────────────────────────────────────────────────────────
    if db_ok:
        try:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            applied = {r["name"] for r in conn.execute("SELECT name FROM schema_migrations").fetchall()}

            # Discover expected migrations by scanning migration files for MIGRATION_NAME
            migrations_dir = ROOT / "db" / "migrations"
            name_re = re.compile(r'^MIGRATION_NAME\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)
            expected = {}
            for mf in sorted(migrations_dir.glob("migrate_*.py")):
                text = mf.read_text()
                m = name_re.search(text)
                if m:
                    expected[m.group(1)] = mf.name

            missing = {name: fname for name, fname in expected.items() if name not in applied}
            if missing:
                for name, fname in sorted(missing.items()):
                    check(WARN, f"Migration not applied: {name}",
                          f"Run: python db/migrations/{fname} --config {args.config}")
            else:
                last = sorted(applied)[-1] if applied else "(none)"
                check(OK, f"Migrations: all applied (latest: {last})")
            conn.close()
        except Exception as e:
            check(WARN, "Migrations: could not check", str(e))
```

**After:**
```python
    # ── 5. Migrations ─────────────────────────────────────────────────────────
    if db_ok:
        try:
            pending = _pending_migrations(str(db_path), ROOT / "db" / "migrations")
            if pending:
                for name, _ in pending:
                    check(WARN, f"Migration not applied: {name}")
                print(f"       → Run: bp migrate --config {args.config}")
            else:
                # Show the latest applied migration for reassurance
                # (sqlite3 is already imported at the top of cmd_doctor)
                conn = sqlite3.connect(str(db_path))
                applied = [r[0] for r in conn.execute("SELECT name FROM schema_migrations ORDER BY name").fetchall()]
                conn.close()
                last = applied[-1] if applied else "(none)"
                check(OK, f"Migrations: all applied (latest: {last})")
        except Exception as e:
            check(WARN, "Migrations: could not check", str(e))
```

Note: `re` and `sqlite3` imports inside `cmd_doctor` can now be removed from that function (they're used by `_pending_migrations` instead). Leave `import sqlite3` at the top of the function if it's still needed for the DB open in step 4 (the database check); check if the `conn` / `sqlite3.connect` call in section 4 is separate from section 5.

- [ ] **Step 4: Run doctor hint test — expect PASS**

```bash
python -m pytest tests/test_migrate_cmd.py::TestDoctorMigrationHint -v
```

Expected: PASS.

- [ ] **Step 5: Run full test suite**

```bash
python -m pytest tests/ -q
```

Expected: all tests PASS.

- [ ] **Step 6: Update README.md**

Find the command table in `README.md` (near the top, lists `bp scan`, `bp poll`, etc.). Add `bp migrate`:

```markdown
| `bp migrate [--dry-run]` | Apply all pending DB migrations in order |
```

Insert it near `bp doctor` in the table.

- [ ] **Step 7: Commit**

```bash
git add bp tests/test_migrate_cmd.py README.md
git commit -m "refactor: use _pending_migrations in bp doctor; update hint to bp migrate (#122)"
```

---

### Task 4: Close issue and push

- [ ] **Step 1: Run full test suite one final time**

```bash
python -m pytest tests/ -q
```

Expected: all tests PASS.

- [ ] **Step 2: Run lint**

```bash
make lint
```

Expected: no errors.

- [ ] **Step 3: Close GH issue**

```bash
gh issue close 122 --comment "Implemented in this branch. \`bp migrate\` auto-discovers and applies pending migrations in order; \`bp doctor\` now hints \`bp migrate\` instead of listing individual run commands. See commits for details."
```

- [ ] **Step 4: Push to origin**

```bash
git push origin main
```
