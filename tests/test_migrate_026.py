"""Migration 026 — legacy_libraries + legacy_assets tables (#162)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


def _fresh_db_up_to_025() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE schema_migrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            applied_at TEXT NOT NULL
        );
    """)
    return conn


def _run_migration(conn: sqlite3.Connection) -> None:
    import sys

    sys.path.insert(0, str(Path(__file__).parent.parent))
    from db.migrations.migrate_026_legacy_index import run_on_conn

    run_on_conn(conn)


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


class TestMigrate026:
    def test_legacy_libraries_table_created(self):
        conn = _fresh_db_up_to_025()
        _run_migration(conn)
        assert "legacy_libraries" in _tables(conn)

    def test_legacy_assets_table_created(self):
        conn = _fresh_db_up_to_025()
        _run_migration(conn)
        assert "legacy_assets" in _tables(conn)

    def test_legacy_libraries_columns(self):
        conn = _fresh_db_up_to_025()
        _run_migration(conn)
        assert _cols(conn, "legacy_libraries") >= {
            "library_uuid",
            "display_name",
            "source_path_last_seen",
            "schema_version",
            "db_mtime",
            "db_size",
            "db_head_hash",
            "asset_count",
            "indexed_at",
        }

    def test_legacy_assets_columns(self):
        conn = _fresh_db_up_to_025()
        _run_migration(conn)
        assert _cols(conn, "legacy_assets") >= {
            "id",
            "library_uuid",
            "asset_uuid",
            "original_filename",
            "fingerprint",
            "date_taken",
            "width",
            "height",
            "latitude",
            "longitude",
            "title",
            "description",
            "keywords",
            "labels",
            "persons",
            "named_face_count",
            "unknown_face_count",
            "master_rel_path",
            "thumbnail_cache_key",
            "thumbnail_status",
            "indexed_at",
        }

    def test_legacy_assets_unique_identity(self):
        conn = _fresh_db_up_to_025()
        _run_migration(conn)
        conn.execute("INSERT INTO legacy_assets (library_uuid, asset_uuid) VALUES ('L', 'A')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO legacy_assets (library_uuid, asset_uuid) VALUES ('L', 'A')")

    def test_indexes_present(self):
        conn = _fresh_db_up_to_025()
        _run_migration(conn)
        idx = {r[1] for r in conn.execute("PRAGMA index_list(legacy_assets)").fetchall()}
        assert "idx_legacy_assets_date" in idx
        assert "idx_legacy_assets_dims" in idx

    def test_idempotent_second_run(self):
        conn = _fresh_db_up_to_025()
        _run_migration(conn)
        _run_migration(conn)
        rows = conn.execute(
            "SELECT name FROM schema_migrations WHERE name='migrate_026_legacy_index'"
        ).fetchall()
        assert len(rows) == 1

    def test_schema_migrations_entry_added(self):
        conn = _fresh_db_up_to_025()
        _run_migration(conn)
        row = conn.execute(
            "SELECT name FROM schema_migrations WHERE name='migrate_026_legacy_index'"
        ).fetchone()
        assert row is not None
