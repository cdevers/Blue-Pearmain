# Blue Pearmain Export Format

`bp export` produces a directory containing three files that together represent a portable snapshot of the BP database.

---

## Output files

| File | Description |
|---|---|
| `photos.ndjson` | Newline-delimited JSON — one object per photo |
| `zones.json` | JSON array of geofence zone definitions |
| `manifest.json` | Export metadata (version, timestamp, counts) |

`manifest.json` fields: `bp_version` (string), `export_format_version` (string), `exported_at` (ISO 8601), `photo_count` (integer), `zone_count` (integer).

---

## Version policy

`export_format_version` must be bumped whenever the shape of `photos.ndjson` or `zones.json` changes in a **breaking** way:

- A field is **removed** or **renamed**
- A field's **type** or **nullability** changes incompatibly
- A field's **semantics** change in a way consumers cannot detect

**Do NOT bump for:**
- New optional fields added to the output (additive, non-breaking)
- Changes to `manifest.json` only

When bumping: update `export_format_version` in `poller/exporter.py` (`write_export()`), add a row to the [Version history](#version-history) table below, and update the field tables in this document.

A schema-validation test in `tests/test_exporter.py` (`TestExportFormatVersion`) asserts the exact field sets for `serialize_photo()` and `serialize_zone()`. That test must be updated alongside any field change.

---

## Version 1 — `photos.ndjson` fields

Each line is a JSON object with the following fields:

| Field | Type | Nullable | Description |
|---|---|---|---|
| `id` | integer | no | Internal BP database row ID |
| `flickr_id` | string | yes | Flickr photo ID; `null` if photo has not been uploaded to Flickr |
| `apple_uuid` | string | yes | Apple Photos UUID |
| `original_filename` | string | yes | Filename as imported from Apple Photos |
| `title` | string | no | Flickr title; empty string `""` if unset |
| `description` | string | no | Flickr description; empty string `""` if unset |
| `tags` | array of string | yes | Flickr tags; falls back to Apple Photos tags; `null` if neither is set |
| `privacy_state` | string | no | BP privacy state (e.g. `approved_public`, `keep_private`, `needs_review`) |
| `review_decision` | string | yes | Most recent review decision applied to this photo |
| `reviewed_at` | string | yes | ISO 8601 datetime of the most recent review action |
| `date_taken` | string | yes | ISO 8601 datetime the photo was taken |
| `location` | object | yes | `{latitude, longitude, city, state, country}` if known; `null` otherwise |
| `geofenced` | boolean | no | `true` if the photo's location falls within an active geofence zone |
| `faces` | array of string | no | Named people detected by Apple Photos (excludes the `_UNKNOWN_` sentinel); empty array if none |
| `albums` | array of string | no | Names of albums the photo belongs to; empty array if none |

### `location` object

When `location` is not null:

| Field | Type | Nullable | Description |
|---|---|---|---|
| `latitude` | float | no | Decimal degrees, WGS84 |
| `longitude` | float | no | Decimal degrees, WGS84 |
| `city` | string | yes | City name from reverse geocoding |
| `state` | string | yes | State/province from reverse geocoding |
| `country` | string | yes | Country name from reverse geocoding |

---

## Version 1 — `zones.json` fields

`zones.json` is a JSON array. Each element has the following fields:

| Field | Type | Nullable | Description |
|---|---|---|---|
| `name` | string | no | Zone identifier (unique) |
| `label` | string | yes | Human-readable label for the zone |
| `latitude` | float | no | Center latitude of the zone (decimal degrees, WGS84) |
| `longitude` | float | no | Center longitude of the zone (decimal degrees, WGS84) |
| `radius_m` | float | no | Zone radius in metres |
| `policy` | string | no | Privacy policy applied to photos within this zone (e.g. `auto_private`) |
| `active` | boolean | no | `true` if the zone is currently enforced |
| `notes` | string | yes | Free-text notes about this zone |

---

## Version history

| Version | Date | Changes |
|---|---|---|
| 1 | 2026-05-23 | Initial format: 15 photo fields, 8 zone fields |
