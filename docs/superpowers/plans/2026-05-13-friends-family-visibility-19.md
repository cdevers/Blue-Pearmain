# Plan: GH #19 — Friends / Family / Friends & Family visibility

## Context

Flickr supports five visibility levels via three boolean flags (`is_public`, `is_friend`, `is_family`). Blue Pearmain currently models only two outcomes — public (`approved_public`) and private (`keep_private`). The three middle levels (Friends, Family, Friends & Family) are useful for people-photos that shouldn't be fully public. `flickr_client.set_permissions()` already accepts `is_friend`/`is_family` but always passes 0. The DB schema, review UI, push logic, and reconcile all need extending.

`already_friends/family` analogues (for photos already at those levels before import) are deferred — detecting them would require parsing `isfriend`/`isfamily` from the Flickr API feed, which is a separate scope.

---

## New vocabulary

| Decision (API) | Privacy state (DB) | Flickr flags |
|---|---|---|
| `make_friends` | `approved_friends` | `is_friend=1, is_family=0, is_public=0` |
| `make_family` | `approved_family` | `is_friend=0, is_family=1, is_public=0` |
| `make_friends_family` | `approved_friends_family` | `is_friend=1, is_family=1, is_public=0` |

---

## Task 1 — DB migration (migrate_015)

**New file:** `db/migrations/migrate_015_friends_family.py`

SQLite cannot ALTER a CHECK constraint in place. Use the standard rename/recreate/copy dance:

```sql
-- 1. Rename existing table
ALTER TABLE photos RENAME TO photos_old;

-- 2. Create new table with widened CHECK (add 3 new privacy_state values)
CREATE TABLE photos ( ...same DDL...
    privacy_state TEXT NOT NULL DEFAULT 'needs_review'
        CHECK(privacy_state IN (
            'auto_private', 'needs_review', 'candidate_public',
            'approved_public', 'keep_private', 'already_public',
            'skipped', 'duplicate_flickr',
            'approved_friends', 'approved_family', 'approved_friends_family'
        )),
    ...
);

-- 3. INSERT INTO photos SELECT * FROM photos_old;
-- 4. DROP TABLE photos_old;
-- 5. Recreate all indexes (copy from schema.sql)
```

