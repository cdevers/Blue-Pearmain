# Export Format v1 Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Write `docs/export-format.md` documenting the v1 export contract, and add a schema-validation test to `tests/test_exporter.py` so field changes fail loudly.

**Architecture:** Documentation-only + 2 new test assertions. No changes to production code.

**Tech Stack:** Markdown, Python/unittest.

---

## File Map

| File | Change |
|---|---|
| `docs/export-format.md` | New — v1 field reference, version policy, CHANGELOG |
| `tests/test_exporter.py` | Add `TestExportFormatVersion` class (2 tests) |

---

### Task 1: Schema-validation test (TDD)

**Files:**
- Modify: `tests/test_exporter.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_exporter.py` (after the existing test classes):

```python
class TestExportFormatVersion(unittest.TestCase):
    """
    Contract tests: assert that serialize_photo and serialize_zone return
    exactly the documented v1 field set. If a field is added or removed,
    this test breaks — the developer must then update docs/export-format.md
    and decide whether to bump export_format_version.
    """

    _EXPECTED_PHOTO_KEYS = {
        "id",
        "flickr_id",
        "apple_uuid",
        "original_filename",
        "title",
        "description",
        "tags",
        "privacy_state",
        "review_decision",
        "reviewed_at",
        "date_taken",
        "location",
        "geofenced",
        "faces",
        "albums",
    }

    _EXPECTED_ZONE_KEYS = {
        "name",
        "label",
        "latitude",
        "longitude",
        "radius_m",
        "policy",
        "active",
        "notes",
    }

    def test_serialize_photo_exact_keys(self):
        """serialize_photo must return exactly the v1 documented fields — no more, no less."""
        row = {
            "id": 1,
            "flickr_id": "123",
            "uuid": "AAAA-BBBB",
            "original_filename": "IMG_001.HEIC",
            "flickr_title": "Title",
            "flickr_description": "Desc",
            "flickr_tags": '["tag1"]',
            "photos_tags": None,
            "privacy_state": "approved_public",
            "review_decision": "make_public",
            "reviewed_at": "2026-01-01T00:00:00",
            "date_taken": "2025-06-15T12:00:00",
            "latitude": 42.3,
            "longitude": -71.1,
            "place_city": "Boston",
            "place_state": "Massachusetts",
            "place_country": "United States",
            "geofence_zone": None,
            "apple_persons": '["Alice"]',
        }
        result = serialize_photo(row, album_names=["Vacation"])
        self.assertEqual(
            set(result.keys()),
            self._EXPECTED_PHOTO_KEYS,
            msg=(
                f"serialize_photo key mismatch.\n"
                f"  Extra keys:   {set(result.keys()) - self._EXPECTED_PHOTO_KEYS}\n"
                f"  Missing keys: {self._EXPECTED_PHOTO_KEYS - set(result.keys())}\n"
                f"If intentional: update docs/export-format.md and bump export_format_version."
            ),
        )

    def test_serialize_zone_exact_keys(self):
        """serialize_zone must return exactly the v1 documented fields — no more, no less."""
        row = {
            "name": "home",
            "label": "Home",
            "latitude": 42.3,
            "longitude": -71.1,
            "radius_m": 500.0,
            "policy": "auto_private",
            "active": 1,
            "notes": "Primary residence",
        }
        result = serialize_zone(row)
        self.assertEqual(
            set(result.keys()),
            self._EXPECTED_ZONE_KEYS,
            msg=(
                f"serialize_zone key mismatch.\n"
                f"  Extra keys:   {set(result.keys()) - self._EXPECTED_ZONE_KEYS}\n"
                f"  Missing keys: {self._EXPECTED_ZONE_KEYS - set(result.keys())}\n"
                f"If intentional: update docs/export-format.md and bump export_format_version."
            ),
        )
```

- [ ] **Step 2: Run tests — expect PASS immediately**

These tests should pass immediately because the documented fields exactly match what `serialize_photo` and `serialize_zone` already produce.

```bash
python -m pytest tests/test_exporter.py::TestExportFormatVersion -v
```

Expected: 2 tests PASS.

- [ ] **Step 3: Run full test suite**

```bash
python -m pytest tests/ -q
```

Expected: all tests PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/test_exporter.py
git commit -m "test: add export format v1 schema validation tests (#121)"
```

---

### Task 2: Write `docs/export-format.md`

**Files:**
- Create: `docs/export-format.md`

- [ ] **Step 1: Create the file**

Write `docs/export-format.md` with this exact content:

````markdown
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
````

- [ ] **Step 2: Verify the file looks correct**

```bash
cat docs/export-format.md | head -20
```

- [ ] **Step 3: Run full test suite (no regressions)**

```bash
python -m pytest tests/ -q
```

Expected: all tests PASS.

- [ ] **Step 4: Run lint**

```bash
make lint
```

Expected: no issues.

- [ ] **Step 5: Commit**

```bash
git add docs/export-format.md
git commit -m "docs: add export-format.md with v1 field reference and version policy (#121)"
```

---

### Task 3: Close issue and push

- [ ] **Step 1: Close GH issue**

```bash
gh issue close 121 --comment "Implemented. \`docs/export-format.md\` documents the v1 contract (15 photo fields, 8 zone fields) with type/nullable/description for each, version bump policy, and CHANGELOG table. \`TestExportFormatVersion\` in \`tests/test_exporter.py\` enforces the exact field sets — any future field change will break this test, prompting a doc update and version bump decision."
```

- [ ] **Step 2: Bump version to 1.0.9 in `pyproject.toml`**

Change `version = "1.0.8"` to `version = "1.0.9"`.

- [ ] **Step 3: Commit version bump**

```bash
git add pyproject.toml
git commit -m "Bump version to 1.0.9"
```

- [ ] **Step 4: Push to origin**

```bash
git push origin main
```
