# Merge Flickr Donor — Duplicates UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a "Merge into Photos record" action to the duplicates UI so that when a duplicate group contains a Flickr-only record (has `flickr_id`, no `uuid`) and at least one Photos-linked record (has `uuid`), the user can copy the Flickr identity onto the Photos record and soft-delete the now-redundant Flickr-only row.

**GitHub issue:** #73

---

## Background

The deduplicator detects groups where the same image was inserted twice: once as a Flickr-only record (uploaded from a phone before the card import) and once as a Photos-linked record (the full-resolution card import). The existing UI supports keeper/discard annotation and "not a duplicate" dismissal, but neither option handles the case where you want to keep the Photos record AND preserve its Flickr linkage from the donor.

The merge operation:
1. Copies all Flickr columns from the donor row onto the target row.
2. Soft-deletes the donor: clears its `flickr_id`, sets `merged_into_id = target.id`, sets `privacy_state = 'duplicate_flickr'`, sets `duplicate_role = 'discard'`.
3. Resolves the duplicate group (`resolved = 1`, `keeper_id = target.id`).

---

## Constraints

- `flickr_id` has a UNIQUE index — the donor's `flickr_id` must be NULLed before or simultaneously with writing it to the target.
- The `privacy_state` CHECK constraint allows `'duplicate_flickr'` already; no schema change needed for that column.
- `merged_into_id` is a new column (migration 014).
- The merge button is only shown on Flickr-only records (have `flickr_id`, no `uuid`) when the group also contains at least one Photos-linked record (has `uuid`).
- Default merge target is the highest-resolution Photos-linked record in the group (largest `width × height`). A dropdown allows switching to another Photos-linked record if the auto-selection is wrong.
- All DB changes in one transaction.

---

## Flickr columns copied donor → target

`flickr_id`, `flickr_secret`, `flickr_server`, `flickr_farm`,
`date_uploaded_flickr`, `tags_pushed_flickr`, `perms_pushed_flickr`,
`flickr_deleted`, `flickr_title`, `flickr_description`, `flickr_tags`,
`flickr_tags_hash`, `flickr_last_updated`, `meta_synced_flickr_at`,
`tags_truncated_for_flickr`, `display_rotation`

---

## Files touched

| File | Change |
|------|--------|
| `db/migrations/migrate_014_merged_into_id.py` | New — adds `merged_into_id` column |
| `db/schema.sql` | Add `merged_into_id` to `photos` table (for fresh DBs) |
| `db/db.py` | New `merge_flickr_donor()` function |
| `reviewer/app.py` | New `"merge"` branch in `/api/duplicates/<id>/assign`; extend `/duplicates` route to pass `flickr_only_ids` + `photos_targets` per group |
| `reviewer/templates/duplicates.html` | Merge button + inline confirm strip per Flickr-only card |
| `tests/test_core.py` | `TestMergeFlickrDonor` — DB-level merge logic |
| `tests/test_review_ui.py` | `TestMergeUI` — route + API behaviour |
| `README.md` | Update test count; note merge action in duplicates UI |

---

## Design details

### Migration 014

```python
def migrate(conn):
    conn.execute("""
        ALTER TABLE photos ADD COLUMN merged_into_id INTEGER REFERENCES photos(id)
    """)
```

Idempotent check: catch `OperationalError: duplicate column name`.

### `db/db.py` — `merge_flickr_donor_in_group(self, donor_id, target_id, group_id)`

New instance method on the `Database` class, following the same pattern as the existing `merge_flickr_into_photos` method (line 111).

Flickr identity fields always copied from donor:
```python
FLICKR_COPY_FIELDS = [
    "flickr_id", "flickr_secret", "flickr_server", "flickr_farm",
    "date_uploaded_flickr", "tags_pushed_flickr", "perms_pushed_flickr",
    "flickr_deleted", "flickr_title", "flickr_description", "flickr_tags",
    "flickr_tags_hash", "flickr_last_updated", "meta_synced_flickr_at",
    "tags_truncated_for_flickr", "display_rotation",
]
```

Steps (all within one transaction, committed by the caller):
1. Fetch donor row (`SELECT * FROM photos WHERE id = donor_id`) — raise `ValueError` if not found, if `uuid IS NOT NULL`, or if `flickr_id IS NULL`.
2. Fetch target row (`SELECT * FROM photos WHERE id = target_id`) — raise `ValueError` if not found or if `uuid IS NULL`.
3. Migrate `photo_albums`: copy album memberships from donor → target using `INSERT OR IGNORE` (same logic as `merge_flickr_into_photos`).
4. Migrate `tag_events`: DELETE from donor, re-INSERT on target (same logic as `merge_flickr_into_photos`; workaround for SQLite FK/ALTER TABLE bug).
5. Migrate `metadata_conflicts`: copy from donor → target using `INSERT OR IGNORE`.
6. Clear `flickr_id` on donor **before** writing it to target (UNIQUE constraint requires this): `UPDATE photos SET flickr_id = NULL WHERE id = donor_id`.
7. `UPDATE photos SET <flickr cols> WHERE id = target_id` for all non-null fields in `FLICKR_COPY_FIELDS`.
8. Soft-delete donor: `UPDATE photos SET merged_into_id = target_id, privacy_state = 'duplicate_flickr', duplicate_role = 'discard' WHERE id = donor_id`.
9. `UPDATE photos SET duplicate_role = 'keeper' WHERE id = target_id`.
10. `UPDATE duplicate_groups SET resolved = 1, keeper_id = target_id, resolved_at = datetime('now') WHERE id = group_id`.

### API — new `"merge"` branch in `/api/duplicates/<group_id>/assign`

