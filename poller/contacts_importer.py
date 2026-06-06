"""Import person birthdays from Apple Contacts into the person_birthdays table (#210).

Provides:
  read_photos_person_contacts — reads Photos.sqlite for person→Contacts UUID map
  fetch_contact_birthdays     — reads the macOS Contacts framework (macOS only)
  run_import                  — coordinates the full import; injectable for testing
"""

from __future__ import annotations

import sqlite3
import sys
from dataclasses import dataclass
from typing import Any, Callable, Protocol


class _BirthdayDB(Protocol):
    def get_person_birthdays(self) -> dict[str, str]: ...
    def set_person_birthday(self, person_name: str, birthday: str) -> None: ...


def _format_birthday(year: int, month: int, day: int) -> str:
    """Return 'YYYY-MM-DD' or 'MM-DD' from raw NSDateComponents integer values.

    NSDateComponentUndefined on 64-bit is a very large integer. year > 9999
    means the year was not recorded in Contacts — store as MM-DD only.
    """
    if year > 9999:
        return f"{month:02d}-{day:02d}"
    return f"{year:04d}-{month:02d}-{day:02d}"


@dataclass
class ImportResult:
    written: int = 0
    skipped_same: int = 0  # already set, same value — not a conflict
    skipped_conflict: int = 0  # already set, different value, --overwrite not passed
    overwritten: int = 0  # replaced because --overwrite was passed
    no_birthday: int = 0  # linked to contact but no birthday recorded in Contacts


def read_photos_person_contacts(photos_db_path: str) -> dict[str, str]:
    """Return {fullname: bare_contact_uuid} for Photos persons linked to a Contact.

    Opens Photos.sqlite read-only. Rows where ZPERSONURI lacks a ':' separator
    are silently skipped (malformed URI).
    """
    conn = sqlite3.connect(f"file:{photos_db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT ZFULLNAME, ZPERSONURI FROM ZPERSON "
            "WHERE ZPERSONURI IS NOT NULL AND ZFULLNAME IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()
    result: dict[str, str] = {}
    for name, uri in rows:
        if ":" not in uri:
            continue
        result[str(name)] = uri.split(":")[0]
    return result


def _check_contacts_authorization(status: int) -> None:
    """Raise PermissionError if Contacts access is denied or restricted.

    Pass the value returned by CNContactStore.authorizationStatusForEntityType_.
    Must only be called on macOS (deferred Contacts import inside).
    """
    import Contacts  # noqa: PLC0415

    if status in (
        Contacts.CNAuthorizationStatusDenied,
        Contacts.CNAuthorizationStatusRestricted,
    ):
        raise PermissionError(
            "Contacts access denied. Grant access in "
            "System Settings → Privacy & Security → Contacts."
        )


def fetch_contact_birthdays(contact_uuids: set[str]) -> dict[str, str]:
    """Fetch birthdays for the given contact UUIDs from the macOS Contacts framework.

    Returns {contact_identifier: 'YYYY-MM-DD' or 'MM-DD'}.
    Raises RuntimeError on non-macOS. Raises PermissionError if access is denied.
    Raises TimeoutError if the TCC dialog is not answered within 30 seconds.
    """
    if sys.platform != "darwin":
        raise RuntimeError("Contacts access requires macOS")

    import threading

    import Contacts  # noqa: PLC0415

    store = Contacts.CNContactStore.alloc().init()
    status = Contacts.CNContactStore.authorizationStatusForEntityType_(
        Contacts.CNEntityTypeContacts
    )

    if status == Contacts.CNAuthorizationStatusNotDetermined:
        print(
            "Requesting access to Contacts — you may see a system permission dialog.",
            flush=True,
        )
        granted_box: list[bool] = [False]
        event = threading.Event()

        def _handler(granted: bool, error: object) -> None:
            granted_box[0] = bool(granted)
            event.set()

        store.requestAccessForEntityType_completionHandler_(Contacts.CNEntityTypeContacts, _handler)
        if not event.wait(timeout=30):
            raise TimeoutError(
                "Timed out waiting for Contacts permission response. "
                "Re-run the command and respond to the system dialog within 30 seconds."
            )
        if not granted_box[0]:
            raise PermissionError("Contacts access denied by user")
    else:
        _check_contacts_authorization(status)

    keys = [Contacts.CNContactBirthdayKey, Contacts.CNContactIdentifierKey]
    request = Contacts.CNContactFetchRequest.alloc().initWithKeysToFetch_(keys)

    contacts: list[Any] = []
    store.enumerateContactsWithFetchRequest_error_usingBlock_(
        request,
        None,
        lambda contact, stop: contacts.append(contact),
    )

    result: dict[str, str] = {}
    for contact in contacts:
        identifier = str(contact.identifier())
        if identifier not in contact_uuids:
            continue
        bday = contact.birthday()
        if bday is None:
            continue
        result[identifier] = _format_birthday(
            int(bday.year()),
            int(bday.month()),
            int(bday.day()),
        )

    return result


def run_import(
    db: _BirthdayDB,
    photos_db_path: str,
    *,
    dry_run: bool,
    overwrite: bool,
    fetcher: Callable[[set[str]], dict[str, str]] = fetch_contact_birthdays,
    reader: Callable[[str], dict[str, str]] = read_photos_person_contacts,
) -> ImportResult:
    """Coordinate the Contacts birthday import.

    fetcher and reader are injectable for testing. Defaults use the real
    Contacts framework and Photos.sqlite respectively.

    Dry-run contract: all counting steps run fully; only writes are skipped.
    The returned ImportResult is identical to a real run.
    """
    person_contacts = reader(photos_db_path)
    contact_birthdays = fetcher(set(person_contacts.values()))
    existing_birthdays = db.get_person_birthdays()

    result = ImportResult()
    writes: list[tuple[str, str]] = []

    for name, uuid in person_contacts.items():
        if uuid not in contact_birthdays:
            result.no_birthday += 1
            continue

        new_bday = contact_birthdays[uuid]
        existing = existing_birthdays.get(name)

        if existing is None:
            writes.append((name, new_bday))
            result.written += 1
        elif existing == new_bday:
            result.skipped_same += 1
        elif overwrite:
            writes.append((name, new_bday))
            result.overwritten += 1
        else:
            result.skipped_conflict += 1

    if not dry_run:
        for name, bday in writes:
            db.set_person_birthday(name, bday)

    return result
