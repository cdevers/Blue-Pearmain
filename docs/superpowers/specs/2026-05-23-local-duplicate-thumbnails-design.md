# Local Photos thumbnails + `local_duplicate` classifier

**Date:** 2026-05-23  
**Status:** Approved — ready for implementation plan  
**Related issue:** [#130](https://github.com/cdevers/Blue-Pearmain/issues/130)

---

## Background

The `/duplicates` reviewer page shows blank thumbnails for all 346 unresolved `uncertain` groups because 218 of the 837 member photos are Apple Photos-only records with no `flickr_id` and no `thumbnail_path`. Two root causes:

1. **`derivative_path` in `thumbnailer.py` checks only one path pattern.** Photos stores pre-generated JPEG derivatives in at least three locations depending on import source (card import, Shared Moments, etc.). Records whose derivatives live outside the `resources/derivatives/masters/` tree never get `thumbnail_path` populated, so the reviewer renders `"no preview"` instead of the actual image.

2. **Same-fingerprint uncertain groups have no dedicated classification.** 333 of the 346 uncertain groups consist of the same image imported into Apple Photos multiple times (identical fingerprint across all members), where one copy was matched to Flickr and the others were not. The deduplicator currently classifies these as `uncertain`, which carries no actionable "Confirm" path and no UI guidance.

---

## Feature A — Fix thumbnail rendering

### `derivative_path` fix (`poller/thumbnailer.py`)

Replace the existing single-path check with three ordered candidates. Return the first that exists on disk:

```python
def derivative_path(uuid: str, library_path: str) -> str | None:
    """
    Return the path to a Photos pre-generated JPEG derivative for this UUID,
    or None if no derivative is found on disk.

    Tries three candidate locations in order:
      1. resources/derivatives/masters/{shard}/{uuid}_4_5005_c.jpeg
         — standard card-import derivative
      2. resources/derivatives/{shard}/{uuid}_1_105_c.jpeg
         — smaller derivative used for some import paths
      3. scopes/momentshared/resources/derivatives/masters/{shard}/{uuid}_4_5005_c.jpeg
         — Shared Moments scope
    """
    if not uuid:
        return None
    shard = uuid[0].lower()
    lib = Path(library_path)
    candidates = [
        lib / "resources" / "derivatives" / "masters" / shard / f"{uuid}_4_5005_c.jpeg",
        lib / "resources" / "derivatives" / shard / f"{uuid}_1_105_c.jpeg",
        lib / "scopes" / "momentshared" / "resources" / "derivatives" / "masters" / shard / f"{uuid}_4_5005_c.jpeg",
    ]
    for p in candidates:
        if p.exists():
            return str(p)
    return None
```

### Live fallback in `/thumb/<id>` (`reviewer/app.py`)

The current thumb route tries: stored URL → local file → Flickr URL → placeholder. Add a step between "local file" and "Flickr URL":

**Step 2.5 — Live derivative lookup:**  
If `thumbnail_path` is empty and the record has a `uuid`, fetch `library_path` from the app config and call `derivative_path(uuid, library_path)`. If a path is returned and the file exists:
- Write `thumbnail_path` back to the DB so future requests skip this step.
- Serve the file via `send_file`.

The thumb route query must also select `uuid` to support this step.

No new endpoint. No template changes. The placeholder SVG remains the true last resort.

### One-time backfill

After shipping the code fix, run:

```bash
python poller/thumbnailer.py --config config/config.yml
```

This populates `thumbnail_path` for the 218 affected uncertain-group members (and the remaining ~300 across the rest of the DB). Going forward, the live fallback handles any new Photos records between thumbnailer cycles.

---

## Feature B — `local_duplicate` classifier

### New `_is_local_duplicate` function (`poller/deduplicator.py`)

```python
def _is_local_duplicate(photos: list[PhotoRow]) -> bool:
    """
    True if all photos in the group share the same non-null fingerprint.

    This pattern indicates the same image was imported into Apple Photos
    multiple times (e.g., card import + iCloud sync + Snapbridge, all producing
    separate UUID records for identical file content). One copy is typically
    matched to a Flickr record; the others were never uploaded.

    DSC_* and IMG_* files are both eligible — this is a content-identity check,
    not a filename-prefix check.
    """
    if len(photos) < 2:
        return False
    fingerprints = {p.fingerprint for p in photos if p.fingerprint}
    if len(fingerprints) != 1:
        return False  # missing or differing fingerprints
    return True
```

### Classification waterfall position

```
1. _is_snapbridge_pair?    → "snapbridge"        (DSC_*, diff fp, diff dims)
2. _is_edit_pair?          → "edit_pair"          (non-DSC_*, diff fp, diff dims)
3. _is_local_duplicate?    → "local_duplicate"    (all same fp, ≥2 photos)
4. gap > 5 min?            → "device_upload"
5. pixel_ratio > 1.1?      → "not_duplicate"      (auto-dismiss)
6. otherwise               → "uncertain"
```

### Keeper assignment

No keeper designated. No photos placed in discards. All photos placed in `review`.

Notes string: `"Local duplicate: {n} copies share fingerprint {fp[:12]}… — same image imported multiple times into Apple Photos."`

Where `fp` is the shared fingerprint and `n` is `len(photos)`.

### UI changes (`reviewer/app.py`, `reviewer/templates/duplicates.html`)

**ORDER BY CASE** — insert between `edit_pair` (1) and `device_upload` (now 3):
```sql
WHEN 'local_duplicate' THEN 2
```

**Sections list** — insert between `edit_pair` and `device_upload`:
```python
(
    "local_duplicate",
    "Local duplicate",
    "Same image imported multiple times into your Photos library. "
    "One copy is already on Flickr; the others were never uploaded. "
    "Use 'Not a duplicate' to dismiss from review.",
),
```

**Badge CSS** — muted purple, visually distinct from the amber Edit Pair badge:
```css
.badge-local_duplicate { background: #1e1030; color: #c084fc; }
```

**Action-button conditional** — add `local_duplicate` alongside `snapbridge`, `edit_pair`, `device_upload`:
```html
{% if section.type in ('snapbridge', 'edit_pair', 'local_duplicate', 'device_upload', 'reupload') %}
```

Section label: `LOCAL DUPLICATE`  
Section actions: **✓ Confirm resolution** + **Not a duplicate** (same pair as Edit Pair — "Not a duplicate" is the expected common choice).

---

## Testing

### `tests/test_thumbnailer.py` (new tests)

- `test_derivative_path_masters` — uuid with derivative in masters/ → returns that path
- `test_derivative_path_shard` — masters/ missing, shard/ present → returns shard path
- `test_derivative_path_momentshared` — masters/ and shard/ missing, momentshared/ present → returns momentshared path
- `test_derivative_path_none` — no candidate exists → returns None

### `tests/test_review_ui.py` (new test)

- `test_thumb_live_fallback_writes_thumbnail_path` — photo with uuid but no thumbnail_path, derivative exists on mock filesystem → response is the file, thumbnail_path written back to DB

### `tests/test_deduplicator.py` (new tests)

- `test_is_local_duplicate_same_fingerprint` — 2 photos, same fingerprint → True
- `test_is_local_duplicate_different_fingerprints` — 2 photos, different fingerprints → False
- `test_is_local_duplicate_missing_fingerprint` — one fingerprint null → False
- `test_is_local_duplicate_single_photo` — 1 photo → False
- `test_classify_group_local_duplicate` — group classified as `local_duplicate`
- `test_local_duplicate_all_photos_in_review` — no photo in discards, all in review

---

## Out of scope

- **Deleting extra Photos records** — BP does not manage Photos library contents; the user handles that manually.
- **Merging local_duplicate members into one DB record** — the 1:1 Flickr:UUID constraint makes this non-trivial; deferred.
- **Handling groups where some fingerprints are null** — the 6 such groups route to `uncertain` unchanged; too few to warrant a special case.
- **Other derivative path patterns** — the three candidates above cover all cases observed in this collection. If edge cases emerge, `derivative_path` can be extended.
