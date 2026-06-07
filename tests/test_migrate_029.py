"""Migration 029 — add date_precision and date_approximate columns (#157)."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def _fresh_db() -> sqlite3.Connection:
    """Minimal in-memory DB with photos and legacy_assets but no precision columns."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE schema_migrations (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT UNIQUE NOT NULL,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE photos (
            id            INTEGER PRIMARY KEY,
            date_taken    TEXT
        );
        CREATE TABLE legacy_assets (
            id            INTEGER PRIMARY KEY,
            date_taken    TEXT
        );
    """)
    return conn


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _run(conn: sqlite3.Connection) -> None:
    import sys

    sys.path.insert(0, str(Path(__file__).parent.parent))
    from db.migrations.migrate_029_date_precision import run_on_conn

    run_on_conn(conn)


class TestMigrate029:
    def test_photos_date_precision_added(self):
        conn = _fresh_db()
        _run(conn)
        assert "date_precision" in _cols(conn, "photos")

    def test_photos_date_approximate_added(self):
        conn = _fresh_db()
        _run(conn)
        assert "date_approximate" in _cols(conn, "photos")

    def test_legacy_assets_date_precision_added(self):
        conn = _fresh_db()
        _run(conn)
        assert "date_precision" in _cols(conn, "legacy_assets")

    def test_legacy_assets_date_approximate_added(self):
        conn = _fresh_db()
        _run(conn)
        assert "date_approximate" in _cols(conn, "legacy_assets")

    def test_date_precision_default_is_exact(self):
        conn = _fresh_db()
        conn.execute("INSERT INTO photos (id, date_taken) VALUES (1, '2020-01-01')")
        conn.commit()
        _run(conn)
        row = conn.execute("SELECT date_precision FROM photos WHERE id = 1").fetchone()
        assert row["date_precision"] == "exact"

    def test_date_approximate_default_is_zero(self):
        conn = _fresh_db()
        conn.execute("INSERT INTO photos (id, date_taken) VALUES (1, '2020-01-01')")
        conn.commit()
        _run(conn)
        row = conn.execute("SELECT date_approximate FROM photos WHERE id = 1").fetchone()
        assert row["date_approximate"] == 0

    def test_existing_rows_preserved(self):
        conn = _fresh_db()
        conn.execute("INSERT INTO photos (id, date_taken) VALUES (99, '2010-05-01')")
        conn.execute("INSERT INTO legacy_assets (id, date_taken) VALUES (99, '2010-05-01')")
        conn.commit()
        _run(conn)
        photos_row = conn.execute("SELECT id, date_taken FROM photos WHERE id = 99").fetchone()
        legacy_row = conn.execute(
            "SELECT id, date_taken FROM legacy_assets WHERE id = 99"
        ).fetchone()
        assert photos_row["id"] == 99
        assert photos_row["date_taken"] == "2010-05-01"
        assert legacy_row["id"] == 99

    def test_idempotent(self):
        conn = _fresh_db()
        _run(conn)
        _run(conn)  # must not raise
        assert "date_precision" in _cols(conn, "photos")

    def test_recorded_in_schema_migrations(self):
        conn = _fresh_db()
        _run(conn)
        row = conn.execute(
            "SELECT name FROM schema_migrations WHERE name = 'migrate_029_date_precision'"
        ).fetchone()
        assert row is not None

    def test_succeeds_without_legacy_assets_table(self):
        # Migration should complete cleanly when legacy_assets hasn't been created yet
        # (i.e. migration 026 hasn't run). Only the photos columns should be added.
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE schema_migrations (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT UNIQUE NOT NULL,
                applied_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE photos (
                id         INTEGER PRIMARY KEY,
                date_taken TEXT
            );
        """)
        _run(conn)  # must not raise
        assert "date_precision" in _cols(conn, "photos")
        assert "legacy_assets" not in {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
