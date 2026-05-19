# Re-upload Dedup Phase 4: UI Cross-Linking — Design (#106)

**GitHub issue:** #106
**Depends on:** #17 (Phase 1 — detection + DB grouping), #104 (Phase 2 — mark/delete discards)

---

## Scope

Phase 4 surfaces `reupload` and `reupload_uncertain` duplicate groups in the existing
`/duplicates` reviewer UI. Currently the route fetches these groups from the DB but silently
drops them — the sections loop only handles `snapbridge`, `device_upload`, and `uncertain`.

Changes are confined to two files:

| File | Change |
|------|--------|
| `reviewer/app.py` | Fix match-key parsing; parse notes JSON; add two section entries |
| `reviewer/templates/duplicates.html` | Add badge CSS; add evidence block; extend action buttons |

No DB schema changes. No new routes. No Flickr API calls.

---

## Architecture

The `/duplicates` route already fetches all unresolved groups via a JOIN. Data flows as follows:

1. **SQL query** — unchanged; already returns `reupload`/`reupload_uncertain` rows
2. **Match-key parsing** — branch on `group_type`: reupload types parse `reupload:{id1}:{id2}`;
   others keep the existing `partition("|")` path
3. **Notes parsing** — for reupload types, parse `notes` as JSON in Python; extract structured
   fields (`summary`, `upload_session_gap`, `filename_match`, `dimension_ratio`,
   `keeper_flickr_id`, `discard_flickr_id`); attach as `notes_parsed` dict to the group
4. **Sections loop** — two new entries added after `uncertain`
5. **Template** — badge CSS, evidence block, action buttons for both new types

---

## `app.py` Changes

### Match-key parsing

Current code crashes on reupload match keys (`reupload:{id1}:{id2}`):

```python
filename, _, date_key = key.partition("|")
```

Fix — branch on `group_type`:

```python
if group_type in ("reupload", "reupload_uncertain"):
    parts = key.split(":")
    display_key = f"{parts[1]} → {parts[2]}"
else:
    filename, _, date_key = key.partition("|")
    display_key = filename
```

### Notes parsing

For reupload types, parse the JSON notes blob and attach to the group dict:

```python
import json

if group_type in ("reupload", "reupload_uncertain"):
    try:
        notes_data = json.loads(group["notes"] or "{}")
    except (json.JSONDecodeError, TypeError):
        notes_data = {}
    group["notes_parsed"] = notes_data
```

`notes_data` keys: `summary`, `upload_session_gap`, `filename_match` (bool),
`dimension_ratio` (float), `keeper_flickr_id`, `discard_flickr_id`.

### Sections loop entries

Add after the existing `uncertain` entry:

```python
{
    "type": "reupload",
    "label": "Re-upload duplicate",
    "description": "Higher-res Flickr copy of a local photo.",
    "action": "confirm",
},
{
    "type": "reupload_uncertain",
    "label": "Possible re-upload",
    "description": "Probable re-upload — needs human review.",
    "action": "mark_reviewed",
},
```

Both actions write `resolved = 1` on the group — no `privacy_state` changes from the UI.

---

## `duplicates.html` Changes

### Badge CSS

Add alongside existing badge rules:

```css
.badge-reupload           { background: #6f42c1; color: #fff; }
.badge-reupload_uncertain { background: #fd7e14; color: #fff; }
```

### Structured evidence block

Rendered under the photo pair for reupload types only:

```html
{% if section.type in ('reupload', 'reupload_uncertain') and group.notes_parsed %}
<div class="dup-evidence">
  <span>Gap: {{ group.notes_parsed.upload_session_gap or '—' }}</span>
  <span>Filename match: {{ 'Yes' if group.notes_parsed.filename_match else 'No' }}</span>
  <span>Dimension ratio: {{ group.notes_parsed.dimension_ratio or '—' }}</span>
  {% if group.notes_parsed.summary %}
    <p class="dup-summary">{{ group.notes_parsed.summary }}</p>
  {% endif %}
</div>
{% endif %}
```

### Action buttons

Extend the existing gate and add a "Mark reviewed" variant:

```html
{% if section.type in ('snapbridge', 'device_upload', 'reupload') %}
  <button ...>Confirm resolution</button>
{% elif section.type == 'reupload_uncertain' %}
  <button ...>Mark reviewed</button>
{% endif %}
```

Both buttons POST to the existing resolution endpoint (`resolved = 1`).

---

## DB State Transitions (UI actions)

| Action | Group type | `resolved` | `privacy_state` |
|--------|-----------|------------|----------------|
| Confirm resolution | `reupload` | 1 | unchanged |
| Mark reviewed | `reupload_uncertain` | 1 | unchanged |

`privacy_state` changes for `reupload` discards happen via `bp dedup --flickr --mark-discards`
(Phase 2), not via the UI.

---

## Edge Cases

| Case | Handling |
|------|----------|
| `notes = NULL` | `json.loads("{}")` → empty dict; evidence block hidden |
| `notes` is not valid JSON | Caught by `except (json.JSONDecodeError, TypeError)`; empty dict |
| `reupload_uncertain` group shown in `reupload` section | Prevented by type check in sections loop |
| No reupload groups in DB | Sections simply render with zero groups (same as other types) |
| `flickr_deleted = 1` on discard | Still shown in UI — resolution state independent of deletion |

---

## Tests

All tests use an in-memory SQLite DB + Flask test client. No Flickr API mocking needed.

| Test | Setup | Assert |
|------|-------|--------|
| Route renders `reupload` groups | `reupload` group + keeper/discard pair | Response contains both Flickr IDs and "Re-upload duplicate" |
| Route renders `reupload_uncertain` groups | `reupload_uncertain` group | "Possible re-upload" and "Mark reviewed" in response |
| Notes JSON rendered | Group with full notes JSON blob | "14 days", "Yes", "0.85" in response |
| Match-key parsing | Group with key `reupload:48900000:48910000` | No 500; both IDs in response |
| Malformed notes is safe | `notes = NULL` or `notes = "not json"` | No 500; page renders |

Tests live in `tests/test_review_ui.py` alongside existing route tests.

---

## Files Touched

| File | Change |
|------|--------|
| `reviewer/app.py` | Match-key branch; notes JSON parsing; two new section entries |
| `reviewer/templates/duplicates.html` | Badge CSS; evidence block; extended action button gate |
| `tests/test_review_ui.py` | 5 new tests covering both group types, notes rendering, edge cases |
