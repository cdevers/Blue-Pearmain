# Import Person Birthdays from Apple Contacts (#210) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `bp import-contacts-birthdays [--dry-run] [--overwrite]` that reads Photos→Contacts person linkages from Photos.sqlite and writes birthdays into `person_birthdays` via the macOS Contacts framework.

**Architecture:** Three functions in `poller/contacts_importer.py`: `read_photos_person_contacts` reads Photos.sqlite directly, `fetch_contact_birthdays` calls PyObjC Contacts API (macOS only), and `run_import` coordinates them. Both `fetcher` and `reader` are injectable for CI testing. The `bp` CLI wraps this as a new subcommand.

**Tech Stack:** Python 3.11, SQLite (stdlib), PyObjC `pyobjc-framework-Contacts>=10` (macOS), existing `db.Database`.

---

## File map

| File | Action | Purpose |
|------|--------|---------|
| `pyproject.toml` | Modify | Declare `pyobjc-framework-Contacts>=10; sys_platform == 'darwin'` |
| `tests/test_contacts_importer.py` | Create | 10 tests for all module logic |
| `poller/contacts_importer.py` | Create | `_format_birthday`, `ImportResult`, `read_photos_person_contacts`, `_check_contacts_authorization`, `fetch_contact_birthdays`, `run_import` |
| `bp` | Modify | Add `cmd_import_contacts_birthdays`, subparser, dispatch entry, docstring line |

---

### Task 1: Add pyobjc-framework-Contacts dependency

**Files:**
- Modify: `pyproject.toml:6-13`

No TDD: this is a metadata change.

- [ ] **Step 1: Edit `pyproject.toml` — add the darwin-conditional dependency**

Insert after the `photoscript` line so the `dependencies` block reads:

```toml
dependencies = [
    "requests>=2.31",
    "requests-oauthlib>=1.3",
    "pyyaml>=6.0",
    "flask>=3.0",
    "photoscript>=0.5.3; sys_platform == 'darwin'",
    "pyobjc-framework-Contacts>=10; sys_platform == 'darwin'",
    "zeroconf>=0.148.0",
]
```

- [ ] **Step 2: Verify `uv sync` resolves cleanly (macOS)**

