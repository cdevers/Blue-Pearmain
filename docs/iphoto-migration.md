# iPhoto Library Migration — Design Notes

**Status:** Not started. This document records the problem statement and rough design ideas for later implementation.

---

## Problem

There are currently ~109,000 photos in the Blue Pearmain database that have a Flickr ID but no Apple Photos UUID. These photos were uploaded to Flickr from an older iPhoto library on a previous computer. That library has not been migrated to the current Apple Photos library.

The practical effects today:

- These photos are tracked in the database (discovered via the Flickr poller) but have no local Photos counterpart.
- The metadata sync engine skips them (filtered out by `uuid IS NOT NULL` in `_select_drift_filtered`).
- No proposals are generated for them — their Flickr tags, titles, and descriptions are orphaned from any Photos record.
- Their thumbnails in the reviewer are served from the Flickr CDN, not from a local file.

---

## Constraints

- The iPhoto library is large enough to overflow current iCloud storage capacity if imported wholesale into Apple Photos + iCloud.
- A decision about storage strategy (expand iCloud, keep offline, use a different sync approach) has not been made yet.
- Migrating iPhoto to Photos is a significant one-time operation; doing it incorrectly could result in duplicates or lost metadata.

---

## What Blue Pearmain could help with

Once the decision is made to migrate:

### Phase M-1: Pre-migration inventory
- Export a manifest of all iPhoto-only photos from the iPhoto library (by date, filename, capture device).
- Cross-reference against the Flickr database to identify which iPhoto photos are already on Flickr (and thus already tracked in the BP database).
- Identify photos that are in iPhoto but NOT on Flickr (no corresponding Flickr record) — these would be net-new to the BP database.

### Phase M-2: Deduplication before import
- Before importing into Photos, identify likely duplicates by capture timestamp + camera model + original filename.
- Flag photos that are already in the current Photos library (imported earlier via other means) to avoid creating duplicate records.

### Phase M-3: Metadata reconciliation after import
- After Photos import, run the scanner to discover newly imported photos and update `uuid` for previously Flickr-only records.
- Match imported photos to existing DB records using Flickr ID (stored in iPhoto metadata / Flickr's original upload record).
- Once UUIDs are populated, the normal metadata sync pipeline takes over: generate proposals for tag/title/description mismatches and apply them via the reviewer.

### Phase M-4: iPhoto keyword migration
- iPhoto keywords were not always migrated cleanly into Photos keywords.
- BP could read iPhoto's SQLite database directly (pre-migration) to extract keyword associations and write them as explicit Photos keywords post-import.

---

## Near-term (before migration)

No code changes needed now. The 109k Flickr-only photos are:

- Counted and visible on the dashboard ("X photos on Flickr with no Photos counterpart").
- Excluded from all proposal generation (correct behavior — nothing to sync to).
- Accessible in the reviewer with Flickr CDN thumbnails.

If a user navigates to one of these photos in the reviewer, the detail page notes "No Apple Photos counterpart" instead of offering a "Photos ↗" link.

---

## Open questions

- Storage strategy for the iPhoto library post-migration (iCloud vs local-only).
- Whether to import the full iPhoto library at once or in batches by year/album.
- How to handle photos that are in iPhoto AND in current Photos (potential duplicates from partial earlier migrations).
- Whether iPhoto's "smart album" definitions should be imported as regular albums.
