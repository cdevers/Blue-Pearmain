# Spec: Person birthdays — store, display age-at-time, birthday filtering (#152)

_Status: spec — not yet implemented_

---

## Problem

BP knows which named people appear in each photo (via `apple_persons` JSON), but knows nothing about *when* those people were born. Without birthdays:

- You can't see how old someone was in a photo.
- You can't find "all birthday photos of Chris" without manually knowing the date and searching.
- The temporal filter has no person-aware dimension.

---

## Approach

1. Add a `person_birthdays` table (new migration).
2. Add birthday editing to the Faces page (`/faces`).
3. Show age-at-time in the photo detail view for people with known birthdays.
4. Add `birthday:<person_name>` as a temporal pattern, consistent with the existing `holiday:`, `month:`, `season:` system — usable in both the map filter and the library filter.

---

## Scope

**In:**
- `person_birthdays` table: `person_name` (PK), `birthday` (`MM-DD` for recurring annual or `YYYY-MM-DD` for full known date)
- CRUD via `POST /api/person-birthday` and `DELETE /api/person-birthday/<name>`
- Birthday input field on the Faces page, one per named person
- Age-at-time in photo detail (`/photo/<id>`): "Chris Devers (age 8)"
- `birthday:<person_name>` temporal pattern in `time_patterns.py` / map dropdown / library filter
- Map dropdown: new "Birthdays" optgroup listing people with known birthdays
- `±2 days` expand applies to birthday patterns (same mechanism as holidays)

**Out (v1):**
- Birthday reminder / push notification
- "Within N days of birthday" fuzzy matching beyond the existing `±2 days` expand
- People who don't have any Apple Photos face data (no `apple_persons` entry)
- Multiple birthdays per person (edge case: not needed)

---

## DB — new table

### Migration file: `db/migrations/migrate_NNN_person_birthdays.py`

```python
DESCRIPTION = "Add person_birthdays table"

def up(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS person_birthdays (
            person_name  TEXT PRIMARY KEY,
            birthday     TEXT NOT NULL,   -- 'MM-DD' or 'YYYY-MM-DD'
            created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        )
    """)
```

### `db/schema.sql` addition

```sql
CREATE TABLE IF NOT EXISTS person_birthdays (
    person_name  TEXT PRIMARY KEY,
    birthday     TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
```

---

## DB layer — `db/db.py`

Add three methods to `CuratorDB`:

```python
def get_person_birthdays(self) -> dict[str, str]:
    """Return {person_name: birthday_str} for all rows."""
    rows = self.conn.execute(
        "SELECT person_name, birthday FROM person_birthdays"
    ).fetchall()
    return {r["person_name"]: r["birthday"] for r in rows}

def set_person_birthday(self, person_name: str, birthday: str) -> None:
    """Upsert a birthday for person_name. birthday is 'MM-DD' or 'YYYY-MM-DD'."""
    now = _utcnow()
    self.conn.execute(
        """INSERT INTO person_birthdays (person_name, birthday, created_at, updated_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(person_name) DO UPDATE SET birthday=excluded.birthday, updated_at=excluded.updated_at""",
        (person_name, birthday, now, now),
    )
    self.conn.commit()

def delete_person_birthday(self, person_name: str) -> None:
    """Remove the birthday for person_name. No-op if absent."""
    self.conn.execute(
        "DELETE FROM person_birthdays WHERE person_name = ?", (person_name,)
    )
    self.conn.commit()
```

---

## API — `reviewer/app.py`

### `POST /api/person-birthday`

```python
@app.route("/api/person-birthday", methods=["POST"])
def api_set_person_birthday() -> Response:
    data = request.get_json(force=True) or {}
    person_name = data.get("person_name", "").strip()
    birthday = data.get("birthday", "").strip()
    if not person_name or not birthday:
        return jsonify({"ok": False, "error": "person_name and birthday required"}), 400
    # Validate format: MM-DD or YYYY-MM-DD
    import re
    if not re.fullmatch(r"\d{2}-\d{2}|\d{4}-\d{2}-\d{2}", birthday):
        return jsonify({"ok": False, "error": "birthday must be MM-DD or YYYY-MM-DD"}), 400
    db().set_person_birthday(person_name, birthday)
    return jsonify({"ok": True})
```