`review_decision` has no CHECK constraint in the schema (it's just a comment), so no migration needed for that column.

Also update **`db/schema.sql`**: add the three new values to the `privacy_state` CHECK and update the comment block.

---

## Task 2 — DB layer (`db/db.py`)

**`record_review()` state_map** (line ~464): extend with 3 new entries:
```python
"make_friends":        "approved_friends",
"make_family":         "approved_family",
"make_friends_family": "approved_friends_family",
```

No other DB method changes needed. `set_privacy_state()` is generic and already works. Stats queries can be updated later (new counts are a cosmetic addition, not a correctness issue).

---

## Task 3 — Shared perms helper (`flickr/flickr_client.py`)

Add a module-level dict and helper function near `set_permissions()`:

```python
# Maps privacy_state → (is_public, is_friend, is_family)
_STATE_PERMS: dict[str, tuple[int, int, int]] = {
    "approved_public":         (1, 0, 0),
    "already_public":          (1, 0, 0),
    "approved_friends":        (0, 1, 0),
    "approved_family":         (0, 0, 1),
    "approved_friends_family": (0, 1, 1),
}

def state_to_perms(privacy_state: str) -> tuple[int, int, int]:
    """Return (is_public, is_friend, is_family) for a given privacy_state."""
    return _STATE_PERMS.get(privacy_state, (0, 0, 0))
```

Both `app.py` and `reconcile.py` already import from `flickr.flickr_client`; they'll import this helper.

---

## Task 4 — Push logic (`reviewer/app.py`)

**`api_decide` validation** (line ~580): add the three new decisions to the accepted list:
```python
if decision not in ("make_public", "confirm_public", "keep_private", "skip",
                    "make_friends", "make_family", "make_friends_family"):
```

**`api_decide` background push** (line ~617): replace the `if _decision == "make_public"` branch with a generic approach using `state_to_perms`:
```python
from flickr.flickr_client import state_to_perms
# record_review() has already updated the DB before the thread is spawned
target_state = db().conn.execute(
    "SELECT privacy_state FROM photos WHERE id = ?", (_photo_id,)
).fetchone()["privacy_state"]
is_pub, is_frn, is_fam = state_to_perms(target_state)
if is_pub or is_frn or is_fam:   # any non-private visibility → push perms
    c.set_permissions(_flickr_id, is_public=is_pub, is_friend=is_frn, is_family=is_fam)
    perms_ok = True
```

**`api_push_approved`** (line ~913): widen the query to cover all approved states, and use `state_to_perms`:
```python
WHERE privacy_state IN (
    'approved_public','approved_friends','approved_family','approved_friends_family'
)
AND flickr_id IS NOT NULL AND perms_pushed_flickr = 0
```
In the loop, replace `c.set_permissions(flickr_id, is_public=1)` with:
```python
from flickr.flickr_client import state_to_perms
is_pub, is_frn, is_fam = state_to_perms(row["privacy_state"])
c.set_permissions(flickr_id, is_public=is_pub, is_friend=is_frn, is_family=is_fam)
```

---

## Task 5 — Reconcile (`poller/reconcile.py`)

The perm check (line ~92) currently compares only `ispublic`. Extend to all three flags:

```python
from flickr.flickr_client import state_to_perms

if db_perms_pushed:
    visibility = photo.get("visibility", {})
    actual = (
        int(visibility.get("ispublic", 0)),
        int(visibility.get("isfriend", 0)),
        int(visibility.get("isfamily", 0)),
    )
    expected = state_to_perms(db_state)  # (0,0,0) for keep_private etc.

    result["perm_expected"] = expected
    result["perm_actual"]   = actual

    if actual != expected:
        result["status"] = "perm_mismatch"
        if fix:
            client.set_permissions(flickr_id,
                is_public=expected[0], is_friend=expected[1], is_family=expected[2])
            result["fixes"].append("perm")
```

Note: `result["perm_expected"]` / `result["perm_actual"]` change from strings (`"public"/"private"`) to tuples. Audit any test or display code that depends on the string form.

---

## Task 6 — Scanner/poller protection (`poller/scanner.py`)

Two guard sites need the new states added to the "already reviewed, don't overwrite" lists:

**Line ~416** (screenshot guard):
```python
if is_screenshot and existing.get("privacy_state") not in (
    "approved_public", "keep_private", "already_public",
    "approved_friends", "approved_family", "approved_friends_family"
):
```

**Line ~426** (general state guard):
```python
if existing.get("privacy_state") not in (
    "approved_public", "keep_private", "already_public", "skipped",
    "approved_friends", "approved_family", "approved_friends_family"
):
```

No changes needed in `poller.py` — the `already_public` branch only fires on `ispublic == 1`; friends/family photos fall through to `classify_flickr_record()` and get their existing DB state preserved.

---

## Task 7 — Review UI templates

### `reviewer/templates/review.html`

**State filter dropdown** (after "Kept private" option, ~line 183): add three new options:
```html
<option value="approved_friends" ...>Friends only ({{ stats.by_state.get('approved_friends',0) }})</option>
<option value="approved_family" ...>Family only ({{ stats.by_state.get('approved_family',0) }})</option>
<option value="approved_friends_family" ...>Friends & Family ({{ stats.by_state.get('approved_friends_family',0) }})</option>
```

**`STATE_CLASS` JS map** (~line 332): add:
```js
make_friends:        'approved_friends',
make_family:         'approved_family',
make_friends_family: 'approved_friends_family',
```

**Decision buttons** (~line 280): keep the main row (Public / Private / Skip) unchanged. Add a `▸ More` toggle that reveals a hidden second row — **user-preferred approach**:
```html
<!-- existing main row unchanged -->
<button class="btn-more"
        onclick="event.stopPropagation(); this.closest('.photo-card').classList.toggle('restricted-open')">
  ▸ More
</button>
<div class="actions-restricted" style="display:none">
  <button class="btn-frn" onclick="...quickDecide(..., 'make_friends', ...)">👥 Friends</button>
  <button class="btn-fam" onclick="...quickDecide(..., 'make_family', ...)">👨‍👩‍👧 Family</button>
  <button class="btn-faf" onclick="...quickDecide(..., 'make_friends_family', ...)">F+F</button>
</div>
```
CSS: `.photo-card.restricted-open .actions-restricted { display: flex; }` and `.photo-card.restricted-open .btn-more { opacity: 0.5; }`. Row collapses after a decision via the existing `card.classList` update path.

### `reviewer/templates/photo.html`

Add three buttons in the decision panel (~line 242), after "Keep private":
```html
<button class="btn btn-friends" onclick="doDecide('make_friends', true)">
  <span class="key-hint">F</span> Friends only + push
</button>
<button class="btn btn-family" onclick="doDecide('make_family', true)">
  Family only + push
</button>
<button class="btn btn-friends-family" onclick="doDecide('make_friends_family', true)">
  Friends & Family + push
</button>
```

Update `doDecide`'s state classification and keyboard shortcuts as needed.

### `reviewer/templates/base.html`

Extend the toast message block (~line 230):
```js
decision === 'make_friends'        ? '👥 Friends only' :
decision === 'make_family'         ? '👨‍👩‍👧 Family only' :
decision === 'make_friends_family' ? '👥 Friends & Family' :
```

---

## Task 8 — Tests

**`tests/test_core.py`:**
- Migration 015 idempotency test (mirrors existing migration tests)
- `record_review()`: assert each new decision maps to the correct privacy_state
- `state_to_perms()`: assert correct flag tuples for all five states + unknown state → `(0,0,0)`
- `api_push_approved` path: mock `set_permissions`, seed an `approved_friends` photo, verify correct flags passed
- Reconcile: friend/family mismatch detected; fix path calls `set_permissions` with correct flags

**`tests/test_review_ui.py`:**
- State filter dropdown contains "approved_friends" option
- New decisions accepted by `api_decide` (not rejected with 400)

---

## Files changed

| File | Change |
|---|---|
| `db/migrations/migrate_015_friends_family.py` | New — widen privacy_state CHECK |
| `db/schema.sql` | Add 3 new privacy_state values to CHECK |
| `db/db.py` | Extend `record_review()` state_map |
| `flickr/flickr_client.py` | Add `state_to_perms()` helper + `_STATE_PERMS` dict |
| `reviewer/app.py` | Extend `api_decide` + `api_push_approved` |
| `poller/reconcile.py` | Extend perm check to friends/family flags |
| `poller/scanner.py` | Add 3 new states to protected-state guards |
| `reviewer/templates/review.html` | New ▸ More toggle + buttons + filter options + JS map |
| `reviewer/templates/photo.html` | New decision buttons |
| `reviewer/templates/base.html` | Toast messages for new decisions |
| `tests/test_core.py` | Migration, record_review, state_to_perms, push, reconcile tests |
| `tests/test_review_ui.py` | Filter and api_decide acceptance tests |

---

## Verification

1. `python -m pytest tests/ -q` — all existing + new tests pass
2. Run dev server, open review grid — `▸ More` toggle appears on each card; clicking reveals Friends/Family/F&F buttons
3. Click "Friends only" on a candidate photo → toast shows "👥 Friends only", state updates
4. With Flickr credentials, push a friends-only approved photo; confirm `ispublic=0, isfriend=1, isfamily=0` via Flickr API
5. `bp reconcile` on a photo where Flickr visibility has drifted — mismatch detected and fixed
6. Scanner rescan of an `approved_friends` photo — state is preserved, not overwritten
