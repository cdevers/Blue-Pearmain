"""Migration 024 — geo_confirmed_none, geo cache cols, proposals CHECK (#145)"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


def _fresh_db_up_to_023() -> sqlite3.Connection:
    """Create an in-memory DB that looks like a post-023 installation."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE schema_migrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            applied_at TEXT NOT NULL
        );
        CREATE TABLE photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uuid TEXT UNIQUE,
            flickr_id TEXT UNIQUE,
            latitude REAL,
            longitude REAL,
            flickr_deleted INTEGER DEFAULT 0
        );
        CREATE TABLE bulk_batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            operation TEXT NOT NULL
        );
        CREATE TABLE metadata_proposals (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            photo_id                INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
            field                   TEXT NOT NULL
                                        CHECK(field IN ('title', 'description', 'tags')),
            proposed_value          TEXT,
            source                  TEXT NOT NULL
                                        CHECK(source IN ('flickr', 'photos', 'manual')),
            target                  TEXT NOT NULL
                                        CHECK(target IN ('flickr', 'photos')),
            conflict_type           TEXT NOT NULL
                                        CHECK(conflict_type IN ('non_conflict', 'divergence', 'collision')),
            source_hash_at_creation TEXT,
            target_hash_at_creation TEXT,
            status                  TEXT NOT NULL DEFAULT 'pending'
                                        CHECK(status IN ('pending', 'applied', 'rejected', 'superseded', 'failed')),
            created_at              TEXT NOT NULL,
            resolved_at             TEXT,
            resolution_note         TEXT,
            batch_id                INTEGER REFERENCES bulk_batches(id)
        );
        -- Seed one existing proposal row
        INSERT INTO photos (uuid) VALUES ('test-uuid-1');
        INSERT INTO metadata_proposals (photo_id, field, source, target, conflict_type, created_at)
        VALUES (1, 'tags', 'flickr', 'photos', 'non_conflict', '2026-01-01T00:00:00');
    """)
    return conn


def _run_migration(conn: sqlite3.Connection) -> None:
    import sys

    sys.path.insert(0, str(Path(__file__).parent.parent))
    from db.migrations.migrate_024_geo_sync import run_on_conn

    run_on_conn(conn)


class TestMigrate024:
    def test_geo_confirmed_none_column_added(self):
        conn = _fresh_db_up_to_023()
        _run_migration(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(photos)").fetchall()}
        assert "geo_confirmed_none" in cols

    def test_geo_confirmed_none_default_zero(self):
        conn = _fresh_db_up_to_023()
        _run_migration(conn)
        conn.execute("INSERT INTO photos (uuid) VALUES ('new-uuid')")
        row = conn.execute("SELECT geo_confirmed_none FROM photos WHERE uuid='new-uuid'").fetchone()
        assert row["geo_confirmed_none"] == 0

    def test_flickr_lat_lon_columns_added(self):
        conn = _fresh_db_up_to_023()
        _run_migration(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(photos)").fetchall()}
        assert "flickr_latitude" in cols
        assert "flickr_longitude" in cols

    def test_photos_lat_lon_columns_added(self):
        conn = _fresh_db_up_to_023()
        _run_migration(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(photos)").fetchall()}
        assert "photos_latitude" in cols
        assert "photos_longitude" in cols

    def test_proposals_check_allows_geo_location(self):
        conn = _fresh_db_up_to_023()
        _run_migration(conn)
        conn.execute(
            "INSERT INTO metadata_proposals (photo_id, field, source, target, conflict_type, created_at)"
            " VALUES (1, 'geo_location', 'flickr', 'photos', 'non_conflict', '2026-01-01T00:00:00')"
        )
        row = conn.execute(
            "SELECT field FROM metadata_proposals WHERE field='geo_location'"
        ).fetchone()
        assert row["field"] == "geo_location"

    def test_proposals_check_still_rejects_invalid_field(self):
        conn = _fresh_db_up_to_023()
        _run_migration(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO metadata_proposals (photo_id, field, source, target, conflict_type, created_at)"
                " VALUES (1, 'invalid_field', 'flickr', 'photos', 'non_conflict', '2026-01-01T00:00:00')"
            )

    def test_existing_proposals_rows_preserved(self):
        conn = _fresh_db_up_to_023()
        _run_migration(conn)
        rows = conn.execute("SELECT field FROM metadata_proposals WHERE field='tags'").fetchall()
        assert len(rows) == 1

    def test_idempotent_second_run(self):
        conn = _fresh_db_up_to_023()
        _run_migration(conn)
        _run_migration(conn)
        rows = conn.execute(
            "SELECT name FROM schema_migrations WHERE name='migrate_024_geo_sync'"
        ).fetchall()
        assert len(rows) == 1

    def test_schema_migrations_entry_added(self):
        conn = _fresh_db_up_to_023()
        _run_migration(conn)
        row = conn.execute(
            "SELECT name FROM schema_migrations WHERE name='migrate_024_geo_sync'"
        ).fetchone()
        assert row is not None