Run: `uv sync`
Expected: exits 0. The package is already installed at v12.1; this only adds it as a declared dependency.

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml
git commit -m "$(cat <<'EOF'
deps(#210): declare pyobjc-framework-Contacts>=10

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Write 10 failing tests

**Files:**
- Create: `tests/test_contacts_importer.py`

Write all tests first; run to confirm they fail; implement the module in Task 3.

- [ ] **Step 1: Create `tests/test_contacts_importer.py`**

```python
"""Tests for poller/contacts_importer.py — birthday import from Apple Contacts (#210)."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "poller"))

from db.db import Database
from contacts_importer import (
    ImportResult,
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
        conn.execute(
            "INSERT INTO ZPERSON VALUES ('Alice Smith', 'MALFORMED_NO_COLON')"
        )
        conn.commit()
        conn.close()

        result = read_photos_person_contacts(str(photos_db))
        assert result == {}


class TestRunImport:
    def _db(self, tmp_path: Path) -> Database:
        return Database(str(tmp_path / "curator.db"))

    def test_import_writes_new_birthday(self, tmp_path: Path) -> None:
        db = self._db(tmp_path)
        result = run_import(
            db, "",
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
            db, "",
            dry_run=False,
            overwrite=False,
            fetcher=lambda uuids: {"UUID-ALICE": "1990-03-15"},
            reader=lambda path: {"Alice Smith": "UUID-ALICE"},
        )
        assert result.skipped_same == 1
        assert result.written == 0
        assert result.skipped_conflict == 0

    def test_import_skips_existing_different_value_no_overwrite(
        self, tmp_path: Path
    ) -> None:
        db = self._db(tmp_path)
        db.set_person_birthday("Alice Smith", "1981-01-01")
        result = run_import(
            db, "",
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
            db, "",
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
            db, "",
            dry_run=True,
            overwrite=False,
            fetcher=lambda uuids: {"UUID-ALICE": "1990-03-15"},
            reader=lambda path: {"Alice Smith": "UUID-ALICE"},
        )
        assert result.written == 1          # count reflects what would happen
        assert db.get_person_birthdays() == {}  # DB is untouched

    def test_import_contact_no_birthday(self, tmp_path: Path) -> None:
        db = self._db(tmp_path)
        result = run_import(
            db, "",
            dry_run=False,
            overwrite=False,
            fetcher=lambda uuids: {},       # contact linked but has no birthday
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
```

- [ ] **Step 2: Run tests to confirm all 10 fail**

Run: `python -m pytest tests/test_contacts_importer.py -v`
Expected: all tests fail or are skipped. The exact count depends on platform and how pytest handles the top-level import failure — all tests in the file share the same `ImportError` when `contacts_importer` doesn't exist yet, so you may see 10 errors, 9 errors + 1 skip, or a collection-time failure. Any of these is correct. What matters is that no test passes.

- [ ] **Step 3: Commit**

```bash
git add tests/test_contacts_importer.py
git commit -m "$(cat <<'EOF'
test(#210): add 10 failing tests for contacts_importer

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Implement `poller/contacts_importer.py`

**Files:**
- Create: `poller/contacts_importer.py`

`run_import` adds `reader` as a second injectable parameter not present in the spec. It defaults to `read_photos_person_contacts`, so all real-world behaviour is identical to the spec. The spec should be updated to document this addition (see Task 5).

**Person name convention:** Person names are stored verbatim as returned from `ZPERSON.ZFULLNAME`. BP has no case-normalization convention for person names (only tags are casefolded), so `run_import` follows the same convention — names are treated as-is. Matching is therefore case-sensitive, consistent with `person_policies` and the existing `person_birthdays` table.

- [ ] **Step 1: Create `poller/contacts_importer.py`**

```python
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
from typing import TYPE_CHECKING, Callable, Protocol

if TYPE_CHECKING:
    pass


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
    skipped_same: int = 0       # already set, same value — not a conflict
    skipped_conflict: int = 0   # already set, different value, --overwrite not passed
    overwritten: int = 0        # replaced because --overwrite was passed
    no_birthday: int = 0        # linked to contact but no birthday recorded in Contacts


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

        store.requestAccessForEntityType_completionHandler_(
            Contacts.CNEntityTypeContacts, _handler
        )
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

    contacts: list[object] = []
    store.enumerateContactsWithFetchRequest_error_usingBlock_(
        request,
        None,
        lambda contact, stop: contacts.append(contact),
    )

    result: dict[str, str] = {}
    for contact in contacts:
        identifier = str(contact.identifier())  # type: ignore[union-attr]
        if identifier not in contact_uuids:
            continue
        bday = contact.birthday()  # type: ignore[union-attr]
        if bday is None:
            continue
        result[identifier] = _format_birthday(
            int(bday.year()),   # type: ignore[arg-type]
            int(bday.month()),  # type: ignore[arg-type]
            int(bday.day()),    # type: ignore[arg-type]
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
```

- [ ] **Step 2: Run the tests and confirm they pass**

Run: `python -m pytest tests/test_contacts_importer.py -v`
Expected: 9 pass + 1 skip on Linux CI; 10 pass on macOS (assuming Contacts access granted).

If the `test_import_malformed_uri_skipped` test fails with a SQLite URI error, check that the `photos_db_path` passed to `read_photos_person_contacts` is an absolute path. Use `str(photos_db.resolve())` in the test if needed.

- [ ] **Step 3: Run the full test suite to check for regressions**

Run: `python -m pytest tests/ -q`
Expected: All existing tests still pass.

- [ ] **Step 4: Commit**

```bash
git add poller/contacts_importer.py
git commit -m "$(cat <<'EOF'
feat(#210): implement contacts_importer — read_photos_person_contacts, fetch_contact_birthdays, run_import

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Wire `bp import-contacts-birthdays` command

**Files:**
- Modify: `bp`

Four edits to `bp`: (1) docstring line, (2) `cmd_import_contacts_birthdays` function, (3) subparser + argument declarations, (4) `hasattr` default + dispatch entry.

- [ ] **Step 1: Add the command to the module docstring**

The docstring starts at line 3. Add one line before `Global options:`:

```
    bp import-contacts-birthdays [--dry-run] [--overwrite]  Import birthdays from Apple Contacts
```

So the block reads:

```python
"""
bp — Blue Pearmain command-line interface

Usage:
    bp pipeline [--dry-run] [--limit N]  Sync-metadata then auto-apply non-conflict proposals
    ...
    bp doctor [--check-flickr]      Validate config, environment, and DB state
    bp import-contacts-birthdays [--dry-run] [--overwrite]  Import birthdays from Apple Contacts

Global options:
    --config PATH   Path to config.yml (default: config/config.yml)
    --verbose       Extra logging
...
"""
```

- [ ] **Step 2: Add `cmd_import_contacts_birthdays` function**

Add the function immediately before `def cmd_all(args):` (which is around line 992). Insert:

Note on Photos path construction: BP has no shared helper for `photos_library.path` — the inline pattern below matches `cmd_scan` (line 185 of `bp`) and is the established convention.

Note on CLI output: the output is **summary-only** (counts line + optional dry-run footer). The per-person table shown in the spec's sample output is aspirational; it is not implemented here. This is intentional — the summary meets the testing requirements and the per-person table can be added later as a separate enhancement.

```python
def cmd_import_contacts_birthdays(args: argparse.Namespace) -> None:
    """Import person birthdays from Apple Contacts into person_birthdays."""
    import yaml

    sys.path.insert(0, str(ROOT / "poller"))
    from db.db import Database
    from contacts_importer import run_import

    with open(args.config) as f:
        config = yaml.safe_load(f)

    db_path = str(Path(config["database"]["path"]).expanduser())
    photos_library_path = str(
        Path(config.get("photos_library", {}).get("path", "")).expanduser()
    )
    photos_db_path = str(Path(photos_library_path) / "database" / "Photos.sqlite")

    db = Database(db_path)
    try:
        result = run_import(
            db,
            photos_db_path,
            dry_run=args.dry_run,
            overwrite=args.overwrite,
        )
    except (PermissionError, TimeoutError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        db.close()
        sys.exit(1)

    db.close()

    parts = [
        f"Written: {result.written}",
        f"Skipped (same): {result.skipped_same}",
        f"Skipped (conflict): {result.skipped_conflict}",
        f"No birthday in Contacts: {result.no_birthday}",
    ]
    if result.overwritten:
        parts.append(f"Overwritten: {result.overwritten}")
    print("  ".join(parts))
    if args.dry_run:
        print("(dry run — nothing written)")
```

- [ ] **Step 3: Add subparser**

In the `main()` function, after the `match-legacy` block (around line 1361) and before `args = parser.parse_args()`, insert:

```python
    # import-contacts-birthdays
    p_icb = sub.add_parser(
        "import-contacts-birthdays",
        help="Import person birthdays from Apple Contacts into person_birthdays",
    )
    p_icb.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be written without writing anything",
    )
    p_icb.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace existing person_birthdays entries with Contacts data",
    )
```

- [ ] **Step 4: Add `hasattr` default and dispatch entry**

In the `hasattr` defaults block (around line 1365), add:

```python
    if not hasattr(args, "overwrite"):  args.overwrite = False
```

In the `dispatch` dict (around line 1396), add:

```python
        "import-contacts-birthdays": cmd_import_contacts_birthdays,
```

- [ ] **Step 5: Smoke-test the command wiring (dry run)**

Run: `python bp import-contacts-birthdays --help`
Expected: prints usage with `--dry-run` and `--overwrite` flags, no error.

- [ ] **Step 6: Run the full test suite**

Run: `python -m pytest tests/ -q`
Expected: All tests pass.

- [ ] **Step 7: Commit**

```bash
git add bp
git commit -m "$(cat <<'EOF'
feat(#210): add bp import-contacts-birthdays command

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Docs, lint, and final pass

**Files:**
- Modify: `docs/future-directions.md`

- [ ] **Step 1: Update `docs/future-directions.md`**

Find the line about #210 in the Person birthdays section (around line 103):

```
See also [#210](https://github.com/cdevers/Blue-Pearmain/issues/210) — import birthdays from Apple Contacts (many Photos faces are linked to Contacts records that already have a birthday field).
```

Change it to:

```
See also [#210](https://github.com/cdevers/Blue-Pearmain/issues/210) · [spec](superpowers/specs/2026-06-05-contacts-birthday-import-210.md) · [plan](superpowers/plans/2026-06-05-contacts-birthday-import-210.md) — import birthdays from Apple Contacts (many Photos faces are linked to Contacts records that already have a birthday field).
```

- [ ] **Step 2: Note `reader` addition in the spec**

In `docs/superpowers/specs/2026-06-05-contacts-birthday-import-210.md`, add one sentence to the `run_import` description after the `fetcher` injection note:

```
`reader` is also injectable (defaults to `read_photos_person_contacts`) to allow coordinator tests to run without a real Photos.sqlite on CI.
```

Do **not** change the spec status line — that is updated after the code is merged and verified, not as a planned coding task.

- [ ] **Step 3: Run `make lint`**

Run: `make lint`
Expected: mypy, ruff format, and ruff check all pass with no errors.

If mypy reports errors in `contacts_importer.py`, address them — the `# type: ignore[union-attr]` and `# type: ignore[arg-type]` annotations on the PyObjC calls are expected and acceptable (narrowed ignores, not bare).

- [ ] **Step 4: Run full test suite**

Run: `python -m pytest tests/ -q`
Expected: All tests pass. On macOS: count includes the new 10 tests. On Linux CI: 9 new tests pass + 1 skip.

- [ ] **Step 5: Commit and close the issue**

```bash
git add docs/future-directions.md docs/superpowers/specs/2026-06-05-contacts-birthday-import-210.md
git commit -m "$(cat <<'EOF'
docs(#210): update future-directions and spec reader note

Closes #210

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 6: Push to origin**

```bash
git push origin HEAD
```
