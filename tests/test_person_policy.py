"""
tests/test_person_policy.py — tests for per-person privacy policy

Run from repo root:
    python -m pytest tests/test_person_policy.py -v
"""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.db import Database
from db.migrations.migrate_019_person_policies import run as run_migration, _already_migrated


def _tmp_db() -> tuple[sqlite3.Connection, str]:
    """Create a minimal throw-away SQLite DB."""
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    conn = sqlite3.connect(f.name)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    conn.commit()
    return conn, f.name


class TestMigration019(unittest.TestCase):
    def test_creates_person_policies_table(self):
        conn, path = _tmp_db()
        run_migration(path)
        tables = [
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        ]
        self.assertIn("person_policies", tables)
        conn.close()

    def test_table_has_expected_columns(self):
        conn, path = _tmp_db()
        run_migration(path)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(person_policies)").fetchall()]
        for col in ("id", "person_name", "policy", "created_at"):
            self.assertIn(col, cols)
        conn.close()

    def test_idempotent_second_run_does_not_fail(self):
        conn, path = _tmp_db()
        run_migration(path)
        run_migration(path)  # must not raise
        conn.close()

    def test_already_migrated_returns_true_after_run(self):
        conn, path = _tmp_db()
        run_migration(path)
        self.assertTrue(_already_migrated(conn))
        conn.close()

    def test_already_migrated_returns_false_before_run(self):
        conn, path = _tmp_db()
        self.assertFalse(_already_migrated(conn))
        conn.close()


def _make_db_with_migration() -> Database:
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    db = Database(Path(f.name))
    from db.migrations.migrate_019_person_policies import run as migrate

    migrate(f.name)
    return db


class TestPersonPolicyDbMethods(unittest.TestCase):
    def test_get_person_policies_returns_empty_dict_initially(self):
        db = _make_db_with_migration()
        result = db.get_person_policies()
        db.close()
        self.assertEqual(result, {})

    def test_set_person_policy_stores_a_policy(self):
        db = _make_db_with_migration()
        db.set_person_policy("Alice", "always_private")
        result = db.get_person_policies()
        db.close()
        self.assertEqual(result.get("Alice"), "always_private")

    def test_set_person_policy_upserts_on_duplicate_name(self):
        db = _make_db_with_migration()
        db.set_person_policy("Alice", "always_private")
        db.set_person_policy("Alice", "always_private")  # must not raise
        result = db.get_person_policies()
        db.close()
        self.assertEqual(list(result.keys()).count("Alice"), 1)

    def test_delete_person_policy_removes_entry(self):
        db = _make_db_with_migration()
        db.set_person_policy("Bob", "always_private")
        db.delete_person_policy("Bob")
        result = db.get_person_policies()
        db.close()
        self.assertNotIn("Bob", result)

    def test_delete_person_policy_no_op_when_not_present(self):
        db = _make_db_with_migration()
        db.delete_person_policy("Nobody")  # must not raise
        db.close()

    def test_get_person_policies_returns_all_policies(self):
        db = _make_db_with_migration()
        db.set_person_policy("Alice", "always_private")
        db.set_person_policy("Charlie", "always_private")
        result = db.get_person_policies()
        db.close()
        self.assertIn("Alice", result)
        self.assertIn("Charlie", result)


