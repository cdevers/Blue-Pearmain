"""Migration 027 — rebuild photo_albums to clear stale SQLite FK metadata (#178)."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def _fresh_db_with_stale_fk_state() -> sqlite3.Connection:
    """
    Create an in-memory DB that reproduces the 'no such table: main.photos_old'
    bug.  This mirrors what happened in production:

    1. photos was created without a CHECK constraint.
    2. photo_albums was created with an FK → photos.
    3. A migration rebuilt photos (rename → create-with-CHECK → copy → drop-old).
    4. Now photo_albums has stale internal FK metadata pointing to the ghost
       photos_old stub, so any write to photo_albums raises OperationalError.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    # Step 1 & 2: initial schema
    conn.execute("""
        CREATE TABLE schema_migrations (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT UNIQUE NOT NULL,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE albums (
            id   INTEGER PRIMARY KEY,
            name TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE photos (
            id            INTEGER PRIMARY KEY,
            privacy_state TEXT NOT NULL DEFAULT 'candidate_public'
        )
    """)
    conn.execute("""
        CREATE TABLE photo_albums (
            photo_id      INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
            album_id      INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
            flickr_pushed INTEGER DEFAULT 0,
            pushed_at     TEXT,
            removed_at    TEXT,
            PRIMARY KEY (photo_id, album_id)
        )
    """)
    conn.execute("CREATE INDEX idx_photo_albums_photo ON photo_albums(photo_id)")
    conn.execute("CREATE INDEX idx_photo_albums_album ON photo_albums(album_id)")
    conn.execute("INSERT INTO albums (id, name) VALUES (1, 'Test Album')")
    conn.execute("INSERT INTO photos (id) VALUES (1), (2)")
    conn.commit()

    # Step 3: migration rebuilds photos with a CHECK constraint (migrations 001/016 pattern)
    conn.execute("ALTER TABLE photos RENAME TO photos_old")
    conn.execute("""
        CREATE TABLE photos (
            id            INTEGER PRIMARY KEY,
            privacy_state TEXT CHECK(privacy_state IN ('candidate_public','public','private'))
                          NOT NULL DEFAULT 'candidate_public'
        )
    """)
    conn.execute("INSERT INTO photos SELECT * FROM photos_old")
    conn.execute("DROP TABLE photos_old")
    conn.commit()

    # Step 4: photo_albums now has stale FK metadata — verify the bug is present
    try:
        conn.execute("INSERT OR IGNORE INTO photo_albums (photo_id, album_id) VALUES (1, 1)")
        conn.commit()
        # If the INSERT succeeded without error, the bug is not present on this
        # SQLite build — that's fine, but the migration should still run cleanly.
    except sqlite3.OperationalError as e:
        if "photos_old" not in str(e):
            raise  # unexpected error
        conn.rollback()

    return conn


def _run_migration(conn: sqlite3.Connection) -> None:
    import sys

    sys.path.insert(0, str(Path(__file__).parent.parent))
    from db.migrations.migrate_027_rebuild_photo_albums import run_on_conn

    run_on_conn(conn)


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


class TestMigrate027:
    def test_photo_albums_table_preserved(self):
        """photo_albums still exists after the migration."""
        conn = _fresh_db_with_stale_fk_state()
        _run_migration(conn)
        assert "photo_albums" in _tables(conn)

    def test_photo_albums_columns_intact(self):
        """All expected columns survive the rebuild."""
        conn = _fresh_db_with_stale_fk_state()
        _run_migration(conn)
        assert _cols(conn, "photo_albums") == {
            "photo_id",
            "album_id",
            "flickr_pushed",
            "pushed_at",
            "removed_at",
        }

    def test_existing_rows_preserved(self):
        """Rows that existed before the migration are not lost."""
        conn = _fresh_db_with_stale_fk_state()
        # Seed a row using direct INSERT without FK checks (workaround for stale state)
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("INSERT INTO photo_albums (photo_id, album_id) VALUES (2, 1)")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.commit()

        _run_migration(conn)

        rows = conn.execute("SELECT photo_id, album_id FROM photo_albums").fetchall()
        assert len(rows) == 1
        assert rows[0]["photo_id"] == 2
        assert rows[0]["album_id"] == 1

    def test_insert_works_after_migration(self):
        """After migration, INSERT OR IGNORE into photo_albums no longer raises."""
        conn = _fresh_db_with_stale_fk_state()
        _run_migration(conn)
        # Should not raise OperationalError
        conn.execute("INSERT OR IGNORE INTO photo_albums (photo_id, album_id) VALUES (1, 1)")
        conn.commit()
        row = conn.execute("SELECT photo_id FROM photo_albums WHERE photo_id = 1").fetchone()
        assert row is not None

    def test_update_works_after_migration(self):
        """After migration, UPDATE on photo_albums no longer raises."""
        conn = _fresh_db_with_stale_fk_state()
        _run_migration(conn)
        conn.execute("INSERT OR IGNORE INTO photo_albums (photo_id, album_id) VALUES (1, 1)")
        conn.commit()
        # Should not raise
        conn.execute(
            "UPDATE photo_albums SET removed_at = '2026-06-01' WHERE photo_id = 1 AND album_id = 1"
        )
        conn.commit()
        row = conn.execute("SELECT removed_at FROM photo_albums WHERE photo_id = 1").fetchone()
        assert row["removed_at"] == "2026-06-01"

    def test_indexes_recreated(self):
        """Both indexes are present after the migration."""
        conn = _fresh_db_with_stale_fk_state()
        _run_migration(conn)
        idx = {r[1] for r in conn.execute("PRAGMA index_list(photo_albums)").fetchall()}
        assert "idx_photo_albums_photo" in idx
        assert "idx_photo_albums_album" in idx

    def test_schema_migrations_entry_added(self):
        """The migration name is recorded in schema_migrations."""
        conn = _fresh_db_with_stale_fk_state()
        _run_migration(conn)
        row = conn.execute(
            "SELECT name FROM schema_migrations WHERE name = 'migrate_027_rebuild_photo_albums'"
        ).fetchone()
        assert row is not None

    def test_idempotent_second_run(self):
        """Running migration twice does not duplicate the schema_migrations row."""
        conn = _fresh_db_with_stale_fk_state()
        _run_migration(conn)
        _run_migration(conn)
        rows = conn.execute(
            "SELECT name FROM schema_migrations WHERE name = 'migrate_027_rebuild_photo_albums'"
        ).fetchall()
        assert len(rows) == 1
