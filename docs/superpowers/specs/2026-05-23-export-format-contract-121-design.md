# Export Format Version Contract Design — Issue #121

**Date:** 2026-05-23
**Issue:** https://github.com/cdevers/Blue-Pearmain/issues/121

---

## Goal

Document the `bp export` format version 1 contract in `docs/export-format.md` — field names, types, semantics — and add a schema-validation test so any future field change breaks loudly, prompting the developer to bump `export_format_version`.

## What `bp export` produces

Three files in the output directory:
- `photos.ndjson` — one JSON object per line, one per photo
- `zones.json` — JSON array of geofence zone objects
- `manifest.json` — metadata about the export itself

`manifest.json` contains `export_format_version: "1"` and `bp_version`. The version string must be bumped whenever the shape of `photos.ndjson` or `zones.json` changes in a breaking way (field removed, renamed, type changed). Non-breaking additions (new optional field) should be documented but may not require a bump — developer judgment call.

## Version 1 Fields

### `photos.ndjson`

| Field | Type | Nullable | Description |
|---|---|---|---|
| `id` | integer | no | Internal BP database row ID |
| `flickr_id` | string | yes | Flickr photo ID (null if not yet uploaded) |
| `apple_uuid` | string | yes | Apple Photos UUID |
| `original_filename` | string | yes | Filename as imported from Photos |
| `title` | string | no | Flickr title (empty string if unset) |
| `description` | string | no | Flickr description (empty string if unset) |
| `tags` | array of string | yes | Flickr tags (falls back to Photos tags; null if none) |
| `privacy_state` | string | no | BP privacy state enum |
| `review_decision` | string | yes | Most recent review decision |
| `reviewed_at` | string | yes | ISO 8601 datetime of last review |
| `date_taken` | string | yes | ISO 8601 datetime photo was taken |
| `location` | object | yes | `{latitude, longitude, city, state, country}` or null |
| `geofenced` | boolean | no | True if photo falls within a geofence zone |
| `faces` | array of string | no | Named people (excludes `_UNKNOWN_`) |
| `albums` | array of string | no | Album names the photo belongs to |

### `zones.json`

| Field | Type | Nullable | Description |
|---|---|---|---|
| `name` | string | no | Zone identifier |
| `label` | string | yes | Human-readable label |
| `latitude` | float | no | Center latitude |
| `longitude` | float | no | Center longitude |
| `radius_m` | float | no | Radius in metres |
| `policy` | string | no | Privacy policy applied within zone |
| `active` | boolean | no | Whether zone is currently enforced |
| `notes` | string | yes | Free-text notes |

## Version bump policy

Bump `export_format_version` (in `poller/exporter.py`, `write_export()`) when:
- A field is removed or renamed
- A field's type or nullability changes
- A field's semantics change incompatibly

Update `docs/export-format.md` version history table and note what changed.

Do NOT need to bump for:
- New optional fields added to the output (additive, non-breaking)
- Changes to `manifest.json` only

## Schema-validation test

Add `TestExportFormatVersion` to `tests/test_exporter.py`. Two tests:
- Assert `serialize_photo()` returns exactly the expected key set
- Assert `serialize_zone()` returns exactly the expected key set

If a field is added or removed without updating the test, pytest fails with a clear set-diff error, prompting a doc update and version bump decision.

## Files changed

| File | Change |
|---|---|
| `docs/export-format.md` | New — v1 field reference + version bump policy + CHANGELOG table |
| `tests/test_exporter.py` | Add `TestExportFormatVersion` (2 tests) |
