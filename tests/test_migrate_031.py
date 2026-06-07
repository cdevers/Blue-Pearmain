"""Migration 031 — add uploaded_flickr_id/uploaded_at to legacy_assets (#230)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))


def _make_conn() -> sqlite3.Connection:
    """Create in-memory DB with legacy tables from migration 026."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE schema_migrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            applied_at TEXT NOT NULL
        );
    """)
    from db.migrations.migrate_026_legacy_index import run_on_conn

    run_on_conn(conn)
    return conn


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _run(conn: sqlite3.Connection) -> None:
    from db.migrations.migrate_031_legacy_upload import run_on_conn

    run_on_conn(conn)


class TestMigrate031:
    def test_adds_uploaded_flickr_id_column(self):
        conn = _make_conn()
        _run(conn)
        cols = _cols(conn, "legacy_assets")
        assert "uploaded_flickr_id" in cols

    def test_adds_uploaded_at_column(self):
        conn = _make_conn()
        _run(conn)
        cols = _cols(conn, "legacy_assets")
        assert "uploaded_at" in cols

    def test_idempotent(self):
        conn = _make_conn()
        _run(conn)
        _run(conn)  # must not raise
        cols = _cols(conn, "legacy_assets")
        assert "uploaded_flickr_id" in cols
        assert "uploaded_at" in cols

    def test_defaults_are_null(self):
        conn = _make_conn()
        _run(conn)
        conn.execute("INSERT INTO legacy_libraries (library_uuid, asset_count) VALUES ('L', 0)")
        conn.execute(
            "INSERT INTO legacy_assets (library_uuid, asset_uuid, named_face_count, unknown_face_count) "
            "VALUES ('L', 'A', 0, 0)"
        )
        row = conn.execute(
            "SELECT uploaded_flickr_id, uploaded_at FROM legacy_assets WHERE asset_uuid = 'A'"
        ).fetchone()
        assert row["uploaded_flickr_id"] is None
        assert row["uploaded_at"] is None

    def test_recorded_in_schema_migrations(self):
        conn = _make_conn()
        _run(conn)
        row = conn.execute(
            "SELECT name FROM schema_migrations WHERE name = 'migrate_031_legacy_upload'"
        ).fetchone()
        assert row is not None
