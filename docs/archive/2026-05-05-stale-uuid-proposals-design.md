# Design: Stale UUID proposal termination (GH #23)

**Date:** 2026-05-05  
**Issue:** https://github.com/cdevers/Blue-Pearmain/issues/23  
**Status:** Approved, ready for implementation

---

## Problem

When `apply_proposal` (or any Photos-write path) calls `photoscript.Photo(uuid)` and the UUID is no longer valid in the Photos library, Photos.app raises an exception containing "invalid photo ID". The proposal stays `pending` indefinitely, blocking bulk-apply and accumulating in the queue.

Known causes: photo deleted from Photos, library rebuilt/repaired (UUID reassignment), wrong library configured.

---

## Schema changes (migration 010)

### `metadata_proposals.status`

Add `'failed'` to the CHECK constraint. Requires table recreation (SQLite cannot ALTER a CHECK constraint):

```sql
CHECK(status IN ('pending', 'applied', 'rejected', 'superseded', 'failed'))
```

Pattern: rename old table → create new → copy data → drop old → recreate indexes. Same pattern as migrate_007.

`db/schema.sql` updated to match.

### `photos.uuid_stale`

```sql
ALTER TABLE photos ADD COLUMN uuid_stale INTEGER NOT NULL DEFAULT 0;
```

Simple column addition. Set to `1` when a stale-UUID error is detected on that photo. Queryable for future features (e.g. `bp find-stale`, Flickr tagging).

---

## Code changes

### `db/db.py` — `resolve_proposal`

Add `"failed"` to the allowed-status assertion:

```python
assert status in ("rejected", "applied", "superseded", "failed")
```

### `flickr/proposal_applier.py` — write-to-Photos helpers

Three helpers catch the "photo not found" exception: `_write_tags_to_photos`, `_apply_text_to_photos`, `_write_text_to_photos_both`. Each gains a sub-check on the error string:

```python
except Exception as e:
    if "invalid photo id" in str(e).lower():
        return {"ok": False, "reason": "stale_uuid", "stale_uuid": True}
    return {"ok": False, "reason": f"photo not found in Photos: {e}"}
```

### Call sites that check the result dict

Four functions receive the result from a write-to-Photos helper: `apply_proposal`, `apply_manual_merge`, `apply_collision_reverse`, `set_photo_text`. Each gains a stale-UUID branch:

```python
if not r["ok"]:
    if r.get("stale_uuid"):
        db.conn.execute(
            "UPDATE photos SET uuid_stale=1, updated_at=? WHERE id=?",
            (_now_iso(), photo_id),
        )
        _mark_failed(db, proposal_id, note="stale_uuid")
        db.conn.commit()
        return {"ok": False, "reason": "stale_uuid"}
    # existing error handling ...
```

A new private helper `_mark_failed(db, proposal_id, note)` sets `status='failed'` and `resolution_note`.

### `apply_batch`

`apply_batch` already wraps each call in try/except and accumulates an `errors` list. The `stale_uuid` return is not an unexpected exception — it's a clean `{"ok": False, "reason": "stale_uuid"}` dict. The existing dict-failure path adds it to `errors`. Change: check for `reason == "stale_uuid"` and count it in `totals["failed"]` instead of `errors`, so the UI toast distinguishes "N applied, M permanently failed" from unexpected errors.

---

## Error detection

Match on `"invalid photo id"` (case-insensitive) in the exception string. This is the string Photos.app returns via photoscript for unknown UUIDs. A different Photos exception (e.g. permissions, Photos not running) does NOT match and the proposal stays `pending` as before.

---

## Testing (5 tests in `TestStaleUuid`)

| Test | What it checks |
|---|---|
| `test_stale_uuid_marks_proposal_failed` | Mock photoscript to raise "invalid photo ID"; assert proposal → `failed`, note = `stale_uuid` |
| `test_stale_uuid_sets_flag_on_photo` | Same setup; assert `uuid_stale = 1` on the photos row |
| `test_stale_uuid_in_apply_batch_counted_as_failed` | Seed two proposals (one stale, one normal); assert `totals["failed"] == 1`, `errors == []` |
| `test_non_uuid_error_stays_pending` | Mock a different Photos exception; assert proposal stays `pending`, `uuid_stale` stays 0 |
| `test_migration_010_idempotent` | Run migration twice; assert no error and schema is correct |

---

## Out of scope

- Writing a Flickr tag to mark stale-UUID photos (flagged as a possible future feature)
- `bp find-stale` CLI command (can be added later using the `uuid_stale` column)
- Clearing the `uuid` field from the photos row to allow re-matching (UUID is preserved so the stale state is visible; clearing it would silently drop the link)
