"""
tests/test_migrate_cmd.py — tests for bp migrate command and _pending_migrations helper

Run from repo root:
    python -m pytest tests/test_migrate_cmd.py -v
"""

import importlib.util
import os
import sqlite3
import tempfile
import unittest
import unittest.mock
from pathlib import Path

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
# cmd_migrate is added in Task 2; accessed via getattr in TestCmdMigrate
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
