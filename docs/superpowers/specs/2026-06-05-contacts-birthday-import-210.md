# Spec: Import person birthdays from Apple Contacts (#210)

_Status: draft_

---

## Problem

#152 added a `person_birthdays` table and birthday-aware filtering. Populating it requires manual entry for each person. Apple Photos face recognition links recognised people to Contacts records, and many of those records already have a birthday field. Importing from that source bootstraps birthday data without requiring manual entry for anyone already in Contacts.

---

## Approach

A new `bp import-contacts-birthdays` command reads the Photos person→Contacts linkage from `Photos.sqlite`, fetches birthdays via the official macOS `Contacts` framework (PyObjC), and writes matches into `person_birthdays`. The command is explicit and opt-in: users who don't want Contacts integration don't run it. `--dry-run` shows what would be written before committing anything.

---

## Scope

**In:**
- New command `bp import-contacts-birthdays [--dry-run] [--overwrite]`
- New module `poller/contacts_importer.py`
- `pyobjc-framework-Contacts>=10; sys_platform == 'darwin'` added to `pyproject.toml` dependencies
- Uses existing `person_birthdays` table and `db.set_person_birthday()` from #152

**Out:**
- No new DB migration (existing table is the target)
- No daemon/scheduler integration (user runs explicitly)
- No UI changes (birthday display already works via #152)
- No support for platforms other than macOS (raises clearly if attempted)

---

## Data flow

```
Photos.sqlite ZPERSON
  → {fullname: bare_contact_uuid}           (direct SQLite read)
      ↓
CNContactStore (PyObjC, triggers TCC prompt on first run)
  → {contact_uuid: "YYYY-MM-DD" or "MM-DD"}
      ↓
person_birthdays table
  → upsert matches; skip existing unless --overwrite
```

Photos stores `ZPERSONURI` (format `UUID:ABPerson`) on `ZPERSON` rows for faces linked to a contact. Stripping `:ABPerson` gives a bare UUID matching `ZUNIQUEID` in the Contacts DB. On the current library: 96 of ~63,800 named persons have a Contacts link; 57 of those have a birthday in Contacts.

---

## Module: `poller/contacts_importer.py`

Three functions:

### `read_photos_person_contacts(photos_db_path: str) -> dict[str, str]`

Opens `Photos.sqlite` read-only, queries:

```sql
SELECT ZFULLNAME, ZPERSONURI
FROM ZPERSON
WHERE ZPERSONURI IS NOT NULL AND ZFULLNAME IS NOT NULL
```

Returns `{fullname: bare_uuid}` where `bare_uuid` is `ZPERSONURI.split(":")[0]`.

### `fetch_contact_birthdays(contact_uuids: set[str]) -> dict[str, str]`

macOS-only. Raises `RuntimeError("Contacts access requires macOS")` on non-darwin so the module stays importable on Linux CI.

Uses `CNContactStore` with a targeted fetch (only `birthday` and `identifier` keys):

```python
import sys
if sys.platform != "darwin":
    raise RuntimeError("Contacts access requires macOS")
import threading
import Contacts

store = Contacts.CNContactStore.alloc().init()

# Check / request TCC authorization
status = Contacts.CNContactStore.authorizationStatusForEntityType_(0)
if status == 0:  # notDetermined — trigger system dialog
    granted_box = [False]
    event = threading.Event()
    def _handler(granted, error):
        granted_box[0] = bool(granted)
        event.set()
    store.requestAccessForEntityType_completionHandler_(0, _handler)
    event.wait(timeout=30)
    if not granted_box[0]:
        raise PermissionError("Contacts access denied by user")
elif status in (1, 2):  # restricted or denied
    raise PermissionError(
        "Contacts access denied. Grant access in "
        "System Settings → Privacy & Security → Contacts."
    )

keys = [Contacts.CNContactBirthdayKey, Contacts.CNContactIdentifierKey]
request = Contacts.CNContactFetchRequest.alloc().initWithKeysToFetch_(keys)

# Enumerate contacts — PyObjC bridges the block as a Python callable
contacts: list = []
store.enumerateContactsWithFetchRequest_error_usingBlock_(
    request, None,
    lambda contact, stop: contacts.append(contact),
)
```

Birthday formatting: `CNContact.birthday` returns `NSDateComponents`. If `dateComponents.year > 9999` (the `NSDateComponentUndefined` sentinel on 64-bit), store as `MM-DD`; otherwise store as `YYYY-MM-DD`. This matches the format `person_birthdays` accepts.

Returns `{contact_uuid: birthday_str}`.

### `run_import(db, photos_db_path, *, dry_run, overwrite, fetcher=fetch_contact_birthdays) -> ImportResult`

Coordinator. `fetcher` defaults to `fetch_contact_birthdays` and is injectable for testing.

```python
@dataclass
class ImportResult:
    written: int
    skipped_same: int       # already set, same value
    skipped_conflict: int   # already set, different value, --overwrite not passed
    overwritten: int        # replaced because --overwrite
    no_birthday: int        # linked to contact but no birthday in Contacts
    unlinked: int           # person name not in Photos→Contacts mapping
```

Steps:
1. `person_contacts = read_photos_person_contacts(photos_db_path)`
2. `contact_birthdays = fetcher(set(person_contacts.values()))`
3. For each `(name, uuid)` in `person_contacts`:
   - If `uuid` not in `contact_birthdays` → `no_birthday += 1`; skip
   - Else get `new_bday = contact_birthdays[uuid]`
   - Get existing: `existing = db.get_person_birthdays().get(name)`
   - If no existing → write; `written += 1`
   - If existing == new_bday → `skipped_same += 1`
   - If existing != new_bday and not overwrite → `skipped_conflict += 1`
   - If existing != new_bday and overwrite → write; `overwritten += 1`
4. If not `dry_run`, call `db.set_person_birthday(name, new_bday)` for all writes

Persons in BP's DB that have no entry in `person_contacts` are `unlinked` — Photos didn't recognise a Contacts match for them, or they have no face entry at all.

---

## CLI command: `bp import-contacts-birthdays`

Added to `bp` following the existing subcommand pattern.

Flags:
- `--dry-run` — print report, write nothing
- `--overwrite` — replace existing `person_birthdays` entries with Contacts data

Output:

```
Requesting access to Contacts — you may see a system permission dialog.

Scanning Photos library for person → Contacts links...
  Found 96 persons linked to Contacts
  57 have a birthday in Contacts

  James Schleicher    1975-09-26  → write
  Brenda Devers       07-13       → write  (year not recorded in Contacts)
  Tony La             1992-10-31  → write
  Chris Devers        1976-02-04  → skip   (already set: 1976-02-04)
  David Palombo       1983-09-23  → skip   (already set: 1981-01-01)  use --overwrite to replace
  ...

  Written: 52   Skipped (same): 1   Skipped (conflict): 1   No birthday in Contacts: 39
(dry run — nothing written)
```

The "Requesting access" line prints before any CNContactStore call so the user isn't surprised by a system dialog. On subsequent runs (already authorised), the line is omitted.

`cmd_import_contacts_birthdays` in `bp` loads config, opens the DB, resolves `photos_library.path`, and calls `run_import`. Exits non-zero on `PermissionError`.

---

## Dependency

```toml
# pyproject.toml — in [project].dependencies
"pyobjc-framework-Contacts>=10; sys_platform == 'darwin'",
```

Placed alongside the existing `photoscript` darwin-conditional line.

---

## Tests: `tests/test_contacts_importer.py`

All tests except one run on Linux CI. The coordinator accepts an injectable `fetcher` callable; tests substitute a plain dict.

| Test | Platform |
|------|----------|
| `test_birthday_format_full_date` — `YYYY-MM-DD` when year ≤ 9999 | any |
| `test_birthday_format_yearless` — `MM-DD` when year > 9999 (NSDateComponentUndefined sentinel) | any |
| `test_import_writes_new_birthday` — person not in `person_birthdays` → written | any |
| `test_import_skips_existing_same_value` — same value already stored → not counted as conflict | any |
| `test_import_skips_existing_different_value_no_overwrite` — different value, no flag → skip, reported | any |
| `test_import_overwrites_existing_with_flag` — `--overwrite` → entry replaced | any |
| `test_import_dry_run_writes_nothing` — dry-run → DB unchanged, result still has counts | any |
| `test_import_no_contact_link` — person with no Contacts URI → unlinked count, not written | any |
| `test_import_contact_no_birthday` — linked contact has no birthday → `no_birthday` count | any |
| `test_authorization_denied_raises` — denied TCC status → `PermissionError`, not silent empty | macOS only (`@pytest.mark.skipif`) |

---

## Implementation checklist

- [ ] Add `pyobjc-framework-Contacts>=10; sys_platform == 'darwin'` to `pyproject.toml`
- [ ] Write `tests/test_contacts_importer.py` (10 tests); confirm they fail; implement module; confirm pass
- [ ] Create `poller/contacts_importer.py` with `read_photos_person_contacts`, `fetch_contact_birthdays`, `run_import`, `ImportResult`
- [ ] Add `cmd_import_contacts_birthdays` to `bp` and wire subparser + dispatch
- [ ] Add `import-contacts-birthdays` to docstring at top of `bp`
- [ ] `make lint` — mypy clean
- [ ] `python -m pytest tests/ -q` — all pass
- [ ] Commit referencing #210
