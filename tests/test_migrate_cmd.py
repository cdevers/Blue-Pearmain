"""
tests/test_migrate_cmd.py — tests for bp migrate command and _pending_migrations helper

Run from repo root:
    python -m pytest tests/test_migrate_cmd.py -v
"""

import argparse
import importlib.util
import io
import os
import sqlite3
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import yaml as _yaml

# Load the `bp` script as a module (it has no .py extension).
# spec_from_file_location returns None for extension-less files on Python 3.14;
# use SourceFileLoader directly and set __file__ so ROOT resolves correctly.
from importlib.machinery import SourceFileLoader

_BP_PATH = Path(__file__).parent.parent / "bp"
_loader = SourceFileLoader("bp", str(_BP_PATH))
_spec = importlib.util.spec_from_loader("bp", _loader, origin=str(_BP_PATH))
_bp_module = importlib.util.module_from_spec(_spec)
_bp_module.__file__ = str(_BP_PATH)
_spec.loader.exec_module(_bp_module)

_pending_migrations = _bp_module._pending_migrations  # type: ignore[attr-defined]
cmd_migrate = _bp_module.cmd_migrate  # type: ignore[attr-defined]
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
            ("migrate_001_alpha", "2026-01-01T00:00:00"),
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


# ---------------------------------------------------------------------------
# cmd_migrate helpers
# ---------------------------------------------------------------------------


def _make_config_file(db_path: str) -> str:
    """Write a minimal config YAML pointing at db_path. Returns the config file path."""
    fd, path = tempfile.mkstemp(suffix=".yml")
    os.close(fd)
    with open(path, "w") as f:
        _yaml.dump({"database": {"path": db_path}}, f)
    return path


# ---------------------------------------------------------------------------
# cmd_migrate
# ---------------------------------------------------------------------------


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

    def _pending_in_tmpdir(self, db_path: str, _mdir: Path = None) -> list:  # type: ignore[assignment]
        return _pending_migrations(db_path, self.mig_dir)

    def test_no_pending_prints_message(self):
        # No migration files in temp dir
        buf = io.StringIO()
        with unittest.mock.patch("sys.stdout", buf):
            with unittest.mock.patch.object(
                _bp_module, "_pending_migrations", self._pending_in_tmpdir
            ):
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

        original = _pending_migrations

        def patched_pending(db_path: str, _mdir: Path) -> list:
            return original(db_path, self.mig_dir)

        with unittest.mock.patch.object(_bp_module, "_pending_migrations", patched_pending):
            cmd_migrate(self._args())

        conn = sqlite3.connect(self.db_path)
        rows = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM schema_migrations ORDER BY applied_at"
            ).fetchall()
        ]
        conn.close()
        self.assertEqual(rows, ["migrate_001_a", "migrate_002_b"])


# ---------------------------------------------------------------------------
# Doctor migration hint
# ---------------------------------------------------------------------------


class TestDoctorMigrationHint(unittest.TestCase):
    def test_doctor_hint_says_bp_migrate(self):
        """Source code must contain 'bp migrate' as the fix hint, not 'python db/migrations'."""
        bp_source = _BP_PATH.read_text()
        self.assertNotIn("python db/migrations", bp_source)
        self.assertIn("bp migrate", bp_source)
