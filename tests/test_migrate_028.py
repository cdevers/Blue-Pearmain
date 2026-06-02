"""Migration 028 — rebuild operation_log, metadata_conflicts, duplicate_groups
to clear stale SQLite FK metadata (#183)."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def _fresh_db_with_stale_fk_state() -> sqlite3.Connection:
    """
    Create an in-memory DB that reproduces the stale-FK bug for the three
    affected tables.  Mirrors what happened in production:

    1. photos is created.
    2. operation_log, metadata_conflicts, duplicate_groups are created with
       FKs → photos.
    3. A migration rebuilds photos (rename → create-new → copy → drop-old).
    4. Now the three tables have stale internal FK metadata pointing to the
       ghost photos_old stub.  Any write to those tables raises OperationalError.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    # Step 1: base schema
    conn.execute("""
        CREATE TABLE schema_migrations (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT UNIQUE NOT NULL,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE photos (
            id            INTEGER PRIMARY KEY,
            privacy_state TEXT NOT NULL DEFAULT 'candidate_public'
        )
    """)
    # Step 2: create the three FK-holding tables (before photos is rebuilt)
    conn.execute("""
        CREATE TABLE operation_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            occurred_at TEXT NOT NULL,
            photo_id    INTEGER REFERENCES photos(id),
            operation   TEXT NOT NULL,
            target      TEXT,
            old_value   TEXT,
            new_value   TEXT,
            trigger     TEXT,
            actor       TEXT NOT NULL DEFAULT 'bp'
        )
    """)
    conn.execute("CREATE INDEX idx_operation_log_photo ON operation_log(photo_id)")
    conn.execute("CREATE INDEX idx_operation_log_operation ON operation_log(operation)")
    conn.execute("CREATE INDEX idx_operation_log_occurred ON operation_log(occurred_at)")
    conn.execute("""
        CREATE TABLE metadata_conflicts (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            photo_id        INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
            field           TEXT NOT NULL
                                CHECK(field IN ('title', 'description', 'tags')),
            flickr_value    TEXT,
            photos_value    TEXT,
            resolved        INTEGER DEFAULT 0,
            resolution      TEXT
                                CHECK(resolution IS NULL OR
                                      resolution IN ('flickr', 'photos', 'manual')),
            resolved_at     TEXT,
            created_at      TEXT NOT NULL,
            UNIQUE(photo_id, field)
        )
    """)
    conn.execute("CREATE INDEX idx_metadata_conflicts_photo ON metadata_conflicts(photo_id)")
    conn.execute(
        "CREATE INDEX idx_metadata_conflicts_unresolved "
        "ON metadata_conflicts(resolved) WHERE resolved = 0"
    )
    conn.execute("""
        CREATE TABLE duplicate_groups (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            match_key       TEXT NOT NULL UNIQUE,
            group_type      TEXT NOT NULL,
            photo_count     INTEGER NOT NULL DEFAULT 0,
            keeper_id       INTEGER REFERENCES photos(id),
            resolved        INTEGER NOT NULL DEFAULT 0,
            resolved_at     TEXT,
            notes           TEXT,
            created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        )
    """)
    conn.execute("INSERT INTO photos (id) VALUES (1), (2)")
    conn.commit()

    # Step 3: rebuild photos via rename/create/copy/drop (migrations 001/016 pattern)
    conn.execute("ALTER TABLE photos RENAME TO photos_old")
    conn.execute("""
        CREATE TABLE photos (
            id            INTEGER PRIMARY KEY,
            privacy_state TEXT CHECK(privacy_state IN (
                              'candidate_public','public','private','needs_review','auto_private'))
                          NOT NULL DEFAULT 'candidate_public'
        )
    """)
    conn.execute("INSERT INTO photos SELECT * FROM photos_old")
    conn.execute("DROP TABLE photos_old")
    conn.commit()

    # Step 4: verify the bug is present on this SQLite build (may not reproduce
    # on all versions, but migration must still run cleanly either way)
    try:
        conn.execute(
            "INSERT INTO operation_log (occurred_at, operation, photo_id, actor) "
            "VALUES ('2026-01-01T00:00:00', 'test', 1, 'bp')"
        )
        conn.commit()
        # Bug not present on this build — OK, migration should still run cleanly.
        # Roll back so we start with an empty operation_log.
        conn.execute("DELETE FROM operation_log")
        conn.commit()
    except sqlite3.OperationalError as e:
        if "photos_old" not in str(e):
            raise
        conn.rollback()

    return conn