class TestClassifyWithPersonPolicies(unittest.TestCase):
    def _photo(self, persons):
        return {"apple_persons": persons, "place_ishome": False}

    def test_always_private_policy_overrides_needs_review(self):
        """A policy-protected person → auto_private, not needs_review."""
        from analyzer.privacy import classify

        photo = self._photo(["Alice"])
        state, reason = classify(
            photo,
            zones=[],
            self_name="Me",
            person_policies={"Alice": "always_private"},
        )
        self.assertEqual(state, "auto_private")
        self.assertIn("Alice", reason)

    def test_always_private_includes_person_name_in_reason(self):
        from analyzer.privacy import classify

        photo = self._photo(["Bob"])
        _, reason = classify(
            photo,
            zones=[],
            self_name="Me",
            person_policies={"Bob": "always_private"},
        )
        self.assertIn("person policy", reason)
        self.assertIn("Bob", reason)

    def test_no_policy_for_person_falls_through_to_needs_review(self):
        """A named person with no policy still triggers needs_review."""
        from analyzer.privacy import classify

        photo = self._photo(["Alice"])
        state, _ = classify(
            photo,
            zones=[],
            self_name="Me",
            person_policies={},
        )
        self.assertEqual(state, "needs_review")

    def test_self_name_excluded_from_policy_check(self):
        """The photographer's own name is never matched against policies."""
        from analyzer.privacy import classify

        photo = self._photo(["Me"])
        state, _ = classify(
            photo,
            zones=[],
            self_name="Me",
            person_policies={"Me": "always_private"},
        )
        # Photo of only self → candidate_public (no other persons)
        self.assertEqual(state, "candidate_public")

    def test_policy_on_one_person_triggers_even_when_other_persons_present(self):
        """If any person has always_private policy, the photo is auto_private."""
        from analyzer.privacy import classify

        photo = self._photo(["Alice", "Bob"])
        state, _ = classify(
            photo,
            zones=[],
            self_name="Me",
            person_policies={"Alice": "always_private"},
        )
        self.assertEqual(state, "auto_private")

    def test_no_person_policies_arg_behaves_as_before(self):
        """Omitting person_policies entirely is equivalent to no policies."""
        from analyzer.privacy import classify

        photo = self._photo(["Alice"])
        state, _ = classify(photo, zones=[], self_name="Me")
        self.assertEqual(state, "needs_review")

    def test_home_flag_still_takes_precedence_over_policy(self):
        """Home flag is checked before person policies."""
        from analyzer.privacy import classify

        photo = {"apple_persons": ["Alice"], "place_ishome": True}
        state, reason = classify(
            photo,
            zones=[],
            self_name="Me",
            person_policies={"Alice": "always_private"},
        )
        self.assertEqual(state, "auto_private")
        self.assertIn("home", reason)

    def test_policy_match_is_case_insensitive(self):
        """Policy keyed as 'alice' matches photo person named 'Alice'."""
        from analyzer.privacy import classify

        photo = self._photo(["Alice"])
        state, reason = classify(
            photo,
            zones=[],
            self_name="Me",
            person_policies={"alice": "always_private"},
        )
        self.assertEqual(state, "auto_private")

    def test_already_reviewed_photos_protected_at_db_layer_not_classify(self):
        """
        classify() returns auto_private for a policy-matched photo regardless
        of any prior state — the protection for already-reviewed photos is
        enforced at the DB layer (upsert_photo's already_reviewed check).
        classify() itself is stateless and always re-evaluates.
        """
        from analyzer.privacy import classify

        photo = self._photo(["Alice"])
        state, reason = classify(
            photo,
            zones=[],
            self_name="Me",
            person_policies={"Alice": "always_private"},
        )
        self.assertEqual(state, "auto_private")


class TestScannerPassesPolicies(unittest.TestCase):
    """Verify that build_enriched_row passes person_policies to classify()."""

    def test_build_enriched_row_passes_person_policies_to_classify(self):
        """
        When person_policies contains an always_private entry that matches a
        named person in the photo, build_enriched_row should produce
        privacy_state='auto_private'.
        """
        from poller.scanner import build_enriched_row

        photo_row = {
            "uuid": "test-uuid",
            "filename": "IMG_001.jpg",
            "date": "2024-01-01 12:00:00",
            "latitude": None,
            "longitude": None,
            "place_ishome": False,
            "place": None,
            "apple_persons": ["Alice"],
            "labels": [],
            "face_info": [],
            "media_analysis": {},
            "title": "",
            "description": "",
            "keywords": [],
            "albums": [],
        }

        result = build_enriched_row(
            photo_row,
            existing={},
            zones=[],
            self_name="Me",
            person_policies={"Alice": "always_private"},
        )
        self.assertEqual(result["privacy_state"], "auto_private")
