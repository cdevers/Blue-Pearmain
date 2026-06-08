"""
tests/test_migrate_baseline_163.py — #163 regression + baseline coverage

Two concerns:
  1. cmd_migrate calls module.run(db_path, dry_run=False). Older migrations
     (004, 005) used the bare run(db_path) signature and crashed with
     TypeError. Guard that 004/005 accept dry_run and honor it.
  2. `bp migrate --baseline` records every pending migration's MIGRATION_NAME
     as applied WITHOUT importing/running its DDL — to resync schema_migrations
     on a live DB whose schema is already present.

Run from repo root:
    python -m pytest tests/test_migrate_baseline_163.py -v
"""

import argparse
import importlib.util
import io
import os
import sqlite3
import tempfile
import unittest
import unittest.mock
from importlib.machinery import SourceFileLoader
from pathlib import Path

import yaml as _yaml

# Load the extension-less `bp` script as a module.
_BP_PATH = Path(__file__).parent.parent / "bp"
_loader = SourceFileLoader("bp", str(_BP_PATH))
_spec = importlib.util.spec_from_loader("bp", _loader, origin=str(_BP_PATH))
_bp_module = importlib.util.module_from_spec(_spec)
_bp_module.__file__ = str(_BP_PATH)
_spec.loader.exec_module(_bp_module)

cmd_migrate = _bp_module.cmd_migrate  # type: ignore[attr-defined]
_pending_migrations = _bp_module._pending_migrations  # type: ignore[attr-defined]

_MIG_DIR = Path(__file__).parent.parent / "db" / "migrations"


def _load_migration(filename: str):
    path = _MIG_DIR / filename
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _name_applied(db_path: str, name: str) -> bool:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT 1 FROM schema_migrations WHERE name = ?", (name,)).fetchone()
        return row is not None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# #163 crash fix: 004/005 run() must accept and honor dry_run
# ---------------------------------------------------------------------------


class TestOldSignatureMigrationsAcceptDryRun(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self):
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db_path + ext)
            except FileNotFoundError:
                pass

    def _check(self, filename: str, migration_name: str):
        mod = _load_migration(filename)
        # dry_run=False is exactly how cmd_migrate calls it — must not raise.
        mod.run(self.db_path, dry_run=False)
        self.assertTrue(
            _name_applied(self.db_path, migration_name),
            f"{migration_name} should be recorded after a real run",
        )

    def _check_dry_run(self, filename: str, migration_name: str):
        fd, fresh = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            mod = _load_migration(filename)
            mod.run(fresh, dry_run=True)
            self.assertFalse(
                _name_applied(fresh, migration_name),
                f"{migration_name} must NOT be recorded under dry_run",
            )
        finally:
            for ext in ("", "-wal", "-shm"):
                try:
                    os.unlink(fresh + ext)
                except FileNotFoundError:
                    pass

    def test_004_albums_accepts_dry_run_false(self):
        self._check("migrate_004_albums.py", "migrate_003_albums")

    def test_004_albums_dry_run_does_not_record(self):
        self._check_dry_run("migrate_004_albums.py", "migrate_003_albums")

    def test_005_conflicts_accepts_dry_run_false(self):
        self._check("migrate_005_metadata_conflicts.py", "migrate_005_metadata_conflicts")

    def test_005_conflicts_dry_run_does_not_record(self):
        self._check_dry_run("migrate_005_metadata_conflicts.py", "migrate_005_metadata_conflicts")


# ---------------------------------------------------------------------------
# bp migrate --baseline
# ---------------------------------------------------------------------------


def _make_db_with_migrations_table() -> str:
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


def _make_config_file(db_path: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".yml")
    os.close(fd)
    with open(path, "w") as f:
        _yaml.dump({"database": {"path": db_path}}, f)
    return path


def _write_boom_migration(directory: Path, filename: str, name: str) -> Path:
    """A migration whose run() raises — proves baseline never executes it."""
    content = f"""\
MIGRATION_NAME = "{name}"

def run(db_path, dry_run=False):
    raise RuntimeError("run() must not be called during --baseline")
"""
    p = directory / filename
    p.write_text(content)
    return p