Request JSON: `{"action": "merge", "donor_id": <int>, "target_id": <int>}`

```python
elif action == "merge":
    donor_id  = data.get("donor_id")
    target_id = data.get("target_id")
    if not donor_id or not target_id:
        return jsonify({"ok": False, "error": "missing donor_id or target_id"}), 400
    # validate both are members of group_id
    for pid in (donor_id, target_id):
        row = conn.execute(
            "SELECT id FROM photos WHERE id = ? AND duplicate_group_id = ?", (pid, group_id)
        ).fetchone()
        if not row:
            return jsonify({"ok": False, "error": f"photo {pid} not in group"}), 400
    try:
        merge_flickr_donor(conn, donor_id, target_id, group_id)
        conn.commit()
        return jsonify({"ok": True})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
```

### Route `/duplicates` — additional group data

For each group, compute before rendering:

```python
flickr_only_ids = {p["id"] for p in group_photos if p["flickr_id"] and not p["uuid"]}
photos_targets = sorted(
    [p for p in group_photos if p["uuid"]],
    key=lambda p: (p["width"] or 0) * (p["height"] or 0),
    reverse=True,
)
# photos_targets rendered as [{id, label: "filename W×H"}]
```

Pass `flickr_only_ids` and `photos_targets` into each group dict.

### Template — `duplicates.html`

Inside `.dup-photo-meta`, after the existing "Make keeper" button:

```html
{% if photo.id in group.flickr_only_ids and group.photos_targets %}
<div class="dup-merge-wrap">
  <button class="btn btn-merge"
          onclick="showMergeConfirm(this, {{ group.id }}, {{ photo.id }},
                   {{ group.photos_targets | tojson }})">
    Merge into Photos record
  </button>
  <div class="merge-confirm" style="display:none">
    <label>Into:
      <select class="merge-target-select"></select>
    </label>
    <button class="btn btn-primary merge-confirm-btn">Confirm merge</button>
    <a class="merge-cancel" href="#">Cancel</a>
  </div>
</div>
{% endif %}
```

CSS:
```css
.btn-merge    { font-size: 12px; padding: 4px 10px; color: #6ab4f5; }
.merge-confirm { margin-top: 6px; display: flex; flex-direction: column; gap: 6px;
                 font-size: 12px; padding: 8px; background: var(--bg);
                 border: 1px solid var(--border); border-radius: var(--radius); }
```

JS:
```javascript
function showMergeConfirm(btn, groupId, donorId, targets) {
  const wrap = btn.closest('.dup-merge-wrap');
  btn.style.display = 'none';
  const confirm = wrap.querySelector('.merge-confirm');
  const sel = wrap.querySelector('.merge-target-select');
  sel.innerHTML = targets.map(t =>
    `<option value="${t.id}">${t.label}</option>`
  ).join('');
  confirm.style.display = '';
  wrap.querySelector('.merge-confirm-btn').onclick =
    () => confirmMerge(groupId, donorId, wrap);
  wrap.querySelector('.merge-cancel').onclick = e => {
    e.preventDefault();
    confirm.style.display = 'none';
    btn.style.display = '';
  };
}

async function confirmMerge(groupId, donorId, wrap) {
  const targetId = parseInt(wrap.querySelector('.merge-target-select').value, 10);
  const confirmBtn = wrap.querySelector('.merge-confirm-btn');
  confirmBtn.disabled = true;
  const r = await apiFetch(`/api/duplicates/${groupId}/assign`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({action: 'merge', donor_id: donorId, target_id: targetId}),
  });
  const d = await r.json();
  if (d.ok) {
    toast('Merged — Flickr identity moved to Photos record');
    const card = document.getElementById(`group-${groupId}`);
    card.style.opacity = '0.4';
    card.querySelectorAll('button').forEach(b => b.disabled = true);
    refreshStats();
  } else {
    toast('Error: ' + (d.error || 'unknown'), 'err');
    confirmBtn.disabled = false;
  }
}
```

---

## Test plan

### `TestMergeFlickrDonor` (test_core.py)

Use a helper `_make_merge_db()` that creates a DB with:
- A duplicate group (group_type `snapbridge`)
- A Flickr-only record (donor): `flickr_id='F1'`, `uuid=None`, some Flickr columns populated
- A Photos-linked record (target): `uuid='U1'`, `flickr_id=None`, dimensions 4000×3000

Tests:
1. `flickr_id` copied from donor to target
2. `flickr_secret`, `date_uploaded_flickr` spot-checked on target
3. Donor `flickr_id` is NULL after merge
4. Donor `merged_into_id == target.id`
5. Donor `privacy_state == 'duplicate_flickr'`, `duplicate_role == 'discard'`
6. Target `duplicate_role == 'keeper'`
7. Group `resolved == 1`, `keeper_id == target.id`
8. `photo_albums` rows from donor are migrated to target
9. `tag_events` rows from donor are migrated to target; donor has none after
10. `ValueError` raised if donor has a `uuid`
11. `ValueError` raised if target has no `uuid`

### `TestMergeUI` (test_review_ui.py)

Fixture `client_with_merge_group`: group with one Flickr-only record + one Photos-linked record.

Tests:
1. GET `/duplicates` — Merge button present on Flickr-only card
2. GET `/duplicates` — Merge button absent on Photos-linked card
3. POST `/api/duplicates/<id>/assign` `merge` action — 200 `{"ok": true}`
4. POST with donor not in group — 404
5. POST with donor that has `uuid` — 400

---

## Out of scope

- Merging when both records have a `uuid` (Photos already de-duplicates those)
- Multi-record merge (more than one donor per group)
- Undo/rollback of a merge (the `merged_into_id` column provides the audit trail; recovery is manual)