### `DELETE /api/person-birthday/<person_name>`

```python
@app.route("/api/person-birthday/<person_name>", methods=["DELETE"])
def api_delete_person_birthday(person_name: str) -> Response:
    db().delete_person_birthday(person_name)
    return jsonify({"ok": True})
```

### `/faces` route — pass birthdays to template

In `faces_view()`, fetch birthdays and pass to `render_template`:

```python
birthdays = db().get_person_birthdays()
return render_template("faces.html", ..., birthdays=birthdays)
```

### `/photo/<id>` route — pass birthdays + computed ages

In `photo_detail()`, fetch birthdays and compute age-at-time for each named person:

```python
birthdays = db().get_person_birthdays()
# Compute age-at-time for people in this photo with known birthdays
person_ages: dict[str, int | None] = {}
date_taken_str = photo.get("date_taken", "")
if date_taken_str:
    photo_date = datetime.date.fromisoformat(date_taken_str[:10])
    for name in (photo.get("apple_persons") or []):
        bday = birthdays.get(name)
        if bday:
            month, day = (int(x) for x in bday[-5:].split("-"))
            birth_year = int(bday[:4]) if len(bday) == 10 else None
            if birth_year:
                age = photo_date.year - birth_year
                if (photo_date.month, photo_date.day) < (month, day):
                    age -= 1
                person_ages[name] = age
return render_template("photo.html", ..., person_ages=person_ages)
```

### `/map` route — pass birthday people to template

In `map_view()`, pass names+birthdays for the map dropdown:

```python
birthday_people = db().get_person_birthdays()  # {name: birthday}
return render_template("map.html", ..., birthday_people=birthday_people)
```

### `/api/map-photos` — handle `birthday:` pattern

In `api_map_photos()`, before calling `parse_pattern`, detect `birthday:` patterns and resolve them to dated ranges (mirroring the `holiday:` path):

```python
if time_pattern and time_pattern.startswith("birthday:"):
    person_name = time_pattern[9:]
    bday = db().get_person_birthdays().get(person_name)
    if bday:
        # rewrite as a pseudo-holiday pattern by computing dates for all years
        years = [r[0] for r in db().conn.execute(
            "SELECT DISTINCT CAST(strftime('%Y', date_taken) AS INTEGER) AS y "
            "FROM photos WHERE date_taken IS NOT NULL ORDER BY y"
        ).fetchall() if r[0] is not None]
        month, day = (int(x) for x in bday[-5:].split("-"))
        frag, frag_params = _birthday_clause(month, day, time_expand, years)
        if frag != "1=1":
            extra_where = f" AND {frag}"
            extra_params = frag_params
    # else: no birthday known → no-op filter (show all)
```

Add helper `_birthday_clause(month, day, expand_days, years)` in `app.py` (or add to `time_patterns.py`):

```python
def _birthday_clause(month: int, day: int, expand_days: int, years: list[int]) -> tuple[str, list]:
    import datetime
    if expand_days == 0:
        dates = []
        for y in years:
            try:
                dates.append(str(datetime.date(y, month, day)))
            except ValueError:
                pass  # Feb 29 in non-leap year
        if not dates:
            return "1=1", []
        ph = ",".join("?" * len(dates))
        return f"(strftime('%Y-%m-%d', p.date_taken) IN ({ph}))", dates
    else:
        clauses, params = [], []
        for y in years:
            try:
                d = datetime.date(y, month, day)
            except ValueError:
                continue
            lo = str(d - datetime.timedelta(days=expand_days))
            hi = str(d + datetime.timedelta(days=expand_days)) + "T23:59:59"
            clauses.append("(p.date_taken BETWEEN ? AND ?)")
            params.extend([lo, hi])
        if not clauses:
            return "1=1", []
        return f"({' OR '.join(clauses)})", params
```

