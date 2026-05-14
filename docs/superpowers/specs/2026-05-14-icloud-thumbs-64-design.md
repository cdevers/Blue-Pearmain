# iCloud-Only Thumbnail Resolution — Design Spec (GH #64)

**Goal:** Stop `bp thumbs` from silently skipping the same 845 iCloud-only records every run. Automatically trigger iCloud downloads for missing originals, resolve what downloads quickly, and report the rest clearly so the user knows progress is happening.

---

## Problem

`bp thumbs` resolves thumbnails from two sources: local Photos derivative files and Flickr CDN URLs. Photos-only records whose originals live in iCloud but have never been downloaded to the Mac have neither — no local derivative, no Flickr ID. Every run reports the same aggregate skip count with no indication of what's happening or whether anything will ever change.

---

## Solution overview

Two new phases added to `run()` in `poller/thumbnailer.py`:

1. **Phase 0** — open osxphotos `PhotosDB` once, build a `uuid → PhotoInfo` map for Photos-only records that need thumbnails.
2. **Phase 2** — after the main loop, wait up to 60 seconds for background downloads, then retry once.

The existing main loop (Phase 1) is extended: when a record has no derivative and no Flickr URL, check whether it is iCloud-only via osxphotos. If so, submit a background download rather than immediately skipping it.

---

## Phase 0 — osxphotos initialisation

Before the main loop, check whether any records needing thumbnails are Photos-only (`uuid IS NOT NULL`, `flickr_id IS NULL`). If so, open `osxphotos.PhotosDB(dbfile=library_path)` and build:

```python
uuid_to_photo: dict[str, PhotoInfo] = {
    p.uuid: p
    for p in photosdb.photos(uuid=photos_only_uuids)
}
```

If there are no Photos-only records needing thumbnails, skip this entirely — `bp thumbs` on a Flickr-heavy run pays no overhead.

---

## Phase 1 — main loop (modified)

Existing logic unchanged for records that resolve via local derivative or Flickr URL.

For records with a UUID and no resolved thumbnail, look up the photo in `uuid_to_photo`:

```python
photo = uuid_to_photo.get(uuid)
if photo and photo.iscloudasset and photo.ismissing:
    future = executor.submit(photo.export, tmpdir, use_photos_export=True)
    icloud_pending.append((row_id, uuid, future))
    # do not increment skipped — this is pending, not skipped
else:
    skipped += 1
```

`executor` is a `ThreadPoolExecutor(max_workers=4)` created before the loop. `tmpdir` is a `tempfile.mkdtemp()` created before the loop and cleaned up after Phase 2.

`use_photos_export=True` routes through Photos.app's native export API, which downloads the original from iCloud if it is not present locally. This requires Photos.app to be running; if it is not, the export call will fail and the record falls into the `icloud_queued` bucket.

---

## Phase 2 — wait and retry

After the main loop completes:

```python
futures = [f for _, _, f in icloud_pending]
concurrent.futures.wait(futures, timeout=60)
```

This waits up to 60 seconds **total** for any of the background downloads to finish. Downloads that complete within this window are immediately usable; the rest will be retried on the next `bp thumbs` run.

Then one retry pass:

```python
icloud_resolved = icloud_queued = 0
for row_id, uuid, _ in icloud_pending:
    thumb = derivative_path(uuid, library_path)
    if thumb:
        icloud_resolved += 1
        if not dry_run:
            db.conn.execute(
                "UPDATE photos SET thumbnail_path = ?, display_rotation = 0 WHERE id = ?",
                (thumb, row_id),
            )
    else:
        icloud_queued += 1
```

After the retry pass:

```python
executor.shutdown(wait=False)   # don't block on stragglers
shutil.rmtree(tmpdir, ignore_errors=True)
```

Exported files in `tmpdir` are discarded — they are a side-effect of triggering the Photos download and are not used directly. The derivative JPEG at `resources/derivatives/masters/{shard}/{uuid}_4_5005_c.jpeg` is what `bp thumbs` reads.

---

## Output

Summary line updated:

```
Done: N local derivatives, N Flickr URLs, N downloaded,
      N iCloud resolved, N iCloud queued (run again), N skipped
```

`iCloud resolved` = photos whose download completed within the 60s window and whose derivative was found on retry.  
`iCloud queued` = photos whose download did not complete in time; derivative will be available on a future run.  
`skipped` = records with no UUID, or a UUID that is not in the Photos library, or any other unresolvable case.

`iCloud queued` and `iCloud resolved` are omitted from the line when both are zero (no iCloud processing occurred).

---

## No new flags or schema changes

The iCloud download path activates automatically whenever Photos-only records are present. No `--download-icloud` flag. No new DB columns.

---

## Testing

All tests mock osxphotos and `photo.export` — no live Photos.app or iCloud access required.

| Test | Expected behaviour |
|------|--------------------|
| iCloud photo whose download completes within timeout | derivative found on retry, `thumbnail_path` set, `display_rotation = 0` |
| iCloud photo whose download does not complete in time | counted as `icloud_queued`, DB unchanged |
| `photo.export` raises an exception | counted as `icloud_queued`, no crash |
| No Photos-only records needing thumbnails | osxphotos never opened |
| Existing local-derivative and Flickr-URL paths | unchanged behaviour |
| `--dry-run` with iCloud photos | exports triggered, DB not written |
| `skipped` count excludes iCloud-pending records | only genuinely unresolvable records counted |
