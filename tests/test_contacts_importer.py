"""Tests for poller/contacts_importer.py — birthday import from Apple Contacts (#210)."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.db import Database
from poller.contacts_importer import (
    ImportResult,  # noqa: F401 — imported for type completeness; used once module exists
    _check_contacts_authorization,
    _format_birthday,
    read_photos_person_contacts,
    run_import,
)


class TestFormatBirthday:
    def test_birthday_format_full_date(self) -> None:
        assert _format_birthday(1975, 9, 26) == "1975-09-26"

    def test_birthday_format_yearless(self) -> None:
        # NSDateComponentUndefined on 64-bit is a very large integer (> 9999)
        assert _format_birthday(9223372036854775807, 7, 13) == "07-13"


class TestReadPhotosPersonContacts:
    def test_import_malformed_uri_skipped(self, tmp_path: Path) -> None:
        photos_db = tmp_path / "Photos.sqlite"
        conn = sqlite3.connect(str(photos_db))
        conn.execute("CREATE TABLE ZPERSON (ZFULLNAME TEXT, ZPERSONURI TEXT)")
        conn.execute("INSERT INTO ZPERSON VALUES ('Alice Smith', 'MALFORMED_NO_COLON')")
        conn.commit()
        conn.close()

        result = read_photos_person_contacts(str(photos_db.resolve()))
        assert result == {}


class TestRunImport:
    def _db(self, tmp_path: Path) -> Database:
        return Database(str(tmp_path / "curator.db"))

    def test_import_writes_new_birthday(self, tmp_path: Path) -> None:
        db = self._db(tmp_path)
        result = run_import(
            db,
            "",
            dry_run=False,
            overwrite=False,
            fetcher=lambda uuids: {"UUID-ALICE": "1990-03-15"},
            reader=lambda path: {"Alice Smith": "UUID-ALICE"},
        )
        assert result.written == 1
        assert result.skipped_same == 0
        assert result.skipped_conflict == 0
        assert db.get_person_birthdays()["Alice Smith"] == "1990-03-15"

    def test_import_skips_existing_same_value(self, tmp_path: Path) -> None:
        db = self._db(tmp_path)
        db.set_person_birthday("Alice Smith", "1990-03-15")
        result = run_import(
            db,
            "",
            dry_run=False,
            overwrite=False,
            fetcher=lambda uuids: {"UUID-ALICE": "1990-03-15"},
            reader=lambda path: {"Alice Smith": "UUID-ALICE"},
        )
        assert result.skipped_same == 1
        assert result.written == 0
        assert result.skipped_conflict == 0

    def test_import_skips_existing_different_value_no_overwrite(self, tmp_path: Path) -> None:
        db = self._db(tmp_path)
        db.set_person_birthday("Alice Smith", "1981-01-01")
        result = run_import(
            db,
            "",
            dry_run=False,
            overwrite=False,
            fetcher=lambda uuids: {"UUID-ALICE": "1990-03-15"},
            reader=lambda path: {"Alice Smith": "UUID-ALICE"},
        )
        assert result.skipped_conflict == 1
        assert result.written == 0
        assert db.get_person_birthdays()["Alice Smith"] == "1981-01-01"

    def test_import_overwrites_existing_with_flag(self, tmp_path: Path) -> None:
        db = self._db(tmp_path)
        db.set_person_birthday("Alice Smith", "1981-01-01")
        result = run_import(
            db,
            "",
            dry_run=False,
            overwrite=True,
            fetcher=lambda uuids: {"UUID-ALICE": "1990-03-15"},
            reader=lambda path: {"Alice Smith": "UUID-ALICE"},
        )
        assert result.overwritten == 1
        assert result.written == 0
        assert db.get_person_birthdays()["Alice Smith"] == "1990-03-15"

    def test_import_dry_run_writes_nothing(self, tmp_path: Path) -> None:
        db = self._db(tmp_path)
        result = run_import(
            db,
            "",
            dry_run=True,
            overwrite=False,
            fetcher=lambda uuids: {"UUID-ALICE": "1990-03-15"},
            reader=lambda path: {"Alice Smith": "UUID-ALICE"},
        )
        assert result.written == 1  # count reflects what would happen
        assert db.get_person_birthdays() == {}  # DB is untouched

    def test_import_contact_no_birthday(self, tmp_path: Path) -> None:
        db = self._db(tmp_path)
        result = run_import(
            db,
            "",
            dry_run=False,
            overwrite=False,
            fetcher=lambda uuids: {},  # contact linked but has no birthday
            reader=lambda path: {"Alice Smith": "UUID-ALICE"},
        )
        assert result.no_birthday == 1
        assert result.written == 0


@pytest.mark.skipif(sys.platform != "darwin", reason="Contacts framework requires macOS")
class TestAuthorizationDeniedRaises:
    def test_authorization_denied_raises(self) -> None:
        import Contacts

        with pytest.raises(PermissionError):
            _check_contacts_authorization(Contacts.CNAuthorizationStatusDenied)