The same `_birthday_clause` logic applies to the library `/api/photos` endpoint if a `birthday:` pattern is passed there.

---

## Template changes

### `faces.html`

For each named person card, add a birthday input row:

```html
<div class="birthday-row">
  <label>Birthday:
    <input type="text" class="birthday-input"
           data-person="{{ person.name }}"
           value="{{ birthdays.get(person.name, '') }}"
           placeholder="MM-DD or YYYY-MM-DD">
  </label>
  <button class="birthday-save-btn" data-person="{{ person.name }}">Save</button>
  <button class="birthday-clear-btn" data-person="{{ person.name }}"
          {% if not birthdays.get(person.name) %}style="display:none"{% endif %}>✕</button>
</div>
```

JS handlers: `fetch POST /api/person-birthday` on save, `fetch DELETE` on clear.

### `photo.html`

In the persons section, for each name in `apple_persons`, show age if known:

```html
{% for name in photo.apple_persons %}
  <span class="person-chip">
    {{ name }}{% if person_ages.get(name) is not none %} (age {{ person_ages[name] }}){% endif %}
  </span>
{% endfor %}
```

### `map.html`

Add a "Birthdays" optgroup to the time select dropdown, populated from `birthday_people`:

```html
{% if birthday_people %}
<optgroup label="Birthdays">
  {% for name in birthday_people|sort %}
  <option value="birthday:{{ name }}">{{ name }}'s birthday</option>
  {% endfor %}
</optgroup>
{% endif %}
```

The existing `±2 days` expand checkbox already applies to this optgroup via the `birthday:` handler using `time_expand`.

---

## Tests

### `tests/test_person_birthdays.py` (new file)

- `test_set_and_get_birthday` — set a birthday, retrieve it via `get_person_birthdays`
- `test_upsert_birthday` — set twice, confirm second value wins
- `test_delete_birthday` — set then delete; confirm absent from `get_person_birthdays`
- `test_delete_nonexistent_is_noop` — no error if person not present
- `test_birthday_clause_exact` — `_birthday_clause(5, 15, 0, [2020, 2021])` returns correct date list
- `test_birthday_clause_expand` — `_birthday_clause(5, 15, 2, [2020])` returns BETWEEN clause
- `test_birthday_clause_leap_day_skip` — Feb 29 in non-leap year skipped gracefully
- `test_api_set_birthday_valid` — POST `/api/person-birthday` with valid `MM-DD` returns `{"ok":true}`
- `test_api_set_birthday_full_date` — POST with `YYYY-MM-DD` accepted
- `test_api_set_birthday_bad_format` — bad format returns 400
- `test_api_delete_birthday` — DELETE `/api/person-birthday/<name>` removes it
- `test_migration_creates_table` — run migration, confirm `person_birthdays` table exists

---

## Implementation checklist

- [ ] Write migration `migrate_NNN_person_birthdays.py`
- [ ] Add `person_birthdays` to `schema.sql`
- [ ] Add `get_person_birthdays`, `set_person_birthday`, `delete_person_birthday` to `db.py`
- [ ] Write tests (12 above); confirm all fail; implement; confirm pass
- [ ] Add `POST /api/person-birthday` and `DELETE /api/person-birthday/<name>` routes
- [ ] Add `_birthday_clause` helper in `app.py`
- [ ] Wire `birthday:` branch in `api_map_photos`
- [ ] Wire `birthday:` branch in library filter (if applicable)
- [ ] Pass `birthdays` to `faces.html`; add birthday input UI + JS
- [ ] Pass `birthday_people` to `map.html`; add Birthdays optgroup
- [ ] Pass `person_ages` to `photo.html`; show age-at-time
- [ ] `make lint` — mypy clean
- [ ] `python -m pytest tests/ -q` — all pass
- [ ] Commit referencing #152
