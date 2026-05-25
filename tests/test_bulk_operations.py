"""
tests/test_bulk_operations.py — tests for bulk operations (#133)

Run from repo root:
    python -m pytest tests/test_bulk_operations.py -v
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


# ===========================================================================
# Task 1 — Migration 023
# ===========================================================================


def _import_migration_023():
    spec = importlib.util.spec_from_file_location(
        "migrate_023_bulk_batches",
        Path(__file__).parent.parent / "db" / "migrations" / "migrate_023_bulk_batches.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestMigration023(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "test.db")
        conn = sqlite3.connect(self.db_path)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS schema_migrations
                (id INTEGER PRIMARY KEY, name TEXT UNIQUE, applied_at TEXT);
            CREATE TABLE IF NOT EXISTS photos (id INTEGER PRIMARY KEY, uuid TEXT);
            CREATE TABLE IF NOT EXISTS metadata_proposals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                photo_id INTEGER NOT NULL REFERENCES photos(id),
                field TEXT NOT NULL,
                proposed_value TEXT,
                source TEXT NOT NULL,
                target TEXT NOT NULL,
                conflict_type TEXT NOT NULL,
                source_hash_at_creation TEXT,
                target_hash_at_creation TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                resolved_at TEXT,
                resolution_note TEXT
            );
        """)
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_creates_bulk_batches_table(self):
        mod = _import_migration_023()
        mod.run(self.db_path)
        conn = sqlite3.connect(self.db_path)
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        conn.close()
        self.assertIn("bulk_batches", tables)

    def test_adds_batch_id_to_proposals(self):
        mod = _import_migration_023()
        mod.run(self.db_path)
        conn = sqlite3.connect(self.db_path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(metadata_proposals)").fetchall()}
        conn.close()
        self.assertIn("batch_id", cols)

    def test_batch_id_is_nullable(self):
        """Existing proposals survive migration with batch_id=NULL."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO photos (uuid) VALUES ('u1')")
        conn.execute("""INSERT INTO metadata_proposals
            (photo_id, field, source, target, conflict_type, status, created_at)
            VALUES (1, 'title', 'flickr', 'photos', 'non_conflict', 'pending', '2026-01-01')""")
        conn.commit()
        conn.close()
        mod = _import_migration_023()
        mod.run(self.db_path)
        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT batch_id FROM metadata_proposals WHERE id=1").fetchone()
        conn.close()
        self.assertIsNone(row[0])

    def test_migration_idempotent(self):
        mod = _import_migration_023()
        mod.run(self.db_path)
        mod.run(self.db_path)  # must not raise

    def test_bulk_batches_columns(self):
        mod = _import_migration_023()
        mod.run(self.db_path)
        conn = sqlite3.connect(self.db_path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(bulk_batches)").fetchall()}
        conn.close()
        self.assertGreaterEqual(
            cols,
            {"id", "operation", "field", "value", "tags", "filter", "photo_count", "created_at"},
        )
