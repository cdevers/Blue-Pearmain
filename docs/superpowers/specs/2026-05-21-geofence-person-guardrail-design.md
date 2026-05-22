# Design: Geofence & Person-Policy Guardrail in Review UI

**Date:** 2026-05-21  
**Status:** Approved — ready for implementation planning  
**GitHub issue:** TBD (to be filed)

---

## Problem

The review UI lets the operator approve photos as public using a single keystroke (`p`) or a single button click. This is appropriate for the normal review flow, but creates accidental-disclosure risk for two categories of photo:

1. **Geofenced photos** — photos taken within a named geofence zone (e.g. "work"). These should be `auto_private` by default, but can reach the review queue retroactively (zone added after classification) or at zone boundaries (GPS uncertainty, border photos).
2. **Private-person photos** — photos containing a person with an `always_private` policy. Same retroactive-addition risk as geofenced photos.

The operator wants protection against accidentally approving either category. The intent: *possible, but cumbersome enough to prevent inattentive approval*.

---

## What triggers the guardrail

A photo in the review queue is **flagged** if either of the following is true:

- `geofence_zone IS NOT NULL` — the photo matched any active geofence zone during classification
- Any name in `apple_persons` (JSON array) has an `always_private` policy in the `person_policies` table

Flagging is checked client-side using data already embedded in the page (see Data Changes below). No extra round-trip is needed per photo.

---

## UX behaviour

### Normal (non-flagged) photo
Unchanged. `p` key approves directly; Approve button approves directly.

### Flagged photo

**In the review grid:**
- A small ⚠️ badge appears on the tile, with a label:
  - `"Geofence: [zone name]"` (e.g. "Geofence: work"), or
  - `"Private person: [name]"` (e.g. "Private person: Jane Smith")
  - If both apply, show both (stacked or comma-separated)

**Keyboard:**
- The `p` key has **no effect** on flagged photos. Pressing it produces a brief visual pulse on the ⚠️ badge (indicating the keypress was received and suppressed), but takes no action.

**Approve button:**
- Replaced by an **"Override →"** button. Visually distinct: amber/outlined style, not the normal green.

**Override modal (opens on "Override →" click):**
- Header: `⚠️ Protected photo`
- Body: One sentence explaining the flag — e.g. *"This photo was taken in the 'work' geofence zone and would normally be kept private."* or *"Jane Smith has a private-person policy."*
- Optional text field: `Reason for override (optional)`
- Two buttons:
  - `Cancel` — dismisses modal, no action (keyboard: Escape)
  - `Make public anyway` — de-emphasised (small, not bold, positioned away from Cancel). **No keyboard shortcut** — requires a deliberate mouse/pointer click.

**On confirm:**
1. Photo is approved as public (same backend call as normal approval).
2. Override is recorded to `operation_log` with:
   - `action`: `'geofence_override'` or `'policy_override'` (or `'geofence_and_policy_override'` if both apply)
   - `photo_id`: the photo's ID
   - `note`: the operator's optional note (may be empty string)
   - `context`: zone name and/or person name, for future auditability

---

## Data changes

### `db.py` — `review_queue()` SQL

Add three columns to the SELECT:
```sql
geofence_zone,
apple_persons,
privacy_reason
```

These are already in the `photos` table. Adding them here makes them available to the template without a second query.

### `app.py` — `review()` route

Pass person policies to the template:
```python
person_policies = db().get_person_policies()   # {name: policy}
# pass as template variable: person_policies=person_policies
```

The template uses this dict to determine whether any person in a photo's `apple_persons` list has `always_private` policy.

### `app.py` — decision endpoint (`/api/review` or equivalent)

Add optional `override_note` parameter (string, may be empty). When present, record to `operation_log` using the existing column mapping:

| `operation_log` column | Value |
|---|---|
| `operation` | `'geofence_override'` \| `'policy_override'` \| `'geofence_and_policy_override'` |
| `target` | `'privacy_state'` |
| `old_value` | current privacy_state of the photo |
| `new_value` | `'approved_public'` |
| `trigger` | JSON string: `{"zone": "work", "person": "Jane Smith", "note": "..."}` — omit absent keys |
| `actor` | `'manual'` |

No schema migration required — `operation_log` already exists and accepts freeform values in all text columns.

---

## Frontend changes

### `review.html` template

1. Embed the set of protected person names as a JS array in the page (via Jinja, in a `<script>` block):
   ```js
   const PRIVATE_PERSONS = new Set({{ private_person_names | tojson }});
   ```
   Where `private_person_names` is computed in the route as:
   ```python
   policies = db().get_person_policies()   # {name: policy}
   private_person_names = [n for n, p in policies.items() if p == "always_private"]
   ```

2. For each photo tile, compute `isProtected` based on `geofence_zone` and `apple_persons` vs `PRIVATE_PERSONS`.

3. Render ⚠️ badge when `isProtected` is true, with appropriate label.

4. In the keyboard handler: guard the `p` key — if the currently-focused photo is flagged, pulse the badge and return early instead of approving.

5. Replace the Approve button with "Override →" for flagged photos.

6. Add the Override modal (single shared modal element, populated dynamically per photo).

---

## Explicit non-goals

- **No UI for overriding `auto_private` photos from outside the review queue.** Photos that are correctly classified `auto_private` remain hidden. The very rare case of intentionally surfacing one is handled by direct DB access until there's demonstrated need for a UI.
- **No retroactive reclassification batch run.** The zone re-scan already handles new photos; existing `candidate_public` photos at zone boundaries are handled by the guardrail, not by bulk reclassification.
- **No change to the `auto_private` → review-queue promotion flow.** That is a separate future feature.

---

## Testing

- Unit: `test_geofence_guardrail.py` — verify `review_queue()` returns `geofence_zone` and `apple_persons` correctly; verify operation_log entry written on override
- Integration: reviewer template renders ⚠️ badge when `geofence_zone` is set; "Override →" button present; normal Approve button absent for flagged photos
- Manual: verify `p` key is suppressed for a flagged photo; modal appears; confirm writes to operation_log

---

## Open questions (resolved)

- *How much audit trail on override?* → Log entry with zone/person name + optional note. No required text entry.
- *Two-stage override or confirm dialog?* → Confirm dialog (modal). Two-stage can be revisited if overrides turn out to be more frequent than expected.
- *Suppress `p` or redirect it to the modal?* → Suppress entirely (pulse badge). Redirecting `p` to open the modal would still be one keypress away from accidental approval.