def _run_migration(conn: sqlite3.Connection) -> None:
    import sys

    sys.path.insert(0, str(Path(__file__).parent.parent))
    from db.migrations.migrate_028_rebuild_fk_tables import run_on_conn

    run_on_conn(conn)


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _indexes(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA index_list({table})").fetchall()}


class TestMigrate028:
    # ------------------------------------------------------------------
    # Tables preserved
    # ------------------------------------------------------------------

    def test_operation_log_table_preserved(self):
        """operation_log still exists after migration."""
        conn = _fresh_db_with_stale_fk_state()
        _run_migration(conn)
        assert "operation_log" in _tables(conn)

    def test_metadata_conflicts_table_preserved(self):
        """metadata_conflicts still exists after migration."""
        conn = _fresh_db_with_stale_fk_state()
        _run_migration(conn)
        assert "metadata_conflicts" in _tables(conn)

    def test_duplicate_groups_table_preserved(self):
        """duplicate_groups still exists after migration."""
        conn = _fresh_db_with_stale_fk_state()
        _run_migration(conn)
        assert "duplicate_groups" in _tables(conn)

    # ------------------------------------------------------------------
    # Columns intact
    # ------------------------------------------------------------------

    def test_operation_log_columns_intact(self):
        conn = _fresh_db_with_stale_fk_state()
        _run_migration(conn)
        assert _cols(conn, "operation_log") == {
            "id",
            "occurred_at",
            "photo_id",
            "operation",
            "target",
            "old_value",
            "new_value",
            "trigger",
            "actor",
        }

    def test_metadata_conflicts_columns_intact(self):
        conn = _fresh_db_with_stale_fk_state()
        _run_migration(conn)
        assert _cols(conn, "metadata_conflicts") == {
            "id",
            "photo_id",
            "field",
            "flickr_value",
            "photos_value",
            "resolved",
            "resolution",
            "resolved_at",
            "created_at",
        }

    def test_duplicate_groups_columns_intact(self):
        conn = _fresh_db_with_stale_fk_state()
        _run_migration(conn)
        assert _cols(conn, "duplicate_groups") == {
            "id",
            "match_key",
            "group_type",
            "photo_count",
            "keeper_id",
            "resolved",
            "resolved_at",
            "notes",
            "created_at",
            "updated_at",
        }

    # ------------------------------------------------------------------
    # Existing rows preserved
    # ------------------------------------------------------------------

    def test_operation_log_rows_preserved(self):
        """Rows that existed before migration survive the rebuild."""
        conn = _fresh_db_with_stale_fk_state()
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            "INSERT INTO operation_log (occurred_at, operation, photo_id, actor) "
            "VALUES ('2026-01-01T00:00:00', 'set_state', 1, 'bp')"
        )
        conn.execute("PRAGMA foreign_keys = ON")
        conn.commit()

        _run_migration(conn)

        rows = conn.execute("SELECT operation FROM operation_log").fetchall()
        assert len(rows) == 1
        assert rows[0]["operation"] == "set_state"

    def test_metadata_conflicts_rows_preserved(self):
        conn = _fresh_db_with_stale_fk_state()
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            "INSERT INTO metadata_conflicts "
            "(photo_id, field, flickr_value, photos_value, created_at) "
            "VALUES (1, 'title', 'Flickr title', 'Photos title', '2026-01-01T00:00:00')"
        )
        conn.execute("PRAGMA foreign_keys = ON")
        conn.commit()

        _run_migration(conn)

        rows = conn.execute("SELECT photo_id, field FROM metadata_conflicts").fetchall()
        assert len(rows) == 1
        assert rows[0]["field"] == "title"

    def test_duplicate_groups_rows_preserved(self):
        conn = _fresh_db_with_stale_fk_state()
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            "INSERT INTO duplicate_groups "
            "(match_key, group_type, photo_count, keeper_id, created_at, updated_at) "
            "VALUES ('IMG_0001.JPG|2026-01-01', 'snapbridge', 2, 1, "
            "        strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), "
            "        strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))"
        )
        conn.execute("PRAGMA foreign_keys = ON")
        conn.commit()

        _run_migration(conn)

        rows = conn.execute("SELECT match_key FROM duplicate_groups").fetchall()
        assert len(rows) == 1
        assert rows[0]["match_key"] == "IMG_0001.JPG|2026-01-01"

    # ------------------------------------------------------------------
    # Writes work after migration
    # ------------------------------------------------------------------

    def test_operation_log_insert_works_after_migration(self):
        """INSERT into operation_log no longer raises after migration."""
        conn = _fresh_db_with_stale_fk_state()
        _run_migration(conn)
        conn.execute(
            "INSERT INTO operation_log (occurred_at, operation, photo_id, actor) "
            "VALUES ('2026-06-01T00:00:00', 'match_legacy_apply', 1, 'bp')"
        )
        conn.commit()
        row = conn.execute("SELECT operation FROM operation_log").fetchone()
        assert row is not None
        assert row["operation"] == "match_legacy_apply"

    def test_metadata_conflicts_insert_works_after_migration(self):
        """INSERT into metadata_conflicts no longer raises after migration."""
        conn = _fresh_db_with_stale_fk_state()
        _run_migration(conn)
        conn.execute(
            "INSERT INTO metadata_conflicts "
            "(photo_id, field, flickr_value, created_at) "
            "VALUES (1, 'description', 'some desc', '2026-06-01T00:00:00')"
        )
        conn.commit()
        row = conn.execute("SELECT field FROM metadata_conflicts WHERE photo_id = 1").fetchone()
        assert row is not None
        assert row["field"] == "description"

    def test_duplicate_groups_insert_works_after_migration(self):
        """INSERT into duplicate_groups no longer raises after migration."""
        conn = _fresh_db_with_stale_fk_state()
        _run_migration(conn)
        conn.execute(
            "INSERT INTO duplicate_groups "
            "(match_key, group_type, photo_count, keeper_id, created_at, updated_at) "
            "VALUES ('IMG_0002.JPG|2026-06-01', 'snapbridge', 2, 2, "
            "        strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), "
            "        strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))"
        )
        conn.commit()
        row = conn.execute("SELECT match_key FROM duplicate_groups WHERE keeper_id = 2").fetchone()
        assert row is not None

    # ------------------------------------------------------------------
    # Indexes recreated
    # ------------------------------------------------------------------

    def test_operation_log_indexes_recreated(self):
        conn = _fresh_db_with_stale_fk_state()
        _run_migration(conn)
        idx = _indexes(conn, "operation_log")
        assert "idx_operation_log_photo" in idx
        assert "idx_operation_log_operation" in idx
        assert "idx_operation_log_occurred" in idx

    def test_metadata_conflicts_indexes_recreated(self):
        conn = _fresh_db_with_stale_fk_state()
        _run_migration(conn)
        idx = _indexes(conn, "metadata_conflicts")
        assert "idx_metadata_conflicts_photo" in idx
        assert "idx_metadata_conflicts_unresolved" in idx

    # ------------------------------------------------------------------
    # schema_migrations entry
    # ------------------------------------------------------------------

    def test_schema_migrations_entry_added(self):
        conn = _fresh_db_with_stale_fk_state()
        _run_migration(conn)
        row = conn.execute(
            "SELECT name FROM schema_migrations WHERE name = 'migrate_028_rebuild_fk_tables'"
        ).fetchone()
        assert row is not None

    # ------------------------------------------------------------------
    # Idempotent
    # ------------------------------------------------------------------

    def test_idempotent_second_run(self):
        """Running migration twice does not duplicate the schema_migrations row."""
        conn = _fresh_db_with_stale_fk_state()
        _run_migration(conn)
        _run_migration(conn)
        rows = conn.execute(
            "SELECT name FROM schema_migrations WHERE name = 'migrate_028_rebuild_fk_tables'"
        ).fetchall()
        assert len(rows) == 1

    # ------------------------------------------------------------------
    # No stale _old tables left behind
    # ------------------------------------------------------------------

    def test_no_stale_old_tables(self):
        """The _old temporary tables are fully cleaned up."""
        conn = _fresh_db_with_stale_fk_state()
        _run_migration(conn)
        tables = _tables(conn)
        assert "operation_log_old" not in tables
        assert "metadata_conflicts_old" not in tables
        assert "duplicate_groups_old" not in tables