class TestMigrateBaseline(unittest.TestCase):
    def setUp(self):
        self.db_path = _make_db_with_migrations_table()
        self.config_path = _make_config_file(self.db_path)
        self._tmpdir = tempfile.TemporaryDirectory()
        self.mig_dir = Path(self._tmpdir.name)

    def tearDown(self):
        os.unlink(self.db_path)
        os.unlink(self.config_path)
        self._tmpdir.cleanup()

    def _args(self, *, baseline: bool = False, dry_run: bool = False):
        a = argparse.Namespace()
        a.config = self.config_path
        a.dry_run = dry_run
        a.baseline = baseline
        a.verbose = False
        return a

    def _pending_in_tmpdir(self, db_path: str, _mdir: Path = None):  # type: ignore[assignment]
        return _pending_migrations(db_path, self.mig_dir)

    def test_baseline_records_names_without_running(self):
        _write_boom_migration(self.mig_dir, "migrate_001_boom.py", "migrate_001_boom")
        _write_boom_migration(self.mig_dir, "migrate_002_boom.py", "migrate_002_boom")

        with unittest.mock.patch.object(_bp_module, "_pending_migrations", self._pending_in_tmpdir):
            cmd_migrate(self._args(baseline=True))  # must NOT raise

        self.assertTrue(_name_applied(self.db_path, "migrate_001_boom"))
        self.assertTrue(_name_applied(self.db_path, "migrate_002_boom"))

    def test_baseline_dry_run_does_not_write(self):
        _write_boom_migration(self.mig_dir, "migrate_001_boom.py", "migrate_001_boom")

        buf = io.StringIO()
        with unittest.mock.patch("sys.stdout", buf):
            with unittest.mock.patch.object(
                _bp_module, "_pending_migrations", self._pending_in_tmpdir
            ):
                cmd_migrate(self._args(baseline=True, dry_run=True))

        self.assertFalse(_name_applied(self.db_path, "migrate_001_boom"))

    def test_baseline_is_idempotent(self):
        _write_boom_migration(self.mig_dir, "migrate_001_boom.py", "migrate_001_boom")

        with unittest.mock.patch.object(_bp_module, "_pending_migrations", self._pending_in_tmpdir):
            cmd_migrate(self._args(baseline=True))
            cmd_migrate(self._args(baseline=True))  # second run: no error

        self.assertTrue(_name_applied(self.db_path, "migrate_001_boom"))


# ---------------------------------------------------------------------------
# migrate_003 — regression guard (#231 bug: MIGRATION_NAME was missing)
# ---------------------------------------------------------------------------


class TestMigrate003(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        # migrate_003 needs schema_migrations to self-register; create it first.
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TABLE schema_migrations "
            "(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, applied_at TEXT NOT NULL)"
        )
        conn.execute(
            # Minimal columns required by the three indexes migrate_003 creates.
            "CREATE TABLE photos "
            "(id INTEGER PRIMARY KEY, privacy_state TEXT, "
            " fingerprint TEXT, original_filename TEXT, date_taken TEXT)"
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db_path + ext)
            except FileNotFoundError:
                pass

    def test_migration_name_constant_is_defined(self):
        mod = _load_migration("migrate_003_dimensions_and_dedup.py")
        self.assertTrue(
            hasattr(mod, "MIGRATION_NAME"),
            "migrate_003 must define MIGRATION_NAME so the runner can discover it",
        )
        self.assertEqual(mod.MIGRATION_NAME, "migrate_003_dimensions_and_dedup")

    def test_run_adds_columns_and_registers(self):
        mod = _load_migration("migrate_003_dimensions_and_dedup.py")
        mod.run(self.db_path, dry_run=False)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cols = {r[1] for r in conn.execute("PRAGMA table_info(photos)").fetchall()}
        conn.close()

        self.assertIn("duplicate_group_id", cols)
        self.assertIn("duplicate_role", cols)
        self.assertTrue(
            _name_applied(self.db_path, "migrate_003_dimensions_and_dedup"),
            "migrate_003 must record itself in schema_migrations",
        )

    def test_dry_run_does_not_register(self):
        mod = _load_migration("migrate_003_dimensions_and_dedup.py")
        mod.run(self.db_path, dry_run=True)
        self.assertFalse(
            _name_applied(self.db_path, "migrate_003_dimensions_and_dedup"),
            "dry_run must not write to schema_migrations",
        )

    def test_runner_discovers_migrate_003(self):
        """_pending_migrations must find migrate_003 when it is not yet applied."""
        result = _pending_migrations(self.db_path, _MIG_DIR)
        names = [name for name, _ in result]
        self.assertIn(
            "migrate_003_dimensions_and_dedup",
            names,
            "_pending_migrations must discover migrate_003 via MIGRATION_NAME",
        )


# ---------------------------------------------------------------------------
# Static lint: every migration file must have MIGRATION_NAME
# ---------------------------------------------------------------------------


class TestAllMigrationsHaveMigrationName(unittest.TestCase):
    # 001 and 002 predate the MIGRATION_NAME pattern. 002 creates the
    # schema_migrations table itself and retroactively records 001. Neither
    # has a column-missing bug because the runner applies them on fresh DBs
    # before schema_migrations exists. They are excluded from this check.
    _EXEMPT = {"migrate_001_privacy_state_check.py", "migrate_002_updated_at_and_indexes.py"}

    _name_re = __import__("re").compile(r"^MIGRATION_NAME\s*=", __import__("re").MULTILINE)

    def test_all_migration_files_have_migration_name(self):
        missing = []
        for mf in sorted(_MIG_DIR.glob("migrate_*.py")):
            if mf.name in self._EXEMPT:
                continue
            if not self._name_re.search(mf.read_text()):
                missing.append(mf.name)
        self.assertEqual(
            missing,
            [],
            f"Migration file(s) missing MIGRATION_NAME constant: {missing}",
        )


if __name__ == "__main__":
    unittest.main()
