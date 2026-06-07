"""Migration 030 — add nominatim_cache table (#217)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))


def _fresh_db() -> sqlite3.Connection:
    """Minimal in-memory DB without nominatim_cache."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE schema_migrations (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT UNIQUE NOT NULL,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE photos (
            id        INTEGER PRIMARY KEY,
            latitude  REAL,
            longitude REAL
        );
    """)
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _run(conn: sqlite3.Connection) -> None:
    from db.migrations.migrate_030_nominatim_cache import run_on_conn

    run_on_conn(conn)


class TestMigrate030:
    def test_creates_nominatim_cache_table(self):
        conn = _fresh_db()
        _run(conn)
        assert "nominatim_cache" in _tables(conn)

    def test_table_has_expected_columns(self):
        conn = _fresh_db()
        _run(conn)
        cols = _cols(conn, "nominatim_cache")
        assert "lat_rounded" in cols
        assert "lon_rounded" in cols
        assert "place_city" in cols
        assert "place_state" in cols
        assert "place_country" in cols
        assert "place_country_code" in cols
        assert "place_neighborhood" in cols
        assert "place_address" in cols
        assert "fetched_at" in cols

    def test_idempotent(self):
        conn = _fresh_db()
        _run(conn)
        _run(conn)  # must not raise
        assert "nominatim_cache" in _tables(conn)

    def test_recorded_in_schema_migrations(self):
        conn = _fresh_db()
        _run(conn)
        row = conn.execute(
            "SELECT name FROM schema_migrations WHERE name = 'migrate_030_nominatim_cache'"
        ).fetchone()
        assert row is not None

    def test_place_fields_nullable(self):
        conn = _fresh_db()
        _run(conn)
        # All place fields should allow NULL (for caching "no result" entries)
        conn.execute(
            "INSERT INTO nominatim_cache (lat_rounded, lon_rounded, fetched_at) VALUES (1.0, 2.0, '2026-01-01T00:00:00Z')"
        )
        conn.commit()
        row = conn.execute("SELECT place_city FROM nominatim_cache").fetchone()
        assert row["place_city"] is None
