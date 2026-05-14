# flickr-curator: EXIF machine tag backfill idea ([GH #8](https://github.com/cdevers/Blue-Pearmain/issues/8), [GH #9](https://github.com/cdevers/Blue-Pearmain/issues/9))

## Background

Years ago, a Perl script scanned EXIF tags and wrote them to Flickr as **machine tags** — structured `namespace:predicate=value` tags that made photos searchable by camera, lens, focal length, aperture, etc.

Example from [5691165623](https://www.flickr.com/photos/cdevers/5691165623/):

    exif:aperture=f/8.0
    exif:exposure=0.004 sec (1/250)
    exif:exposure_bias=0 EV
    exif:filename=DSC_8051.JPG
    exif:flash=Off, Did not fire
    exif:focal_length=18 mm
    exif:iso_speed=100
    exif:lens=18-200mm f/3.5-5.6
    camera:make=NIKON CORPORATION
    camera:model=NIKON D7000
    exif:orientation=Horizontal (normal)
    exif:shutter_count=9317
    exif:vari_program=Auto(Flash Off)
    meta:exif=1350394705

## Current state

- Tags still **display** on photo pages (confirmed via screenshot, April 2026)
- Clicking a machine tag (e.g. `flickr.com/photos/tags/camera:model=nikond7000`) goes to a **blank page** — even for a user with many matching photos
- Machine tag search appears **broken on the web UI** as of April 2026
- **Still to test:** whether the `flickr.photos.search` API with `machine_tags=` parameter returns results (API and web UI don't always match)
  - Try: `machine_tags=camera:model=NIKON+D7000`
  - Try: scoped to `user_id=me` in case global index is broken but per-user isn't
  - Try older web URL: `flickr.com/photos/search/?machine_tags=camera:model=NIKOND7000`

**Preliminary conclusion:** machine tag search may be dead, even though the tags are stored and displayed. If the API also returns nothing, the searchability use case is gone.

**Residual value if search is dead:** tags still survive as durable structured metadata attached to the photo record, visible in the UI, and present in Flickr exports. Not nothing, but much less useful than intended.

## Proposed feature for flickr-curator (Blue Pearlmain)

Add a **machine tag backfill** mode that:

1. Pulls EXIF from local source (Apple Photos metadata, sidecar, or NAS-cached EXIF)
2. Formats tags using the established `namespace:predicate=value` schema
3. Calls `flickr.photos.addTags` to write them back
4. Skips photos that already have machine tags (idempotent)

**Priority:** low until machine tag search is confirmed working or Flickr fixes it. Investigation tracked in [GH #8](https://github.com/cdevers/Blue-Pearmain/issues/8); backfill feature in [GH #9](https://github.com/cdevers/Blue-Pearmain/issues/9) (blocked on #8).

### Minimum viable tag set

| Tag | Source |
|-----|--------|
| `camera:make` | EXIF Make |
| `camera:model` | EXIF Model |
| `exif:lens` | EXIF LensModel |
| `exif:focal_length` | EXIF FocalLength |
| `exif:aperture` | EXIF FNumber |
| `exif:iso_speed` | EXIF ISO |
| `exif:exposure` | EXIF ExposureTime |
| `exif:flash` | EXIF Flash |

### Notes

- Flickr also exposes EXIF via `flickr.photos.getExif` — could use as source for photos without local files (backfill from what Flickr already knows), and to detect what is already tagged
- Reference project: [FlickrExifTagger](https://github.com/languitar/FlickrExifTagger) — probably easier to build from scratch within flickr-curator's existing architecture
- Tag format should match historical schema exactly so old searches still work if/when Flickr fixes search

## Open questions

- **Are machine tags still honored in Flickr API search?** Test with `flickr.photos.search(machine_tags='camera:model=NIKON D7000', user_id='me')`
- Does Flickr deduplicate tags on write, or will repeated runs accumulate duplicates?
- Should this be a one-shot CLI subcommand or integrated into the main curation workflow?
