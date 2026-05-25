# Bulk Operations — Design Spec

**Date:** 2026-05-24  
**Status:** Approved for implementation planning  
**Related issue:** TBD (to be filed before implementation begins)

---

## Overview

A bulk-operations feature for Blue Pearmain that lets the user annotate groups of photos in a single action — setting default titles and descriptions across an event set, stamping a batch of photos with shared tags, or cleaning up a misspelled tag. The primary mental model is **second-pass annotation**: the user has already triaged photos individually; now they want to enrich a group efficiently before refining individuals.

---

## Scope

### In v1

| Field | Operations |
|---|---|
| Title | Bulk set (with option to skip photos that already have one) |
| Description | Bulk set (with option to skip photos that already have one) |
| Tags | Add tags to a set; Remove tags from a set (separate actions) |

Tag *add* is the dominant use case. Tag *remove* is an escape hatch (typo correction, hitting Flickr's 75-tag limit).

### Explicitly out of v1

- **Star ratings** — used to *differentiate* within a set (highlighting "keepers"), not to stamp across one. Bulk application would undermine the purpose.
- **Privacy status** — requires fine-grained per-photo judgment. Bulk flips risk mistakes that are hard to audit after the fact.

---

## Feature 1 — New `/library` page

A new route serving as the primary home for bulk operations.

### Filter bar

A horizontal bar at the top of the page. Active filters render as dismissible chips. Photo count shown right-aligned.

| Filter | Control |
|---|---|
| Date range | Date picker (from / to) |
| Album | Dropdown of Flickr photosets |
| Tags | Tag search (filter to photos having a specific tag) |
| Status | Dropdown: All / Public / Private / Pending |
| Untitled only | Toggle chip — when active, shows only photos with no title |

### Photo grid

Fills the page below the filter bar. Larger grid than the review queue (no sidebar stealing space). Works well on iPad/phone via LAN.

Checkboxes appear on photo thumbnails on hover. Selected photos show a blue highlight border.

### Selection row

Sits between the filter bar and the grid:

> ☐ Select all matching — 142 photos

Checking selects every photo matching the current active filters. The user can then **manually deselect exceptions** (filter-then-subtract pattern). Unchecking clears all selections. Manual individual picks work independently of the select-all state.

### Action bar

Appears above the grid when ≥ 1 photo is selected:

```
47 selected  |  Edit title  Edit description  Add tags  Remove tags  ✕ Clear
```

Disappears (or collapses) when selection is cleared.

### Navigation

`Library` added to the top nav alongside `Review`, `Proposals`, `Duplicates`, etc.

---

## Feature 2 — Inline edit panel

When the user clicks an action in the action bar, a panel expands **between the filter bar and the photo grid**. The grid stays visible below the panel, dimmed — selected photos remain highlighted with their blue border so the user can verify the set and spot accidental inclusions or omissions before confirming.

If something looks wrong, the user cancels, adjusts the selection in the grid, and re-triggers the action.

### Title / description panel

```
┌─ Edit title · 47 photos ──────────────────────────────────┐
│  [MFA Boston — May 2024                                  ] │
│  ☑ Skip photos that already have a title                   │
│     32 will be updated · 15 skipped                        │
│  [Queue 32 proposals]  Cancel                              │
└────────────────────────────────────────────────────────────┘
```

- Single-line input for title; multiline textarea for description
- "Skip" checkbox is checked by default; count updates live as the user types or toggles the checkbox
- Confirming creates one proposal per affected photo

### Add tags panel

```
┌─ Add tags · 47 photos ────────────────────────────────────┐
│  [mfa-boston ×] [impressionism ×] [type to add…]          │
│  These 2 tags will be added to 47 photos                   │
│  [Queue proposals]  Cancel                                 │
└────────────────────────────────────────────────────────────┘
```

- Chip/token input — type to search existing tags, Enter to commit each chip
- No skip option needed: adding a tag to a photo that already has it is a no-op

### Remove tags panel

Same chip input as add; chips render in red.

```
┌─ Remove tags · 47 photos ─────────────────────────────────┐
│  [mfa-bosten ×]                                            │
│  This tag will be removed from photos that have it         │
│  [Queue proposals]  Cancel                                 │
└────────────────────────────────────────────────────────────┘
```

- Summary reflects actual coverage: "removed from photos that have it" rather than claiming all 47 will be affected

---

## Feature 3 — Bulk-select mode in `/review`

The existing review queue gets a lightweight **"Select" toggle** in its toolbar. When active:

- Checkboxes appear on thumbnails
- The same action bar appears (title / description / add tags / remove tags)
- The same inline panel and proposal flow as the library view
- "Select all" selects all photos currently visible in the queue

Privacy decisions (public/private/skip) and star ratings remain single-photo operations in the review queue.

---

## Action model

All bulk actions go through the existing proposals system:

1. User selects photos, chooses action, fills in the panel
2. Clicks "Queue N proposals" — no write to Flickr or Apple Photos yet
3. Proposals appear in the `/proposals` queue (same schema as sync-engine proposals)
4. The existing bulk-approve button on `/proposals` can approve them all in one click, or the user reviews individually

No new proposal type is needed. Proposals generated by bulk operations are indistinguishable from sync-engine proposals in the queue — they carry the same field, old value, new value, and source.

---

## API and data layer

### New route

| Route | Description |
|---|---|
| `GET /library` | Library page; accepts filter params as query string |

### New endpoint

`POST /api/bulk-edit`

Accepts one of two payload shapes:

**Explicit selection (manual picks):**
```json
{
  "field": "title",
  "value": "MFA Boston — May 2024",
  "photo_ids": [1042, 1043, 1089, ...],
  "skip_existing": true
}
```

**Filter-based selection (select-all-matching):**
```json
{
  "field": "tags_add",
  "tags": ["mfa-boston", "impressionism"],
  "filter": {
    "date_from": "2024-05-01",
    "date_to": "2024-05-31",
    "album": null,
    "tag": null,
    "status": null,
    "untitled": false
  }
}
```

The backend resolves the filter server-side. This keeps the request payload small for large sets and avoids loading thousands of IDs into the browser.

Returns: `{ "ok": true, "proposals_created": 32 }`

### DB impact

No schema changes required. Proposals are written to the existing `proposals` table. The library view query selects from the existing `photos` table with filter conditions — a new DB method on `Database` that accepts filter parameters.

### Pagination

The library grid paginates like the review queue. Manual selected IDs are tracked client-side across pages. Filter-based selection is stateless — resolved server-side at submit time, so pagination doesn't affect correctness.

---

## Out of scope (future)

- Bulk album assignment (tracked separately as issue #124)
- Bulk privacy changes (deferred; fine-grained judgment needed)
- Bulk star ratings (deferred; used for intra-set differentiation)
- Undo / undo history for bulk proposals (the proposal queue itself serves this role — reject before pushing)
